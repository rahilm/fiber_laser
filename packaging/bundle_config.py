"""What goes into the PyInstaller bundle (used by packaging/mlaser.spec and packaging/build.py).

Kept apart from the spec so the lists can be imported and tested (tests/test_packaging.py)
without running PyInstaller.
"""
from __future__ import annotations

import re
import sys
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
    # pulled in by the PySide6 hooks but unused: QtNetwork only served the TUIO/VNC plugins and
    # the TLS backends; on Linux libQt6DBus itself stays (libQt6Gui links it), only its binding
    # goes (Review 2, P2-8)
    "QtNetwork", "QtDBus",
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
                "WebView", "Help", "Sql", "Test", "Labs", "FFmpeg", "Network"]
QT_DROP_PLUGIN_DIRS = ["assetimporters", "canbus", "designer", "geometryloaders", "geoservices",
                       "multimedia", "position", "qmltooling", "renderers", "renderplugins",
                       "sceneparsers", "scxmldatamodel", "sensors", "sqldrivers", "texttospeech",
                       "webview", "wayland-graphics-integration-server",
                       "tls", "networkinformation"]     # no QtNetwork use: no OpenSSL in the bundle
# single plugins (substrings of the file name):
# * GTK3 theme (pulls the build host's GTK/cairo/pango, ~15 MB), Qt PDF image format (QtPdf is
#   not shipped), the GPL-only virtual keyboard;
# * the TUIO touch and VNC plugins: the only users of QtNetwork (Review 2, P2-8);
# * embedded-Linux platforms (eglfs, linuxfb, Vulkan KHR display, minimal EGL, evdev input):
#   the app runs on a desktop (xcb / wayland; offscreen and minimal for tests);
# * qdirect2d (Windows): an alternative to qwindows, never selected by the app;
# * image formats nothing reads through Qt: the app's raster import goes through Pillow
#   (mlaser.engrave.image_ops), Qt reads only PNG (built in), JPEG (report pictures), SVG, ICO,
#   GIF, ICNS
QT_DROP_PLUGINS = ["qgtk3", "qpdf", "qtvirtualkeyboard",
                   "qtuiotouch", "qvnc",
                   "qeglfs", "qlinuxfb", "qvkkhrdisplay", "qminimalegl", "qevdev",
                   "qdirect2d",
                   "qtiff", "qwebp", "qtga", "qwbmp"]
# plugin folders of the embedded-Linux platforms (see above)
QT_DROP_PLUGIN_DIRS += ["egldeviceintegrations"]

# other binaries the app does not use (file names, lower case):
# * opengl32sw.dll (Windows, 20.6 MB): Mesa llvmpipe, Qt's software OpenGL fallback.  It is
#   loaded only when the app creates an OpenGL context (QOpenGLWidget, Qt Quick, QOpenGLContext)
#   and the driver has no OpenGL 2; mlaser paints with the raster engine (QPainter on a
#   QGraphicsView / QImage, no OpenGL viewport) and the QtOpenGL bindings are excluded
#   (QT_UNUSED), so it is never loaded;
# * libcrypto-3-x64.dll / libssl-3-x64.dll (Windows, 8.5 MB): PySide6's OpenSSL for the Qt TLS
#   plugin, which is not shipped (QT_DROP_PLUGIN_DIRS "tls"); Python's own ssl/hashlib use
#   libcrypto-3.dll / libssl-3.dll, which stay;
# * Pillow's AVIF codec (_avif and libavif, 5-8 MB): AVIF is not an engrave input format
#   (the open dialog offers PNG, JPEG, BMP; mlaser.engrave.dialog.FILE_FILTER)
DROP_FILES = ["opengl32sw.dll", "libcrypto-3-x64.dll", "libssl-3-x64.dll"]
_DROP_FILE_RE = re.compile(r"(?:^|[/\\])PIL[/\\]_avif\.", re.IGNORECASE)

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
    if parts[-1].lower() in DROP_FILES or _DROP_FILE_RE.search(p):
        return True
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
    # Qt's own translations (99 .qm files, 7 MB): mlaser installs no QTranslator for Qt's
    # strings (its own catalogues are mlaser/i18n, English only so far), so they are never read
    if p.startswith("PySide6/") and "/translations/" in p and p.endswith(".qm"):
        return True
    if "/qml/" in p or p.endswith("/qml") or "/Qt/qml" in p:
        return True
    if "qtwebengine" in p.lower():
        return True
    return drop_binary(p)


# ------------------------------------------------------------------ Linux: host system libraries
# PyInstaller copies every shared library the collected binaries link to, including the build
# host's system libraries (/lib, /usr/lib).  Those must come from the target instead: they talk
# to system services and configuration of the target's own version (CUPS, D-Bus, systemd,
# fontconfig's fonts.conf, the GL/EGL drivers, the certificate store), they get the target's
# security updates (OpenSSL, gnutls, krb5), and the C/C++ runtime and X11/xcb core are part of
# every desktop (the manylinux policy assumes them, PyInstaller excludes glibc, GL and libxcb).
# The PySide6 wheels expect exactly this: they ship Qt and ICU and link the rest from the system
# (Qt's "Qt for X11 Requirements").  So the Linux bundle drops every library resolved from a
# system directory except the X11 helper libraries below, which a minimal desktop may lack
# (the classic "could not load the Qt platform plugin xcb" error); they depend only on libxcb
# and libxkbcommon, which the target has.  docs/dev/RELEASE.md lists the target's packages.
HOST_LIB_DIRS = ("/lib/", "/lib64/", "/usr/lib/", "/usr/lib64/", "/usr/local/lib/")
HOST_LIB_KEEP = re.compile(r"^lib(xcb-(cursor|icccm|image|keysyms|render-util|util)|xkbcommon-x11)"
                           r"\.so(\.\d+)*$")


