"""After the main loop, no worker thread runs a GUI handler or emits into an object the GUI thread
is deleting.

The window's close queues the controller's disconnect on the command worker; it ran while
``release_widgets`` deleted the window.  Its events went to the event dispatcher
(``qtbridge.QtDispatcher``), whose emit failed on the deleted window, and the bus then ran the
window's handlers on the worker thread (``EventBus.publish``'s synchronous fallback, likewise a
command ticket's completion callback): ``SafetyUI.refresh`` restyled the safety banner off the
GUI thread while the GUI thread destroyed it.  Seen as "Could not parse stylesheet" on the
banner and "malloc(): unsorted double linked list corrupted" (exit code -6, Python 3.12), and as
a segmentation fault in ``release_widgets`` (exit code -11, Python 3.14 CI).  Now a delivery from
another thread to a closed or deleted dispatcher is dropped, and ``release_widgets`` closes the
dispatchers and background calls before it deletes anything."""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("PySide6")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _on_thread(fn) -> None:
    t = threading.Thread(target=fn, name="worker")
    t.start()
    t.join(10)
    assert not t.is_alive()


def _bus_with(disp):
    from mlaser.machine import events as EV
    bus = EV.EventBus()
    bus.set_dispatcher(disp)
    ran: list = []
    bus.subscribe(EV.Event, lambda ev: ran.append(threading.current_thread().name))
    return bus, ran, EV


def test_event_from_a_worker_to_a_deleted_dispatcher_is_not_run_on_the_worker(app):
    import shiboken6

    from mlaser.ui.qtbridge import QtDispatcher
    disp = QtDispatcher()
    bus, ran, EV = _bus_with(disp)
    shiboken6.delete(disp)                       # the window (its parent) was deleted
    _on_thread(lambda: bus.publish(EV.Log("disconnected")))
    assert ran == [], f"a GUI handler ran on {ran}"
    assert bus.handler_errors == 0


def test_event_from_a_worker_to_a_closed_dispatcher_is_dropped(app):
    from mlaser.ui.qtbridge import QtDispatcher
    disp = QtDispatcher()
    bus, ran, EV = _bus_with(disp)
    disp.close()
    _on_thread(lambda: bus.publish(EV.Log("disconnected")))
    app.processEvents()
    assert ran == [] and disp.ui_thread is None
    bus.publish(EV.Log("on the GUI thread"))     # the GUI thread's own deliveries still run
    assert ran == [threading.current_thread().name]


def test_command_callback_after_the_window_was_deleted_is_not_run_on_the_worker(app):
    import shiboken6

    from mlaser.controller.commands import CommandExecutor
    from mlaser.ui.qtbridge import QtDispatcher
    disp = QtDispatcher()
    ex = CommandExecutor(dispatch=lambda: disp)
    t = ex.submit("disconnect", lambda: None)
    t.wait(10)
    shiboken6.delete(disp)
    ran: list = []
    _on_thread(lambda: t.then(lambda _t: ran.append(threading.current_thread().name)))
    assert ran == []


def test_closed_background_call_delivers_nothing(app):
    from mlaser.ui.qtbridge import BackgroundCall
    go = threading.Event()
    call = BackgroundCall()
    done: list = []
    call.start(lambda: go.wait(10), lambda res, err: done.append(res))
    call.close()
    go.set()
    call._thread.join(10)
    app.processEvents()
    call._deliver()
    assert done == []


def test_release_widgets_closes_the_bridges_before_deleting(app):
    import shiboken6
    from PySide6.QtWidgets import QWidget

    from mlaser.ui import __main__ as ui_main
    from mlaser.ui.qtbridge import BackgroundCall, QtDispatcher
    seen: list = []

    class D(QtDispatcher):
        def close(self) -> None:
            seen.append(("dispatcher", shiboken6.isValid(win)))
            super().close()

    class B(BackgroundCall):
        def close(self) -> None:
            seen.append(("call", shiboken6.isValid(win)))
            super().close()

    win = QWidget()
    inner = QWidget(win)
    D(win)
    B(inner)
    ui_main.release_widgets(app)
    assert sorted(seen) == [("call", True), ("dispatcher", True)]
    assert not shiboken6.isValid(win)
