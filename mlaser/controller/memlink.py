"""An in-memory card link for tests: the request bytes go straight to
:meth:`CardSimulator.handle() <mlaser.controller.simulator.CardSimulator.handle>` and its reply
bytes come back, with no socket and no simulator thread (docs/dev/TESTING.md, "A seam under the
job runner").

The bytes are exactly the ones a UDP card would see: the client encodes and decodes every
frame (tid, CRC, retries, the stale-reply rule) as it does on the network.  A request the
simulator drops (``sim.inject(link_down=True)``, reply loss) is a receive timeout, which waits
the timeout on the controller's clock (:class:`~mlaser.controller.clock.ManualClock`: virtual
time, no wall-clock wait).
"""
from __future__ import annotations

import collections
from typing import Optional


class MemoryLink:
    """One channel's link (the main and the halt channel each have their own)."""

    def __init__(self, sim, clock) -> None:
        self.sim = sim
        self.clock = clock
        self._pending: collections.deque = collections.deque()

    def send(self, data: bytes) -> None:
        out = self.sim.handle(bytes(data))
        if out is not None:
            self._pending.append(out)

    def recv(self, timeout_s: float) -> Optional[bytes]:
        if self._pending:
            return self._pending.popleft()
        self.clock.sleep(timeout_s)          # nobody answered: the timeout passes
        return self._pending.popleft() if self._pending else None

    def drain(self) -> None:
        """FUN_100061f0: one pending reply is dropped before a send (as on the socket)."""
        if self._pending:
            self._pending.popleft()

    def close(self) -> None:
        self._pending.clear()


__all__ = ["MemoryLink"]