def is_host_system_lib(src: str, platform: str = sys.platform) -> bool:
    """A Linux library PyInstaller resolved from the build host's system directories that the
    target must provide (see HOST_LIB_DIRS / HOST_LIB_KEEP)."""
    if not platform.startswith("linux") or not src:
        return False
    s = src.replace("\\", "/")
    if not s.startswith(HOST_LIB_DIRS):
        return False
    return not HOST_LIB_KEEP.match(_base(s))


def drop_entry(entry, platform: str = sys.platform) -> bool:
    """A PyInstaller TOC entry ``(dest, src, typecode)`` that stays out of the bundle."""
    return drop_binary(entry[0]) or (entry[2] != "SYMLINK" and is_host_system_lib(entry[1], platform))


# ------------------------------------------------------------------ OR-Tools on Windows
def ortools_dlls(pkg_dir: str) -> list[tuple[str, str]]:
    """``binaries`` for the PyInstaller hook (packaging/hooks/hook-ortools.py): the DLLs of the
    Windows OR-Tools wheel in ``ortools/.libs``.  ``ortools/__init__.py`` loads them by full path
    from there (``_load_ortools_libs``) before any extension module imports ``ortools.dll``;
    PyInstaller cannot find them itself (no rpath on Windows), so before this hook the Windows
    bundle had no ortools.dll and sort mode 20 fell back silently (Review 2, P2-8)."""
    libs = Path(pkg_dir) / ".libs"
    return [(str(f), "ortools/.libs") for f in sorted(libs.glob("*.dll"))]


def dedupe_package_libs(binaries: list) -> tuple[list, list]:
    """Drop a DLL at the bundle's top level when a package ships the same file name in its own
    ``.libs`` folder (Windows: PyInstaller resolves the extension's imports along PATH, which on
    a CI runner can hold a different libprotobuf.dll / abseil_dll.dll; the package's copy is the
    one its ``__init__`` preloads, and the loader then reuses it by name).  Returns (kept, removed)."""
    in_libs = {_base(b[0]) for b in binaries
               if "/.libs/" in b[0].replace("\\", "/") and b[0].lower().endswith(".dll")}
    removed = [b for b in binaries
               if "/" not in b[0].replace("\\", "/") and _base(b[0]) in in_libs]
    return [b for b in binaries if b not in removed], removed


def missing_qt_imports(binaries: list, imports_of) -> list[tuple[str, str]]:
    """(binary, Qt library) pairs where a kept binary links a Qt library that is not in the
    bundle: a drop rule went too far.  The spec fails the build on any."""
    have = {_base(b[0]) for b in binaries}
    out = []
    for b in binaries:
        if b[2] == "SYMLINK":
            continue
        try:
            names = imports_of(b[1])
        except Exception:
            continue
        for n in names:
            if qt_module_of(n) and _base(n) not in have and _qt_framework_base(n) not in have:
                out.append((b[0], _base(n)))
    return out


def _qt_framework_base(name: str) -> str:
    """'@rpath/QtCore.framework/Versions/A/QtCore' -> 'qtcore' (macOS framework binaries)."""
    m = re.search(r"([A-Za-z0-9]+)\.framework", name)
    return m.group(1).lower() if m else ""


# ------------------------------------------------------------------ bundle manifest
#: optional backends: manifest key -> module whose import (and check) proves it works.
#: ``--bundle-check`` in the frozen app turns every one the manifest says was shipped into a
#: required item (mlaser.__main__.bundle_check), so a backend that is in the bundle but cannot
#: load (Windows ortools.dll, Review 2) fails the smoke test instead of falling back silently.
OPTIONAL_BACKENDS = {"ortools": "ortools.constraint_solver.pywrapcp", "spyrrow": "spyrrow",
                     "ruckig": "ruckig", "hid": "hid", "serial": "serial",
                     "native_kernel": "mlaser._native._emitter"}
MANIFEST_NAME = "bundle_manifest.json"


def manifest(shipped: dict, version_: str) -> dict:
    """Content of ``mlaser/bundle_manifest.json`` in the bundle (written by the spec)."""
    unknown = set(shipped) - set(OPTIONAL_BACKENDS)
    if unknown:
        raise ValueError(f"unknown backends {sorted(unknown)}")
    return {"format": 1, "version": version_,
            "shipped": {k: bool(shipped.get(k)) for k in OPTIONAL_BACKENDS}}


def help_datas(root: Path) -> list[tuple[str, str]]:
    """PyInstaller ``datas`` entries (source file, destination folder) for the help documents:
    the files ``tools/help_build.py`` puts into a wheel, at ``mlaser/help/docs`` where
    ``mlaser.diag.helpdocs.docs_root`` looks for them."""
    sys.path.insert(0, str(root / "tools"))
    try:
        import help_build
    finally:
        sys.path.pop(0)
    docs = root / "docs"
    out = []
    for rel in help_build.shipped_files(str(docs)):
        head = rel.rpartition("/")[0]
        out.append((str(docs / rel), "mlaser/help/docs" + ("/" + head if head else "")))
    return out


def version(root: Path) -> str:
    """Project version from pyproject.toml (no tomllib dependency on the spec side)."""
    import tomllib
    with open(root / "pyproject.toml", "rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def _base(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1].lower()


def prune_binaries(binaries: list, imports_of, drop=drop_entry) -> tuple[list, list]:
    """Drop the binaries ``drop`` selects (:func:`drop_entry`: :func:`drop_binary`, and on Linux
    the host system libraries), then the libraries that only they needed.

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

    kept = [b for b in binaries if not drop(b)]
    removed = [b for b in binaries if drop(b)]
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
