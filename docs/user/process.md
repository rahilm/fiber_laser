# Process: technology database, properties, preview and cost

This page covers the Process features of mlaser (Wave 3): the material/technology database, the
Process workspace, the Properties panel, and the preview with its time and cost estimate.

> **The example technologies are examples.** mlaser ships a small set (mild steel with oxygen,
> stainless steel and aluminium with nitrogen, 1–6 mm, 1.5 kW). They are marked
> **EXAMPLE VALUES: verify on your machine before cutting** everywhere they appear. They were
> not tested on any machine. Cut test pieces, tune the values, then save them as your own
> version and tick *Tested on this machine*.

## 1. Technology database

A *technology* is everything one layer needs to cut one kind of sheet. It is identified by:

| Key | Example |
|---|---|
| material | `Mild steel S235` |
| thickness | `3 mm` |
| gas | `O2`, `N2`, `Air` |
| nozzle | `1.0 double` (a number in the name is used as the bore diameter for gas estimates) |
| laser power class (optional) | `1.5 kW` |

A technology holds:

* **Cut values**: speed (mm/s), laser power (peak current, %), duty (%), frequency (Hz), gas, gas
  pressure (bar), cut height (mm) and focus (mm).
* **Pierce**: the process type (direct cut, fixed height, one-step or two-step pierce) and the
  values of each stage: height, focus, power, duty, frequency, gas, pressure and time. Stages
  run from the last to the first, then the cut starts.
* **Start and delays**: slow start, laser on/off delays, lift height.
* **All parameters**: every other layer parameter of the original program (164 in total),
  so a technology loses nothing a layer can hold.
* **Craft defaults**: lead-in (type, length, angle, radius), kerf compensation and width,
  micro-joints per part and joint length.
* **Rules**: *small features*, holes smaller than a size get a shorter lead and no micro-joints;
  *corners*, cooling points at corners sharper than an angle; *lead placement* and *tipping*
  (see [Cutting strategy](cutting-strategy.md)).
* Optional **gas flow** at the cut pressure (l/min) and **material density** (g/cm³), used by
  the cost estimate. Notes, and a *Tested on this machine* mark.

Every field shows its unit, and the editor refuses values outside the range of the original
program's parameter table (hover a field to see its range and parameter name).

The records are edited on the **Library** page of the Process workspace (the switch *Drawing |
Library* at the top). The Library takes the whole width of the workspace (the drawing makes
room and comes back with *Drawing*): the record list is beside the editor when the page is wide
enough, above it when not, and the editor's sections sit in as many columns as fit. The form
fits without scrolling: sections fold (each folded one shows its values on one line) when the
room runs out. *Show all parameters* and the search box list every matching parameter, and that
list scrolls.

### Where it is stored

There is one database per machine profile, in the mlaser data directory
(`technology/<profile>.sqlite3`; on Linux `~/.local/share/mlaser/`). The profile name is the
setting *Machine profile (technology database)*. The example set is added to a new database
once. If you delete examples, they are not added again.

### Versions, "what changed", revert

* **Save version** stores your edits as a new version, with an optional comment. Nothing is
  overwritten: the *History* tab lists every version, and *Restore this version* makes an old
  version current again (as a new version).
* **What changed** compares:
  * *Technology vs factory values*: the current version against the version that was shipped
    or imported (for a technology you created, against the defaults);
  * *Layer vs its technology*: what was changed on a layer after the technology was applied;
  * *Unsaved edits*: the editor against the stored version.
* **Revert to factory** goes back to the shipped or imported values (again as a new version).
* **Save as new…** copies the technology under a new name (your own record, not an example).

### Import and export

* **JSON** (`mlaser-technology/1`): export all technologies, import them on another machine.
  Imported records keep their version history from the import on.
* **The original program's craft and `LayerPara.xml` files**: every layer that the file
  defines becomes one technology with all of its values. The material is the file name, the
  thickness the first number in it, and the gas comes from the layer's gas type; rename them
  afterwards. Export to `LayerPara.xml` writes the selected technology as layer 1.

## 2. Process workspace

Open it with **Process+ → Process** (or the *Process* tab next to *Layer parameters*).

1. Choose **Material**, **Thickness**, **Gas** and **Nozzle**. The lists only offer
   combinations that exist.
2. Choose **Apply to**: one layer, or all layers in use.
3. Choose what else to apply: **Leads**, **Kerf**, **Micro-joints**, **Rules**.
4. Press **Apply technology**. This is one undo step (Ctrl+Z restores the layers and parts).

Applying a technology sets the layer's parameters exactly as if you had typed them into the
*Layer parameters* dialog. Parameters the technology does not set get the original program's
defaults. Values from an earlier `LayerPara.xml` do not remain.

The **Layers** list shows each layer in use with its technology and its state:

