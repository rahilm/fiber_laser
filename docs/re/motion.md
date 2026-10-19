# Motion: velocity planning and interpolation

Sources:
- `MotionCtrl.dll`, PDB path `MotionCtrl_dll_V1.3.22`, dump `decomp/MotionCtrl.c`.
- A newer copy of the same classes (version string `1.3.23` at CADModule 0x10123360), statically linked into `CADModule.dll`.
- `splineAnalyerVc100.dll`.
- `JumpAddTime.txt`.

Python code is in `mlaser/motion/`:
- `profile.py`: 1-D profiles.
- `planner.py`: look-ahead.
- `interp.py`: exported interpolators, simulation and time estimate.

Tags: **O** = observed in code (address given), **I** = inferred, **U** = unknown.

## 1. Who uses what (O)

- Nothing imports `MotionCtrl.dll` statically, and its name appears in no other binary's strings, so the shipped DLL looks unused at run time (I).
- CADModule carries its own copy of the classes `CNurbsContour`, `CContoutSmooth`/`IContourSmooth` and `CVelocityPlanning`/`IVelocityPlanning`:
  - RTTI at CADModule.strings 1013f928..1013f9c8.
  - Planner constructor 0x1010cdf0, factory 0x1010d140.
  - Smoother factory 0x100fa680.
- These classes are owned by `CInterpMrg`:
  - Constructor 0x10102490: smoother at +0x108, planner at +0x10c.
  - Two instances live at CCADModule+0xdc and +0x130.
- `CInterpMrg::Interp` (0x10105260) runs the cutting pipeline:
  1. Contour, turned into 0xd0-byte primitives: lines, arcs, splines, leads and overcut (CADModule 0x1006f2d0).
  2. Micro-joint breakpoints (0x100673f0) and cooling points (0x10067fa0).
  3. `CContoutSmooth`: vt[1] sets parameters from +0xe0; vt[2] loads the contour with the tolerance, a flag and `K_X`/`K_Y`; vt[9] runs; vt[5] outputs nodes of three doubles {radius, s, factor}.
  4. Node conversion to 72-byte planner points: `s=node[1]`, `R=node[0]`, `factor=node[2]`, `feed=CInterpMrg+0xa0`.
  5. Cooling points: speed forced to 0 at each point (0x10104640). Points within 0.2 mm of either end are ignored.
  6. `CVelocityPlanning`: vt[1](points, params at CInterpMrg+0x88, use_curvature=1), then vt[4](out, n) gives one arc length per period.
  7. The smoother's vt[8] maps every sampled s back to XY.
- MainApp then turns each XY sample into a 28-byte item and NCModule packs the items into FIFO words; see cam.md §8.
- **The controller therefore receives pre-interpolated points, one per period**. The default period is 1 ms (planner parameter p5).

## 2. 1-D profiles (O)

Profile record: `double[20]` inside a 0xA8-byte element (the planner keeps the block's first and
last point index in the last 8 bytes, +0xa0/+0xa4).

| Index | Content |
|---|---|
| 0 | L |
| 1 | vs |
| 2 | vmax |
| 3 | ve |
| 4 | A |
| 5 | J |
| 6..12 | durations t1..t7 |
| 13..19 | distances s1..s7 |

The seven phases are: jerk+, accel, jerk-, cruise, jerk-, decel, jerk+.

