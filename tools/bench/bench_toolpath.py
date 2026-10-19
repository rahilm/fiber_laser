"""build_toolpath per sort mode on N synthetic parts (rect + circular hole, line lead-in), best of 3.
Usage: python -I bench_toolpath.py [N]"""
import sys, time, math
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from mlaser.model import Document, Part, Contour, Line, Arc, Craft, LeadParams
from mlaser.cam.toolpath import CamParams, build_toolpath
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
cols = int(math.ceil(math.sqrt(N * 2)))
def rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)
def circ(cx, cy, r):
    s = (cx + r, cy); return Contour([Arc(s, s, (cx, cy), True)], True)
doc = Document(parts=[Part([circ((k % cols) * 30 + 12, (k // cols) * 30 + 12, 5), rect((k % cols) * 30, (k // cols) * 30, 24, 24)],
                           Craft(layer=1 + k % 4, lead_in=LeadParams(kind=1, length=2.0))) for k in range(N)])
for mode in (5, 0, 1, 6, 20, 21):
    ts = []
    for r in range(3):
        t = time.perf_counter(); build_toolpath(doc, CamParams(sort_mode=mode)); ts.append(time.perf_counter() - t)
    print(f"build_toolpath sort {mode:2d}: {min(ts)*1000:8.1f} ms")
