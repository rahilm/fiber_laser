# Display units: audit of every site

The user's request: *"The setting for units should be universal (setting to imperial should make
everything in the application imperial)."* This is the list of every place in the application
that shows or takes a length, speed, acceleration, area, pressure, gas flow or mass, with its
status. No site is left unconverted without a written reason. The user guide is
[docs/user/units.md](../user/units.md).

## Rules

* **Internal values never change.** Documents, ConfigService values, the technology database,
  ledger records, the protocol, the planner, the goldens and the parity captures stay in mm,
  mm/s, mm/s², bar, kg, l/min, g/cm³. Only display and input convert
  (`mlaser/services/units.py`, Qt-free; widgets in `mlaser/ui/units_qt.py`).
* **No drift.** A `QuantitySpinBox` keeps the exact internal value it was given; only a user
  edit replaces it, and retyping the text it shows is not an edit. Dialogs write back what
  changed, never a value re-read from display text. Table cells keep the internal value in
  `units_qt.VALUE_ROLE`.
* **Live.** Every converted site relabels when `units.*` changes (`QuantitySpinBox`,
  `QuantityLabel` and `units_qt.on_units_changed`, which dies with its widget).
* **Lower layers.** Packages below `mlaser.services` (machine, cam, report, labels) cannot import
  the units service: they take formatters injected by it (`mlaser.machine.preflight.set_text_format`,
  `mlaser.cam.tipping.SIZE_TEXT`) or a `units=` argument from their UI caller, and their metric
  text is byte-for-byte what it was.
* **Registry parameters** declare their unit in `formats/data/mlaser_params.json`: those in mm,
  mm/s, mm/s² convert everywhere they are shown (`units_qt.spin_for_setting` / `spin_for_unit`,
  `kind_for_unit`). The 896 with no declared unit are shown as stored (reason below).

## Proof that nothing internal changed

* `tools/make_goldens.py --check`: bit-identical (every golden).
* `tests/test_units.py::test_compiled_jobs_identical_in_imperial`: seeded golden jobs compiled
  with the system bound to imperial give the same records.
* `tests/test_units_ui.py::test_edit_in_imperial_stores_mm`,
  `test_untouched_value_never_drifts`, and one "typed in imperial, stored in mm" test per area
  in `tests/test_units_sweep_design.py`, `_shop.py`, `_machine.py`, `tests/test_units_cli.py`
  and `plugins/mlaser-onshape/tests/test_onshape_units.py`.

## Sweep and allowlist

`tests/test_units_sweep.py` opens every workspace (and every tab in it) of the real main window
in imperial and fails on any visible metric unit text in labels, buttons, group titles, spin-box
text and suffixes, combo items, table / tree / list headers and cells, tab titles, tooltips and
window titles; the `_design`, `_shop` and `_machine` sweeps do the same for the dialogs, wizards
and diagnostics windows, and every sweep checks the live switch both ways. The allowlist
(`tests/units_sweep.py`), each with its reason:

| Text | Reason |
|---|---|
| `p/mm`, pulses per mm | the motion card's calibration constant, defined per mm by the card and its parameter files; shown as the card reports it |
| technology record names ("Mild steel 3 mm O2") | data: the key a record is stored and matched under, not a measurement shown for editing (the thickness field converts) |
| combo items that are exactly a unit name | a unit chooser (the `units.*` overrides, `UN.GasPressureUnit`, the storage unit of layer pressures) lists unit names as its choices |
| `ms` | milliseconds are time (never converted) |
| `m` followed by digits (`m1`) | an identifier, not a unit |

Exempt widget subtrees (converted by the parallel **ui-process** track, which owns these files in
this wave; remove the entries from `tests/units_sweep.py` `EXEMPT` when it merges): the technology
editor (`TechnologyEditor`), the layer-parameters dialog (`LayerParamDialog`), and the Properties
panel's Leads, Kerf and Micro-joint sections.

## Counts

| Status | Sites |
|---|---|
| converted | 193 |
| not a unit | 36 |
| left with a written reason | 15 |
| stays mm (JSON / files / data) | 14 |
| allowlisted (data) | 11 |
| not shown / not a display | 9 |
| converted by ui-process | 8 |
| supporting change | 4 |
| new (units API) | 2 |
| **total** | **292** |

## Sites

## Design area (fork F1): canvas, status bar, panels, dialogs, CAM / craft / draw / nest dialogs