* *As applied*: unchanged since the technology was applied;
* *Modified: n fields*: edited afterwards (see *What changed → Layer vs its technology*);
* *example values*: from the example set;
* *version n available*: the technology was saved again after it was applied, so apply it
  again to use the new values;
* *Set by hand* / *Not defined*: no technology. A layer that is not defined cannot be cut; the
  pre-flight check refuses it.

**Save layer as technology…** stores a layer you tuned by hand as a new technology.

The tab **Cutting strategy** (next to *Technology*, *What changed* and *History*) works on the
drawing (details in [Cutting strategy](cutting-strategy.md)):

* **Lead-ins**: *Check* lists the lead-ins that reach another contour, cross their own contour or
  pierce at the sheet edge, and draws them red on the canvas; *Fix* moves their start or shortens
  them (the original's 0.7 rule); *Place starts* puts every start on a corner or a long straight
  segment by the rules (start on, smallest feature, clearance).
* **Tipping on the slats**: the bed (shown; edited in Machine › Setup › Bed), the strategy of the
  layer's recipe (micro-joint on the heavy side, cut last, rapids around; shown), *Check* and *Fix*.

Each change is one undo step. The pre-flight check of a Start warns about colliding lead-ins
(and, when turned on, about pieces that can tip).

The rules (start on, smallest feature, clearance, tipping strategy) are the recipe's: edit them
in **Library**, section *Cutting strategy*. The tab shows them with a link there.

## 3. Properties panel

The *Properties* panel follows the selection on the canvas. Changes preview live on the canvas
(the dashed outline is the toolpath the change will produce, computed by the same CAM code as
the job) and are applied when you confirm the field (Enter, Tab or leaving it, choosing from a
list, or a button). Each applied change is one undo step.

| Section | Fields |
|---|---|
| Transform | X / Y of the lower-left corner (mm), width / height (mm, they scale together so arcs stay arcs), rotate by (°) |
| Layer | the layer of the selection, its technology, a link to the layer's values (*Edit in Process › Layers*) |
| Leads | lead in and lead out: type, then the sizes that type uses (a line: angle and length; line + arc: the radius too), with a live diagram of the lead on the scrap side |
| Kerf | compensation (none / inside / outside) and width (mm) |
| Start point | position along the contour (%) or *Automatic (long side)* |
| Micro-joints | joints per part and length (mm): *Apply* / *Clear* |
| Overcut and gap | length (mm): *Gap*, *Overcut*, *Seal* |
| Cooling points | at corners sharper than an angle (°), or *Clear* |

Under each value, where it comes from: *from Mild steel S235 3 mm* (the recipe), *from layer 2*,
or *set on this part* with a restore button. Click the source to edit it there.

**It fits the panel.** When the panel is short, the sections you use least fold first (leads and
kerf stay open longest); a folded section shows its values on one line (*Leads: in: Line 3 mm ·
out: None*). Click a title to open it: the one you opened stays open, others fold to make room.
Titles too long for a narrow panel end in "…" (the whole title is in the tooltip).

**One contour of a part.** A part with several contours shows *Applies to*: choose a contour
(*Contour 2 (hole)*) to give it its own lead, kerf or start point; the other contours keep the
part's values. *Whole part* goes back to the part.

**In Design** the panel shows *Transform* and *Layer* only; the cut settings are edited in the
Process workspace (one place for each setting; the link takes you there).

## 4. Preview and estimate

The **Preview** panel plays the job that **Start** will send to the card. It is the same
planned job, not a separate simulation, so the times are those of the job itself.

* the slider is the job's timeline; drag it, or press **▶** to play (speed ×1 to ×50);
* **|◀ / ▶|** jump to the previous / next contour; *Contour* jumps to contour *n*;
* on the canvas, amber is the cut so far, dashed blue the rapids, small rings the pierces; the
  head marker is filled red while the beam is on;
* the status line shows what the machine is doing (cutting, rapid, piercing…), the laser state,
  the contour and the head position;
* **Clear** removes the preview from the canvas.

The **Estimate** card (bottom of the Process workspace) lists:

| Line | Source |
|---|---|
| total, cutting, rapid, pierce and process time; pierces; cut and rapid length | the planned job |
| gas (m³) | the technology's gas flow, else an estimate from the nozzle bore and the pressure (orifice flow with an assumed discharge coefficient). If none is known the card asks for the flow |
| laser and machine energy (kWh), electrical power while cutting (kW) | rated laser power × power % ÷ wall-plug efficiency (an assumption, 35 % by default), plus the machine's own draw |
| material (kg) | the bounding box of the cut × thickness × density |
| costs | machine hour rate, gas, electricity and material prices |

Lines marked **≈** are estimates; lines with **⚠** need an input. A price of 0 counts as *not
set*: the card says so instead of showing 0. Set prices and assumptions in
[Settings › Costing](settings.md) (**Rates and assumptions…** opens that page); the Report
ledger uses the same rates and currency.
