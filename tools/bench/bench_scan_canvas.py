"""Engraving-scan document (many 1-segment parts from raster G-code): canvas load/paint, toolpath, compile, at K parts."""
import sys, time, gc
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import numpy as np
from mlaser.engrave.raster import generate_gcode, RasterParams
from mlaser.io.gcode import parse_gcode
from mlaser.model import Document
K = int(sys.argv[1])
def T(label, fn, *a, **kw):
    t = time.perf_counter(); r = fn(*a, **kw); dt = time.perf_counter() - t
    print(f"{label:52s} {dt*1000:9.1f} ms", flush=True); return r
img = (np.indices((600, 800)).sum(0) % 255).astype(np.uint8)
full = parse_gcode(generate_gcode(img, RasterParams(mode="dither", width_mm=100, rho=10)))
doc = Document(parts=full.parts[:K], layers=full.layers)
print(f"{len(doc.parts)} parts")
from mlaser.cam.toolpath import build_toolpath, CamParams
items = T("build_toolpath sort 0", build_toolpath, doc, CamParams(sort_mode=0))
if K <= 20000:
    from mlaser.nc import compile_job
    from mlaser.nc.machine import Machine
    j = T("compile_job smooth=True", compile_job, doc, CamParams(sort_mode=0), Machine())
    print(f"   steps {j.steps}, replay {j.duration_s:.0f}s, pierces {j.pierces}")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter
app = QApplication([])
from mlaser.ui.mainwindow import MainWindow
w = MainWindow(); w.resize(1600, 1000); w.show(); app.processEvents()
T("MainWindow.set_document", w.set_document, doc)
print(f"   scene items {len(w.canvas.scene().items())}")
img2 = QImage(1600, 1000, QImage.Format_ARGB32)
def paint():
    p = QPainter(img2); w.canvas.render(p); p.end()
T("render to QImage", paint); T("render to QImage again", paint)
T("gc.collect() (full)", gc.collect)
import resource; print(f"maxrss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} MB")
