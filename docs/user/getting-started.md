# Getting started

## Install

mlaser needs Python 3.12 or 3.13. From a source checkout:

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

The window has a ribbon with the tabs **Start, Common, Process, Draw, System Analysis, Advanced
and Nest**, the layer and part panels on the left, the drawing in the middle, the run controls on
the right, and the log at the bottom. The status bar shows the card, follower and laser states
and the head position.

## Try a job on the simulator

1. Start with `mlaser --demo` (or **Advanced → Hardware → Connect to simulator**).
2. Open a drawing (**File → Open**, `.dxf`, `.svg`, `.chf` or G-code), or keep the demo part.
3. Pick the layer parameters (speed, power, gas) in the layer panel.
4. Press **Sim** to preview the toolpath with the motion planner; the log shows the time estimate.
5. Press **Frame** to trace the outline, then **Start**. The head marker and the red track follow
   the position the simulator reports.

Nothing here touches a real machine. Before you do that, read [Safety](safety.md) and
[Importing your Mlaser setup](importing-mlaser-setup.md).

## Command-line options

| Option | Effect |
|---|---|
| `--demo` | sample document plus the in-process card simulator |
| `--sim`, `--sim-speed N` | connect to the simulator (N times faster than real time) |
| `--real --ini ipAdd.ini` | connect to a real card (untested) |
| `--laser-off` | dry-run mode: the laser byte of every step is cleared |
| `--lang CODE`, `--import-lang PATH` | language; import translations from your Mlaser install |
| `--fake` | a demo controller without the card protocol |

## Where mlaser keeps its files

Settings, logs, reports and imported languages live in the data folder: `~/.mlaser/` by default,
or the folder in `$MLASER_DATA_DIR`. Deleting it resets mlaser to its defaults.
