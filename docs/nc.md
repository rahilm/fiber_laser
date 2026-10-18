# NC compiler and controller integration

Code: `mlaser/nc/` (`machine.py`, `compiler.py`, `stream.py`), `mlaser/controller/ui_adapter.py`,
`mlaser/controller/process.py`.
Tests: `tests/test_nc.py`, `tests/test_integration.py`, `tests/test_process_*.py`.
Tags: **O** observed (address given in the cited doc), **I** inferred, **U** unknown.

## 1. Pipeline

```
Document --build_toolpath--> MotionItems --plan_path / rapid S-curve--> s(t) every period
        --geometry--> XY mm --ftol(dmm*1e4*ppu + carry)--> int pulses --split |d|>127--> op-3000 items
        --build_fifo_frames (test per ManuItem)--> write(102, [seq, items...]) with RO[15]/RO[16] flow control
```

| Stage | Original (O) | Rebuild |
|---|---|---|
| toolpath | CADModule primitives 0x1006f2d0, MainApp job loop 0x00459100 | `cam.toolpath.build_toolpath` (cam.md §10) |
| planning + sampling | CInterpMrg 0x10105260, CVelocityPlanning, 1 ms period | `motion.interp.plan_path` + `Plan.sample(period)`; rapids `_rapid_profile` |
| pulses | MainApp FUN_00462630 → FUN_00473bd0: one ManuItem per sample **including sample 0** (zero delta, beam on); `d = ftol((x·1e4 − x_prev·1e4)·ppu + carry)`, ppu = (WritePulse/SpeedRatio)/1e4 (FUN_00458b60), `carry = v + carry − d` in the globals 0x9f2358[0..1]; no step to the exact end of a cut | `_Emitter.quantise` / `emit_samples` (identical, parity §9.R). See §1.1 |
| int8 limit | FUN_10043c20 packs dx/dy as bytes | each item is one period, so a larger delta is spread over n periods (the move slows down); counted in `Job.overspeed_periods` |
| wire item | op 3000 `encode_step` (protocol.md §5) | freq/duty = layer `frequency_hz`/`duty_pct` through the speed curves and slow start/end blocks (O, protocol.md §10.4); micro-joint gaps = samples first(s ≥ start) .. first(s ≥ end) **inclusive** (FUN_00469950, `gap_flags`), duty 0 or `duty·MC.AdvMicLinkPower_Pre/100`; power word (+8) = **0xFFFF** for every sample (0x0046297b, O) |
| process items | MainApp sequences → ManuItems → NC FUN_10043c20 | `controller.process.ProcessSequencer` → `ManuEncoder` (protocol.md §10) |
| frames | FUN_10043c20: size test `0x4af < words·4` once per ManuItem, after its whole expansion (frames up to 1199 bytes + one expansion, 306 words seen) | `Job.frames(first_seq)` = `build_fifo_frames(Job.groups())` (one group per ManuItem expansion) |
| streaming | fillFifo FUN_10052e90 | `nc.stream.JobStreamer` (subclass of `FifoStreamer`) |

## 2. Item mapping

