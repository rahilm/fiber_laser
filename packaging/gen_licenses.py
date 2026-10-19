"""Write the LICENSES/ folder of a bundle from the installed distributions' licence metadata.

``python packaging/gen_licenses.py OUT_DIR [--bundle CONTENTS_DIR]``

* ``mlaser-LICENSE.txt``: the project's MIT licence;
* ``Qt-PySide6-NOTICE.txt``: which Qt / PySide6 version is shipped, that they are LGPL-3.0,
  where the source is and how to replace the Qt libraries in the bundle;
* ``LGPL-3.0.txt`` and ``GPL-3.0.txt`` (LGPL-3.0 is a set of additional permissions on top of
  the GPL-3.0, so both texts must accompany the libraries);
* ``THIRD_PARTY_NOTICES.txt``: one entry per bundled distribution (name, version, licence
  from ``License-Expression`` / ``License`` / classifiers, home page) followed by the full text
  of every licence file listed in its ``.dist-info`` (PEP 639 ``License-File``, or the usual
  LICENSE/COPYING names), plus the CPython licence and the vendored Tabler icons licence;
* ``third_party/<name>-<version>/``: the same licence files, unmodified.

A distribution counts as bundled when one of its top-level import names exists in
``--bundle`` (PyInstaller's contents folder: ``_internal`` or ``Contents/Frameworks``); without
``--bundle`` the runtime dependency closure of mlaser (installed extras included) is used.
Run at every build so the notices match what is actually shipped.
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import re
import shutil
import sys
import sysconfig
from pathlib import Path
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# build tools and test/dev-only distributions: never part of the shipped program
NEVER = {"mlaser", "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "setuptools", "pip",
         "packaging", "pytest", "hypothesis", "matplotlib", "mypy", "import-linter", "ruff",
         "pre-commit", "virtualenv"}
# dual-licensed distributions: the option this program uses
CHOSEN = {"hidapi": " (used under the BSD option)", "pyside6-essentials": " (used under LGPL-3.0)",
          "pyside6": " (used under LGPL-3.0)", "shiboken6": " (used under LGPL-3.0)",
          "pyside6-addons": " (used under LGPL-3.0)"}
LICENSE_NAMES = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS|COPYRIGHT)", re.IGNORECASE)

QT_NOTICE = """\
Qt and Qt for Python (PySide6) in this program
==============================================

This program uses the Qt {qt} libraries and Qt for Python (PySide6 {pyside}, Shiboken6
{shiboken}), (C) The Qt Company Ltd. and other contributors, under the GNU Lesser General Public
License version 3 (LGPL-3.0, see LGPL-3.0.txt and GPL-3.0.txt in this folder).  The Qt
libraries are not modified.

Source code
-----------
* Qt {qt}:      https://download.qt.io/official_releases/qt/{qt_minor}/{qt}/single/
* PySide6 {pyside}: https://download.qt.io/official_releases/QtForPython/pyside6/
                (PySide6 / Shiboken6 source archive "pyside-setup-everywhere-src-{pyside}")
* git:           https://code.qt.io/cgit/qt/qtbase.git  https://code.qt.io/cgit/pyside/pyside-setup.git
The mlaser source code (MIT, see mlaser-LICENSE.txt) is published by the project; this build's
version is {version}.  If you received this program without a link to the corresponding Qt
source, you may ask the distributor for it.

Replacing the Qt libraries
--------------------------
The program is a "one-folder" build: Qt is kept as ordinary shared libraries next to the
program and is loaded at start-up, so you can replace it with your own (modified) build of the
same Qt {qt_minor}.x series (binary compatible with Qt {qt}):

{locations}

1. Build Qt {qt_minor}.x (and, if you change the Python bindings, PySide6 {pyside}) from source.
2. Replace the Qt library files listed above with yours, keeping their file names
   (and on Windows the plugins under the "plugins" folder next to them).
3. On macOS the application bundle is code-signed: after replacing files re-sign it, e.g.
   "codesign --force --deep --sign - mlaser.app" (ad-hoc) or with your own identity.
4. Start the program as usual; it uses the replaced libraries.

