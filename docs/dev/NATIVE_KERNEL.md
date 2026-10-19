# Native emitter kernel (default)

The per-sample emitter loop (sample -> quantise -> encode, MainApp FUN_00462630 /
FUN_00473bd0, ported in `mlaser/parity_ports/mainapp/sampling.py`) is the hottest part of
`compile_items` that is not geometry: about 2.5-3.4 us per step in Python.
`mlaser._native._emitter` is a C kernel of that loop that produces **bit-identical** output.
It is compiled with mlaser whenever a C compiler is present and is **used by default**: the
default ParityProfile `"original"` runs it after a built-in self-check against the Python
port.  The Python port stays the reference and the automatic fallback.
(docs/review/architecture.md §4.1, §4.3 item 6, P2-2.)

## Layout

| path | what |
|---|---|
| `mlaser/_native/_emitter.c` | the kernel (CPython C API, MIT), an optional extension of the mlaser distribution |
| `setup.py` | declares the extension and its compiler flags (the rest of the build is pyproject.toml); `MLASER_BUILD_NATIVE` |
| `mlaser/_native/__init__.py` | imports `_emitter` if it was built (`None` and `IMPORT_ERROR` otherwise); `python -m mlaser._native [--require] [--json]` prints the status |
| `mlaser/parity_ports/profiles/native.py` | `NativeEmitter`, `emit_samples`, the default selection (`DefaultEmitter`, `select`, `kernel_status`, `configure`, `selfcheck`), profile `native-sampling`, `STATS` |
| `tools/build_native.py` | rebuild in place (kernel required), `--wheel`, `--check` (FMA disassembly check, self-check, selection, parity smoke run) |
| `tools/native_parity.py` | every golden job with `original-python` and with the default profile (must be on the kernel), compared bit for bit |
| `tools/bench_native.py` | compile time and us/step, Python port vs kernel (compile_job, compile_items, the emitter alone) |
| `tests/test_native_kernel.py` | hypothesis property tests, error paths, sample of golden jobs, FMA check; the full golden comparison is marked `golden` |
| `tests/test_native_default.py` | default selection: `MLASER_NATIVE` values, `configure`, fallback on a missing kernel / failed self-check (with a warning), the self-check catching a perturbed kernel |

## Selection

| profile | emitter loop |
|---|---|
| `"original"` (default) | the C kernel if it is built and passes its self-check, else the Python port (`MLASER_NATIVE`) |
| `"original-python"` | always the Python port (the reference; `mlaser.parity_ports.ORIGINAL_PYTHON`) |
| `"original-reference"` | always the Python port, plus the statement-level ordering references |
| `"native-sampling"` | always the C kernel; raises `NativeKernelMissing` when it is not built (no fallback) |

`ORIGINAL.sampling` points `Emitter` at `profiles.native.DefaultEmitter` and `emit_samples` at
the kernel's `emit_samples` (which hands a plain port `Emitter` straight to the port's
`emit_samples`); `gap_flags` and `sample_speeds` are the port's.  The compiler takes both from
the active profile (`compile_items`: `prof.sampling.Emitter(...)`, `emit_samples` is a
`dispatch`), so nothing else changes.

**At first use** (the first `DefaultEmitter(...)`, i.e. the first compile, or the first
`kernel_status()`), once per process and under a lock, `select()` decides:

1. `MLASER_NATIVE` (or `configure(mode)`, which overrides it until `configure(None)`):
   `0` / `off` / `false` / `python` = Python port; `1` / `on` / `true` / `require` = the kernel is
   required; unset / empty / `auto` = automatic.  Any other value: a warning and `auto`.
2. `off`: the Python port.  Otherwise, if `mlaser._native._emitter` is not importable: `auto`
   uses the Python port with a logged **warning**; `on` makes every compile raise
   `NativeKernelMissing` (an `ImportError`).
3. `selfcheck()`: a built-in known-answer vector (about 2 100 steps per machine, three machines:
   ppm 2500/3 and 1234.5678, 1000 with `max_step` 40.0 and carries 1 ulp from +-0.5 / 1, a dry
   run with ppm 199.99 / 3001.7) is run through `NativeEmitter` + the kernel's `emit_samples`
   and through the port's `Emitter` + `emit_samples`; wire items, starts, position, carry,
   pulse position, steps, overspeed and the laser-on length must be identical (`float.hex`),
   and the kernel must have run every call itself (no hand-back to the port).  The vector
   covers a cut run at varying speed, SpeedCurve duty / frequency evaluated in C, slow start /
   end, micro-joint gaps with a negative duty (floor division), 40 long rapids split over many
   periods (round half to even), out-of-range explicit duty / frequency values (clamps), and
   dwells.  About 25 ms.  A failure (or an exception): `auto` falls back to the Python port
   with a **warning** naming the difference; `on` raises `NativeSelfCheckFailed`.
   Checked by hand: the kernel compiled with `-ffp-contract=fast -mfma` (5 FMA instructions)
   fails it ("output differs from the Python port in carry x, carry y").

