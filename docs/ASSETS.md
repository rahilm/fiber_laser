# Bundled assets: provenance and licences

mlaser is MIT-licensed. This file lists every non-code resource in the package (`mlaser/`)
and in the test data (`tests/data/`), where it comes from, and its licence. It also records
what was removed from earlier snapshots, and why.

Rule of thumb: **facts** needed to read and write the user's own files or talk to their
hardware stay. That covers parameter keys, types, defaults, ranges, struct offsets, register
numbers and colour values. They are not creative expression, and interoperability needs them.
**Creative material** authored by the vendor is not redistributed: icons, bitmaps, translated
text and label wording. Where the UI needs it, we wrote our own, or the user loads it from their
own copy of the original at runtime.

## 1. In the package

| Resource | What | Provenance | Licence |
|---|---|---|---|
| `mlaser/ui/resources/icons/tabler/*.svg` (167 files) | UI icons (ribbon, menus, control panel, CAM+/Devices tabs) | [Tabler Icons](https://github.com/tabler/tabler-icons) 3.24.0, npm `@tabler/icons`, `icons/outline/`, unmodified. Only icons referenced by `mlaser/ui/icons.py` are vendored (a test checks that the set matches). | MIT, © Paweł Kuna; `icons/tabler/LICENSE` |
| `mlaser/i18n/en.json` | English UI catalogue: 1,858 strings, readable ids, placeholder names, map from the original's keys | Written for mlaser (see §3) | MIT (mlaser) |
| `mlaser/formats/data/mlaser_params.json` | 1,129 parameter descriptors | Facts recovered from MainApp.exe's descriptor tables (§4) | facts; file is mlaser's (MIT) |
| `mlaser/ui/resources/layer_colors.json` | 11 RGB layer colours | Values read from MainApp.exe `.rdata` (FUN_004473c0; docs/ui.md §3.1). Needed so layers in the user's `.chf` files keep the colours they have in the original. | facts |

Code-level tables of the original's protocol, register and alarm numbers (`mlaser/controller`,
`mlaser/diag`, ...) are interoperability facts and are documented next to the code. Some modules
carry short English fallback labels for registers and alarms (e.g. `mlaser/diag/registers.py`
`RO_EN`). These are plain technical names ("Input Status", "FIFO space margin"). They are used
only when no translator is passed, and the catalogue supplies the UI's text.

## 2. Removed

| What | Where it was | Count | Why removed | Replaced by |
|---|---|---|---|---|
| Icons and bitmaps extracted from MainApp.exe (RT_PNG ribbon strips 346..452, RT_BITMAP control-panel buttons 467..1457 incl. dark-theme variants, dashboard tiles 458..506, edge-seek/verticality diagrams, gauges) | `mlaser/ui/resources/icons/{strips,buttons,misc}` | 478 files (15 + 348 + 115), ~5.5 MB | vendor artwork, not redistributable; raster images were also blurry at 150 % scaling | Tabler SVG icons through a palette-aware SVG icon engine (`mlaser/ui/icons.py`); commands without a fitting icon are text-only buttons |
| The original's language files `lang.txt` (zh + en) and `Russian/German/Spanish/Portuguese/French/Italian/Polish/Vietnamese/Turkdili.txt` | `mlaser/ui/resources/lang` | 10 files | vendor-authored text and translations | our own English catalogue + the runtime importer (§3) |
| `params.json`: parameter registry scraped from MainApp.exe string literals (name, label key, guessed section, neighbouring literals as "hints") | `mlaser/ui/resources/params.json` | 1 file, 862 entries | inaccurate (sections such as `%.3f`, hints such as `语言包`), a subset of the descriptor tables | `mlaser.ui.params.registry()` now derives from `mlaser_params.json` |
| Vendor label text in the descriptor file: `label_en`, `label_zh` per descriptor and `en`/`zh` per enum option | `mlaser/formats/data/mlaser_params.json` | 1,129 × 2 labels + option labels | vendor-authored wording | labels resolved at run time from `en.json` (English) and imported translations (Chinese); `ParamDef.label_en`/`label_zh` keep working |
| Copies of the original's `lang.txt`, `French.txt`, `lang.ini` used as test data | `tests/data/Lang` | 3 files | as above | a small synthetic fixture in the same file format, written for the tests |

**Git history.** These files were part of the first commit (`87a0fad Baseline: import the
current rebuild tree`). Removing them from the working tree does not remove them from history.
Before the repository is published, rewrite the history (e.g. `git filter-repo --path
mlaser/ui/resources/icons --path mlaser/ui/resources/lang --path mlaser/ui/resources/params.json
--path tests/data/Lang --invert-paths`) or publish from a fresh, squashed root commit.

