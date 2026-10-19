"""Shell dialogs: user-mode PIN prompt and PIN setup, the keyboard shortcut sheet, the
first-run welcome sheet."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtCore import QRegularExpression
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from mlaser.services import usermodes as um

#: the user documentation folder of a source checkout (docs/user); a packaged build shows the
#: path it looked for
DOCS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs", "user"))


def ask_pin(parent, tr, target: str) -> Optional[str]:
    """The PIN for switching to ``target`` (None when cancelled)."""
    title = tr.tr("shell.pin.title")
    label = tr.format("shell.pin.prompt", mode=tr.tr(f"shell.mode.{target}"))
    text, ok = QInputDialog.getText(parent, title, label, QLineEdit.Password)
    return text if ok and text is not None else None


class PinSetupDialog(QDialog):
    """Service mode: set or clear the Engineer and Service PINs (4-12 digits)."""

    def __init__(self, modes: um.UserModeService, tr, parent=None):
        super().__init__(parent)
        self.modes = modes
        self.tr_ = tr
        self.setWindowTitle(tr.tr("shell.pin.setup_title"))
        v = QVBoxLayout(self)
        info = QLabel(tr.tr("shell.pin.setup_info"))
        info.setWordWrap(True)
        v.addWidget(info)
        form = QFormLayout()
        self.edits: dict[str, QLineEdit] = {}
        self.clear: dict[str, QCheckBox] = {}
        val = QRegularExpressionValidator(QRegularExpression(r"\d{0,12}"))
        for m in (um.ENGINEER, um.SERVICE):
            row = QHBoxLayout()
            e = QLineEdit()
            e.setEchoMode(QLineEdit.Password)
            e.setValidator(val)
            e.setAccessibleName(tr.format("shell.pin.for", mode=tr.tr(f"shell.mode.{m}")))
            e.setPlaceholderText(tr.tr("shell.pin.set" if modes.has_pin(m) else "shell.pin.none"))
            row.addWidget(e, 1)
            c = QCheckBox(tr.tr("shell.pin.clear"))
            c.setEnabled(modes.has_pin(m))
            row.addWidget(c)
            form.addRow(tr.format("shell.pin.for", mode=tr.tr(f"shell.mode.{m}")), row)
            self.edits[m], self.clear[m] = e, c
        v.addLayout(form)
        self.error = QLabel()
        self.error.setWordWrap(True)
        v.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _ok(self) -> None:
        try:
            for m in (um.ENGINEER, um.SERVICE):
                pin = self.edits[m].text()
                if self.clear[m].isChecked():
                    self.modes.set_pin(m, "")
                elif pin:
                    self.modes.set_pin(m, pin)
        except (ValueError, PermissionError) as exc:
            self.error.setText(f"⚠ {exc}")
            return
        self.accept()


class ShortcutSheet(QDialog):
    """Help > Keyboard shortcuts: generated from the command registry and the key tables."""

    def __init__(self, rows: list[tuple[str, str, str, str]], tr, parent=None):
        super().__init__(parent)
        self.setObjectName("shell_shortcuts")
        self.setWindowTitle(tr.tr("shell.shortcuts.title"))
        v = QVBoxLayout(self)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr.tr("shell.shortcuts.filter"))
        self.filter.setAccessibleName(tr.tr("shell.shortcuts.filter"))
        v.addWidget(self.filter)
        heads = [tr.tr("shell.shortcuts.workspace"), tr.tr("shell.shortcuts.command"),
                 tr.tr("shell.shortcuts.key"), tr.tr("shell.shortcuts.where")]
        self.table = QTableWidget(len(rows), 4)
        self.table.setHorizontalHeaderLabels(heads)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAccessibleName(tr.tr("shell.shortcuts.title"))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(val))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        v.addWidget(self.table, 1)
        self.filter.textChanged.connect(self._filter)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.close)
        v.addWidget(bb)
        self.resize(720, 560)

    def _filter(self, text: str) -> None:
        t = text.lower().strip()
        for r in range(self.table.rowCount()):
            hit = not t or any(t in (self.table.item(r, c).text().lower()) for c in range(4))
            self.table.setRowHidden(r, not hit)


class WelcomeSheet(QDialog):
    """First run: the simulator, importing the original's parameters and languages, the safety
    model (disarmed by default) and where the user docs are."""

    def __init__(self, shell, parent=None):
        super().__init__(parent)
        self.shell = shell
        tr = shell.tr
        self.setObjectName("shell_welcome")
        self.setWindowTitle(tr.tr("shell.welcome.title"))
        v = QVBoxLayout(self)
        head = QLabel(f"<h2>{tr.tr('shell.welcome.heading')}</h2><p>{tr.tr('shell.welcome.intro')}</p>")
        head.setWordWrap(True)
        v.addWidget(head)
        self.buttons: dict[str, QPushButton] = {}

        def section(title_key: str, text_key: str, actions: list[tuple[str, str]]) -> None:
            box = QGroupBox(tr.tr(title_key))
            bl = QVBoxLayout(box)
            lab = QLabel(tr.tr(text_key))
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.RichText)
            bl.addWidget(lab)
            row = QHBoxLayout()
            for key, cmd in actions:
                b = QPushButton(tr.tr(key))
                b.setMinimumHeight(36)
                b.clicked.connect(lambda _=False, c=cmd: self._run(c))
                row.addWidget(b)
                self.buttons[cmd] = b
            row.addStretch(1)
            bl.addLayout(row)
            v.addWidget(box)

        section("shell.welcome.sim_title", "shell.welcome.sim_text", [("shell.welcome.sim_button", "hw.connect_sim")])
        section("shell.welcome.import_title", "shell.welcome.import_text",
                [("shell.welcome.import_params", "shell.import_params"),
                 ("shell.welcome.import_lang", "app.import_language")])
        section("shell.welcome.safety_title", "shell.welcome.safety_text", [])
        section("shell.welcome.docs_title", "shell.welcome.docs_text",
                [("shell.welcome.docs_button", "shell.docs"), ("shell.welcome.keys_button", "shell.shortcuts")])
        self.again = QCheckBox(tr.tr("shell.welcome.again"))
        v.addWidget(self.again)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.close)
        bb.button(QDialogButtonBox.Close).setText(tr.tr("shell.welcome.start"))
        v.addWidget(bb)
        self.resize(640, 640)

    def _run(self, cmd: str) -> None:
        self.shell.w.run(cmd)

    def done(self, r: int) -> None:  # noqa: D401 - QDialog API
        self.shell.config.set("shell.welcome_done", not self.again.isChecked())
        super().done(r)

    def closeEvent(self, ev) -> None:
        self.shell.config.set("shell.welcome_done", not self.again.isChecked())
        super().closeEvent(ev)


def open_docs(window, tr) -> bool:
    """docs/user/shell.md in the offline help browser (``app.help``, ext_diag)."""
    diag = window.extension("ext_diag") if hasattr(window, "extension") else None
    if diag is not None and hasattr(diag, "help"):
        browser = diag.help()
        if browser is not None and hasattr(browser, "open"):
            try:
                browser.open("user/shell.md")
            except Exception:  # noqa: BLE001 - the help browser shows its home page instead
                pass
            return True
    QMessageBox.information(window, tr.tr("shell.docs"), tr.format("shell.docs.missing", path=DOCS_DIR))
    return False


__all__ = ["ask_pin", "PinSetupDialog", "ShortcutSheet", "WelcomeSheet", "open_docs", "DOCS_DIR"]