You may also run mlaser from its source code against any Qt/PySide6 installation
("pip install PySide6" or your own build): see the project's README.
"""


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _meta(dist: md.Distribution, key: str) -> list[str]:
    return list(dist.metadata.get_all(key) or [])


def licence_of(dist: md.Distribution) -> str:
    expr = dist.metadata.get("License-Expression")
    if expr:
        return str(expr)
    lic = (dist.metadata.get("License") or "").strip()
    classifiers = [c.split("::")[-1].strip() for c in _meta(dist, "Classifier")
                   if c.startswith("License ::")]
    if lic and "\n" not in lic and len(lic) < 120:
        return lic
    return " / ".join(classifiers) or (lic.splitlines()[0][:120] if lic else "UNKNOWN")


def homepage_of(dist: md.Distribution) -> str:
    hp = dist.metadata.get("Home-page") or ""
    if hp:
        return str(hp)
    for url in _meta(dist, "Project-URL"):
        label, _, link = url.partition(",")
        if label.strip().lower() in ("homepage", "home", "source", "repository", "source code"):
            return link.strip()
    urls = _meta(dist, "Project-URL")
    return urls[0].partition(",")[2].strip() if urls else ""


def licence_files(dist: md.Distribution) -> list[Path]:
    """Licence files in the distribution's .dist-info (PEP 639 or conventional names)."""
    out: list[Path] = []
    declared = set(_meta(dist, "License-File"))
    for f in dist.files or []:
        parts = f.parts
        if not parts or not parts[0].endswith(".dist-info"):
            continue
        rel = "/".join(parts[1:])
        inside_licenses = len(parts) > 2 and parts[1] == "licenses"
        if inside_licenses or rel in declared or LICENSE_NAMES.match(parts[-1]):
            p = Path(str(dist.locate_file(f)))
            if p.is_file() and p not in out:
                out.append(p)
    return out


def top_level_names(dist: md.Distribution) -> set[str]:
    names: set[str] = set()
    tl = dist.read_text("top_level.txt")
    if tl:
        names.update(n.strip() for n in tl.split() if n.strip())
    for f in dist.files or []:
        first = f.parts[0] if f.parts else ""
        if not first or first.endswith((".dist-info", ".egg-info", ".pth")) or first in ("..", "__pycache__"):
            continue
        names.add(first.split(".")[0] if "." in first and not first.endswith(".libs") else first)
    return names


def dependency_closure(root: str = "mlaser") -> list[md.Distribution]:
    seen: dict[str, md.Distribution] = {}
    todo = [root]
    while todo:
        name = _norm(todo.pop())
        if name in seen:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        seen[name] = dist
        for req in dist.requires or []:
            req_name = re.split(r"[\s;\[<>=!~(]", req, maxsplit=1)[0]
            todo.append(req_name)    # extras included when installed (closure over what exists)
    return list(seen.values())


def _module_name(rel: str) -> str:
    """'svgelements/svgelements.py' -> 'svgelements.svgelements' ('' for non-modules)."""
    if not rel.endswith(".py"):
        return ""
    mod = rel[:-3].replace("/", ".")
    return mod[:-9] if mod.endswith(".__init__") else mod


def bundled_distributions(bundles: Optional[list[Path]] = None,
                          modules: Optional[set[str]] = None) -> list[md.Distribution]:
    """Distributions with at least one file in the bundle: an extension module, shared library
    or data file present under one of ``bundles``, or a Python module in ``modules`` (the
    PYZ archive's module names).  Without ``bundles`` the mlaser dependency closure."""
    if not bundles:
        dists = dependency_closure()
    else:
        dists = []
        mods = modules or set()
        owners: dict[str, int] = {}
        all_dists = list(md.distributions())
        for d in all_dists:
            for f in d.files or []:
                owners["/".join(f.parts)] = owners.get("/".join(f.parts), 0) + 1
        for d in all_dists:
            for f in d.files or []:
                if not f.parts or f.parts[0].endswith(".dist-info") or f.parts[0] == "..":
                    continue
                rel = "/".join(f.parts)
                if rel.endswith(".pyc") or owners.get(rel, 0) > 1:   # e.g. PySide6/__init__.py
                    continue
                mod = _module_name(rel)
                if (mod and mod in mods) or (not mod and any((b / rel).exists() for b in bundles)):
                    dists.append(d)
                    break
    out: dict[str, md.Distribution] = {}
    for d in dists:
        n = _norm(d.metadata["Name"])
        if n not in NEVER and n not in out:
            out[n] = d
    return sorted(out.values(), key=lambda d: _norm(d.metadata["Name"]))


def pyz_modules(toc: Path) -> set[str]:
    """Module names in PyInstaller's PYZ-00.toc (a Python literal of (name, path, type) tuples)."""
    import ast
    data = ast.literal_eval(toc.read_text(encoding="utf-8"))
    entries = data[-1] if isinstance(data, tuple) else data
    return {e[0] for e in entries if isinstance(e, (tuple, list)) and e}


