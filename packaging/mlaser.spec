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
  output and exit codes (on Linux both behave the same).

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


def have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


# ------------------------------------------------------------------ data files
datas = []
# mlaser package data: the [tool.setuptools.package-data] of pyproject.toml, plus any fonts that
# may sit under mlaser/cad at build time
datas += collect_data_files("mlaser.ui", includes=["resources/*.json", "resources/icons/tabler/*"])
datas += collect_data_files("mlaser.i18n", includes=["*.json"])
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
if os.environ.get("MLASER_BUNDLE_MODERN", "1") != "0":
    hiddenimports += [m for m in cfg.MODERN_IMPORTS if have(m.split(".")[0])]
if os.environ.get("MLASER_BUNDLE_DEVICES", "1") != "0":
    hiddenimports += [m for m in cfg.DEVICE_IMPORTS if have(m)]

a = Analysis(                              # noqa: F821
    [str(ROOT / "mlaser" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
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

a.binaries, _removed = cfg.prune_binaries(
    list(a.binaries), lambda src: [n for n, _ in bindepend.get_imports(src)])
print(f"mlaser.spec: removed {len(_removed)} unused binaries")
a.datas = [d for d in a.datas if not cfg.drop_data(d[0])]

pyz = PYZ(a.pure)                          # noqa: F821

# optional app icon (none is shipped yet: PyInstaller's default icon is used)
_icon = HERE / "icons" / ("mlaser.ico" if IS_WIN else "mlaser.icns")
icon = str(_icon) if _icon.is_file() else None


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
cli = _exe(APP + "-console", console=True)

coll = COLLECT(                            # noqa: F821
    gui, cli, a.binaries, a.datas,
    strip=False, upx=False, upx_exclude=[], name=APP,
)

if IS_MAC:
    app = BUNDLE(                          # noqa: F821
        coll,
        name=APP + ".app",
        icon=icon,
        bundle_identifier="io.github.mlaser",
        info_plist={
            "CFBundleShortVersionString": cfg.version(ROOT),
            "CFBundleVersion": cfg.version(ROOT),
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            # the app talks to the card on the local network (macOS 15 local-network privacy)
            "NSLocalNetworkUsageDescription":
                "mlaser talks to the laser's motion card on the local network.",
        },
    )