## 3. UI text (i18n)

* **English** (`mlaser/i18n/en.json`) is mlaser's own catalogue. To build it, a script listed
  every key the code uses (`tools/i18n_keys.py`: string literals, module-level key tables, and
  the label keys of the descriptor file). Where the original's English column was a plain UI
  label ("Save as", "Cut Speed"), it served as a starting point. The text was then normalised
  and corrected:
  * Mistranslations reported in docs/review/ux.md are fixed: "Copy to maun" → "Copy to Main
    Drawing", "Roasting machine test" → "Burn-in Test", "Select Inside Mould" → "Select Inner
    Contours", "Pline" → "Polyline", "Cut/Extension" → "Trim/Extend", "SelectedManu" →
    "Selected Parts Only", "Clear Warning" → "Clear Alarm", "Hardware connect successfully" →
    "Hardware connected", and the two "Fine-tuning" captions → "Keyboard Nudge".
  * Terminology: Drill → Pierce, Manu → Process, Frog Jump → Leapfrog, Dedust → Dust Extraction.
  * Dozens of typos fixed (Graudal, Precsion, Velcity, mico-link, Sperm recovery speed → Fine
    homing speed, ...). Messages were rewritten as plain sentences.
  * Two entries whose "English" was Chinese now have English text, and nine label keys that
    the original never defined (it showed the raw key) were given labels.
  * Placeholders are named, e.g. `{minutes}` instead of `%d`, with a declared positional order.
* **Readable ids.** Ids look like `cmd.machine.home`, `panel.estimate`,
  `param.cut_basic.cut_speed` and `alarm.bus_fault`. The `legacy` table maps every original key
  (`mf15`, `pd125`, ...) to an id, so existing `tr("mf15")` call sites keep working. The format
  is plain JSON. A Qt Linguist `.ts` export can be generated from it when translators need one.
* **Other languages** are not shipped. `mlaser.i18n.importer` reads the `Lang` folder of the
  user's own Mlaser install, or one of its `.txt` files. It copies the texts into the user's
  data folder (`~/.mlaser/lang/<code>.json`), keyed by the original's keys. The translator
  joins them to our ids through the `legacy` table. Imported printf-style texts are formatted
  with the same call as our named placeholders. Usage is in the README ("Languages").
  `--lang-dir` reads an install in place without copying.
* **Simplified Chinese** goes through the importer as well. A proper zh catalogue needs a
  native-speaker review of about 1,850 technical strings. Until then, users with the original
  import its Chinese, and everyone else gets English. No machine translation was added.

## 4. Data recovered from the binary

| File | Kept (facts) | Removed / replaced |
|---|---|---|
| `mlaser_params.json` | file, table, index, category, XML key, label **key**, type, default, unit, min/max, condition, flag, struct offset, enum option **keys** (the `SP.Lang` options are language names, used as-is) | English/Chinese label text (§2) |
| `layer_colors.json` | 11 colour values | — |
| `params.json` (ui) | — | whole file (superseded, §2) |

These facts are what the program needs to read and write `HardPara.xml`, `ManuPara.xml`,
`LayerPara.xml` and `.chf` files byte-exactly, to show the user's parameters with the right
types and ranges, and to map card memory. They describe an interface. They are not creative
expression.

## 5. Test data (`tests/data`, not in the package)

| Files | Provenance | Note |
|---|---|---|
| `Lang/lang.txt`, `Lang/French.txt`, `Lang/lang.ini` | written for mlaser's tests in the original's format | synthetic |
| `dxf/*.dxf` | DXF samples used by the import tests | not from the Mlaser download (no byte match); provenance not recorded here. Check before publishing |
| `softPara.ini`, `AutosaveParam*.ini`, `ManuContour.dat`, `JumpAddTime.txt`, `LanFormatEStr.txt`, `PithCompensate.pcf`, `Help/calib.csv`, `Temp/*` | byte-identical copies of working/state files from the original's install folder (`File/`, `Help/`, root) | small numeric state and key lists, used to test the readers byte-exactly; facts |
| `autosave.chf`, `tempGraph.chf` | the original's autosave drawings from the download's `File/` folder | test inputs for the `.chf` reader. They are drawings, not text or art, but they came with the vendor's archive. Before a public release, consider replacing them with drawings saved by the user's own machine |

## 6. Not bundled, never copied

Vendor branding (splash and logo bitmaps RT_BITMAP 472/42885, `res/splash.bmp`,
`File/help.bmp`, `File/logo_1.bmp`, the application icon), the original's binaries and DLLs,
help documents and firmware files.
