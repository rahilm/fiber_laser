"""Operating-system power and scheduling hints for a machine controller.

* **macOS App Nap.**  A process macOS considers in the background (window hidden or covered, or
  no window at all, as on a CI runner) gets its timers coalesced and its priority lowered: the
  GUI's 100 ms safety refresh, the run bar and the jog dead-man would run late.
  :func:`app_activity` holds an ``NSProcessInfo`` activity (user initiated, latency critical,
  idle *system* sleep still allowed) for the life of the process, which opts it out.
* **Sleep during a job.**  :func:`keep_awake` (``True`` while a job runs) stops idle system sleep:
  macOS through a second activity (``NSActivityIdleSystemSleepDisabled``), Windows with
  ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``, Linux with a logind inhibitor
  (``org.freedesktop.login1.Manager.Inhibit`` "sleep:idle", held by a ``systemd-inhibit``
  child for the job's duration; ``gnome-session-inhibit`` where there is no systemd).  The
  display may still sleep.  docs/user/run.md still says to disable suspend on the machine PC.

Everything here is best effort and never raises: a missing framework or an old OS leaves the
behaviour as it was.  Qt-free, and no machine imports (the CLI and the streaming child may use it).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Any, Optional

log = logging.getLogger("mlaser.power")

# NSActivityOptions (Foundation/NSProcessInfo.h)
NS_IDLE_SYSTEM_SLEEP_DISABLED = 1 << 20
NS_USER_INITIATED = 0x00FFFFFF | NS_IDLE_SYSTEM_SLEEP_DISABLED
NS_USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP = NS_USER_INITIATED & ~NS_IDLE_SYSTEM_SLEEP_DISABLED
NS_LATENCY_CRITICAL = 0xFF00000000

# SetThreadExecutionState (winbase.h)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_lock = threading.Lock()
_app_token: Optional[int] = None
_job_token: Optional[int] = None
_win_awake = False
_linux_inhibitor: Optional["subprocess.Popen"] = None
_linux_tried = False                 # one attempt per job (the inhibitor may be refused)


class _ObjC:
    """The few Objective-C runtime calls needed, through ctypes (no PyObjC dependency).  Each
    ``objc_msgSend`` use gets its own exact prototype (required on arm64)."""

    def __init__(self) -> None:
        path = ctypes.util.find_library("objc")
        if not path:
            raise OSError("libobjc not found")
        self.lib = ctypes.cdll.LoadLibrary(path)
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.lib.objc_getClass.argtypes = [ctypes.c_char_p]
        self.lib.sel_registerName.restype = ctypes.c_void_p
        self.lib.sel_registerName.argtypes = [ctypes.c_char_p]
        self._send = ctypes.cast(self.lib.objc_msgSend, ctypes.c_void_p).value

    def send(self, restype: Any, obj: int, selector: str, *args: tuple) -> Any:
        """``[obj selector args...]``; ``args`` are (ctype, value) pairs."""
        proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *[t for t, _v in args])
        fn = proto(self._send)
        return fn(obj, self.lib.sel_registerName(selector.encode()), *[v for _t, v in args])

    def cls(self, name: str) -> int:
        c = self.lib.objc_getClass(name.encode())
        if not c:
            raise OSError(f"no Objective-C class {name}")
        return c

    def nsstring(self, text: str) -> int:
        return self.send(ctypes.c_void_p, self.cls("NSString"), "stringWithUTF8String:",
                         (ctypes.c_char_p, text.encode("utf-8")))


_objc: Optional[_ObjC] = None


def _mac() -> Optional[_ObjC]:
    global _objc
    if sys.platform != "darwin":
        return None
    if _objc is None:
        try:
            _objc = _ObjC()
        except Exception as exc:  # noqa: BLE001 - best effort
            log.info("macOS activity API unavailable: %s", exc)
            return None
    return _objc


def _begin(options: int, reason: str) -> Optional[int]:
    oc = _mac()
    if oc is None:
        return None
    try:
        pinfo = oc.send(ctypes.c_void_p, oc.cls("NSProcessInfo"), "processInfo")
        token = oc.send(ctypes.c_void_p, pinfo, "beginActivityWithOptions:reason:",
                        (ctypes.c_uint64, options), (ctypes.c_void_p, oc.nsstring(reason)))
        if token:
            oc.send(ctypes.c_void_p, token, "retain")      # ours until endActivity
        return token or None
    except Exception as exc:  # noqa: BLE001 - best effort
        log.info("macOS activity not started: %s", exc)
        return None


def _end(token: Optional[int]) -> None:
    oc = _mac()
    if oc is None or not token:
        return
    try:
        pinfo = oc.send(ctypes.c_void_p, oc.cls("NSProcessInfo"), "processInfo")
        oc.send(None, pinfo, "endActivity:", (ctypes.c_void_p, token))
        oc.send(None, token, "release")
    except Exception as exc:  # noqa: BLE001 - best effort
        log.info("macOS activity not ended: %s", exc)


def app_activity(reason: str = "mlaser controls a laser cutter") -> bool:
    """Opt the process out of App Nap for its lifetime (macOS; no-op elsewhere).  Idempotent;
    True when an activity is held."""
    global _app_token
    with _lock:
        if _app_token is None:
            _app_token = _begin(NS_USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP | NS_LATENCY_CRITICAL, reason)
            if _app_token:
                log.info("macOS App Nap disabled (latency-critical activity)")
        return _app_token is not None


def keep_awake(on: bool, reason: str = "a laser job is running") -> bool:
    """While ``on``: no idle system sleep (macOS, Windows).  Cheap to call on every refresh: it
    acts only on a change.  Returns whether the hint is held."""
    global _job_token, _win_awake
    with _lock:
        if sys.platform == "darwin":
            if on and _job_token is None:
                _job_token = _begin(NS_USER_INITIATED | NS_LATENCY_CRITICAL, reason)
            elif not on and _job_token is not None:
                _end(_job_token)
                _job_token = None
            return _job_token is not None
        if sys.platform.startswith("linux"):
            return _linux_keep_awake(on, reason)
        if sys.platform == "win32":
            if on != _win_awake:
                try:
                    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
                    ok = ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint32(flags))  # type: ignore[attr-defined]
                    _win_awake = on if ok else False
                except Exception as exc:  # noqa: BLE001 - best effort
                    log.info("SetThreadExecutionState failed: %s", exc)
            return _win_awake
        return False


def linux_inhibit_command(reason: str, which=shutil.which) -> Optional[list[str]]:
    """The command that holds a sleep/idle inhibitor while it runs (None: none available)."""
    if which("systemd-inhibit"):
        return ["systemd-inhibit", "--what=sleep:idle", "--who=mlaser", f"--why={reason}", "--mode=block",
                "sleep", "infinity"]
    if which("gnome-session-inhibit"):
        return ["gnome-session-inhibit", "--inhibit", "suspend:idle", "--reason", reason, "sleep", "infinity"]
    return None


def _linux_keep_awake(on: bool, reason: str) -> bool:
    global _linux_inhibitor, _linux_tried
    proc = _linux_inhibitor
    if on and proc is None and not _linux_tried:
        _linux_tried = True
        cmd = linux_inhibit_command(reason)
        if cmd is None or os.environ.get("MLASER_NO_INHIBIT"):
            return False
        try:
            _linux_inhibitor = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, start_new_session=True)
            log.info("sleep inhibited while the job runs (%s)", cmd[0])
        except OSError as exc:
            log.info("no sleep inhibitor: %s", exc)
            _linux_inhibitor = None
    elif not on:
        _linux_tried = False
    if not on and proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        _linux_inhibitor = None
    return _linux_inhibitor is not None and _linux_inhibitor.poll() is None


__all__ = ["app_activity", "keep_awake", "linux_inhibit_command"]
