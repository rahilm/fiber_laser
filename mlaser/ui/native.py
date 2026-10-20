"""Platform integration: the application's identity, activation and the OS shell features.

One module per concern of "feels native on the platform it runs on" (docs/dev/NATIVE.md):

=================  ==============================  ==============================  ===============================
                   macOS                           Windows                         Linux
=================  ==============================  ==============================  ===============================
identity           name/display name, Dock icon    AppUserModelID (taskbar         desktop file name (Wayland
                   (``setWindowIcon``), CFBundle-  grouping, pinning, jump list),  app id, ``_KDE_NET_WM_DESKTOP_
                   Name for source runs            window icon                     FILE``), window icon
activation         Regular policy + activate at    (the OS activates a new         (the WM activates; startup
                   start (a terminal keeps focus   process' first window)          notification id from the
                   under cooperative activation)                                   launcher)
recent files       File > Open Recent, Dock menu   File > Open Recent, jump list   File > Open Recent
job progress       Dock badge "42%"                taskbar progress (ITaskbar-     launcher progress (Unity
                                                   List3)                          LauncherEntry over D-Bus)
alarm              Dock bounce (``alert``)         taskbar flash (``alert``)       urgency hint (``alert``)
dark mode          NSApp appearance (Qt)           dark title bar (DWM)            portal colour scheme (Qt)
file dialogs       native sheet (Qt default)       IFileDialog (Qt default)        xdg-desktop-portal theme
open from shell    QFileOpenEvent (Finder, Dock)   command line                    command line (%F)
=================  ==============================  ==============================  ===============================

Every OS call is best effort: it never raises and never blocks start-up (the ``ctypes`` calls
are guarded and logged at INFO).  :func:`before_qapplication` runs before the QApplication
exists, :func:`init_application` right after, :class:`NativeIntegration` once the main
window is built.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Any, Callable, Optional

log = logging.getLogger("mlaser.native")

APP_NAME = "mlaser"
DISPLAY_NAME = "mlaser"
#: reverse-DNS id: the macOS bundle id, the Linux desktop file name (io.github.mlaser.desktop)
APP_ID = "io.github.mlaser"
#: Windows AppUserModelID (Company.Product[.SubProduct], no spaces)
WINDOWS_AUMID = "io.github.mlaser.mlaser"
RECENT_KEY = "file.recent"
RECENT_MAX = 10

MACOS, WINDOWS, LINUX = "macos", "windows", "linux"


def platform_id(plat: Optional[str] = None) -> str:
    plat = plat or sys.platform
    if plat == "darwin":
        return MACOS
    if plat.startswith("win"):
        return WINDOWS
    return LINUX


def icon_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "icons", "app", "mlaser.svg")


def app_icon():
    """The application icon (SVG, every size)."""
    from PySide6.QtGui import QIcon
    return QIcon(icon_path())


# ---------------------------------------------------------------------------------- macOS
def _objc():
    if sys.platform != "darwin":
        return None
    from mlaser._power import _mac
    oc = _mac()
    if oc is None:
        return None
    try:
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
    except OSError:
        return None
    return oc


def _nsapp(oc) -> int:
    return oc.send(ctypes.c_void_p, oc.cls("NSApplication"), "sharedApplication")


def _class_name(oc, obj: int) -> str:
    oc.lib.object_getClassName.restype = ctypes.c_char_p
    oc.lib.object_getClassName.argtypes = [ctypes.c_void_p]
    return (oc.lib.object_getClassName(obj) or b"").decode()


def mac_source_identity(name: str = APP_NAME) -> bool:
    """Source runs (``uv run mlaser``, ``python -m mlaser``) are a Python process: the menu bar
    would say "Python".  Rename the process and, when the main bundle's info dictionary is
    mutable (python.org's Python.app), its ``CFBundleName`` (what AppKit shows as the
    application menu's title).  Before the QApplication exists.  No-op in the .app."""
    if getattr(sys, "frozen", False):
        return False
    oc = _objc()
    if oc is None:
        return False
    try:
        pinfo = oc.send(ctypes.c_void_p, oc.cls("NSProcessInfo"), "processInfo")
        oc.send(None, pinfo, "setProcessName:", (ctypes.c_void_p, oc.nsstring(name)))
        bundle = oc.send(ctypes.c_void_p, oc.cls("NSBundle"), "mainBundle")
        info = oc.send(ctypes.c_void_p, bundle, "infoDictionary") if bundle else None
        # only a dictionary that is certainly mutable: mutating an immutable one throws an
        # Objective-C exception, which ctypes cannot catch
        if info and _class_name(oc, info) == "__NSDictionaryM":
            for key in ("CFBundleName", "CFBundleDisplayName"):
                oc.send(None, info, "setObject:forKey:", (ctypes.c_void_p, oc.nsstring(name)),
                        (ctypes.c_void_p, oc.nsstring(key)))
            return True
        log.info("macOS: bundle info dictionary %s left as is", _class_name(oc, info) if info else None)
    except Exception as exc:  # noqa: BLE001 - best effort
        log.info("macOS identity not set: %s", exc)
    return False


def mac_launchservices_name(name: str = APP_NAME) -> bool:
    """Source runs: the Dock and the application switcher label a plain executable by its
    file name ("python3.12").  LaunchServices' display name of this process is set to
    ``name`` (``_LSSetApplicationInformationItem``, the call AppKit itself uses; plain C, so
    a failure is a return code, never an exception).  After the QApplication exists."""
    if sys.platform != "darwin" or getattr(sys, "frozen", False):
        return False
    try:
        cs = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        get_asn = cs._LSGetCurrentApplicationASN
        set_item = cs._LSSetApplicationInformationItem
        get_asn.restype = ctypes.c_void_p
        set_item.restype = ctypes.c_int
        set_item.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        key = ctypes.c_void_p.in_dll(cs, "_kLSDisplayNameKey").value
        asn = get_asn()
        if not asn or not key:
            return False
        cfname = cf.CFStringCreateWithCString(None, name.encode("utf-8"), 0x08000100)
        rc = set_item(-2, asn, key, cfname, None)          # kLSDefaultSessionID
        return rc == 0
    except Exception as exc:  # noqa: BLE001 - private API, best effort
        log.info("LaunchServices display name not set: %s", exc)
        return False


def mac_defaults() -> None:
    """AppKit defaults set before the menu bar exists: no automatic "Enter Full Screen" item
    in the View menu (the Window menu has ours, with ⌃⌘F)."""
    oc = _objc()
    if oc is None:
        return
    try:
        ud = oc.send(ctypes.c_void_p, oc.cls("NSUserDefaults"), "standardUserDefaults")
        oc.send(None, ud, "setBool:forKey:", (ctypes.c_bool, False),
                (ctypes.c_void_p, oc.nsstring("NSFullScreenMenuItemEverywhere")))
    except Exception as exc:  # noqa: BLE001
        log.info("macOS defaults not set: %s", exc)


def mac_activation_policy() -> Optional[str]:
    oc = _objc()
    if oc is None:
        return None
    try:
        p = int(oc.send(ctypes.c_long, _nsapp(oc), "activationPolicy"))
        return {0: "regular", 1: "accessory", 2: "prohibited"}.get(p, str(p))
    except Exception:  # noqa: BLE001
        return None


def mac_activate() -> bool:
    """Make the application a regular Dock application (if a stale bundle or the launcher made
    it background-only) and bring it to the front: a process started from a terminal is not
    activated by macOS 14+ on its own, so the menu bar kept showing the terminal's menus."""
    oc = _objc()
    if oc is None:
        return False
    try:
        app = _nsapp(oc)
        if int(oc.send(ctypes.c_long, app, "activationPolicy")) != 0:
            oc.send(ctypes.c_bool, app, "setActivationPolicy:", (ctypes.c_long, 0))
            log.info("macOS: activation policy set to regular")
        sel = oc.lib.sel_registerName(b"activate")
        if oc.send(ctypes.c_bool, app, "respondsToSelector:", (ctypes.c_void_p, sel)):
            oc.send(None, app, "activate")                       # macOS 14+
        oc.send(None, app, "activateIgnoringOtherApps:", (ctypes.c_bool, True))
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("macOS activation failed: %s", exc)
        return False


def mac_badge(text: str) -> None:
    """The Dock tile's badge ("" removes it)."""
    oc = _objc()
    if oc is None:
        return
    try:
        tile = oc.send(ctypes.c_void_p, _nsapp(oc), "dockTile")
        oc.send(None, tile, "setBadgeLabel:", (ctypes.c_void_p, oc.nsstring(text) if text else None))
    except Exception as exc:  # noqa: BLE001
        log.info("macOS badge failed: %s", exc)


def mac_set_help_menu(title: str) -> bool:
    """Tell AppKit which menu is Help, so it gets the system's help search field."""
    oc = _objc()
    if oc is None:
        return False
    try:
        app = _nsapp(oc)
        main = oc.send(ctypes.c_void_p, app, "mainMenu")
        if not main:
            return False
        item = oc.send(ctypes.c_void_p, main, "itemWithTitle:", (ctypes.c_void_p, oc.nsstring(title)))
        sub = oc.send(ctypes.c_void_p, item, "submenu") if item else None
        if sub:
            oc.send(None, app, "setHelpMenu:", (ctypes.c_void_p, sub))
            return True
    except Exception as exc:  # noqa: BLE001
        log.info("macOS help menu not set: %s", exc)
    return False


# ---------------------------------------------------------------------------------- Windows
def windows_set_app_id(app_id: str = WINDOWS_AUMID) -> bool:
    """The process' AppUserModelID: the taskbar groups the windows under mlaser (not
    python.exe), shows mlaser's icon, pins mlaser and owns its jump list.  Before any window."""
    if not sys.platform.startswith("win"):
        return False
    try:
        hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))  # type: ignore[attr-defined]
        return hr == 0
    except Exception as exc:  # noqa: BLE001
        log.info("AppUserModelID not set: %s", exc)
        return False


