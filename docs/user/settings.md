# Settings

The application's preferences: **Settings…** in the application menu on macOS (⌘,), **Options…**
in the Tools menu on Windows, **Preferences…** in the Edit menu on Linux (Ctrl+, on both). The
command palette finds it as *Settings* (`app.settings`; `app.options` opens the same window).

* **Pages** on the left (a drop-down above the page when the window is narrow); **search** finds a
  setting on any page by its name, its help text or its key.
* Each setting has its help text under the field. **Modified** marks a value that differs from the
  default; **Reset** next to it, or **Reset page**, goes back to the default. A value outside the
  allowed range is refused with the reason under the field.
* **Applying changes.** On macOS a change applies at once (there is only a Close button). On
  Windows and Linux the changes apply with **OK** or **Apply**; **Cancel** discards them. Most
  changes take effect immediately (the canvas, the theme, the language, the nest defaults...);
  the few that need a restart say *Applies after restart*.
* **User modes.** In Operator mode only the operator's preferences (language, units, display)
  can be changed; the other pages are shown read-only. Engineer mode changes everything here.
* **One home per value.** Settings holds application-wide preferences. Values that belong to the
  machine, a material or technology, a layer or one job have their home elsewhere; where Settings
  shows one of them, it is read-only with an **Edit in …** button that opens its home. Safety
  values (interlocks, heartbeat) are never here: they are in the Safety I/O dialog, linked from
  *Machine connection*.
* Unit-bearing values are shown in your display units (Settings › Units); they are always stored in
  millimetres.
