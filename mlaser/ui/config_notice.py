"""The start-up notice for a configuration that could not be loaded (architecture review A3).

:class:`mlaser.services.config.ConfigService` tells "no ``config.json``" (a fresh install) from
"a ``config.json`` that cannot be used" (unreadable, damaged, wrong shape, a newer format).  In
the second case mlaser runs on the default settings - default step scale, no safety inputs, no
heartbeat - so until the operator has answered this notice:

* arming is refused (:func:`apply_hold`: an arm hold on the controller, re-applied when the
  window swaps its controller), and
* ``config.json`` is not written (the service refuses, :class:`~mlaser.services.config.ConfigWriteHeld`).

The notice is blocking and offers, as verbs: restore the last settings that loaded cleanly
(``config.json.bak``; the default button when there is one), continue with the defaults, or quit
(the escape button, and the default when there is no backup).  It only adds refusals; no
interlock or check is relaxed.
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox, QLabel

from . import style

HOLD_KEY = "config"

RESTORE, DEFAULTS, QUIT = "restore", "defaults", "quit"


def _tr(win: Any, key: str, **kw: Any) -> str:
    tr = getattr(win, "tr_", None)
    if tr is None:
        from .i18n import translator
        tr = translator()
    return tr.format(key, **kw) if kw else tr.tr(key)


def apply_hold(win: Any) -> None:
    """Refuse arming on the window's controller while the configuration's load error is not
    answered; lift that refusal once it is."""
    cfg = getattr(win, "config", None)
    ctl = getattr(win, "ctl", None)
    fn = getattr(ctl, "set_arm_hold", None)
    if cfg is None or fn is None:
        return
    blocked = bool(getattr(cfg, "load_blocked", False))
    fn(HOLD_KEY, _tr(win, "config.notice.arm_hold") if blocked else None)


def _breakable(path: Any) -> str:
    """A path the label can wrap at its separators (a long path never widens the dialog)."""
    s = str(path)
    for sep in ("/", "\\", "-", "_", "."):
        s = s.replace(sep, sep + "​")
    return s


class ConfigNoticeDialog(style.Dialog):
    """The blocking notice (see the module docstring); :attr:`choice` after ``exec()``."""

    def __init__(self, win: Any) -> None:
        cfg = win.config
        kind = cfg.load_error_kind if cfg.load_error_kind in ("damaged", "unreadable", "newer") else "damaged"
        super().__init__(win, _tr(win, "config.notice.title"),
                         heading_text=_tr(win, f"config.notice.heading.{kind}"),
                         explanation=_tr(win, "config.notice.explanation"),
                         buttons=QDialogButtonBox.NoButton)
        self.setObjectName("config_notice")
        self.choice = QUIT
        lines = [_tr(win, "config.notice.file", path=_breakable(cfg.path)),
                 _tr(win, "config.notice.problem", error=cfg.load_error)]
        if cfg.load_error_evidence is not None:
            lines.append(_tr(win, "config.notice.copy", path=_breakable(cfg.load_error_evidence)))
        self.has_backup = bool(cfg.backup_usable())
        lines.append(_tr(win, "config.notice.backup", path=_breakable(cfg.backup_path)) if self.has_backup
                     else _tr(win, "config.notice.no_backup"))
        self.details = QLabel("\n".join(lines))
        self.details.setObjectName("config_notice_details")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.addWidget(self.details)
        bb = self.buttons
        self.b_restore: Optional[Any] = None
        if self.has_backup:
            self.b_restore = bb.addButton(_tr(win, "config.notice.restore"), QDialogButtonBox.AcceptRole)
            self.b_restore.clicked.connect(lambda: self._done(RESTORE))
        self.b_defaults = bb.addButton(_tr(win, "config.notice.defaults"), QDialogButtonBox.DestructiveRole)
        self.b_defaults.clicked.connect(lambda: self._done(DEFAULTS))
        self.b_quit = bb.addButton(_tr(win, "config.notice.quit"), QDialogButtonBox.RejectRole)
        self.b_quit.clicked.connect(lambda: self._done(QUIT))
        # the safe default: the last good settings, else quitting (never "continue with the
        # defaults" by Enter); Escape / closing the window = quit
        default = self.b_restore or self.b_quit
        for b in (self.b_restore, self.b_defaults, self.b_quit):
            if b is not None:
                b.setAutoDefault(b is default)
                b.setDefault(b is default)
        default.setFocus()
        self.setWindowModality(Qt.ApplicationModal)

    def _done(self, choice: str) -> None:
        self.choice = choice
        if choice == QUIT:
            self.reject()
        else:
            self.accept()

    def reject(self) -> None:          # Escape, the window's close button
        self.choice = QUIT if self.choice not in (RESTORE, DEFAULTS) else self.choice
        super().reject()


def show_if_needed(win: Any, run=None) -> bool:
    """Show the notice when the configuration could not be loaded; act on the answer.  False:
    the operator chose to quit.  ``run(dialog) -> choice`` replaces ``exec()`` (tests)."""
    cfg = getattr(win, "config", None)
    if cfg is None or not getattr(cfg, "load_blocked", False):
        apply_hold(win)
        return True
    log = getattr(getattr(win, "log_panel", None), "log", None)
    if log is not None:
        log(_tr(win, "config.notice.log", error=cfg.load_error))
    while True:
        dlg = ConfigNoticeDialog(win)
        try:
            if run is not None:
                choice = run(dlg)
            else:
                dlg.exec()
                choice = dlg.choice
        finally:
            dlg.deleteLater()
        if choice == RESTORE:
            if cfg.restore_backup():
                if log is not None:
                    log(_tr(win, "config.notice.log_restored", path=str(cfg.backup_path)))
                break
            continue                       # the backup became unusable: ask again (no restore offer)
        if choice == DEFAULTS:
            cfg.acknowledge_load_error()
            if log is not None:
                log(_tr(win, "config.notice.log_defaults"))
            break
        apply_hold(win)
        return False
    apply_hold(win)
    return True


__all__ = ["ConfigNoticeDialog", "apply_hold", "show_if_needed", "HOLD_KEY"]
