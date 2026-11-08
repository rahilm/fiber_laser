"""The state display (UX audit W1, W2, W4, W8, W9, W10, W13, W15, W18, O2, O7, O8): one state
vocabulary on two named axes, ISA-101-style colours that read in every theme, one set of run
controls per screen, the dock's enabling with reasons, the hold-to-fire laser button, one clock
for the run figures, permanent status-bar chips, the interlock chip and the OFFLINE banner."""
from __future__ import annotations

import colorsys
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from mlaser.services.operator import JobClock, run_status  # noqa: E402
from mlaser.ui.shell import theme as T  # noqa: E402
from mlaser.ui.shell.contrast import contrast  # noqa: E402

THEMES = (T.LIGHT, T.DARK, T.HIGH_CONTRAST)
NORMAL = ("READY", "DISARMED", "DRY-RUN", "SIMULATOR", "HOLD", "NOT-HOMED", "OFFLINE", "RUNNING")


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    from wiring import dispose, real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    w.resize(1280, 800)
    w.show()
    app.processEvents()
    yield w
    w.ctl.stop()
    dispose(w)


def pump(app, s=0.15):
    t = time.monotonic() + s
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.01)


def _hsv(c: str) -> tuple[float, float, float]:
    q = QColor(c)
    return colorsys.rgb_to_hsv(q.redF(), q.greenF(), q.blueF())


# ------------------------------------------------------------------------- W1 colours
@pytest.mark.parametrize("theme", THEMES)
def test_state_colours_read_and_follow_isa101(theme):
    t = T.STATE_TOKENS[theme]
    assert set(t) >= set(NORMAL) | set(T.ABNORMAL_STATES)
    for kind, (bg, fg) in t.items():
        assert contrast(bg, fg) >= 4.5, (theme, kind, bg, fg, contrast(bg, fg))
    for kind in NORMAL:                                   # normal states: grey / slate, no hue
        _h, s, _v = _hsv(t[kind][0])
        assert s <= 0.3, (theme, kind, t[kind][0])
    h, s, _v = _hsv(t["ARMED"][0])                        # ARMED: the amber hazard colour
    assert 0.08 <= h <= 0.16 and s > 0.6, (theme, t["ARMED"])
    for kind in ("ALARM", "FAULT", "E-STOP"):             # ALARM / FAULT: red
        h, s, _v = _hsv(t[kind][0])
        assert (h <= 0.03 or h >= 0.97) and s > 0.6, (theme, kind)
    assert t["FAULT"][0] != t["ALARM"][0]                 # FAULT darker than ALARM
    assert QColor(t["FAULT"][0]).lightness() < QColor(t["ALARM"][0]).lightness()
    # STOP is the only red control: its fill is red, no run control but STOP is filled
    from mlaser.ui import style
    h, s, _v = _hsv(T.THEMES[theme]["danger_fill"])
    assert (h <= 0.03 or h >= 0.97) and s > 0.6
    assert [k for k, rk in style.RUN_KINDS.items() if rk.role == "danger_fill"] == ["stop"]


def test_every_state_has_an_svg_icon():
    from mlaser.ui import icons
    from mlaser.ui.state_display import LASER_STATES, MACHINE_STATES
    for s in MACHINE_STATES + LASER_STATES:
        assert icons.has_icon(icons.STATE_ICONS[s]), s


# ------------------------------------------------------------------------- W2 vocabulary
def _st(banner="DISARMED", armed=False, dry_run=False, simulator=False, state="ready"):
    return SimpleNamespace(banner=banner, armed=armed, dry_run=dry_run, simulator=simulator,
                           state=SimpleNamespace(value=state), fault="f")


def _ms(connected=True, mode="ready", homed=True, alarms=()):
    return SimpleNamespace(connected=connected, mode=mode, homed=homed, alarms=list(alarms))