* Values are stored in `config.json` in the user configuration folder (see
  [Importing your Mlaser setup](importing-mlaser-setup.md) for the original program's files).
  When you import an original installation, the Options values you had changed there are taken
  into the matching settings once.

The tables below list every setting: its key (for scripts and `config.json`), its default and
what it does. The developer audit of the original Options dialog is in
[docs/dev/settings-audit.md](../dev/settings-audit.md).


## General

Also on this page: the display units (metric or imperial, and per-quantity overrides: the Units service's `units.*` settings). Stored values never change, only what is shown and typed.

| Setting | Key | Default | What it does |
|---|---|---|---|
| Language | `shell.language` | system language | The language of the interface. System language follows the computer's language when mlaser has texts for it, else English. Other languages can be imported from an original Mlaser installation. Operator mode can change it. |
| Open the last drawing at start | `app.reopen_last` | off | At start, open the drawing that was open last. Off by default: a machine should start with an empty drawing unless you ask for one. Operator mode can change it. Applies after restart. |
| Autosave interval | `plugin.ext_project.autosave_interval_s` | 120 s | How often a changed project is saved to the recovery file (0 = never). The recovery file is offered after a crash. |


## Appearance

| Setting | Key | Default | What it does |
|---|---|---|---|
| Theme | `shell.theme` | Light | Light, dark or high contrast (black, white and yellow, for safety glasses). The safety banners keep their own colours in every theme. Operator mode can change it. |
| Classic ribbon | `shell.classic_ribbon` | off | Show the original program's ribbon with every command instead of the task workspaces' toolbars. Operator mode can change it. |


## Canvas

| Setting | Key | Default | What it does |
|---|---|---|---|
| Show part bounding boxes | `display.show_bbox` | off | Draw the bounding rectangle of each part. Operator mode can change it. |
| Show open contours in red | `display.unclosed_red` | on | Open contours cannot be cut with a kerf offset or a lead-in; drawing them in red makes them easy to find before cutting. Operator mode can change it. |
| Show the cutting order numbers | `display.show_index` | on | Number each contour in the order it is cut. Operator mode can change it. |
| Show contour start points | `display.show_start` | on | Mark where each contour starts (where the lead-in pierces). Operator mode can change it. |
| Show the cutting direction | `display.show_direction` | off | Arrows along each contour in the cutting direction. Operator mode can change it. |
| Show rapid moves | `display.show_move_path` | off | Dashed lines for the moves between contours with the laser off. Operator mode can change it. |
| Arrow-key nudge step | `design.nudge_step` | 1 mm | How far the arrow keys move the selection. |
| Nudge step from the selection size | `design.nudge_auto` | on | Derive the arrow-key step from the size of the selection instead of the fixed step. |


## Import & Export

| Setting | Key | Default | What it does |
|---|---|---|---|
| Clean up a drawing when it is opened | `import.cleanup_on_open` | off | Run the clean-up below on every opened DXF, SVG or G-code drawing (one undo step). Off: use Optimize when you need it; the drawing then stays exactly as it was drawn. |
| Remove tiny contours | `import.remove_small` | on | Delete contours smaller than the size below (dust, dimension arrows, points). |
| Tiny contour size | `import.small_gate` | 0.1 mm | Contours whose extent is below this are removed. |
| Remove duplicate lines | `import.remove_overlap` | on | Delete lines drawn twice on top of each other; a double line would be cut twice. |
| Duplicate tolerance | `import.overlap_gate` | 0.1 mm | Lines closer than this count as the same line. |
| Join connected lines | `import.merge_type` | Direction first | Join lines and arcs that touch into contours; the rule decides which neighbour wins at a branch. |
| Join tolerance | `import.connect_gate` | 0.1 mm | Ends closer than this are joined. |
| Smooth into arcs | `import.smooth` | off | Replace runs of short lines by arcs (smoother motion, smaller programs). |
| Smoothing accuracy | `import.smooth_accuracy` | 0.05 mm | The largest deviation of the fitted arcs from the original lines. |
| Close gaps up to | `import.close_gap` | 0 mm | Close open contours whose ends are at most this far apart (0 = never). |
| Read DXF files the way CAD programs draw them (ezdxf) | `plugin.ext_options.dxf_modern` | off | On: HATCH boundaries and entities are read the way CAD programs draw them (ezdxf). Off: the original program's import rules, exactly. |
| Review every drawing after import | `plugin.orders.review_after_import` | on | Show the import review (repairs, recognised parts) for every imported drawing. |
| Join open ends closer than | `plugin.orders.join_tol` | 0.1 mm | The review joins open ends closer than this. |
| Close gaps up to | `plugin.orders.close_gap` | 0.5 mm | The review closes gaps up to this size. |
| Part recognition (nested, layer, block, file, none) | `plugin.orders.recognition` | Outlines with their holes | How the drawing is split into parts before nesting. |
| Feature layers (stay with their part) | `plugin.orders.feature_layers` | ETCH*,MARK*,ENGRAV*,SCRIBE*,TEXT* | Layers whose geometry stays with the part it lies in (engraving, marking). Comma separated, * matches any text. |
| Ignored layers | `plugin.orders.ignore_layers` | BEND*,DIM*,DEFPOINTS,CONSTRUCT*,CENTER*,HIDDEN*,TITLE*,FRAME*,BORDER* | Layers that are never cut (bend lines, dimensions, frames). Comma separated, * matches any text. |
| Islands in holes (keep, parts) | `plugin.orders.islands` | Keep with the part | What happens to contours inside the hole of a part. |
| Part label size | `plugin.ext_labels.label_size` | 62x29 | The label stock for part labels. |
| Remnant label size | `plugin.ext_labels.remnant_size` | 62x29 | The label stock for remnant labels. |
| Job ticket page size | `plugin.ext_labels.ticket_page` | A4 | The paper size of the job ticket. |
| Write a remnant label PDF when a remnant is saved | `plugin.ext_labels.remnant_auto` | off | Write a remnant label PDF when the Run workspace saves a remnant. |
| Marking layer | `plugin.ext_labels.mark_layer` | 10 | The layer (technology) that marks part numbers on the sheet. |
| Smallest marking text height | `plugin.ext_labels.mark_min_height` | 2 mm | Marking text is never smaller than this. |
| Largest marking text height | `plugin.ext_labels.mark_max_height` | 8 mm | Marking text is never larger than this. |
| Marking clearance from edges and holes | `plugin.ext_labels.mark_margin` | 1.5 mm | The clearance of marking text from part edges and holes. |


## CAM tools

| Setting | Key | Default | What it does |
|---|---|---|---|
| Array | `cam.tool.array` | the tool's defaults | What the Array dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Circular array | `cam.tool.circular_array` | the tool's defaults | What the Circular array dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Fill sheet | `cam.tool.sheet_fill` | the tool's defaults | What the Fill sheet dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Fill circles | `cam.tool.fill` | the tool's defaults | What the Fill circles dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Linear fill | `cam.tool.linear_fill` | the tool's defaults | What the Linear fill dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Round corners | `cam.tool.fillet` | the tool's defaults | What the Round corners dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Unload loop | `cam.tool.unload` | the tool's defaults | What the Unload loop dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| α corner | `cam.tool.alpha` | the tool's defaults | What the α corner dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Cooling points | `cam.tool.cool` | the tool's defaults | What the Cooling points dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Rectangles with common edges | `cam.tool.rect_share` | the tool's defaults | What the Rectangles with common edges dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Fly-cut circles | `cam.tool.circle_fly` | the tool's defaults | What the Fly-cut circles dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Fly-cut lines | `cam.tool.line_fly` | the tool's defaults | What the Fly-cut lines dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Bridge | `cam.tool.bridge` | the tool's defaults | What the Bridge dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| One-key planning | `cam.tool.onekey` | the tool's defaults | What the One-key planning dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Engraving scan | `cam.tool.engrave_scan` | the tool's defaults | What the Engraving scan dialog was last run with; it starts from these values next time. Reset gives it its defaults back. |
| Pre-flight: check lead-in collisions | `cam.preflight_lead_check` | on | Before Start or Dry run, check whether a lead-in crosses another part. |
| Pre-flight: check tipping on the slats | `cam.preflight_tip_check` | off | Before Start, check whether freed pieces could tip between the slats. |


## Costing

| Setting | Key | Default | What it does |
|---|---|---|---|
| Currency | `costing.currency` | EUR | The ISO code of the currency (EUR, USD, CHF...) of every cost: the cost card, quotes, the CLI estimate and the production ledger. |
| Machine hour rate | `costing.machine_per_h` | 0 /h | What one machine hour costs (depreciation, operator, overhead). Used by the cost estimate and the ledger. |
| Electricity price | `costing.power_per_kwh` | 0 /kWh | Per kWh, for the energy part of the estimate. |
| Oxygen price | `costing.gas_o2_per_m3` | 0 /m³ | Per m³ at atmospheric pressure. |
| Nitrogen price | `costing.gas_n2_per_m3` | 0 /m³ | Per m³ at atmospheric pressure. |
| Compressed air price | `costing.gas_air_per_m3` | 0 /m³ | Per m³ at atmospheric pressure. |
| Material price (per kg) | `costing.material_per_kg` | 0 /kg | The sheet material for the estimate; the sheet is the job's sheet, else the cut parts' bounding box. |
| Laser wall-plug efficiency | `costing.wall_plug_pct` | 35 % | The laser source's electrical efficiency (an assumption for the energy estimate). |
| Machine power besides the laser | `costing.machine_kw` | 0 kW | Drives, chiller and extraction. |
| Nozzle discharge coefficient | `costing.nozzle_cd` | 0.8 | An assumption for the gas estimate when the technology gives no flow. |
| Laser rated power | `costing.laser_rated_w` | 0 W | Belongs to the machine (0 = from the technology). Read-only here: Edit in Machine › Setup. |
| Gas (per hour) | `costing.gas_per_h` | 0 /h | The ledger's assist-gas cost per hour of cutting. |
| Power (per hour) | `costing.power_per_h` | 0 /h | The ledger's electricity cost per hour of cutting. |
| Wear per metre cut | `costing.per_cut_m` | 0 /m | Nozzle and lens wear per metre of cut. |
| Cost per pierce | `costing.per_pierce` | 0 | Consumable wear per pierce. |
| Material (per m²) | `costing.material_per_m2` | 0 /m² | The ledger's material cost per m² of the used sheet. |


## Run & Queue

| Setting | Key | Default | What it does |
|---|---|---|---|
| Save the leftover of a nested sheet as a remnant | `plugin.run.remnant_auto` | on | After a nested sheet, offer its leftover as a remnant for later jobs. |
| Smallest remnant width | `plugin.run.remnant_min_width` | 50 mm | Leftovers narrower than this are scrap, not remnants. |
| Smallest remnant area | `plugin.run.remnant_min_area` | 40000 mm² | Leftovers smaller than this are scrap. |


## Network & API

| Setting | Key | Default | What it does |
|---|---|---|---|
| LAN monitor enabled | `plugin.run.monitor_enabled` | off | Serve a read-only status page on the network (machine state, job progress). The Run workspace's monitor toggle switches the same setting. |
| LAN monitor interface address | `plugin.run.monitor_host` | 127.0.0.1 | The network interface the monitor listens on (127.0.0.1 = this computer only). |
| LAN monitor port | `plugin.run.monitor_port` | 8765 | The TCP port of the monitor. |
| LAN monitor access token | `plugin.run.monitor_token` | (empty) | The access token the monitor asks for; empty creates one. |
| Automation API on | `plugin.ext_automation.enabled` | off | The HTTP API for MES and scripts (docs: Automation). It never moves the machine; queued jobs wait for the operator. Read-only here: Edit in Automation API settings. |
| Listen on (address) | `plugin.ext_automation.host` | 127.0.0.1 | The address the API listens on (127.0.0.1 = this computer only). Read-only here: Edit in Automation API settings. |
| Port | `plugin.ext_automation.port` | 8766 | The TCP port of the API. Read-only here: Edit in Automation API settings. |
| Accept jobs for the queue (operator approval) | `plugin.ext_automation.queue_intake` | off | Clients may add jobs to the queue; the operator still approves each one. Read-only here: Edit in Automation API settings. |
| Accept uploaded drawings | `plugin.ext_automation.uploads` | on | Clients may upload drawings. Read-only here: Edit in Automation API settings. |
| Job folders | `plugin.ext_automation.job_folders` | (empty) | Folders watched for new jobs (separated by ;). Read-only here: Edit in Automation API settings. |
| Largest upload (MB) | `plugin.ext_automation.max_upload_mb` | 20 | The largest drawing a client may upload. Read-only here: Edit in Automation API settings. |


## Notifications

| Setting | Key | Default | What it does |
|---|---|---|---|
| Send notifications | `plugin.ext_notify.enabled` | off | Send notifications about the shop floor (sheet done, alarms...) to the channels below. Read-only here: Edit in Notification settings. |
| Machine name (title prefix) | `plugin.ext_notify.machine_name` | (empty) | Put in front of every notification title (useful with several machines). Read-only here: Edit in Notification settings. |
| Include simulator, dry-run and practice runs | `plugin.ext_notify.include_simulator` | off | Also notify about simulator, dry-run and practice runs. Read-only here: Edit in Notification settings. |
| ntfy | `plugin.ext_notify.ntfy_enabled` | off | Push notifications through an ntfy server. Read-only here: Edit in Notification settings. |
| ntfy server | `plugin.ext_notify.ntfy_server` | https://ntfy.sh | The ntfy server. Read-only here: Edit in Notification settings. |
| ntfy topic | `plugin.ext_notify.ntfy_topic` | (empty) | The ntfy topic (keep it hard to guess). Read-only here: Edit in Notification settings. |
| E-mail | `plugin.ext_notify.email_enabled` | off | Send notifications by e-mail. Read-only here: Edit in Notification settings. |
| SMTP server | `plugin.ext_notify.smtp_host` | (empty) | The outgoing mail server. The password is kept in the system key store (channels dialog). Read-only here: Edit in Notification settings. |
| SMTP port | `plugin.ext_notify.smtp_port` | 587 | 587 for STARTTLS, 465 for SSL. Read-only here: Edit in Notification settings. |
| Connection security | `plugin.ext_notify.smtp_security` | starttls | How the connection to the mail server is encrypted. Read-only here: Edit in Notification settings. |
| SMTP user (empty: no login) | `plugin.ext_notify.smtp_user` | (empty) | The mail server login (empty: no login). Read-only here: Edit in Notification settings. |
| Sender address | `plugin.ext_notify.email_from` | (empty) | The sender address. Read-only here: Edit in Notification settings. |
| Recipients (comma separated) | `plugin.ext_notify.email_to` | (empty) | Recipients, comma separated. Read-only here: Edit in Notification settings. |
| Webhook | `plugin.ext_notify.webhook_enabled` | off | POST each notification as JSON to a URL. Read-only here: Edit in Notification settings. |
| Webhook URL | `plugin.ext_notify.webhook_url` | (empty) | The webhook URL. Read-only here: Edit in Notification settings. |
| Same notification at most every | `plugin.ext_notify.min_interval_s` | 60 s | The same notification is not repeated more often than this. Read-only here: Edit in Notification settings. |
| At most per hour (all notifications) | `plugin.ext_notify.max_per_hour` | 60 | A cap on all notifications per hour. Read-only here: Edit in Notification settings. |
| Quiet from (HH:MM, empty: off) | `plugin.ext_notify.quiet_start` | (empty) | Quiet hours start (HH:MM; empty: none). Read-only here: Edit in Notification settings. |
| Quiet until (HH:MM) | `plugin.ext_notify.quiet_end` | (empty) | Quiet hours end (HH:MM). Read-only here: Edit in Notification settings. |
| Alarms also during quiet hours | `plugin.ext_notify.quiet_alarms` | on | Alarms are sent during quiet hours too. Read-only here: Edit in Notification settings. |
| Notify: Sheet done | `plugin.ext_notify.event_sheet_done` | on | Notify when a sheet is done. Read-only here: Edit in Notification settings. |
| Channels: Sheet done | `plugin.ext_notify.channels_sheet_done` | all | The channels for this event: all, or a comma separated list of ntfy, email, webhook. Read-only here: Edit in Notification settings. |
| Notify: Queue waits for the next sheet | `plugin.ext_notify.event_queue_waiting` | on | Notify when the queue waits for the next sheet. Read-only here: Edit in Notification settings. |
| Channels: Queue waits for the next sheet | `plugin.ext_notify.channels_queue_waiting` | all | The channels for this event: all, or a comma separated list of ntfy, email, webhook. Read-only here: Edit in Notification settings. |
| Notify: Alarm or fault | `plugin.ext_notify.event_alarm` | on | Notify when an alarm or fault occurs. Read-only here: Edit in Notification settings. |
| Channels: Alarm or fault | `plugin.ext_notify.channels_alarm` | all | The channels for this event: all, or a comma separated list of ntfy, email, webhook. Read-only here: Edit in Notification settings. |
| Notify: Job finished or stopped | `plugin.ext_notify.event_job_finished` | on | Notify when a job finishes or stops. Read-only here: Edit in Notification settings. |
| Channels: Job finished or stopped | `plugin.ext_notify.channels_job_finished` | all | The channels for this event: all, or a comma separated list of ntfy, email, webhook. Read-only here: Edit in Notification settings. |
| Notify: Maintenance due | `plugin.ext_notify.event_maintenance_due` | on | Notify when maintenance is due. Read-only here: Edit in Notification settings. |
| Channels: Maintenance due | `plugin.ext_notify.channels_maintenance_due` | all | The channels for this event: all, or a comma separated list of ntfy, email, webhook. Read-only here: Edit in Notification settings. |


## Plugins

The settings of installed third-party plugins, and a link to the Plugin manager ([Plugins](plugins.md)).


## Users & PINs

The current user mode and which PINs are set, with *Change user mode…* and *Set PINs…* (Service mode). See [The window](shell.md).


## Machine connection

A read-only summary (connection, controller, card address, machine profile, user mode) and links to the Machine workspace, Machine settings, the network addresses and the Safety I/O dialog.


## Advanced

| Setting | Key | Default | What it does |
|---|---|---|---|
| Kerf offset fallback (pyclipper) | `plugin.ext_options.kerf_clipper` | off | When the exact kerf offset of a contour fails, offset it with pyclipper instead of reporting an error. |
| Parity profile | `plugin.ext_options.parity_profile` | original | The motion and import rules. 'original' is bit-exact with the original program (the goldens and the parity runs use it); the others show their warning when chosen. Read-only here: Edit in Parity profile. |


## Where is each setting?

Every value has one home; the Settings window's search also finds values that live elsewhere
and opens their home.

| Kind of value | Its home | In Settings |
|---|---|---|
| Application preferences (language, units, theme, canvas, import defaults, costing, network, notifications, plugins, user modes) | **Settings** | edited here |
| Card and machine parameters (axes, homing, laser, gas, follower, I/O, motion and process rules, edge seek, coordinates, the cutting bed's slats, the machine profile) | **Machine › Setup** (Service mode) | read-only summary on *Machine connection*; search opens Machine › Setup |
| Interlocks and the heartbeat | **Machine › Safety I/O** | never shown; linked |
| Cutting technology (speed, power, gas, pierce, lead and kerf defaults per material) | **Process › Library** | search opens Process |
| Layer values | **Process › Layers** | search opens Process |
| One part's leads, kerf, micro-joints, start point, cooling | **Process › Properties** | not shown |
| One job's nest (spacing, edge, rotation, sheet) and cutting order | **Nest** / **Process** | not shown (the dialogs and panels remember their last values) |
| A one-shot tool's parameters (array, fill, fly-cut...) | the tool's dialog | *CAM tools* shows the last-used values with Reset |
| Notification channels and the automation API | their settings dialogs (with the passwords and tokens) | read-only, with *Edit in …* |

`app.last_file` records the last drawing opened (for *Open the last drawing at start*).
