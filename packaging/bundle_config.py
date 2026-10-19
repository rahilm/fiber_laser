"""What goes into the PyInstaller bundle (used by packaging/mlaser.spec and packaging/build.py).

Kept apart from the spec so the lists can be imported and tested (tests/test_packaging.py)
without running PyInstaller.
"""
from __future__ import annotations

import re
from pathlib import Path

# packages whose non-Python files are collected (fonts, resources, data tables)
DATA_PACKAGES = ["ezdxf", "svgelements", "fontTools", "shapely", "pyclipper", "uharfbuzz", "segno",
                 "PIL", "platformdirs"]
# never bundle test data, C sources or typing stubs of those packages
DATA_EXCLUDES = ["**/tests/**", "**/test/**", "**/*.pyx", "**/*.pxd", "**/*.c", "**/*.cpp",
                 "**/*.h", "**/*.hpp", "**/*.pyi", "ezdxf/acc/**/*.cpp"]

# imported lazily or by name (importlib) inside the libraries
HIDDEN_IMPORTS = [
    "svgelements", "pyclipper", "uharfbuzz", "fontTools.ttLib", "fontTools.pens.recordingPen",
    "fontTools.pens.basePen", "segno", "PIL.Image", "shapely", "ezdxf", "ezdxf.addons",
    "ezdxf.fonts", "ezdxf.render", "ezdxf.path", "platformdirs",
]
MODERN_IMPORTS = ["ortools.constraint_solver.pywrapcp", "ortools.constraint_solver.routing_enums_pb2",
                  "spyrrow", "ruckig"]
DEVICE_IMPORTS = ["hid", "serial", "serial.tools.list_ports"]

# mlaser modules left out: the parity capture/compare tooling (needs matplotlib, Wine captures;
# docs/review/architecture.md 3.4) -- not mlaser.parity_ports, which the app needs
_DROP_MLASER = re.compile(r"^mlaser\.parity(\.|$)")


def keep_mlaser_module(name: str) -> bool:
    return not _DROP_MLASER.match(name)


# unused Qt bindings (the app imports QtCore, QtGui, QtWidgets, QtSvg, QtPrintSupport only)
QT_UNUSED = [
    "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
    "QtAxContainer", "QtBluetooth", "QtCanvasPainter", "QtCharts", "QtConcurrent",
    "QtDataVisualization", "QtDesigner", "QtGraphs", "QtGraphsWidgets", "QtHelp", "QtHttpServer",
    "QtLabsStyleKit", "QtLocation", "QtMultimedia", "QtMultimediaWidgets", "QtNetworkAuth",
    "QtNfc", "QtOpenGL", "QtOpenGLWidgets", "QtPdf", "QtPdfWidgets", "QtPositioning", "QtQml",
    "QtQmlFeatures", "QtQuick", "QtQuick3D", "QtQuickControls2", "QtQuickTest", "QtQuickWidgets",
    "QtRemoteObjects", "QtScxml", "QtSensors", "QtSerialBus", "QtSerialPort", "QtSpatialAudio",
    "QtSql", "QtStateMachine", "QtTest", "QtTextToSpeech", "QtUiTools", "QtWebChannel",
    "QtWebEngineCore", "QtWebEngineQuick", "QtWebEngineWidgets", "QtWebSockets", "QtWebView",
    "QtXml", "QtAsyncio",
]
PY_EXCLUDES = [
    "mlaser.parity", "tests", "matplotlib", "pandas", "pytest", "_pytest", "hypothesis", "IPython",
    "tkinter", "_tkinter", "PyInstaller", "PyQt5", "PyQt6", "PySide2", "mypy", "importlinter",
    "grimp", "ruff", "pre_commit", "virtualenv", "setuptools", "pip",
    "yaml",            # only numpy.__config__.show() would use it
]


def excludes() -> list[str]:
    return PY_EXCLUDES + [f"PySide6.{m}" for m in QT_UNUSED]


# Qt libraries that are GPL-3.0-only (or commercial) in Qt 6 open source: never ship them in an
# LGPL bundle.  Matched against library file names (libQt6Charts.so.6, Qt6Charts.dll,
# QtCharts.framework).  https://doc.qt.io/qt-6/qtmodules.html ("Licenses and Attributions")
QT_GPL_ONLY = ["Charts", "DataVisualization", "Graphs", "Lottie", "NetworkAuth", "Quick3D",
               "VirtualKeyboard", "WaylandCompositor", "Designer", "HttpServer", "Coap", "Mqtt",
               "OpcUa", "QuickTimeline", "ShaderTools", "CanvasPainter", "LabsStyleKit"]
