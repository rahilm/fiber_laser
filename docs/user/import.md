# Import: review and repair, part recognition, orders

Customer drawings are the shop's raw material, and they are rarely clean. This page covers what
happens after a drawing is opened: the **import review** that finds and fixes problems, **part
recognition** that turns contours into real parts for nesting, and **order intake** from parts
lists. The import itself is unchanged: by default it builds exactly what the original Mlaser
builds (one object per contour, see [Design](design.md#import)). Review, repair and recognition
are separate steps after it, and every repair is one undo step.

## Import review (repair)

After **File → Open** or **Import** of a drawing, mlaser checks it. When it finds something, the
**Import review** window opens (switch this off with *Review every drawing after import*, setting
`plugin.orders.review_after_import`; open it any time with the command **Import review**,
`import.review`, from the command palette Ctrl+K). The pre-flight check before Start also offers
it: a drawing with open contours shows one line such as *44 open contours in 22 parts* with a
**Repair...** button.

| Found | Shown as | Fixed by *Fix all safe issues* |
|---|---|---|
| open contours | the gap between the two ends (mm) and the distance to the nearest other open end | open pieces whose ends are closer than *Join open ends closer than* (default 0.1 mm) are joined, then contours whose own gap is at most *Close gaps up to* (default 0.5 mm) are closed with a line. Pieces on different layers are never joined |
| duplicate contours | a contour lying on another one: it would be cut twice | the later one is removed |
| overlapping lines | open lines or arcs lying on each other, with the doubled length | merged, so each stretch is cut once |
| zero-length geometry | segments or contours of zero length (points drawn on purpose are kept) | removed |
| tiny segments | segments shorter than 0.01 mm | merged into the neighbouring segment |
| tiny objects | contours smaller than 0.1 mm | removed |
| self-intersections | a closed contour that crosses itself (figure-eight) | **not** fixed automatically: correct it in the CAD program |
| text not converted | TEXT / MTEXT that did not become outlines (it would not be cut) | converted with the bundled font, at the place the import would have put it |

Click a line in the list to zoom to it on the canvas. **Preview** draws the closing lines and
marks every finding without changing the drawing; **Fix all safe issues** applies the fixes that
are ticked, as **one undo step** (Ctrl+Z restores the drawing). Gaps larger than the tolerance
are left open on purpose: a 3 mm gap is usually a drawing error that a straight line would hide.

The tolerances are settings of the Orders plugin: `plugin.orders.join_tol`, `plugin.orders.close_gap`
(mm).

## Part recognition

An imported DXF has one object per contour, so a gear and its bore are two objects. Before
nesting, mlaser groups them into parts:

* **Outline with holes** (default): a closed contour that is not inside another one is a
  part's outline; everything inside it (holes, slots, etch lines, text, small islands) belongs
  to that part. Setting `plugin.orders.islands` = `parts` makes an island inside a hole a part of
  its own (part in part).
* **One part per layer**: for drawings where each part is on its own layer.
* **One part per block**: one part per block reference (INSERT) of the DXF; identical blocks
  are identical parts. Contours outside every block are grouped by outline.
* **Whole drawing as one part**, or **every object a part**.

Layers named like `ETCH*`, `MARK*`, `ENGRAV*`, `SCRIBE*`, `TEXT*` are *features*: they stay with
the part they are in and are never a part's outline. Layers named like `BEND*`, `DIM*`,
`DEFPOINTS`, `CONSTRUCT*`, `CENTER*`, `HIDDEN*`, `TITLE*`, `FRAME*`, `BORDER*` are ignored (title
blocks, bend lines, dimensions). Both lists are settings (`plugin.orders.feature_layers`,
`plugin.orders.ignore_layers`, comma-separated, `*` matches anything).

**Identical parts** are counted: parts with the same shape, moved or rotated, become one part
with a quantity (6 gears drawn in one file are *gear ×6*). Mirror images are different parts (a
left and a right bracket stay apart), unless the part is symmetric. Open contours that lie in no
part are not parts; they are left out of the nest with a message (repair them first).

**Nest → + From drawing** opens the **Recognised parts** window: each part with a picture, its
count, its size and its holes. Choose the rule at the top, select parts and press **Merge**
(they are one part) or **Split** (undo a grouping: identical copies become separate parts, a
part becomes one part per object), then **Nest these**. With a selection on the canvas only the
selected objects are used.

## Orders: several files and parts lists

### Several files

**Nest → Import file...** accepts several files at once (and a folder in the Orders tab). Each
file's parts are recognised: a single-part file becomes one part named after the file, a file
with several parts gives `<file>-1`, `<file>-2` ... You can also drop files on the Nest
workspace or the Orders tab.

### Parts lists (CSV)

The **Orders** tab (next to Nest) holds the open order lines. **Import parts list...** reads a
CSV file; **Template...** saves a template to fill in. Columns:

| Column | Also accepted as | Value |
|---|---|---|
| `file` | path, filename, drawing, dxf, part | the drawing (`.dxf`, `.svg`, `.chf`), relative to the CSV file's folder |
| `quantity` | qty, count, pcs, pieces | how many of the file (a whole number ≥ 1; default 1) |
| `material` | mat | the material name, as in the technology database (`Mild steel S235`); a unique part of a name is accepted (`S235`) |
| `thickness` | thk, thickness (mm) | mm, decimal point or comma |
| `customer` | client | |
| `order` | ref, reference, order no, po, job | the order or reference number |
| `due` | due date, deadline, date | `yyyy-mm-dd` or `dd.mm.yyyy` |
| `priority` | prio | a whole number (higher is nested first) or `low` / `normal` / `high` / `urgent` |
| `notes` | comment | optional |
| `part_number` | P/N, part no. | optional; printed on the part labels |
| `recognition` | mode | optional: `nested`, `layer`, `block`, `file` or `none` for this file |

The separator may be `,`, `;` or a tab; the file may be UTF-8 or Windows-1252 (Excel's "CSV"
export). Lines starting with `#` are comments. A file without a header row is read in the
template's column order. A row that cannot be used (file not found, quantity not a whole number,
thickness out of range, a date that does not exist, ...) is listed with its line number and the
reason; the other rows are imported.

The table shows each line with its progress (parts cut / parts ordered, from the production
report). Material, thickness, quantity, customer, order, due date and priority can be edited in
the table.

### From the order to the sheet

Choose a **material × thickness** group and press **Load into Nest**: the Nest part list gets
the group's parts with their quantities (line quantity × parts in the file) and priorities,
the Nest material and thickness are set, and the matching technology from the database is
applied to the layers the parts use (the Process workspace shows it as applied). Material and
thickness are entered once: in the order, or in the Process workspace (the Nest field follows
the technology on the layers until you type in it), or with Nest's **Technology** button.

Every nest part keeps its customer and order reference. **Send to queue** writes them on the
queue entry of each sheet ("ACME · 4711: 6 × gear"), the Run screen shows them on the load card,
and the production report records them with the cut sheet. The parts lists are saved in
`<data dir>/production/orders.json` and in the `.mlz` project.

Quoting is not part of mlaser yet.
