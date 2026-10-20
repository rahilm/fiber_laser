# mlaser-onshape: Onshape parts to laser order lines

An mlaser plugin (plugin standard v1, plugin API 2). It reads an assembly's bill of materials
from Onshape and picks the parts this laser cuts. For each part it measures the thickness,
maps the Onshape material to the technology database and draws the part's flat face as a
DXF. The parts become order lines grouped by material and thickness, and each group can be
loaded into Nest.

The plugin uses only Onshape's public REST API, with your own API keys. It contacts no
other server and never starts anything on the machine: its manifest declares
`machine = false` and `queue = false`.

## Install

From a release, open **Options+ > Plugins > Plugin manager**, install the `mlaser-onshape`
wheel and then enable it. Enabling needs Service mode. Restart mlaser.

For development, install it next to mlaser:

```sh
pip install -e plugins/mlaser-onshape
```

The plugin shows the permissions it asks for before it is enabled:

| permission | why |
|---|---|
| network `cad.onshape.com`, `*.onshape.com` | the Onshape API; `*.onshape.com` covers an Onshape Enterprise domain such as `yourschool.onshape.com` |
| secrets | keeps your Onshape API keys in the secret store, never in the settings file |
| files | writes DXF files and a download cache in the plugin's data folder |
| orders | adds order lines to the Orders list |

## Set up

1. **Create API keys.** In Onshape's Developer Portal, go to **API keys** and create a key
   pair with read access (*Application can read your documents*). The keys belong to the
   Onshape account that created them, and every call counts against that account's daily
   API limit.
2. **Connection.** Open the **Onshape** workspace (rail button, Engineer mode) and go to the
   **Connection** tab:
   - **Onshape server**: keep `https://cad.onshape.com`, or enter your Enterprise address,
     such as `https://yourschool.onshape.com`. The plugin refuses any address outside
     `onshape.com`.
   - **Access key** and **Secret key**, then **Save**. The keys go to the secret store and
     the fields clear. **Forget the keys** removes them again.
   - **Test connection** shows the Onshape account name.
   - **Process properties**: the custom properties that hold a part's manufacturing steps,
     listed in the order the steps happen. The default is `Pre Process, Process, Process 2`.
     If your company uses other names, enter them here. A column called `Process 1` is read
     as `Process`.
   - **Customer / team**: written on the order lines. Leave it empty to use the document
     name.
3. **Machine.** Use the **Machine** tab to say which parts belong to this laser:
   - **By a process property**: enter the values that mean this machine, separated by
     commas, for example `Laser, Fiber laser` (case does not matter). **Pick from the BOM…**
     lists every value found in the loaded BOM. **Properties that count** chooses which
     properties may name the laser, for example only `Process`, or `Process` and
     `Process 2`.
   - **Include parts whose laser step comes after another process**: Onshape does not know
     which steps are already done. A part whose `Process` is *Lathe* and whose `Process 2`
     is *Laser* is therefore either always included (box ticked) or never included (the
     default). Enter what is already made in the parts table's **Made** column.
   - **Every sheet-like part**: use this when your team does not use a process property.
     It takes every part whose smaller side is at least *n* times its thickness and whose
     thickness is no more than the set maximum.
   - **Robots** multiplies the BOM quantity (two robots: ×2). **Spares per part** is added
     to every part.
   - Click **Apply**.

## Use

1. **Browse.** Search for a document, or pick *Recent*, *My documents*, *Shared with me* and
   so on. Folders and projects open like folders, and **Up** goes back. Selecting a
   document lists its workspaces and versions, then its assemblies. Choose an assembly and
   click **Load bill of materials**. The plugin reads the *flattened* BOM, so every part
   appears once with its total quantity. Each configured part keeps the configuration of
   its BOM row. Standard content (library screws) and sub-assembly rows are skipped.
2. **Parts table.** The parts for this machine are ticked. The columns show:
   - the name, P/N and material;
   - the sheet material from the technology database;
   - thickness three ways: the distance between the faces (*t faces*), volume ÷ the
     largest face (*t volume*) and the value written in the material name (*t name*);
   - the BOM quantity, **Made** (editable) and **To cut**, which is BOM quantity × robots +
     spares − made;
   - the process values, the DXF face and a status.
3. **Download DXFs & create orders** downloads every ticked part, writes its DXF and adds
   the parts that are ready to the Orders list. A progress bar and **Cancel** show while it
   runs; parts already in flight finish. The **API calls** counter shows how many requests
   went to Onshape.
4. **Materials.** Every Onshape material name, such as `Aluminum - 6061` or `Steel 4mm`, is
   mapped once to a material of the technology database, such as `Aluminium AlMg3` or
   `Mild steel S235`. That name is also the sheet material that Orders and Nest use. The
   drop-down puts its best suggestions first, and a name that already equals a database
   material maps itself. Plastics and wood (polycarbonate, acrylic, HDPE, plywood …) are
   never mapped automatically, because a fiber laser does not cut them. Every mapping is
   saved. After mapping, click **Create orders for ready parts**. A second run replaces the
   order lines an earlier run made for the same parts instead of adding them again.