# large Qt libraries / plugin folders the app does not use
QT_DROP_LIBS = ["WebEngine", "Pdf", "Quick", "Qml", "3D", "Multimedia", "Location", "Positioning",
                "Bluetooth", "Nfc", "Sensors", "SerialBus", "SerialPort", "TextToSpeech",
                "RemoteObjects", "Scxml", "StateMachine", "SpatialAudio", "WebChannel", "WebSockets",
                "WebView", "Help", "Sql", "Test", "Labs", "FFmpeg"]
QT_DROP_PLUGIN_DIRS = ["assetimporters", "canbus", "designer", "geometryloaders", "geoservices",
                       "multimedia", "position", "qmltooling", "renderers", "renderplugins",
                       "sceneparsers", "scxmldatamodel", "sensors", "sqldrivers", "texttospeech",
                       "webview", "wayland-graphics-integration-server",
                       "tls", "networkinformation"]     # no QtNetwork use: no OpenSSL in the bundle
# single plugins: GTK3 theme (pulls the build host's GTK/cairo/pango, ~15 MB), Qt PDF image
# format (QtPdf is not shipped), the GPL-only virtual keyboard
QT_DROP_PLUGINS = ["qgtk3", "qpdf", "qtvirtualkeyboard"]

_QT_LIB = re.compile(r"(?:^|[/\\])(?:lib)?Qt6?([A-Za-z0-9]+?)(?:\.so[.\d]*|\.dll|\.dylib|\.framework)",
                     re.IGNORECASE)


def qt_module_of(path: str) -> str:
    """'PySide6/Qt/lib/libQt6Charts.so.6' -> 'Charts' ('' if not a Qt library)."""
    m = _QT_LIB.search(path.replace("\\", "/"))
    return m.group(1) if m else ""


def is_gpl_only(path: str) -> bool:
    mod = qt_module_of(path)
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if "virtualkeyboard" in name:          # libqtvirtualkeyboardplugin.so links Qt Virtual Keyboard
        return True
    return any(mod.lower().startswith(g.lower()) for g in QT_GPL_ONLY) if mod else False


def drop_binary(dest: str) -> bool:
    p = dest.replace("\\", "/")
    parts = p.split("/")
    if "plugins" in parts:
        i = parts.index("plugins")
        if i + 1 < len(parts) and parts[i + 1] in QT_DROP_PLUGIN_DIRS:
            return True
        if any(k in parts[-1].lower() for k in QT_DROP_PLUGINS):
            return True
    mod = qt_module_of(p)
    if is_gpl_only(p) or (mod and any(mod.lower().startswith(d.lower()) for d in QT_DROP_LIBS)):
        return True
    return False


def drop_data(dest: str) -> bool:
    p = dest.replace("\\", "/")
    if "/qml/" in p or p.endswith("/qml") or "/Qt/qml" in p:
        return True
    if "qtwebengine" in p.lower():
        return True
    return drop_binary(p)


def version(root: Path) -> str:
    """Project version from pyproject.toml (no tomllib dependency on the spec side)."""
    import tomllib
    with open(root / "pyproject.toml", "rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def _base(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1].lower()


def prune_binaries(binaries: list, imports_of) -> tuple[list, list]:
    """Drop the binaries :func:`drop_binary` selects, then the libraries that only they needed.

    ``binaries``: PyInstaller TOC entries ``(dest, src, typecode)``; ``imports_of(src)``: the
    link-time imports of a file as basenames or paths (``PyInstaller.depend.bindepend``).  A
    library is removed only if a dropped binary imports it (directly or through another
    removed library) and no kept binary does, so libraries loaded with dlopen/ctypes stay.
    Returns (kept, removed)."""
    def imports(entry) -> set[str]:
        if entry[2] == "SYMLINK":
            return set()
        try:
            return {_base(n) for n in imports_of(entry[1])}
        except Exception:
            return set()

    kept = [b for b in binaries if not drop_binary(b[0])]
    removed = [b for b in binaries if drop_binary(b[0])]
    while True:
        orphans_cand: set[str] = set()
        for b in removed:
            orphans_cand |= imports(b)
        needed: set[str] = set()
        for b in kept:
            needed |= imports(b)
        gone = [b for b in kept if b[2] != "SYMLINK" and _base(b[0]) in orphans_cand
                and _base(b[0]) not in needed]
        if not gone:
            break
        kept = [b for b in kept if b not in gone]
        removed += gone
    # symlinks pointing at removed files (PyInstaller's parent-directory links)
    removed_dest = {b[0].replace("\\", "/") for b in removed}
    removed_base = {_base(b[0]) for b in removed}
    links = [b for b in kept if b[2] == "SYMLINK" and (_base(b[0]) in removed_base
                                                       or b[1].replace("\\", "/") in removed_dest)]
    kept = [b for b in kept if b not in links]
    return kept, removed + links
