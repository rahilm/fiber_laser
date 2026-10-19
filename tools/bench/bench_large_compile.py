"""compile_job on N synthetic parts, smooth off/on, with and without the _locate patch; memory."""
import sys, time, math, tracemalloc
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from mlaser.model import Document, Part, Contour, Line, Arc, Craft, LeadParams
from mlaser.nc import compile_job
from mlaser.nc.machine import Machine
N = int(sys.argv[1]); cols = int(math.ceil(math.sqrt(N * 2)))
def rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]; return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)
def circ(cx, cy, r):
    s = (cx + r, cy); return Contour([Arc(s, s, (cx, cy), True)], True)
doc = Document(parts=[Part([circ((k % cols) * 30 + 12, (k // cols) * 30 + 12, 5), rect((k % cols) * 30, (k // cols) * 30, 24, 24)],
                           Craft(layer=1, lead_in=LeadParams(kind=1, length=2.0))) for k in range(N)])
for sm in (False, True):
    import resource
    if 0: tracemalloc.start()
    t = time.perf_counter(); job = compile_job(doc, None, Machine(smooth=sm)); dt = time.perf_counter() - t
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    print(f"N={N} smooth={sm}: compile {dt:.2f}s, steps {job.steps}, wire {len(job.wire)}, replay {job.duration_s:.0f}s ({job.duration_s/dt:.0f}x real time), "
          f"cut {job.laser_on_length_mm:.0f} mm, pierces {job.pierces}, manu {len(job.manu)}, traced peak {peak/2**20:.0f} MB, "
          f"bytes/step {peak/max(1,job.steps):.0f}")
    del job
