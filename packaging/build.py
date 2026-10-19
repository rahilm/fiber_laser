"""Build the mlaser one-folder bundle with PyInstaller (Windows, macOS .app, Linux).

``python packaging/build.py [--smoke] [--archive] [--slim] [--out packaging/build]``

Steps:
1. PyInstaller with packaging/mlaser.spec -> ``<out>/dist/mlaser/`` (and ``mlaser.app`` on macOS);
2. LICENSES/ (packaging/gen_licenses.py, from the bundled distributions' metadata) next to the
   executables (``Contents/Resources/LICENSES`` in the .app);
3. LGPL check: fails when a GPL-only Qt module or a onefile build slipped in;
4. size report (``<out>/build-info.json``);
5. ``--smoke``: run the console executable (with ``MLASER_NATIVE=1``: the C emitter kernel
   must be bundled and pass its self-check) with ``--bundle-check tests/data/dxf/gear.dxf``,
   ``QT_QPA_PLATFORM=offscreen --demo --smoke-test 5`` and ``--selftest`` (informative);
6. ``--archive``: ``<out>/mlaser-<version>-<os>-<arch>.zip|tar.gz`` (keeps symlinks and
   executable bits, unlike uploading the folder).

Signing is separate (packaging/sign_windows.ps1, packaging/sign_macos.sh) and must run before
``--archive`` if you sign locally; CI does: build -> sign -> smoke -> archive.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import bundle_config as cfg      # noqa: E402
import gen_licenses              # noqa: E402

APP = "mlaser"


def os_tag() -> str:
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def layout(dist: Path) -> dict:
    """Paths inside the built bundle."""
    exe_suffix = ".exe" if sys.platform.startswith("win") else ""
    folder = dist / APP
    out = {"folder": folder, "contents": folder / "_internal", "licenses": folder / "LICENSES",
           "gui": folder / (APP + exe_suffix), "console": folder / (APP + "-console" + exe_suffix),
           "app": None}
    if sys.platform == "darwin":
        app = dist / (APP + ".app")
        out.update(app=app, contents=app / "Contents" / "Frameworks",
                   licenses=app / "Contents" / "Resources" / "LICENSES",
                   gui=app / "Contents" / "MacOS" / APP,
                   console=app / "Contents" / "MacOS" / (APP + "-console"))
    return out


def run_pyinstaller(dist: Path, work: Path, clean: bool) -> None:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--log-level", "WARN",
           "--distpath", str(dist), "--workpath", str(work), str(HERE / "mlaser.spec")]
    if clean:
        cmd.insert(3, "--clean")
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def tree_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def lgpl_check(lay: dict) -> list[str]:
    """Problems that would break the LGPL setup (empty list = fine)."""
    problems = []
    contents: Path = lay["contents"]
    if not contents.is_dir():
        problems.append(f"no contents folder {contents}: not a onedir build?")
        return problems
    qt_core = [p for p in contents.rglob("*") if cfg.qt_module_of(p.name) == "Core"]
    if not qt_core:
        problems.append("Qt Core is not a separate shared library in the bundle")
    for p in contents.rglob("*"):
        if cfg.is_gpl_only(p.name):
            problems.append(f"GPL-only Qt module in the bundle: {p.relative_to(contents)}")
    if not (lay["licenses"] / "Qt-PySide6-NOTICE.txt").is_file():
        problems.append("LICENSES/Qt-PySide6-NOTICE.txt missing")
    return problems


def size_report(lay: dict, top: int = 15) -> dict:
    root: Path = lay["app"] or lay["folder"]
    contents: Path = lay["contents"]
    groups: dict[str, int] = {}
    for f in contents.rglob("*"):
        if f.is_file() and not f.is_symlink():
            rel = f.relative_to(contents).parts
            key = "/".join(rel[:3]) if rel[0] == "PySide6" and len(rel) > 3 else rel[0]
            groups[key] = groups.get(key, 0) + f.stat().st_size
    biggest = sorted(groups.items(), key=lambda kv: -kv[1])[:top]
    return {"total_bytes": tree_size(root), "root": str(root),
            "largest": [{"path": k, "bytes": v} for k, v in biggest]}


def _is_elf(p: Path) -> bool:
    try:
        with open(p, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def _elf_files(root: Path) -> list[Path]:
    return sorted(f for f in root.rglob("*") if f.is_file() and not f.is_symlink() and _is_elf(f))


def strip_linux(contents: Path) -> dict:
    """``strip --strip-unneeded`` the shared libraries and extension modules (Linux; about 43 MB of
    symbol tables and debug info, Review 2 P2-8).  Not touched: the executables (the bootloader
    carries the appended archive), PySide6/shiboken6 (Qt and its bindings stay the unmodified
    LGPL libraries, packaging/README.md) and files with a .hmac/.chk companion (PyInstaller's
    own rule: checksummed libraries).  Also not touched: libraries vendored by wheels in ``*.libs``
    directories (numpy.libs, shapely.libs, ortools/.libs ...): auditwheel rewrote them with
    patchelf, and stripping such a file can leave its load segments misaligned ("ELF load command
    address/offset not page-aligned": numpy's OpenBLAS in the first Review 2 Package run)."""
    before = after = n = 0
    for f in _elf_files(contents):
        rel = f.relative_to(contents).parts
        if rel[0] in ("PySide6", "shiboken6") or any(part.endswith(".libs") for part in rel[:-1]) \
                or f.with_name(f.name + ".hmac").exists() or f.with_suffix(".chk").exists():
            continue
        size = f.stat().st_size
        subprocess.run(["strip", "--strip-unneeded", str(f)], check=True)
        before += size
        after += f.stat().st_size
        n += 1
    return {"files": n, "bytes_before": before, "bytes_saved": before - after}


_VER = __import__("re").compile(r"\b(GLIBC|GLIBCXX|CXXABI)_(\d+(?:\.\d+)*)\b")


def symbol_floor(root: Path) -> dict:
    """Linux: the newest glibc / libstdc++ symbol versions any ELF file in the bundle requires
    (``objdump -p`` version references), with one file that needs each.  The bundle runs on a
    target whose glibc and libstdc++ provide these (docs/dev/RELEASE.md, "glibc floor")."""
    best: dict[str, tuple] = {}
    for f in _elf_files(root):
        p = subprocess.run(["objdump", "-p", str(f)], capture_output=True, text=True)
        refs = p.stdout.split("Version References:", 1)
        if len(refs) < 2:
            continue
        for lib, ver in _VER.findall(refs[1]):
            key = tuple(int(x) for x in ver.split("."))
            if lib not in best or key > best[lib][0]:
                best[lib] = (key, str(f.relative_to(root)))
    return {lib: {"version": ".".join(map(str, k)), "file": f} for lib, (k, f) in sorted(best.items())}


def host_libs(work: Path) -> list[str]:
    """Libraries in the final bundle that PyInstaller took from the build host's system
    directories (Linux; should be only bundle_config.HOST_LIB_KEEP)."""
    toc = work / "mlaser" / "COLLECT-00.toc"
    if not toc.is_file():
        return []
    import ast

    def walk(o):
        if isinstance(o, (list, tuple)):
            if len(o) == 3 and all(isinstance(x, str) for x in o):
                yield o
            else:
                for x in o:
                    yield from walk(x)
    return sorted({d for d, src, ty in walk(ast.literal_eval(toc.read_text(encoding="utf-8")))
                   if ty in ("BINARY", "EXTENSION") and src.startswith(cfg.HOST_LIB_DIRS)})


def smoke(lay: dict, out: Path, seconds: float = 5.0) -> list[dict]:
    exe = lay["console"]
    # the C emitter kernel must be in the bundle and pass its self-check (bundle check FAIL
    # otherwise); MLASER_NATIVE=auto in the environment accepts a bundle without it
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", MLASER_CONFIG_DIR=str(out / "cfg"),
               MLASER_NATIVE=os.environ.get("MLASER_NATIVE", "1"))
    results = []
    sample = ROOT / "tests" / "data" / "dxf" / "gear.dxf"
    check = ["--bundle-check"] + ([str(sample)] if sample.is_file() else [])
    for args in (check, ["--demo", "--smoke-test", str(seconds)], ["--selftest"]):
        t0 = time.perf_counter()
        p = subprocess.run([str(exe), *args], env=env, capture_output=True, text=True,
                           timeout=seconds + 120)
        results.append({"args": args, "rc": p.returncode, "seconds": round(time.perf_counter() - t0, 2),
                        "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:]})
        print(f"+ {exe.name} {' '.join(args)} -> exit {p.returncode} "
              f"({results[-1]['seconds']} s)\n{p.stdout}{p.stderr}", flush=True)
    return results


def archive(lay: dict, out: Path) -> Path:
    version = cfg.version(ROOT)
    arch = platform.machine().lower() or "unknown"
    base = out / f"{APP}-{version}-{os_tag()}-{arch}"
    src: Path = lay["app"] or lay["folder"]
    fmt = "zip" if sys.platform.startswith("win") else "gztar"
    return Path(shutil.make_archive(str(base), fmt, root_dir=src.parent, base_dir=src.name))


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=HERE / "build", help="build folder (git-ignored)")
    ap.add_argument("--no-clean", action="store_true", help="reuse PyInstaller's cache")
    ap.add_argument("--slim", action="store_true", help="leave out OR-Tools/spyrrow/Ruckig")
    ap.add_argument("--smoke", action="store_true", help="run the frozen smoke test and self-test")
    ap.add_argument("--archive", action="store_true", help="pack the bundle into an archive")
    ap.add_argument("--skip-build", action="store_true", help="use the existing build as it is (after signing): only check, smoke-test, archive")
    ap.add_argument("--no-strip", action="store_true", help="Linux: keep the symbol tables")
    ap.add_argument("--glibc-floor", metavar="X.Y",
                    help="Linux: fail when a bundled file needs a newer glibc than X.Y")
    ap.add_argument("--summary", type=Path, metavar="FILE",
                    help="append a Markdown summary (sizes, backends, smoke results) to FILE, "
                         "e.g. $GITHUB_STEP_SUMMARY")
    a = ap.parse_args(argv)

    out = a.out.resolve()
    dist, work = out / "dist", out / "work"
    if a.slim:
        os.environ["MLASER_BUNDLE_MODERN"] = "0"
    t0 = time.perf_counter()
    if not a.skip_build:
        run_pyinstaller(dist, work, clean=not a.no_clean)
    build_s = time.perf_counter() - t0
    lay = layout(dist)
    stripped = None
    if not a.skip_build and sys.platform.startswith("linux") and not a.no_strip:
        stripped = strip_linux(lay["contents"])
        print(f"strip: {stripped['files']} files, {stripped['bytes_saved'] / 1e6:.1f} MB saved")

    if a.skip_build:
        # after signing: never touch the bundle again (it would break the signature)
        names = sorted(p.name for p in (lay["licenses"] / "third_party").glob("*"))
    else:
        toc = work / "mlaser" / "PYZ-00.toc"
        modules = gen_licenses.pyz_modules(toc) if toc.is_file() else None
        extra_roots = [lay["app"] / "Contents" / "Resources"] if lay["app"] else []
        names = gen_licenses.generate(lay["licenses"], lay["contents"], modules=modules,
                                      more_bundles=extra_roots)
        if lay["app"] is not None:
            # the plain folder next to the .app gets the notices too (it is the same build)
            gen_licenses.generate(lay["folder"] / "LICENSES", lay["folder"] / "_internal",
                                  modules=modules)
        print(f"licences: {len(names)} distributions -> {lay['licenses']}")

    problems = lgpl_check(lay)
    for p in problems:
        print("LGPL CHECK:", p, file=sys.stderr)
    info = {"version": cfg.version(ROOT), "python": platform.python_version(), "os": os_tag(),
            "build_seconds": round(build_s, 1), "distributions": names,
            "size": size_report(lay), "lgpl_problems": problems}
    total = info["size"]["total_bytes"]
    print(f"bundle: {info['size']['root']}  {total / 1e6:.1f} MB, build {build_s:.0f} s")
    for item in info["size"]["largest"]:
        print(f"  {item['bytes'] / 1e6:8.1f} MB  {item['path']}")
    linux_problems: list[str] = []
    if sys.platform.startswith("linux"):
        info["strip"] = stripped
        info["host_libs"] = host_libs(work)
        info["symbol_floor"] = floor = symbol_floor(lay["folder"])
        print("host system libraries in the bundle:", ", ".join(info["host_libs"]) or "none")
        print("symbol floor: " + ", ".join(f"{k} {v['version']} ({v['file']})" for k, v in floor.items()))
        unexpected = [h for h in info["host_libs"] if not cfg.HOST_LIB_KEEP.match(Path(h).name)]
        if unexpected:
            linux_problems.append(f"host system libraries in the bundle: {unexpected}")
        need = floor.get("GLIBC", {}).get("version")
        if a.glibc_floor and need and _vkey(need) > _vkey(a.glibc_floor):
            linux_problems.append(f"the bundle needs glibc {need} ({floor['GLIBC']['file']}), "
                                  f"newer than the floor {a.glibc_floor}")
        for p in linux_problems:
            print("BUNDLE CHECK:", p, file=sys.stderr)
        info["linux_problems"] = linux_problems
    info["files"] = sum(1 for f in (lay["app"] or lay["folder"]).rglob("*") if not f.is_dir())
    rc = 1 if problems or linux_problems else 0
    if a.smoke:
        info["smoke"] = smoke(lay, out)
        if any(r["rc"] != 0 for r in info["smoke"][:2]):   # the self-test may fail offline
            rc = 1
    if a.archive:
        arc = archive(lay, out)
        info["archive"] = str(arc)
        info["archive_bytes"] = arc.stat().st_size
        print(f"archive: {arc}  {arc.stat().st_size / 1e6:.1f} MB")
    if a.skip_build and (out / "build-info.json").is_file():
        prev = json.loads((out / "build-info.json").read_text(encoding="utf-8"))
        info["build_seconds"] = prev.get("build_seconds", info["build_seconds"])
        info["distributions"] = prev.get("distributions", names)
        if prev.get("strip") is not None:
            info["strip"] = prev["strip"]
    (out / "build-info.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(summary_markdown(info))
    return rc


def summary_markdown(info: dict) -> str:
    """The numbers docs/dev/RELEASE.md tracks per OS, for the CI job summary."""
    mb = lambda b: f"{b / 1e6:.1f} MB" if b else "-"      # noqa: E731
    rows = [("OS", info["os"]), ("folder", mb(info["size"]["total_bytes"])),
            ("files", str(info.get("files", "-"))), ("archive", mb(info.get("archive_bytes")))]
    if "symbol_floor" in info:
        rows.append(("symbol floor", ", ".join(f"{k} {v['version']}" for k, v in info["symbol_floor"].items())))
        rows.append(("host libraries", ", ".join(info.get("host_libs") or []) or "none"))
    out = [f"### mlaser bundle ({info['os']})", "", "| | |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    for r in info.get("smoke", []):
        out += ["", f"`{' '.join(r['args'])}` -> exit {r['rc']}", "", "```",
                (r["stdout"] + r["stderr"]).strip()[-3000:], "```"]
    return "\n".join(out) + "\n\n"


if __name__ == "__main__":
    sys.exit(main())