`kernel_status()` (also `mlaser.parity_ports.kernel_status()`) returns a `KernelStatus`:
`active` (`native` / `python` / `error`), `mode`, `source` (`default`, `MLASER_NATIVE=..`,
`configure()`), `reason`, `selfcheck`, `module` (the extension's file), `kernel_version`;
`.text()` is the one-line form.  It is shown in the About dialog, printed by
`mlaser --selftest` (exit 1 when `MLASER_NATIVE=1` and the kernel cannot be used) and
`--bundle-check` (a required item under `MLASER_NATIVE=1`), and logged at selection (INFO when
the kernel runs, WARNING on a fallback).

**Inside the kernel** there is a per-call fallback, which is part of the bit-identity proof:
whenever an input is outside the domain the C code reproduces exactly, the call undoes what it
appended to `wire` / `starts`, leaves the emitter state untouched, and the port's Python code
runs the same call.  Conditions: a coordinate that is not a float (or an int below 2^53), a
`pts` item that is not a 2-tuple, `|v + carry| >= 2^52`, a pulse delta beyond 2^31 or more than
2^20 periods for one sample, a step over int8 (encode_step's ValueError), `max_step` not a
positive finite number, a duty / frequency that is not an int below 2^62, a `power` override,
an exception in a callback, a non-list `wire`.  So NaN / inf coordinates, oversize steps and
odd types raise exactly the port's exception.  `STATS` counts kernel and port calls; over the
326 golden jobs (2026-10-19): 1208 of 1208 `emit_samples` runs, 456048 of 456048 `to` calls
and all 1322 non-empty `dwell` calls ran in C (an earlier data set had one NaN rapid, which
the kernel handed to the port and which raised the port's ValueError).

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
* **A compiler-made FMA that no flag turns off.**  LLVM 20+ (Apple clang 21 / Xcode 26 on the
  macOS arm64 runners; reproduced with `zig cc` 0.15, LLVM 20, `--target=aarch64-macos`)
  lowers `fmod(r, 2.0)` (a constant power-of-two divisor) to `r - trunc(r * 0.5) * 2.0` with an
  `fmadd`, even under `-ffp-contract=off` and `#pragma STDC FP_CONTRACT OFF`: it is an
  exact rewrite, not a contraction, so neither applies (only `-ffp-model=strict` stops it).
  The three `fmadd` the check refused on macOS were all that, in `round_half_even` (inlined
  three times).  The parity test of `round_half_even` is now an integer test,
  `((long long)r & 1)`, exact because it only runs when `q - floor(q) == 0.5`, i.e. `|r| <
  2^52`; Linux output unchanged (native parity 326 jobs / 3 045 237 steps, goldens with the
  kernel on and off, 2026-10-19).  The wheel jobs now run the same FMA disassembly check on
  every installed wheel (`tools/build_native.py --check --installed`).
* **x87 excess precision** (32-bit x86 only): would need `-msse2 -mfpmath=sse`; no 32-bit
  build is planned.
* **`ceil` / `floor`** are exact functions in every libm (no rounding involved); the kernel no
  longer calls `fmod` (see the FMA trap above).
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
  326 jobs, 3 045 237 steps, 0 differences (about 45 s; 2026-10-19, with the default profile
on the kernel against `original-python`).  Also a `golden`-marked test.
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

x86-64 (container, gcc 13.3, CPython 3.13), best of 5, `smooth=False`
(`python tools/bench_native.py --repeat 5`, 2026-10-19, on a shared host: load average about
233 on 64 cores; PERFORMANCE.md §5 has the end-to-end compile times):

| job | steps | compile_items Python | compile_items native | emitter alone Python | emitter alone native |
|---|---|---|---|---|---|
| tests/data/chf/sample_v9.chf | 142 155 | 0.62 s (4.34 us/step) | 0.33 s (2.28 us/step) | 2.37 us/step | 0.15 us/step |
| synthetic 300 parts (2 contours each) | 904 525 | 3.26 s (3.60 us/step) | 1.65 s (1.82 us/step) | 2.71 us/step | 0.18 us/step |

"Emitter alone" replays the job's recorded `emit_samples` calls on a fresh emitter (the
target was about 0.1 s per million steps; the kernel is at 0.15-0.17 s/M, about 15-22x the
port).  What is left of `compile_items` is planning, `geom.at` sampling and the process items,
not the emitter; the rapids still loop over `em.to` in Python (one kernel call per sample,
about 0.6 us each).

## Building and packaging

The extension is part of the mlaser distribution (root `setup.py`, `optional=True`):

* **Development:** `uv sync` builds the editable install with setuptools and compiles the
  extension in place (`mlaser/_native/_emitter.cpython-3XX-*.so`, git-ignored).
  `[tool.uv] cache-keys` lists `setup.py` and `mlaser/_native/*.c`, so a change to the C source
  makes the next `uv sync` / `uv run` rebuild it.  `python tools/build_native.py` rebuilds in
  place on demand (kernel required) and runs `--check`.
* **No compiler:** the build prints setuptools' "building optional extension failed" warning and
  installs mlaser without the module; the default profile then runs the Python port (warning at
  first use).  CI checks this path (job `sdist` of package.yml: the sdist installed with
  `CC=/bin/false`).
* **`MLASER_BUILD_NATIVE`**: `1` makes a compile error fatal (CI, cibuildwheel, bundles), `0`
  skips the extension, unset / `auto` = optional.
* **Wheels:** cibuildwheel (`[tool.cibuildwheel]` in pyproject.toml, job `wheels` of
  package.yml): CPython 3.12 and 3.13 for manylinux x86_64 and aarch64, Windows AMD64, macOS
  arm64 and x86_64, one native runner per architecture.  Each wheel is installed in a clean
  environment and `python -m mlaser._native --require` must report the kernel active after its
  self-check.  No 32-bit builds (x87, below), no musllinux (no PySide6 wheels).
* **sdist:** carries `setup.py` and `_emitter.c` (MANIFEST.in), never a built module; the wheel
  carries the module, not the C source.
* **PyInstaller:** `collect_submodules("mlaser")` picks up `mlaser._native._emitter` like any
  other module; `packaging/build.py --smoke` runs the frozen program with `MLASER_NATIVE=1`,
  so a bundle without a working kernel fails its bundle check.

### Compiler flags and portability

Bit-identical output depends on evaluating every expression of `_emitter.c` exactly as the
port does, one IEEE binary64 operation at a time:

| compiler | flags (setup.py) | why |
|---|---|---|
| gcc (Linux, manylinux) | `-O2 -ffp-contract=off -fno-fast-math` | GCC contracts `a*b + c` into an FMA by default in GNU C mode (`-ffp-contract=fast`) on any target that has one (aarch64, x86 with `-mfma`) |
| clang (macOS, Apple silicon) | same | clang's default `-ffp-contract=on` contracts within an expression, and arm64 always has FMA; also `#pragma STDC FP_CONTRACT OFF` in the source |
| MSVC (Windows) | `/O2 /fp:precise` | `/fp:precise` does not contract unless `/fp:contract` is given (VS 2022); the source adds `#pragma float_control(precise, on)` then `#pragma fp_contract(off)` |

Never `-ffast-math`, `-Ofast`, `-ffp-contract=fast`, `-march=native`, `/fp:fast` or
`/fp:contract`.  The source refuses some of it at compile time: `#error` when `__FAST_MATH__` is
defined or `FLT_EVAL_METHOD` is neither 0 nor -1 (x87 excess precision on 32-bit x86), and a
static check that `long long` is 64 bits (LP64 and LLP64).  The C is the portable subset that
gcc, clang and MSVC (VS 2019+) compile alike: `long long` for every integer, `<math.h>`
`floor` / `ceil` / `fmod` / `fabs` / `isnan` / `isfinite` (exact functions in every libm, no
rounding), no compiler builtins, no VLAs, no `_Static_assert` (MSVC C needs `/std:c11` for it).
`math.hypot` is not computed in C at all (CPython's own, called back).

Checks of the built module, per platform: `tools/build_native.py --check` disassembles it
(objdump / llvm-objdump; dumpbin on Windows when it is on PATH, otherwise skipped) and refuses
any FMA mnemonic, then runs the self-check; at run time the self-check guards every process
anyway; CI runs `tools/native_parity.py` (all 326 golden jobs, kernel vs port) and the goldens
with `MLASER_NATIVE=1` and `=0` on Linux, Windows and macOS (ci.yml).

## Rules

* The Python port is the reference.  A change to `sampling.py`, `frames.encode_step` or
  `SpeedCurve` needs the matching change in `_emitter.c` (same operation order) and a
  `tools/native_parity.py` run in the same commit (the kernel is the default: a mismatch would
  ship).  The self-check catches only what its vector exercises.
* No `-march=native`, `-ffast-math`, `-Ofast`, `-ffp-contract=fast`.
