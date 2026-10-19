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
                               QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from mlaser.services import settings_import as SI

from .common import MIN_TARGET, T

AREA_KEYS = {"params": "wizard.import.area_params", "layers": "wizard.import.area_layers",
             "ip": "wizard.import.area_ip", "jump": "wizard.import.area_jump",
             "calib": "wizard.import.area_calib", "lang": "wizard.import.area_lang"}


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


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
            b = QPushButton(T(window, "wizard.import.browse"))
            b.clicked.connect(pick)
            row.addWidget(b)
            f.addRow(T(window, key), row)
        nex = SI.localappdata_nexcut()
        if nex is not None:
            b = QPushButton(T(window, "wizard.import.use_localappdata", path=str(nex)))
            b.clicked.connect(lambda: self.params.setText(str(nex)))
            f.addRow("", b)
        lay.addLayout(f)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([T(window, "wizard.import.c_setting"), T(window, "wizard.import.c_label"),
                                   T(window, "wizard.import.c_current"), T(window, "wizard.import.c_new")])
        self.tree.setAccessibleName(T(window, "wizard.import.preview"))
        lay.addWidget(self.tree, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.status)
        bb = QDialogButtonBox()
        self.b_preview = bb.addButton(T(window, "wizard.import.preview"), QDialogButtonBox.ActionRole)
        self.b_apply = bb.addButton(T(window, "wizard.import.apply"), QDialogButtonBox.AcceptRole)
        bb.addButton(QDialogButtonBox.Close)
        for b in (self.b_preview, self.b_apply):
            b.setMinimumHeight(MIN_TARGET)
        self.b_preview.clicked.connect(self.preview)
        self.b_apply.clicked.connect(self.apply)
        bb.rejected.connect(self.reject)
        self.b_apply.setEnabled(False)
        lay.addWidget(bb)

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
            self.status.setText("⚠ " + T(self.w, "wizard.import.choose"))
            return None
        self.plan = plan = SI.preview(inst, self.config, pd)
        self.tree.clear()
        if plan.errors:
            self.status.setText("⚠ " + "; ".join(plan.errors))
            self.b_apply.setEnabled(False)
            return plan
        by = plan.by_area()
        avail = plan.available()
        for area in SI.AREAS:
            changes = by.get(area, [])
            n = len(changes)
            if area == "layers":
                n = plan.layers
            elif area == "lang":
                n = len(plan.languages)
            top = QTreeWidgetItem([T(self.w, AREA_KEYS[area]), T(self.w, "wizard.import.count", n=n), "", ""])
            top.setData(0, Qt.UserRole, area)
            top.setFlags(top.flags() | Qt.ItemIsUserCheckable)
            top.setCheckState(0, Qt.Checked if area in avail else Qt.Unchecked)
            if area not in avail:
                top.setDisabled(True)
            self.tree.addTopLevelItem(top)
            for c in changes:
                label = self._label(c)
                top.addChild(QTreeWidgetItem([c.key, label, _fmt(c.old) + (f" {c.unit}" if c.unit and c.old is not None else ""),
                                              _fmt(c.new) + (f" {c.unit}" if c.unit else "")]))
            if area == "lang":
                for code, name, cnt in plan.languages:
                    top.addChild(QTreeWidgetItem([code, name, "", T(self.w, "wizard.import.texts", n=cnt)]))
            if area == "layers" and plan.layers:
                top.addChild(QTreeWidgetItem(["LayerPara.xml", T(self.w, "wizard.import.layers_note"), "",
                                              T(self.w, "wizard.import.count", n=plan.layers)]))
            top.setExpanded(area != "params" or n <= 20)
        for i in range(4):
            self.tree.resizeColumnToContents(i)
        warn = [T(self.w, f"wizard.import.warn_{w}") if w == "no_params" else w for w in plan.warnings]
        src = [f"{k}: {v}" for k, v in plan.files.items()]
        self.status.setText(T(self.w, "wizard.import.found", n=len(plan.files)) + "  " + "; ".join(src)
                            + (("\n⚠ " + "; ".join(warn)) if warn else ""))
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

    def apply(self) -> Optional[SI.ImportResult]:
        if self.plan is None:
            return None
        try:
            self.result = res = SI.apply(self.plan, self.config, self.chosen_areas())
        except SI.WriteBackError as e:
            self.status.setText("⚠ " + str(e))
            return None
        txt = T(self.w, "wizard.import.done", areas=", ".join(f"{T(self.w, AREA_KEYS[a])} ({n})"
                                                                 for a, n in res.applied.items()) or "–")
        if res.config_backup is not None:
            txt += "\n" + T(self.w, "wizard.import.backup", path=str(res.config_backup))
        if res.errors:
            txt += "\n⚠ " + "; ".join(res.errors)
        if "lang" in res.applied:
            tr = getattr(self.w, "tr_", None)
            if tr is not None:
                tr.reload()
        self.status.setText(txt)
        self.b_apply.setEnabled(False)
        return res


__all__ = ["SettingsImportDialog"]