def test_machine_and_laser_axes():
    from mlaser.ui.state_display import laser_state, machine_state
    ctl = SimpleNamespace(breakpoint=None)
    assert machine_state(_st(), None) == "offline"
    assert machine_state(_st(), _ms(connected=False), (), ctl) == "offline"
    assert machine_state(_st(), _ms(homed=False), (), ctl, True) == "not_homed"
    assert machine_state(_st(), _ms(homed=True), (), ctl, False) == "ready"
    assert machine_state(_st(), _ms(mode="process"), (), ctl) == "running"
    assert machine_state(_st(), _ms(mode="pause"), (), ctl) == "hold"
    assert machine_state(_st(), _ms(), (), SimpleNamespace(breakpoint=object()), False) == "hold"
    assert machine_state(_st(), _ms(), ["a"], ctl) == "alarm"
    assert machine_state(_st(), _ms(), [SimpleNamespace(kind="estop")], ctl) == "estop"
    assert machine_state(_st(banner="FAULT"), _ms(), ["a"], ctl) == "fault"
    assert laser_state(_st(armed=True)) == "armed"
    assert laser_state(_st(dry_run=True)) == "dry_run"
    assert laser_state(_st()) == "disarmed"


def test_one_vocabulary_on_every_display(win, app):
    """The run bar's pill, the Run screen's state block and the banner say the same words, and
    each display names its axis."""
    from mlaser.ui import state_display as SD
    su = win.safety_ui
    win.ctl.connect()
    win.commands.run("workspace.run")
    pump(app)
    su.refresh()
    scr = win.extension("run").screen
    scr.refresh()
    words = {SD.machine_label(win, s) for s in SD.MACHINE_STATES}
    assert su.run_bar.pill.text() in words and scr.state.text() == su.run_bar.pill.text()
    assert su.run_bar.pill.accessibleName().startswith(SD.axis_label(win, "machine"))
    assert su.banner.badge.text() == SD.laser_label(win, "disarmed")
    assert su.banner.badge.accessibleName().startswith(SD.axis_label(win, "laser"))
    assert win.ctl.arm()
    su.refresh()
    assert su.banner.badge.text() == SD.laser_label(win, "armed") and su.banner.badge.kind == "ARMED"


def test_laser_source_chip_and_last_known_values(win, app):
    win.ctl.connect()
    pump(app)
    win._poll()
    assert win.st_lf.text().startswith(win.tr_.tr("status.dev.laser_source"))
    assert "last known" not in win.st_xy.text()
    win.ctl.disconnect()
    pump(app)
    win._poll()
    assert "last known" in win.st_xy.text() and win.st_xy.property("stale")
    assert "last known" in win.st_lf.text()


# ------------------------------------------------------------------------- O2 banner
def test_offline_banner_connects_and_keeps_its_height(win, app, monkeypatch):
    su = win.safety_ui
    su.refresh()
    b = su.banner
    assert b.action == "connect" and b.btn.isVisibleTo(b) and b.badge.state == "offline"
    h = b.height()
    opened = []
    monkeypatch.setattr(su, "open_connection", lambda: opened.append(True))
    b.btn.click()
    assert opened == [True]
    win.ctl.connect()
    su.refresh()
    pump(app)
    assert b.action == "arm" and b.height() == h
    assert win.ctl.arm()
    su.refresh()
    pump(app)
    assert b.action == "disarm" and b.height() == h
    win.ctl.raise_fault("test")
    su.refresh()
    pump(app)
    # FAULT: no button of its own (the alarm banner carries the one Reset alarm), same height
    assert b.action == "" and not b.btn.isVisibleTo(b) and b.height() == h
    assert su.alarm_bar.isVisible() and su.alarm.reset.isVisibleTo(su.alarm)


def test_connect_opens_machine_connection(win, app):
    win.safety_ui.open_connection()
    pump(app)
    assert win.shell.host.current == "machine"


