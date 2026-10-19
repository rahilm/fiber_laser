"""The parts :class:`mlaser.controller.ui_adapter.UiController` is assembled from.

* :mod:`.connection`  connect / disconnect, the status poller, alarms, interlocks, link events
* :mod:`.motion`      homing, jog (with the hold-to-jog dead-man), moves, return to zero
* :mod:`.io`          outputs, laser fire / test / off, laser power, gas type
* :mod:`.jobs`        compile, pre-flight, the job worker, pause / resume / stop, breakpoints

They share the state ``UiController.__init__`` sets up and talk to the card only through the
safety supervisor (``self.sup``; ``self._mc`` is its guarded main channel).
"""
