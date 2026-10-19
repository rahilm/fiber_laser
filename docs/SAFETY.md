# Safety architecture

mlaser drives a multi-kW fiber laser and heavy axes. This document says what the software
guarantees, how, and where the limits are. The rationale is docs/review/architecture.md §2.
The behaviour is checked by `tests/safety/` (marker `safety`, part of the default run).

**Software alone cannot make the machine safe.** A PC can freeze, lose power or be killed. The
hardware watchdog relay (§11) and the machine's own e-stop chain are required.

## 1. Components

| Piece | Module | Role |
|---|---|---|
| `SafetyFSM`, command policy | `machine/safety.py` | state machine; classifies every card request |
| `SafetySupervisor` | `controller/supervisor.py` | the **only** holder of the card transport; guard, halt channel, link monitor, heartbeat |
| pre-flight check | `machine/preflight.py` | structured report before Start / Frame / Dry run / Resume / moves |
| interlock monitor | `controller/interlocks.py` | follower, gas, laser source, door (50 ms) |
| streamer | `controller/stream.py` | buffered-motion cap, no auto-restart, child supervision |
| exit paths | `machine/lifecycle.py` | atexit, SIGINT/SIGTERM, `sys.excepthook`, `threading.excepthook` |
| audit and logs | `machine/audit.py`, `machine/logsetup.py` | rotating `mlaser.log`, JSON-lines `audit.log`, `crash.log` |
| UI | `ui/safety_ui.py`, `ui/qtbridge.py`, `ui/safety_texts.py` | banners, run bar, global keys, dialogs |
| typed interface | `machine/api.py` (`MachineAPI`), `machine/events.py` (`EventBus`) | what the UI may call / receive |

## 2. State machine

```
OFFLINE --connect--> DISARMED --arm--> ARMED --start--> RUNNING <--> PAUSED
   ^                    ^  ^  <-disarm-+                   |            |
   |                    |  +------ job end (disarmed) -----+            |
   |                    +--reset (operator; link up, no alarm, no interlock)-- FAULT <-- any
   +------------------- disconnect / close --------------------------------  (latched)
```

* A connection **always starts disarmed** (or in FAULT if a fault is still latched; a fault
  survives a reconnect until it is reset).
* Arming is an operator action (state banner or run bar, with a confirmation). It is refused in
  dry-run mode (`--laser-off`), while a fault is latched, while an interlock holds, and while a
  job runs.
* RUNNING/PAUSED remember whether the job was started armed. Frame, dry run and
  Backward/Forward run disarmed.
* FAULT is latched: Start, Resume, jog, home, moves and FIFO starts are refused until
  `reset_fault()` (operator). The reset clears the card alarms (`[9999, 5, 0, 0]`), reads the
  status back, checks the interlocks, and leaves the machine **disarmed**. A breakpoint taken at
  the fault is kept, so the operator arms and presses Resume (laser-off rapid back, re-pierce,
  rest of the wire: `nc.resume`).

Faults are raised by: link loss, a card alarm during a job, a stream fault (unexpected idle,
underrun, child died/stalled/lost its parent), a refused command inside a job, an interlock, an
uncaught exception anywhere (excepthooks), and a failed laser-off.

## 3. Command policy (every outgoing request)

`MotionController.transact` calls the supervisor's guard before every request. The request is
classified (`classify_request`):

| class | examples | allowed |
|---|---|---|
| READ | status, parameters | always |
| HALT | `[1, 0x1f, 2, ...]`, PWM/DA/laser-sync 0, outputs off, `[118, 5, 0]`, FIFO stop/clear, alarm reset | always |
| IO | non-laser outputs: gas valves (manual gas test), red light, follower, dust extraction, lube | connected |
| MOTION | jog, move, home, FIFO start, laser-off step frames, Z/follower moves, zero | not in FAULT/OFFLINE, not after a halt until a new operator action |
| EMISSION | step items with a duty, PWM duty > 0, DA > 0, laser outputs on (shutter/gate, `LGP.DOLaser*`, `LGP.DOLaserGate*`, `ProcessConfig.do_laser`), laser sync on, laser-source emission | armed, in ARMED or RUNNING |
| CONFIG | register 100, parameter blocks, fast map, firmware/byte downloads | not during a job |