# ------------------------------------------------------------------------- W15 interlocks
def test_interlock_chip_in_every_workspace(win, app, monkeypatch):
    tb = win.shell.topbar
    sio = SimpleNamespace(notices=lambda: [1, 2, 3], notice_text=lambda: "")
    monkeypatch.setattr(win, "safety_io", sio, raising=False)
    for ws in ("design", "run", "machine"):
        win.commands.run(f"workspace.{ws}")
        win.shell._poll()
        assert tb.interlocks.isVisibleTo(tb) and "3" in tb.interlocks.text(), ws
        assert tb.interlocks.accessibleName()
    opened = []
    from mlaser.ui import safety_io
    monkeypatch.setattr(safety_io, "open_dialog", lambda w, *a: opened.append(w))
    tb.interlocks.click()
    assert opened == [win]
    monkeypatch.setattr(win, "safety_io", SimpleNamespace(notices=lambda: [], notice_text=lambda: ""), raising=False)
    win.shell._poll()
    assert not tb.interlocks.isVisibleTo(tb)


# ------------------------------------------------------------------------- W13 status bar
def test_status_chips_are_permanent(win, app):
    sb = win.statusBar()
    win.ctl.connect()
    win._poll()
    pump(app)
    shown = [w for w in (win.st_mc, win.st_lf, win.st_xy) if w.isVisible()]
    assert win.st_mc in shown and win.st_lf in shown
    sb.showMessage("A long transient message " * 6)
    pump(app)
    for w in shown:
        assert w.isVisible(), w.objectName()             # never hidden by a message
    sb.clearMessage()


# ------------------------------------------------------------------------- W8 dock
def test_dock_controls_disabled_with_reasons(win, app):
    p = win.control
    su = win.safety_ui
    su.refresh()
    for k in ("jog_X+", "mp29_1", "mp30", "mf514", "mp39", "out_gas", "mf21"):
        b = p.buttons[k]
        assert not b.isEnabled() and b.property("disabled_reason") == win.tr_.tr("panel.why.offline"), k
    win.ctl.connect()
    su.refresh()
    for k in ("jog_X+", "mp29_1", "out_gas", "mf21"):
        assert p.buttons[k].isEnabled(), k
    win.ctl.raise_fault("test")
    su.refresh()
    for k in ("jog_X+", "mp29_1", "mf514", "out_gas"):
        b = p.buttons[k]
        assert not b.isEnabled() and b.property("disabled_reason") == win.tr_.tr("run.why.fault"), k
    assert p.buttons["mf21"].isEnabled()
    assert su.run_bar.buttons["stop"].isEnabled()         # STOP: always


# ------------------------------------------------------------------------- W9 fire laser
def test_fire_laser_needs_a_hold(win, app):
    p = win.control
    fired = []
    b = p.buttons["out_laser"]
    b._fire = fired.append
    assert b.text() == win.tr_.tr("panel.fire_laser")
    assert b.parentWidget() is not p.buttons["out_gas"].parentWidget()    # its own row
    win.ctl.connect()
    assert win.ctl.arm()
    win.safety_ui.refresh()
    assert b.isEnabled()
    b.setDown(True)
    b.pressed.emit()
    pump(app, 0.2)
    b.setDown(False)
    b.released.emit()
    assert fired == []                                     # a short press does nothing
    b.setDown(True)
    b.pressed.emit()
    pump(app, 0.75)
    assert fired == [True] and b.fired
    b.setDown(False)
    b.released.emit()
    assert fired == [True, False]
    # disarmed while held: the beam goes off, the button is disabled with its reason
    b.setDown(True)
    b.pressed.emit()
    pump(app, 0.75)
    win.safety_ui.disarm()
    assert fired[-1] is False and not b.isEnabled()
    b.setDown(False)


