# Automation: command line and automation API

mlaser can be used from scripts and from the shop's own tools, without anyone clicking through
the program:

* the **command line** (`mlaser compile`, `estimate`, `nest`, `validate`, `convert`,
  `queue add`) works on files: no window, no machine connection;
* the **automation API** is an optional local web service of the running program: status and
  events for an ERP, a website, Home Assistant or a stack light, and an intake for new jobs.

Both are built so that **nothing outside the machine can start it**. They have no way to start,
stop, hold, arm or move the machine. A job that a script or the API submits waits for the
operator's **approval** on the Run screen. Even an approved job runs only after the operator
loads the sheet and presses **Sheet loaded: start**, through the normal Start: arming question,
pre-flight check, start gate ([Safety](safety.md), [docs/SAFETY.md](../SAFETY.md)).

## Command line

The commands use the same functions as the program: the same file import, the same technology
database and **Apply**, the same compiler (the job Start would stream), the same nesting and the
same pre-flight checks. The CLI never opens a connection to a card.

```
mlaser compile  DRAWING [--tech MAT/THICK[/GAS[/NOZZLE]]] [--frames OUT.jsonl] [--start X,Y] [--dry-run] [--mode frame]
mlaser estimate DRAWING [--tech ...] [--rates RATES.json]
mlaser nest     FILE[:QTY] ... | PARTS.csv  --sheet WxH [--sheets N] [--spacing 3] [--margin 5]
                [--rotation 90] [--time-limit 30] [--tech ...] [--out DIR] [--format mlz|chf|dxf|svg]
mlaser validate DRAWING [--tech ...] [--travel WxH] [--sheet WxH] [--dry-run]
mlaser convert  IN OUT                      (.dxf .svg .chf .mlz, by extension)
mlaser queue add  DRAWING [-n SHEETS] [--name] [--material] [--thickness] [--sheet] [--note] [--reference]
mlaser queue list
```

`mlaser` is the program itself: these words in the first position run the command instead of
the window (`python -m mlaser ...` and `mlaser-cli ...` do the same). Add `--json` to any
command for one JSON object on standard output, for scripts. Options common to all commands:
`--config PATH` (another `config.json`), `--tech-db PATH` (another technology database);
`MLASER_DATA_DIR` / `MLASER_CONFIG_DIR` select the data and configuration directories.

### What the commands do

