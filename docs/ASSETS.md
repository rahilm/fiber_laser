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
| `mlaser/ui/resources/layer_colors.json` | 11 RGB layer colours | Values read from MainApp.exe `.rdata` (FUN_004473c0; docs/re/ui.md §3.1). Needed so layers in the user's `.chf` files keep the colours they have in the original. | facts |
| `mlaser/cad/fontdata/NotoSansSC-Regular-subset.otf` (1,763,304 B, SHA-256 `ca60a451…8dd1f9e`) | bundled CJK-capable text font (Draw → Text, DXF TEXT fallback): Noto Sans SC 2.004, subset to GB 2312 + Latin/Greek/Cyrillic + punctuation by `tools/build_bundled_fonts.py` (downloads https://github.com/notofonts/noto-cjk/raw/Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf, checks its SHA-256 `faa6c9df…5ea9`, subsets deterministically; `--full-cjk` keeps all of U+4E00–9FFF, 5.2 MB; the full upstream SC file is 8.3 MB, Noto Sans CJK SC ~16 MB) | SIL OFL 1.1, no Reserved Font Name, © 2014-2021 Adobe; `fontdata/OFL-NotoSansCJK.txt` |
| `mlaser/cad/fontdata/NotoSansArabic-Regular-subset.ttf` (112,560 B, SHA-256 `870a5d20…b493d`) | Arabic fallback font for shaping: Noto Sans Arabic 2.013 (release zip SHA-256 `1301acea…54f1`, member `NotoSansArabic/unhinted/ttf/NotoSansArabic-Regular.ttf`), subset by the same script | SIL OFL 1.1; `fontdata/OFL-NotoSansArabic.txt` |

Packaging (not in the Python package, copied into frozen bundles by `packaging/`):
`packaging/licenses/LGPL-3.0.txt` and `GPL-3.0.txt` are the verbatim FSF licence texts; the
build generates `LICENSES/THIRD_PARTY_NOTICES.txt` from the installed dist-info metadata of what
is actually in the bundle (`packaging/gen_licenses.py`), plus a Qt/PySide6 notice with the Qt
version, the source location and how to replace the shared Qt libraries (LGPL-3.0 §4).

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
| Copies of the original's `lang.txt`, `French.txt`, `lang.ini` used as test data (the synthetic replacement first lived at the same path) | `tests/data/Lang` | 3 files | as above | a small synthetic fixture in the same file format, written for the tests, now in `tests/data/lang_fixture` |
| The original's autosave drawings `File/autosave.chf` (v9, one 839-element scan graph) and `File/Temp/tempGraph.chf` (v5, one circle) | `tests/data/autosave.chf`, `tests/data/tempGraph.chf` | 2 files, 658 KB | came with the vendor's archive | `tests/data/chf/sample_v9.chf`, `sample_v5.chf`, written by `tools/make_test_chf.py` from mlaser's model |
| Byte-identical copies of the original's state files: `File/AutosaveParam1/2.ini`, `File/ManuContour.dat`, `File/PithCompensate.pcf`, `File/softPara.ini`, `File/Temp/{AutosaveParam1,AutosaveParam2,ManuContour,tempIsBreak}.*`, `JumpAddTime.txt`, `LanFormatEStr.txt`, `Help/calib.csv` | `tests/data/` (root, `Temp/`, `Help/`) | 13 files | came with the vendor's archive (small numeric state, but copies all the same) | hand-written files in the same formats in `tests/data/appdir/` (install-folder layout) |

The tests that check the original's own files byte for byte still exist; they read them from a
local copy of the install (`MLASER_VENDOR_SAMPLES`, docs/dev/TESTING.md) and skip without it.

**Git history.** These files were part of the first commit (`87a0fad Baseline: import the
current rebuild tree`); the test-data copies stayed until they were replaced. Removing them from
the working tree does not remove them from history. Before the repository is published, rewrite
the history, or publish from a fresh, squashed root commit. The complete path list (no path in it
exists in the current tree, so the rewrite removes nothing that is still used):

```
git filter-repo --invert-paths \
  --path mlaser/ui/resources/icons/buttons \
  --path mlaser/ui/resources/icons/strips \
  --path mlaser/ui/resources/icons/misc \
  --path mlaser/ui/resources/lang \
  --path mlaser/ui/resources/params.json \
  --path tests/data/Lang \
  --path tests/data/autosave.chf \
  --path tests/data/tempGraph.chf \
  --path tests/data/AutosaveParam1.ini \
  --path tests/data/AutosaveParam2.ini \
  --path tests/data/ManuContour.dat \
  --path tests/data/PithCompensate.pcf \
  --path tests/data/softPara.ini \
  --path tests/data/JumpAddTime.txt \
  --path tests/data/LanFormatEStr.txt \
  --path tests/data/Help \
  --path tests/data/Temp
```

(`mlaser/ui/resources/icons/tabler` and `tests/data/lang_fixture` must stay: do not pass the
parent `icons` directory.)

**Label text in earlier versions of `mlaser/formats/data/mlaser_params.json`.** The baseline
version of that file (blob `c6d04277`, commit `87a0fad`) still contains the vendor's label
wording; since `62e608f` it is stripped (blob `6504c66c`, the current one). It cannot be removed
with a path filter, because the file itself must stay; strip it with a blob callback instead.
Structure: the file is one JSON array of 1,129 descriptor objects. In the old version every
descriptor has two string fields `"label_en"` and `"label_zh"` (between `"lang"` and `"type"`),
and the 153 descriptors with an `"options"` array have, in each of its 451 option objects, two
string fields `"en"` and `"zh"` next to `"lang"`. No other field carries vendor text (`"lang"`
keys, `"unit"`, `"cond"` and the numbers are facts and stay). Deleting those four keys and
re-serialising with `json.dumps(data, indent=1, ensure_ascii=False) + "\n"` (UTF-8) reproduces
the current blob byte for byte, so after the rewrite the file is identical in every commit:

```python
# git filter-repo --blob-callback (run together with the --path list above, or separately)
import json
try:
    d = json.loads(blob.data)
except ValueError:
    d = None
if isinstance(d, list) and d and isinstance(d[0], dict) and "label_en" in d[0] and "struct_offset" in d[0]:
    for e in d:
        e.pop("label_en", None); e.pop("label_zh", None)
        for o in e.get("options") or []:
            o.pop("en", None); o.pop("zh", None)
    blob.data = (json.dumps(d, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
```

No other JSON file in the history has these fields (the old `mlaser/ui/resources/params.json`,
with its vendor string "hints", goes with the path list).

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
| `chf/sample_v9.chf`, `chf/sample_v5.chf` | written by `tools/make_test_chf.py` (deterministic, from mlaser's model through `save_chf`; the text graph uses the bundled Noto font) | replace the original's autosave drawings; `--check` verifies them, and the golden `.chf` sample cases are built on them |
| `appdir/` (`File/*.ini`, `File/ManuContour.dat`, `File/PithCompensate.pcf`, `File/softPara.ini`, `File/Temp/*`, `JumpAddTime.txt`, `LanFormatEStr.txt`, `Help/calib.csv`) | written by hand for mlaser's tests in the original's formats (docs/re/formats.md), with our own values; same layout as the install folder | replace the copies of the original's state files |
| `lang_fixture/lang.txt`, `French.txt`, `lang.ini` | written for mlaser's tests in the original's format | synthetic (two short technical entries, `mf6` Start and `ROREgName_5` Input Status, read like the original's) |
| `project/v0_sample.mlz` | an mlaser project written by mlaser (format 0) | own |
| `dxf/*.dxf` | generated with ezdxf by the parity harness's `compare/make_samples.py` (outside this repository); differ from a fresh run only in timestamps and GUIDs | own; not from the Mlaser download (no byte match with its `dxfTemplate.dxf`) |

The original's own files are read only from a local copy of the install
(`MLASER_VENDOR_SAMPLES`, `tests/vendor_samples.py`). A few tests quote single short lines of the
original's logs and reports as inline strings (`tests/test_controller.py` frame bytes,
`tests/test_report_records.py` one record per report format, calibration numbers in
`tests/test_devices_follower.py` / `test_settings_import.py`): facts needed to parse those
formats, a line each.

## 6. Not bundled, never copied

Vendor branding (splash and logo bitmaps RT_BITMAP 472/42885, `res/splash.bmp`,
`File/help.bmp`, `File/logo_1.bmp`, the application icon), the original's binaries and DLLs,
help documents and firmware files.

## 7. Dependency licences

Every dependency must be MIT-compatible for redistribution with an MIT program: no GPL, AGPL or
non-commercial licences (docs/review/libraries.md, "Licence context").  Checked on 2026-10-18
from the installed dist-info metadata and licence files, and the upstream repositories.

| Package | Version | Licence | Use | Evidence / notes |
|---|---|---|---|---|
| PySide6 / shiboken6 | 6.12.0 | LGPL-3.0-only (or GPL-2/3), used under the LGPL | the UI | frozen builds are one-folder: Qt stays as replaceable shared libraries; licence notice + source pointer in the bundle (`packaging/README.md`) |
| ezdxf | 1.4.5 | MIT | DXF import (faithful and modern modes) | dist-info `licenses/LICENSE` |
| numpy | 2.5.4 | BSD-3-Clause (+ 0BSD, MIT, Zlib, CC0 parts) | everywhere | METADATA |
| shapely | 2.2.0 | BSD-3-Clause; bundled GEOS is LGPL-2.1 as a shared library | geometry, STRtree filters | METADATA |
| segno | 1.6.6 | BSD-3-Clause | QR codes | METADATA |
| fontTools | 4.67.0 | MIT | glyph outlines, font subsetting | dist-info `licenses/LICENSE` (© 2017 Just van Rossum) |
| Pillow | 13.0.0 | MIT-CMU | bitmap engraving | METADATA |
| **svgelements** | 1.9.6 | MIT | SVG import | dist-info `LICENSE` (© 2019 meerk40t), PyPI `License: MIT` |
| **pyclipper** | 1.4.0 | MIT; embedded Clipper 1 (Angus Johnson) Boost Software License 1.0 | opt-in kerf fallback, synthetic bold text | dist-info `licenses/LICENSE`; README "License" section and the `src/clipper.cpp` header in github.com/fonttools/pyclipper state BSL-1.0 for Clipper |
| **uharfbuzz** | 0.56.3 | Apache-2.0; HarfBuzz inside under the "Old MIT" licence | text shaping | dist-info `licenses/LICENSE`; the wheel does not ship HarfBuzz's COPYING, the bundle notices add it |
| **ortools** (extra `ortools`) | 9.15 | Apache-2.0 | optional sort mode 20 backend (profile `modern-ortools`) | METADATA; pulls pandas (BSD-3), protobuf (BSD-3), absl-py, immutabledict (Apache-2.0 / MIT) |
| **spyrrow** (extra `nest`) | 0.9.0 | MIT (PyPI licence field empty; repo `LICENSE.txt` and Cargo.toml `license = "MIT"`, © 2025 Paul Durand-Lupinski) | optional nesting engine | wheel `licenses/LICENSE.txt` and CycloneDX SBOM (`sboms/spyrrow.cyclonedx.json`); GitHub licence API |
| sparrow (inside spyrrow) | git 881cdcb | MIT (© 2025 Jeroen Gardeyn, KU Leuven) | strip-packing solver, statically linked | repository LICENSE at that revision |
| jagua-rs (inside spyrrow) | 0.7.0 | **MPL-2.0**, unmodified | collision engine, statically linked | crates.io / SBOM. MPL-2.0 is file-level copyleft: distributing it unmodified in binary form inside a larger work is allowed (§3.3); recipients must be told where the source is (§3.2(a)): https://github.com/JeroenGar/jagua-rs, crate jagua-rs 0.7.0, and the MPL text kept. Modified jagua-rs files would have to be published under MPL-2.0. The other 99 Rust crates in the SBOM are MIT / Apache-2.0 / Unlicense / Zlib / Unicode-3.0 |
| **ruckig** (extra `ruckig`) | 0.19.4 | MIT (Community edition) | optional modern planner profile `modern-ruckig` | dist-info `licenses/LICENSE` (© 2021 Lars Berscheid). Only offline Community features are used (no intermediate waypoints / cloud API, which are Pro); a test checks it |
| hidapi, pyserial (extra `devices`) | 0.15.0, 3.5 | BSD (hidapi dual BSD/GPL-3: used under BSD) | pendant, serial links | METADATA |
| platformdirs (optional) | 4.13 | MIT | config dir of the first-run self-test | METADATA |
| `mlaser._native` (optional C extension in the main package, `mlaser/_native/_emitter.c`) | (package version) | MIT (this project) | C emitter kernel, the default when it builds and passes its self-check (docs/dev/NATIVE_KERNEL.md) | own code; calls CPython's `math.hypot` at run time, copies nothing |
| hypothesis (dev only) | 6.170 | MPL-2.0 | property tests, not distributed | METADATA |
| PyInstaller (build tool only) | 6.22 | GPL-2.0-or-later with the bootloader exception | frozen builds | the exception allows distributing the frozen app under any licence; PyInstaller itself is not shipped as a library |

Avoided on purpose (libraries.md): GPL (potrace and bindings, scapy, pygcode, deepnest core),
AGPL (PyMuPDF, Nuitka without its runtime exception), non-commercial (LKH-3 / elkai).
