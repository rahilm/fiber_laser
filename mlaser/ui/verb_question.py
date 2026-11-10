"""A question with verb buttons whose safe answer is the default (UX review 2026-11).

The questions that start motion ("Home the machine", "Go to origin"), apply machine parameters
or delete a record ask with two named buttons ("Home now" / "Not now", "Clear history" /
"Cancel"), the safe one the default and the Escape answer: one stray Enter never moves the
axes, changes the machine or deletes anything.  There is never a "don't ask again" here.

A destructive action gets :attr:`QMessageBox.DestructiveRole` (the platform places and marks it
as such: macOS sets it apart, Windows and Linux in their own order); the text says what is lost.
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtWidgets import QMessageBox, QWidget


def verb_question(parent: Optional[QWidget], title: str, text: str, yes_text: str, no_text: str,
                  informative: str = "", warning: bool = False, destructive: bool = False,
                  detail: str = "") -> bool:
    """Ask; True only when the operator chose ``yes_text``.  ``no_text`` is the default and the
    Escape button.  ``detail``: a longer list behind the platform's "Show details" button."""
    box = QMessageBox(parent)
    try:
        box.setIcon(QMessageBox.Warning if warning or destructive else QMessageBox.Question)
        box.setWindowTitle(title)
        box.setText(text)
        if informative:
            box.setInformativeText(informative)
        if detail:
            box.setDetailedText(detail)
        yes = box.addButton(yes_text, QMessageBox.DestructiveRole if destructive else QMessageBox.AcceptRole)
        no = box.addButton(no_text, QMessageBox.RejectRole)
        yes.setObjectName("verb_yes")
        no.setObjectName("verb_no")
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        return box.clickedButton() is yes
    finally:
        box.deleteLater()


def answer_with(box: Any, yes: bool) -> int:
    """Tests: click the verb (``yes``) or the safe button of a :func:`verb_question` box from a
    patched ``QMessageBox.exec``."""
    from PySide6.QtWidgets import QPushButton
    b = box.findChild(QPushButton, "verb_yes" if yes else "verb_no")
    if b is not None:
        b.click()
    return 0


__all__ = ["verb_question", "answer_with"]