| File | Site | Quantity | Kind | Status |
|---|---|---|---|---|
| mlaser/ui/canvas.py | `drawForeground`: top / left rulers (new), tick labels in the display unit, unit in the corner | length | length | converted (nice steps: metric 1-2-5 mm; imperial 1/64 … 1/2, 1, 2, 3, 6, 12 in, 2, 3, 5, 10 ft …; `ruler_step`) |
| mlaser/ui/canvas.py | `drawBackground`: grid at the ruler's major step (`ViewOptions.show_grid`, off by default, no command yet) | length | length | converted |
| mlaser/ui/canvas.py | `ruler_labels` / `ruler_steps`; relabel on a unit change (`on_units_changed`) | length | length | converted |
| mlaser/ui/canvas.py | docstrings "0.5 mm steps", "Model units (mm)" | – | – | not shown (code comments) |
| mlaser/ui/mainwindow.py | status bar cursor readout `st_da` (was `X: {x:.3f} Y: {y:.3f}`, no unit) | length | length | converted, live relabel |
| mlaser/ui/mainwindow.py | status bar machine position `st_xy` | length | length | converted, live relabel |
| mlaser/ui/mainwindow.py | status bar speed `st_cv` (`status.speed`, unit placeholder) | speed | speed | converted, live relabel |
| mlaser/ui/mainwindow.py | Size presets `xform.size_10mm/50mm/100mm/200mm` (labels "10mm"…) | length | length | converted: labels in the display unit; imperial presets are 1/2, 2, 4, 8 in (`SIZE_PRESETS`, `size_preset`) |
| mlaser/ui/mainwindow.py | `paste` offset 10 mm | length | – | not shown (internal constant, no text) |
| mlaser/ui/mainwindow.py | `layer_panel.pressure_unit` getter (UN.GasPressureUnit) | pressure | pressure | supporting change (MPa-stored layer pressures convert correctly) |
| mlaser/ui/mainwindow.py | rotate / scale / simulation-speed ValueDialogs | angle / % / factor | – | not a unit |
| mlaser/ui/panels.py | layer table Cut Speed column (+ header unit) | speed | speed | converted; cells keep the value in VALUE_ROLE, edits parse typed units, an unchanged display keeps the value |
| mlaser/ui/panels.py | layer table Gas Pressure column (+ header unit) | pressure | pressure | converted (×10 when UN.GasPressureUnit = MPa) |
| mlaser/ui/panels.py | layer table Cut Height column (+ header unit) | length | length | converted |
| mlaser/ui/panels.py | layer table Power % / Frequency Hz / Gas type | % / Hz | – | not a unit |
| mlaser/ui/panels.py | part list Size column `W×H` + header "Part size (in)" | length | length | converted, live refresh |
| mlaser/ui/panels.py | jog step presets mp90..mp99 (0.05 … 200, no unit shown) | length | length | converted: labels with unit; imperial presets 0.001 … 7.5 in, each no longer than the metric step it replaces; item data = mm |
| mlaser/ui/panels.py | Backward / Forward length `bf_len` (QDoubleSpinBox, no suffix) | length | length | converted (QuantitySpinBox) |
| mlaser/ui/panels.py | laser power % spin | % | – | not a unit |
| mlaser/ui/dialogs.py | ParamGridDialog float editors of settings in mm, mm/s, mm / s, mm/s2, mm / s2 (115 registry settings) — Options and Hardware dialogs (hw.options), and the grid LayerParamDialog inherits | length / speed / accel | by unit (`units_qt.kind_for`) | converted (QuantitySpinBox, stored unit unchanged; OK writes only changed values) |
| mlaser/ui/dialogs.py | ParamGridDialog int setting in mm (GRP.AutoMicroLinkStep) | length | length | converted (stored as integer mm: a typed 2 in = 50.8 mm is stored as 51) |
| mlaser/ui/dialogs.py | ParamGridDialog label suffix "(mm)" etc. | – | by unit | converted, live relabel |
| mlaser/ui/dialogs.py | 8 bool + 3 enum registry settings tagged "mm" (GRP.IsShow*, ZF.ZFType, GRP.AutoMicroLinkType …) | – | – | unit dropped from the label (a registry artefact on a check box / option list) |
| mlaser/ui/dialogs.py | QR.QRCodeLineDensity (int, tagged "mm") | lines per mm | – | not a length: shown without the misleading "mm", unconverted (`UNIT_NOT_A_QUANTITY`) |
| mlaser/ui/dialogs.py | 896 registry settings with no unit (126 look like lengths / speeds by name, e.g. MAC.FastSpeed, ZF.ZFDockHeight) | ? | – | left: the registry declares no unit; guessing one could misreport a machine parameter |
| mlaser/ui/dialogs.py | ParamGridDialog option "MPa" of UN.GasPressureUnit | – | – | allowlisted: the choice of the unit the layers' gas pressures are STORED in (data semantics, not display) |
| mlaser/ui/dialogs.py | LeadDialog length (in / out) | length | length | converted |
| mlaser/ui/dialogs.py | LeadDialog radius (in / out) | length | length | converted |
| mlaser/ui/dialogs.py | LeadDialog angle | angle | – | not a unit |
| mlaser/ui/dialogs.py | KerfDialog width | kerf | small_length | converted |
| mlaser/ui/dialogs.py | MicroJointDialog distance | length | length | converted |
| mlaser/ui/dialogs.py | MicroJointDialog length | gap | small_length | converted |
| mlaser/ui/dialogs.py | MicroJointDialog min size | length | length | converted |
| mlaser/ui/dialogs.py | LayerParamDialog (subclass of the grid) | – | – | converted by ui-process (its own code); the generic grid conversion applies |
| mlaser/ui/properties/panel.py | Transform X, Y, Width, Height | length | length | converted (exact mm kept; untouched values never drift) |
| mlaser/ui/properties/panel.py | Transform rotate, start %, cooling angle | angle / % | – | not a unit |
| mlaser/ui/properties/panel.py | Leads length / radius (in, out), Kerf width, Micro-joint length | length | – | converted by ui-process |
| mlaser/ui/properties/lead_diagram.py | "1 mm" scale label, lead info text | length | – | converted by ui-process (Leads section) |
| mlaser/ui/ext_cam.py | FieldDialog: new field kinds "len" / "slen" (QuantitySpinBox) | – | – | supporting |
| mlaser/ui/ext_cam.py | Array row / column offset | length | length | converted |
| mlaser/ui/ext_cam.py | Circular array centre X / Y | length | length | converted |
| mlaser/ui/ext_cam.py | Circular array total angle | angle | – | not a unit |
| mlaser/ui/ext_cam.py | Fill sheet width / height / gap / margin | length | length | converted |
| mlaser/ui/ext_cam.py | Linear fill spacing / margin (angle not a unit) | length | length | converted |
| mlaser/ui/ext_cam.py | Fill circle radius / stock / space | length | length | converted |
| mlaser/ui/ext_cam.py | Optimize: smallest length, duplicate limit, combine precision, smooth precision, close gaps | tolerance | small_length | converted |
| mlaser/ui/ext_cam.py | Arc round radius (angles not a unit) | length | length | converted |
| mlaser/ui/ext_cam.py | Unload loop radius | length | length | converted |
| mlaser/ui/ext_cam.py | α corner min edge, outside lead length (max angle not a unit) | length | length | converted |
| mlaser/ui/ext_cam.py | Rectangle common edge over-cut length | length | length | converted |
| mlaser/ui/ext_cam.py | Circle fly cut max fly length (velocity ratio not a unit) | length | length | converted |
| mlaser/ui/ext_cam.py | Line fly cut collinear tolerance | tolerance | small_length | converted |
| mlaser/ui/ext_cam.py | Line fly cut max linker length, max fly length | length | length | converted |
| mlaser/ui/ext_cam.py | log "mf250: cut length … mm" | length | length | converted (formatted at the time of the action) |
| mlaser/ui/ext_craft.py | Bridge width | length | length | converted |
| mlaser/ui/ext_craft.py | Manual micro-joint length | gap | small_length | converted |
| mlaser/ui/ext_craft.py | Gap seal length, overcut length | gap | small_length | converted |
| mlaser/ui/ext_craft.py | Engraving side line length | length | length | converted |
| mlaser/ui/ext_craft.py | logs mf88 point / lead length, mp68 dock point | length | small_length | converted (formatted at the time of the action) |
| mlaser/ui/ext_craft.py | log mf93 start (fraction of the contour) | – | – | not a unit |
| mlaser/ui/ext_draw.py | Text font height | length | length | converted |
| mlaser/ui/ext_draw.py | QR code width | length | length | converted |
| mlaser/ui/ext_draw.py | QR line density (lines per mm, no unit shown) | density | – | not a length (shown as before, no unit text) |
| mlaser/ui/ext_draw.py | Adv text offset | length | length | converted |
| mlaser/ui/ext_draw.py | Fill radius / stock / space | length | length | converted |
| mlaser/ui/ext_draw.py | Merge tolerance | tolerance | small_length | converted |
| mlaser/ui/ext_draw.py | Size dialog width / height | length | length | converted |
| mlaser/ui/ext_draw.py | Fine-tune step ("Step Length(mm)" -> "Step Length") | length | length | converted |
| mlaser/ui/ext_draw.py | Measure result (`view.measure_result`, log + status bar) | length | small_length | converted (3 decimals mm / 4 in) |
| mlaser/ui/ext_nest.py | Nest params part spacing, edge stock, share edge min length | length | length | converted |
| mlaser/ui/ext_nest.py | Nest params kerf, accuracy (curve tolerance) | tolerance | small_length | converted |
| mlaser/ui/ext_nest.py | Nest params raster cell (label "Raster cell (mm)" -> "Raster cell") | length | length | converted |
| mlaser/ui/ext_nest.py | Nest params rotation step, time limit | angle / s | – | not a unit |
| mlaser/ui/ext_nest.py | Std sheet dialog width / height | length | length | converted |
| mlaser/ui/ext_nest.py | Nest panel sheet table W / H columns (+ header units) | length | length | converted; edits parse typed units, unchanged display keeps the value |
| mlaser/ui/ext_nest.py | Result utilisation %, log line | % / s | – | not a unit |
| mlaser/ui/preview_scrubber.py | `preview.status` X / Y | length | length | converted, live relabel |
| mlaser/ui/preview_scrubber.py | cost card cut / travel length (m) | length | long_length | converted (m / ft), live refresh |
| mlaser/ui/preview_scrubber.py | cost card gas volume (m³ + l) | volume | volume | converted (ft³ in imperial) |
| mlaser/ui/preview_scrubber.py | cost card material mass (kg) | mass | mass | converted (lb) |
| mlaser/ui/preview_scrubber.py | cost card time, energy (kWh), peak power (kW), money | – | – | not a unit |
| mlaser/ui/preview_scrubber.py | cost card notes (`cost.note.*` args: nozzle, sheet size, thickness, density) | – | – | formatted by mlaser/services/costing.py (shop fork); on a unit switch the note follows at the next plan |
| mlaser/ui/safety_ui.py | Pre-flight dialog job extent | length | length | converted |
| mlaser/ui/safety_ui.py | Pre-flight dialog travel limits (was `TravelLimits.describe()` in mm) | length | length | converted |
| mlaser/machine/preflight.py | Pre-flight issue messages "job extent X … mm is outside", "target (…) mm is outside the travel" | length | – | outside the Design files: being converted in mlaser/machine/preflight.py by another fork (`TEXT_FORMAT` formatters); not counted here |
| mlaser/ui/safety_io.py | heartbeat Hz, input / output numbers | Hz / – | – | not a unit |
| mlaser/ui/shell/theme.py | high-contrast style sheet `QSpinBox, QDoubleSpinBox` selectors | – | – | supporting: `QAbstractSpinBox` so QuantitySpinBox keeps the border / focus ring |
| mlaser/ui/wizards/settings_import.py | import preview old / new value with the registry unit | by unit | by setting | converted (schema kind; bool / enum / QR density unconverted) |
| mlaser/ui/workspaces/process.py | ProcessWorkspace pick: thickness combo items "3 mm" (data = mm key) | length | length | converted (`_thickness_label`, live relabel) |
| mlaser/ui/workspaces/process.py | TechnologyEditor | – | – | converted by ui-process |
| mlaser/ui/ribbon_spec.py | menu keys "10mm" … "200mm" | length | length | converted at run time (mainwindow relabels the actions) |
| mlaser/ui/help_browser.py | help pages (docs/user/*.md) mention mm | – | – | left: static documentation; docs/user/units.md explains the display units |
| mlaser/ui/ext_options.py, ext_project.py, ribbon.py, plugin_manager.py, import_worker.py, docops.py, shell/__init__.py, topbar.py, palette.py, keymap.py, dialogs.py (shell), contrast.py, workspace_host.py | – | – | – | no length / speed / accel / area / pressure sites |

## Shop floor (fork F2): Nest, Orders, Cutting strategy, Report, Approval, labels, rates, practice

| File | Site | Quantity | Kind | Status |
|---|---|---|---|---|
| mlaser/ui/workspaces/nest.py | `thickness` spin (sheet material thickness) | length | length | converted (QuantitySpinBox) |
| mlaser/ui/workspaces/nest.py | `sheet_w` / `sheet_h` spins (add sheet) | length | length | converted |
| mlaser/ui/workspaces/nest.py | `spacing`, `margin` spins | length | length | converted |
| mlaser/ui/workspaces/nest.py | Options: `share_min` (shortest shared edge) | length | length | converted |
| mlaser/ui/workspaces/nest.py | Options: `kerf` | kerf | small_length | converted |
| mlaser/ui/workspaces/nest.py | Options: `scrap_pitch`, `remnant_min` | length | length | converted |
| mlaser/ui/workspaces/nest.py | `time_limit` spin (s) | time | - | not a unit to convert (seconds) |
| mlaser/ui/workspaces/nest.py | stock list `nestws.sheet_item` (sheet size) | length | length | converted (`size_text`, relabels on switch) |
| mlaser/ui/workspaces/nest.py | stock list remnant items (`Remnant.label()` "W x H mm material t mm") | length | length | converted (`remnant_label` in the UI; the store's own `label()` stays mm: data) |
| mlaser/ui/workspaces/nest.py | results list: sheet size, `nestws.leftover` size | length | length | converted |
| mlaser/ui/workspaces/nest.py | `nestws.sheet_title` (selected sheet size) | length | length | converted |
| mlaser/ui/workspaces/nest.py | compare table "Cut m" column header and values | cut length | long_length | converted (header "Cut (ft)", values in ft) |
| mlaser/ui/workspaces/nest.py | compare table tooltip `nestws.run_tip` (spacing) | length | length | converted |
| mlaser/ui/workspaces/nest.py | status `nestws.msg.no_tech` (thickness) | length | length | converted (both call sites) |
| mlaser/ui/workspaces/nest.py | rotation combo (`15°` etc.), per-part rotation | angle | angle | not converted (degrees) |
| mlaser/ui/workspaces/nest.py | log of nest engine notes / import problems | - | - | not a unit (engine / services texts carry no lengths); services/orders problems: see "outside" below |
| mlaser/ui/workspaces/orders.py | Import review `join_tol`, `close_gap` spins | tolerance | small_length | converted |
| mlaser/ui/workspaces/orders.py | Orders table thickness column (header + cells) | length | length | converted (header "Thickness (in)", cell keeps mm in VALUE_ROLE; typed input parsed with units, retyping the shown text keeps the exact value) |
| mlaser/ui/workspaces/orders.py | material × thickness group combo `orders.group_item` | length | length | converted |
| mlaser/ui/workspaces/orders.py | `orders.msg.nest`, `orders.msg.no_tech`, `orders.tech.pick` | length | length | converted |
| mlaser/ui/workspaces/orders.py | plugin settings `join_tol` / `close_gap` (Setting unit "mm") | tolerance | length (kind_for_unit) | converted by the generic setting renderer (Settings dialog uses `units_qt.spin_for_setting`); stored mm |
| mlaser/ui/workspaces/orders.py | Import review findings list (`f.message` from services/repair.py: "gap 0.200 mm") | length | - | converted at the source (mlaser/services/repair.py uses `units.mm_text`) |
| mlaser/ui/workspaces/orders.py | Parts dialog group labels / summary (`services/parts.py` "W × H mm") | length | - | converted at the source (mlaser/services/parts.py `units.mm_size`) |
| mlaser/ui/workspaces/orders.py | pre-flight material-check messages (`services/material_check.py`) | length | - | converted at the source (mlaser/services/material_check.py) |
| mlaser/ui/workspaces/orders.py | CSV row errors (`services/orders.py` "thickness ... between 0 and 100 mm") | length | - | left: the error is about the CSV file's own thickness column, which is read in mm (see the services/orders.py row in the core section) |
| mlaser/ui/workspaces/cutting_strategy.py | `min_feature` spin | length | length | converted |
| mlaser/ui/workspaces/cutting_strategy.py | `clearance` spin | clearance | small_length | converted |
| mlaser/ui/workspaces/cutting_strategy.py | bed fields `bed.slat_pitch`, `bed.slat_width`, `bed.slat_offset` (special "unknown") | length | length | converted (stored mm in ConfigService; untouched fields never written) |
| mlaser/ui/workspaces/cutting_strategy.py | `cutstrat.bed` / `cutstrat.bed.offset` label | length | length | converted (relabels on switch) |
| mlaser/ui/workspaces/cutting_strategy.py | issue list (`i.message` from mlaser/cam collision / tipping) | length | - | converted at the source (mlaser/cam/tipping.py `SIZE_TEXT`, injected by units; collision messages carry no lengths) |
| mlaser/ui/workspaces/report.py | tile "Cut length" (`12.5 m`) | cut length | long_length | converted (ft) |
| mlaser/ui/workspaces/report.py | tables Per day / Jobs / Per job: "Cut (m)" header + values | cut length | long_length | converted (header "Cut (ft)") |
| mlaser/ui/workspaces/report.py | Jobs table Material column ("S235 3 mm") | thickness | length | converted |
| mlaser/ui/workspaces/report.py | PDF export summary "Cut length" + tables (from the shown table texts) | cut length | long_length | converted |
| mlaser/ui/workspaces/report.py | CSV export (ledger `cut_m`, `width_mm`, `thickness_mm` columns) | - | - | allowlisted: data export, SI with the unit in every column name (documented columns) |
| mlaser/ui/workspaces/report.py | rate settings `per_cut_m` ("/m"), `material_per_m2` ("/m²") | prices | per_long_length / per_large_area | label texts made unit-free ("per length cut", "per area of the used sheet"); the Settings dialog needs `kind_for_unit("/m")`/`("/m²")` (see merge notes); stored per m / per m² |
| mlaser/ui/workspaces/report.py | DayChart "Cutting time per day (h)" | time | - | not a unit to convert |
| mlaser/ui/workspaces/approval.py | approval table Material column (thickness) | length | length | converted (relabels on switch) |
| mlaser/ui/ext_process.py | RatesDialog: `sheet_w_mm`, `sheet_h_mm` | length | length | converted ("auto" special value kept) |
| mlaser/ui/ext_process.py | RatesDialog: `gas_*_per_m3` (€/m³) | price per gas volume | per_volume | converted ("€ 0.120 /ft³"; stored per m³) |
| mlaser/ui/ext_process.py | RatesDialog: `material_per_kg` (€/kg) | price per mass | per_mass | converted (per lb) |
| mlaser/ui/ext_process.py | RatesDialog: `gas_flow_l_min` (l/min) | gas flow | flow | converted (ft³/h) |
| mlaser/ui/ext_process.py | RatesDialog: €/h, €/kWh, W, %, kW, Cd | - | - | not converted (no length/area/pressure/mass) |
| mlaser/ui/ext_process.py | cost card rebuilt on a units switch (notes carry lengths) | - | - | converted (`on_units_changed` -> `update_card`) |
| mlaser/services/costing.py | note args `cost.note.sheet` / `sheet_bbox` (size, thickness, density) | length, density | length / density | converted (args are display text made with `units.current()` at card build; math unchanged) |
| mlaser/services/costing.py | note args `cost.note.gas_orifice` (nozzle diameter) | length | length | converted |
| mlaser/services/costing.py | Line units "m", "m³", "kg" | - | - | displayed by preview_scrubber.CostCardWidget (fork F1, converted there) |
| mlaser/ui/ext_labels.py | plugin settings `mark_min_height`, `mark_max_height`, `mark_margin` (unit "mm") | length | length | converted by the generic setting renderer (`spin_for_setting`); stored mm |
| mlaser/ui/ext_labels.py | part labels / remnant labels / job tickets PDF: passes `units=U.current()` | - | - | converted |
| mlaser/labels/labels.py | printed material + thickness (`_mat`) | length | length | converted (units injected: mlaser.labels sits below services) |
| mlaser/labels/labels.py | printed part size `W x H mm`, remnant size, remnant area `m²` | length, area | length / large_area | converted |
| mlaser/labels/labels.py | label stock names ("62 x 29 mm (roll, Brother DK-11209)", "A4 ... L7163") | - | - | allowlisted: product names of the label stock as sold (the 4 x 6 in roll already says both) |
| mlaser/labels/labels.py | `TicketData.sheet` / `PartLabel.sheet` ("3000 x 1500 mm" from the queue entry) | length | - | left: stored descriptive text of the queue entry (mlaser/services/queue: data) |
| mlaser/labels/marking.py | etch text heights / margins | length | - | not displayed (geometry in mm) |
| mlaser/report/printing.py | legacy report.exe page rows: size, cut length, move length | length | length / long_length | converted when the caller passes `units` (imperial); metric keeps report.exe's text exactly |
| mlaser/report/window.py | legacy ReportWindow table size / cut / move columns; CountFeeDialog totals, their "(m)" captions and prices per m | length | long_length / per_long_length | converted when `units` is passed (imperial); prices typed per ft are converted to per m for `cost()` |
| mlaser/report/window.py | caller `ext_diag.py` (`ReportWindow(self.store)`) | - | - | converted (ext_diag passes `units=U.current()`, also on reopen) |
| mlaser/report/records.py | report.txt / TotalReport / ProcessesStatistic lines ("x mm", "m", "mm") | - | - | allowlisted: the original program's file formats (data) |
| mlaser/report/ledger.py | CSV columns `cut_m`, `width_mm` ... | - | - | allowlisted: data export, SI with the unit in the column name |
| mlaser/ui/ext_practice.py | `practice.step.load.text` ("300 x 200 mm sheet") | length | length | converted (`{sheet}` placeholder; panel refreshes on a switch) |
| mlaser/ui/ext_notify.py | delay / interval spins (" s") | time | - | not converted (seconds) |
| mlaser/ui/ext_automation.py | automation API JSON `thickness_mm`, `position_mm` | - | - | allowlisted: API / wire format stays mm with the unit in the field name |

## Machine, Run, wizards, diagnostics (fork F3)

| File | Site | Quantity | Kind | Status |
|---|---|---|---|---|
| mlaser/ui/workspaces/run.py | `RunScreen.refresh` live values: "Speed {v}" | head speed | speed | converted (`run.val.speed` text takes the formatted value) |
| mlaser/ui/workspaces/run.py | `RunScreen.refresh` live values: "Gas {v}" pressure part | gas pressure | pressure | converted |
| mlaser/ui/workspaces/run.py | `RunScreen.refresh` live values: "Height {v}" | follower height | length | converted (`run.val.height`) |
| mlaser/ui/workspaces/run.py | `RunScreen.refresh` live values: "X {x}  Y {y}  Z {z} {unit}" | head position | length | converted (`run.val.position` gains `{unit}`) |
| mlaser/ui/workspaces/run.py | `RunScreen.refresh` progress / elapsed / ETA | time, % | - | not a unit (ETA unaffected) |
| mlaser/ui/workspaces/run.py | `REASONS["run.reason.breakpoint"]` / `RunWorkspace.reason_text` | breakpoint position | length | converted (`{position}` = "X 1.00, Y 2.00 in") |
| mlaser/ui/workspaces/run.py | `QueuePanel.refresh` material column "{material} {t}" | sheet thickness | length | converted (`thickness_text`); refreshes on a units switch |
| mlaser/ui/workspaces/run.py | `LoadSheetCard.show_card` thickness | sheet thickness | length | converted (`card_in_units`, from the queue entry's mm value) |
| mlaser/ui/workspaces/run.py | `LoadSheetCard.show_card` "Sheet {size}" | sheet size | length | converted (from `sheet_bounds`, mm) |
| mlaser/ui/workspaces/run.py | `LoadSheetCard.show_card` mismatch lines | thickness in the material-check messages | length | converted for display (`mm_text`: "<n> mm" → display units; the audit log and `services/material_check.py` keep mm) |
| mlaser/ui/workspaces/run.py | `LoadSheetCard.show_card` technology line "{material} {t} mm · {gas}" | technology record name | - | allowlisted: technology record names are data (tests/units_sweep.py allowlist) |
| mlaser/ui/workspaces/run.py | `LoadSheetCard` cache key | - | - | reset on a units switch (redrawn at the next refresh) |
| mlaser/ui/workspaces/run.py | `RunWorkspace.monitor_snapshot` "speed", "position" (web monitor page) | speed, position | speed, length | converted (human-readable strings; the snapshot's numeric fields stay mm) |
| mlaser/ui/workspaces/run.py | settings `remnant_min_width` (unit "mm"), `remnant_min_area` (unit "mm²") | remnant thresholds | length, area | stored unit declared; shown through the generic settings renderers (`units_qt.spin_for_setting`, Settings track) |
| mlaser/ui/workspaces/run.py | `RunCanvas` (live view drawing) | - | - | not a unit (pixels; no ruler) |
| mlaser/ui/workspaces/run.py | queue quantity `QSpinBox`, monitor port | count, port | - | not a unit |
| mlaser/ui/workspaces/machine.py | `SettingsPanel._editor` float settings with a unit | hardware parameters (mm, mm/s, mm / s, mm / s2) | from the registry unit | converted (`spin_for_unit` → QuantitySpinBox; value() stays in the registry unit; untouched values never applied) |
| mlaser/ui/workspaces/machine.py | `SettingsPanel._editor` float "p / mm" (A*.PulseEquivalent) | pulse equivalent | - | allowlisted: calibration constant defined per mm by the card |
| mlaser/ui/workspaces/machine.py | `SettingsPanel._editor` int settings (ms, us) | time | - | not a unit to convert |
| mlaser/ui/workspaces/machine.py | `SettingsPanel._editor` int settings tagged "mm" (GRP.AutoMicroLinkStep, QR.QRCodeLineDensity) | registry int | - | left: not in the Machine workspace's hardware sections; integer registry values (a step count, a lines-per-mm density) mis-tagged "mm" by the registry, not lengths |
| mlaser/ui/workspaces/machine.py | `SettingsPanel` row titles "Name (unit)" | unit label | from the registry unit | converted (display unit; relabelled live, edits kept); a convertible unit on a bool/enum (registry mis-tag: GRP.IsShow*, ZF.ZFType) is dropped |
| mlaser/ui/workspaces/machine.py | registry params with no unit (MAC.SoftLimitMaxLen, MAC.FastSpeed, HP*, ...) | lengths / speeds by name | - | left: the registry declares no unit; guessing one could misreport a machine parameter |
| mlaser/ui/workspaces/machine.py | `MachineWorkspace.tick` position "X … Y … Z … {unit}" | head position | length | converted (0.001 mm / 0.0001 in) |
| mlaser/ui/workspaces/machine.py | `MachineWorkspace.tick` tool origins (`ws.machine.origins`) | origins | length | converted (text gains `{unit}`) |
| mlaser/ui/workspaces/machine.py | alarms table duration "{dur:.1f} s" | time | time | not converted (time) |
| mlaser/ui/workspaces/machine.py | connection port `QSpinBox` | port | - | not a unit |
| mlaser/ui/wizards/common.py | `JogPad` step buttons "0.1 / 1 / 10 mm" | jog step | length | converted: imperial offers 0.005 / 0.05 / 0.5 in (0.127 / 1.27 / 12.7 mm); `step_mm` property carries mm to `jog_step` |
| mlaser/ui/wizards/common.py | `JogPad` fast checkbox, continuous jog | - | - | not a unit (the card's jog speeds) |
| mlaser/ui/wizards/common.py | `OutlineView` "Corner ({x}, {y}) {unit}", "Radius {r}" | edge-seek result | length | converted; repaints on a switch |
| mlaser/ui/wizards/common.py | `OutlineView` angle | angle | angle | not converted |
| mlaser/ui/wizards/common.py | `unit_values()` | `{jog_step}`, `{min_gap}`, `{unit}` placeholders | length | added (wizard step texts with a fixed length) |
| mlaser/ui/wizards/base.py | step text, action button, error status | `wizard.comm.jog_x_text/_button`, `jog_y_*`, `wizard.edge.error.too_close` | length | converted (texts take `{jog_step}` / `{min_gap}`); relabel on a switch without re-running a step |
| mlaser/ui/wizards/procedures.py | `EdgeSeekWizard` params form: start X/Y, board X/Y, offset X/Y, follow height | lengths | length | converted (QuantitySpinBox via `spin_for_unit`) |
| mlaser/ui/wizards/procedures.py | `EdgeSeekWizard` params form: out speed | speed | speed | converted |
| mlaser/ui/wizards/procedures.py | `EdgeSeekWizard` params form: X/Y percent | % | - | not a unit |
| mlaser/ui/wizards/procedures.py | `EdgeSeekWizard` point label "P{n} = ({x}, {y}) {unit}" | measured point | length | converted |
| mlaser/ui/wizards/procedures.py | `EdgeSeekWizard` angle texts | angle | angle | not converted |
| mlaser/ui/wizards/procedures.py | `CommissioningWizard` checklist `home_ok`, `jog_moved`, `jog_position` | home position, jog distances | length | converted (`_detail_values`; the report file keeps mm numbers) |
| mlaser/ui/wizards/procedures.py | `CommissioningWizard` `scale_ok` / `scale_mismatch` / `scaling_text` "p/mm" | pulses per mm | - | allowlisted: card calibration constant |
| mlaser/ui/wizards/procedures.py | `LaserWizard` duration (ms), pause (s), duty %, power W | time, %, W | - | not a unit to convert |
| mlaser/ui/wizards/procedures.py | `FollowerWizard` quality text (points, span) | follower curve | - | not converted: raw follower counts |
| mlaser/ui/ext_machine.py | `_apply_edge` log angle | angle | angle | not a unit |
| mlaser/ui/ext_machine.py | other logs (laser type, coordinate system, upgrades, parameters) | - | - | not a unit |
| mlaser/ui/ext_card.py | `CardExtension.params` log "step speed cap" | speed | speed | converted |
| mlaser/ui/ext_card.py | `CardExtension.params` log "pulses/mm", period ms | calibration | - | allowlisted: pulses/mm calibration constant; ms is time |
| mlaser/ui/ext_card.py | `CardExtension.params` log `sc.summary()` (soft limits "…mm", "counts/mm", "p/mm", "mm/s") | card scaling readout | - | left: raw card diagnostics decoded in mlaser/controller/client.py (`CardScaling.summary`, outside this track's files), written once to the log |
| mlaser/ui/ext_devices.py | follower dialog "Follow value" `QDoubleSpinBox`, calibration grid "Height / Raw", plot "height (um)" | follower height | - | left: raw device units of the follower's serial protocol (integers in µm sent as is); a service diagnostic |
| mlaser/ui/ext_devices.py | follower reference `QSpinBox`, laser port, power % | raw / port / % | - | not a unit |
| mlaser/ui/ext_diag.py | `ParamForm` float registry params with a unit (diag dialogs, ball arm, interferometer, burn-in) | registry lengths / speeds | from the registry unit | converted (QuantitySpinBox, label with the display unit, live) |
| mlaser/ui/ext_diag.py | `ParamForm` registry params with no unit (SP.Interfere*, GRP.BallArm*) | by name | - | left: the registry declares no unit |
| mlaser/ui/ext_diag.py | `ParamForm` int params with a unit | registry ints | - | left: unit kept as the registry gives it (no int length params in the diag dialogs) |
| mlaser/ui/ext_diag.py | `ControllerMonitor` axis position | axis position | small_length | converted |
| mlaser/ui/ext_diag.py | `ControllerMonitor` register pages, pulse jog "pulses", "speed (pulses/s)", DA/PWM | card registers | - | allowlisted: raw card diagnostics (pulses, register values) |
| mlaser/ui/ext_diag.py | `DeviceMonitor` AF "StepLen" | autofocus step | small_length | converted |
| mlaser/ui/ext_diag.py | `DeviceMonitor` EC speed / length | jog speed, jog length | speed, length | converted |
| mlaser/ui/ext_diag.py | `MeasureWindow` table X / Y / Z / speed, summary "max speed" | samples | length, speed | converted (headers carry the unit) |
| mlaser/ui/ext_diag.py | `MeasureWindow.save` CSV | samples | - | left: data file stays mm (internal values) |
| mlaser/ui/ext_diag.py | `DeviceReportDialog` X/Y/Z total move length (stored in m) | mileage | long_length | converted (`device_rows`: ft in imperial) |
| mlaser/ui/ext_diag.py | `DeviceReportDialog` times, counts | time, count | - | not a unit |
| mlaser/ui/ext_diag.py | `InterferometerPanel` compensation table (position, measured, deviations, backlash) | pitch errors | small_length | converted (headers with the unit, live) |
| mlaser/ui/ext_diag.py | `InterferometerPanel` "Average backlash" (`cp15` → `pitch.average_backlash`) | backlash | small_length | converted (text takes the formatted value) |
| mlaser/ui/ext_diag.py | `InterferometerPanel` program build (`pulses_per_mm`) | - | - | not a display |
| mlaser/ui/ext_diag.py | ball-arm log "R=…" | radius | length | converted |
| mlaser/ui/ext_diag.py | `MaintainDialog` cycle / warning days | time | - | not a unit |
| mlaser/ui/facade.py | module docstring "step lengths … mm" | - | - | not a display (docstring) |
| mlaser/web/page.py | page labels and the `speed` / `position` slots | - | - | not a unit in itself: shows the strings `run.py` builds (converted there) |
| mlaser/web/snapshot.py, server.py | sheet SVG, JSON | geometry | - | not a display of a unit (SVG in model mm, JSON stays mm) |

## CLI (mlaser/cli) and the Onshape plugin (plugins/mlaser-onshape)

| File | Site | Quantity | Kind | Status |
|---|---|---|---|---|
| mlaser/cli/__init__.py | `_common` / top-level parser: `--units` | option | - | added |
| mlaser/cli/__init__.py | `main`: result envelope | JSON | - | JSON stays mm, `"units": "mm"` added |
| mlaser/cli/__init__.py | `main`: error envelope | JSON | - | JSON stays mm, `"units": "mm"` added |
| mlaser/cli/__init__.py | `_start` (`--start X,Y`) | input length | length | converted: typed units accepted, plain = mm |
| mlaser/cli/__init__.py | `_size` (`--sheet`, `--travel` WxH) | input length | length | converted: typed units accepted (`120inx60in`), plain = mm |
| mlaser/cli/__init__.py | nest `--spacing`, `--margin` | input length | length | converted: typed units accepted, plain = mm |
| mlaser/cli/__init__.py | nest / queue add `--thickness` | input length | length | converted: typed units accepted, plain = mm |
| mlaser/cli/__init__.py | queue add `--sheet` | free text | - | not a unit: a free-text description stored as given (help text only says "free text") |
| mlaser/cli/__init__.py | help texts "in mm" | help | - | kept: they say plain numbers are mm (true) and that a unit may be typed |
| mlaser/cli/common.py | `plan_summary`, `document_summary` (`*_mm`, `bounds`, `size_mm`) | JSON | - | JSON stays mm |
| mlaser/cli/common.py | `out_units`, `set_output_units`, `parse_length`, `JSON_UNITS` | helpers | - | added |
| mlaser/cli/commands.py | compile text: cut / rapid length | length | long_length | converted (m / ft) |
| mlaser/cli/commands.py | compile text: extent | length | length | converted |
| mlaser/cli/commands.py | estimate text: cut length | length | long_length | converted |
| mlaser/cli/commands.py | estimate text: cost lines in m (length.cut, length.travel) | length | long_length | converted |
| mlaser/cli/commands.py | estimate text: cost line m³ (gas.volume) | volume | volume | converted (ft³) |
| mlaser/cli/commands.py | estimate text: cost line kg (material.mass) | mass | mass | converted (lb) |
| mlaser/cli/commands.py | estimate text: s, kWh, kW, currency lines | time / energy / power / money | - | not converted: no imperial counterpart in use (s, kWh, kW) |
| mlaser/cli/commands.py | estimate JSON `length_mm`, cost `lines[].unit` | JSON | - | JSON stays mm (cost lines keep their own units: m, m³, kg, as the cost card) |
| mlaser/cli/commands.py | validate `exceeds_sheet` issue | length | length | text converted; JSON message stays mm, the issue gains numeric `cut_bounds` / `sheet` (mm) |
| mlaser/cli/commands.py | validate: pre-flight issue messages (`out_of_travel`, travel `describe()`) | length | length | converted: the text output rebuilds the pre-flight messages in the output units (`units.messages_in`); the JSON keeps the mm text |
| mlaser/cli/commands.py | validate JSON `travel`, `extent` | JSON | - | JSON stays mm |
| mlaser/cli/commands.py | nest text / JSON (`thickness_mm`, sheet `size`) | text shows no lengths; JSON | - | JSON stays mm |
| mlaser/cli/commands.py | queue add / list (`thickness_mm`) | JSON | - | JSON stays mm; text shows no lengths |
| mlaser/cli/commands.py | `write_frames` header `start_mm` | file format | - | not display: frames file format stays mm |
| mlaser/cli/export.py | DXF / SVG writer (`$INSUNITS` mm, `width="…mm"`) | file format | - | not display: the exported files are mm by format |
| mlaser/cli/plugin.py | plugin manager output | - | - | not a unit (no quantities) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | parts table t faces / t volume / t name cells | thickness | length | converted (number in display units, unit in the header) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | parts table headers of those columns | thickness | length | converted: "t faces (in)", relabels live |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | parts table DXF face label ("Top (+Z) 120 x 80 mm") | length | length | converted at display (`mm_text`) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | parts table Notes / reason (pipeline + geometry texts in mm) | length | length | converted at display (`mm_text`) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | DXF face picker items ("… mm (… mm²)") | length, area | length, area | converted at display (`mm_text`) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | sheet width / height spin boxes | length | length | converted: QuantitySpinBox (values stay mm in the plugin settings) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | Machine tab "Sheet: up to" (max sheet thickness) | length | length | converted: QuantitySpinBox |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | Machine tab "Sheet: width ≥ thickness ×" | ratio | - | not a unit (dimensionless) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | Connection tab thickness tolerance | small length | small_length | converted: QuantitySpinBox |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | Materials tab "Thickness in the name" | length | length | converted (`fmt`, trimmed) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | Materials tab Onshape material names ("Steel 3 mm") | name | - | not converted: the Onshape material name is data (matched against the technology DB as typed) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | group card title (material × thickness) | length | length | converted at display (`mm_text`) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | group card area / box area (m²) | area | large_area | converted (ft²) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | group card sheet size | length | length | converted (`fmt_size`) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | group card "larger than the sheet" list | length | length | converted at display (`mm_text`) |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | status "Loaded <material> <t> mm into Nest" | length | length | converted |
| plugins/mlaser-onshape/mlaser_onshape/ui.py | live switch | - | - | `on_units_changed` -> headers + `refresh_table` (cells, materials, cards) |
| plugins/mlaser-onshape/mlaser_onshape/service.py | `Setting(... unit="mm")` thickness_tol, sheet_w, sheet_h | stored | - | internal: stored mm (the generic settings UI converts via `kind_for_unit`) |
| plugins/mlaser-onshape/mlaser_onshape/pipeline.py | notes, reasons, `GroupSummary.title` (f-strings in mm) | text | - | left mm in the Qt-free pipeline (its strings are data the UI converts with `mm_text`) |
| plugins/mlaser-onshape/mlaser_onshape/geometry.py | face labels, open-loop note | text | - | left mm in the geometry (converted by the UI with `mm_text`) |
| plugins/mlaser-onshape/mlaser_onshape/bom.py | `max_sheet_mm` | stored | - | internal mm |
| plugins/mlaser-onshape/mlaser_onshape/dxfout.py, client.py, materials.py | DXF in mm, API values, thickness parsed from names | data | - | not display |

## Core, services and lower-layer messages (Units track)

| File | Site | Quantity | Kind | Status |
|---|---|---|---|---|
| mlaser/services/units.py | the `units.*` settings, `Units`, kinds, parse, listeners | all | all | new |
| mlaser/ui/units_qt.py | `QuantitySpinBox`, `QuantityLabel`, `on_units_changed`, cell / header / status helpers, `spin_for_setting`, `range_text` | all | all | new |
| mlaser/services/config.py | schema: the `units.*` block registered (`UNITS_SETTINGS`) | – | – | supporting change (Settings track owns the file) |
| mlaser/ui/mainwindow.py | `units.bind(self.config)` | – | – | supporting change |
| mlaser/machine/preflight.py | `TravelLimits.describe`, `out_of_travel` job extent and target texts | length | length | converted (formatters injected by `units`: machine sits below services; metric text unchanged apart from the unit after each range) |
| mlaser/cam/tipping.py | `TipPiece.message` size "(W x H mm)" | length | length | converted (`SIZE_TEXT` formatter injected by `units`; metric text byte-identical) |
| mlaser/services/remnants.py | `Remnant.label()` size and thickness | length | length | converted (`units.mm_size` / `mm_text`: metric text byte-identical) |
| mlaser/services/parts.py | `PartGroup.label()` size | length | length | converted |
| mlaser/services/repair.py | import-review findings: gap, nearest open end, overlap length, tiny segment / object | small length / length | small_length, length | converted |
| mlaser/services/material_check.py | `SheetSpec.label`, mismatch / example-values messages, load card size, technology line, thickness | length | length | converted; the override signature uses the metric text so an override survives a unit switch |
| mlaser/services/practice.py | `practice.step.load.text` default ("{sheet}") | length | length | converted (matches the catalogue) |
| mlaser/services/orders.py | CSV row error "thickness … must be between 0 and 100 mm" | length | length | left: an import error about the CSV file's own column, which is read in mm (the CSV format is documented in mm) |
| mlaser/services/queue.py | queue entry `sheet` free text ("3000 x 1500 mm", a remnant id, or what an intake request sent) | length | – | left: free text stored with the entry (Nest writes it in the units in force when the sheet is queued); labels and tickets print it as stored. Displays that have the numbers (load card, run queue) format from `sheet_bounds` / the entry's mm values |
| mlaser/ui/ext_diag.py | `ReportWindow(self.store, units=…)` (legacy report viewer) | length / area | long_length | converted (units handed in; updated on reopen) |
| mlaser/ui/properties/lead_diagram.py | "1 mm" scale bar, lead info text | length | length | converted by ui-process (Properties > Leads) |
| mlaser/ui/workspaces/process.py | `TechnologyEditor` (all fields, tooltips, pierce table headers, record title) | length / speed / pressure / flow / density | – | converted by ui-process |
| mlaser/ui/dialogs.py | `LayerParamDialog` specifics (the generic grid conversion already reaches it) | – | – | converted by ui-process |
| mlaser/ui/properties/panel.py | Leads / Kerf / Micro-joint sections | length | length, small_length | converted by ui-process |
| mlaser/services/costing.py | cost-card notes are formatted when the card is computed | length / mass | – | follows a switch at the next plan (the card is rebuilt on a units change where the Process plugin holds it: ext_process) |
| mlaser/services/units.py | the original's unit rows `MC.UnitType`, `UN.SpeedUnit`, `UN.AccUnit`, `UN.GasPressureUnit` (ManuPara.xml) | system / speed / accel / pressure | – | converted: mapped onto `units.system` / `units.speed` / `units.accel` / `units.pressure` (`migrate_legacy`: copied when set and the target is not, also after an import), so there is one units setting each; the Settings window hides the original rows (Settings track, S-3). `UN.GasPressureUnit` keeps its storage meaning (layer pressures stored in MPa when 1), which the layer table, costing and technology still read |
| (other tracks) | `InheritedField`, layer inspector, Library, Machine › Setup homes (work items P-3, P-5, R-1, R-5) | all | – | left: not built yet (other tracks); their contract: `units_qt.QuantitySpinBox` editors and `units.current().fmt` source chips; checked by the imperial sweeps once they land |
