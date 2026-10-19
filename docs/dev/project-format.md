# Native project format (.mlz)

`.mlz` is mlaser's own project file (`mlaser/project/`, Qt-free). It stores the typed document
model and everything that belongs to a job: layers, technology assignments, the nest job with its
last result, the job queue, raster engravings, embedded fonts. It also stores the opaque `.chf`
records, so that a drawing opened from `.chf` and exported again stays byte-identical.

`.chf` stays the import and export interchange format (`mlaser/formats/chf.py`, the byte-exact
port in `mlaser/parity_ports/cadmodule/chf.py`). The original program cannot read `.mlz`.

```python
from mlaser.project import Project, save_project, load_project, export_chf

save_project("job.mlz", Project(document=doc, technology={"1": {"material": "S235"}}))
pr = load_project("job.mlz")          # ProjectError with a readable message when damaged
chf_bytes = export_chf(pr.document)   # raster parts as their burn segments
```

## Archive layout (format version 1)

| Member | Content |
|---|---|
| `mimetype` | `application/vnd.mlaser.project+zip`, stored uncompressed as the first member |
| `manifest.json` | `format` (`"mlaser-project"`), `version`, `app`, `created`, `saved`, `files` (sha256 and size of every other member), `sections`, `chf`, `dropped` (what could not be saved) |
| `document.json` | the `Document` (below), schema in `mlaser/project/schema.py` |
| `sections/<name>.json` | `technology`, `queue` and any section a plugin registers |
| `nest/job.json` | the `NestJob`: parts, sheets, parameters and the last `NestResult` |
| `blobs/<n>.npy`, `blobs/<n>.bin` | raster engraving arrays (`np.save`, no pickle), large byte values |
| `chf/records.chf` | the `.chf` export of the document when it carries `.chf` records (below) |
| `fonts/<file>` | fonts of text outlines that are not bundled (when embedded) |

`python -m mlaser.project schema` prints the JSON schemas, `python -m mlaser.project info FILE`
the manifest and a summary of a file.

## document.json

* Floats are written with `repr`, so they round-trip exactly (`-0.0` too). NaN and infinities,
  tuples, dicts with non-string keys and bytes are tagged objects: `{"$float": "nan"}`,
  `{"$tuple": [...]}`, `{"$dict": [[k, v], ...]}`, `{"$b64": "..."}`, `{"$npy": "blobs/..."}`.
* Segments are compact arrays: `["L", x0, y0, x1, y1]` and `["A", x0, y0, x1, y1, cx, cy, ccw]`.
* A part is `name`, `selected`, `craft`, `contours`, `raster` (array blob names), `extra`, and
  `chf_graph`: the index of its graph in `chf/records.chf`.
* A contour is `closed`, `segments`, `craft` (per-contour override), `glyphs` and
  `glyphs_current`, and `extra`.
* A craft holds every typed field (see "Typed model" below). `joints_auto` and `joint_widths_chf`
  keep how the joint widths were resolved, so a later change of `joint_width` still applies to
  the joints that were given without a width.
* Model objects found inside `extra` (the contour that the alpha-corner tool keeps, fly-cut scan
  paths) and the nest classes are tagged dataclasses (`{"$dc": "ScanPath", "f": {...}}`). Only
  the classes listed in `mlaser.project.codec.DATACLASSES` are decoded. Nothing is imported by a
  name taken from the file, and there is no pickle.
* A value that cannot be encoded (a callable, an unknown object) is left out. Its path is listed
  in `manifest.json` `dropped` and in `Project.notes`. Saving does not fail because of it.

## Byte-exact .chf through the project

The `.chf` codec keeps the parsed record tree of a loaded file in the model:

* `Part.extra['chf']` and `['_chf_snapshot']`;
* `Contour.extra['chf']` and `['_chf_craft']`;
* `Document.extra['chf']`.

The writer folds model edits into these records. These records are not encoded as JSON. Instead,
when the document has any of them, the project stores the `.chf` export of the document
(`dumps_chf(doc, version=doc.version)`) as `chf/records.chf`. On load, the file is parsed again
and the records of graph `chf_graph` are grafted back onto that part and its contours.

As a result:

* `.chf` → `.mlz` → `.chf` is byte-identical for an unedited drawing (`tests/test_project_format.py`,
  both samples);
* an edited drawing exports the same bytes before and after a project round trip;
* data that `.chf` cannot hold (cooling points in mm, `joint_width`, raster parts, sections)
  comes from the JSON.

## Typed model

`mlaser/model.py` has typed, documented fields for the data that used to live in `extra` keys:

