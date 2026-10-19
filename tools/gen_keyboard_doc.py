"""Write docs/user/keyboard.md from the key tables and the command registry (the same rows as
Help > Keyboard shortcuts in the application).

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/gen_keyboard_doc.py [--check]
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
for _var in ("MLASER_CONFIG_DIR", "MLASER_APP_DIR", "MLASER_DATA_DIR"):
    os.environ.setdefault(_var, tempfile.mkdtemp(prefix="mlaser-keys-"))
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "docs", "user", "keyboard.md")


def generate() -> str:
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow
    from mlaser.ui.shell import keymap
    w = MainWindow(DemoController(), extensions=EXTENSIONS)
    w._test_no_dialogs = True
    try:
        return keymap.markdown(w.shell.shortcut_rows())
    finally:
        w.close()


if __name__ == "__main__":
    text = generate()
    if "--check" in sys.argv:
        cur = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        if cur != text:
            print(f"{OUT} is out of date: run tools/gen_keyboard_doc.py")
            sys.exit(1)
        print("docs/user/keyboard.md is up to date")
    else:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {OUT}")