**Off commands always pass.** The guard is direction-aware: anything that turns something off -
PWM duty 0, DA 0, an output to 0 (shutter close included), laser sync off, follower stop, axis
stop, FIFO stop/clear, alarm reset, laser-source emission off - is HALT class and is never
refused, in any state including FAULT and OFFLINE (`tests/safety/test_arming_and_preflight.py::
test_off_commands_always_pass`).  The laser button's release therefore always sends the PWM-off
pair, even when the press was refused.

**Which outputs need arming:** only the laser outputs (shutter/gate and the laser DOs).  Gas
valves and the other process outputs can be switched by hand while disarmed (they are still
switched off by every halt); the beam cannot come on without arming because PWM, DA, laser
sync, step duty and the shutter all need it.

Refusals raise `CommandRefused` (a `ControllerError`, code 9101) and are written to the audit
log. The UI, diag and devices get `ctl.card()`, a `CardHandle` without access to the socket,
the guard or `close`; `ctl.mc` is a deprecated alias of it. The streaming child applies the same
policy to its own socket (no emission unless the job was started armed; nothing after a cancel).
The laser source (`devices.laser`: `emission(True)`, `EMON`, `ELE`) is refused unless the
connected controller is armed.

## 4. `safe_halt()`

Sent through the **halt channel** (a second socket, 40 ms timeout, 2 sends, its own lock, so it
never waits behind a blocked transaction), every command on its own, best effort, in the order
of the original's program stop (parity `test_stop_sequence`):

1. decelerated stop `[1, 0x1f, 2, decel, 200000]`
2. PWM off `[9999, 3, LC.PtLaserFreq, 0, 0]`, `[9999, 17, ...]`
3. process outputs off `[9999, 2, mask, 0]` (+ `[9999, 13, ...]`)
4. follower stop `[118, 5, 0]`
5. FIFO stop (103 := 3), FIFO clear (103 := 1) (not for a pause)

A halt that did not get through completely is repeated every 0.5 s until it does (the link may
come back). After a halt nothing moves until the operator starts a new action (halt latch in the
guard), so a streamer racing the stop cannot restart the card.

Called from: the job worker's `finally` (every exit except a verified complete run),
`disconnect()`, `closeEvent`, `atexit`, SIGINT/SIGTERM, `sys.excepthook` / `threading.excepthook`,
every FAULT, Stop, Pause (without the FIFO part), the jog dead-man fallback.

## 5. Pre-flight check

`ctl.preflight(action, document, ...)` returns errors (blocking) and warnings (acknowledged in
the dialog). The controller runs it itself before Start, Frame, Dry run, Resume, `move_to`,
Return-to-zero and Backward/Forward (with the exact extent of the compiled job); the UI runs it
first on the document to show the dialog.

| code | severity |
|---|---|
| `fault`, `alarm` | error |
| `not_homed` (X/Y status bit 15; policy `require_homed`, on for real cards, off for the simulator) | error |
| `not_armed` (the job fires the laser) | error |
| `layer_undefined` (no parameters in the document or the machine layer table; UI placeholders count as undefined) | error for a cut, warning laser-off |
| `out_of_travel` (card soft limits read back at connect, else `MAC*.SoftLimitMaxLen`) | error |
| `bad_geometry` (NaN/inf) | error |
| `open_contour`, `zero_length`, `travel_unknown`, `homed_unknown` | warning |

## 6. Streaming

* The card FIFO holds at most `max_buffer_ms` (default 1200 ms) of motion, prefill included:
  whatever happens to the PC or the link, the card runs on for at most that long.
