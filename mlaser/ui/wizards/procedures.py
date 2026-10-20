"""The concrete wizards: edge seek, follower calibration, focus homing, laser test and the
commissioning checklist (each a :class:`~.base.WizardDialog` over a Qt-free procedure)."""
from __future__ import annotations

import copy
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from mlaser.controller import edgeseek as E
from mlaser.services.procedures import commissioning as CM
from mlaser.services.procedures import edge_seek as ES
from mlaser.services.procedures import engine as PE
from mlaser.services.procedures import simrig
from mlaser.services.procedures.follower_cal import FollowerCalibrationProcedure, curve_quality
from mlaser.services.procedures.focus_home import AF_TYPES, FocusHomingProcedure
from mlaser.services.procedures.laser_test import LaserTestParams, LaserTestProcedure

from .. import icons
from .base import WizardDialog
from .common import MIN_TARGET, JogPad, OutlineView, T


def _spin(lo: float, hi: float, val: float, unit: str = "", dec: int = 3) -> QDoubleSpinBox:
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setValue(float(val))
    if unit:
        w.setSuffix(f" {unit}")
    w.setMinimumHeight(32)
    return w


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    return t


# =========================================================================== edge seek
DEMO_PARAMS = dict(start=(130.0, 80.0), board_x=400.0, board_y=300.0, current_start=False,
                   move_speed=400.0, dock_type=7, timeout_s=20.0)


class EdgeSeekWizard(WizardDialog):
    """``ext``: the ext_machine MachineExtension (ops, apply, log)."""

    FIELDS = (("start_x", "wizard.edge.f_start_x", "mm"), ("start_y", "wizard.edge.f_start_y", "mm"),
              ("board_x", "wizard.edge.f_board_x", "mm"), ("board_y", "wizard.edge.f_board_y", "mm"),
              ("x_percent", "wizard.edge.f_x_percent", ""), ("y_percent", "wizard.edge.f_y_percent", ""),
              ("offset_x", "wizard.edge.f_offset_x", "mm"), ("offset_y", "wizard.edge.f_offset_y", "mm"),
              ("out_speed", "wizard.edge.f_out_speed", "mm/s"),
              ("follow_height", "wizard.edge.f_follow_height", "mm"))

    def __init__(self, window: Any, ext: Any, mode: str = "cap4", parent: Optional[QWidget] = None) -> None:
        self.ext = ext
        self.mode = mode
        self.sim_kind = {"cap3": "sheet", "cap4": "sheet", "disc": "disc"}.get(mode, "card")
        super().__init__(window, self._factory, parent)

    def _factory(self, machine: Any, dry: bool) -> ES.EdgeSeekProcedure:
        ops = self.ext.ops
        params = copy.copy(ops.edge)
        if dry:
            for k, v in DEMO_PARAMS.items():
                setattr(params, k, v)
        p = ES.EdgeSeekProcedure(machine, self.mode, params=params, values=ops.values,
                                 apply=lambda r: self.call_on_gui(lambda: self.ext.apply_edge_result(r)),
                                 set_origin=ops.set_origin, dry_run=dry, log_fn=self.ext.log)
        if dry and self.mode == "disc":
            p.disc_guess = (310.0, 190.0)
        return p

    def page(self, step: PE.Step) -> QWidget:
        p: ES.EdgeSeekProcedure = self.proc         # type: ignore[assignment]
        if step.id == "params":
            w = QWidget()
            f = QFormLayout(w)
            self.cur = QCheckBox(T(self.w, "wizard.edge.f_current"))
            self.cur.setChecked(p.params.current_start)
            f.addRow(self.cur)
            self.ed: dict[str, QDoubleSpinBox] = {}
            for attr, key, unit in self.FIELDS:
                if self.mode == "disc" and attr not in ("out_speed", "follow_height"):
                    continue
                v = p.params.start[0] if attr == "start_x" else p.params.start[1] if attr == "start_y" \
                    else getattr(p.params, attr)
                w2 = _spin(-100000, 100000, v, unit)
                f.addRow(T(self.w, key), w2)
                self.ed[attr] = w2
            return w
        if step.id == "review":
            w = QWidget()
            lay = QVBoxLayout(w)
            self.view = OutlineView(self.w)
            lay.addWidget(self.view, 1)
            self.set_origin_cb = QCheckBox(T(self.w, "wizard.edge.set_origin"))
            self.set_origin_cb.setChecked(p.set_origin_after)
            self.set_origin_cb.toggled.connect(lambda on: setattr(self.proc, "set_origin_after", bool(on)))
            lay.addWidget(self.set_origin_cb)
            return w
        if step.id in ("p1", "p2"):
            w = QWidget()
            lay = QVBoxLayout(w)
            self.jog = JogPad(self.w, self.machine, z=False)
            lay.addWidget(self.jog)
            lbl = QLabel()
            lbl.setObjectName("pointLabel")
            lay.addWidget(lbl)
            lay.addStretch(1)
            return w
        return super().page(step)

    def refresh_page(self, step: PE.Step, page: QWidget) -> None:
        p: ES.EdgeSeekProcedure = self.proc         # type: ignore[assignment]
        if step.id == "review":
            data = p.outline()
            self.view.set_data(data)
            canvas = getattr(self.w, "canvas", None)
            if canvas is not None and data.get("outline"):
                marks = [q for q in (data.get("corner"), data.get("origin")) if q] + list(data.get("points", []))
                canvas.set_preview(data["outline"], marks, color=icons.AMBER)
        elif step.id in ("p1", "p2"):
            n = 1 if step.id == "p1" else 2
            lbl = page.findChild(QLabel, "pointLabel")
            q = p.points.get(n)
            if lbl is not None:
                lbl.setText(T(self.w, "wizard.edge.point_value", n=n, x=f"{q[0]:.3f}", y=f"{q[1]:.3f}")
                            if q else "")
        elif step.id == "apply" and p.applied:
            self.status.setText(T(self.w, "wizard.edge.applied", angle=f"{p.result.angle_deg:.3f}"))

    def collect(self, step: PE.Step) -> bool:
        if step.id != "params":
            return True
        prm = self.proc.params                       # type: ignore[attr-defined]
        prm.current_start = self.cur.isChecked()
        for attr, w in self.ed.items():
            if attr == "start_x":
                prm.start = (w.value(), prm.start[1])
            elif attr == "start_y":
                prm.start = (prm.start[0], w.value())
            else:
                setattr(prm, attr, w.value())
        if not self.proc.dry_run:                    # remembered like the original's dialog
            self.ext.ops.edge = copy.copy(prm)
        return True

    def status_text(self) -> str:
        p: ES.EdgeSeekProcedure = self.proc         # type: ignore[assignment]
        if p.step is not None and p.step.id == "review" and p.result is not None:
            txt = T(self.w, "wizard.edge.result", angle=f"{p.result.angle_deg:.3f}")
            if p.dry_run:
                txt += "  " + T(self.w, "wizard.edge.dry_not_applied")
            return txt
        return super().status_text()

    def on_close(self) -> None:
        canvas = getattr(self.w, "canvas", None)
        if canvas is not None:
            canvas.clear_preview()


