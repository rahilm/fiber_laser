"""Part list refresh at 2,000 parts (docs/dev/PERFORMANCE.md, "Part list")."""
import os, sys, time, statistics
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from PySide6.QtWidgets import QApplication
app = QApplication([])
from mlaser import ops
from mlaser.model import Document, Part, Contour, Line, Arc, Craft
from mlaser.ui.i18n import translator
from mlaser.ui.panels import PartListPanel
import copy, math
def part(i):
    x, y = (i % 50) * 60.0, (i // 50) * 60.0
    sq = Contour([Line((x, y), (x + 50, y)), Line((x + 50, y), (x + 50, y + 50)), Line((x + 50, y + 50), (x, y + 50)), Line((x, y + 50), (x, y))], True)
    c = Contour([Arc((x + 35, y + 25), (x + 35, y + 25), (x + 25, y + 25), True)], True)
    return Part([sq, c], Craft(layer=1 + i % 3), name=f"p{i}")
doc = Document(parts=[part(i) for i in range(2000)])
pl = PartListPanel(translator())
t0=time.perf_counter(); pl.set_document(doc); print(f"first (cold) refresh: {(time.perf_counter()-t0)*1e3:.1f} ms")
def t(n=5):
    xs=[]
    for _ in range(n):
        t0=time.perf_counter(); pl.refresh(); xs.append((time.perf_counter()-t0)*1e3)
    return statistics.median(xs)
print(f"refresh unchanged 2000 parts: {t():.1f} ms")
ops.translate(doc, 1.0, 0.0, doc.parts[:10])
print(f"after moving 10 parts: {t(1):.1f} ms")
t0=time.perf_counter(); bbs=[p.bbox() for p in doc.parts]; print(f"bbox only: {(time.perf_counter()-t0)*1e3:.1f} ms")
