# Getting started

## Install

mlaser needs Python 3.12, 3.13 or 3.14 (3.12 is the reference the test jobs are checked on). From a
source checkout:

```bash
uv sync --locked                 # creates .venv with mlaser and its dependencies
uv sync --locked --extra devices # adds the pendant (HID) and serial device support
```

Without uv: `python -m venv .venv` and `.venv/bin/pip install -e .`. A packaged build (one
folder, no Python needed) is made with `packaging/build.py`; see [Release](../dev/RELEASE.md).

## First start

```bash
mlaser --demo        # a sample drawing and the built-in card simulator
```

On the first start mlaser runs a short network self-test (it checks that the card's network can
be reached; `python -m mlaser --selftest` runs it again). Set `MLASER_NO_FIRSTRUN=1` to skip it.

The window has the menu bar (File, Edit, View, Draw, Process, Workspace, Machine, Tools, Window,
Help), a top bar with the command search (Ctrl+K), the safety state banner, the **rail** of
workspaces on the left (Design, Process, Nest, Run, Report, Machine), the drawing or the
workspace's own screen in the middle with its panels, and the **run bar** at the bottom (Start,
Hold, STOP, Frame, Dry run, Arm laser). The status bar shows the card and laser states and the
head position. [The window](shell.md) describes all of it; the original's ribbon is still there
(**View › Classic ribbon**).

## Try a job on the simulator

1. Start with `mlaser --demo` (or **Machine › Connect to simulator**, or *Built-in simulator* and
   **Connect** on the Machine workspace's *Connection* page). The run bar then reads *Simulator*
   and the state banner shows the violet **SIMULATOR** chip.
2. Open a drawing (**File › Open**: `.mlz`, `.chf`, `.dxf`, `.svg` or G-code), or keep the demo part.
3. On the **Process** workspace choose a technology for each layer (*Choose technology…*), or set
   the layer's values by hand ([Process](process.md#2-process-workspace)).
4. **View › Simulate (preview)** (`P` on the canvas) plays the toolpath with the motion planner;
   the Preview panel shows the time estimate.
5. On the **Run** workspace press **Frame** to trace the outline, then **Start**. Start checks the
   job first (the pre-flight check), then asks *Arm and start* (Cancel is the default). The live
   view follows the position the simulator reports; the machine is disarmed again when the job
   ends ([Safety](safety.md#arming)).

Nothing here touches a real machine. Before you do that, read [Safety](safety.md) and
[Importing your Mlaser setup](importing-mlaser-setup.md).

## Command-line options

| Option | Effect |
|---|---|
| `--demo` | sample document plus the in-process card simulator |
| `--sim`, `--sim-speed N` | connect to the simulator (N times faster than real time) |
| `--real` | connect to a real card at the configured address (untested); `--host`, `--port` override the address for this start, `--ini ipAdd.ini` reads it from the original's file |
| `--laser-off` | dry-run mode: the laser byte of every step is cleared |
| `--lang CODE`, `--import-lang PATH`, `--lang-dir PATH` | language; import translations from your Mlaser install, or read them from it without importing |
| `--fake` | a demo controller without the card protocol |
| `--log-dir DIR` | write the log files there instead of the user log folder |
| `FILE` | a `.mlz`, `.chf`, `.dxf`, `.svg` or `.nc` file to open (a second start hands it to the running window) |

`mlaser --help` lists them all. `python -m mlaser --selftest` runs the network self-test and
reports whether the fast C kernel is in use; the [command-line tools](automation.md)
(`mlaser compile`, `nest`, `validate` ...) run without the window.

## Where mlaser keeps its files

mlaser uses the operating system's usual folders:

| What | Linux | Windows | macOS | Override |
|---|---|---|---|---|
| Settings (`config.json` and its backups) | `~/.config/mlaser/` | `%LOCALAPPDATA%\mlaser\` | `~/Library/Application Support/mlaser/` | `$MLASER_CONFIG_DIR` |
| Data: reports, alarm history, technology database, queue, remnants, backups | `~/.local/share/mlaser/` | `%LOCALAPPDATA%\mlaser\` | `~/Library/Application Support/mlaser/` | `$MLASER_DATA_DIR` |
| Logs (`mlaser.log`, `audit.log`) | `~/.local/state/mlaser/log/` | `%LOCALAPPDATA%\mlaser\Logs\` | `~/Library/Logs/mlaser/` | `--log-dir`, `$MLASER_LOG_DIR` |
| Imported languages | `~/.mlaser/lang/` | `%USERPROFILE%\.mlaser\lang\` | `~/.mlaser/lang/` | `$MLASER_LANG_DIR`, or `lang/` in `$MLASER_DATA_DIR` |

**Tools › Diagnostics › Open log directory** opens the log folder. Deleting the settings folder
resets mlaser to its defaults (the machine parameters included: back them up first, **File ›
Parameters › Back up parameters to a file…**).
