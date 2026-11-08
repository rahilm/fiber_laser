"""One mlaser per user session (UI evaluation F6), on every platform.

A second launch (a double-click in the file manager, ``mlaser part.dxf`` in a terminal, the
Windows jump list, the Linux desktop file's ``%F``) does not start a second application with a
second controller and a second writer of ``config.json``: it hands its file arguments to the
running instance, which opens them and comes to the front, and exits (exit code 0).  On macOS
LaunchServices already routes Finder and Dock opens to the running app as a QFileOpenEvent
(``native.create_application``); a CLI launch now behaves the same, and the forwarded files
take exactly that path (``app.file_open_handlers``: ``NativeIntegration.open_file``).

How it works:

* **who is first** is decided by an OS lock (:class:`mlaser._oslock.FileLock`) on
  ``<runtime dir>/instance-<key>.lock``, held for the life of the process and released by the
  OS when it exits or crashes: no stale state, no PID reuse;
* the first instance listens on a ``QLocalServer`` (a Unix domain socket in the runtime
  directory on macOS / Linux, a named pipe on Windows, user-only access) named after the same
  key; because it holds the lock, it removes a socket a crashed predecessor left behind
  before it listens;
* a later instance connects and speaks a small versioned protocol: one JSON object per line.
  The server greets with ``{"app": "mlaser", "proto": 1, "pid": N}``, the client sends
  ``{"proto": 1, "cmd": "open", "files": [...absolute paths], "cwd": ...}`` and the server
  answers ``{"ok": true}`` (the files are opened after the answer, so a progress dialog in the
  first instance never makes the second wait).  A different ``proto`` is refused with a clear
  message, never guessed at.  On Windows the client first calls ``AllowSetForegroundWindow``
  for the server's PID, so the raised window really comes to the front.

The **key** is the user name and the configuration directory (``services.config.config_dir``):
one instance per user and per configuration, so two instances never write the same
``config.json``.  ``--new-instance`` (or ``MLASER_SINGLE_INSTANCE=0``) starts another one
anyway (tests, development); the card lock (:mod:`mlaser.controller.cardlock`) still keeps two
processes from driving the same card.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import sys
import time
from typing import Any, Callable, Optional

from mlaser._oslock import FileLock, LockHeld, runtime_dir

log = logging.getLogger("mlaser.single_instance")

PROTO = 1
CONNECT_TIMEOUT_S = 10.0      # the first instance may still be starting (lock taken, not yet listening)
REPLY_TIMEOUT_MS = 5000


def enabled(environ: Optional[dict] = None) -> bool:
    """False with ``MLASER_SINGLE_INSTANCE=0`` (tests, development)."""
    env = os.environ if environ is None else environ
    return str(env.get("MLASER_SINGLE_INSTANCE", "1")).strip().lower() not in ("0", "no", "false", "off")


def instance_key(config_dir: Optional[str] = None) -> str:
    """A short stable key of (user, configuration directory)."""
    if config_dir is None:
        from mlaser.services.config import config_dir as _cd
        config_dir = str(_cd())
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name (a container): the uid / profile still separates
        user = str(getattr(os, "getuid", lambda: "")())
    raw = f"{user}\0{os.path.normcase(os.path.abspath(config_dir))}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def server_name(key: str) -> str:
    """The QLocalServer name: a socket path in the runtime directory on Unix (short enough for
    ``sun_path``, else a plain name in Qt's temp directory), a pipe name on Windows."""
    if sys.platform.startswith("win"):
        return f"mlaser-{key}"
    path = os.path.join(str(runtime_dir()), f"instance-{key}.sock")
    return path if len(path.encode()) < 100 else f"mlaser-{key}"


def _encode(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def _decode(line: bytes) -> dict:
    v = json.loads(line.decode("utf-8"))
    if not isinstance(v, dict):
        raise ValueError("not an object")
    return v


class InstanceError(RuntimeError):
    """The running instance could not be reached or refused the request."""


class SingleInstance:
    """The instance guard of one process.  ``claim()`` returns True for the first instance
    (it now holds the lock and listens); False when another one runs (then call
    :meth:`forward`)."""

    def __init__(self, key: Optional[str] = None):
        self.key = key or instance_key()
        self.lock = FileLock(runtime_dir() / f"instance-{self.key}.lock")
        self.name = server_name(self.key)
        self.server: Any = None
        self.owner: dict = {}
        #: called with no arguments when another launch asks to be shown (raise the window)
        self.on_activate: Optional[Callable[[], None]] = None
        #: called with the list of files of another launch (default: the QFileOpenEvent path)
        self.on_files: Optional[Callable[[list[str]], None]] = None
        self.requests = 0

    # ------------------------------------------------------------------ first instance
    def claim(self) -> bool:
        try:
            self.lock.acquire({"what": "mlaser instance", "server": self.name})
        except LockHeld as e:
            self.owner = e.owner
            return False
        self._listen()
        return True

    def _listen(self) -> None:
        from PySide6.QtNetwork import QLocalServer
        srv = QLocalServer()
        srv.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        # we hold the lock: a socket left by a crashed instance is stale, remove it first
        QLocalServer.removeServer(self.name)
        if not srv.listen(self.name):
            log.warning("single instance: cannot listen on %s (%s); other launches start their own",
                        self.name, srv.errorString())
            return
        srv.newConnection.connect(self._new_connection)
        self.server = srv

    def _new_connection(self) -> None:
        srv = self.server
        while srv is not None and srv.hasPendingConnections():
            sock = srv.nextPendingConnection()
            if sock is None:
                break
            buf = bytearray()
            sock.write(_encode({"app": "mlaser", "proto": PROTO, "pid": os.getpid()}))
            sock.flush()

            done = [False]

            def ready(sock=sock, buf=buf, done=done) -> None:
                if done[0]:
                    return
                buf += bytes(sock.readAll().data())
                if b"\n" not in buf:
                    if len(buf) > 1 << 20:
                        sock.abort()
                    return
                done[0] = True                  # one request per connection
                line = bytes(buf[: buf.index(b"\n")])
                reply, files = self._handle(line)
                sock.write(_encode(reply))
                sock.flush()
                sock.disconnectFromServer()
                if reply.get("ok"):
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(0, lambda: self._serve(files))

            sock.readyRead.connect(ready)
            sock.disconnected.connect(sock.deleteLater)

    def _handle(self, line: bytes) -> tuple[dict, list[str]]:
        try:
            req = _decode(line)
        except ValueError:
            return {"ok": False, "error": "bad request"}, []
        if req.get("proto") != PROTO:
            log.warning("single instance: a launch speaking protocol %r asked (this is %d)",
                        req.get("proto"), PROTO)
            return {"ok": False, "proto": PROTO,
                    "error": f"protocol {req.get('proto')!r} is not {PROTO}: a different mlaser "
                             "version is running"}, []
        if req.get("cmd") != "open":
            return {"ok": False, "error": f"unknown command {req.get('cmd')!r}"}, []
        files = [str(f) for f in (req.get("files") or []) if isinstance(f, str) and f]
        self.requests += 1
        return {"ok": True}, files

    def _serve(self, files: list[str]) -> None:
        log.info("single instance: another launch handed over %d file(s)", len(files))
        try:
            if self.on_activate is not None:
                self.on_activate()
        except Exception:  # noqa: BLE001 - activation is best effort
            log.exception("single instance: activation failed")
        if not files:
            return
        if self.on_files is not None:
            self.on_files(files)
            return
        open_files(files)

    def close(self) -> None:
        srv, self.server = self.server, None
        if srv is not None:
            try:
                srv.close()
            except RuntimeError:
                pass
        self.lock.release()

    # ------------------------------------------------------------------ a later instance
    def forward(self, files: list[str], timeout_s: float = CONNECT_TIMEOUT_S) -> dict:
        """Hand ``files`` (made absolute here) to the running instance; its answer.  Raises
        :class:`InstanceError` when it cannot be reached in time or refuses."""
        from PySide6.QtNetwork import QLocalSocket
        paths = [os.path.abspath(f) for f in files]
        deadline = time.monotonic() + timeout_s
        sock = QLocalSocket()
        while True:
            sock.connectToServer(self.name)
            if sock.waitForConnected(500):
                break
            if time.monotonic() > deadline:
                raise InstanceError(f"the running mlaser (process {self.owner.get('pid', '?')}) "
                                    f"does not answer on {self.name}: {sock.errorString()}")
            time.sleep(0.1)
        try:
            hello = self._read_line(sock)
            if hello.get("app") != "mlaser":
                raise InstanceError("not an mlaser instance on the other end")
            if hello.get("proto") != PROTO:
                raise InstanceError(f"the running mlaser speaks protocol {hello.get('proto')!r}, "
                                    f"this one {PROTO}: close it, then start this version")
            allow_foreground(hello.get("pid"))
            sock.write(_encode({"proto": PROTO, "cmd": "open", "files": paths, "cwd": os.getcwd()}))
            sock.flush()
            sock.waitForBytesWritten(REPLY_TIMEOUT_MS)
            reply = self._read_line(sock)
        finally:
            sock.abort()
        if not reply.get("ok"):
            raise InstanceError(str(reply.get("error") or "refused"))
        return reply

    @staticmethod
    def _read_line(sock) -> dict:
        buf = b""
        t_end = time.monotonic() + REPLY_TIMEOUT_MS / 1000
        while b"\n" not in buf:
            left = int((t_end - time.monotonic()) * 1000)
            if left <= 0 or (not sock.bytesAvailable() and not sock.waitForReadyRead(left)):
                if b"\n" in buf:
                    break
                raise InstanceError("no answer from the running mlaser")
            buf += bytes(sock.readAll().data())
        try:
            return _decode(buf[: buf.index(b"\n")])
        except ValueError as e:
            raise InstanceError(f"garbled answer from the running mlaser ({e})") from None


def open_files(files: list[str]) -> None:
    """The files of another launch, the way a QFileOpenEvent delivers them (macOS Finder /
    Dock): to ``app.file_open_handlers``, or queued in ``app.pending_files`` until the window
    exists (``native.create_application``)."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    handlers = list(getattr(app, "file_open_handlers", None) or [])
    for path in files:
        if handlers:
            for fn in handlers:
                fn(path)
        elif hasattr(app, "pending_files"):
            app.pending_files.append(path)


def activate_window(win) -> None:
    """Bring ``win`` to the front: un-minimise, raise, activate (macOS: activate the app)."""
    from PySide6.QtCore import Qt
    if win.windowState() & Qt.WindowMinimized:
        win.setWindowState((win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
    if not win.isVisible():
        win.show()
    win.raise_()
    win.activateWindow()
    from . import native
    if native.platform_id() == native.MACOS:
        native.mac_activate()
    else:
        from PySide6.QtWidgets import QApplication
        QApplication.alert(win, 1500)          # where the WM refuses focus stealing: attention


def allow_foreground(pid: Any) -> None:
    """Windows: let the running instance take the foreground (it is not the process the user
    just started, so the OS would only flash its taskbar button)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(int(pid) if pid else -1)  # -1 = ASFW_ANY
    except Exception as exc:  # noqa: BLE001
        log.info("AllowSetForegroundWindow failed: %s", exc)


__all__ = ["PROTO", "SingleInstance", "InstanceError", "enabled", "instance_key", "server_name",
           "open_files", "activate_window", "allow_foreground"]