Every function exists twice: in MotionCtrl.dll and in the v1.3.23 copy inside CADModule.dll. The
rebuild follows the CADModule copy (the one MainApp uses). The two copies have the same constants
and branch structure, and the debug harness (now `parity/harness/cad_motion_harness.c`, built by `parity/build.sh`; originally `work/motionK/h/dbg_harness.exe`, which calls
CADModule's functions through their addresses) gives identical results for both (O).

| Function | MotionCtrl | CADModule | Python |
|---|---|---|---|
| S-curve solver | 0x1000eeb0 | 0x101064b0 | `profile.scurve_solve` / `scurve_profile` |
| distance needed for v0→v1 | 0x1000ecc0 | 0x101062c0 | `profile.accel_distance` |
| bisection root finder | 0x1000ed30 | 0x10106330 | `profile._bisect` |
| f(v), low side saturated / jerk-only | 0x1000ec70 / 0x1000ec20 | 0x10106270 / 0x10106230 | `_f_sat_lo` / `_f_jerk` |
| accel-only profile (planner merge/flatten) | 0x100112c0 | 0x10107d20 | `profile.accel_profile` |
| trapezoid solver (no jerk) | 0x1000f5a0 | 0x10106bb0 | `profile.trapezoid_profile` |
| segInterp dispatcher + checks | 0x10010090 | 0x10106d40 | `interp._validate_seg` |
| fixed-period sampler (segInterp) | 0x1000f730 | 0x10106f20 | `Profile.sample` |
| fixed-period sampler (planner block) | 0x100136a0 | 0x101094e0 | `Profile.sample_from` |

**Distance for a speed change** (0x1000ecc0), with dv = v1 − v0 (0 when dv < 0):
- If dv < A²/J: `d = sqrt(dv/J)*(v0+v1)`.
- Otherwise: `d = (v0+v1)*(dv/A + A/J)/2`.

**S-curve solver cases.** vlo = min(vs, ve) is planned first; if vs > ve the phases are mirrored
at the end. vmax is raised to vhi first.
1. d(vhi→vmax) + d(vlo→vmax) < L: keep vmax; each ramp has t1 = t3 = A/J and
   t2 = (Δv − J t1²)/A when Δv ≥ A²/J, else t1 = t3 = sqrt(Δv/J).
2. Otherwise, with v3 = A²/J + vhi:
   - if L > (v3+vhi)·A/J + d(vlo→v3), both ramps saturate the acceleration and vmax has a
     closed form: `vmax = (sqrt(A⁴ + 2J(J(vlo²+vhi²) − A²(vlo+vhi) + 2AJL)) − A²) / 2J`;
   - else, if d(vlo→vhi) ≤ L: with v4 = max(vhi, A²/J + vlo), the solver bisects
     (tolerance **min(vmax/1000, 0.001)**, D-01b) between v4 and v3 with the low side saturated
     when d(vhi→v4) + d(vlo→v4) ≤ L, otherwise between vhi and v4 with both ramps jerk-only.
     For L < 0.01 it uses a constant-acceleration form instead, `vmax = sqrt(A·L + vlo² + vhi²)`;
   - else (vhi cannot be reached) it lowers A and J: `A=(vhi-vlo)(vhi+vlo)/L`,
     `J=A(vhi+vlo)/L`, pure jerk-up/jerk-down, and returns false ("infeasible").
3. The distances follow from the durations. The cruise distance is the remainder,
   s4 = L − Σ others, and t4 = s4/vmax. This can be a tiny negative number, which the DLL keeps.

**Accel-only profile** (0x10107d20, used by the planner's merge and flatten passes). It is one
ramp vlo→vhi that exactly fills L. With S = vhi+vlo and D = vhi−vlo:
- D < 0.001: constant speed with t4 = L/vlo (the A field is set to S).
- If the jerk-only ramp sqrt(D/J)·S ≥ L: A = D·S/L, J = A·S/L.
- Otherwise J is halved when sqrt(2D/J)·S < L, then A = J·L/S − J·sqrt((L/S)² − D/J),
  t1 = t3 = A/J and t2 = (D − J t1²)/A.

**Samplers.** Positions are evaluated per phase with the same closed forms (phase 2 uses
v1 = vs + J t1²/2, phase 3 counts back from its end with vmax, and so on). Times advance by
repeated addition of dt = period·0.001. Phases 1–6 are sampled while t < phase end, and phase 7
while t ≤ T.
- segInterp (0x1000f730): t starts at 0. If `trunc(T/period·1000 + 0.5) < 1`, no samples are
  produced. Each sample is divided by L, then all are multiplied by 1/last, so the last value is
  1 and **no extra end sample exists** (D-01).
- Planner (0x101094e0): see §4.

**Trapezoid (mode ≠ 2) — two bugs in both copies (D-02, accepted).**
- The cruise **distance** L−sa−sd is stored in the cruise-time slot t4. L=100, vmax=200, A=2000
  then lasts 80.2 s instead of 0.6 s.
- When no cruise fits, the peak is `sqrt(A·L + vhi²)` instead of `sqrt(A·L + (vs²+ve²)/2)`.
  L=1, vs=10, ve=30 gives 33.85 ms; the correct value is 30 ms.

Calling CADModule's segInterp_time copy (0x10106e70) shows the same numbers (80200 ms, 80890 ms,
33.85 ms). The path is **unreachable** in the shipped application:
- MotionCtrl.dll is never loaded (§1).
- CADModule reaches the trapezoid only through 0x10106d40, which is called only by its
  segInterp/segInterp_time copies 0x101078a0/0x10106e70. Those are called only from the thunks
  0x100e8420/0x100e8470 in slots 0x1ec/0x1f0 of the 219-slot interface vtable at 0x10122c4c.
- MainApp calls those slots (on `this+0x1c`) at 9 sites: 0x45858b, 0x458e93, 0x45f467,
  0x45f7d7, 0x45fcc9, 0x4619e3, 0x466808, 0x466a21, 0x466a94. All 9 push mode **2**.

The rebuild therefore keeps the physically correct trapezoid. (O for the code and call sites. I:
that the object at MainApp `this+0x1c` is the CADModule interface, based on the matching
argument lists.)

**Units** (O):
- Lengths in mm, speeds in mm/s, accelerations in mm/s², jerk in mm/s³.
- The interpolation period is in **ms**: 0x1000f730 multiplies it by 0.001.
- `segInterp_time` returns the summed phase durations × 1000, i.e. ms.

## 3. Exported functions (O)

| Export | Address | Signature (recovered) | Notes |
|---|---|---|---|
| `segInterp` | 0x100101c0 (CAD copy 0x101078a0) | `(vector<double>* out, L, vmax, vs, ve, acc, jerk, period_ms, mode)` | Checks: L ≥ 1e-4; 0.001 ≤ vmax ≤ 1e4; vs and ve are clipped to vmax (CADModule clips **before** the next test, MotionCtrl after it); L ≥ min(vs,ve)·0.00025, otherwise returns 1 (too short); vs, ve ≥ 0; L ≤ 1e6; acc ≥ 5. mode 2 = S-curve, any other mode = trapezoid. Output: s/L every period, the last value = 1 by normalisation (§2). |
| `segInterp_time` | 0x10010260 (CAD 0x10106e70) | `(double* ms, same arguments)` | Sum of the 7 durations × 1000. |
| `arcInterp` | 0x10010320 | `(…, vector<double>* out, a0, a1, R, vmax, vs, ve, accX, accY, jerkX, jerkY, period_ms, mode)` | See below. |
| `newVelocityPlanning` | 0x10016640 | returns `CVelocityPlanning*` (vtable 0x10019730) | See §4. |
| `newContourSmooth` | 0x10005e10 | returns `CContoutSmooth*` (vtable 0x100196b0) | See §5. |

**arcInterp** (ported exactly as `interp.arc_interp`, D-03).
- Checks: vmax, vs, ve, R ≥ 0; acc ≥ 5; jerk ≥ 2; period ≥ 0.01; L/vmax ≤ 1e6. Otherwise the
  output is empty.
- **mode 2:** one S-curve on L = |a1−a0|·R with A = min(accX, accY) and J = min(jerkX, jerkY).
  vs and ve are **not** clipped. It is sampled like segInterp.
- **Other modes** work in per-period units: v in mm/period = v·T/1000, and a in mm/period² =
  a·T²/10⁶.
  - The number of steps is N = trunc(L/vmax + 0.5).
  - **N < 5:** constant acceleration with N2 = trunc(2L/(vs+ve) + 0.5) samples,
    s_i = (i²·a/2 + vs·i)/L with a = (ve−vs)/N2, followed by a final 1.0.
  - **Otherwise:**
    - At θ_k = a0 + k(a1−a0)/N, the limit is `v_k = min(vmax, sqrt(R·min(accY/|sin θ|, accX/|cos θ|)))`.
      An axis is ignored when its |sin| or |cos| is < 0.001. Then v_0 ≤ vs and v_N ≤ ve.
    - A forward pass and a backward pass limit each **axis** velocity, vx = v|sin θ| and
      vy = v|cos θ|. The axis whose velocity change needs more time
      (Δvx/accX versus Δvy/accY) is checked against its distance |Δcos|·R or |Δsin|·R. When the
      change does not fit, `v_k = sqrt(v_axis,prev² + 2·a_axis·d_axis)/|axis factor|`.
    - Step times are dt_k = 2(L/N)/(v_{k−1}+v_k). Samples are taken at integer periods 0, 1, 2,
      … (while t < total), with constant acceleration inside a step. Every value is divided by
      the last one, and no 1.0 is appended.

## 4. CVelocityPlanning (look-ahead)

MainApp uses the CADModule copy:
- factory 0x1010d140, constructor 0x1010cdf0, object 0x150 bytes, vtable 0x10123374;
- owned by CInterpMrg (§1).

MotionCtrl's copy:
- vtable 0x10019730, object 0x140 bytes.

| Slot | MotionCtrl | CADModule | Purpose |
|---|---|---|---|
| [0] | 0x100165d0 | 0x1010d110 | destructor |
| [1] | 0x10016720 | 0x1010df60 | plan(points, params, use_curvature) |
| [2] | 0x100168c0 | 0x1010c9a0 | total time (0x101079c0: sum of all phase durations) |
| [3] | 0x100168f0 | 0x1010cb80 | total time as ticks |
| [4] | 0x10016970 | 0x1010c9c0 | sample(out, n) |
| [7] | 0x100173b0 | 0x1010cd20 | version: writes 0x1926f = 103023, i.e. 1.3.23 (string "1.3.23") |

Both [2] and [4] switch to a second internal planner (+0xe0 in CAD) when the byte at +0x148 is
set. vt[1] clears that byte, and the code that sets it was not found (U, not ported).

### Parameter block (O)

The Python class is `planner.PlannerParams`. CADModule has **ten** doubles at this+0x28:
- defaults 0x1013fcc8;
- the same values in CInterpMrg's constructor at +0x88: [2000, 0.125, 0.05, 200, 0, 1, 0, 200, 0, 200].

MotionCtrl has the first eight; its defaults (0x1001c288) differ only in p7 = 10.

| # | Field | Default (CAD) | Use |
|---|---|---|---|
| p0 | max_acc | 2000 mm/s² | A |
| p1 | acc_time | 0.125 s | Clamped to [0.06, 0.25]. Used for J and for q = 0.5/p1. |
| p2 | precision | 0.05 | Shape factor of the curvature speed law (0.05 is neutral). |
| p3 | max_vel | 200 mm/s | |
| p4 | min_vel | 0 | |
| p5 | period_ms | 1.0 | Sampling period. |
| p6 | start_slow_len | 0 | Active when ≥ 0.1 mm. |
| p7 | start_slow_vel | 200 (MotionCtrl 10) | Raised to at least p4. MotionCtrl also raises it to at least 0.1; CAD does not. |
| p8 | end_slow_len | 0 | CAD only; active when ≥ 0.1 mm. |
| p9 | end_slow_vel | 200 | CAD only. |

Jerk (vt[1]):
- If p3 ≤ p0·p1/2: `J = 4·p3/p1²`.
- Otherwise: `J = 2·p0/p1`.

### Planner point (O)

Input record, 72 bytes:

| Index | Content |
|---|---|
| [0] | s |
| [1] | radius of curvature |
| [2] | commanded feed |
| [3] | extra limit |
| [8] | speed factor |

Fields [4..7] are not read.

Internal record, 40 bytes:

| Index | Content |
|---|---|
| [0] | ds = s_i − s_{i−1} |
| [1] | v (point speed) |
| [2] | w (limit of segment i−1→i) |
| [3] | A (unused) |
| [4] | s |

### Algorithm (CAD addresses; every step is ported exactly in `planner.py`)

1. **Point limits, use_curvature = 1** (0x1010d1b0 = MotionCtrl 0x100169b0):
   - v_i = curvature speed (0x1010c9f0; the formula is in `curvature_speed`). Initially w_i = feed_i.
   - For i ≥ 1: `w_i = min(max(v_i, v_{i−1}), feed_i)`. **w is not capped by p3.**
   - Fill pass, repeated until no limit < 0.01 is left: a segment limit ≤ 0.01 takes the
     previous one (i ≥ 2), or for i = 1 the next one when that is larger.
   - Then `v_i = min(min(max(min(v_i, p3)·factor_i, p4), limit_i), feed_i)`, and v_0 = v_{n−1} = 0.
   - The CAD copy also appends the point speeds of points with factor < 0.99 to `Log\VelDecc.txt`
     (not reproduced).
2. **use_curvature = 0** (0x1010d8c0 = 0x10016f60, D-05): v_i = limit_i and w_i = feed_i.
   There are **no clamps and no fill**, and the end speeds are kept.
3. **Slow start / slow end** (0x1010da50; MotionCtrl's 0x10017270 has the start part only):
   - If the path is shorter than p6 + p8, both lengths become half the path.
   - Start: take the first point i ≥ 1 with s_i + 0.01 > L0.
     - If |s_i − L0| > 0.05, a point is inserted at L0. It is a copy of point 0 with
       v = w = min(w_0, p7).
     - The v and w of points 0..i (including the inserted point) are capped to p7.
   - End: searching backwards, take the first point j with s_end − s_j > L1.
     - A copy of point j+1 is inserted at s_end − L1 if it is more than 0.05 away.
     - Points j+2..end are capped to p9.
4. **Blocks** (0x1010bbc0).
   - Block parameters: A = p0, J, acc_time = p1, period = p5, Vmax = p3.
   - **Valleys** (0x10109070) over the interior steps i−1→i:
     - The trend of each step is +1 if Δv > 1, 0 if Δv ≥ −1, otherwise −1.
     - (i−1, prev+trend) is recorded when trend·prev < 1 and (prev = −1 or trend = 1). prev is
       the previous step's trend, **flat steps included** (the earlier port skipped flat steps).
     - A sum of 0 (falling→rising) gives a split at i−1.
     - A sum of −1 (falling→flat) gives a split at the flat start. If the next record is a rise
       (or this is the last record), the next record's index is also a split.
     - The last point closes the list.
   - **Valley-pair cap** (0x10108590): for consecutive splits (a, b), if
     max(v_a, w_{a+1..b}) < 1.2·max(v_a, v_b), then w_b = min(w_b, max(v_a, v_b)). The loops only
     index point b.
   - **Each range** (0x1010a890, recursive, tolerance 0.05):
     - Block (0x10108920): L = s_b − s_a, vs = v_a, ve = v_b, vmax = max(v_a, w_{a+1..b})
       (0x10108720). Adaptive jerk is `J·(0.4 + 0.6·(vmax − min(vs,ve))/p3)`.
     - S-curve solve.
     - **Infeasible fix-up**, only for 2-point paths: if the solve is infeasible and the raised
       J ≥ 1.2·J, the higher end speed is lowered by bisection to what a jerk-only ramp reaches,
       (v0+v)(v²−v0²) = J·L² (0x10107b10, tolerance min(0.01·vmax, 5)). The new speed is written
       back to the point and the block is re-solved with A, J. For a decelerating block the
       function then emits **no block**.
   - **Violation search** (0x10108b80):
     - Decelerating/flat block: the backward check (0x101082e0, tol·1.25) gives code 3. Otherwise,
       if vmax − vs > 0.001, the forward check (0x10108070, 2·tol) gives code 1.
     - Accelerating block: the forward check (1.25·tol) gives code 1. Otherwise, if
       vmax − ve > 0.001, the backward check (2·tol) gives code 3.
     - The checks walk from the block end over the points inside the ramp phases and compare the
       profile speed at each point's distance with its limit:
       - forward: v_i + max(1, tol·v_i);
       - backward: v_i(1+tol) + 8.
     - How the profile speed is evaluated:
       - phase 1/7 by Cardano on s = v0 t + J t³/6, with pow(·, 1/3);
       - phase 2/6 as sqrt(v1² + 2A d);
       - phase 3/5 approximated as sqrt(v2² + 2(vmax−v2)/t·d). The backward check divides by
         **t6** where t5 is meant; both copies do this.
     - If neither check fires, the cruise points with v_i + max(0.5, tol·v_i) < vmax are
       considered, and the one with the lowest v gives code 2.
     - Actions: code 1 → one block [a, c] + recursion [c, b]; code 3 → recursion [a, c] + one
       block [c, b]; code 2 → recursion on both halves.
   - **Merge steep blocks** (0x1010b030): this applies to blocks whose solve raised J to ≥ 1.2·J.
     - Backward pass: a decelerating one absorbs the preceding blocks (taking their vs and vmax)
       until the absorbed vs < its ve, or until d(ve→vs) < the merged length.
     - Forward pass: the same for accelerating blocks.
     - Each merged block is re-solved with A and the adaptive jerk.
   - **Merge short ramps** (0x1010ab40 with 0x10107930):
     - A block is +1 (−1) when T ≤ 1.5·acc_time, one side's ramp time is ≤ 1 ms, and the
       acceleration (deceleration) time is > 0.75·T.
     - Every run of equal non-zero class, including runs of one, becomes one accel-only block
       from the run's first point to its last (0x10107f40: vmax = max(vs, ve), adaptive jerk).
   - **Flatten bumps** (0x101087a0, last block first): if vmax ∈ [max(vs,ve)+0.01, 1.33·max(vs,ve)],
     then vmax = max(vs,ve). The block is re-solved as an S-curve if (the higher end's ramp time
     + t4)/T ≥ 0.2 (old profile), otherwise as accel-only.
5. **Sampling** (vt[4] 0x1010a0d0 → block sampler 0x101094e0):
   - The carry starts at −1. Each block is sampled from t0 = dt − carry, or t0 = 0 when carry < 0.
   - Phases 1–6 are sampled while t < phase end, and phase 7 while t ≤ T.
   - The new carry is T − t_last, or carry − T when the block produced no sample.
   - The output is the absolute s: block start (the sum of the previous L) + position.
   - **No final sample at the exact end** (D-01): "line 100 mm" gives 1134 samples ending at
     99.999999 mm, exactly as the DLL does.
   - CInterpMrg asks for at most 6000 samples per call (+0x148); the CAD loop does not stop early.

### Verification

The rebuild matches both DLLs exactly:
- 14/14 parity planner cases: time and every sample; the largest sample difference is 7e-15 mm.
- Fuzzing over 1450 random paths against the **CADModule** copy and 700 against MotionCtrl:
  - The paths mix lines, small/large radii, feed changes, factors, limits, use_curvature = 0,
    slow start/end, periods of 0.5–2 ms, and acc_time values outside the clamp range.
  - The worst differences are 1.4e-14 s and 2.3e-13 mm.
- Against MotionCtrl, the only differences (15 of 400 cases, all checked) are where the two copies'
  code differs:
  - CADModule's slow start also caps point 0;
  - the short-path halving with slow end;
  - MotionCtrl's p7 ≥ 0.1;
  - the carry after a block that produced no sample. MotionCtrl's 0x100136a0 tests whether the
    *whole* output vector is empty, CADModule's 0x101094e0 tests the block's own vector, which
    gives one sample more after a sub-period block.
- The DLL itself never terminates on a 2-point path with use_curvature = 0 and a 1e9 limit. Its
  fix-up bisection cannot reach the tolerance there; the rebuild stops after 4000 halvings.
- segInterp matches sample by sample: about 440 random mode-2 cases against CADModule's copy, and
  600 against MotionCtrl's. The only 4 MotionCtrl differences come from its clip-after-check order
  (vs, ve > vmax with a tiny L).
- arcInterp (465 random cases, modes 0/1/2) matches sample by sample.

## 5. CContoutSmooth / CNurbsContour (partly observed)

- **Constructor** (0x10005e10, 0x48 bytes): this+0x18 = 0.001, +0x20 = 37 (int), +0x28 = 4.0, +0x30 = 2.2, +0x38 = 1.2, +0x40 = −1.
- **Parameter setter** (vt[1] 0x1000a890) clamps:

  | Offset | Type | Range |
  |---|---|---|
  | +0x18 | double | [0, 0.1] |
  | +0x20 | int | [7, 30] |
  | +0x28 | double | [2, 8] |
  | +0x30 | double | [2.2, 4] (corrected in §9; was listed as [1.2, 4]) |
  | +0x38 | double | [1, 8] |

  The meanings are U. The likely reading is chord tolerance, window/point count, and three shape factors.
- **Load** (vt[2] 0x10005f70): `(contour, tol∈[0.01,0.3], flag, K_X, K_Y)`.
  - It walks the 0xd0-byte primitives: type 1 line, 2 arc (0x48-byte arc struct at +0x20), 3 spline (uses `ISpline2DAnalyer`).
  - It builds NURBS (`CNurbsContour`) and outputs nodes of {curvature radius, s, factor}.
- **K_X/K_Y** come from `JumpAddTime.txt [Arc2SegVelK]` (default 100) × 0.01. They are read in CADModule 0x101041d0 and stored at CInterpMrg+0x68/+0x70.
- The smoothing and corner-rounding algorithm was recovered in phase 2: see §9 (`mlaser/motion/smooth.py`).
  - The rebuild runs this port by default (`MotionConfig.smooth = True`, as the original). The older
    substitute (`smooth=False`: sharp corners get R = tol·c/(1−c), c = cos(deflection/2), and line
    interiors get extra R = 1e9 points) is kept for comparison only (I).

## 6. splineAnalyerVc100.dll (O)

- It wraps the **openNURBS** library: many ON_* strings, `ON_NurbsCurve` vtable 0x100dd5cc.
- Exports `newSpline2DAnalyer` (0x100c3270) and `newSpline3DAnalyer` (0x100c9250).
- `CSpline2DAnalyer` vtable is 0x100e0bb4.
- vt[1] 0x100bea60 = set curve from (control points (x,y), knots, weights, degree). It defaults to dimension 3 and order 4 (cubic), and is rational when weights are given.
- The other slots evaluate points and derivatives and sample by tolerance/arc length (U in detail).
- CADModule uses it to flatten DXF SPLINEs (from DxfParse type 8 `CDxfNubrs2d`) and inside the smoother.
- The rebuild uses ezdxf's B-spline flattening instead.

## 7. JumpAddTime.txt

Shipped values: `[Jump] AddTime=200`, `[Axis4Freq] Is4Freq=0`, `[LimitSamllCircleVel] IsLimit=0 SlowRatio=3`, `[Arc2SegVelK] K_X=100 K_Y=100`.

- **AddTime** (O, MainApp 0x004657e0 at line 79170, default 100): read in the jump/end-cut routine; the jump time becomes `AddTime·0.001 + estimated move time` and is compared with the planner time. So it is about 200 ms of extra time per jump (I: settle/lift time). Modelled as `MotionConfig.jump_add_time_ms`.
- **Arc2SegVelK** (O): see §5. Modelled as `arc2seg_k`, a factor on arc feeds (I).
- **LimitSamllCircleVel / SlowRatio** and **Axis4Freq**: the reader was not found in the dumps (U).
  The small-circle limit the planner actually applies comes from MP.EnableSmallCircleSpeedLimit /
  MP.SmallCircleSpeedLimitRatio (CInterpMrg +0x18/+0x20, §9.5).

## 8. Simulation and time estimate (rebuild)

`interp.simulate(items, MotionConfig)` and `interp.estimate_time`:
- **Cuts:** consecutive connected cut moves form one planned path that starts and ends at rest. Micro-joint spans stay in the path with laser=False, matching the original's per-sample duty of 0.
- **Rapids:** stop-to-stop S-curves with `rapid_vel`/`rapid_acc`, plus AddTime. MainApp's rapid profile (0x0045f5b0/0x0045fdf0) is 1-D (O); its parameters are U.
- **Delay / Pierce:** dwell time.
- **Output:** `Sample(t, x, y, v, laser)` every period.

## 9. CContoutSmooth algorithm (recovered)

Code: `mlaser/motion/smooth.py` (`SmoothParams`, `ContourSmoother`, `smooth_nodes`, `smooth_path_points`, `SmoothedPath`). Tests: `tests/test_camx_smooth.py`.
Addresses are MotionCtrl.dll unless prefixed "CAD" (CADModule.dll v1.3.23 copy). Tags: **O** observed, **I** inferred, **U** unknown.

### 9.1 Objects and calls

| Item | MotionCtrl | CAD | Tag |
|---|---|---|---|
| factory | `newContourSmooth` 0x10005e10 (0x48 bytes) | 0x100fa680 (0x68 bytes: adds vectors at +0x44, +0x54) | O |
| vtable | 0x100196b0: [0] dtor 0x10005e70, [1] set 0x1000a890, [2] load 0x10005f70, [3] 0x100084e0, [4] 0x10007fd0, [5] points 0x10008b70, [6] 0x10008df0, [7] 0x10009260 | 0x10123128: [0] 0x100fa6f0, [1] 0x100f9350, [2] load 0x100ff000, [3] 0x100fd5b0, [4] 0x100fc6a0, [5] nodes 0x100fc150, [6] 0x100fdca0, [7] 0x100f9bc0, [8] s→XY 0x100f9fb0, [9] 0x100f9940 | O |
| driver | — | CInterpMrg::Interp 0x10105260: `vt[1](this+0xe0)`, `vt[2](prims, tol=+0x50, flag=+0x4c, K_X=+0x68, K_Y=+0x70, mode)`, `vt[9]()`, then `vt[4]` if mode==4 else `vt[5](&nodes)` | O |

The slot numbering of motion.md §1 (vt[5] nodes, vt[8] s→XY, vt[9] run) is the CAD layout. In MotionCtrl, load does all the work and slot [5] returns XY points.
CAD's load takes a seventh argument (`mode`) and records primitive boundaries in the +0x44 list, which vt[9] reads back. It uses the same constants: -0.984375, 0.34, 0.125, 1.08/0.42/1.5/0.5, 0.8/0.2, 1e-5, 1e-4, 0.01/0.3 (O, constant set of CAD 0x100ff000). The core algorithm is therefore taken to be the same (I).

### 9.2 Parameters (setter 0x1000a890 copies 40 bytes from +0x18; ctor defaults 0x10005e10 = CInterpMrg ctor 0x10102490 values at +0xe0)

| Offset | `SmoothParams` field | Default | Clamp | Meaning (from its single use in load) | Tag |
|---|---|---|---|---|---|
| +0x18 dbl | `lead_tol` | 0.001 | [0, 0.1] | corner tolerance used instead of `tol` at the joint before a type-4 primitive (marker after a line-only lead-in), i.e. the lead-in/contour joint | O (use), I (name) |
| +0x20 int | `arc_limit_deg` | 37 | [7, 30] | on an arc, blend leg d ≤ r·P·π/180 (int × 0x1001c1a0 = deg→rad). CInterpMrg always calls the setter, so the effective default is 30 | O |
| +0x28 dbl | `line_ext` | 4.0 | [2, 8] | blend extent kd = P·d on a line | O |
| +0x30 dbl | `arc_ext` | 2.2 | **[2.2, 4]** | blend extent kd = P·d on an arc or spline (motion.md §5 had [1.2, 4]; the dump shows 0x10019888 = 2.2 as the lower bound) | O |
| +0x38 dbl | `shallow` | 1.2 | [1, 8] | shallow-corner factor: V = L0·P/8 for turns ≤ 10.15° | O |

**Load arguments** (O): `tol` is clamped to [0.01, 0.3] (0x10019900/0x100199e0). `flag` = 0 builds plain pieces with no blending (FUN_10009800), so neighbouring line nodes keep R = 10000 and corners are not slowed. `K_X`/`K_Y` = `[Arc2SegVelK]` × 0.01.

### 9.3 Algorithm (load 0x10005f70)

1. **FUN_1000a3c0(tol)** (O)
   - Drop primitives shorter than 0.005 (0x10019970), except markers.
   - An arc with sweep < 30° (0x10019968) and sagitta r(1−cos(sweep/2)) < 0.01 becomes a line.
   - An arc between two lines whose directions have dot ≥ 0.05 (0x10019768), with sagitta ≤ (acos(dot)/π + 0.5)·tol, is removed: both lines are extended to their intersection, provided it lies within the arc's chord in x and y. 90° fillets are kept, because dot = 0.
2. **FUN_100099d0** (O)
   - Merge consecutive lines when dot > 0 and |v1×v2| < 0.001·L1 and < 0.001·L2.
   - Merge co-circular arcs with the same direction (centre and radius within 0.001).
   - Remove type-4 markers, remembering the joint index for `lead_tol`.
3. **FUN_1000ab70(prims, 0.1, 2.0)**
   - **O:** runs of lines with length ≤ 2 mm and turns with dot ≥ 0 and |cross| ≤ sin 30° are gathered into a point list. FUN_1000a970/FUN_1000d0a0 reduce it (FUN_1000e230, tolerance 0.1·3 = 0.3; the chord length uses the approximation max·(1 + 0.5r − 0.125r²)), and if more than 2 points remain they are interpolated with ISpline2DAnalyer, order 4. The result is a type-3 spline primitive.
   - **I:** the rebuild uses Douglas–Peucker plus chord-length global cubic interpolation, and keeps the lines if fewer than 4 points remain.
4. **Corner sizing** (O, x87 0x100063df–0x1000674d, decoded from the FPU stack). Notation:
   - e = unit end tangent of the previous primitive, s = unit start tangent of the next.
   - c = −e·s.
   - K = −0.984375 (turn ≈ 10.15°).
   - tol_j = `lead_tol` at the lead joint, otherwise `tol`.

   Rules:
   - **No blend** if tol_j < 1e-5, or if |(−e)×s| < 0.34 and c > 0 (a cusp, turn > ≈160°). The joint becomes a sentinel and the path stops there.
   - **L0** = 2√2·tol_j / √(1 + max(c, K)) = 2·tol_j / sin(turn/2). This is the leg length at which the blend apex deviates by exactly tol_j.
   - **V** depends on the turn:
     - V = L0 when c ≥ 0 (turn ≥ 90°).
     - V = L0·P4/8 when c ≤ K.
     - Otherwise V = L0·(1 − (1 − P4/8)·(acos c − π/2)/(acos K − π/2)).
   - **Each side:** d = min(V, L_prim, and r·P1·π/180 on an arc). The extent is kd = P2·d on a line and P3·d otherwise.
   - Stored as: prev +0x120 = d, +0x138 = kd; next +0xd8 = d, +0xf0 = kd. The first and last open ends are 0.
5. **Per primitive** (O, 0x100067a2)
   - If kd_s + kd_e > L, scale both down to fit.
   - Then, if L < 1.5·(d_s + d_e), set d = kd/f with f = 1.08, or f = 1.08 + 0.42·(dsum/L − 0.5) when dsum < 2L.
   - Finally, if L − kd_s − kd_e < 0.1 (and the extents are > 0.001), add the remainder to the larger extent.
6. **Arc→line factor** (O, piece loop 0x10006b00–0x10006e80). For an arc followed by a line with dot(t_arc(L−kd), t_line(kd)) > 0.8:
   - a = |t.x|/(|t.x| + |t.y|)
   - w = (1−a)·K_Y + a·K_X
   - w ← w + (1−dot)(1−w)/0.2, clamped to [0, 1]
   - The result is stored at element +0x160 and becomes the following blend's factor (CNurbsContour +8). Other blends have factor 1 (I: the element default).
7. **Pieces** (O, 0x10006908–0x10007557)
   - Each primitive is trimmed to [kd_s, L − kd_e]:
     - lines: FUN_10007e50 → type 4 (FUN_10001580);
     - arcs: FUN_100014b0 → type 3;
     - splines: parameter window +0x68/+0x70.
     - Pieces shorter than 1e-4 are deleted.
   - Between primitives, if d_e or d_s ≥ 1e-5, FUN_10007590 builds a blend (type 2, +0x108 = 2): a **cubic clamped B-spline** with knots [0,0,0,0, 1/(n−3), …, 1,1,1,1] and control polygon A-side + [vertex] + B-side:
     - line side: the point at kd and the point at distance d from the vertex;
     - arc side (FUN_1000a240): P(kd), P(kd) ± h·t, Q ∓ h·T, P(d), where Q lies 0.667·(kd−d) along the arc (0x10019980) and h = 1.0667·r·(1−cos θ)/sin θ with θ = 0.667(kd−d)/(2r) (0x10019978: a Bézier handle);
     - spline side: P(kd), P(kd) + t·min(d, kd−d)·c (c = 0.25 if the other side is a line, else 0.5), P(d).
   - For a symmetric line/line corner the curve at the interior knot is V + d(t_B − t_A)/4, so the apex deviation is exactly tol (tested).
   - Blends with length < 1e-4 are dropped. If neither side has d ≥ 1e-5, the joint becomes the sentinel this+0x40.
   - For arc/arc joints with dot > cos 5° (0x10019808), the blend stores a radius floor min(rA, rB) at +0x110.
8. **Nodes** (CAD vt[5] 0x100fc150 + sampler 0x100f72a0) (O)
   - Line pieces give R = 10000 (0x1011f2a0) and arc pieces R = r, at s = 0, L·0.5, L.
   - Spline and blend pieces: if the curve is not straight (analyser vt+0x4c, tolerance 1e-12), the nodes come from the piece's sample table (§9.4: FUN_100f5ce0/5f10/6090, 3 mm gap). Merge nodes closer than 0.001 (keeping the minimum), set the larger of two adjacent radii < 0.05 to 0.05, and floor every R at 0.001.
   - Concatenation:
     - The first node is {R0, 0, 1}.
     - At each piece start, the previous node's R ← min(R, first R of the piece).
     - Blend pieces emit only their minimum-radius interior node and their end node, both with R ≥ floor.
     - Other pieces emit nodes 1..n−1, offset in s.
     - The last node of each piece carries the piece factor.
     - At a sentinel, both neighbouring end nodes get R = 0 (a full stop).
9. **Hand-off to the planner** (CAD 0x10105260) (O): each 24-byte node {R, s, factor} becomes a planner point with [0] = s, [1] = R, [2] = feed (CInterpMrg+0xa0), [8] = factor. After planning, vt[8] maps every sampled s on the **smoothed** path back to XY.

Side finding (O, not part of the smoother): right after vt[5], CInterpMrg checks for a small circle when its +0x18 flag is set and the contour has < 4 primitives ending in an arc with radius r ≤ 6 (0x1011fc00) and sweep > 3π/2 (0x1011fb18). If so, it multiplies the **radius** of every planner point with |R − r| ≤ min(0.05·r, 0.5) by CInterpMrg+0x20. This is the likely consumer of `[LimitSamllCircleVel]` IsLimit/SlowRatio (I).

### 9.4 Curve pieces: the sample table (O, agent V)

Blend (and spline) pieces are never evaluated directly. Code: `mlaser/motion/curvetable.py`
(`CurveTable`), C runtime math: `mlaser/motion/crtmath.py`.

1. **Analyser curve** (splineAnalyer vt[1] 0x100bea60): the blend's control polygon and its clamped
   uniform knots i/(n-3) become a **rational** cubic openNURBS curve (m_is_rat = 1, weights 1,
   dimension 3). Evaluation = `ON_EvaluateNurbsSpan` -> FUN_10058400: basis FUN_10057820
   (`ON_EvaluateNurbsBasis`), derivatives FUN_10057c10, sum of (x, y, 0, w), quotient rule
   FUN_1006cd30. The weights matter: w = sum N_i is 1 only up to the last bit, and the quotient rule
   moves the points by that much.
2. **Samples** (vt+0x30 FUN_100bfe70 with 2 deg and 0.05, the piece's +0x118/+0x120; a spline piece uses max(min(glyph length, 1), 0.05), §9.4a): every knot
   span >= 1e-5 gets its end parameters plus the interior samples of FUN_100bf9c0. That function
   evaluates EvTangent (FUN_1004d1d0) at the start, middle and end. Its inputs are the turn
   (acos of the tangent angles, FUN_100c42c0) and the chord sum (two `_hypot`). The step count is
   N = even_up(max(min(trunc(turn/2deg + 0.5), trunc(chord/0.005)), trunc(chord/0.05 + 0.5))).
   When turn > 15 rad and chord > 0.25, the span is instead split into even_up(trunc(turn/15 +
   0.001) + 1) sub-spans, recursively, and the sub-span ends themselves are not samples. Each
   sample is evaluated with D2 (`ON_EvCurvature`, FUN_1006abf0): point, unit tangent, curvature
   vector K and 1/|D1|^2. Samples closer than 0.001 mm in x and y to the previous one are
   dropped.
3. **Table** (CAD FUN_100f83c0, 0xa8-byte records at piece +0x84): samples closer than 1e-4 mm are
   dropped, and R = 1/|K| (0.001..10000). Between two samples the path is a circular arc when the
   mean radius is < 500, the chord is <= Rm*pi/4, the tangents lie on opposite sides of the chord
   and the sagitta is >= 1e-6. The centre comes from sqrt(Rm^2 - (c/2)^2), the angles from
   FUN_10009540 (atan2). Otherwise the interval is a chord. s accumulates chord or arc lengths.
   FUN_100f6c90 then smooths R: the mean over i-2..i+2 within 2 mm, and
   R <- R - 0.75 (R - mean).
4. **Nodes** (FUN_100f72a0):
   - FUN_100f5ce0: indices where the trend of R changes (thresholds +-1e-6).
   - FUN_100f5f10: drops triples closer than 0.05 mm whose outer radii agree within 10 %.
   - FUN_100f6090: adds a record every 3 mm and wherever consecutive tangents have dot < 0.85,
     and keeps the smaller radius of trend points closer than 0.1 mm.
   - The window [u0, u1] is applied to the result.
   - The end radii come from FUN_100f6840, which interpolates the raw (unsmoothed) radii with the
     weights swapped: at the start of an interval it returns the far record's radius.
   - Merge (< 0.001 mm) and end rules as listed in 9.3 step 8. Then, from node pair (1, 2) on, the
     larger of two neighbouring radii below 0.05 becomes 0.05, and every node except node 0 is
     floored at 0.001.
5. **s -> XY** (vt[8] FUN_100f9fb0 -> FUN_100f8b60 -> FUN_100f8180): the first record with
   s_k >= target; a point on the chord, or angle a1 - (a1 - a0) f on the arc. This is the
   "0.5 um s->XY" residual of the former port: XY lies on the table, not on the spline.

**C runtime.** The reference values come from the original under Wine. There msvcr100.dll is Wine's
builtin, whose `acos`, `atan2` and `_hypot` are musl's double-precision code. `crtmath.py` ports
those three, with 0 differences on 4000 random arguments each; Python's `math` differs in 6 to 19 %
of the cases by 1 ulp. This matters because on a symmetric 90 deg corner every knot span turns by
exactly pi/4. turn/2deg + 0.5 = 23.0 then rounds to 22 or 23 depending on the last bit of `acos`:
the rectangle's corners get 47, 45 and 47 table records. Native msvcr100 on Windows uses Intel's
SSE2 libm and an x87 `_hypot` (control word 0x133f), which can differ in the last bit (I, not
measurable without a native run).

### 9.4a Spline primitives (O, agent W; parity JC-07 fixed)

A DXF spline glyph (CEditableSpline, kind 7) is never flattened for the smoother. Code:
`smooth._TableG`, `smooth.glyph_table`, `curvetable.CurveTable.point_f/tangent_f/windowed`,
`curvetable._bezier_eval`. Tests: `tests/test_motion_spline_prims.py`.

1. **Primitive** (FUN_1006f2d0, type-7 branch): one type-3 record per glyph run, holding a copy of
   the glyph's spline (FUN_10005490; reversed by FUN_10036cd0 when the contour runs the glyph
   backwards: control points in reverse order, knots k'[i] = k[last] - k[n-1-i]) and the window
   +0xc0/+0xc8 = [f0, f1], fractions of the glyph in travel order. Only the glyph that holds the
   start is cut: f0 = FUN_10063b40(+0x180) = (L t - (acc - gl)) / gl for the first record, f1 the
   same fraction of t + 1 - 1 for the last one, 0 and 1 for every other glyph. +8 = (f1 - f0) * gl.
2. **Piece** (CAD load 0x100ff000, type-3 branch): FUN_100f92b0(piece, spline, +8 / (f1 - f0), 0)
   builds the analyser curve and its table (§9.4). The third argument (+0x78, the glyph length)
   sets the sampling tolerance of FUN_100f83c0: min(+0x78, 1) raised to +0x120 = 0.05, so a glyph
   longer than 1 mm is sampled with 1 mm chords (a quarter ellipse of 18.16 mm: 46 equal steps,
   47 records). Blends pass DAT_1011d8c8 = -1 and keep 0.05. The piece window is [f0, f1], the
   element length +8 = piece +0x100 = (f1 - f0) * table length (FUN_100f6770), i.e. the TABLE's
   length, not the glyph's.
3. **Bezier evaluation**: the glyph spans are single cubic spans (knots [0,0,0,0,2,2,2,2]), so
   ON_EvaluateNurbsSpan (FUN_100584d0) takes ON_EvaluateBezier (FUN_10057270): homogeneous
   control points copied forwards when t == t0 or (t <= (t0 + t1)/2 and t != t1), else backwards;
   a0 = (t1 - t) d, a1 = (t - t0) d with d = 1/(t1 - t0); full de Casteljau triangle
   CV[j] = CV[j+1] a1 + CV[j] a0 (skipped when a1 == 0); derivatives CV[i] = (CV[i] - CV[i-1]) (c/h)
   with c = degree, degree - 1, ... and h = t1 - t (backwards t0 - t), the factor c * (1/h) formed
   once per level (0x10057602); then the quotient rule for w (w drifts from 1 by an ulp). With it
   the 47 records of every spline piece of the sample are bit-identical to the original's.
4. **Element point / tangent** (corner sizing and blend construction): FUN_100f9430 /
   FUN_100f95b0 call FUN_100f5ac0 / FUN_100f5890 with the piece fraction s / +8. FUN_100f5ac0
   searches k with FUN_100f57d0 (interpolation search, s[k-1] <= total t < s[k]) and returns
   r - (r - prev) (s_k - total t) / len_k on a chord, angle a1 - (a1 - a0) (...) on an arc (not the
   arithmetic of FUN_100f8180). The ends of the element: start tangent +0xe0 at fraction 0; the
   end tangent +0x128 is taken at distance = the PRIMITIVE's length (f1 - f0) gl, i.e. at fraction
   gl / table length (about 0.9998), not at 1 (O). P(L - kd_e) and T(L - kd_e) use the fraction
   1 - kd_e / L; the A-side handle is (T m) c + P with m = min(kd_e - d_e, d_e) (FUN_100fa720).
5. **Pieces**: the element's piece keeps its object and gets the window f0 + kd_s / total,
   f1 - kd_e / total (total = table length, FUN_100f5cb0); its nodes and s -> XY come from the
   table as for blends (FUN_100f72a0 with the window, FUN_100f8b60 maps the fraction through it).
6. **Rebuild input**: `cam.toolpath` keeps the flattened chords as CutLines for everything else
   and attaches `move.curve` (a `cam.geometry.CurveRef`) with the travel range `t0`/`t1` on the
   glyph; `ContourSmoother._to_prims` joins consecutive pieces of one glyph run into one type-3
   primitive with window t / glyph length. Glyph ends are exact (0, 1) and the run that ends at
   the start ends at the start walk itself, as in the original. `nc.compiler._split_move` (slow
   start/end splits) keeps the curve on both halves. Without `move.curve` (or with
   `MotionConfig.smooth = False`) nothing changes: lines, arcs and the short-line fit are as
   before (parity: all non-spline cases unchanged).
7. **Lead tangent at a spline start**: `cam.geometry.SplineCurve.tangent` is now the analyser's
   EvTangent (openNURBS evaluation + ON_3dVector::Unitize, `curvetable._ev_tangent`); with it the
   lead-in start is bit-identical (before 1 ulp off in x).

Effect on the planner and the job: the smoother hands CVelocityPlanning the original's 42 nodes
on the spline_ellipse sample (before 809 nodes from the short-line fit of the 116 chords),
1256 samples (before 1346) that agree with the original's to 1e-12 mm; the curvature now comes
from the table radii (R 3.2..29 mm on the ellipse) instead of the fitted polyline spline.

### 9.5 Load-stage details recovered with the table (O, agent V)

- **Sharp B side** (FUN_10007590 at 0x1000787f) applies when the B primitive is an arc or a spline
  and dot(t_A, t_B) <= cos(57.29578 * 10) = 0.3736. Which point comes first depends on the turn
  side and on the side the arc curves to:
  - [P(kd) - T(kd) d, P(kd)], or
  - [P(d), handles or P(kd) - T(kd) min(kd - d, d)/2, P(kd)].
  The generic spline B side uses (kd - d)/2 without the min.
- **FUN_100099d0 merges by geometry only.** Arcs merge with centre and radius within 0.001, the
  same direction, and no limit on the sweep, so a full circle plus its overcut is one arc. The
  original's primitives carry no laser state. Micro-joints are a separate switch list placed on
  the smoothed path by fraction (FUN_10102770), so the rebuild joins micro-joint splits back and
  `laser_at` maps the fraction. Lines and points use FUN_10007b80 / FUN_10007cf0
  (P = p0 + (p1 - p0) s/L, L = hypot).
- **When smoothing runs:** always, for contours. FUN_100c6fe0 pushes smooth bit 1 for every
  CGlyContour and group member and 0 for CGlyScan paths, and select 0x10102060 stores it at
  CInterpMrg+0x4c. There is no parameter, so `Machine.smooth` and `MotionConfig.smooth` default
  to True.
- **Lead joint:** a type-4 marker follows only a lead-in whose last primitive is a line (type 1;
  type 3 line+arc ends in an arc). `cam.toolpath.build_toolpath` tags those moves (`.lead = "in"`),
  `interp.lead_in_index` finds the joint (again after the slow-start split, by lead length) and
  `plan_path(..., lead_index=)` hands it to the smoother.
- **Small circle** (Interp 0x10105260, checked under the harness): +0x18 (MP.EnableSmallCircleSpeedLimit)
  enables it. It applies when the primitive list has fewer than 4 entries (the marker counts) and
  ends in an arc with r <= 6 and sweep > 3pi/2: planner points with |R - r| <= min(0.05 r, 0.5)
  get R * (+0x20, MP.SmallCircleSpeedLimitRatio). See `MotionConfig` for the whole +8..+0x48
  block and its parameter sources (MainApp FUN_004572f0 -> ICADModule 0x1fc -> FUN_101020b0).

### 9.6 Unknowns (U)

- FUN_1000e230 (short-line reduction) and the fit of the short-line spline (FUN_1000ab70). The
  rebuild keeps Douglas-Peucker plus global interpolation, and the fitted spline's nodes are still
  sampled every 0.02 mm, not through a table.
- Spline primitives: ported (§9.4a). Not exercised against the original: multi-span spline
  glyphs (they take the basis path of §9.4, already exact for blends), backwards glyphs (the
  FUN_10036cd0 reversal is ported from the dump only) and splines split by micro-joints or by
  the rebuild's slow start/end split (the original keeps one primitive; the rebuild keeps runs
  with different feeds apart).
- The micro-joint deceleration (FUN_10103780, CInterpMrg +8/+9/+0x10) is carried in
  `MotionConfig` but not applied. The CAD vt[4] (mode 4) path is also not ported.
- The MainApp values of the Interp tolerance (+0x50) and of the planner block. The job loop that
  sets them runs after the licence check; the rebuild keeps 0.05 and the planner defaults.

## Unknowns

- CContoutSmooth: the remaining details listed in §9.6 (short-line fit, mode/vt[4]).
- CVelocityPlanning:
  - The second internal planner (+0xe0) and the flag +0x148 that selects it in vt[2]/vt[4]: who sets it is unknown, and it is not ported.
  - Exact return value of 0x10107b10 in its early exits: the x87 stack on those paths is unbalanced. The fuzzed 2-point fix-up cases match.
- Planner point fields [4..7].
- The rapid (jump) speed and acceleration parameters, which live in MainApp/ParaModule.
- The LimitSamllCircleVel and Axis4Freq consumers.