| Field | Old key | Meaning |
|---|---|---|
| `Contour.craft` | `Contour.extra['craft']` | per-contour craft override (multi-contour parts) |
| `Contour.glyphs`, `glyph_key` | `['glyphs']`, `['glyph_segments']` | source glyphs; `glyph_key` is a fingerprint of the segments they describe (`model.segment_key`), not a deep copy |
| `Part.raster` | `Part.extra['raster']` | the `RasterEngraving` of a raster part |
| `Craft.micro_joints` | (positions) + `['micro_joint_widths']` | `MicroJoint(position, width)`, mm |
| `Craft.joint_width` | `['micro_joint_len']` | default width of joints given without one |
| `Craft.cool_points` | `['cool_points']` and `Craft.cool_point` | the one cooling-point list (mm of arc length; a legacy (x, y) point is projected) |
| `Craft.start_param` | `['start_param']` | start as a fraction of the contour length |
| `Craft.overcut` | `['overcut']` | signed overcut / gap, mm |
| `Craft.lead_error` | `['lead_error']` | lead-check error mark |

A micro-joint given without a width takes its width in this order:

1. the `.chf` per-joint width at the same index;
2. `joint_width`;
3. 0.5 mm.

This is the order `cam.toolpath` used before. `Craft.cool_point` is a deprecated property for
the first (x, y) entry of `cool_points`.

### The old keys still work

`extra` is a `model.LegacyExtra` view:

* the old keys read, write, test and delete the typed fields;
* iteration, `dict(extra)`, `extra.items()` and a standalone `copy.deepcopy(extra)` include them,
  so these give what the old dict gave;
* the dict itself stores only opaque data.

Every use of an old key issues `model.LegacyExtraWarning`, a `DeprecationWarning` that the test
configuration silences. The exception is the frozen parity ports: the `.chf` codec, the DXF rules
and the ordering keep using the old keys unchanged, and they do so without a warning.

`model.EXTRA_KEYS` lists the remaining `extra` keys, with their owner and meaning. They are
opaque round-trip data, importer notes and tool state.

Costs:

* `build_toolpath` and the goldens are unchanged (bit-identical);
* creating a `Contour` costs about 0.8 µs more, because of the `extra` descriptor;
* a deep copy of 600 random parts costs about 25 % more.

## Versions and migrations

`manifest.json` `version` is the format version. `mlaser.project.migrations` has one step per
version (`MIGRATIONS[n]`: the members of version n become the members of version n + 1). A step
rewrites JSON and never uses the model classes, so an old file keeps loading after the model
changes.

* Files newer than the program are refused: "made by a newer mlaser (project format N …)".
* A migrated file loads with the note "migrated from project format N". It is written in the
  current version the next time it is saved.
* Version 0 was the Wave 3 draft: one `project.json` with the pre-typed model. The fixture is
  `tests/data/project/v0_sample.mlz`.

When you change the layout of a member, do all of the following:

1. bump `CURRENT`;
2. add the migration step;
3. add a fixture of the old version to `tests/data/project/`;
4. extend `test_v0_fixture_migrates` (or add a test for the new step);
5. update this page.

## Validation and errors

Loading checks the following, in order:

1. the zip structure;
2. the member names (no absolute paths, no `..`);
3. the total size (4 GB at most);
4. the CRC of every member;
5. the manifest (schema, format, version);
6. the sha256 and size of every listed member;
7. `document.json` against the schema, with the JSON path of the first errors;
8. the decoding.

Every failure is a `ProjectError` whose message names the file and the problem, for example
`job.mlz: document.json is damaged (checksum mismatch)` or
`job.mlz: not an mlaser project (.mlz) file`.

## Atomic save, autosave and recovery

`save_project` (and `atomic_write`) writes a temporary file in the target folder, calls fsync,
then `os.replace`, then fsyncs the folder. A crash during the save leaves either the old file or
the new one.

`mlaser.project.Autosave(dir)` writes two files:

* `autosave.mlz`, the whole project;
* `autosave.chf`, which the original writes when a job is prepared. The breakpoint and resume
  path (`AutosaveParam*.ini`, `nc/resume.py`) indexes the graphs of this file.

`recover()` prefers `autosave.mlz`. It falls back to `autosave.chf` when the project is missing,
damaged, or older than the `.chf` (for example a `.chf` written by the original program).

In the application:

* `ext_machine` autosaves when a job is prepared;
* System Recovery and the start-up crash prompt use `recover()`;
* `ext_project` also autosaves every `autosave_interval_s` seconds (setting
  `plugin.ext_project.autosave_interval_s`, default 120, 0 = off) when the document has changed.

## Plugin sections

```python
proj = window.extension("ext_project")
proj.register_section("technology", get=lambda: db.assignments(), set=db.load_assignments)
```

`get()` returns JSON-able data. It may also contain model objects, nest objects, tuples or numpy
arrays, which are encoded as described above. `set(value)` receives the loaded value when a
project is opened or recovered. `technology` (an object) and `queue` (a list of objects) have
schemas; any other name holds free data.

The providers `project.open(path)`, `project.save(path)` and `project.current()` are what the
Shell's File > Open / Save use for `.mlz`. The commands are `file.open_project`,
`file.save_project` and `file.export_chf`.
