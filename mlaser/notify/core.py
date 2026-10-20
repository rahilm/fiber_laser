"""The notifier: filter, rate-limit and queue notifications; one worker thread sends them.

:meth:`Notifier.emit` is called from wherever an event is seen (the GUI thread, an event-bus
handler on the streamer's or the poller's thread).  It never blocks and never raises: it reads
the settings, applies the per-event switch, the quiet hours and the rate limits, and puts the
notification on a bounded queue (a full queue drops it and counts the drop).  The daemon worker
looks the channel secrets up and sends with short timeouts; every result is logged and kept in
:attr:`Notifier.results` for the settings dialog.

Settings (a mapping, normally the plugin's ``plugin.ext_notify.*`` settings; see
:data:`SETTING_DEFAULTS` for every key): ``enabled``, the channels (``ntfy_*``, ``email_*``,
``smtp_*``, ``webhook_*``), per event ``event_<kind>`` (on/off) and ``channels_<kind>``
(``all`` or a comma list of ``ntfy, email, webhook``), ``min_interval_s`` (the same
notification, e.g. the same alarm, at most once per interval), ``max_per_hour`` (a token bucket
over every notification, burst :data:`BURST`), ``quiet_start`` / ``quiet_end`` (``HH:MM``, local
time; nothing but alarms in between, and alarms too unless ``quiet_alarms``).

Nothing can be controlled through a notification: the channels only send.
"""
from __future__ import annotations

import collections
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import backends as B

log = logging.getLogger("mlaser.notify")

EVENTS = ("sheet_done", "queue_waiting", "alarm", "job_finished", "maintenance_due")
TEST = "test"
#: ntfy priorities (1 min .. 5 urgent); e-mail and webhook carry them as data
PRIORITY = {"alarm": 5, "queue_waiting": 4, "sheet_done": 3, "job_finished": 3, "maintenance_due": 3, TEST: 2}
TAGS = {"alarm": ("rotating_light",), "queue_waiting": ("hourglass",), "sheet_done": ("white_check_mark",),
        "job_finished": ("checkered_flag",), "maintenance_due": ("wrench",), TEST: ("test_tube",)}
#: secret names in the SecretStore, per channel
SECRETS = {"ntfy": "notify.ntfy_token", "email": "notify.smtp_password", "webhook": "notify.webhook_secret"}
QUEUE_SIZE = 64
BURST = 10.0

SETTING_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "machine_name": "",
    "include_simulator": False,
    "ntfy_enabled": False, "ntfy_server": "https://ntfy.sh", "ntfy_topic": "",
    "email_enabled": False, "smtp_host": "", "smtp_port": 587, "smtp_security": 0,   # 0 STARTTLS, 1 SSL, 2 none
    "smtp_user": "", "email_from": "", "email_to": "",
    "webhook_enabled": False, "webhook_url": "",
    "min_interval_s": 60.0, "max_per_hour": 60.0,
    "quiet_start": "", "quiet_end": "", "quiet_alarms": True,
    **{f"event_{k}": True for k in EVENTS},
    **{f"channels_{k}": "all" for k in EVENTS},
}
SECURITY = ("starttls", "ssl", "none")


@dataclass(frozen=True)
class Notification:
    kind: str                         # one of EVENTS or TEST
    title: str
    message: str
    key: str = ""                     # dedupe key (the same alarm, the same sheet); "" = kind
    priority: int = 3
    tags: tuple = ()
    data: dict = field(default_factory=dict)
    t: float = field(default_factory=time.time)


def make(kind: str, title: str, message: str, key: str = "", **data: Any) -> Notification:
    return Notification(kind, title, message, key or kind, PRIORITY.get(kind, 3), TAGS.get(kind, ()), data)


@dataclass(frozen=True)
class Result:
    t: float
    kind: str
    channel: str
    ok: bool
    detail: str


def _hhmm(s: Any) -> Optional[int]:
    try:
        h, m = str(s).strip().split(":")
        v = int(h) * 60 + int(m)
        return v if 0 <= v < 24 * 60 else None
    except (ValueError, AttributeError):
        return None


