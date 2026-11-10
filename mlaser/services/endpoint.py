"""The motion card's endpoint: one record, read the same way everywhere (architecture review A10).

The ``ip`` value of the configuration is the record: the IP dialog's addresses (``pd445`` =
the card's address ... ``pd453``) plus the transport of the card link - ``port``, ``transport``
(``udp`` / ``tcp``) and the client's timing (``timeout_ms``, ``fifo_time_ms``, ``max_recv``,
``send_interval_ms``, ``fifo_timeout_ms``, ``connect_wait_ms``), the fields of
:class:`mlaser.controller.client.TransportConfig`.  The settings import stores the values of the
original's ``File/ipAdd.ini`` there (:func:`record_from_ini`), and every reader builds its
``TransportConfig`` from it (:func:`transport_config`, :func:`resolve`): the controller made at
start (``ui/__main__.py``), Reconnect, the Machine workspace and the network self-test
(``mlaser.netcheck``).  A field the record does not hold keeps the ``TransportConfig`` default.

The supervisor's link deadline and the heartbeat are not part of the record and do not change
with it.  Qt-free.
"""
from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from typing import Any, Optional

from mlaser.controller.client import TransportConfig

#: record key -> ipAdd.ini key, for the transport fields (``pd445`` holds the address)
INI_KEYS: dict[str, str] = {
    "port": "CardPort", "transport": "AccessType", "timeout_ms": "MCTimeout",
    "fifo_time_ms": "MCFifoTime", "max_recv": "MCMaxRecvTime", "send_interval_ms": "MCSendInterval",
    "fifo_timeout_ms": "FifoTimeout", "connect_wait_ms": "ConnectWait",
}
HOST_KEY = "pd445"
DEFAULT = TransportConfig()


def _fields() -> dict[str, Any]:
    return {f.name: f for f in fields(TransportConfig)}


def transport_config(record: Optional[dict]) -> TransportConfig:
    """The ``TransportConfig`` of an ``ip`` record (defaults for what it does not hold; a value
    that does not fit is ignored, never guessed)."""
    tc = TransportConfig()
    rec = record if isinstance(record, dict) else {}
    host = str(rec.get(HOST_KEY, "") or "").strip()
    if host:
        tc.host = host
    for name in INI_KEYS:
        if name not in rec:
            continue
        v = rec[name]
        try:
            if name == "transport":
                v = str(v).lower()
                if v not in ("udp", "tcp"):
                    continue
            else:
                v = int(v)
                if v < 0 or (name in ("port", "timeout_ms") and v == 0) or (name == "port" and v > 65535):
                    continue
        except (TypeError, ValueError):
            continue
        setattr(tc, name, v)
    return tc


def record_from_ini(path: str) -> dict[str, Any]:
    """The record fields an ``ipAdd.ini`` sets (``pd445`` and the transport fields its keys
    name; read by :meth:`TransportConfig.from_ini`, the app's one ini reader)."""
    from .settings_import import read_ip_ini
    present = read_ip_ini(Path(path))
    tc = TransportConfig.from_ini(path)
    out: dict[str, Any] = {}
    if "CardIP" in present:
        out[HOST_KEY] = tc.host
    for name, ini_key in INI_KEYS.items():
        if ini_key in present:
            out[name] = getattr(tc, name)
    return out


def record_of(config: Any) -> dict:
    """The ``ip`` record of a ConfigService (or of a plain dict of values); {} when unset."""
    if config is None:
        return {}
    v = config.get("ip", None) if hasattr(config, "get") else None
    return dict(v) if isinstance(v, dict) else {}


def resolve(record: Optional[dict], host: Optional[str] = None, port: Optional[int] = None,
            ini: Optional[str] = None) -> tuple[TransportConfig, str]:
    """(``TransportConfig``, source) of the card: ``ini`` (an explicit ipAdd.ini, as before)
    else the stored record with ``host`` / ``port`` (the command line) over it.  ``source``:
    the ini path, ``command line``, ``configuration`` or ``default``."""
    if ini:
        return TransportConfig.from_ini(ini), str(ini)
    rec = record if isinstance(record, dict) else {}
    tc = transport_config(rec)
    if host or port:
        tc = replace(tc, host=host or tc.host, port=int(port or tc.port))
        return tc, "command line"
    stored = bool(str(rec.get(HOST_KEY, "") or "").strip()) or any(k in rec for k in INI_KEYS)
    return tc, ("configuration" if stored else "default")


def apply_to(ctl: Any, tc: TransportConfig) -> None:
    """Give a controller the endpoint ``tc`` (its ``cfg``); for one built without it."""
    cfg = getattr(ctl, "cfg", None)
    if cfg is None:
        return
    for name in _fields():
        setattr(cfg, name, getattr(tc, name))


__all__ = ["INI_KEYS", "HOST_KEY", "transport_config", "record_from_ini", "record_of", "resolve", "apply_to"]
