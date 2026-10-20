# Automation API reference

The automation API is an opt-in local REST + WebSocket server inside the running mlaser
program (`mlaser.api`, plugin `mlaser.ui.ext_automation`). It gives the shop's own tools the
machine status, an event stream and a queue intake whose jobs need the operator's approval.
The user-facing description and the settings are in
[docs/user/automation.md](../user/automation.md); the machine-readable description is served at
`/api/v1/openapi.json` (OpenAPI 3.1, `mlaser/api/openapi.py`).

## No remote motion

The API cannot start, stop, hold, resume, arm, move, home or approve anything. This is a design
property, not a missing feature:

* **The server holds no route to the machine.** `AutomationServer` receives a status dict and
  events that the application publishes, and two callables that write into the intake inbox
  (`IntakeStore.submit`, `store_upload`). It never receives the controller, the window or the
  queue object. The import-linter contract *Automation API and notifications cannot reach the
  machine* forbids `mlaser.api` (and `mlaser.notify`) to import `mlaser.controller`,
  `mlaser.devices`, `mlaser.protocol`, `mlaser.nc.stream`, `mlaser.machine.api` and
  `mlaser.machine.safety`.
* **The only write is queue intake**, and a submitted job is *pending approval*: it lives in
  `<data>/production/pending.json`, never in `queue.json`, so neither the queue runner nor an
  older program version can run it. The operator approves it on the Run screen (Queue tab); the
  API has no approve endpoint. An approved job is an ordinary queue entry: it runs only after
  **Run queue** and **Sheet loaded: start**, through `SafetyUI.start` (arming question,
  pre-flight, the start gate, docs/SAFETY.md §5a).
* **A submission carries no cutting parameters** (`layers`, `technology`, `extra` ... are
  refused with `400 unknown_field`); the layer parameters come from the drawing and the
  machine's layer table.
* Paths that sound like control (`/start`, `/stop`, `/hold`, `/arm`, `/move`, `/approve`, ...)
  answer `403 no_remote_control` with the reason, so an integrator learns it at once.

`tests/safety/test_automation_api.py` runs every method on every path, a WebSocket session and
the submissions against the real application wiring with a spy controller and asserts that not
one motion, arming, output or safety-state method is called, and that a submitted job is not
runnable until the operator approves it.

### Why no remote Stop

A remote **Stop** looks harmless ("stopping is always safe"), and it is the one control an
integrator asks for first. It is deliberately not offered:

* **Stopping belongs to the e-stop chain and the operator.** A person who sees a problem at the
  machine presses the e-stop, which acts in hardware and does not depend on this PC, its
  network or its software (docs/SAFETY.md §11). A remote Stop invites the opposite habit,
  "someone will stop it from the office", which is no safety function: the network, the PC, the
  client or the person can fail without anyone noticing, and nothing in it is rated for safety.
* **A stop is not neutral on a cutting machine.** It leaves a part half cut, a pierce
  unfinished, a breakpoint the operator must resume from, gas running down; the operator at the
  machine decides what happens next. A stop nobody at the machine asked for surprises the person
  standing at it.
* **A stop endpoint is a write endpoint into the machine.** It would need a second token scope,
  its own audit and its own failure modes, and it would be the first route from the network to
  the supervisor. Keeping the API without any such route keeps the analysis simple: nothing the
  network sends reaches the controller.

A shop that wants a remote stop wires one in **hardware**: a stop button in the e-stop chain, or
a safety-rated remote stop device, never a program on the network.

## Transport and authentication

* `http://<host>:<port>/api/v1/...`, default `127.0.0.1:8766`; plain HTTP (keep it on the PC or
  a trusted shop network).
* Token: `Authorization: Bearer <token>` on every request except `/health` and `/openapi.json`
  (missing or wrong: `401`, compared in constant time). REST never reads the token from the
  URL. The WebSocket also accepts `?token=` because browsers cannot set its headers; the log
  masks it.