class Notifier:
    """See the module docstring.  ``settings``: a mapping (or a callable returning one);
    ``secrets``: a :class:`~mlaser.notify.secrets.SecretStore` (or anything with ``get``)."""

    def __init__(self, settings: Any, secrets: Any = None, senders: Optional[dict] = None,
                 clock: Callable[[], float] = time.time, queue_size: int = QUEUE_SIZE,
                 timeout_s: float = B.TIMEOUT_S, start: bool = True) -> None:
        self._settings = settings
        self.secrets = secrets
        self.senders = dict(senders or B.SENDERS)
        self.clock = clock
        self.timeout_s = timeout_s
        self.results: collections.deque[Result] = collections.deque(maxlen=50)
        self.counts = {"queued": 0, "sent": 0, "failed": 0, "dropped": 0, "limited": 0, "suppressed": 0}
        self.listeners: list[Callable[[Result], None]] = []      # called on the worker thread
        self._q: queue.Queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._tokens = BURST
        self._t_tokens = clock()
        self._busy = 0
        self._idle = threading.Event()
        self._idle.set()
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        if start:
            self.start()

    # ------------------------------------------------------------------ settings
    def setting(self, key: str) -> Any:
        m = self._settings() if callable(self._settings) else self._settings
        try:
            v = m[key]
        except (KeyError, TypeError):
            v = None
        return SETTING_DEFAULTS.get(key) if v is None else v

    def channel_config(self, channel: str) -> dict:
        s = self.setting
        if channel == "ntfy":
            return {"server": s("ntfy_server"), "topic": s("ntfy_topic")}
        if channel == "email":
            sec = s("smtp_security")
            if isinstance(sec, (int, float)):
                sec = SECURITY[int(sec)] if 0 <= int(sec) < len(SECURITY) else "starttls"
            return {"host": s("smtp_host"), "port": s("smtp_port"), "security": sec, "user": s("smtp_user"),
                    "from": s("email_from"), "to": s("email_to")}
        if channel == "webhook":
            return {"url": s("webhook_url"), "machine": s("machine_name")}
        return {}

    def enabled_channels(self) -> list[str]:
        return [c for c in B.CHANNELS if bool(self.setting(f"{c}_enabled"))]

    def channels_for(self, kind: str) -> list[str]:
        on = self.enabled_channels()
        if kind == TEST:
            return on
        want = str(self.setting(f"channels_{kind}") or "all").strip().lower()
        if want in ("", "all", "*"):
            return on
        names = {w.strip() for w in want.replace(";", ",").split(",")}
        return [c for c in on if c in names]

    def quiet_now(self, kind: str) -> bool:
        a, b = _hhmm(self.setting("quiet_start")), _hhmm(self.setting("quiet_end"))
        if a is None or b is None or a == b:
            return False
        if kind == "alarm" and bool(self.setting("quiet_alarms")):
            return False
        lt = time.localtime(self.clock())
        now = lt.tm_hour * 60 + lt.tm_min
        return (a <= now < b) if a < b else (now >= a or now < b)

    # ------------------------------------------------------------------ emit (any thread)
    def emit(self, n: Notification, force: bool = False) -> bool:
        """Queue ``n``; True when it was queued.  ``force`` (the test send) skips the event
        switches, the quiet hours and the rate limits.  Never blocks, never raises."""
        try:
            return self._emit(n, force)
        except Exception:  # noqa: BLE001 - a notification must never break its caller
            log.exception("notifications: emit failed")
            return False

    def _emit(self, n: Notification, force: bool) -> bool:
        if self._closed:
            return False
        if not force:
            if not bool(self.setting("enabled")) or (n.kind != TEST and not bool(self.setting(f"event_{n.kind}"))):
                self.counts["suppressed"] += 1
                return False
            if self.quiet_now(n.kind):
                self.counts["suppressed"] += 1
                return False
        channels = self.channels_for(n.kind)
        if not channels:
            self.counts["suppressed"] += 1
            return False
        if not force and not self._rate_ok(n):
            self.counts["limited"] += 1
            log.info("notifications: %s rate-limited", n.kind)
            return False
        cfgs = {c: self.channel_config(c) for c in channels}
        try:
            with self._lock:
                self._busy += 1
                self._idle.clear()
            self._q.put_nowait((n, cfgs))
        except queue.Full:
            self._done_one()
            self.counts["dropped"] += 1
            log.warning("notifications: queue full, %s dropped", n.kind)
            return False
        self.counts["queued"] += 1
        return True

    def _rate_ok(self, n: Notification) -> bool:
        now = self.clock()
        with self._lock:
            key = f"{n.kind}:{n.key or n.kind}"
            interval = float(self.setting("min_interval_s") or 0.0)
            last = self._last.get(key)
            if last is not None and now - last < interval:
                return False
            per_h = float(self.setting("max_per_hour") or 0.0)
            if per_h > 0:
                self._tokens = min(BURST, self._tokens + (now - self._t_tokens) * per_h / 3600.0)
                self._t_tokens = now
                if self._tokens < 1.0:
                    return False
                self._tokens -= 1.0
            self._last[key] = now
            if len(self._last) > 512:
                for k in sorted(self._last, key=self._last.__getitem__)[:256]:
                    del self._last[k]
            return True

    # ------------------------------------------------------------------ worker
    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._closed = False
            self._thread = threading.Thread(target=self._run, name="mlaser-notify", daemon=True)
            self._thread.start()

    def _done_one(self) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)
            if self._busy == 0:
                self._idle.set()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                self._done_one()
                return
            n, cfgs = item
            try:
                for channel, cfg in cfgs.items():
                    self._send(n, channel, cfg)
            finally:
                self._done_one()

    def _send(self, n: Notification, channel: str, cfg: dict) -> None:
        sender = self.senders.get(channel)
        try:
            if sender is None:
                raise B.ChannelError(f"unknown channel {channel}")
            secret = ""
            if self.secrets is not None and channel in SECRETS:
                secret = self.secrets.get(SECRETS[channel]) or ""
            detail = str(sender(n, cfg, secret, self.timeout_s) or "ok")
            res = Result(self.clock(), n.kind, channel, True, detail)
            self.counts["sent"] += 1
            log.info("notification %s sent via %s (%s)", n.kind, channel, detail)
        except Exception as exc:  # noqa: BLE001 - logged and shown, never raised
            res = Result(self.clock(), n.kind, channel, False, f"{type(exc).__name__}: {exc}")
            self.counts["failed"] += 1
            log.warning("notification %s via %s failed: %s", n.kind, channel, exc)
        self.results.append(res)
        for fn in list(self.listeners):
            try:
                fn(res)
            except Exception:  # noqa: BLE001
                log.exception("notification listener failed")

    def flush(self, timeout: float = 30.0) -> bool:
        """Wait until everything queued was sent (tests, closing)."""
        return self._idle.wait(timeout)

    def close(self, timeout: float = 2.0) -> None:
        self._closed = True
        t = self._thread
        if t is None:
            return
        try:
            with self._lock:
                self._busy += 1
                self._idle.clear()
            self._q.put_nowait(None)
        except queue.Full:
            self._done_one()
        t.join(timeout)
        self._thread = None


__all__ = ["Notifier", "Notification", "Result", "make", "EVENTS", "TEST", "PRIORITY", "SECRETS",
           "SETTING_DEFAULTS", "SECURITY", "QUEUE_SIZE", "BURST"]
