# Changelog

All notable changes to mlaser are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/) (`0.1.0rc1`, `0.1.0rc2`, ..., `0.1.0`; tags
`v0.1.0-rc1` style, see [docs/dev/RELEASE.md](docs/dev/RELEASE.md)).

Changes to safety behaviour, to the compiled output (the goldens) or to the parity fence are
always listed, with the reason. Machine-safety fixes are marked **Safety**.

## [Unreleased]

### Added

- Parts list (deep review 2026-11): its own dock in Design and Process, with find (Ctrl/⌘+F),
  filters by layer and warnings, sorting, and selection kept in step with the canvas both
  ways. "Assign technology…" gives selected parts another technology in one undo step, by
  moving them to a layer that uses it. Arrow keys in the parts list move through the rows
  instead of nudging the selected parts.
- Help › Save diagnostic bundle… (also `mlaser-cli diag-bundle out.zip`): one zip on your
  computer with versions, redacted settings, machine parameters, safety history, recent card
  messages, alarms and logs, for a bug report. Nothing is sent; the drawing only if you tick it.
- Help › Check for updates, and an optional check at start (Settings › Network & API ›
  Updates, off by default): one request to GitHub with no identifiers, a notice with a link;
  nothing is downloaded or installed. About, the first log line and the bundle show the
  version and the commit the build came from.
- Run: after STOP or a finished job, the Run screen shows where the job stopped (part k of n,
  time, duration, document) and the live view numbers and marks the cut parts; "Cut the
  remaining parts" and "Start from part n" start a new job of those parts with the usual
  checks (withdrawn, with the reason, when the drawing changed).
- Crash recovery: the first crash on a new installation is now detected, and the dialog says
  exactly what it restores. The resume point of a held job comes back when it matches the
  recovered drawing, offered in the resume bar without any motion; on a real machine you must
  home before resuming.
- Nest: "Open sheet n of N in Run" takes the sheet straight to the Run workspace, and Start
  names the sheet and its parts.
- Parameters: "Compare with backup or file…" shows the difference to a backup or another
  `config.json` and can restore those values.
- The machine state shows HOMING / MOVING while the axes home or move; the simulator is marked
  with a violet SIMULATOR chip and "Simulator" on the run bar. The connection choices read
  "Built-in simulator" / "Network address", and an address is never called a real card.
- Tests: the client's wire traffic and the simulator are checked against an independent
  decoder written from the protocol documentation; owners can replay their own card captures
  (`MLASER_CAPTURE_DIR`; the licence registers 59500/59503 are skipped). The simulator's
  answer to a re-sent FIFO frame is selectable (`--duplicate-fifo
  accept-twice|dedupe|reject`); new bench check `tools/bench_duplicate_frame.py` and
  `docs/dev/BENCH_CHECKS.md`.
- CI: the parity guard and the native check also run on pushes to main; nightly runs show
  every job's result in their summary; branch coverage is measured on the Linux 3.12 job;
  wheels and CI for Python 3.14 (the 3.13 column on Windows and macOS moves to the nightly
  run); actions pinned by commit SHA, Dependabot for actions and Python dependencies, build
  provenance attestations for release artifacts (`gh attestation verify <file> --repo
  rahilm/fiber_laser`).
