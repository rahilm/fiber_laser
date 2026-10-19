# Report workspace: production, time, cost, alarms

Open it with `workspace.report`.

Every finished cutting job (completed or stopped) is recorded from the controller's job events:
start and end time, actual and estimated time, cut and rapid length, pierces, sheet size,
material and thickness, utilisation (nested sheets), the queue entry, the alarms during the job
and its cost. Every card alarm and latched fault is recorded with the time it was cleared.
Files: `<data dir>/Report/production.jsonl` and `alarms.jsonl` (the original's
`TotalReport.txt` and the job report pages are still written; **Job report pages...** opens
them).

* **Period**: today, the last 7 or 30 days, or everything.
* Tiles: jobs (done / all), cutting time, actual vs. estimated time, cut length, pierces, mean
  utilisation, cost, alarms (count and time).
* Chart: cutting time per day; a red mark under a day with stopped jobs.
* Tables: per day, every job, per job name, alarm history (with the remedy).
* **Export CSV...** writes three files (days, jobs, alarms; UTF-8 for spreadsheets);
  **Export PDF...** an A4 report of the summary and the tables.

## Cost

The cost of a job comes from the time-and-cost card of the plan when it provides one; otherwise
from the rates in the settings `plugin.report.*`: machine, gas and power per hour of cutting,
wear per metre cut, per pierce, material per m² of the sheet, and the currency.
