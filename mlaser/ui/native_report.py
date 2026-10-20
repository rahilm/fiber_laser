"""A platform-integration report of the running application (``MLASER_NATIVE_REPORT``).

The native-check CI workflow (``.github/workflows/native-check.yml``, ``tools/native_check.py``)
starts the real application on macOS, Windows and Linux runners with
``MLASER_NATIVE_REPORT=<file.json>``; a few seconds after the window shows, the application
writes what Qt itself knows about its platform integration (style, colour scheme, the menu bar
and its roles, application identity, window title / file path / modified flag, macOS activation
policy, Windows AppUserModelID and title-bar theme) and a grab of the main window next to it.
The workflow adds what the operating system says (``lsappinfo``, System Events, the Dock, the
foreground window, a full-screen screenshot).

Without the variable nothing happens.  Never raises.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

log = logging.getLogger("mlaser.native")

ENV = "MLASER_NATIVE_REPORT"


def menu_bar(win):
    """The window's menu bar without creating one (``QMainWindow.menuBar()`` would replace the
    shell's menu widget): the shell's, else any QMenuBar child of the window."""
    from PySide6.QtWidgets import QMenuBar
    mb = getattr(getattr(win, "shell", None), "menubar", None)
    if mb is not None:
        return mb
    mw = win.menuWidget() if hasattr(win, "menuWidget") else None
    if isinstance(mw, QMenuBar):
        return mw
    return win.findChild(QMenuBar)


def _menus(win) -> list[dict[str, Any]]:
    from PySide6.QtGui import QKeySequence
    out: list[dict[str, Any]] = []
    mb = menu_bar(win)
    if mb is None:
        return out
    for act in mb.actions():
        menu = act.menu()
        items = []
        if menu is not None:
            for a in menu.actions():
                if a.isSeparator():
                    continue
                items.append({"text": a.text(), "enabled": a.isEnabled(), "visible": a.isVisible(),
                              "shortcut": a.shortcut().toString(QKeySequence.PortableText),
                              "role": str(a.menuRole()).rsplit(".", 1)[-1],
                              "submenu": a.menu() is not None, "command": a.data() or ""})
        out.append({"title": act.text(), "visible": act.isVisible(), "items": items})
    return out


def _mac_state() -> dict[str, Any]:
    if sys.platform != "darwin":
        return {}
    try:
        import ctypes

        from mlaser._power import _mac
        oc = _mac()
        if oc is None:
            return {"error": "no objc"}
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
        app = oc.send(ctypes.c_void_p, oc.cls("NSApplication"), "sharedApplication")
        policy = oc.send(ctypes.c_long, app, "activationPolicy")
        active = oc.send(ctypes.c_bool, app, "isActive")
        main_menu = oc.send(ctypes.c_void_p, app, "mainMenu")
        n_items = oc.send(ctypes.c_long, main_menu, "numberOfItems") if main_menu else 0
        titles = []
        for i in range(int(n_items)):
            item = oc.send(ctypes.c_void_p, main_menu, "itemAtIndex:", (ctypes.c_long, i))
            s = oc.send(ctypes.c_void_p, item, "title")
            titles.append(oc.send(ctypes.c_char_p, s, "UTF8String").decode("utf-8", "replace") if s else "")
        bundle = oc.send(ctypes.c_void_p, oc.cls("NSBundle"), "mainBundle")
        bid = oc.send(ctypes.c_void_p, bundle, "bundleIdentifier")
        return {"activation_policy": {0: "regular", 1: "accessory", 2: "prohibited"}.get(int(policy), policy),
                "is_active": bool(active), "ns_main_menu": titles,
                "bundle_id": oc.send(ctypes.c_char_p, bid, "UTF8String").decode() if bid else None}
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return {"error": repr(exc)}


def _win_state(win) -> dict[str, Any]:
    if sys.platform != "win32":
        return {}
    out: dict[str, Any] = {}
    try:
        import ctypes
        from ctypes import wintypes
        p = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(p))  # type: ignore[attr-defined]
        out["app_user_model_id"] = p.value if hr == 0 else None
        hwnd = int(win.winId())
        val = ctypes.c_int(0)
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(  # type: ignore[attr-defined]
            wintypes.HWND(hwnd), 20, ctypes.byref(val), ctypes.sizeof(val))
        out["dwm_dark_title_bar"] = bool(val.value) if hr == 0 else None
        out["foreground_is_main"] = int(ctypes.windll.user32.GetForegroundWindow() or 0) == hwnd  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        out["error"] = repr(exc)
    return out


def report(win) -> dict[str, Any]:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QPalette
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    assert app is not None
    hints = app.styleHints()
    scheme = hints.colorScheme() if hasattr(hints, "colorScheme") else None
    mb = menu_bar(win)
    shell = getattr(win, "shell", None)
    topbar = getattr(shell, "topbar", None)
    menu_btn = getattr(topbar, "menu_btn", None)
    return {
        "pid": os.getpid(),
        "platform": sys.platform,
        "qpa": QGuiApplication.platformName(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "argv0": sys.argv[0] if sys.argv else "",
        "application_name": app.applicationName(),
        "application_display_name": app.applicationDisplayName(),
        "organization_domain": app.organizationDomain(),
        "desktop_file_name": QGuiApplication.desktopFileName(),
        "window_icon_null": app.windowIcon().isNull(),
        "style": app.style().name() if hasattr(app.style(), "name") else app.style().objectName(),
        "color_scheme": str(scheme).rsplit(".", 1)[-1] if scheme is not None else None,
        "theme_setting": app.property("mlaser_theme"),
        "palette_window": app.palette().color(QPalette.Window).name(),
        "app_style_sheet_len": len(app.styleSheet()),
        "window_style_sheet_len": len(win.styleSheet()),
        "window_title": win.windowTitle(),
        "window_file_path": win.windowFilePath(),
        "window_modified": win.isWindowModified(),
        "window_active": win.isActiveWindow(),
        "application_state": str(app.applicationState()).rsplit(".", 1)[-1],
        "unified_title_toolbar": win.unifiedTitleAndToolBarOnMac(),
        "menu_bar": {"present": mb is not None,
                     "native": bool(mb is not None and mb.isNativeMenuBar()),
                     "visible": bool(mb is not None and mb.isVisible()),
                     "dont_use_native": app.testAttribute(Qt.AA_DontUseNativeMenuBar),
                     "menus": _menus(win)},
        "in_window_menu_button": bool(menu_btn is not None and menu_btn.isVisibleTo(win)),
        "macos": _mac_state(),
        "windows": _win_state(win),
    }


def install(win, delay_ms: int = 6000) -> bool:
    """Write the report ``delay_ms`` after start when ``MLASER_NATIVE_REPORT`` is set."""
    path = os.environ.get(ENV)
    if not path:
        return False
    from PySide6.QtCore import QTimer

    def write() -> None:
        try:
            data = report(win)
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, default=str)
            win.grab().save(os.path.splitext(path)[0] + "-window.png")
            os.replace(path + ".tmp", path)
        except Exception:  # noqa: BLE001 - diagnostics only
            log.exception("native report failed")
    QTimer.singleShot(delay_ms, write)
    return True


__all__ = ["install", "report", "ENV"]
