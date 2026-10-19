# Native emitter kernel (optional)

The per-sample emitter loop (sample -> quantise -> encode, MainApp FUN_00462630 /
FUN_00473bd0, ported in `mlaser/parity_ports/mainapp/sampling.py`) is the hottest part of
`compile_items` that is not geometry: about 2.5-3.4 us per step in Python.  `mlaser-native` is
an optional C kernel of that loop that produces **bit-identical** output.  The Python port stays
the reference and the default; the kernel is selected through the `native-sampling`
ParityProfile.  (docs/review/architecture.md §4.1, §4.3 item 6, P2-2.)

## Layout

| path | what |
|---|---|
| `native/` | separate distribution `mlaser-native` (setuptools, MIT): `pyproject.toml`, `setup.py` (the compiler flags), `src/mlaser_native/_emitter.c` |
| `mlaser/parity_ports/profiles/native.py` | profile `native-sampling`: `NativeEmitter`, `emit_samples`, `available()`, `require()`, `STATS` |
| `tools/build_native.py` | build + install (editable) or `--wheel`; then FMA check (objdump) and a parity smoke test |
| `tools/native_parity.py` | every golden job under both profiles, compared bit for bit |
| `tools/bench_native.py` | us/step, Python vs native (compile_items and the emitter alone) |
| `tests/test_native_kernel.py` | hypothesis property tests, error paths, sample of golden jobs, FMA check; the full golden comparison is marked `golden` |

## Selecting it

```python
import mlaser.parity_ports.profiles.native as native   # registers "native-sampling"
from mlaser.parity_ports import use_profile
native.available()                                      # is mlaser_native importable?
with use_profile("native-sampling"):
    job = compile_job(doc)
```

