"""Benchmark: load .chf, compile_job phases, cProfile.  Usage: python -I bench_compile.py <file.chf|.dxf> [smooth 0/1] [profile 0/1]"""
import sys, time, resource, math, cProfile, pstats, io
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
path = sys.argv[1]
smooth = sys.argv[2] != "0" if len(sys.argv) > 2 else True
prof = sys.argv[3] == "1" if len(sys.argv) > 3 else False
t = time.perf_counter()
from mlaser.formats.chf import load_chf
from mlaser.io.dxf_import import import_dxf
from mlaser.nc import compiler as C
from mlaser.nc.machine import Machine
from mlaser.cam.toolpath import CamParams, build_toolpath
from mlaser.controller import frames as F
print(f"import {time.perf_counter()-t:.2f}s")
t = time.perf_counter()
doc = load_chf(path) if path.endswith(".chf") else import_dxf(path)
tl = time.perf_counter() - t
nseg = sum(len(c.segments) for p in doc.parts for c in p.contours)
ncont = sum(len(p.contours) for p in doc.parts)
print(f"load {tl*1000:.1f} ms: {len(doc.parts)} parts {ncont} contours {nseg} segments")
m = Machine(smooth=smooth)
t0 = time.perf_counter()
d2 = C.placed_document(C.with_layers(doc, None)); prm = CamParams()
t1 = time.perf_counter(); items = build_toolpath(d2, prm)
t2 = time.perf_counter()
pr = cProfile.Profile() if prof else None
if pr: pr.enable()
job = C.compile_items(items, m, prm.origin, d2, False)
if pr: pr.disable()
t3 = time.perf_counter()
frames = job.frames(1)
t4 = time.perf_counter()
nb = 0
for fr in frames[:2000]:
    b = F.encode_request(F.Request(func=0x40, addr=F.REG_FIFO_DATA, count=len(fr), values=fr))
    nb += len(b)
t5 = time.perf_counter()
enc_per = (t5 - t4) / max(1, min(2000, len(frames)))
print(f"smooth={smooth} toolpath items={len(items)} place {1000*(t1-t0):.1f} ms, build_toolpath {1000*(t2-t1):.1f} ms, "
      f"compile_items {t3-t2:.2f} s, frames {1000*(t4-t3):.1f} ms ({len(frames)} frames), encode_request {enc_per*1e6:.1f} us/frame -> {enc_per*len(frames)*1000:.0f} ms total")
print(f"steps {job.steps} wire items {len(job.wire)} replay {job.duration_s:.1f} s (+process {job.process_time_s:.1f}s), cut {job.laser_on_length_mm:.0f} mm rapid {job.rapid_length_mm:.0f} mm pierces {job.pierces}")
tot = t3 - t0 + (t4 - t3)
print(f"compile total {tot:.2f}s ratio real/compile = {job.duration_s/tot:.1f}x ; per step {1e6*(t3-t2)/max(1,job.steps):.1f} us")
print(f"maxrss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} MB")
w = job.wire; print(f"python objects per step: list wire item ~{sys.getsizeof(w[0]) if w else 0}+ints; wire list {len(w)}")
if pr:
    s = io.StringIO(); ps = pstats.Stats(pr, stream=s).sort_stats("tottime"); ps.print_stats(25); print(s.getvalue()[:6000])
    s = io.StringIO(); ps = pstats.Stats(pr, stream=s).sort_stats("cumulative"); ps.print_stats(30); print(s.getvalue()[:6000])