| MotionItem | Wire |
|---|---|
| `Rapid` | sampled stop-to-stop S-curve (`Machine.rapid_vel/acc`, I), laser off, then `jump_add_time_ms` laser-off dwell (JumpAddTime.txt AddTime, O) |
| `CutLine`/`CutArc` | connected runs planned together; one step per planner sample incl. sample 0; laser byte = LaserOn state, off on the gap samples of `laser=False` moves (inclusive rule); no closing step: the next item starts from the run's nominal end |
| `Pierce` | staged pierce from GP.ManuType (1..5 stages, s = N−1…0: gas, follow-to op 105 / relative 109, laser PWM, dwell or gradual op 108/109, bolt drill = DrillDelay·1000/cycle zero steps with the beam off + FUN_00458650 PWM ramp, laser off, gas off) or smooth pierce (O, protocol.md §10.3). A layer without stages but with `pierce['time_s']` keeps the beam-on dwell (I) |
| `Delay` | zero-delta dwell, laser state held (cool points: laser off) |
| `LaserOn` | start-cut sequence (`laser_on_sequence`): cut gas, follow item (op 104 / fix height / adv. fix height), gas delay, laser PWM `[9999,3,CutFreq,CutPower,1]`, GP.LaserOnDelay. **No** laser sync on: the job loop pushes it (0x0045a101) into the vector the start cut clears (`resize(0)` at 0x0046546a) (O, parity JB-04). `Job.cut_blocks` records the block |
| `LaserOff` | laser sync off, end-cut sequence: delays, laser PWM off, gas off unless kept for a short hop, lift op 109 (class 8) or dock (class 9) unless the short-hop no-lift rule applies (O) |
| `LayerChange` | sets the layer duty/frequency; optional DA (`power_pct·Machine.power_scale`)/PWM FIFO items when `Machine.emit_layer_commands` (I, off by default). The step power word does not depend on the layer (0xFFFF) |
| `IO` | `[9999, 2/13, mask, value]` FIFO item |
| `Follow` | hint only: the follower is driven by the start/end-cut sequences (with `process.enabled = False`: phase-1 behaviour) |
| `Rapid` (cut follows) | pre-jump ManuItem class 0x12 first (focus pre-position / peak DA; nothing on a machine without AF and laser DA) |
| `Marker` | `Job.markers` (wire index → part/contour) for UI progress |

### 1.1 Pulse quantisation and the cumulative position (O, parity JB-01)

`_Emitter.quantise` is FUN_00473bd0 on both axes, with the remainder kept between calls. The
original's remainder is a global that nothing zeroes after process start (only FUN_00473bd0
touches 0x9f2358 (objdump: two accesses, both in it); its callers include the cut samples
FUN_00462630, the jumps FUN_0045f5b0, the clean-residue spiral FUN_00458790, FUN_00461290 and
FUN_004666e0). It therefore carries across elements, rapids and jobs.
In the rebuild the remainder lives in the `_Emitter`: it starts at `compile_items(..., carry=(0, 0))`,
i.e. a fresh program start, and is returned as `Job.pulse_carry`. A caller that wants a whole
session's behaviour passes the previous job's `pulse_carry` on. Consequences:

- `start_pulses + Σdeltas == end_pulses` (exact, the card replays the deltas);
- `end_pulses + pulse_carry` equals the nominal end · ppm up to the sub-tick gaps after the last
  sample of each cut run (the last sample is the last tick t ≤ T, motion.md §4; there is no closing
  step and the next element starts at the nominal end, as FUN_0045f5b0 starts the jump at the
  element end point). These gaps are far below 0.01 pulse because runs end at v = 0;
- so the card may end one pulse short of (or past) the nominal end, never more.

