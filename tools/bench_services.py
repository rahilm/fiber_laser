"""Measure undo cost and GUI-thread time per edit on a sample document.

    QT_QPA_PLATFORM=offscreen python tools/bench_services.py [file.chf]

Prints: one whole-document deepcopy (the pre-Wave-2 undo snapshot), one DocumentService edit
command (the scoped swap command with shared round-trip records), the retained undo bytes, and
the time the GUI thread spends in a ribbon edit (rotate 90) until it returns to the event loop
(planning runs in the worker), plus the worker's toolpath + compile time.  Works on the old tree
too (then it measures MainWindow._edit with its synchronous toolpath rebuild).
"""
from __future__ import annotations

import copy
import os
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def ms(fn, n=5):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1e3)
    return statistics.median(out)


def main(path: str) -> None:
    from mlaser.formats.chf import load_chf
    doc = load_chf(path)
    nseg = sum(len(c.segments) for p in doc.parts for c in p.contours)
    print(f"{os.path.basename(path)}: {len(doc.parts)} parts, {sum(len(p.contours) for p in doc.parts)} contours, {nseg} segments")
    print(f"whole-document deepcopy (old undo snapshot): {ms(lambda: copy.deepcopy(doc)):.1f} ms")
    try:
        from mlaser.services.document import DocumentService
        from mlaser import ops
        svc = DocumentService(copy.deepcopy(doc))
        print(f"DocumentService.edit (rotate 90, scope = targets): "
              f"{ms(lambda: svc.edit(lambda d: ops.rotate(d, 90), 'rot', parts=ops.targets(svc.document))):.1f} ms "
              f"(op included), undo {ms(svc.undo, 5):.2f} ms")
        svc2 = DocumentService(copy.deepcopy(doc))
        print(f"DocumentService.edit (no-op, whole document scope): {ms(lambda: svc2.edit(lambda d: None, 'noop')):.1f} ms, "
              f"retained per command ~{svc2.stack.cost / max(1, len(svc2.stack)) / 1024:.0f} KiB estimated")
    except ImportError:
        pass
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import MainWindow
    w = MainWindow(DemoController())
    w.set_document(load_chf(path))
    jobs = getattr(w, "jobs", None)
    if jobs is not None:
        jobs.settle(timeout=600)
    times = []
    for _ in range(3):
        t = time.perf_counter()
        w.run("xform.rot_cw90")
        times.append((time.perf_counter() - t) * 1e3)
        app.processEvents()
        if jobs is not None:
            t1 = time.perf_counter()
            jobs.settle(timeout=600)
            worker = (time.perf_counter() - t1) * 1e3
            print(f"  worker after the edit: {worker:.0f} ms (toolpath + compile, compile {jobs.stats['last_compile_s']*1e3:.0f} ms)")
    print(f"GUI thread per ribbon edit (rotate 90): {statistics.median(times):.1f} ms")
    w.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "tests", "data", "autosave.chf"))