The profile derives from `original` and replaces only the `sampling` slot (`Emitter`,
`emit_samples`; `gap_flags` and `sample_speeds` stay the port's).  The compiler takes both
from the active profile (`compile_items`: `prof.sampling.Emitter(...)`, `emit_samples` is a
`dispatch`), so nothing else changes.

**Without the kernel**: `mlaser` installs and runs without a compiler; the profile module still
imports and registers, and `native.available()` is False.  Compiling under the profile raises
`NativeKernelMissing` (an `ImportError`) from `NativeEmitter.__init__`, with the build
instructions.  There is **no silent fallback** to Python at profile level (a user who selects
the native profile and gets the slow path would not notice otherwise).

**Inside the kernel** there is a per-call fallback, which is part of the bit-identity proof:
whenever an input is outside the domain the C code reproduces exactly, the call undoes what it
appended to `wire` / `starts`, leaves the emitter state untouched, and the port's Python code
runs the same call.  Conditions: a coordinate that is not a float (or an int below 2^53), a
`pts` item that is not a 2-tuple, `|v + carry| >= 2^52`, a pulse delta beyond 2^31 or more than
2^20 periods for one sample, a step over int8 (encode_step's ValueError), `max_step` not a
positive finite number, a duty / frequency that is not an int below 2^62, a `power` override,
an exception in a callback, a non-list `wire`.  So NaN / inf coordinates, oversize steps and
odd types raise exactly the port's exception.  `STATS` counts kernel and port calls; over the
326 golden jobs: 1141 of 1141 `emit_samples` runs, 447339 of 447340 `to` calls (the one is a
NaN rapid that raises the port's ValueError) and every non-empty `dwell` ran in C.

## What the kernel computes, operation by operation

State per emitter: `pos` (x, y), `carry` (2 doubles), `cur` (2 ints), `steps`, `overspeed`,
`on_len`, plus `wire` / `starts`.  The kernel receives the state as a tuple and returns the new
one (committed only on success).  All arithmetic is IEEE binary64 in exactly the order below.

1. **Speeds** (`sample_speeds`): `k = 1000.0 / period_ms` (computed in Python);
   `sp[i] = (ss[i] - ss[i-1]) * k`, `sp[0] = sp[1]`, `sp = 0.0` for a single sample.
2. **Duty / frequency** per sample: with curves, `dcurve(sp)` then `fcurve(sp)`.  A
   `mlaser.nc.process.SpeedCurve` is evaluated in C: `base` if disabled or `v > threshold`;
   else the first segment with `a < v <= b` (else segment 0) and
   `int((v - a) * slope + (y0 + 0.5))` - one subtraction, one multiplication, one addition of
   the precomputed `(y0 + 0.5)`, truncation.  Any other callable is called back into Python.
   Without curves: `em.duty`, `em.freq`.
3. **Slow start / end**: `if s < up_len` -> `(up_duty, up_freq)`; `if total - s < down_len` ->
   `(down_duty, down_freq)` (the override tuples are pre-filtered in Python on `up and up[4]`).
4. **Micro-joint gaps** (`gap_flags`, computed by the port in Python): laser on and
   `micro_pct > 0` -> `duty = int(duty) * int(pct) // 100` (Python floor division, reproduced
   with a sign-corrected C division); otherwise the sample is laser off.
5. **Quantise** (FUN_00473bd0), x then y: `v = (p * 10000.0 - q * 10000.0) * ppu`,
   `t = v + carry`, `d = int(t)` (truncation toward zero = C `(long long)t` for `|t| < 2^52`),
   `carry = t - d` (`d` converted back to a double: exact), `pos = (x, y)`.
   `ppu = pulses_per_mm / 10000.0` is computed in Python by the port's `__init__`.
6. **Split** (`Emitter.to`): `n = max(1, ceil(max(|dx|, |dy|) / max_step))` - both operands
   are exact doubles, so the C division is Python's correctly rounded int / int true division;
   `overspeed += n - 1`; for `k = 1..n`: `ex = round(dx * k / n)` with `dx * k` an exact
   integer below 2^51, IEEE division, and **round half to even** (`round_half_even`: `floor`,
   exact difference, `> 0.5` up, `== 0.5` up if odd), step `(ex - px, ey - py)`.
7. **Step** (`_step` + `frames.encode_step`): `on = laser and not dry`; explicit frequency:
   `max(0, min(0xFFFF, f))`; the emitter's own `self.freq` (rapids, dwells) is NOT clamped,
   only masked (`f & 0xFFFF or 5000`) as in the port; `duty = clamp(d, 0, 255) if on else 0`;
   `w1 = (f & 0xFF) << 24 | duty << 16 | (dy & 0xFF) << 8 | (dx & 0xFF)`,
   `w2 = ((power & 0xFFFF) if duty else 0) << 16 | f >> 8`, header `item_header(OP_STEP, 2)`;
   `starts.append(len(wire))`, `wire.append([header, w1, w2])`, `steps += 1`, `cur += (dx, dy)`.
8. **Cut length**: if `on and d > 0` (the unclamped duty), `on_len += math.hypot(dx / ppm_x,
   dy / ppm_y)`.  The value is **CPython's own `math.hypot`**, called back from C and memoised
   per (dx, dy) (255 x 255 table per emitter, NaN = not yet computed; reset if `em.ppm` is
   replaced).  The addition to `on_len` is done in C in the same order.
9. **Dwell**: `n = int(round(seconds * 1000.0 / period_ms))` in Python, then `n` zero steps
   in C with the emitter's duty / frequency.

### Traps found (and how each is handled)

* **`math.hypot` is not libm `hypot`.**  CPython 3.10+ computes it with its own
  `vector_norm` (scaled, compensated, correctly rounded in 3.12+); glibc's `hypot` differs in
  the last bit for some inputs, and `sqrt(x*x + y*y)` more often.  Not reproduced: callback +
  memo, so the bits are CPython's by construction; the steps only take 255^2 distinct values.
* **`round()` is half-to-even**, `int()` truncates; C `round()` is half-away-from-zero.  Own
  `round_half_even` (tested against `round` on random doubles and on every k + 0.5).
* **Two frequency rules**: an explicit `freq` argument is clamped to 0..0xFFFF before
  `encode_step`; `self.freq` is only masked.  A negative / > 0xFFFF value encodes differently in
  the two paths (property tests draw such values).
* **The duty test for the cut length uses the unclamped duty** (`d > 0`), the item the
  clamped one (equivalent, since `clamp(d, 0, 255) > 0` iff `d > 0`; kept literal anyway).
* **Python floor division** of a negative duty product (`//`), not C truncation.
* **int / int true division** in `n = ceil(m / max_step)` and `dx * k / n`: equal to the C
  double division only while the operands are exact doubles; the kernel keeps them < 2^53
  (limits above).  `max_step` may be an int or a float.
* **Floating-point contraction.**  GCC contracts `a*b + c` into an FMA by default when the
  target has one (`-ffp-contract=fast` in GNU C mode), clang does the same on AArch64 /
  Apple silicon (`-ffp-contract=on`).  `(v - a) * slope + (y0 + 0.5)` and
  `(p*1e4 - q*1e4) * ppu + carry` are exactly the shapes that would contract.  Built with
  `-O2 -ffp-contract=off -fno-fast-math`, no `-march` (also `#pragma STDC FP_CONTRACT OFF` for
  clang, `#pragma fp_contract(off)` / `float_control(precise)` for MSVC); `tools/build_native.py`
  and `test_no_fma_in_the_kernel` disassemble the module and refuse any FMA mnemonic (x86
  FMA3/FMA4, AArch64 `fmadd`/`fmsub`/`fmla`, ...).  On x86-64 the baseline ISA has no FMA anyway;
  the check matters for arm64 wheels and for anyone adding `-march=native`.
* **x87 excess precision** (32-bit x86 only): would need `-msse2 -mfpmath=sse`; no 32-bit
  build is planned.
* **`ceil` / `floor` / `fmod`** are exact functions in every libm (no rounding involved).
* **GC**: the port's wire items are tracked lists; the kernel untracks its (3 ints, never
  mutated, no cycle possible).  Visible only through `gc.is_tracked`; the cyclic GC otherwise
  re-scanned the growing wire list and cost 3x the kernel time.  The kernel also shares the int
  objects of repeated `w1` / `w2` values (direct-mapped cache, process-global: the module
  declares no per-interpreter-GIL support and needs the GIL).

## Verification

* `python tools/native_parity.py`: all 26 `jobs_samples` cases (sample .chf / DXF x two
  configs) and the 300 `jobs_random` seeds of `tools/make_goldens.py`, compiled under both
  profiles; compares `Job.wire`, `Job.frames()`, the group starts, markers, cut blocks,
  ManuItem indexes, steps, overspeed, start / end pulses, end position, and **exactly**
  (`float.hex`) `pulse_carry` and `laser_on_length_mm`, or the same exception.  Result:
  326 jobs, 3 205 860 steps, 0 differences (about 50 s).  Also a `golden`-marked test.
* `tests/test_native_kernel.py`: hypothesis streams of `emit_samples` / `to` / `dwell` /
  repositioning calls on random machines (ppm 0.5 .. 2e5, also 1000 int, 2500/3), carries at
  0, -0, +-0.5 +-1 ulp, 0.999.., +-(1 - 2^-53), random; deltas from 1e-6 to 60 mm (splits
  over many periods), negative coordinates, zero-length samples, gaps (inclusive rule, past the
  end), SpeedCurves (random nodes, enabled / disabled) and generic callables, slow start / end
  overrides with odd duty / frequency values, micro-joint percentages, dry run, `max_step` 127
  / 40 / 127.0 / 200 (int8 overflow -> ValueError on both), NaN / inf / 1e300 coordinates;
  every state field after every call compared exactly.  Plus `round_half_even` vs `round`.
* FMA check on the built module (above).
* Mutation check (by hand, during development): making the frequency clamp unconditional or
  rounding halves away from zero makes the property tests fail.  Replacing the SpeedCurve
  expression by an explicit `fma()` is NOT caught: a search of 4 million random curve
  evaluations (`math.fma` vs the two-step form) found no case where the truncated result
  differs, so that guard rests on the build flags and the disassembly check, not on the
  tests.

## Measurements

x86-64 (container, gcc 13.3, CPython 3.13), best of 3, `smooth=False`
(`python tools/bench_native.py`):

| job | steps | compile_items Python | compile_items native | emitter alone Python | emitter alone native |
|---|---|---|---|---|---|
| tests/data/autosave.chf | 235 537 | 0.96 s (4.06 us/step) | 0.43 s (1.81 us/step) | 2.51 us/step | 0.17 us/step |
| synthetic 300 parts (2 contours each) | 904 525 | 3.12 s (3.45 us/step) | 1.19 s (1.31 us/step) | 3.40 us/step | 0.15 us/step |

"Emitter alone" replays the job's recorded `emit_samples` calls on a fresh emitter (the
target was about 0.1 s per million steps; the kernel is at 0.15-0.17 s/M, about 15-22x the
port).  What is left of `compile_items` is planning, `geom.at` sampling and the process items,
not the emitter; the rapids still loop over `em.to` in Python (one kernel call per sample,
about 0.6 us each).

## Building and packaging

Development: `python tools/build_native.py` (editable install into the running interpreter
with its own setuptools, no network; then the FMA check and a smoke parity run).  `uv sync`
removes it again because `mlaser-native` is not a dependency of `mlaser`; rebuild after a sync.

Wheels (plan, not set up in CI yet): `native/` is a plain setuptools C extension with no
dependency, so `cibuildwheel` builds it unchanged:

* Linux: manylinux2014 / musllinux x86_64 and aarch64 (gcc; the flags come from setup.py);
* macOS: x86_64 and arm64 (clang; `-ffp-contract=off` is essential on arm64);
* Windows: AMD64 (MSVC `/O2 /fp:precise`; MSVC does not contract without `/fp:contract`).
* CPython 3.12 and 3.13 (the C API used is the stable subset of 3.12+; not the limited API).
* A CI job per platform should run `tools/build_native.py --check` (objdump/llvm-objdump; on
  Windows `dumpbin /disasm` and grep for `vfmadd`) and `tools/native_parity.py`; the wheel is
  shipped only if both pass.  The PyInstaller bundle may include the module; the profile
  stays opt-in.

## Rules

* The Python port is the reference.  A change to `sampling.py`, `frames.encode_step` or
  `SpeedCurve` needs the matching change in `_emitter.c` (same operation order) and a
  `tools/native_parity.py` run; until then `native-sampling` must not be used.
* No `-march=native`, `-ffast-math`, `-Ofast`, `-ffp-contract=fast`.
