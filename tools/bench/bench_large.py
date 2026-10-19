"""Large synthetic document: N parts (rect with circle hole) - CAM, ordering per mode, compile, canvas, undo snapshot.
Usage: QT_QPA_PLATFORM=offscreen python -I bench_large.py N [compile 0/1]"""
import sys, time, copy, math
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from mlaser.model import Document, Part, Contour, Line, Arc, Craft, LeadParams
from mlaser.cam.toolpath import CamParams, build_toolpath
N = int(sys.argv[1]); do_compile = len(sys.argv) > 2 and sys.argv[2] == "1"
def rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)
def circ(cx, cy, r):
    s = (cx + r, cy); return Contour([Arc(s, s, (cx, cy), True)], True)
cols = int(math.ceil(math.sqrt(N * 2)))
parts = []
for k in range(N):
    x, y = (k % cols) * 30.0, (k // cols) * 30.0
    parts.append(Part([circ(x + 12, y + 12, 5), rect(x, y, 24, 24)], Craft(layer=1 + k % 4, lead_in=LeadParams(kind=1, length=2.0))))
doc = Document(parts=parts)
def T(label, fn, *a, **kw):
    t = time.perf_counter(); r = fn(*a, **kw); dt = time.perf_counter() - t
    print(f"{label:46s} {dt*1000:9.1f} ms"); return r
print(f"N={N} parts, {2*N} contours")
for mode in (0, 5, 1, 6, 20, 21):
    items = T(f"build_toolpath sort_mode={mode}", build_toolpath, doc, CamParams(sort_mode=mode))
items = T("build_toolpath sort_mode=5", build_toolpath, doc, CamParams(sort_mode=5))
T("deepcopy(doc) (undo snapshot)", copy.deepcopy, doc)
T("doc.bbox()", doc.bbox)
if do_compile:
    from mlaser.nc import compile_job
    from mlaser.nc.machine import Machine
    for sm in (False, True):
        job = T(f"compile_job smooth={sm}", compile_job, doc, None, Machine(smooth=sm))
        print(f"   steps {job.steps} replay {job.duration_s:.0f}s pierces {job.pierces}")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter
from PySide6.QtCore import QRectF
app = QApplication.instance() or QApplication([])
from mlaser.ui.mainwindow import MainWindow
w = T("MainWindow() bare", MainWindow)
w.resize(1600, 1000); w.show(); app.processEvents()
T("MainWindow.set_document (canvas+toolpath+panels)", w.set_document, doc)
cv = w.canvas
print(f"   scene items: {len(cv.scene().items())}")
T("canvas.rebuild()", cv.rebuild)
T("canvas.set_document()", cv.set_document, doc)
T("part_list.set_document", w.part_list.set_document, doc)
T("layer_panel.set_document", w.layer_panel.set_document, doc)
img = QImage(1600, 1000, QImage.Format_ARGB32)
def paint():
    p = QPainter(img); cv.render(p); p.end()
cv.zoom_fit(); app.processEvents()
for i in range(2): T(f"render viewport to QImage (fit) #{i}", paint)
def repaint_zoom(f):
    cv.zoom(f); cv.viewport().repaint(); app.processEvents()
T("zoom x4 + repaint", repaint_zoom, 4.0)
T("zoom x0.25 + repaint", repaint_zoom, 0.25)
def pan():
    cv.horizontalScrollBar().setValue(cv.horizontalScrollBar().value() + 200); cv.viewport().repaint(); app.processEvents()
T("pan + repaint", pan)
sx, sy = parts[N // 2].contours[1].segments[0].start
T("pick() hit test (1 click)", cv.pick, sx, sy)
T("pick() miss far away", cv.pick, -1000.0, -1000.0)
T("select_parts(all)", cv.select_parts, range(N))
T("select_parts([0])", cv.select_parts, [0])
T("_rubber_select(whole doc)", cv._rubber_select, QRectF(-10, -10, cols * 30 + 20, cols * 30 + 20), True, False)
T("MainWindow._snapshot (undo)", w._snapshot)
T("MainWindow._refresh (after any edit)", w._refresh)
import resource; print(f"maxrss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} MB")
