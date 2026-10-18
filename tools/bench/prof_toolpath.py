"""cProfile build_toolpath (sort 0 and 5) and MainWindow._refresh on N synthetic parts."""
import sys, cProfile, pstats, io
sys.argv = [sys.argv[0], sys.argv[1] if len(sys.argv) > 1 else "2000"]
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
import math
from mlaser.model import Document, Part, Contour, Line, Arc, Craft, LeadParams
from mlaser.cam.toolpath import CamParams, build_toolpath
N = int(sys.argv[1]); cols = int(math.ceil(math.sqrt(N * 2)))
def rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]; return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)
def circ(cx, cy, r):
    s = (cx + r, cy); return Contour([Arc(s, s, (cx, cy), True)], True)
doc = Document(parts=[Part([circ((k % cols) * 30 + 12, (k // cols) * 30 + 12, 5), rect((k % cols) * 30, (k // cols) * 30, 24, 24)],
                           Craft(layer=1 + k % 4, lead_in=LeadParams(kind=1, length=2.0))) for k in range(N)])
def prof(label, fn):
    pr = cProfile.Profile(); pr.enable(); fn(); pr.disable()
    s = io.StringIO(); pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(14); print("=====", label); print(s.getvalue()[:3500])
prof("build_toolpath sort 0", lambda: build_toolpath(doc, CamParams(sort_mode=0)))
prof("build_toolpath sort 5", lambda: build_toolpath(doc, CamParams(sort_mode=5)))
from PySide6.QtWidgets import QApplication
app = QApplication([])
from mlaser.ui.mainwindow import MainWindow
w = MainWindow(); w.set_document(doc)
prof("MainWindow._refresh", w._refresh)
prof("canvas.select_parts([0])", lambda: w.canvas.select_parts([0]))