# =========================================================================== follower
class FollowerWizard(WizardDialog):
    sim_kind = "follower"

    def __init__(self, window: Any, save_path: Optional[str] = None, parent: Optional[QWidget] = None) -> None:
        self.save_path = save_path
        super().__init__(window, lambda m, dry: FollowerCalibrationProcedure(
            m, save_path=self.save_path, dry_run=dry, log_fn=getattr(window, "log", None)), parent)

    def page(self, step: PE.Step) -> QWidget:
        if step.id == "review":
            from ..ext_devices import CurvePlot
            w = QWidget()
            lay = QVBoxLayout(w)
            self.plot = CurvePlot()
            self.plot.setAccessibleName(T(self.w, "wizard.follower.plot_name"))
            lay.addWidget(self.plot, 1)
            self.quality = QLabel()
            self.quality.setWordWrap(True)
            lay.addWidget(self.quality)
            return w
        return super().page(step)

    def refresh_page(self, step: PE.Step, page: QWidget) -> None:
        p: FollowerCalibrationProcedure = self.proc   # type: ignore[assignment]
        if step.id == "review" and p.table is not None:
            self.plot.set_points(p.table.points)
            q = curve_quality(p.table)
            self.quality.setText(T(self.w, "wizard.follower.quality", grade=p.grade or "-", points=q["points"],
                                   span=q["span"], mono=T(self.w, "wizard.yes" if q["monotonic"] else "wizard.no")))

    def status_text(self) -> str:
        p: FollowerCalibrationProcedure = self.proc   # type: ignore[assignment]
        if p.saved is not None and p.state == PE.DONE:
            return T(self.w, "wizard.follower.saved", path=str(p.saved))
        return super().status_text()


# =========================================================================== focus
class FocusWizard(WizardDialog):
    sim_kind = "card"

    def __init__(self, window: Any, ext: Any, parent: Optional[QWidget] = None) -> None:
        self.ext = ext
        super().__init__(window, lambda m, dry: FocusHomingProcedure(
            m, values=ext.values, home_fn=ext.ops.home_af, dry_run=dry, log_fn=ext.log), parent)

    def status_text(self) -> str:
        r = self.proc.results.get("check")
        if isinstance(r, dict) and self.proc.step is not None and self.proc.step.id == "home":
            return T(self.w, "wizard.focus.type_is", type=T(self.w, AF_TYPES.get(r["type"], "")))
        return super().status_text()


