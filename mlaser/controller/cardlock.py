"""One process drives a motion card at a time (UI evaluation F6).

The card protocol has no session: the UDP socket is unbound, so two programs on the PC (two
mlaser instances, one started with ``--new-instance`` or with another configuration, or a
script) could both command the same card, and each would think it knows where the head is.
:meth:`ConnectionPart.connect <mlaser.controller.parts.connection.ConnectionPart.connect>`
therefore takes an exclusive OS lock keyed by the card's address before it opens a socket, and
:meth:`disconnect` releases it.  The lock (:class:`mlaser._oslock.FileLock`) is the operating
system's and dies with the process: a crashed or killed owner never leaves the card blocked.

The lock files live in the per-user runtime directory (:func:`mlaser._oslock.runtime_dir`,
``$MLASER_RUNTIME_DIR`` overrides it).  ``MLASER_CARD_LOCK=0`` turns the lock off (tests that
deliberately connect two controllers, development); never on a production machine.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
from typing import Optional

from mlaser._oslock import FileLock, LockHeld, runtime_dir

log = logging.getLogger("mlaser.controller.cardlock")


def enabled(environ: Optional[dict] = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get("MLASER_CARD_LOCK", "1")).strip().lower() not in ("0", "no", "false", "off")


def card_key(host: str, port: int) -> str:
    """``10.1.1.168-502``: the address normalised (IPv4/IPv6 text forms, case of a name)."""
    h = (host or "").strip().lower()
    try:
        h = str(ipaddress.ip_address(h))
    except ValueError:
        pass
    if h in ("localhost", "::1"):
        h = "127.0.0.1"
    return re.sub(r"[^a-z0-9.\-]", "_", h) + f"-{int(port)}"


def lock_path(host: str, port: int):
    return runtime_dir() / f"card-{card_key(host, port)}.lock"


class CardBusy(OSError):
    """The card is driven by another process (``owner``: what it wrote: pid, host, ...)."""

    def __init__(self, host: str, port: int, owner: dict):
        self.host, self.port, self.owner = host, int(port), owner
        pid = owner.get("pid")
        who = f"process {pid}" if pid else "another process"
        super().__init__(f"the motion card at {host}:{port} is in use by another mlaser ({who}); "
                         "close the connection there first (or close that mlaser)")


def acquire(host: str, port: int, transport: str = "udp") -> Optional[FileLock]:
    """The card's lock, held until :meth:`FileLock.release`; None when locking is off.  Raises
    :class:`CardBusy` when another process holds it."""
    if not enabled():
        return None
    lock = FileLock(lock_path(host, port))
    try:
        return lock.acquire({"what": "mlaser card connection", "card": f"{host}:{port}",
                             "transport": transport})
    except LockHeld as e:
        raise CardBusy(host, port, e.owner) from None
    except OSError as e:
        # the runtime directory is not writable: a read-only home must not make the machine
        # unusable; this lock is the second line behind the single application instance
        log.warning("card lock unavailable (%s): connecting without it", e)
        return None


__all__ = ["CardBusy", "acquire", "card_key", "enabled", "lock_path"]