# ------------------------------------------------------------------------- W10 clock
def test_progress_is_monotonic_across_hold_and_continue():
    c = JobClock()
    c.started(False, 100.0)
    assert c.whole(0.4) == pytest.approx(0.4)
    c.started(True, 130.0)                                 # Continue: a resumed job from 0
    assert c.whole(0.0) == pytest.approx(0.4)
    assert c.whole(0.5) == pytest.approx(0.7)
    assert c.whole(0.1) == pytest.approx(0.7)              # never backwards
    assert c.elapsed(160.0) == pytest.approx(60.0)         # from the job's start
    c.started(False, 200.0)                                # a new job
    assert c.whole(0.1) == pytest.approx(0.1) and c.elapsed(201.0) == pytest.approx(1.0)
    ms = SimpleNamespace(connected=True, mode="process", progress=0.5, x=0, y=0, z=0, speed=0, job_index=-1)
    st = SimpleNamespace(armed=True, dry_run=False, simulator=True, banner="ARMED")
    r = run_status(ms, st, [], None, 0.0, None, clock=c, now=210.0)
    assert r.progress == pytest.approx(0.5) and r.elapsed_s == pytest.approx(10.0)


# ------------------------------------------------------------------------- W4 / W18 run bar
def test_run_bar_on_the_run_workspace_and_the_start_target(win, app, tmp_path):
    su = win.safety_ui
    rb = su.run_bar
    win.commands.run("workspace.design")
    su.refresh()
    assert rb.buttons["start"].isVisible() and rb.buttons["stop"].isVisible()
    assert rb.progress.format() == win.tr_.format("run.start_target", name=su.start_target_name())
    win.commands.run("workspace.run")
    su.refresh()
    pump(app)
    assert not rb.buttons["start"].isVisible() and not rb.buttons["stop"].isVisible()
    assert rb.pill.isVisible() and rb.progress.isVisible()
    scr = win.extension("run").screen
    order = [scr.buttons[k] for k in ("start", "hold", "stop", "frame", "dry_run", "arm")]
    ys = [(b.mapTo(scr, b.rect().topLeft()).y(), b.mapTo(scr, b.rect().topLeft()).x()) for b in order]
    assert ys == sorted(ys)                                # the run bar's order
    # a queue waiting: Start's menu names both choices; Start itself still runs the drawing
    ws = win.extension("run")
    p = tmp_path / "q.dxf"
    p.write_text("0\nEOF\n")
    ws.queue.add(str(p), 1)
    rb._start_menu_show()
    assert rb.start_drawing_act.isVisible() and rb.start_queue_act.isVisible()
    assert "1" in rb.start_queue_act.text() and su.start_target_name() in rb.start_drawing_act.text()


# ------------------------------------------------------------------------- W3 readiness strip
def test_run_screen_readiness_strip(win, app, monkeypatch):
    from mlaser.services.readiness import ReadinessItem
    su = win.safety_ui
    items = [ReadinessItem("homed", False, "Homed", "X and Y are not homed", "machine.home"),
             ReadinessItem("connected", False, "Connected", "Not connected", "hw.reconnect")]
    monkeypatch.setattr(su, "readiness", lambda: items)
    ran = []
    monkeypatch.setattr(su, "run_readiness_action", ran.append)
    win.commands.run("workspace.run")
    scr = win.extension("run").screen
    scr._ready_tick = 0
    scr.refresh()
    assert scr.ready_box.isVisibleTo(scr)
    assert set(scr.ready_buttons) == {"homed"}             # the connection is the banner's
    scr.ready_buttons["homed"].click()
    assert ran == ["machine.home"]
    items[:] = [ReadinessItem("homed", True, "Homed")]
    scr._ready_tick = 0
    scr.refresh()
    assert not scr.ready_box.isVisibleTo(scr)


# ------------------------------------------------------------------------- O8 live view
def test_live_view_colours_and_legend(app):
    from mlaser.ui import style
    from mlaser.ui.workspaces.run import RunCanvas
    c = RunCanvas()
    assert c.current_color() == style.color("accent") and c.current_color() != style.color("danger")
    assert c.done_color() == style.color("muted")
    assert len(c.legend_items()) == 3
    c.resize(400, 300)
    c.grab()                                               # paints (with the legend) without error