* The streamer never restarts the FIFO on its own (only to release its own hold). An unexpected
  idle (external stop, alarm, FIFO dry) or an underrun (`watchdog_ms`) is a `StreamFault`.
* `ProcessStreamer` (default for real cards): the child is the only user of the main connection
  during the job and relays the status. The child halts the card itself when `os.getppid()`
  changes, when the parent's heartbeat counter stops for 0.3 s, or when the card has not answered
  for `link_deadline_ms` (the shared hold / cancel / heartbeat flags have no cross-process lock,
  so a parent killed at any moment cannot leave the child blocked). The parent faults when the child dies or, once it has sent its first
  message, is silent for 0.5 s; the child's start-up (an interpreter start, often more than
  0.5 s on Windows) is bounded by `ProcessStreamer.startup_s` (20 s) instead, and the card
  holds nothing of the job before the child has spoken. Hold and
  cancel are shared-memory flags: the pause hold is visible to the child before the stop goes
  out. `multiprocessing.freeze_support()` is called for frozen builds.
* While a job streams, the heap that existed before it is frozen (`gc.freeze`, `stream.quiet_gc`):
  a full garbage collection of a large drawing's objects would otherwise stop every thread of
  the process for up to a second (the in-thread streamer, the parent's watch of the child).

## 7. Link loss

While a job streams, no successful reply for `link_deadline_ms` (150 ms) latches FAULT (in the
process streamer the child detects it; the parent watches the child with 500 ms). While idle the
deadline is `idle_link_deadline_ms` (1.5 s), counted only while a request is unanswered, and from the end of a host stall at the latest
(a monitor wake-up more than 0.3 s late: a GUI thread held the interpreter for seconds, and the
requests timed out on the wall clock without the card being at fault). The halt is attempted at once and repeated; the card
can only run out what is buffered (≤ 1.2 s). The in-thread streamer prepares the job (wire copy,
timing, FIFO frames: about 0.1 s for 30 000 items, more on a slow PC) before the streaming
deadline applies, while the status poller still talks to the card (`JobStreamer.prepare`):
before, that preparation ran inside the 150 ms and latched a spurious "link lost" at the start of
long jobs on slow hosts.

## 8. Interlocks, jog, Stop

* Interlocks (`InterlockConfig`, values `safety.*`): follower status word 1 non-zero (on-board
  FTC), gas-low input during a cut, laser-source alarm input when armed or running, door input
  during a cut. A trip latches FAULT and halts; an active interlock refuses arming and reset
  (the reset reads the follower words afresh, and a status without them never clears a
  follower alarm: unknown is not clear).
  The original has no readable laser-source alarm (docs/re/devices.md §4), so the source's alarm
  output must be wired to an input to be monitored.
* Hold-to-jog: a continuous jog needs a keep-alive (`jog_keepalive()`, sent every 100 ms by the
  run panel and by the pendant reader); without one for `jog_deadman_s` (0.5 s) the jog is
  stopped. Release, focus loss, panel hidden, Stop and pendant HID errors stop it at once.
  `jog_stop` is idempotent: a jog already stopped (by the dead-man, a Stop, a halt) sends no
  second stop sequence, so the wire matches the original's single stop.
* Stop never blocks the caller: it flags the cancel, a background thread sends the halt first,
  then waits for the worker. Global keys while a job runs: Space = Hold/continue (not while typing
  in a text field), Esc and F12 = Stop.

## 9. Simulator and fault injection

`CardSimulator(pessimistic=True)`: after a FIFO stop or a decelerated stop the card keeps
executing buffered items for `halt_lag_ms`; the beam keeps its state until an explicit PWM off;
a FIFO start is accepted while an alarm is latched. `sim.inject(...)` / `SimFaults`: `link_down`,
`request_loss`, `reply_loss`, `latency_ms`, `jitter_ms`, `duplicate_writes`. The safety tests use
the pessimistic mode. A halt completes the step being executed (the 1-pulse resume flake).

## 10. UI

State banner (FAULT dark red with Reset, ARMED red with Disarm, DRY-RUN amber, SIMULATOR blue,
DISARMED grey with Arm), alarm banner with remedy texts (`ui/safety_texts.py`) and Details, the
status bar never shows Ready while an alarm or fault is latched, a run bar at the bottom (not in
a scroll area, 56 px targets; checked at 1280×720 = 1920×1080 at 150 %; on the Run operator
screen, which is a workspace page in the shell's scroll area, Stop, Hold, Start, Frame, Dry run
and Arm are always in view, the resume and queue controls below them may need a vertical
scroll at that size), the laser fire button
only when armed, no file open / import / new document while a job runs, start-up homing as one
dialog with "don't ask again", crash recovery as a dialog.

## 11. Hardware watchdog ("PC alive")

`UiController(heartbeat_output=N, heartbeat_hz=5)` toggles card output N as a square wave
(N toggles 10 times a second at 5 Hz) through the halt channel while connected, and stops it
(output off) on FAULT, link loss and disconnect. Default: off (0).

Wiring concept: output N drives a **retriggerable watchdog relay** (a "missing pulse" detector,
e.g. a timer relay in retrigger mode with ~300-500 ms timeout) whose normally-open contact sits
in series in the laser-enable (interlock) chain of the source, and optionally the gas valve
supply. When the pulses stop (PC frozen, program killed, link lost, fault) the relay drops and
the laser cannot emit, whatever the card does with its buffered FIFO.

**Caveat:** this is a concept. The owner must choose a relay rated for the circuit, wire it
according to the laser source's interlock documentation and the machine's risk assessment, and
validate it with the laser physically disabled: pull the network cable mid-job, kill the
program, power off the PC, and check that the enable chain opens within the timeout. The MCC100's
own behaviour on link loss is unknown (docs/re/protocol.md §9) and must be bench-tested too.

## 12. Measured bounds (simulator, pessimistic mode)

See the tables at the top of `tests/safety/test_*.py` (each asserted) and the report of the
Wave 2 Safety track. Real hardware timing (link deadline, prefill cap) must be re-tuned on the
machine (architecture review P0-3 risk: underrun tuning).

## 13. Known limits

* The card's own link-loss reaction is unknown; the buffered motion (≤ 1.2 s) runs out.
* Whether a FIFO stop holds the beam on the real card is INFERRED; the halt sends PWM off
  explicitly.
* Compile still runs on the GUI thread (Services track, `PlannedJob`).
* Follower status bit layout is unknown: any non-zero alarm word trips.

## 14. Guided procedures (Wave 3)

The wizards of the Machine workspace (`mlaser.services.procedures`, docs/user/machine.md) keep
these rules:

* **Abort** is enabled in every step and always calls `safe_halt` (also when nothing moves),
  then the procedure's clean-up (disarm, follower calibration cancel, edge-seek mode cancel).
  A step that fails halts too; a step that fails *because* of the abort counts as aborted.
* A step that moves or fires starts only from its button and first calls
  `MachineAPI.operator_action(what)`: that clears the halt latch (§4) for this new operator
  action and nothing else (a latched FAULT still refuses motion in the guard).  Workers never
  clear the latch on their own.
* The laser power test arms only inside the procedure (with a confirmation before Arm and before
  Fire), and disarms at the end, on abort and on failure.  It is refused in dry-run mode, with
  alarms or a fault.
* `MachineAPI.heartbeat_hold(True)` stops the "PC alive" output (§11) for the commissioning
  test of the watchdog relay; it can only ever drop the relay.  The checklist releases it when
  the question is answered, aborted or finished.
* A dry run uses a private simulator controller; nothing is sent to the real card.
