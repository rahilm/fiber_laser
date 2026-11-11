"""``safe_halt`` tells the streaming child before the halt goes out (ci-win4).

The streaming child re-sends a FIFO frame whose reply did not come (``transact``'s retry,
600 ms FIFO timeout) only when its re-send check passes; that check reads the shared cancel flag.
``raise_fault`` and the Hold set the flag before their halt; ``safe_halt`` set it after the halt
sequence, so a retry falling inside the sequence could reach the card after its stop command.
The flag is a shared-memory write (no join, no logging): the halt still goes out at once.
"""
from __future__ import annotations

import threading


class _Child:
    def __init__(self, order: list):
        self.order = order

    def set_cancel(self) -> None:
        self.order.append("child cancel")

    def set_hold(self, on: bool) -> None:  # pragma: no cover - not used here
        pass


def test_safe_halt_cancels_the_streaming_child_before_the_halt(make_ctl, monkeypatch):
    ctl = make_ctl(process=False)
    order: list = []
    sup = ctl._sup
    monkeypatch.setattr(sup, "safe_halt", lambda reason, pause=False, latch=True: order.append("halt"))
    monkeypatch.setattr(sup, "safe_halt_soon",
                        lambda reason, pause=False, latch=True: order.append("halt") or threading.Event())
    release = threading.Event()
    worker = threading.Thread(target=release.wait, args=(5.0,), daemon=True)
    worker.start()
    old_worker, old_streamer = ctl._worker, ctl._streamer
    ctl._worker, ctl._streamer = worker, _Child(order)
    try:
        ctl.safe_halt("test")
    finally:
        release.set()
        worker.join(5.0)
        ctl._worker, ctl._streamer = old_worker, old_streamer
    assert "halt" in order and "child cancel" in order, order
    assert order.index("child cancel") < order.index("halt"), order
    assert ctl._cancel.is_set()