- Interactive commissioning checklist (issue #6): below the steps, a live status row per check
  (connection, card alarms, scaling against the parameters, soft limits, emergency stop, each
  interlock input, homing, heartbeat, jog directions) follows the status the controller already
  reads, with a symbol-and-word chip per row. Each wired sensor has a "test it" prompt ("Open
  the door now") that passes when the input changes and the guard refuses (the guard's own
  classification; nothing is armed, moved or started); testing every wired interlock completes
  the Interlocks check. Every failing row has a fix button that opens the one place that fixes
  it: Safety I/O at that input, the network check, Import parameters..., Adopt the card's
  values (scaling stored as the PC's parameters, never in a dry run), Clear Alarm.
- Plugin manager (Options+ > Plugins, `app.plugin_manager`; `mlaser plugin list | search |
  info | install | uninstall | update | enable | disable | hosts | repo ...`): plugins from
  repositories (an index URL or `github:owner/repo`), a wheel URL or file; every install is
  verified first (sha256, `RECORD`, pure wheel, manifest = index, compatibility, dependencies,
  no module that shadows the core), unpacked into the data directory and recorded; installing
  never enables; updates keep a plugin enabled only when its entry point and permissions are
  unchanged; exact uninstall; everything audited (docs/user/plugins.md).
- The plugin standard v1 and plugin API 2 (docs/dev/PLUGIN_STANDARD.md): the manifest
  `mlaser-plugin.toml` (read without importing), declared permissions enforced on the plugin
  API (network hosts, secrets, the controller, the document, orders, queue), `ctx.http`
  (https only, host allowlist, checked redirects, size cap, retries), `ctx.secrets`,
  `ctx.data_dir`, `ctx.run_in_background` with progress, `ctx.services`, `ctx.show_message`,
  `ctx.icon`; the index format with a JSON Schema, the index builder
  (`python -m mlaser.services.plugin_index`, `tools/plugin_index.py`), a template for plugin
  repositories with a release workflow, and the project's plugin wheels and index published
  with every release (the default repository). `mlaser.__version__`.
- **Safety**: a plugin with a manifest gets `ctx.controller` only when it declares `machine`
  and the service engineer granted it machine authority; a plugin whose declared permissions
  changed since it was enabled is not loaded until it is enabled again.

- Command line: `mlaser compile | estimate | nest | validate | convert | queue add | queue list`
  (also `python -m mlaser ...`, `mlaser-cli`), JSON output with `--json`, documented exit codes,
  the GUI's services, no card connection (docs/user/automation.md).
- Automation API (off by default): local REST + WebSocket, read-only status and events, queue
  intake whose jobs wait for the operator's approval on the Run screen; token, localhost by
  default, the LAN monitor's limits, OpenAPI description; no remote start, stop, hold, arm,
  move or approval (docs/dev/API.md).
- **Safety**: jobs submitted from outside (API, CLI) wait in a separate approval list
  (`pending.json`) and become queue entries only when the operator approves them; a submission
  carries no cutting parameters.
- Notifications: sheet done, queue waiting, alarm / fault, job finished, maintenance due over
  ntfy, e-mail (SMTP) and webhook, per-event settings, rate limits, secrets in the keyring or a
  0600 file (docs/user/notifications.md).
- Labels: part labels on roll and A4 sheet sizes, QR job tickets per queued sheet, remnant
  labels (PDF), part-ID etching on a marking layer (docs/user/labels.md).
- Practice mode: six guided lessons on the simulator with progress (docs/user/practice.md).
- Jog pads show the jog speed next to Fast (the slow one, or the fast one while Fast is on),
  the same values as Machine › Setup.
- Array and Circular array make their copies with a progress dialog and Cancel; the window
  keeps responding, and the result is one undo step.
- Canvas legend: folds behind an arrow unless the canvas is tall, and shows each part of the
  toolpath in its real line style, now with the pierce ring. Hovering a part names its layer.
- Plugins: `ctx.register_project_section` keeps a plugin's data in the `.mlz` project; the
  plugin guide lists what the API does not cover yet.
- Tests: job tests run on a virtual clock and an in-memory simulator, in milliseconds.
- CI: a documentation-only change skips the test matrix's steps (the checks stay green under
  their names) and still runs the docs checks.

### Changed

- **Safety**: the machine is disarmed when a laser job ends, is stopped or fails; the next
  Start asks to arm again. A hold at a breakpoint, Frame, Dry run, the job queue between
  sheets and the laser-test series keep the arming. Reason: an armed machine left behind
  after a cut was one key press away from the next one (deep review 2026-11, UX 1).
- **Safety**: Start, Frame and Dry run keys (F5, F6, Shift+F6) act only on the Run workspace
  and never while you type in a field. Pressed elsewhere they open the Run workspace and
  focus the button, without starting anything. Space only holds a job; a second press no
  longer continues it (continue with Start/Continue). Stop (F12, Esc) works everywhere as
  before.
- **Safety**: after Connect, "Home the machine" asks with "Home now" / "Not now"; Not now is
  the default and the Escape answer, and there is no "Don't ask again" (reconnecting never
  homes unasked). "Go to origin" asks the same way.
- **Safety**: the commissioning checklist no longer homes or jogs while the card scaling
  disagrees with the parameters, and no longer jogs before the emergency stop is tested; with
  failures it ends "Not finished: N checks failed".
- **Safety**: Start, Frame and Dry run wait while the axes home or move ("Homing in
  progress"); STOP stops the move at once.
- **Safety**: importing parameters always shows a preview with the motion-related values
  (travel, scaling, homing, limit switches, axis direction) marked, asks before applying
  (Cancel is the default), backs up the configuration first and offers to reconnect the
  card. Machine › Setup › Apply and the parameter restores ask and back up too. Back-up and
  restore are labelled by direction ("Back up parameters to the motion card…", "Restore
  parameters from the motion card…").
- **Safety**: plugins can no longer change the plugin grants, safety settings or another
  plugin's settings through their configuration; only the operator grants machine authority
  and network hosts, in the Plugin manager. A plugin needs machine authority to hold or
  release the "PC alive" heartbeat and to get the machine controller at all, including old
  plugins without a manifest. The Plugin manager says plainly that permissions catch mistakes
  and record what you agreed to, but do not stop a plugin written to get round them.
- An alarm, e-stop, fault or lost link is shown once, in the state banner, with its own icon,
  the remedy, the laser state and one Reset alarm (the second alarm bar is gone).
- Start and Dry run on large nests begin streaming within about a second of host work instead
  of 10-40 s and use far less memory (1,000 parts: 27.6 s and +2.1 GB before, 0.2 s and
  +138 MB now); the streaming process receives the job through shared memory. The window and
  its Stop button stay responsive while Start / Dry run are checked (the status bar says
  "Checking the job…" meanwhile). The wire words sent to
  the card are unchanged (goldens and parity bit-identical).
- Editing a large drawing no longer starts a full job compile after every change: the
  toolpath updates at once, and the compile follows two seconds after the last edit, or at
  Start. Moving a large selection with the arrow keys is about three times faster, and its
  undo restores the exact positions.
- mlaser keeps much less memory on large jobs (one compiled plan of a big job instead of
  six) and gives it back when you open another drawing. Opening a drawing can still be
  undone, but the previous drawing's edit history is cleared.
- Run bar: the target is plain text, the progress is a thin line with percent / elapsed /
  ETA, and on the Run workspace the bar is a single row. Process values are shown as
  label/value pairs with steady figures; the "no technology" notice has its own action
  button.
- "Set origin here" / "Go to origin" are disabled, with the reason, in the Float coordinate
  system (run from the command palette they say why in the status bar); "Clear history" for
  alarms asks first; Preferences and Setup search find the travel
  limit and jog speeds under everyday words ("travel", "soft limit", "jog speed").
- The example-values warning of a technology recipe is asked once per recipe per session.
- mlaser depends on PySide6-Essentials ≥ 6.10 instead of the full PySide6, which saves about
  440 MB.
- Tests: the responsive-layout sweeps, the practice lessons and the timing tests spread over
  several workers; the affected tests take about half the time.
- Canvas: the head marker, the cut track and the previews are drawn over a cached picture of
  the drawing, so a status tick during a job costs about 1 ms whatever the drawing size or
  job length (it was 32 ms at 300 parts and 260 ms at 2,700, and grew with the track).
  Resizing the window with a large drawing open is up to twice as fast. On Windows at
  125 % / 150 % scaling the canvas still paints whole, as before.
- **Safety** (UX audit C1): Start while disarmed runs the pre-flight check first, as if armed;
  only when it passes one question follows with the arming safety text, *Arm and start* /
  *Cancel* (Cancel the default, warning icon). Before, the question defaulted to OK, skipped the
  safety text and armed before the check, so a refused start (e-stop pressed, no technology)
  left the machine ARMED. A start refused after arming now disarms again and says so. The Arm
  question has the verb button *Arm laser* and the warning icon (O13); every safety question
  defaults to Cancel.
- **Safety** (UX audit C2): *Reset alarm* reads the card again after `[9999, 5, 0, 0]` and clears
  only the alarms the card no longer reports; with the cause still present it reports "Reset
  refused: <alarm> still active — <remedy>" instead of "alarms cleared". The run state, the
  banners, the status chips and the run controls read one alarm source (`ctl.active_alarms`):
  the run bar showed READY after a reset while the e-stop was still pressed.
- **Safety** (UX audit C3): an emergency-stop or servo alarm disarms the machine also when idle
  or held (before: only an alarm during a job latched a FAULT; idle the machine stayed ARMED
  through the e-stop and its reset). Arming is refused while such an alarm is active, so after
  the reset the operator arms again. New run state E-STOP. docs/SAFETY.md §2.
- **Safety** (UX audit C4): Start, Frame and Dry run are disabled while a card alarm or e-stop is
  active, with the reason "Reset the alarm first: <alarm>".
- **Safety** (UX audit W17): Stop while held stays immediate and ends the job but keeps the
  resume point: "Stopped: resume point kept" with *Resume from here* / *Discard* (before it was
  discarded silently).
- Start prerequisites before Start (UX audit W3): run state NOT HOMED with *Home axes* in the
  state banner; `services.readiness.readiness()` (connected, alarms, homed, technology,
  interlocks, armed: ok / reason / fix command) and `SafetyUI.readiness()` for a readiness strip.
- One name per action (UX audit W5): *Reset alarm* everywhere (banner, Run screen, dock, menu,
  alarm list, pre-flight text), *Arm laser* without an ellipsis; a fault and card alarms share
  one banner with one *Reset alarm*.
- Pre-flight dialog (UX audit W6): titled "Before Start" / "Before Frame" / "Before Dry run",
  grouped issues with a severity icon and word, fix buttons for blocking errors (*Choose
  technology…* opens Process), a click selects the part, "<Action> anyway" / Cancel (Cancel the
  default); cutting-only warnings of Frame and Dry run are information, not warnings.
- Plain-language fault and log texts (UX audit W12): the banners show "Lost connection to the
  motion card" instead of the raw reason (card words, wire index, deadline), which stays in the
  tooltip, Details and the log file (`Log.detail`); "Held: resume point at X…, Y… mm" instead of
  "paused (send pause manu cmd): breakpoint at wire 5960".
- Machine › Connection (UX audit O9): "Demo (no card)" only for the demo controller ("Not
  connected" for a card controller without a card), the safety state in words, Connect /
  Disconnect enabled by the connection state.
- UX audit (workflow and visuals): a layer without a technology opens on *Choose technology…*
  with *Use for layer N* and its values folded until one is chosen; the layer table shows up
  to four rows in full; the Library has *Use for layer N* / *Assign to layer…*. The empty canvas
  offers Open, Import, the sample and practice. The welcome sheet has a *Connect your machine*
  path to the commissioning checklist (Service mode said in place, the switch offered, also
  for any machine command that needs a higher mode). The commissioning step list shows each
  step's result (✓ ! ✗ – …), Back says why it is off, and homing can be left for later (the
  jog checks that start from home are then skipped, never run unhomed). On Fusion, fields and
  check boxes get a boundary of at least 3:1 in every theme (the platforms' own styles are
  untouched). Process fields show "100 mm/s" (every decimal while editing); Properties with
  nothing selected shows the hint and the drawing's extent; Nest's sheet fields have labels,
  its texts real plurals and one utilisation rounding; the Run screen's selected tab is marked;
  the plugin log message names the real menu (Tools › Plugins).
- **Safety** (issue #5, UI evaluation F1): with the card switched off (e-stop) no operator command
  blocks the window any more (measured before: jog release 29 s, Gas 7 s, Clear alarm 6 s, fault
  Reset 3.5-6.6 s per press, Disconnect 10 s, window close 3.6 s). Panel commands given on the
  GUI thread run in order on one command worker; Stop, Hold and every halt still go out at once
  from their own thread, and a Stop drops the motion commands queued before it. Once the link is
  known to be lost, every request that is not a halt is refused at once and halt-class requests
  are sent once instead of waiting out the retry policy (the monitor keeps repeating the full
  halt). Nothing changes on the wire for a card that answers. docs/SAFETY.md §4a.
  The run log (one line per log message), the alarm history and the Report's alarm records are
  written by a file writer thread instead of the GUI thread: on Windows the run log's file open
  alone stood up to 0.75 s right after the card lost power (the antivirus holds a file being
  written).
- Report: every job record has a source (real / simulator / dry run / practice); the figures
  count real jobs by default (Runs filter), dry runs are recorded too. The device counters
  (processing hours, maintenance), the planned quantity and `TotalReport.txt` count real jobs
  only (review 2: a simulator sheet was recorded as production).

- `SECURITY.md`: private reporting of security and machine-safety issues (GitHub private
  vulnerability reporting).
- `CONTRIBUTING.md`: setup, the contract checked on every commit, parity and bit-exact rules,
  when goldens may be regenerated, rules for safety-relevant changes, recommended branch
  protection.
- `.github/CODEOWNERS`: owner review for the parity fence, the goldens, the controller, the
  machine safety layer and docs/SAFETY.md.
- CI: the parity guard (`.github/workflows/parity-guard.yml`, `tools/parity_guard.py`): a pull
  request that changes `tests/golden/` or `mlaser/parity_ports/` needs the label
  `parity-change` or a `Parity-Change:` commit trailer.
- CI: the default suite runs on pytest-xdist (`-n auto --dist loadgroup`); the safety and
  simulator timing tests stay together on one worker (xdist group `timing`).
- `.python-version` (3.12, the reference interpreter of the goldens).
- `tools/bench_native.py --smooth`: compile figures with the default machine (smoothing on).
- **Safety**: the gas-low, door and laser-source-alarm interlocks and the PC-alive heartbeat are
  configurable (`safety.*` in the ConfigService; Machine > Hardware settings > Safety I/O,
  Engineer and Service mode) and reach the controller at start-up and after every change (a
  change waits while the machine is armed or a job runs).  Unconfigured interlocks are named on
  the Run screen, warned about in the pre-flight of a laser job on a real machine, and flagged
  by the commissioning checklist, which also has a wiring test.  Defaults stay off.
- **Safety**: third-party (entry-point) plugins are opt-in (Options+ > Plugins, Service mode,
  `plugins.allowlist`), load after the safety UI, cannot register `run.*` / `shell.*` /
  `workspace.*` commands, and need granted machine authority to arm, reset faults or change
  safety settings.  Trust model in docs/dev/PLUGINS.md §6.
- **Safety**: limits for untrusted files: `.mlz` (unlisted members ignored, per-member and total
  size, entry count, compression ratio, JSON depth), DXF (INSERT / MINSERT expansion), SVG
  (`<use>` expansion); imports run off the GUI thread with progress and Cancel.
- LAN monitor: socket timeouts, a connection cap (per client too), a per-client rate limit; the
  sheet picture is cached and rebuilt only on change.
- Command palette: "No command matches" instead of an empty list.
- README and docs/dev/PERFORMANCE.md: suite times re-measured; compile figures labelled
  "smoothing off" (the emitter in isolation) and "default profile" (smoothing on, what the
  application compiles).
- **Safety**: a motion or laser command whose reply did not arrive is never sent again after a
  Stop, halt or fault that came while it waited.
- **Safety**: Restore parameters from the motion card or a file first shows what changes (the
  import's review, motion values marked and first), then asks and backs up; what was reviewed
  is what is stored.
- A second Hold, or a Hold during Stop, is ignored. Resume waits until the Hold has finished
  taking its breakpoint.
- Run queue: "Sheet loaded: start" no longer freezes the window while a large sheet is checked
  ("Checking the sheet's job…"); STOP or Leave queue meanwhile cancels the start.
- The time estimate appears with the toolpath, without waiting for the full compile (same
  numbers).
- The Run workspace fits half a 1512 px screen and 1280 × 800 with a taskbar at a 12 pt font,
  without scrolling; the "Load this sheet" card shows its picture while the Queue tab is open.
- Nest and Report fit a 1280 × 800 screen with a taskbar also with a 10 pt or 12 pt font: Nest
  balances its columns, Report's figures take one row, and the log under the page gives way
  down to its tab bar.
- Buttons come in two heights: mouse size at the PC, touch size (44 px) for controls that move
  the machine or switch an output and on the shop-floor screens. One size per panel.
- Tables share one look: left-aligned headers, numbers right-aligned, even row heights, and a
  line saying what to do when a table is empty (Orders, Nest parts, alarm log).
- Report and multi-page workspaces use the Run screen's touch tabs; docks and dialogs use the
  platform's own tabs. Safety I/O uses plain section headings instead of frames.
- Red and amber mean alarm, stop and hazard only: the canvas head marker and cut track are no
  longer red, the simulation's stop icon is neutral, and "Interlocks: n not configured" is a
  neutral notice.
- Accessibility: layer colours as drawn, and the toolpath colours, stay apart for red-green
  colour-blind users. Gaps are dotted grey and the pierce ring has its own colour. Colours in
  files are unchanged.
- Canvas: ruler numbers are readable in the light theme; the canvas bar sits past the rulers;
  thin lines are one pixel on high-DPI displays; the kerf band shows only where the kerf is
  visible; the Run live view has the canvas background.
- Icons follow a live theme switch (the interlocks chip, the run buttons). In high contrast,
  buttons have a visible edge. Disabled text is easier to read. Step lists, the import review
  and Nest use the app's own icons instead of font symbols.
- Machine › Connection starts on the network address once one is stored and shows what is
  connected; a first connect is logged as a connect to that address, never "Hardware
  reconnected".
- Faster: a nudge of the whole selection moves the drawing instead of redrawing it (about 3× at
  2,700 parts); resizing the window with a drawing open on Process (260 → 126 ms per step at
  300 parts); the properties panel on large selections, and nothing while hidden.
- Windows at 125 % / 150 %: the head marker and cut track draw over the cached canvas picture,
  as at 100 %.
- User guide: every menu path, workspace and folder describes the current window; a test checks
  the guide's menu paths against the real menus.
- macOS: README and release notes explain opening an unsigned build on macOS 15 (Open Anyway)
  and the Local Network permission; the network self-test names that permission.
- Python 3.14 classifier; PERFORMANCE.md re-measured with the machine and its load stated.

### Fixed

- **Safety**: a rare race where a fault raised as a job ended or was held could be lost
  without an operator reset: the safety state machine now decides and changes state under
  one lock, and only a reset or going offline leaves FAULT (deep review 2026-11, arch A2).
- **Safety**: a slow or stalled disk can no longer delay a halt or the window: log and audit
  files are written in the background (a halt took 1.75 s with stalled audit writes, about
  1 ms now; the last records can be lost if the program is killed).
- **Safety**: if the planned-quantity or material check fails to load, cutting is blocked
  with a clear message at start-up instead of running unchecked; an unreadable alarm state
  disables Start ("Alarm state unknown").
- **Safety**: a part whose cut path (leads, kerf) leaves the travel is refused before "Arm
  and start", once, with "Show in Design".
- **Safety**: Stop pressed while a job is still being prepared returns to Ready at once: no
  streaming deadline, process signal or FIFO reset follows it (before, the worker finished
  the preparation and talked to the card tens of seconds after the Stop). The worker still
  halts once more and sends its "off" commands.
- A damaged, unreadable or newer `config.json` is no longer replaced by the defaults without
  a word. mlaser keeps a copy, shows a notice (restore the last good settings, continue with
  defaults, or quit), and refuses arming until it is answered. A damaged alarm history or
  technology database no longer removes the Machine or Process workspace; a built-in part
  that fails to load is named in the status bar.
- Importing an Mlaser install stores the card's port, link type and timeouts, plus the
  follower and pierce timeouts. Every connection path and the network self-test use that one
  address.
- `pip install mlaser` works again (platformdirs is now a declared dependency).
- Opening a large drawing no longer freezes the window for several seconds while the import
  review runs. Copying hundreds of parts no longer freezes the window: the DXF copy for other
  programs follows in the background, and copy, paste and arrays show a busy state.
- After a crash, the compile workers no longer stay running.
- The Nest page fits a 1280 × 800 screen again: its columns keep their widths instead of
  collapsing to one field per row (issue #3). The window no longer grows past the screen
  when you open Nest › Orders or another tall workspace on a small screen. The log under the
  page gives way to the page on short screens (down to two lines). The welcome sheet fits a
  1280 × 800 screen without scrolling.
- Machine › Monitors shows its first monitor straight away instead of an empty frame. Log
  viewer: long log file names are shortened in the middle, with the full name in the
  tooltip; no sideways scrolling with large fonts.
- Simulator: Resume after a pause is no longer refused by the pessimistic simulator.
- A new toolpath no longer freezes the window for about 150 ms on large drawings: it is
  built in slices and the old one stays visible until the new one is complete (issue #1).
- Parts dock: at the window's minimum height "Assign technology…" was cut when the filter
  row wrapped; the dock's minimum height now follows the wrapped row.
- **Safety**: the heartbeat output (the external watchdog relay) keeps its rate on a host whose
  timers fire late (macOS: a 16-50 ms wait came 50-150 ms late). A wait of the safety monitor
  that ended past a heartbeat edge by more than half a period resynchronised the edge, so edges
  were lost (2.9-3.2 edges/s of 4 under 300 ms late waits); the monitor now ends its waits
  before each deadline by the lateness it has seen and sleeps the rest in 2 ms slices.
- **Safety**: with the heartbeat configured, the safety monitor no longer spins (a whole core,
  and the interpreter lock the window and the poller need) for as long as a fault, a lost link
  or the commissioning hold stops the heartbeat.
- Run screen: "Ready" no longer shows for a moment while a job or a resume starts (progress 0
  after Continue), nor "Running" again after the job ended; a Stop given while a job is still
  starting now stops and joins it instead of leaving a stale hold.
- System Recovery uses `autosave.mlz` whenever it was written in the same save as
  `autosave.chf`: the `.chf` is written first, so a slow write (a busy disk, a virus scanner)
  can no longer make the project look older and recovery fall back to the `.chf`.
- Closing the window (and every counters save) tells the settings' listeners once instead of
  once per counter: the GUI thread's close takes about a quarter of the time.
- **Safety**: the Run screen's STOP, HOLD, START and Arm can no longer be squeezed out of view by
  docked panels (the classic Nest dock at 1440 x 900): the Run workspace hides other docks.
- A plugin pre-claiming `run.stop` no longer stops the application from starting.
- **Safety**: answering No to "A job is running. Stop it and quit?" no longer shuts the
  application half down. The plugins' close hooks, the job service shutdown and
  `NormalExit=1` (which turns off the crash-recovery prompt) ran before the question; now
  closing decides first, and only an accepted close (also Quit, the Dock, logging out) halts
  the machine, first, then runs the hooks (UI evaluation F4).
- Unsaved work: closing, quitting, File > New, Open, a recent drawing or a file from the OS
  now ask Save / Don't Save / Cancel for a modified document (window-modal, a sheet on macOS,
  the platform's button order); Save that is cancelled or fails cancels the close. Every
  file the user keeps (`.chf`, `.mlz`, nest projects, task files, exports, reports, settings,
  machine `.ini` files) is replaced atomically, so a crash mid-write leaves the old file (the
  `.chf` bytes are unchanged). Save and the periodic autosave serialise and write in a worker:
  no more 1.2-1.6 s freezes every two minutes on large drawings (UI evaluation F3).
- After a lost connection is restored and the fault reset, the machine state shows Ready again,
  not Offline.
- Diagnostics › Dedust switches the dust extractor through the controller like the other
  outputs.
- The empty-canvas panel no longer covers the canvas bar in a short window.
- macOS: the safety banner's Connect… button is no longer cut at its top and bottom.
- `--smoke-test` now ends even when the start takes longer than its time (the program kept
  running on slow Windows and macOS machines); connecting waits for a disconnect that was
  queued while it waited.
- An idle Machine page no longer keeps the processor busy (it re-set the same tooltips and
  button states at every poll).
- The simulator no longer reports "executing without data" at the end of a job.
- Part list: following a select-all on the canvas no longer stalls with thousands of parts.
- Workspace tool bars never hide commands behind the overflow chevron with the Windows style.
- Nest: with a larger font the results column gives the parts and sheet columns room instead
  of the page scrolling.
- Report: its tabs scroll in a narrow window on macOS instead of widening the window.
- Canvas bar: Fit drawing / Fit table show as icons when the view is too narrow for their names.
- macOS: the start buttons on the empty canvas no longer overlap.
- Python 3.14.0-3.14.4: the "no full garbage collection while a job streams" check knows the
  incremental collector, which has no full collections to hold off.

## [0.1.0rc1]

First release candidate. **Not validated on any real machine**: every part that talks to the
card, the laser, the gas or the follower is tested against mlaser's own simulator only, and the
file formats and computations against the original program under Wine (README, "Safety
warning").

### Added

- **File formats**: `.chf` drawings read and written byte-exact (v5 / v9), machine and layer
  parameter XML (`HardPara.xml`, `ManuPara.xml`, `LayerPara.xml`), DXF (original rules and an
  ezdxf "modern" mode), SVG (svgelements), G-code; the native `.mlz` project format with a
  schema, migrations, atomic save, autosave and recovery (docs/dev/project-format.md).
- **CAM**: leads, kerf (shapely; opt-in pyclipper fallback), micro-joints, cooling points,
  ordering (the original's sort modes as bit-exact ports; opt-in OR-Tools mode 20), common
  edge, fly-cut, raster engraving as one object.
- **Motion and NC**: the original's look-ahead velocity planner and contour smoother as
  bit-exact ports (opt-in Ruckig profile, not hardware-validated), 1 ms sampling, pulse
  quantisation, step items and the MCC100 FIFO frames; packed wire storage; an optional C
  kernel for the emitter loop (bit-identical to the Python port, self-checked at first use).
- **Machine control**: MCC100 card client (UDP/TCP), a card simulator with a pessimistic mode
  and fault injection, job streaming in a supervised child process, breakpoint / resume,
  homing, jog, coordinate systems, edge seek, follower and laser-source devices, XHC pendant.
- **Safety** (docs/SAFETY.md): connect disarmed; an arming state machine that classifies every
  outgoing card command; `safe_halt()` on every exit path; link-loss detection with a latched
  FAULT; pre-flight checks (travel, homed, armed, undefined layers, geometry); follower, gas,
  laser-source and door interlock monitor; hold-to-jog dead-man; asynchronous Stop; global
  Hold / Stop keys; "PC alive" heartbeat output for an external watchdog relay; audit log and
  crash handler.
- **Application**: task workspaces (Design, Process, Nest, Run, Report, Machine), a persistent
  run bar, Ctrl+K command palette, optional classic ribbon; Operator / Engineer / Service
  modes; properties panel; preview with scrubber and a time and cost card; technology
  database; graphical nesting (built-in and spyrrow), job queue, production reports and
  remnant stock; guided procedures (edge seek, follower calibration, focus homing, laser
  power test, commissioning checklist, settings import from an original install); read-only
  LAN monitor; light, dark and high-contrast themes; packaged help.
- **Configuration**: `ConfigService`, typed from the original's 1,129-entry parameter registry,
  with platform directories and XML import / export.
- **Plugins**: `PluginContext` API and `mlaser.plugins` entry points (docs/dev/PLUGINS.md).
- **Languages**: English catalogue; other languages are imported from the user's own Mlaser
  install (none are shipped).
- **Packaging**: wheels (CPython 3.12 / 3.13; Linux x86_64 and aarch64, Windows AMD64, macOS
  arm64) with the C kernel, an sdist, and PyInstaller one-folder bundles for Windows, Linux and
  macOS.
- **Tests and CI**: unit, ui, sim and safety suites; frozen goldens checked bit-exactly on
  CPython 3.12 with the C kernel on and off; the Wine parity suite (local, needs the external
  harness); import-layer contracts, mypy on the typed core, the i18n check and a documentation
  link check.

[Unreleased]: https://github.com/rahilm/fiber_laser/compare/v0.1.0-rc1...HEAD
[0.1.0rc1]: https://github.com/rahilm/fiber_laser/releases/tag/v0.1.0-rc1
