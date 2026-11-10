"""Save diagnostic bundle… (``diag.bundle``) and Check for updates (``app.check_updates``).

:class:`BundleDialog` shows what the bundle will hold (:func:`mlaser.diag.bundle.contents`) and
an "Include the drawing" box, off by default, before anything is written; Save… asks for the
file and writes it in a worker (:class:`mlaser.ui.qtbridge.BackgroundCall`): the configuration
and the plan summary are copied on the GUI thread (milliseconds), the logs are read and the zip
is written off it.  No card command, no network: the file stays on this computer.

:func:`check_updates` runs :func:`mlaser.services.update_check.check` in a worker and shows a
notice with the release page link when a newer release exists.  ``quiet``: the opt-in check at
start (nothing is shown unless there is a newer release, and nothing while a job runs).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QFileDialog, QLabel, QMessageBox, QWidget

from mlaser.diag import bundle as B
from mlaser.services import diagnostics as D

from . import style as S
from .qtbridge import BackgroundCall
from .style import Dialog

log = logging.getLogger("mlaser.ui.diag_bundle")


def _tr(window: Any, key: str, default: str, **values: Any) -> str:
    tr = getattr(window, "tr_", None)
    text = None
    if tr is not None:
        try:
            if tr.has(key):
                text = tr.format(key, **values) if values else tr.tr(key)
        except Exception:  # noqa: BLE001
            text = None
    if text is None:
        text = default.format(**values) if values else default
    return text


def screens_info() -> list[dict]:
    """Geometry and device pixel ratio of every screen (the bundle's system section)."""
    out = []
    for s in QGuiApplication.screens():
        g, a = s.geometry(), s.availableGeometry()
        out.append({"name": s.name(), "geometry": [g.x(), g.y(), g.width(), g.height()],
                    "available": [a.x(), a.y(), a.width(), a.height()], "dpr": s.devicePixelRatio(),
                    "logical_dpi": round(s.logicalDotsPerInch(), 1), "refresh_hz": round(s.refreshRate(), 1),
                    "primary": s is QGuiApplication.primaryScreen()})
    return out


def _document(window: Any) -> Any:
    docs = getattr(window, "docs", None)
    return getattr(docs, "document", None) if docs is not None else getattr(window, "document", None)


class BundleDialog(Dialog):
    """The list of what is included, the drawing box (off), Save… / Cancel."""

    def __init__(self, window: Any, parent: Optional[QWidget] = None) -> None:
        self.w = window
        super().__init__(parent if parent is not None else (window if isinstance(window, QWidget) else None),
                         _tr(window, "diag.bundle.title", "Save diagnostic bundle"),
                         explanation=_tr(window, "diag.bundle.explanation",
                                         "One zip file for a bug report. Nothing is sent: the file stays on this "
                                         "computer and you decide whether to send it. No card command is used."),
                         buttons=QDialogButtonBox.Cancel)
        self.setObjectName("diag_bundle_dialog")
        self.list = QLabel()
        self.list.setObjectName("diag_bundle_contents")
        self.list.setWordWrap(True)
        self.list.setTextFormat(Qt.PlainText)
        self.list.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.addWidget(self.list)
        self.include_drawing = QCheckBox(_tr(window, "diag.bundle.include_drawing", "Include the drawing"))
        self.include_drawing.setObjectName("diag_bundle_include_drawing")
        self.include_drawing.setChecked(False)
        doc = _document(window)
        has_doc = bool(getattr(doc, "parts", None))
        S.set_enabled(self.include_drawing, has_doc,
                      "" if has_doc else _tr(window, "diag.bundle.no_drawing", "The drawing is empty"))
        self.include_drawing.toggled.connect(lambda _on: self._fill())
        self.body.addWidget(self.include_drawing)
        self.status = S.caption("")
        self.status.setObjectName("diag_bundle_status")
        self.body.addWidget(self.status)
        self.save_btn = self.buttons.addButton(_tr(window, "diag.bundle.save", "Save…"), QDialogButtonBox.AcceptRole)
        self.save_btn.setObjectName("diag_bundle_save")
        self.save_btn.setDefault(True)
        try:
            self.buttons.accepted.disconnect(self.accept)
        except (RuntimeError, TypeError):
            pass
        self.buttons.accepted.connect(self.save)
        self._call = BackgroundCall(self, "mlaser-diag-bundle")
        self.saved_path: Optional[str] = None
        self.manifest: Optional[dict] = None
        self.error: Optional[BaseException] = None
        self._fill()

    def items(self) -> list[B.Item]:
        src = B.Sources(drawing=b"x" if self.include_drawing.isChecked() else None)
        return B.contents(src, self.include_drawing.isChecked())

    def _fill(self) -> None:
        lines = [_tr(self.w, "diag.bundle.contents", "The bundle will contain:")]
        for it in self.items():
            lines.append(_tr(self.w, "diag.bundle.item_line", "• {what}", what=_tr(self.w, it.key, it.what)))
        if not self.include_drawing.isChecked():
            lines.append(_tr(self.w, "diag.bundle.no_geometry", "• No drawing geometry (tick the box to include it)"))
        self.list.setText("\n".join(lines))

    # ------------------------------------------------------------------ saving
    def save(self) -> None:
        start = os.path.join(os.path.expanduser("~"), B.default_name())
        path, _f = QFileDialog.getSaveFileName(self, _tr(self.w, "diag.bundle.title", "Save diagnostic bundle"),
                                               start, _tr(self.w, "diag.bundle.filter", "Zip archives (*.zip)"))
        if path:
            if not path.lower().endswith(".zip"):
                path += ".zip"
            self.save_to(path)

    def save_to(self, path: str, on_done: Optional[Callable[[Optional[str]], None]] = None) -> bool:
        """Collect on this thread (copies), write in the worker; ``on_done(path or None)``."""
        if self._call.busy:
            return False
        include = self.include_drawing.isChecked()
        ctl = getattr(self.w, "ctl", None)
        doc = _document(self.w)
        src = D.snapshot(getattr(self.w, "config", None), ctl, doc, screens=screens_info(),
                         drawing_name=os.path.basename(str(getattr(getattr(self.w, "docs", None), "path", "") or ""))
                         or "drawing.mlz")
        snap = None
        if include and doc is not None:
            snap = _snapshot(self.w)
        S.set_enabled(self.save_btn, False)
        self.status.setText(_tr(self.w, "diag.bundle.writing", "Writing the bundle…"))

        def work() -> dict:
            if snap is not None:
                from mlaser.project.format import dumps_project
                src.drawing = dumps_project(snap, embed_fonts=False)
            return D.write(path, src, include_drawing=include and snap is not None)

        def done(res: object, err: Optional[BaseException]) -> None:
            S.set_enabled(self.save_btn, True)
            if err is not None:
                self.error = err
                log.warning("diagnostic bundle not written: %s", err)
                self.status.setText(_tr(self.w, "diag.bundle.failed", "Not written: {error}", error=err))
                if on_done:
                    on_done(None)
                return
            self.saved_path, self.manifest = path, res if isinstance(res, dict) else None
            log.info("diagnostic bundle written: %s", path)
            msg = _tr(self.w, "diag.bundle.saved", "Diagnostic bundle saved: {path}", path=path)
            sb = getattr(self.w, "statusBar", None)
            if callable(sb):
                try:
                    sb().showMessage(msg, 10000)
                except Exception:  # noqa: BLE001
                    pass
            self.status.setText(msg)
            if on_done:
                on_done(path)
            else:
                self.accept()
        return self._call.start(work, done)

    def wait(self, timeout: float = 60.0) -> bool:
        return self._call.wait(timeout)


def _snapshot(window: Any) -> Any:
    """A copy of the document taken on the GUI thread (save_worker's snapshot, sliced)."""
    docs = getattr(window, "docs", None)
    if docs is None or not hasattr(docs, "plan_snapshot"):
        import copy
        return copy.deepcopy(_document(window))
    from .save_worker import snapshot_now
    return snapshot_now(docs, window if isinstance(window, QWidget) else None)


def open_bundle_dialog(window: Any, parent: Optional[QWidget] = None) -> BundleDialog:
    """The ``diag.bundle`` command: the dialog, modal (a save in progress finishes inside it)."""
    d = BundleDialog(window, parent)
    d.exec()
    return d


def bundle_button(window: Any, box: QDialogButtonBox, parent: Optional[QWidget] = None) -> Any:
    """Add "Save diagnostic bundle…" to a dialog's button box (the fault and log dialogs)."""
    b = box.addButton(_tr(window, "cmd.diag.bundle", "Save diagnostic bundle…"), QDialogButtonBox.ActionRole)
    b.setObjectName("diag_bundle_button")
    b.clicked.connect(lambda _=False: BundleDialog(window, parent).exec())
    return b


# =========================================================================== update notice
def check_updates(window: Any, quiet: bool = False, fetch: Optional[Callable[[], Any]] = None,
                  on_result: Optional[Callable[[Any], None]] = None) -> BackgroundCall:
    """Ask GitHub once in a worker (:mod:`mlaser.services.update_check`); show the result."""
    from mlaser.services import update_check as U
    call = BackgroundCall(window if isinstance(window, QWidget) else None, "mlaser-update-check")
    sb = getattr(window, "statusBar", None)
    if not quiet and callable(sb):      # the operator asked: say at once that it is under way
        try:
            sb().showMessage(_tr(window, "update.checking", "Checking for updates…"), 10000)
        except Exception:  # noqa: BLE001
            pass

    def done(res: Any, err: Optional[BaseException]) -> None:
        if err is not None or res is None:
            res = U.UpdateResult("", error=str(err))
        if on_result is not None:
            on_result(res)
        _show_update(window, res, quiet)
    call.start(lambda: U.check(fetch=fetch), done)
    return call


def _job_running(window: Any) -> bool:
    try:
        return bool(window.ctl.job_active())
    except Exception:  # noqa: BLE001
        return False


def _show_update(window: Any, res: Any, quiet: bool) -> None:
    sb = getattr(window, "statusBar", None)

    def status(text: str) -> None:
        if callable(sb):
            try:
                sb().showMessage(text, 10000)
            except Exception:  # noqa: BLE001
                pass
    if not res.ok:
        if not quiet:            # the operator asked: say it did not work; the reason is in the log
            status(_tr(window, "update.failed", "Could not check for updates (see the log)."))
        return
    if not res.newer:
        if not quiet:
            status(_tr(window, "update.latest", "mlaser {version} is the newest release.", version=res.current))
        return
    text = _tr(window, "update.newer", "mlaser {latest} is available (you have {current}). Nothing is "
               "downloaded or installed automatically.", latest=res.latest, current=res.current)
    status(text)
    if quiet and _job_running(window):
        return                   # never a dialog over a running job: the status line and log tell
    if not isinstance(window, QWidget):
        return
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle(_tr(window, "update.title", "Update available"))
    box.setText(text)
    box.setObjectName("update_notice")
    page = box.addButton(_tr(window, "update.open_page", "Open release page"), QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Close)
    box.setAttribute(Qt.WA_DeleteOnClose)
    box.buttonClicked.connect(lambda b: QDesktopServices.openUrl(QUrl(res.url)) if b is page else None)
    box.open()


__all__ = ["BundleDialog", "open_bundle_dialog", "bundle_button", "check_updates", "screens_info"]
