"""Shell dialogs: user-mode PIN prompt and PIN setup, the keyboard shortcut sheet, the
first-run welcome sheet."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtCore import QRegularExpression
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QSizePolicy, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from mlaser.services import usermodes as um

from .. import style
from ..responsive import flow, mark_list
from ..style import Dialog

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


class WelcomeSheet(Dialog):
    """First run: practice on the simulator, importing the original's parameters and languages,
    the safety model (disarmed by default) and where the user docs are.

    One dialog frame (style.Dialog), plain sections (a heading and wrapped text, no group-box
    frames), and the text reflows with the width: the sections sit in one vertical scroll area
    whose labels take the height they need (heightForWidth), so nothing is clipped at any width
    or font size; the sheet's size comes from its content, capped by the screen."""

    def __init__(self, shell, parent=None):
        tr = shell.tr
        super().__init__(parent, tr.tr("shell.welcome.title"), buttons=QDialogButtonBox.Close)
        self.shell = shell
        self.setObjectName("shell_welcome")
        head = style.heading(tr.tr("shell.welcome.heading"), "display")
        head.setObjectName("welcome_heading")
        intro = QLabel(tr.tr("shell.welcome.intro"))
        intro.setWordWrap(True)
        self.body.addWidget(head)
        self.body.addWidget(intro)
        self.actions: dict[str, QPushButton] = {}
        content = QWidget()
        content.setObjectName("welcome_sections")
        cv = style.vbox(content, sp=style.SP3)

        def section(title_key: str, text_keys: list[str], actions: list[tuple[str, str]]) -> None:
            sec, bl = style.section(tr.tr(title_key))
            for tk in text_keys:
                lab = QLabel(tr.tr(tk))
                lab.setWordWrap(True)
                lab.setTextFormat(Qt.RichText)
                lab.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
                bl.addWidget(lab)
            if actions:
                btns = []
                for key, cmd in actions:
                    b = QPushButton(tr.tr(key))
                    b.setMinimumHeight(style.TARGET)
                    b.clicked.connect(lambda _=False, c=cmd: self._run(c))
                    self.actions[cmd] = b
                    btns.append(b)
                bl.addWidget(flow(btns, justify=False))
            cv.addWidget(sec)

        # one practice section (the simulator and the guided lessons are the same idea)
        section("practice.welcome_title", ["shell.welcome.sim_text", "practice.welcome_text"],
                [("shell.welcome.sim_button", "hw.connect_sim"), ("practice.welcome_button", "practice.open")])
        section("shell.welcome.import_title", ["shell.welcome.import_text"],
                [("shell.welcome.import_params", "shell.import_params"),
                 ("shell.welcome.import_lang", "app.import_language")])
        section("shell.welcome.safety_title", ["shell.welcome.safety_text"], [])
        section("shell.welcome.docs_title", ["shell.welcome.docs_text"],
                [("shell.welcome.docs_button", "shell.docs"), ("shell.welcome.keys_button", "shell.shortcuts")])
        cv.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("welcome_scroll")
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(content)
        mark_list(self.scroll)                 # a page of text: it may scroll vertically, never sideways
        self.body.addWidget(self.scroll, 1)
        self.again = QCheckBox(tr.tr("shell.welcome.again"))
        self.body.addWidget(self.again)
        self.buttons.button(QDialogButtonBox.Close).setText(tr.tr("shell.welcome.get_started"))
        self.buttons.rejected.connect(self.close)
        self._content = content
        self.fit()

    def fit(self, width: int = 0) -> None:
        """Size from the content at ``width`` (default: about 46 average characters a line),
        capped by the available screen height."""
        fm = self.fontMetrics()
        w = width or max(fm.averageCharWidth() * 72, 420)
        lay = self._content.layout()
        inner = w - 2 * style.SP3 - self.scroll.verticalScrollBar().sizeHint().width()
        need = lay.totalHeightForWidth(inner) if lay.hasHeightForWidth() else self._content.sizeHint().height()
        chrome = self.sizeHint().height() - self.scroll.sizeHint().height()
        h = need + chrome
        scr = self.screen().availableGeometry() if self.screen() is not None else None
        if scr is not None:
            h = min(h, int(scr.height() * 0.9))
            w = min(w, scr.width())
        self.resize(w, h)

    def sections_clipped(self) -> list[str]:
        """Labels shorter than the height their text needs at their width (tests)."""
        out = []
        for lab in self._content.findChildren(QLabel):
            if lab.isVisible() and lab.wordWrap() and lab.height() < lab.heightForWidth(lab.width()):
                out.append(lab.text()[:40])
        return out

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