* Limits (the LAN monitor's, `mlaser.web.server`): socket timeout 10 s, connection lifetime 30 s,
  16 connections at once and 4 per client address (`503`), 5 requests/s per address with bursts
  of 20 (`429`, `Retry-After`). A request body needs `Content-Length` (chunked: `411`) and is
  capped before it is read (`413`): 1 MB for a JSON submission, the upload limit (default
  20 MB, as base64) when uploads are on. At most 4 WebSocket streams (`503`), each closed after
  an hour (close code 1013: reconnect) and pinged every 15 s (no pong within an interval: closed).
* Every response carries `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Content-Security-Policy:
  default-src 'none'`.
* Errors: `{"error": {"code": "...", "message": "..."}}`.

## Resources

### `GET /api/v1/health`

`{"ok": true, "api": "mlaser-automation/1"}`. No token, no data.

### `GET /api/v1/status`

What the program published last (about once a second, on its own thread; a request never waits
for the GUI):

```json
{"state": "running", "reason": "Cutting", "mode": "process", "armed": true, "laser_enabled": true,
 "simulator": false, "source": "real", "progress": 0.42, "elapsed_s": 312.0, "eta_s": 431.5,
 "part": 7, "parts": 24, "contour": 19, "contours": 61, "position_mm": [812.4, 233.1, 0.0],
 "speed_mm_s": 41.7, "breakpoint": false, "job": "nest_sheet1.chf",
 "alarms": [], "queue": [{"id": "4f1c...", "name": "nest #1", "quantity": 2, "done": 0,
 "status": "running", "material": "Mild steel S235", "thickness_mm": 3.0, "sheet": "3000 x 1500 mm",
 "est_s": 743.5, "actual_s": 0.0, "source": "api"}],
 "queue_totals": {"entries": 1, "sheets": 2, "done": 0, "remaining": 2, "remaining_s": 1487.0},
 "queue_runner": {"state": "running", "current": {"id": "4f1c...", "...": "..."}},
 "pending_approval": [], "queue_intake": true, "remote_control": false, "published": 1792458091.79}
```

`state` is `offline`, `ready`, `running`, `hold`, `alarm` or `fault` (the Run screen's state);
`source` says whether the program drives the real machine, the simulator, a dry run or a practice
lesson. `remote_control` is always `false`.

### `GET /api/v1/queue`

`{"entries": [...], "pending_approval": [...], "queue_intake": true, "published": ...}`; a pending
job is `{"id", "name", "file", "quantity", "material", "thickness_mm", "sheet", "note",
"reference", "source", "client", "submitted", "status": "pending_approval"}` (the file name only,
never the local path).

### `GET /api/v1/events?after=<seq>&limit=<n>&wait=<s>`

Events with a sequence number greater than `after` (at most `limit`, 100 by default, 500 at
most). With `wait` (up to 20 s) the request waits for the next event (long poll). Answer:
`{"events": [...], "next": <seq to pass as after>, "lost": <events dropped before you read them>}`.
The server keeps the last 1000 events; the sequence restarts with the program.

| `type` | fields |
|---|---|
| `job_started` | `mode` (`process` / `dry_run` / `frame`), `resume`, `source` (`real` / `simulator` / `dry_run` / `practice`), `job` |
| `job_finished` | `mode`, `ok`, `reason` (`done`, `stopped`, `paused`, `fault: ...`), `duration_s`, `source`, `job` |
| `alarm`, `alarm_cleared` | `alarms`: `[{"code", "text"}]` |
| `state` | `old`, `new` (safety state), `reason`, `fault` |
| `armed`, `disarmed` | `reason` (disarmed) |
| `breakpoint` | `active` |
| `queue_changed` | `entries`, `sheets`, `done`, `remaining`, `remaining_s` |
| `sheet_done` | `entry`, `ok` |
| `queue_waiting` | `next` (the entry whose sheet the operator should load), `message` |
| `intake_submitted` | `job` (the pending job) |
| `intake_approved`, `intake_rejected` | `id` |

Every event has `seq`, `t` (Unix time) and `type`.

### `GET /api/v1/ws` (WebSocket)

RFC 6455. The first message is `{"type": "hello", "seq": <last seq>, "status": {...}}`; then one
event per text frame, as above (and `{"type": "lost", "events": n}` when the client fell behind
the buffer). The stream is read-only: text and binary frames from the client are ignored, an
unmasked or oversized client frame closes it (1002 / 1009). Reconnect after a close and continue
with `GET /events?after=<last seq>` to fill the gap.

### `POST /api/v1/queue`

Submit a job; answer `202` with the pending job. Requires *Accept jobs for the queue* (else
`403 queue_intake_off`). Body (`Content-Type: application/json`), either an upload:

```json
{"filename": "bracket.dxf", "content_base64": "MAoqU0VDVElPTgo...", "quantity": 4,
 "name": "bracket", "material": "S235", "thickness_mm": 3, "sheet": "3000 x 1500",
 "note": "for order 4711", "reference": "SO-4711"}
```

or a drawing in one of the configured job folders: `{"path": "/orders/4711/bracket.dxf", ...}`.

| Field | Rule |
|---|---|
| `path` / `filename` | `.chf`, `.dxf` or `.svg` (the queue's formats); a path must lie inside a job folder after resolving links (`403 forbidden_path`) and exist (`404 not_found`) |
| `content_base64` | the drawing, at most the upload limit (`413 too_large`); uploads must be on (`403 upload_off`) |
| `quantity` | 1 .. 9999 sheets |
| `thickness_mm` | 0 .. 200 |
| `name`, `material`, `sheet`, `note`, `reference` | text, control characters removed, at most 120 / 80 / 80 / 500 / 120 characters |

Any other field is refused (`400 unknown_field`). At most 100 jobs wait for approval at a time
(`429 full`). Every submission, approval and rejection is in the audit log (`intake_submitted`,
`intake_approved`, `intake_rejected`).

## Examples

```sh
TOKEN=...   # Automation API settings > Copy
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/api/v1/status | jq .state
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8766/api/v1/events?after=0&wait=20"
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d "{\"filename\": \"bracket.dxf\", \"content_base64\": \"$(base64 -w0 bracket.dxf)\", \"quantity\": 2}" \
     http://127.0.0.1:8766/api/v1/queue
```

```python
import json, urllib.request
req = urllib.request.Request("http://127.0.0.1:8766/api/v1/events?after=0&wait=20",
                             headers={"Authorization": "Bearer " + TOKEN})
after = 0
while True:
    with urllib.request.urlopen(req.full_url.replace("after=0", f"after={after}"), timeout=30) as f:
        body = json.load(f)
    for ev in body["events"]:
        if ev["type"] == "queue_waiting":
            print("load the next sheet:", ev["next"]["name"])
    after = body["next"]
```

## For contributors

* New read-only data goes into the status dict (`AutomationPlugin.status_dict`) or an event
  (`publish_event`); both are built by the application, the server only serves them.
* Never pass the controller, the window, a `MachineAPI` view or a queue object into
  `mlaser.api`. A new write needs a design review against docs/SAFETY.md; it must go through the
  intake (operator approval), never around it.
* Tests: `tests/safety/test_automation_api.py` (auth, read-only, approval, no motion, limits) and
  `tests/test_intake.py`.