def _python_licence() -> Optional[Path]:
    for cand in (Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt",
                 Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"):
        if cand.is_file():
            return cand
    return None


def _version(name: str) -> str:
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return "?"


def qt_locations(bundle: Optional[Path]) -> str:
    if bundle is not None:
        hits = sorted({str(p.parent.relative_to(bundle.parent)) for p in bundle.rglob("*")
                       if re.match(r"^(lib)?Qt6?Core(\.so[.\d]*|\.dll|\.dylib)?$|^QtCore\.framework$",
                                   p.name)})
        if hits:
            return "\n".join(f"   this bundle: {h}" for h in hits)
    return ("   Windows: mlaser\\_internal\\PySide6\\Qt6*.dll (plugins in ...\\PySide6\\plugins)\n"
            "   macOS:   mlaser.app/Contents/Frameworks/PySide6/Qt/lib/Qt*.framework\n"
            "   Linux:   mlaser/_internal/PySide6/Qt/lib/libQt6*.so.6")


def qt_version() -> str:
    try:
        from PySide6 import QtCore   # build environment only
        return str(QtCore.qVersion())
    except Exception:
        return _version("PySide6-Essentials").split("+")[0]


def generate(out: Path, bundle: Optional[Path] = None, extra: Iterable[tuple[str, Path]] = (),
             modules: Optional[set[str]] = None, more_bundles: Iterable[Path] = ()) -> list[str]:
    """Write the LICENSES folder; returns the names of the distributions listed."""
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "third_party").mkdir(parents=True)
    shutil.copyfile(ROOT / "LICENSE", out / "mlaser-LICENSE.txt")
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        shutil.copyfile(HERE / "licenses" / name, out / name)
    qt = qt_version()
    proj_version = _version("mlaser")
    (out / "Qt-PySide6-NOTICE.txt").write_text(QT_NOTICE.format(
        qt=qt, qt_minor=".".join(qt.split(".")[:2]), pyside=_version("PySide6"),
        shiboken=_version("shiboken6"), version=proj_version, locations=qt_locations(bundle)),
        encoding="utf-8")

    roots = [bundle, *more_bundles] if bundle is not None else []
    dists = bundled_distributions(roots, modules)
    summary = ["Third-party software in this mlaser build", "=" * 41, "",
               f"mlaser {proj_version} is MIT-licensed (mlaser-LICENSE.txt). It bundles:", ""]
    body: list[str] = []
    names: list[str] = []
    for d in dists:
        name, ver = d.metadata["Name"], d.version
        names.append(name)
        lic, hp = licence_of(d), homepage_of(d)
        files = licence_files(d)
        if lic == "UNKNOWN" and files:
            first = next((ln.strip() for ln in files[0].read_text(encoding="utf-8", errors="replace")
                          .splitlines() if ln.strip()), "")
            lic = f"see {files[0].name} ({first[:60]})" if first else lic
        lic += CHOSEN.get(_norm(name), "")
        summary.append(f"  {name} {ver}: {lic}" + (f"  <{hp}>" if hp else ""))
        dest = out / "third_party" / f"{name}-{ver}"
        dest.mkdir(parents=True, exist_ok=True)
        body += ["", "-" * 78, f"{name} {ver}", f"Licence: {lic}"] + ([f"Home: {hp}"] if hp else [])
        if not files:
            body.append("(no licence file in the distribution metadata; see the home page)")
        for f in files:
            shutil.copyfile(f, dest / f.name)
            body += ["", f"--- {f.name} ---", f.read_text(encoding="utf-8", errors="replace").rstrip()]
    py = _python_licence()
    pyver = ".".join(map(str, sys.version_info[:3]))
    summary.append(f"  CPython {pyver}: PSF-2.0  <https://www.python.org/>")
    if py:
        shutil.copyfile(py, out / "third_party" / f"CPython-{pyver}-LICENSE.txt")
        body += ["", "-" * 78, f"CPython {pyver}", "Licence: PSF-2.0", "",
                 py.read_text(encoding="utf-8", errors="replace").rstrip()]
    tabler = ROOT / "mlaser" / "ui" / "resources" / "icons" / "tabler" / "LICENSE"
    extras = list(extra)
    if tabler.is_file():
        extras.insert(0, ("Tabler Icons (mlaser/ui/resources/icons/tabler, MIT)", tabler))
    for title, path in extras:
        summary.append(f"  {title}")
        shutil.copyfile(path, out / "third_party" / (re.sub(r"\W+", "_", title.split("(")[0]).strip("_")
                                                      + "-LICENSE.txt"))
        body += ["", "-" * 78, title, "", path.read_text(encoding="utf-8", errors="replace").rstrip()]
    summary += ["", "Qt / PySide6 / Shiboken6 are LGPL-3.0: see Qt-PySide6-NOTICE.txt.",
                "Libraries bundled inside wheels (e.g. GEOS in shapely: LGPL-2.1; OpenBLAS and the",
                "GCC runtime in numpy; libjpeg, libpng, FreeType, HarfBuzz in Pillow) are covered by",
                "the licence files of those wheels, reproduced below.  They are shared libraries too.",
                "The PyInstaller bootloader is GPL-2.0 with the bootloader exception (no obligations",
                "for the program it starts)."]
    (out / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(summary + body) + "\n", encoding="utf-8")
    return names


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--bundle", type=Path, help="PyInstaller contents folder (_internal)")
    ap.add_argument("--pyz-toc", type=Path, help="PyInstaller work folder's PYZ-00.toc (module list)")
    a = ap.parse_args(argv)
    names = generate(a.out, a.bundle, modules=pyz_modules(a.pyz_toc) if a.pyz_toc else None)
    print(f"wrote {a.out} ({len(names)} distributions: {', '.join(names)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