Rapids use the same quantiser (the rebuild's own S-curve samples, I).

The step item has no duration: each one is one servo period. Its 16-bit field is the PWM
frequency and its byte is the duty (MainApp FUN_00462630, see protocol.md §5); this resolves the
earlier conflict with docs/cam.md §10 in cam.md's favour. Job time = steps × period.

## 3. UiController (facade → card)

| Facade | Wire | Status |
|---|---|---|
| connect | read 1000×2, write 100 := 8888 | O sequence, I meaning |
| state polling | read 1000×36 + 2000×50 every 50 ms (thread) | O |
| jog / jog step | `[3, axis, ftol(v·ppm), acc, 10·acc, ftol(d·ppm)]` (axis number X 0 / Y 1; v MC.JogSlow/FastSpeed, acc MC.XFastMoveAcc unscaled, ×0.1 slow; d stroke MAC.SoftLimitMaxLen or the step); release `[1, 31, 2, decel, 200000]`, 103 := 3, 103 := 1 | O (protocol.md §4, §11a) |
| position-to / returns | `[5, 3, ftol(v·ppm), ftol(acc), 10·…, ftol(Δx·ppm), ftol(Δy·ppm), 0, 0]` relative; position-to v = jog speed, acc ·0.8; returns (mark, tool/manual origin, 回零位) v = MC.XFastMoveSpeed or 100, acc MC.XFastMoveAcc | O (protocol.md §11a, §17) |
| start / dry run / frame | compile_job / frame_items → JobStreamer in a worker thread | see §1 |
| pause / resume | default: program stop + breakpoint (RO[23]/RO[24], AutosaveParam), resume via `nc.resume` + `compiler.resume_items`; `pause_mode="fifo"` keeps 103 := 3 / 2 | O stop sequence (protocol.md §14), I resume details |
| stop | job running: cancel stream, `[1, 0x1f, 2, decel, 200000]`, PWM off at LC.PtLaserFreq, outputs off, `[118,5,0]`, 103 := 3, 103 := 1; idle: `[101]`, `[1, 0x1f, 2, decel, 200000]`, 103 := 3, 103 := 1; decel = ftol(clamp(JogStopDccFactor·XFastMoveAcc, 2000, MaxAcc)), unscaled | O (protocol.md §14) |
| outputs | `[9999, 2/13, mask, value]`; output numbers from `machine.process.output_map()` (HardPara MGP.*, LGP.*, DO.*) | O format, O numbers when loaded |
| laser button (mp51) | press: `[9999, 3, PtLaserFreq, LaserPointPower, 1]` only with the gate open; release: PWM off pair `[9999, 3|17, PtLaserFreq, 0, 0]` (MC.PtLaserTime_ms 0); timed shot (PtLaserTime_ms > 0) = `laser_test` | O (protocol.md §11a), timed shot I |
| laser test | 100 ms zero-motion steps with the laser byte set | I |
| laser power | `[9999, 4, 0, pct·100]` | O format, I scale |
| home | `[2, mask, 0]`, mask from the homing mode; done when the homed mask (status bit 15) covers it; Z only with a follower; start-up: red light off, prompt, `[2, 3, 0]`, read 1050×3 | O (protocol.md §12) |
| alarms / reset | decoded RO[6]/RO[7] + axis status bits; reset `[9999, 5, 0, 0]`; any alarm pauses the job | O (protocol.md §13) |
| backward / forward | replay the path's own step deltas with the laser off | I |
| follower | on-card follower through `mlaser.devices.follower` | O ([devices.md](devices.md)) |

Progress = executed bytes = sent − (capacity − RO[16]), capacity = RO[16] after clear (I).
The streamer prefills until the FIFO is full. A 40 ms watchdog stops the FIFO and turns the PWM
off before the card runs dry, then restarts once refilled. For a real card the streamer runs in a
separate process (`ProcessStreamer`) so UI work can't starve it (protocol.md §15).

## 4. Process items and machine parameters

- `Machine.process` (`controller.process.ProcessConfig`) holds the follower, gas, laser-DA and
  delay parameters. `Machine()` uses `ProcessConfig.bench()` (I). `Machine.from_dat_dir(dat_dir,
  ini_path=None, jump_add_time=None)` loads HardPara.xml/ManuPara.xml through
  `mlaser.formats.params.MachineParams`. The servo period comes from `AX.InterpolationCycle` (µs, O) and the process
  parameters from `ProcessConfig.from_params`. The ipAdd.ini timeouts are FollowOvertime/SectionDrillOvertime/
  GradualDrillAddOvertime and DAMinVal.
- Layer process values come from `controller.process.LayerView`. Precedence: model fields (speed, duty, freq, peak, gas,
  pressure, cut height), then `LayerParams.extra['process']` overrides (GP.* keys), then `extra['values']` (LayerPara.xml),
  then the descriptor defaults.
- `Job.process_items`, `Job.process_time_s` (estimate, not included in `duration_s`), `Job.total_time_s`,
  `Job.manu` ((wire index, ManuItem) pairs, the bolt-drill class-0 items included), `Job.cut_blocks`
  ((first sample wire index, end, layer) per LaserOn..LaserOff, used by `resume_items`) and
  `Job.groups()` (the ManuItem expansion boundaries `Job.frames` packs by) describe what was added.
- `machine.process.enabled = False` restores the phase-1 output (steps only, plus the legacy `gas_output`).
- The full wire format and the evidence are in protocol.md §10.