| Command | Result |
|---|---|
| `compile` | the planned job: mode, time (card time and the planner's estimate), pierces, cut and rapid length, extent, wire items and frames; with `--frames` the card frames, one JSON array per line after a header line `{"format": "mlaser-frames/1", ...}` |
| `estimate` | time per kind (cut, travel, pierce / process), lengths, pierces and the cost card of the Process workspace. Rates: `--rates` (a JSON file with the fields of *Rates and assumptions*: `machine_per_h`, `power_per_kwh`, `gas_per_m3` `{"O2", "N2", "Air"}`, `material_per_kg`, `machine_kw`, `currency` ...) or the rates saved in the program. A rate that is not set shows as *input needed*; the total is then `null`, never a misleading 0 |
| `nest` | parts onto sheets with the program's nester: placed and unplaced per part, utilisation per sheet; `--out DIR` writes one file per used sheet and `<name>.json`. `.mlz` (default) keeps the layer parameters of `--tech`; `.chf` (the queue's format) has no layer table, the machine's layer table supplies it |
| `validate` | the checks of the pre-flight that do not need a machine: layers without cutting parameters, geometry (NaN, zero length, open contours), the job's exact extent against the travel (`--travel`, else the machine parameters), and with `--sheet` whether the cut fits on that sheet. Homing, arming, alarms, interlocks and the head position are listed as *not checked*: the program checks them at Start |
| `convert` | `.dxf` / `.svg` / `.chf` / `.mlz` into each other. DXF and SVG get the geometry only (one DXF layer per process layer); `.chf` and `.mlz` keep leads, kerf and joints, `.mlz` also the layer parameters |
| `queue add` | submits a job for approval (see below); a `.mlz` is converted to a `.chf` for the queue |
| `queue list` | the queue and the jobs waiting for approval (read-only) |
| `plugin ...` | the Plugin manager: `list`, `search`, `info`, `install`, `uninstall`, `update`, `enable`, `disable`, `hosts`, `repo add/remove/list` ([Plugins](plugins.md#command-line)) |

`--tech` picks a record of the technology database like Process > Apply: material (any unique
part of its name), thickness, and the gas and nozzle when needed to make it unique, e.g.
`"S235/3/O2"`. An ambiguous choice is an error that lists the candidates. A parts list CSV is the
same format as the Nest workspace's Orders tab ([Import](import.md#orders-several-files-and-parts-lists):
`file`, `quantity`, `material`, `thickness`, `customer`, `order`, `due`, `priority`, header
names matched loosely, `,` `;` or tabs, UTF-8 or Windows-1252, relative files against the CSV);
the CLI also reads `name`, `rotation` (allowed step in degrees, 0 = none) and `mirror`
(yes/no). A refused row is an error (exit 3) that names its line.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | done, and the answer is "no": `validate` found errors, `nest` left parts unplaced |
| 2 | usage: unknown command or option, a malformed value (`--sheet axb`, `--tech S235`) |
| 3 | an input file is missing, unreadable or of an unsupported type |
| 4 | the work failed: compile or nest error, unknown or ambiguous technology |
| 5 | an output file could not be written |
| 6 | `queue add` refused (the approval list is full); `plugin` refused (a verification check, https only, `--yes` missing) |

With `--json` an error is `{"ok": false, "exit_code": N, "error": {"code": "...", "message": "..."}}`.

### Examples

```
mlaser estimate bracket.dxf --tech "S235/3/O2" --json | jq .time.total_s
mlaser nest order.csv --sheet 3000x1500 --tech "1.4301/2" --out sheets/ --json
mlaser validate sheets/nest_sheet1.mlz --travel 3000x1500 || echo "not ready to cut"
mlaser queue add sheets/nest_sheet1.mlz -n 2 --material "S235" --thickness 3 --reference "SO-4711"
```

## Jobs from outside wait for approval

`mlaser queue add` and the API's queue intake drop the job into an **inbox**
(`<data>/production/inbox`). The running program picks it up within a second; a job submitted
while the program is closed appears at its next start. On the **Run** screen the **Queue** tab
then shows a list *submitted from outside* above the queue, with where it came from (command
line user, API client address), the file, quantity, material and the note, and the Run screen's
queue card says how many wait.

* **Approve** (after a confirmation) adds the job to the queue as an ordinary entry. It does not
  start: running the queue still needs the operator's **Run queue** and **Sheet loaded: start**
  for every sheet.
* **Reject** removes it (an uploaded drawing is deleted).

A submission can carry only the drawing, a name, the quantity, material, thickness, a sheet
description, a note and a reference. It carries **no cutting parameters**: those come from the
drawing and the machine's layer table, as for a job the operator adds. Waiting jobs are kept in
`pending.json`, separately from the queue file; at most 100 wait at a time.

## Automation API

A local web service of the running program, **off by default**. Turn it on with the command
**Automation API on/off** (`api.toggle`, Engineer mode); a warning explains what it shares
before it starts. **Automation API settings** (`api.settings`) sets:

| Setting | Default | |
|---|---|---|
| Listen on | `127.0.0.1` | only programs on this PC can connect; the machine PC's LAN address opens it to the network |
| Port | 8766 | |
| Token | generated | the key a program needs (*Show*, *Copy*, *New token*: old programs stop working) |
| Accept jobs for the queue | off | the queue intake; every job waits for approval |
| Accept uploaded drawings | on | jobs may send the drawing itself (up to 20 MB) |
| Job folders | (none) | folders a job may name by path instead (e.g. a shared order folder), `;`-separated |

While it runs, the Run screen shows **Automation API ON** with the address.

What a program can do with it:

* read the **status**: machine state and reason, armed or not, progress and ETA, the job, alarms
  with their codes, the queue and the jobs waiting for approval (`GET /api/v1/status`);
* follow the **events**: job started / finished (with whether it ran on the real machine or the
  simulator), alarm raised / cleared, state changes, sheet done, queue waiting for the next sheet,
  submissions approved or rejected (`GET /api/v1/events`, or pushed over a WebSocket at
  `/api/v1/ws`);
* **submit a job** for approval (`POST /api/v1/queue`).

That is all. There is no request that starts, stops, holds, resumes, arms, moves, homes or
approves anything, and asking for one gets a clear refusal. Stopping the machine is the e-stop
and the operator's job, never a network message's. The developer reference with the request
formats and examples is [docs/dev/API.md](../dev/API.md).

**Security.** The API is plain HTTP without encryption. Keep it on `127.0.0.1` unless the shop
network is trusted, never forward its port to the internet, and give the token only to the
programs that need it. Its limits (connections, requests per second, sizes, time-outs) are the
same as the [LAN monitor](lan_monitor.md)'s, so a misbehaving client cannot slow the program
down.

## See also

[Notifications](notifications.md) (send-only messages to a phone or a mailbox),
[Run](run.md) (the queue), [Report](report.md) (production figures: simulator and practice
runs are not counted), [Labels](labels.md).