5. **Sheets.** One card is shown per material × thickness group. Each card lists the
   number of parts and pieces, the part area, a rough sheet count for the sheet size above
   the cards, and any part larger than the sheet. **Load into Nest** loads that group from
   the Orders list into the Nest workspace (the Orders command *Load into Nest*), which
   also selects the technology.

### Statuses

| status | meaning |
|---|---|
| ready | DXF written, material mapped: becomes an order line |
| map the material | DXF written; map the Onshape material in the Materials tab |
| not for a fiber laser | a plastic or wood: map it by hand if you really mean it |
| not this machine | no counted process property names this machine (the reason says what it is instead) |
| nothing to cut | already made enough |
| no flat face | a shaft, a revolved or mesh body: there is nothing to flatten |
| not sheet | (sheet mode) too thick or too narrow for a plate |
| DXF error | an edge could not be drawn or a loop does not close (the notes say which) |
| Onshape error | the request failed (keys, access to the document, API limit) |

## How the DXF is made

The DXF is built from Onshape's exact part geometry (`/bodydetails`). It does not use
Onshape's translation service.

**Which face is drawn.**
- The plugin groups the part's flat faces into *sides* by the direction they face. A
  side's area is the area of all its flat faces together, so a pocketed side has the same
  area as the plain side opposite it.
- The side with the largest area wins.
- When the two big sides tie, the side with *pockets, bosses or counterbores* wins: it has
  flat faces below its outer plane. Through-holes show on both sides, so they decide
  nothing.
- A plain, symmetric plate uses its **Top (+Z)** face. For other directions the order is
  Front (−Y), Right (+X), then the opposite faces.
- The drawn face is the largest flat face of the chosen side.

**Choosing another face.** Select a part and click **DXF face…** to pick another face. The
choice is saved for that part and used by every later download. Choose *Automatic* to go
back. If the saved face no longer exists (the part was re-modelled), the automatic choice
is used and the notes say so.

**How it is drawn.**
- The face is drawn **as seen from outside**, in millimetres.
- The lower-left corner of the outline sits at 0,0.
- X follows the part's X axis, or the part's Y axis when the face looks along X.
- The file is an R12 DXF with everything on layer 0.
- Lines, arcs and circles stay exact LINE, ARC and CIRCLE entities.
- Ellipses and B-splines are evaluated exactly from their definition and written as
  polylines within 0.02 mm.
- Any other edge type is fetched as a polyline from `/tessellatededges` at a 0.02 mm chord
  tolerance.

The DXFs are saved under `<mlaser data>/plugins/data/onshape/dxf/<document>/`. Each file is
named by the part number, or by the part name when there is none; a configured part gets a
short suffix per configuration.

**Thickness.** The plugin measures the thickness two ways:
- the distance between the largest flat face and the largest face opposite it;
- volume ÷ the area of the largest flat face, from `/massproperties`.

The first value is used. When the two disagree (pockets, bosses), the notes say so. A
thickness written in the material name, such as `3mm`, `6 mm`, `1/8"`, `1/4in`, `0.125 in`,
`.25"` or `3/16 inch`, is compared with the measured value, and a difference larger than
the tolerance is noted. The measured value always wins. The mass-properties cross-check
costs one more call per part and can be switched off in Connection.

## API limits and the cache

Onshape limits API calls per account and day.
- Answers that cannot change are cached in the plugin's data folder: a version, or a
  workspace at a given document *microversion*.
- The cache key holds the microversion, element, part and configuration. A second
  download of an unchanged document therefore costs one call, the microversion check.
- At most two requests run at the same time.
- mlaser's HTTP client retries `429 Too Many Requests` and `503`, and waits as long as
  `Retry-After` asks. When the limit persists, the plugin says so.
- **Clear the download cache** in Connection empties the cache.

Per part, a download costs up to four calls: body details, mass properties, tessellated
edges (only for unusual edges) and, optionally, a thumbnail. Each BOM load costs two more:
the microversion check and the BOM.

## Offline example

```sh
python plugins/mlaser-onshape/examples/offline_demo.py
```

The example runs the whole path without an Onshape account, against the fake Onshape server
that the tests use (127.0.0.1 only). It prints each part's status, the order lines and the
sheet groups.

## Tests

```sh
python -m pytest plugins/mlaser-onshape/tests
```

The tests run with mlaser's own environment, without installing the plugin. They use:
- a fake Onshape server (threaded `http.server` on 127.0.0.1) that returns JSON shaped like
  Onshape's OpenAPI schemas;
- a stub plugin context that implements plugin API 2 (`ctx.http`, `ctx.secrets`,
  `ctx.data_dir`, `ctx.run_in_background`, `ctx.services`).

## Limits

- Only API-key (Basic) authentication; no OAuth sign-in.
- Thumbnails (`/shadedviews`) are supported by the client, but the table does not show them.
- Sheet-metal parts are flattened face by face and are not unfolded. Use Onshape's flat
  pattern for bent parts.
- The sheet count is an estimate from the parts' bounding boxes. Nest decides the real
  layout.

Licence: MIT.
