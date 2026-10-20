# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one-folder (onedir) build of mlaser for Windows, macOS (.app) and Linux.

Build with ``python packaging/build.py`` (it runs this spec, then the licence and size checks),
or directly ``pyinstaller --noconfirm --distpath packaging/build/dist --workpath
packaging/build/work packaging/mlaser.spec``.

Why onedir, never onefile: Qt (LGPL-3.0) must stay a set of replaceable shared libraries next to
the program (``_internal/PySide6/Qt/lib`` on Linux, ``_internal/PySide6`` on Windows,
``Contents/Frameworks/PySide6/Qt/lib`` in the macOS bundle), see packaging/README.md.

Two executables share one folder:
* ``mlaser``: the GUI (windowed on Windows/macOS: no console window);
* ``mlaser-console``: the same program with a console, for ``--selftest`` / ``--smoke-test``
  output and exit codes (on Linux a symlink to ``mlaser``: both would behave the same).

Environment knobs:
* ``MLASER_BUNDLE_MODERN=0``: leave out the optional modern libraries (OR-Tools ~45 MB, spyrrow,
  Ruckig) even when installed;  default: bundled when installed (they are imported lazily, so
  PyInstaller would not find them by itself).
* ``MLASER_BUNDLE_DEVICES=0``: leave out hidapi / pyserial.
"""
import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH).resolve()          # noqa: F821 (SPECPATH is set by PyInstaller)
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import bundle_config as cfg               # noqa: E402  (packaging/bundle_config.py)

APP = "mlaser"
IS_WIN, IS_MAC = sys.platform.startswith("win"), sys.platform == "darwin"
IS_LINUX = not (IS_WIN or IS_MAC)


def have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


# ------------------------------------------------------------------ data files
datas = []
# mlaser package data: the [tool.setuptools.package-data] of pyproject.toml, plus any fonts that
# may sit under mlaser/cad at build time
datas += collect_data_files("mlaser.ui", includes=["resources/*.json", "resources/icons/tabler/*",
                                               "resources/icons/app/*"])
datas += collect_data_files("mlaser.i18n", includes=["*.json", "qm/*.qm"])
datas += collect_data_files("mlaser.formats", includes=["data/*"])
datas += collect_data_files("mlaser.cad", includes=["**/*.ttf", "**/*.otf", "**/*.ttc", "**/*.shx",
                                                     "**/*.lff", "**/LICENSE*", "**/OFL*"])
# the help documents (docs/user, dev, re): Help reads them from mlaser/help/docs
datas += cfg.help_datas(ROOT)
# third-party data (fonts/resources of ezdxf, svgelements, fontTools, shapely ...)
for pkg in cfg.DATA_PACKAGES:
    if have(pkg):
        datas += collect_data_files(pkg, excludes=cfg.DATA_EXCLUDES)

# ------------------------------------------------------------------ hidden imports
hiddenimports = collect_submodules("mlaser", filter=cfg.keep_mlaser_module)
hiddenimports += [m for m in cfg.HIDDEN_IMPORTS if have(m.split(".")[0])]
MODERN = os.environ.get("MLASER_BUNDLE_MODERN", "1") != "0"
DEVICES = os.environ.get("MLASER_BUNDLE_DEVICES", "1") != "0"
if MODERN:
    hiddenimports += [m for m in cfg.MODERN_IMPORTS if have(m.split(".")[0])]
if DEVICES:
    hiddenimports += [m for m in cfg.DEVICE_IMPORTS if have(m)]

# the manifest of the optional backends this build ships: the frozen --bundle-check requires
# each of them to load and work (bundle_config.OPTIONAL_BACKENDS)
_shipped = {k: have(mod.split(".")[0]) if k not in ("native_kernel",) else have(mod)
            for k, mod in cfg.OPTIONAL_BACKENDS.items()}
for k in ("ortools", "spyrrow", "ruckig"):
    _shipped[k] = _shipped[k] and MODERN
for k in ("hid", "serial"):
    _shipped[k] = _shipped[k] and DEVICES
_manifest = Path(workpath) / cfg.MANIFEST_NAME   # noqa: F821 (set by PyInstaller)
_manifest.parent.mkdir(parents=True, exist_ok=True)
import json                                         # noqa: E402

_manifest.write_text(json.dumps(cfg.manifest(_shipped, cfg.version(ROOT)), indent=1), encoding="utf-8")
datas += [(str(_manifest), "mlaser")]
print(f"mlaser.spec: optional backends shipped: {sorted(k for k, v in _shipped.items() if v)}")

a = Analysis(                              # noqa: F821
    [str(ROOT / "mlaser" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(HERE / "hooks")],      # hook-ortools.py (Windows DLLs)
    hooksconfig={},
    runtime_hooks=[],
    excludes=cfg.excludes(),
    noarchive=False,
    optimize=0,                           # asserts and docstrings stay (argparse help, checks)
)
# drop GPL-only / unused Qt libraries and plugins a hook may still have pulled in
# (and the system libraries only they pulled in, e.g. GTK for the gtk3 theme plugin, OpenSSL for
# the TLS plugins)
from PyInstaller.depend import bindepend   # noqa: E402

def _imports(src):
    return [n for n, _ in bindepend.get_imports(src)]


# Linux: also the build host's system libraries (bundle_config.is_host_system_lib)
a.binaries, _removed = cfg.prune_binaries(list(a.binaries), _imports)
print(f"mlaser.spec: removed {len(_removed)} unused binaries")
if IS_WIN:
    a.binaries, _dupes = cfg.dedupe_package_libs(list(a.binaries))
    print(f"mlaser.spec: removed {len(_dupes)} top-level copies of package DLLs: "
          f"{sorted(b[0] for b in _dupes)}")
_missing = cfg.missing_qt_imports(list(a.binaries), _imports)
if _missing:
    raise SystemExit("mlaser.spec: a dropped Qt library is still needed: "
                     + ", ".join(f"{b} -> {lib}" for b, lib in _missing))
a.datas = [d for d in a.datas if not cfg.drop_data(d[0])]

pyz = PYZ(a.pure)                          # noqa: F821

# the application icon, rendered from mlaser/ui/resources/icons/app/mlaser.svg
# (packaging/make_icons.py: .icns for the .app, .ico for the Windows executables, the hicolor
# PNGs for the Linux .desktop entry)
import subprocess                                   # noqa: E402

_icons = Path(workpath) / "icons"                   # noqa: F821 (set by PyInstaller)
subprocess.run([sys.executable, str(HERE / "make_icons.py"), str(_icons)], check=True,
               env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
_icon = _icons / ("mlaser.ico" if IS_WIN else "mlaser.icns")
icon = str(_icon) if _icon.is_file() and not IS_LINUX else None
if IS_LINUX:
    # the freedesktop entry and its icons (packaging/linux/README: install-desktop-entry.sh)
    a.datas += [("share/applications/io.github.mlaser.desktop", str(HERE / "linux" / "io.github.mlaser.desktop"), "DATA"),
                ("share/install-desktop-entry.sh", str(HERE / "linux" / "install-desktop-entry.sh"), "DATA")]
    for _png in sorted((_icons / "hicolor").glob("*/apps/*.png")):
        a.datas += [(f"share/icons/hicolor/{_png.parent.parent.name}/apps/{_png.name}", str(_png), "DATA")]


def _exe(name: str, console: bool):
    return EXE(                            # noqa: F821
        pyz, a.scripts, [],
        exclude_binaries=True,             # onedir
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,                         # UPX breaks Qt plugins and code signatures
        console=console,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,            # signing: packaging/sign_macos.sh after the build
        entitlements_file=None,
        icon=icon,
    )


gui = _exe(APP, console=not (IS_WIN or IS_MAC))
# Linux: both executables would be the same console program (11.6 MB each, the PYZ is
# embedded); mlaser-console is a symlink to mlaser there (the bootloader finds _internal through
# the resolved path).  Windows/macOS need two builds (windowed GUI + console).
cli = None if IS_LINUX else _exe(APP + "-console", console=True)

coll = COLLECT(                            # noqa: F821
    *[e for e in (gui, cli) if e is not None], a.binaries, a.datas,
    strip=False, upx=False, upx_exclude=[], name=APP,
)
if IS_LINUX:
    _link = Path(DISTPATH) / APP / (APP + "-console")   # noqa: F821 (set by PyInstaller)
    if _link.is_symlink() or _link.exists():
        _link.unlink()
    _link.symlink_to(APP)

if IS_MAC:
    app = BUNDLE(                          # noqa: F821
        coll,
        name=APP + ".app",
        icon=icon,
        bundle_identifier="io.github.mlaser",
        info_plist={
            "CFBundleName": APP,
            "CFBundleDisplayName": APP,
            "CFBundleShortVersionString": cfg.version(ROOT),
            "CFBundleVersion": cfg.version(ROOT),
            # a regular Dock application.  BUNDLE inherits ``console`` from the last executable
            # of the COLLECT (mlaser-console, console=True) and then writes LSBackgroundOnly=true:
            # macOS started the app with the "prohibited" activation policy, with no Dock icon,
            # no menu bar and never frontmost (native-check workflow, 2026-10-20)
            "LSBackgroundOnly": False,
            "LSUIElement": False,
            "NSPrincipalClass": "NSApplication",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,      # follow the system's dark appearance
            "NSSupportsAutomaticGraphicsSwitching": True,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "LSMinimumSystemVersion": "12.0",
            # drawings open from Finder (Open With / drag onto the Dock icon): QFileOpenEvent,
            # mlaser/ui/native.py (argv_emulation stays off: it blocks the start-up event loop)
            "CFBundleDocumentTypes": [
                {"CFBundleTypeName": "Mlaser drawing", "CFBundleTypeRole": "Editor",
                 "LSHandlerRank": "Owner", "CFBundleTypeExtensions": ["chf", "mlz"]},
                {"CFBundleTypeName": "Drawing", "CFBundleTypeRole": "Viewer",
                 "LSHandlerRank": "Alternate", "CFBundleTypeExtensions": ["dxf", "svg", "nc"]},
            ],
            # the app talks to the card on the local network (macOS 15 local-network privacy)
            "NSLocalNetworkUsageDescription":
                "mlaser talks to the laser's motion card on the local network.",
        },
    )