def windows_dark_title_bar(win, dark: bool) -> bool:
    """DWMWA_USE_IMMERSIVE_DARK_MODE (20; 19 before Windows 10 20H1) on the window frame."""
    if not sys.platform.startswith("win"):
        return False
    try:
        hwnd = ctypes.c_void_p(int(win.winId()))
        val = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
        for attr in (20, 19):
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                return True
    except Exception as exc:  # noqa: BLE001
        log.info("dark title bar not set: %s", exc)
    return False


def windows_icon_file() -> Optional[str]:
    """``mlaser.ico`` in the user data directory, rendered once from the SVG (source runs have
    no executable carrying the icon)."""
    try:
        from mlaser.services.config import data_dir
        path = os.path.join(str(data_dir()), "mlaser.ico")
        if os.path.isfile(path) and os.path.getmtime(path) >= os.path.getmtime(icon_path()):
            return path
        import io

        from PIL import Image
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        app_icon().pixmap(256, 256).toImage().save(buf, "PNG")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Image.open(io.BytesIO(bytes(ba.data()))).convert("RGBA").save(
            path, sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
        return path
    except Exception as exc:  # noqa: BLE001
        log.info("no .ico for the taskbar: %s", exc)
        return None


def windows_window_identity(win, relaunch: Optional[str] = None, icon: Optional[str] = None) -> bool:
    """The taskbar identity of the window (``SHGetPropertyStoreForWindow``): the AppUserModelID
    and, for a source run, the relaunch command, name and icon.  Without them the taskbar shows
    a source run's button with a generic icon (python.exe has no mlaser icon)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        store = ctypes.c_void_p()
        iid = _GUID.of("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
        hr = ctypes.windll.shell32.SHGetPropertyStoreForWindow(  # type: ignore[attr-defined]
            ctypes.c_void_p(int(win.winId())), ctypes.byref(iid), ctypes.byref(store))
        if hr != 0 or not store.value:
            return False

        class _PROPKEY(ctypes.Structure):
            _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]

        class _PROPVAR(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                        ("r3", ctypes.c_ushort), ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]
        fmt = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"           # PKEY_AppUserModel_*
        values = [(5, WINDOWS_AUMID)]                            # ID
        if relaunch and icon:
            values += [(2, relaunch), (4, DISPLAY_NAME), (3, icon + ",0")]  # RelaunchCommand, name, icon
        keep = []
        ok = True
        for pid, text in values:
            key = _PROPKEY(_GUID.of(fmt), pid)
            buf = ctypes.create_unicode_buffer(text)
            keep.append(buf)
            var = _PROPVAR(31, 0, 0, 0, ctypes.cast(buf, ctypes.c_void_p), None)   # VT_LPWSTR
            ok = _com_call(store.value, 6, ctypes.c_long, (ctypes.c_void_p, ctypes.addressof(key)),
                           (ctypes.c_void_p, ctypes.addressof(var))) == 0 and ok
        _com_call(store.value, 7, ctypes.c_long)                                   # Commit
        _com_call(store.value, 2, ctypes.c_ulong)
        return ok
    except Exception as exc:  # noqa: BLE001
        log.info("window identity not set: %s", exc)
        return False


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16),
                ("d4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "_GUID":
        import uuid
        u = uuid.UUID(text)
        g = cls()
        g.d1, g.d2, g.d3 = u.fields[0], u.fields[1], u.fields[2]
        for i, b in enumerate(u.bytes[8:]):
            g.d4[i] = b
        return g


def _com_call(obj: int, index: int, restype, *args) -> Any:
    """Call the ``index``-th method of a COM interface pointer (``args``: (ctype, value))."""
    vtbl = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *[t for t, _ in args])
    return proto(vtbl[index])(obj, *[v for _, v in args])


def _co_create(clsid: str, iid: str) -> Optional[int]:
    ole = ctypes.windll.ole32  # type: ignore[attr-defined]
    ole.CoInitialize(None)
    ptr = ctypes.c_void_p()
    hr = ole.CoCreateInstance(ctypes.byref(_GUID.of(clsid)), None, 1, ctypes.byref(_GUID.of(iid)),
                              ctypes.byref(ptr))
    return ptr.value if hr == 0 and ptr.value else None


class WindowsTaskbar:
    """``ITaskbarList3``: progress on the taskbar button while a job runs."""
    CLSID = "56FDF344-FD6D-11d0-958A-006097C9A090"
    IID = "ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf"
    NOPROGRESS, NORMAL, ERROR, PAUSED = 0, 2, 4, 8

    def __init__(self) -> None:
        self._ptr: Optional[int] = None
        self._ok = sys.platform.startswith("win")

    def _get(self) -> Optional[int]:
        if self._ptr is None and self._ok:
            try:
                self._ptr = _co_create(self.CLSID, self.IID)
                if self._ptr:
                    _com_call(self._ptr, 3, ctypes.c_long)                  # HrInit
                else:
                    self._ok = False
            except Exception as exc:  # noqa: BLE001
                log.info("ITaskbarList3 unavailable: %s", exc)
                self._ok = False
        return self._ptr

    def progress(self, win, fraction: Optional[float], state: int = NORMAL) -> bool:
        p = self._get()
        if not p:
            return False
        try:
            hwnd = ctypes.c_void_p(int(win.winId()))
            if fraction is None:
                _com_call(p, 10, ctypes.c_long, (ctypes.c_void_p, hwnd), (ctypes.c_int, self.NOPROGRESS))
            else:
                _com_call(p, 10, ctypes.c_long, (ctypes.c_void_p, hwnd), (ctypes.c_int, state))
                _com_call(p, 9, ctypes.c_long, (ctypes.c_void_p, hwnd),
                          (ctypes.c_uint64, int(max(0.0, min(1.0, fraction)) * 1000)), (ctypes.c_uint64, 1000))
            return True
        except Exception as exc:  # noqa: BLE001
            log.info("taskbar progress failed: %s", exc)
            return False


def windows_jump_list(paths: list[str], exe: Optional[str] = None, args_prefix: str = "",
                      category: str = "Recent") -> bool:
    """A custom jump list with the recent drawings (``ICustomDestinationList``: one category of
    ``IShellLinkW`` entries, each starting mlaser with the file).  Windows only; best effort."""
    if not sys.platform.startswith("win"):
        return False
    try:
        exe = exe or sys.executable
        if not getattr(sys, "frozen", False) and not args_prefix:
            args_prefix = "-m mlaser"
        dest = _co_create("77f10cf0-3db5-4966-b520-b7c54fd35ed6", "6332debf-87b5-4670-90c0-5e57b408a49e")
        coll = _co_create("2d3468c1-36a7-43b6-ac24-d3f02fd9607a", "5632b1a4-e38a-400a-928a-d4cd63230295")
        if not dest or not coll:
            return False
        _com_call(dest, 3, ctypes.c_long, (ctypes.c_wchar_p, WINDOWS_AUMID))          # SetAppID
        slots = ctypes.c_uint(0)
        removed = ctypes.c_void_p()
        iid_array = _GUID.of("92CA9DCD-5622-4bba-A805-5E9F541BD8C9")                  # IObjectArray
        hr = _com_call(dest, 4, ctypes.c_long, (ctypes.POINTER(ctypes.c_uint), ctypes.byref(slots)),
                       (ctypes.c_void_p, ctypes.addressof(iid_array)), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(removed)))
        if hr != 0:
            return False
        for path in paths[: max(1, int(slots.value or RECENT_MAX))]:
            link = _co_create("00021401-0000-0000-C000-000000000046", "000214F9-0000-0000-C000-000000000046")
            if not link:
                continue
            _com_call(link, 20, ctypes.c_long, (ctypes.c_wchar_p, exe))                               # SetPath
            _com_call(link, 11, ctypes.c_long, (ctypes.c_wchar_p, f'{args_prefix} "{path}"'.strip()))  # SetArguments
            _com_call(link, 7, ctypes.c_long, (ctypes.c_wchar_p, path))                               # SetDescription
            _com_call(link, 17, ctypes.c_long, (ctypes.c_wchar_p, exe), (ctypes.c_int, 0))           # SetIconLocation
            # the title shown in the list: PKEY_Title through IPropertyStore
            store = ctypes.c_void_p()
            iid_store = _GUID.of("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
            if _com_call(link, 0, ctypes.c_long, (ctypes.c_void_p, ctypes.addressof(iid_store)),
                         (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(store))) == 0 and store.value:
                class _PROPKEY(ctypes.Structure):
                    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]

                class _PROPVAR(ctypes.Structure):
                    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                                ("r3", ctypes.c_ushort), ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]
                key = _PROPKEY(_GUID.of("F29F85E0-4FF9-1068-AB91-08002B27B3D9"), 2)
                title = ctypes.create_unicode_buffer(os.path.basename(path))
                var = _PROPVAR(31, 0, 0, 0, ctypes.cast(title, ctypes.c_void_p), None)        # VT_LPWSTR
                _com_call(store.value, 6, ctypes.c_long, (ctypes.c_void_p, ctypes.addressof(key)),
                          (ctypes.c_void_p, ctypes.addressof(var)))                           # SetValue
                _com_call(store.value, 7, ctypes.c_long)                                      # Commit
                _com_call(store.value, 2, ctypes.c_ulong)
            _com_call(coll, 5, ctypes.c_long, (ctypes.c_void_p, link))                        # AddObject
            _com_call(link, 2, ctypes.c_ulong)
        if paths:
            _com_call(dest, 5, ctypes.c_long, (ctypes.c_wchar_p, category), (ctypes.c_void_p, coll))  # AppendCategory
        hr = _com_call(dest, 8, ctypes.c_long)                                                # CommitList
        _com_call(coll, 2, ctypes.c_ulong)
        _com_call(dest, 2, ctypes.c_ulong)
        return hr == 0
    except Exception as exc:  # noqa: BLE001
        log.info("jump list not updated: %s", exc)
        return False


# ---------------------------------------------------------------------------------- Linux
def desktop_entry(exec_cmd: str = "mlaser") -> str:
    """The freedesktop entry (packaging/linux/io.github.mlaser.desktop is this text)."""
    return "\n".join([
        "[Desktop Entry]", "Type=Application", "Version=1.5", f"Name={APP_NAME}",
        "GenericName=Laser cutting CAM and machine control",
        "Comment=Design, nest and cut sheet metal on a fiber-laser cutter",
        f"Exec={exec_cmd} %F", f"TryExec={exec_cmd.split()[0]}", f"Icon={APP_ID}", "Terminal=false",
        "Categories=Graphics;Engineering;Qt;", "Keywords=laser;cutting;CAM;CNC;DXF;nesting;",
        "MimeType=image/vnd.dxf;image/svg+xml;", "StartupNotify=true", f"StartupWMClass={APP_NAME}",
        "SingleMainWindow=true", ""])


def linux_platform_theme(environ: Optional[dict] = None, plugin_dir: Optional[str] = None) -> Optional[str]:
    """The Qt platform theme to ask for, or None to keep Qt's choice: the bundle has no GTK
    theme plugin (packaging drops it with GTK), so file dialogs and the colour scheme come
    from the xdg-desktop-portal theme there; a user's QT_QPA_PLATFORMTHEME always wins."""
    env = os.environ if environ is None else environ
    if env.get("QT_QPA_PLATFORMTHEME"):
        return None
    if plugin_dir is None:
        try:
            import PySide6
            plugin_dir = os.path.join(os.path.dirname(PySide6.__file__), "Qt", "plugins", "platformthemes")
        except Exception:  # noqa: BLE001
            return None
    have = set(os.listdir(plugin_dir)) if os.path.isdir(plugin_dir) else set()
    if any("gtk3" in n for n in have):
        return None                         # Qt picks GTK on GNOME itself (native dialogs)
    if any("xdgdesktopportal" in n for n in have) and (env.get("XDG_CURRENT_DESKTOP") or env.get("FLATPAK_ID")):
        return "xdgdesktopportal"
    return None


class LinuxLauncher:
    """Unity LauncherEntry (KDE Plasma, Ubuntu Dock, Dash to Dock): progress and urgency on the
    launcher icon.  The D-Bus signal goes out through ``gdbus`` (GLib's tool, on every desktop
    that shows launcher progress): the bundle does not ship QtDBus.  Sent only on a change."""

    def __init__(self, gdbus: Optional[str] = None) -> None:
        import shutil
        self._last: Optional[tuple] = None
        self.gdbus = gdbus if gdbus is not None else (shutil.which("gdbus") or "")

    @staticmethod
    def command(gdbus: str, fraction: Optional[float], urgent: bool) -> list[str]:
        props = (f"{{'progress': <{float(fraction or 0.0):.3f}>, "
                 f"'progress-visible': <{'true' if fraction is not None else 'false'}>, "
                 f"'urgent': <{'true' if urgent else 'false'}>}}")
        return [gdbus, "emit", "--session", "--object-path", "/io/github/mlaser",
                "--signal", "com.canonical.Unity.LauncherEntry.Update",
                f"application://{APP_ID}.desktop", props]

    def progress(self, fraction: Optional[float], urgent: bool = False) -> bool:
        if platform_id() != LINUX or not self.gdbus or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return False
        key = (None if fraction is None else round(fraction, 2), urgent)
        if key == self._last:
            return True
        self._last = key
        try:
            import subprocess
            subprocess.Popen(self.command(self.gdbus, fraction, urgent), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError as exc:
            log.info("launcher progress failed: %s", exc)
            return False


# ---------------------------------------------------------------------------------- start-up
def before_qapplication() -> None:
    """Identity that must exist before the QApplication (and its first window)."""
    plat = platform_id()
    if plat == WINDOWS:
        windows_set_app_id()
    elif plat == MACOS:
        mac_source_identity()
        mac_defaults()
    else:
        theme = linux_platform_theme()
        if theme:
            os.environ["QT_QPA_PLATFORMTHEME"] = theme


def init_application(app) -> None:
    """Name, display name, desktop file name, icon (the Dock / taskbar icon of a source run),
    no icons in macOS menus."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(DISPLAY_NAME)
    app.setOrganizationName(APP_NAME)
    app.setOrganizationDomain("mlaser.github.io")
    QGuiApplication.setDesktopFileName(APP_ID)
    try:
        from mlaser import __version__
        app.setApplicationVersion(str(__version__))
    except Exception:  # noqa: BLE001
        pass
    app.setWindowIcon(app_icon())             # the Dock icon of a source run on macOS too
    if platform_id() == MACOS:
        app.setAttribute(Qt.AA_DontShowIconsInMenus, True)
        mac_launchservices_name()


# ---------------------------------------------------------------------------------- recent files
class RecentFiles:
    """The recent drawings (ConfigService ``file.recent``): File > Open Recent, the macOS Dock
    menu and the Windows jump list.  Qt-free apart from nothing: plain list logic."""

    def __init__(self, config, key: str = RECENT_KEY, limit: int = RECENT_MAX):
        self.config = config
        self.key = key
        self.limit = limit
        self.listeners: list[Callable[[list[str]], None]] = []

    @property
    def paths(self) -> list[str]:
        v = self.config.get(self.key, []) if self.config is not None else []
        return [p for p in (v or []) if isinstance(p, str)]

    def add(self, path: str) -> None:
        if not path:
            return
        path = os.path.abspath(path)
        cur = self.paths
        new = [path] + [p for p in cur if os.path.normcase(p) != os.path.normcase(path)]
        new = new[: self.limit]
        if new != cur:
            self._store(new)

    def clear(self) -> None:
        self._store([])

    def existing(self) -> list[str]:
        return [p for p in self.paths if os.path.exists(p)]

    def _store(self, paths: list[str]) -> None:
        if self.config is not None:
            self.config.set(self.key, paths)
        for fn in list(self.listeners):
            try:
                fn(paths)
            except Exception:  # noqa: BLE001
                log.exception("recent-files listener failed")


def register_settings(config) -> None:
    from mlaser.services.config import Setting
    try:
        config.register([Setting(RECENT_KEY, "json", [], label="shell.menu.open_recent", section="shell",
                                 source="app")])
    except KeyError:
        pass


# ---------------------------------------------------------------------------------- the window
class NativeIntegration:
    """Per-window platform features (created by the shell once the window is built)."""

    def __init__(self, window, config=None, tr=None):
        self.w = window
        self.tr = tr
        self.plat = platform_id()
        if config is not None:
            register_settings(config)
        self.recent = RecentFiles(config)
        self.recent.listeners.append(self._recent_changed)
        self.taskbar = WindowsTaskbar() if self.plat == WINDOWS else None
        self.launcher = LinuxLauncher() if self.plat == LINUX else None
        self.dock_menu = None
        self._progress: Optional[int] = None
        self._alarm = False
        self._activated = False
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None and hasattr(app, "file_open_handlers"):
            app.file_open_handlers.append(self.open_file)
            for path in app.pending_files:        # opened before the window existed
                self.open_file(path)
            app.pending_files.clear()
        if self.plat == MACOS:
            self._build_dock_menu()
        self._recent_changed(self.recent.paths, initial=True)

    # ------------------------------------------------------------------ start / activation
    def window_shown(self) -> None:
        """First show of the main window: activate (macOS), title bar theme (Windows)."""
        if self.plat == MACOS and not self._activated:
            self._activated = True
            mac_activate()
        if self.plat == WINDOWS:
            # the title bar of the theme chosen in mlaser (Qt follows only the system's)
            self.color_scheme_changed(self._dark())
            if getattr(sys, "frozen", False):
                windows_window_identity(self.w)
            else:
                exe = sys.executable.replace("python.exe", "pythonw.exe")
                windows_window_identity(self.w, f'"{exe}" -m mlaser', windows_icon_file())
        self.w.raise_()
        self.w.activateWindow()

    def _dark(self) -> bool:
        from PySide6.QtGui import QPalette
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        return bool(app is not None and app.palette().color(QPalette.Window).lightness() < 128)

    def color_scheme_changed(self, dark: bool) -> None:
        if self.plat == WINDOWS and self.w.isVisible():
            windows_dark_title_bar(self.w, dark)

    # ------------------------------------------------------------------ files
    def open_file(self, path: str) -> bool:
        """A file from the OS (Finder, Dock, jump list, the recent menu)."""
        if not path:
            return False
        ok = bool(self.w.load_path(path))
        if ok:
            self.recent.add(path)
        return ok

    def document_path(self, path: Optional[str]) -> None:
        """The document's path changed (opened or saved as): it is recent."""
        if path:
            self.recent.add(path)

    def _recent_changed(self, paths: list[str], initial: bool = False) -> None:
        if self.plat == MACOS:
            self._fill_dock_menu()
        elif self.plat == WINDOWS:
            cat = self.tr.tr("shell.menu.recent_drawings") if self.tr is not None else "Recent drawings"
            windows_jump_list([p for p in paths if os.path.exists(p)], category=cat)

    def _build_dock_menu(self) -> None:
        from PySide6.QtWidgets import QMenu
        self.dock_menu = QMenu()
        self._fill_dock_menu()
        try:
            self.dock_menu.setAsDockMenu()
        except Exception as exc:  # noqa: BLE001
            log.info("Dock menu not set: %s", exc)

    def _fill_dock_menu(self) -> None:
        m = self.dock_menu
        if m is None:
            return
        m.clear()
        for p in self.recent.existing():
            a = m.addAction(os.path.basename(p))
            a.setToolTip(p)
            a.triggered.connect(lambda _=False, p=p: self.open_file(p))

    # ------------------------------------------------------------------ job feedback
    def job_feedback(self, running: bool, progress: float, alarm: bool) -> None:
        """From the shell's 100 ms poll: progress while a job runs, attention on a new alarm."""
        pct = int(round(max(0.0, min(1.0, progress)) * 100)) if running else None
        if pct != self._progress:
            self._progress = pct
            if self.plat == MACOS:
                mac_badge(f"{pct}%" if pct is not None else "")
            elif self.plat == WINDOWS and self.taskbar is not None:
                self.taskbar.progress(self.w, None if pct is None else pct / 100.0,
                                      WindowsTaskbar.ERROR if alarm else WindowsTaskbar.NORMAL)
            elif self.launcher is not None:
                self.launcher.progress(None if pct is None else pct / 100.0, alarm)
        if alarm and not self._alarm:
            from PySide6.QtWidgets import QApplication
            QApplication.alert(self.w, 0)          # Dock bounce, taskbar flash, urgency hint
            if self.launcher is not None:
                self.launcher.progress(None if pct is None else pct / 100.0, True)
        elif not alarm and self._alarm and self.launcher is not None:
            self.launcher.progress(None if pct is None else pct / 100.0, False)
        self._alarm = alarm

    def close(self) -> None:
        if self._progress is not None:
            self.job_feedback(False, 0.0, False)
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        handlers = getattr(app, "file_open_handlers", None)
        if handlers is not None and self.open_file in handlers:
            handlers.remove(self.open_file)


def create_application(argv: list[str]):
    """The application object: a QApplication that takes QFileOpenEvent (macOS: a drawing
    opened from Finder, dropped on the Dock icon or chosen in the Dock menu).  Only the
    application object gets these events, so no application-wide event filter is needed."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    class MlaserApplication(QApplication):
        def __init__(self, args: list[str]):
            super().__init__(args)
            self.file_open_handlers: list[Callable[[str], Any]] = []
            self.pending_files: list[str] = []

        def event(self, ev) -> bool:  # noqa: A003 - Qt API
            if ev.type() == QEvent.FileOpen:
                path = ev.file() or ev.url().toLocalFile()
                if path:
                    if self.file_open_handlers:
                        for fn in list(self.file_open_handlers):
                            fn(path)
                    else:
                        self.pending_files.append(path)
                    return True
            return super().event(ev)

    return MlaserApplication(argv)


__all__ = ["APP_NAME", "APP_ID", "WINDOWS_AUMID", "platform_id", "before_qapplication",
           "init_application", "create_application", "NativeIntegration", "RecentFiles", "desktop_entry", "app_icon",
           "linux_platform_theme", "mac_activate", "windows_set_app_id", "windows_dark_title_bar"]
