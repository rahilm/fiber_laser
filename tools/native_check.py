#!/usr/bin/env python3
"""Launch the real application and record how the operating system sees it (native-check CI).

    python tools/native_check.py --label source --out shots/ [--wait 20] [--expect] -- uv run mlaser --demo
    python tools/native_check.py --label app --out shots/ --mac-app packaging/build/dist/mlaser.app -- --demo

The application is started with ``MLASER_NATIVE_REPORT`` (its own Qt-side report,
:mod:`mlaser.ui.native_report`), a throw-away config / data / log directory, and then probed:

* macOS: ``lsappinfo`` (type Foreground / UIElement / BackgroundOnly, name, bundle id), the
  frontmost application, the menu bar titles and the Dock items through System Events
  (``osascript``; needs accessibility, which hosted runners grant), ``screencapture``;
* Windows: the process' top-level windows (title, class, tool-window style), the foreground
  window, the DWM dark title bar, a screenshot (Pillow ``ImageGrab``);
* Linux (X11): ``_NET_ACTIVE_WINDOW``, ``WM_CLASS``, ``_NET_WM_ICON``, ``_GTK_APPLICATION_ID``,
  a screenshot.

Everything goes to ``<out>/<label>.json`` and ``<out>/<label>.md`` (and the screenshots).
``--expect`` turns the platform expectations (:func:`expectations`) into the exit code.
Standard library only (Pillow is used for screenshots when installed).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional


def sh(cmd: list[str], timeout: float = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + (("\n[stderr] " + r.stderr.strip()) if r.stderr.strip() else "")).strip()
    except Exception as exc:  # noqa: BLE001
        return f"[failed: {exc!r}]"


def osa(script: str) -> str:
    return sh(["osascript", "-e", script])


def screenshot(path: Path) -> str:
    if sys.platform == "darwin":
        return sh(["screencapture", "-x", str(path)]) or "ok"
    try:
        from PIL import ImageGrab
        if sys.platform == "win32":
            img = ImageGrab.grab(all_screens=True)
        else:
            img = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY") or None)
        img.save(path)
        return "ok"
    except Exception as exc:  # noqa: BLE001
        if shutil.which("import"):
            return sh(["import", "-window", "root", str(path)]) or "ok (import)"
        return f"[failed: {exc!r}]"


# ---------------------------------------------------------------------- per platform
def probe_mac(pid: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["lsappinfo_info"] = sh(["lsappinfo", "info", "-only", "name", "-only", "bundleid", "-only", "pid", str(pid)])
    out["lsappinfo_full"] = sh(["lsappinfo", "info", str(pid)])
    front = sh(["lsappinfo", "front"])
    out["lsappinfo_front"] = front
    out["front_info"] = sh(["lsappinfo", "info", "-only", "name", "-only", "pid", front.split()[0]]) if front else ""
    out["is_front"] = f'"pid"={pid}' in out["front_info"].replace(" ", "") or f"pid={pid}" in out["front_info"].replace(" ", "")
    proc = f"(first process whose unix id is {pid})"
    out["se_name"] = osa(f'tell application "System Events" to get name of {proc}')
    out["se_frontmost"] = osa(f'tell application "System Events" to get frontmost of {proc}')
    out["se_background_only"] = osa(f'tell application "System Events" to get background only of {proc}')
    out["se_menu_bar"] = osa(f'tell application "System Events" to get name of every menu bar item of menu bar 1 of {proc}')
    out["se_frontmost_process"] = osa('tell application "System Events" to get name of first process whose frontmost is true')
    out["se_windows"] = osa(f'tell application "System Events" to get name of every window of {proc}')
    out["dock_items"] = osa('tell application "System Events" to tell process "Dock" to get name of every UI element of list 1')
    import re
    m = re.search(r'type="(\w+)"', out["lsappinfo_full"])
    out["type"] = m.group(1) if m else "unknown"            # Foreground / UIElement / BackgroundOnly
    out["dock_has_app"] = any(n.strip().lower() in ("mlaser", "python") for n in out["dock_items"].split(","))
    return out


def probe_win(pid: int) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    pids = _descendants(pid) | {pid}
    wins: list[dict[str, Any]] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _l):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value in pids and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            ex = user32.GetWindowLongW(hwnd, -20)
            dark = ctypes.c_int(0)
            hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)  # type: ignore[attr-defined]
            wins.append({"hwnd": int(hwnd), "pid": p.value, "title": title.value, "class": cls.value,
                         "tool_window": bool(ex & 0x80), "app_window": bool(ex & 0x40000),
                         "dark_title_bar": bool(dark.value) if hr == 0 else None})
        return True
    user32.EnumWindows(proto(cb), 0)
    fg = user32.GetForegroundWindow()
    fgp = wintypes.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(fgp))
    return {"windows": wins, "foreground_pid": fgp.value, "is_front": fgp.value in pids}


def probe_linux(pid: int) -> dict[str, Any]:
    out: dict[str, Any] = {"active": sh(["xprop", "-root", "_NET_ACTIVE_WINDOW"])}
    wm = sh(["wmctrl", "-lpx"]) if shutil.which("wmctrl") else ""
    out["wmctrl"] = wm
    pids = {str(p) for p in _descendants(pid) | {pid}}
    ids = [ln.split()[0] for ln in wm.splitlines() if len(ln.split()) > 2 and ln.split()[2] in pids]
    out["windows"] = {i: sh(["xprop", "-id", i, "WM_CLASS", "WM_NAME", "_NET_WM_NAME", "_GTK_APPLICATION_ID",
                             "_KDE_NET_WM_DESKTOP_FILE", "_NET_WM_PID", "_KDE_NET_WM_APPMENU_SERVICE_NAME"])
                      + "\n_NET_WM_ICON: " + ("present" if "_NET_WM_ICON(CARDINAL)" in sh(["xprop", "-id", i, "-notype", "_NET_WM_ICON"], 30)[:200]
                                              else "absent") for i in ids}
    act = out["active"].replace(",", " ").split()
    act_id = next((t for t in act if t.startswith("0x") and t != "0x0"), "")
    out["active_pid"] = sh(["xprop", "-id", act_id, "_NET_WM_PID"]) if act_id else ""
    out["is_front"] = any(f"= {p}" in out["active_pid"] for p in pids)
    return out


def _descendants(pid: int) -> set[int]:
    """Child processes (``uv run`` starts Python as a child)."""
    rows: list[tuple[int, int]] = []
    if sys.platform == "win32":
        txt = sh(["powershell", "-NoProfile", "-Command",
                  "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }"], 60)
    else:
        txt = sh(["ps", "-axo", "pid=,ppid="])
    for ln in txt.splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1])))
    found, todo = set(), [pid]
    while todo:
        p = todo.pop()
        for c, pp in rows:
            if pp == p and c not in found:
                found.add(c)
                todo.append(c)
    return found


def expectations(rep: dict[str, Any], osd: dict[str, Any]) -> list[str]:
    """Failures (empty = native enough)."""
    bad = []
    menus = [m["title"].replace("&", "") for m in (rep.get("menu_bar") or {}).get("menus", [])]
    for need in ("File", "Edit", "View", "Window", "Help"):
        if need not in menus:
            bad.append(f"menu {need!r} missing from the menu bar ({menus})")
    if rep.get("in_window_menu_button"):
        bad.append("the in-window menu button is still shown")
    plat = rep.get("platform")
    if plat == "darwin":
        if osd.get("type") != "Foreground":
            bad.append(f"lsappinfo type {osd.get('type')} (want Foreground)")
        if not osd.get("is_front") and "true" not in str(osd.get("se_frontmost")):
            bad.append("not the frontmost application")
        if (rep.get("macos") or {}).get("activation_policy") != "regular":
            bad.append(f"activation policy {(rep.get('macos') or {}).get('activation_policy')}")
        if not rep.get("menu_bar", {}).get("native"):
            bad.append("the menu bar is not the native (global) one")
        if rep.get("style") not in ("macos", "macintosh"):
            bad.append(f"style {rep.get('style')} (want macos)")
        if rep.get("application_display_name", "").lower() != "mlaser":
            bad.append(f"display name {rep.get('application_display_name')!r}")
    elif plat == "win32":
        if not osd.get("is_front"):
            bad.append("not the foreground window")
        if not (rep.get("windows") or {}).get("app_user_model_id"):
            bad.append("no AppUserModelID")
        if rep.get("style") not in ("windows11", "windowsvista"):
            bad.append(f"style {rep.get('style')} (want windows11 / windowsvista)")
    else:
        if not rep.get("desktop_file_name"):
            bad.append("no desktop file name")
    if rep.get("window_icon_null"):
        bad.append("no application icon")
    return bad


def markdown(label: str, rep: dict[str, Any], osd: dict[str, Any], bad: list[str]) -> str:
    lines = [f"### {label}", ""]
    keys = ("platform", "qpa", "frozen", "executable", "application_name", "application_display_name",
            "desktop_file_name", "style", "color_scheme", "theme_setting", "app_style_sheet_len",
            "window_style_sheet_len", "window_title", "window_file_path", "window_active", "application_state",
            "in_window_menu_button", "window_icon_null")
    lines += ["| Qt says | |", "|---|---|"] + [f"| {k} | `{rep.get(k)}` |" for k in keys]
    for k in ("macos", "windows"):
        if rep.get(k):
            lines.append(f"| {k} | `{json.dumps(rep[k])}` |")
    mb = rep.get("menu_bar") or {}
    lines.append(f"| menu bar | native={mb.get('native')} menus={[m['title'] for m in mb.get('menus', [])]} |")
    lines += ["", "| OS says | |", "|---|---|"]
    for k, v in osd.items():
        if k in ("lsappinfo_full",):
            continue
        lines.append(f"| {k} | `{str(v)[:600].replace('|', '/').replace(chr(10), ' ')}` |")
    lines += ["", "**Expectations:** " + ("all met" if not bad else "; ".join(bad)), ""]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--wait", type=float, default=60, help="seconds to wait for the in-app report")
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--mac-app", help="launch this .app bundle through LaunchServices (open)")
    ap.add_argument("--expect", action="store_true")
    ap.add_argument("--summary", help="append the markdown to this file (GITHUB_STEP_SUMMARY)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=JSON",
                    help="a ConfigService value for the run (default: shell.welcome_done=true)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="mlaser-native-"))
    rep_path = (out / f"{a.label}-report.json").resolve()
    if rep_path.exists():
        rep_path.unlink()
    seed = {"shell.welcome_done": True}
    for kv in a.set:
        k, _, v = kv.partition("=")
        try:
            seed[k] = json.loads(v)
        except ValueError:
            seed[k] = v
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    (tmp / "config" / "config.json").write_text(json.dumps({"version": 1, "values": seed}), encoding="utf-8")
    env = dict(os.environ)
    env.update({"MLASER_NATIVE_REPORT": str(rep_path), "MLASER_CONFIG_DIR": str(tmp / "config"),
                "MLASER_DATA_DIR": str(tmp / "data"), "MLASER_LOG_DIR": str(tmp / "log"),
                "MLASER_APP_DIR": str(tmp / "app"), "MLASER_NO_FIRSTRUN": "1"})
    if not os.environ.get("NATIVE_CHECK_KEEP_QPA"):
        env.pop("QT_QPA_PLATFORM", None)
    if a.mac_app:
        envargs = [f"--env={k}={env[k]}" for k in ("MLASER_NATIVE_REPORT", "MLASER_CONFIG_DIR", "MLASER_DATA_DIR",
                                                   "MLASER_LOG_DIR", "MLASER_APP_DIR", "MLASER_NO_FIRSTRUN")]
        launch = ["open", "-n", "-W", *envargs, a.mac_app, "--args", *cmd]
    else:
        launch = cmd
    print("launch:", launch, flush=True)
    log = open(out / f"{a.label}-stdout.txt", "w", encoding="utf-8")
    proc = subprocess.Popen(launch, env=env, stdout=log, stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < a.wait and not rep_path.exists():
        if proc.poll() is not None and not a.mac_app:
            break
        time.sleep(0.5)
    time.sleep(a.settle)
    rep: dict[str, Any] = {}
    if rep_path.exists():
        rep = json.loads(rep_path.read_text(encoding="utf-8"))
    pid = int(rep.get("pid") or proc.pid)
    osd: dict[str, Any] = {"report_found": bool(rep), "seconds": round(time.time() - t0, 1)}
    try:
        if sys.platform == "darwin":
            osd.update(probe_mac(pid))
        elif sys.platform == "win32":
            osd.update(probe_win(pid))
        else:
            osd.update(probe_linux(pid))
    except Exception as exc:  # noqa: BLE001
        osd["probe_error"] = repr(exc)
    osd["screenshot"] = screenshot(out / f"{a.label}-screen.png")
    bad = expectations(rep, osd) if rep else ["no in-app report (the app did not start?)"]
    (out / f"{a.label}.json").write_text(json.dumps({"qt": rep, "os": osd, "failures": bad}, indent=1, default=str),
                                         encoding="utf-8")
    md = markdown(a.label, rep, osd, bad)
    (out / f"{a.label}.md").write_text(md, encoding="utf-8")
    print(md, flush=True)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    # end it
    for p in sorted(_descendants(proc.pid) | {pid}, reverse=True):
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p)], capture_output=True)
            else:
                os.kill(p, 9)
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    log.close()
    print(Path(out / f"{a.label}-stdout.txt").read_text(encoding="utf-8", errors="replace")[-3000:])
    return 1 if (a.expect and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