# =========================================================================== laser test
class LaserWizard(WizardDialog):
    sim_kind = "card"

    def __init__(self, window: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(window, lambda m, dry: LaserTestProcedure(
            m, LaserTestParams(), dry_run=dry, log_fn=getattr(window, "log", None)), parent)

    def page(self, step: PE.Step) -> QWidget:
        p: LaserTestProcedure = self.proc            # type: ignore[assignment]
        if step.id == "params":
            w = QWidget()
            f = QFormLayout(w)
            self.levels = QLineEdit(", ".join(f"{v:g}" for v in p.params.levels))
            self.levels.setMinimumHeight(32)
            f.addRow(T(self.w, "wizard.laser.f_levels"), self.levels)
            self.duration = _spin(1, 2000, p.params.duration_ms, "ms", 0)
            f.addRow(T(self.w, "wizard.laser.f_duration"), self.duration)
            self.pause = _spin(0.1, 30, p.params.pause_s, "s", 1)
            f.addRow(T(self.w, "wizard.laser.f_pause"), self.pause)
            return w
        if step.id == "results":
            w = QWidget()
            lay = QVBoxLayout(w)
            self.results = _table([T(self.w, "wizard.laser.c_duty"), T(self.w, "wizard.laser.c_duration"),
                                   T(self.w, "wizard.laser.c_fired"), T(self.w, "wizard.laser.c_measured")])
            self.results.setEditTriggers(QAbstractItemView.AllEditTriggers)
            lay.addWidget(self.results)
            return w
        return super().page(step)

    def refresh_page(self, step: PE.Step, page: QWidget) -> None:
        p: LaserTestProcedure = self.proc            # type: ignore[assignment]
        if step.id == "results" and self.results.rowCount() != len(p.pulses):
            self.results.setRowCount(len(p.pulses))
            for i, r in enumerate(p.pulses):
                for j, v in enumerate((f"{r.duty_pct:g} %", f"{r.duration_ms:g} ms",
                                       "✓" if r.fired else "✗")):
                    it = QTableWidgetItem(v)
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    self.results.setItem(i, j, it)
                self.results.setItem(i, 3, QTableWidgetItem(""))

    def collect(self, step: PE.Step) -> bool:
        p: LaserTestProcedure = self.proc            # type: ignore[assignment]
        if step.id == "params":
            try:
                lv = [float(x) for x in self.levels.text().replace(";", ",").split(",") if x.strip()]
                prm = LaserTestParams(lv, self.duration.value(), self.pause.value())
                prm.validate()
            except ValueError:
                self.status.setText("⚠ " + T(self.w, "wizard.laser.error.levels_text"))
                return False
            except PE.StepFailed as e:
                self.status.setText("⚠ " + T(self.w, e.key, **e.values))
                return False
            p.params = prm
        elif step.id == "results":
            for i, r in enumerate(p.pulses):
                it = self.results.item(i, 3)
                try:
                    r.measured_w = float(it.text().replace("W", "").strip()) if it and it.text().strip() else None
                except ValueError:
                    r.measured_w = None
        return True

    def confirm_run(self, step: PE.Step) -> bool:
        if step.id == "arm":
            return self._ask("wizard.laser.arm_confirm", "wizard.laser.arm_title")
        if step.id == "fire":
            return self._ask("wizard.laser.fire_confirm", "wizard.laser.fire_title")
        return True


# =========================================================================== commissioning
STATUS_TEXT = {CM.PASS: ("✓", "wizard.comm.s_pass", icons.GREEN), CM.WARN: ("!", "wizard.comm.s_warn", icons.AMBER),
               CM.FAIL: ("✗", "wizard.comm.s_fail", icons.RED), CM.SKIP: ("–", "wizard.comm.s_skip", None),
               CM.PENDING: ("…", "wizard.comm.s_pending", None)}


class CommissioningWizard(WizardDialog):
    def __init__(self, window: Any, ext: Any, parent: Optional[QWidget] = None) -> None:
        self.ext = ext
        super().__init__(window, lambda m, dry: CM.CommissioningProcedure(
            m, config=getattr(ext.ctx, "config", None), ops=ext.ops if not dry else None, dry_run=dry,
            log_fn=ext.log), parent)
        self.checklist = _table([T(self.w, "wizard.comm.c_item"), T(self.w, "wizard.comm.c_status"),
                                 T(self.w, "wizard.comm.c_detail")])
        self.right.insertWidget(4, self.checklist, 1)
        self.right.setStretchFactor(self.stack, 0)
        self.resize(980, 760)
        self.refresh()

    def make_rig(self) -> Any:
        rig = simrig.simulator_controller("card", heartbeat_output=7)
        self._rig_interlocks(rig)
        return rig

    def _rig_interlocks(self, rig: Any) -> None:
        """Dry run: the private simulator gets the configured interlock inputs (the wiring test
        then toggles its inputs); its heartbeat stays on output 7."""
        cfg = getattr(self.ext.ctx, "config", None)
        if rig is None or cfg is None:
            return
        from mlaser.services import safety_io
        # through the audited API: the interlock configuration is read-only on the controller
        if not rig.configure_safety(interlocks=safety_io.from_config(cfg).interlocks,
                                    source="operator", reason="commissioning dry run"):
            raise RuntimeError("the dry-run simulator refused the interlock configuration")

    def configure_safety_io(self) -> None:
        """The Safety I/O dialog from the checklist, then the interlock check again."""
        from ..safety_io import SafetyIODialog
        d = SafetyIODialog(self.w, self)
        if d.exec() or getattr(self, "_test_accept_safety_io", False):
            self._rig_interlocks(self.rig)
            p: CM.CommissioningProcedure = self.proc     # type: ignore[assignment]
            p.recheck_interlocks()
        self.refresh()

    def page(self, step: PE.Step) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        if step.id in ("interlocks", "interlock_test"):
            b = QPushButton(T(self.w, "wizard.comm.fix_safety_io"))
            b.setObjectName("configure_safety_io")
            b.setMinimumHeight(MIN_TARGET)
            b.clicked.connect(lambda: self.configure_safety_io())
            lay.addWidget(b)
        elif step.id == "soft_limits":
            b = QPushButton(T(self.w, "wizard.comm.fix_limits"))
            b.setObjectName("fix")
            b.setMinimumHeight(MIN_TARGET)
            b.clicked.connect(lambda: (self.proc.fix_soft_limits(), self.refresh()))
            lay.addWidget(b)
        elif step.id == "heartbeat_relay":
            b = QPushButton(T(self.w, "wizard.comm.hold_heartbeat"))
            b.setCheckable(True)
            b.setMinimumHeight(MIN_TARGET)
            b.toggled.connect(lambda on: self.proc.heartbeat_hold(on))
            lay.addWidget(b)
        elif step.id in ("jog_x_dir", "jog_y_dir"):
            axis = "X" if step.id == "jog_x_dir" else "Y"
            b = QPushButton(T(self.w, "wizard.comm.fix_reverse_axis", axis=axis))
            b.setObjectName("fix")
            b.setMinimumHeight(MIN_TARGET)
            b.clicked.connect(lambda _=False, a=axis: (self.proc.fix_axis_direction(a), self.refresh()))
            lay.addWidget(b)
        lay.addStretch(1)
        return w

    def refresh_page(self, step: PE.Step, page: QWidget) -> None:
        p: CM.CommissioningProcedure = self.proc     # type: ignore[assignment]
        fix = page.findChild(QPushButton, "fix")
        if fix is not None:
            key = "soft_limits" if step.id == "soft_limits" else f"jog_{step.id[4]}"
            it = p.items.get(key)
            fix.setEnabled(bool(it and it.fix) and not p.dry_run)

    def refresh(self) -> None:
        super().refresh()
        t = getattr(self, "checklist", None)
        if t is None:
            return
        p: CM.CommissioningProcedure = self.proc     # type: ignore[assignment]
        t.setRowCount(len(p.items))
        for i, it in enumerate(p.items.values()):
            sym, key, color = STATUS_TEXT[it.status]
            cells = (T(self.w, CM.TITLES[it.id]), f"{sym} {T(self.w, key)}",
                     T(self.w, it.detail, **{k: v for k, v in it.values.items() if not isinstance(v, dict)})
                     if it.detail else "")
            for j, v in enumerate(cells):
                cell = QTableWidgetItem(v)
                if j == 1 and color:
                    from PySide6.QtGui import QColor
                    cell.setForeground(QColor(color))
                t.setItem(i, j, cell)

    def status_text(self) -> str:
        p: CM.CommissioningProcedure = self.proc     # type: ignore[assignment]
        if p.state in (PE.DONE, PE.ABORTED) and p.report_path is not None:
            s = p.summary()["counts"]
            return T(self.w, "wizard.comm.report", path=str(p.report_path), passed=s[CM.PASS], warned=s[CM.WARN],
                     failed=s[CM.FAIL])
        return super().status_text()


__all__ = ["EdgeSeekWizard", "FollowerWizard", "FocusWizard", "LaserWizard", "CommissioningWizard"]
