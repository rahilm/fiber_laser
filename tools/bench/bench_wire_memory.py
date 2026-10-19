"""Memory of a compiled job's wire, plain lists vs packed arrays (nc/packed.py), per hour of
job; compile time; pickle vs shared-memory hand-off.  Usage: python -I bench_wire_memory.py [N parts]"""
import gc, math, pickle, sys, time, tracemalloc
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from mlaser.model import Document, Part, Contour, Line, Arc, Craft, LeadParams
from mlaser.nc import compiler as C
from mlaser.nc.machine import Machine
N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
cols = int(math.ceil(math.sqrt(N * 2)))
def rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]; return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)
def circ(cx, cy, r):
    s = (cx + r, cy); return Contour([Arc(s, s, (cx, cy), True)], True)
doc = Document(parts=[Part([circ((k % cols) * 30 + 12, (k // cols) * 30 + 12, 5), rect((k % cols) * 30, (k // cols) * 30, 24, 24)],
                           Craft(layer=1, lead_in=LeadParams(kind=1, length=2.0))) for k in range(N)])
for packed in (False, True):
    C.PACK_WIRE = packed
    gc.collect(); tracemalloc.start()
    t = time.perf_counter(); job = C.compile_job(doc, None, Machine(smooth=False)); dt = time.perf_counter() - t
    toolpath, job.toolpath = job.toolpath, []
    gc.collect(); cur, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    hours = job.duration_s / 3600
    print(f"packed={packed}: compile {dt:.2f}s, {job.steps} steps, {len(job.wire)} items, replay {job.duration_s:.0f}s; "
          f"retained {cur/2**20:.1f} MB ({cur/len(job.wire):.0f} B/item, {cur/2**20/hours:.0f} MB per hour of job), peak {peak/2**20:.0f} MB")
    t = time.perf_counter(); fr = job.frames(); tf = time.perf_counter() - t
    print(f"   frames() {tf:.2f}s, {len(fr)} frames")
    if packed:
        t = time.perf_counter(); s = job.to_shared(); blob = pickle.dumps(s); j2 = C.Job.from_shared(pickle.loads(blob)); ts = time.perf_counter() - t
        print(f"   to_shared+pickle+from_shared {ts*1000:.0f} ms, pickled {len(blob)} B, shared block {s.handle.size/2**20:.1f} MB")
        j2.wire.close(); s.release()
    else:
        t = time.perf_counter(); blob = pickle.dumps(job.wire); tp = time.perf_counter() - t
        print(f"   pickle(wire) {tp*1000:.0f} ms, {len(blob)/2**20:.1f} MB")
    del job, fr; gc.collect()
