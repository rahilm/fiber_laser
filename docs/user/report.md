# Report workspace: production, time, cost, alarms

Open it with `workspace.report`.

Every finished cutting job and dry run (completed or stopped) is recorded from the controller's job events:
start and end time, actual and estimated time, cut and rapid length, pierces, sheet size,
material and thickness, utilisation (nested sheets), the queue entry, the alarms during the job
and its cost. Every card alarm and latched fault is recorded with the time it was cleared.
Files: `<data dir>/Report/production.jsonl` and `alarms.jsonl` (the original's
`TotalReport.txt` and the job report pages are still written; **Job report pages...** opens
them).

* **Period**: today, the last 7 or 30 days, or everything.
* **Runs**: *Production only* (default) or *All runs*. Every record says where the job ran
  (column **Run**): **real** (a cut on the machine), **simulator** (the built-in card simulator
  or the demo controller), **dry run** (laser off: the Dry run button, the laser-off mode) or
  **practice** (a [practice lesson](practice.md)). Only real jobs count in the tiles, the chart
  and the tables by default; the line under the tables says how many other runs were left out.
  The device counters (processing hours, which drive the maintenance reminders), the planned
  quantity and the original's `TotalReport.txt` count real jobs only, always. A record written
  before this distinction existed counts as real. A simulator in another program reached over
  the network (`mlaser-cardsim`) looks like a real card and is recorded as real.
* Tiles: jobs (done / all), cutting time, actual vs. estimated time, cut length, pierces, mean
  utilisation, cost, alarms (count and time).
* Chart: cutting time per day; a red mark under a day with stopped jobs.
* Tables: per day, every job, per job name, alarm history (with the remedy).
* **Export CSV...** writes three files (days, jobs, alarms; UTF-8 for spreadsheets; the jobs file has
  a `source` column);
  **Export PDF...** an A4 report of the summary and the tables.

## Cost

The cost of a job comes from the time-and-cost card of the plan when it provides one; otherwise
from the application's rates ([Settings › Costing](settings.md), `costing.*`): machine, gas and
power per hour of cutting, wear per metre cut, per pierce, material per m² of the sheet, and the
currency (the same currency as the cost card).
