"""Shared pytest setup: headless Qt, and an orderly Qt teardown.

PySide6 can segfault at interpreter exit when Python-owned widgets outlive the QApplication
(seen intermittently in tests that open dialogs).  Deleting every top-level widget and
flushing deferred deletes while the application still exists avoids that.
"""
import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_sessionfinish(session, exitstatus):
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        w.close()
        w.deleteLater()
    app.processEvents()
    gc.collect()
    app.processEvents()
