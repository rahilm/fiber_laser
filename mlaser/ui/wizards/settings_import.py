"""Guided "Import from my Mlaser installation" (mlaser.services.settings_import).

1. choose the install folder (and the parameter folder, ``%LOCALAPPDATA%\\NexCut`` or a copy);
2. Preview: every change grouped by area (parameters, layer table, IP addresses, jump settle
   time, follower calibration, languages) with the current and the new value; areas can be
   unticked;
3. Apply: the chosen areas.  The original installation is only read, never written.
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from mlaser.services import settings_import as SI

from .. import icons, style
from .common import T

AREA_KEYS = {"params": "wizard.import.area_params", "layers": "wizard.import.area_layers",
             "ip": "wizard.import.area_ip", "jump": "wizard.import.area_jump",
             "calib": "wizard.import.area_calib", "lang": "wizard.import.area_lang"}


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _value(v: Any, unit: str, key: str = "") -> str:
    """A parameter value with its unit: converted to the display units when the setting is a
    number in a unit that converts (mm, mm/s, mm/s² ...; the stored value stays as imported), else
    as stored."""
    from mlaser.services import units
    from mlaser.services.config import registry_settings
    from mlaser.ui.dialogs import setting_kind
    d = registry_settings().get(key)
    k = setting_kind(d) if d is not None else None
    if k is not None and isinstance(v, (int, float)) and not isinstance(v, bool):
        return units.current().fmt(float(v), k, trim=True, decimals=max(units.current().decimals(k), 4))
    if v is None:
        return _fmt(v)
    return _fmt(v) + (f" {unit}" if unit else "")


class SettingsImportDialog(QDialog):
    def __init__(self, window: Any, config: Any, parent: Optional[QWidget] = None,
                 install: str = "", param_dir: str = "") -> None:
        super().__init__(parent if parent is not None else (window if isinstance(window, QWidget) else None))
        self.w = window
        self.config = config
        self.plan: Optional[SI.ImportPlan] = None
        self.result: Optional[SI.ImportResult] = None
        self.setWindowTitle(T(window, "wizard.import.title"))
        self.resize(920, 640)
        lay = QVBoxLayout(self)
        intro = QLabel(T(window, "wizard.import.intro"))
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)
        f = QFormLayout()
        self.install = QLineEdit(install)
        self.params = QLineEdit(param_dir)
        self.params.setPlaceholderText(T(window, "wizard.import.params_placeholder"))
        for edit, key, pick in ((self.install, "wizard.import.install", self._pick_install),
                                (self.params, "wizard.import.param_dir", self._pick_params)):
            row = QHBoxLayout()
            edit.setMinimumHeight(32)
            row.addWidget(edit, 1)
            b = style.button(T(window, "wizard.import.browse"), size="touch")
            b.clicked.connect(pick)
            row.addWidget(b)
            f.addRow(T(window, key), row)
        nex = SI.localappdata_nexcut()
        if nex is not None:
            b = style.button(T(window, "wizard.import.use_localappdata", path=str(nex)), size="touch")
            b.clicked.connect(lambda: self.params.setText(str(nex)))
            f.addRow("", b)
        lay.addLayout(f)
        # the list of changes is the one part that scrolls (its rows); the form around it does not
        self.tree = style.table_look(QTreeWidget())
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels([T(window, "wizard.import.c_setting"), T(window, "wizard.import.c_label"),
                                   T(window, "wizard.import.c_current"), T(window, "wizard.import.c_new"),
                                   T(window, "wizard.import.c_safety")])
        self.tree.setAccessibleName(T(window, "wizard.import.preview"))
        lay.addWidget(self.tree, 1)
        self.status = style.IconLabel()               # a warning: after its icon, never a glyph
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        bb = style.size_buttons(QDialogButtonBox(), "touch")
        self.b_compare = bb.addButton(T(window, "wizard.import.compare"), QDialogButtonBox.ActionRole)
        self.b_compare.setToolTip(T(window, "wizard.import.compare_tip"))
        self.b_preview = bb.addButton(T(window, "wizard.import.preview"), QDialogButtonBox.ActionRole)
        self.b_reconnect = bb.addButton(T(window, "wizard.import.reconnect"), QDialogButtonBox.ActionRole)
        self.b_reconnect.setVisible(False)
        self.b_apply = bb.addButton(T(window, "wizard.import.apply"), QDialogButtonBox.ActionRole)
        bb.addButton(QDialogButtonBox.Close)
        self.b_compare.clicked.connect(lambda: self.compare())
        self.b_preview.clicked.connect(self.preview)
        self.b_reconnect.clicked.connect(self.reconnect)
        self.b_apply.clicked.connect(lambda: self.apply())
        bb.rejected.connect(self.reject)
        self.b_apply.setEnabled(False)
        lay.addWidget(bb)
        if param_dir or install:
            self.preview()

    def _pick_install(self) -> None:
        d = QFileDialog.getExistingDirectory(self, T(self.w, "wizard.import.install"), self.install.text())
        if d:
            self.install.setText(d)

    def _pick_params(self) -> None:
        d = QFileDialog.getExistingDirectory(self, T(self.w, "wizard.import.param_dir"), self.params.text())
        if d:
            self.params.setText(d)

    # ------------------------------------------------------------------ preview / apply
    def preview(self) -> Optional[SI.ImportPlan]:
        inst = self.install.text().strip() or None
        pd = self.params.text().strip() or None
        if inst is None and pd is None:
            style.set_status(self.status, style.warning_text(T(self.w, "wizard.import.choose")))
            return None
        return self._fill(SI.preview(inst, self.config, pd))

    def compare(self, path: str = "") -> Optional[SI.ImportPlan]:
        """Compare the configuration with a backup (made before every import and Apply) or
        another mlaser configuration file; Apply then restores it."""
        if not path:
            backups = SI.list_backups(self.config)
            start = str(backups[0]) if backups else str(getattr(self.config, "path", "") or "")
            path, _ = QFileDialog.getOpenFileName(self, T(self.w, "wizard.import.compare"), start,
                                                  T(self.w, "wizard.import.compare_filter"))
        if not path:
            return None
        return self._fill(SI.preview_backup(path, self.config))

    def _fill(self, plan: SI.ImportPlan) -> SI.ImportPlan:
        """Show what ``plan`` changes: per area, every value current -> new; the values that
        make motion safe marked (a word in the Check column and bold, never colour alone)."""
        self.plan = plan
        self.result = None
        self.b_reconnect.setVisible(False)
        self.tree.clear()
        self.b_apply.setText(T(self.w, "wizard.import.restore" if plan.kind == "backup" else "wizard.import.apply"))
        if plan.errors:
            style.set_status(self.status, style.warning_text("; ".join(plan.errors)))
            self.b_apply.setEnabled(False)
            return plan
        by = plan.by_area()
        avail = plan.available()
        areas = ("params",) if plan.kind == "backup" else SI.AREAS
        for area in areas:
            changes = by.get(area, [])
            n = len(changes)
            if area == "layers":
                n = plan.layers
            elif area == "lang":
                n = len(plan.languages)
            nsafe = len([c for c in changes if SI.safety_category(c.key)]) if area == "params" else 0
            top = QTreeWidgetItem([T(self.w, AREA_KEYS[area]), T(self.w, "wizard.import.count", n=n), "", "",
                                   T(self.w, "wizard.import.safety_count", n=nsafe) if nsafe else ""])
            top.setData(0, Qt.UserRole, area)
            if nsafe:
                top.setIcon(4, icons.icon("alert-triangle", "warning"))
            top.setFlags(top.flags() | Qt.ItemIsUserCheckable)
            top.setCheckState(0, Qt.Checked if area in avail else Qt.Unchecked)
            if area not in avail:
                top.setDisabled(True)
            self.tree.addTopLevelItem(top)
            # the safety-relevant values first: they are the ones to read
            for c in sorted(changes, key=lambda c: not SI.safety_category(c.key)):
                label = self._label(c)
                cat = SI.safety_category(c.key) if area == "params" else ""
                it = QTreeWidgetItem([c.key, label, _value(c.old, c.unit, c.key), _value(c.new, c.unit, c.key),
                                      T(self.w, f"wizard.import.safety_{cat}") if cat else ""])
                if cat:
                    it.setIcon(4, icons.icon("alert-triangle", "warning"))
                    it.setData(0, Qt.UserRole, "safety")
                    f = it.font(0)
                    f.setBold(True)
                    for col in range(5):
                        it.setFont(col, f)
                        it.setToolTip(col, T(self.w, "wizard.import.safety_tip"))
                top.addChild(it)
            if area == "lang":
                for code, name, cnt in plan.languages:
                    top.addChild(QTreeWidgetItem([code, name, "", T(self.w, "wizard.import.texts", n=cnt)]))
            if area == "layers" and plan.layers:
                top.addChild(QTreeWidgetItem(["LayerPara.xml", T(self.w, "wizard.import.layers_note"), "",  # i18n: ok (file name)
                                              T(self.w, "wizard.import.count", n=plan.layers)]))
            top.setExpanded(area != "params" or n <= 20 or nsafe > 0)
        for i in range(5):
            self.tree.resizeColumnToContents(i)
        if plan.kind == "backup":
            head = T(self.w, "wizard.import.comparing", path=str(plan.files.get("config", "")))
            if not plan.changes:
                head += "\n" + T(self.w, "wizard.import.no_difference")
            style.set_status(self.status, head)
        else:
            warn = [T(self.w, f"wizard.import.warn_{w}") if w == "no_params" else w for w in plan.warnings]
            src = [f"{k}: {v}" for k, v in plan.files.items()]
            found = T(self.w, "wizard.import.found", n=len(plan.files)) + "  " + "; ".join(src)
            style.set_status(self.status, style.warning_text(found + "\n" + "; ".join(warn)) if warn else found)
        self.b_apply.setEnabled(bool(avail))
        return plan

    def _label(self, c: SI.Change) -> str:
        if not c.label:
            return ""
        tr = getattr(self.w, "tr_", None)
        if tr is not None and c.area == "params":
            g, n = tr.tr_param(c.label)
            return f"{g} · {n}" if g else n
        return T(self.w, c.label)

    def chosen_areas(self) -> list[str]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.checkState(0) == Qt.Checked and not it.isDisabled():
                out.append(it.data(0, Qt.UserRole))
        return out

    def confirm(self, areas: list[str]) -> bool:
        """"Apply changes" / "Cancel" (Cancel the default): how many values change, how many of
        them change how the machine moves (listed under "Show details"), that the configuration
        is backed up first and whether the card needs a reconnect."""
        from ..verb_question import verb_question
        plan = self.plan
        assert plan is not None
        by = plan.by_area()
        extra = {"layers": plan.layers, "lang": len(plan.languages), "calib": 1}
        n = sum(extra[a] if a in extra else len(by.get(a, [])) for a in areas)
        safe = plan.safety_changes(areas)
        info = [T(self.w, "wizard.import.confirm_safety", n=len(safe))] if safe else []
        info.append(T(self.w, "wizard.import.confirm_backup"))
        if SI.needs_reconnect(plan, areas):
            info.append(T(self.w, "wizard.import.confirm_reconnect"))
        detail = "\n".join(f"{c.key}  {self._label(c)}: {_value(c.old, c.unit, c.key)} → {_value(c.new, c.unit, c.key)}"
                           for c in safe)
        restore = plan.kind == "backup"
        return verb_question(self, T(self.w, "wizard.import.confirm_title"),
                             T(self.w, "wizard.import.confirm_restore" if restore else "wizard.import.confirm_text", n=n),
                             T(self.w, "wizard.import.restore" if restore else "wizard.import.apply"),
                             T(self.w, "wizard.import.cancel"), informative="\n\n".join(info),
                             warning=bool(safe), detail=detail)

    def apply(self, confirm: bool = True) -> Optional[SI.ImportResult]:
        if self.plan is None:
            return None
        areas = self.chosen_areas()
        if not areas:
            return None
        if confirm and not self.confirm(areas):
            style.set_status(self.status, T(self.w, "wizard.import.cancelled"))
            return None
        try:
            self.result = res = SI.apply(self.plan, self.config, areas)
        except SI.WriteBackError as e:
            style.set_status(self.status, style.warning_text(str(e)))
            return None
        txt = T(self.w, "wizard.import.done", areas=", ".join(f"{T(self.w, AREA_KEYS[a])} ({n})"
                                                                 for a, n in res.applied.items()) or "–")
        if res.config_backup is not None:
            txt += "\n" + T(self.w, "wizard.import.backup", path=str(res.config_backup))
        if res.errors:
            txt += "\n" + "; ".join(res.errors)
        if "lang" in res.applied:
            tr = getattr(self.w, "tr_", None)
            if tr is not None:
                tr.reload()
        if "params" in res.applied and self._connected():
            txt += "\n" + T(self.w, "wizard.import.reconnect_needed")
            self.b_reconnect.setVisible(True)
        style.set_status(self.status, style.warning_text(txt) if res.errors else txt)
        self.b_apply.setEnabled(False)
        return res

    def _connected(self) -> bool:
        ctl = getattr(self.w, "ctl", None)
        try:
            return bool(ctl is not None and ctl.state().connected)
        except Exception:  # noqa: BLE001 - no controller state: nothing to reconnect
            return False

    def reconnect(self) -> Any:
        """Reconnect the card now (``hw.reconnect``): the new parameters reach the card."""
        cmds = getattr(self.w, "commands", None)
        if cmds is None or cmds.get("hw.reconnect") is None:
            return None
        self.b_reconnect.setVisible(False)
        return cmds.run("hw.reconnect")


__all__ = ["SettingsImportDialog"]
