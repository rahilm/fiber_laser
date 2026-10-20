# Responsive layout

The window adapts to the space it actually gets: the window size, the width the user dragged a
dock to, the font size and the screen's scale. There are no screen-size breakpoints. Every
threshold comes from the content, for example "the widest label plus its field no longer fits"
or "two columns of the widest section no longer fit".

## Rules

1. **No horizontal scroll bar** anywhere in the main window. A table hides its least important
   columns first; its key column stretches.
2. **No vertical scroll bar on a form or control panel.** This covers Run Control, the jog pad,
   the outputs, the run bar, the Run screen's controls, the Machine sections and the Properties
   sections. When a panel does not fit, it first turns **compact** (smaller paddings), then
   **folds** its secondary sections, lowest priority first. A section the user opened stays open.
   *Lists* (item views, logs, card lists marked with `responsive.mark_list`) may scroll vertically.
   A form that the user turns into a list (the technology form with *Show all parameters* or a
   search: every matching parameter) marks itself as one while it is. A section the user opens
   that is taller than the panel stays open and the panel scrolls (`scroll_overflow`); the
   default layout never does.
3. **STOP, Hold, Start and Arm are never folded, moved, scrolled away or shrunk.** They keep at
   least 56 px (`responsive.TARGET`) in every user mode and theme (`responsive.keep_min_size`).
   Other touch controls on the Run screen keep at least 44 px.
4. **Recovery.** A layout depends only on the size, the content and the user's open/closed
   choices. A panel that is shrunk and then grown again looks the same as before.
5. **No fixed pixel sizes** except touch-target minimums and icon sizes. Widths that depend on
   the content (a list as wide as its longest entry, a status label as wide as "00:00:00") are
   computed from the font metrics.
6. **One scroll container per page.** The shell wraps every page in a `FitArea`. It scrolls only
   when the screen is smaller than what the page needs.

## Helpers (`mlaser/ui/responsive.py`)

| Helper | Use |
|---|---|
| `FlowLayout(uniform=…, justify=…)` | Buttons that wrap to the next row. `uniform=True` gives an auto-fit grid of equal columns. It reports `heightForWidth`. |
| `ResponsiveForm` | Puts the label beside the field while the widest label plus the widest field minimum fit, and above it otherwise. `addRow(widget)` spans both columns; `set_row_visible(field, on)` hides a row (it takes no room). |
| `CollapsibleSection(title, body, collapsible, priority, fit_title=False)` | A titled section. The header is a button with an arrow, so keyboard and screen readers can use it. `fit_title`: a long title ends in "…" in a narrow panel (full text in the tooltip), and a folded section shows `set_summary(text)` after its title ("in: Line 3 mm · out: None"). `open_by_user()`: open it as the user would ("Edit in …"). |
| `ElidedToolButton` | A tool button whose text ends in "…" when it is narrower than the text (full text in the tooltip). Section titles, the source chip of an inherited value. |
| `ColumnsPanel(max_columns, grow=…, scroll_overflow=…)` | Arranges sections in 1–3 columns. Default: as many columns as the width allows. `grow=True`: one column until the height runs out, then more columns, asking the parent for the width. `add_pinned` widgets (STOP) are always shown in full. `column_min`: a callable giving the width a column needs before a second one is used (forms pass the width their labels need beside the fields). `scroll_overflow=True` (a panel in a scroll area: Properties, the technology form): the section the user opened last is never folded again, what does not fit then scrolls; its minimum is the folded height at its width (headers side by side when wide), and after folding it reopens the sections that still fit (`refill`). `fold=False`: nothing folds (a list). |
| `CompactWhenNarrow`, `set_compact` | Smaller paddings, and icon-only `compactable` tool buttons whose text moves to the tooltip. |
| `ColumnFitter(view, priorities, stretch)` | Tables and trees: hides low-priority columns while the view is narrow and shows them again when it widens. |
| `FitArea` | The shell's page wrapper. It takes the page's minimum (height-for-width aware) as its own, capped at 85 % of the screen. |
| `CurrentPageStack` | A stacked widget sized by its *current* page. |
| `keep_min_size(widget, size)` | Keeps a minimum size when a style sheet's `min-height` rule stops matching. |
| `columns_for(width, min_column, spacing, max_columns)` | The number of columns that fit. |

## Docks (`mlaser/ui/shell/dock_policy.py`)

* The side docks run the full height of the window. The log dock sits under the page only.
* A dock's width is either the width the user dragged it to (remembered, and restored from the
  saved layout) or its content's `sizeHint`. It is never less than the content's minimum.
* When the window shrinks, the docks give way in proportion, down to their minimums. When the
  window grows again, they return to their remembered or natural widths.
* The canvas keeps `CANVAS_MIN` (480 px), because the drawing has to stay workable. Docks are
  never moved between areas automatically. Earlier versions did this, and it left stale tab bars
  drawn over the panels.
* On a workspace switch, the docks that the new workspace does not use are hidden before the
  page changes, and its own docks are shown after. The window's minimum never combines the old
  docks with the new page.

## Window size

* **Minimum:** whatever the visible content needs. It is not a constant. Measured at the default
  Linux font (DejaVu Sans 9 pt, 96 dpi, Fusion):

  | Workspace | Minimum (logical px) |
  |---|---|
  | Design | 939 × 446 |
  | Process (no drawing / a drawing) | 881 × 641 / 881 × 718 |
  | Process › Library | 881 × 644 |
  | Nest | 881 × 765 |
  | Run | 881 × 658 |
  | Report | 968 × 716 |
  | Machine | 881 × 564 |

  (Design's minimum grew from 404 when the layer legend's swatches moved to the responsive
  `FlowLayout`, whose minimum counts the rows it wraps to.)

  A minimum height can grow when the window gets narrower, because rows that wrap need more
  height. The page wrapper reports this and the window follows.
  The run bar sets the floor of the width: every button shows its full text and nothing goes to
  an overflow menu. The rail sets the floor of the height: it switches to icon-only buttons
  before any workspace can disappear. A larger font raises these minimums. The harness
  (`test_minimum_size_fits_the_smallest_sample`) keeps Design within 1280 × 800.
* **First start:** the whole available screen, without the menu bar and Dock or taskbar.
  **Later starts:** the saved geometry (`shell.geometry`), clamped to the current screen
  (`Shell.fit_to_screen`, called by `mlaser.ui.__main__`).

## Layout harness

`tests/test_responsive_layout.py` opens the real window (`tests/wiring.py`, with the Onshape
plugin when it is installed) and runs `tests/layout_audit.py` on every view: each workspace,
each tab of a tabbed workspace, each dock of a tabbed dock group, and each Machine section.

* sample sizes 1280×800, 1440×900, 1512×875, 1800×1169 and 1920×1080 (these are sample points,
  not design targets);
* a sweep of widths from the window minimum to 2560 px in 40 px steps
  (`MLASER_LAYOUT_STEP`), at several heights;
* a sweep of the side docks from their minimum width to wide;
* live resizing: shrink from 2560 px to the minimum and grow back. The layout must end up as it
  started;
* a 125 % font, imperial display units (longer values), and 2× scale on a 1512 × 982 point screen
  (a MacBook Pro 14"), which also checks the first-start and saved geometry;
* on a window server (`QT_QPA_PLATFORM=cocoa`, `windows` or `xcb`), `test_fits_this_screen`:
  Design, Run and Machine fit the real screen with the page wrapper's real screen cap. The CI
  runners have 1024 × 768 screens;
* STOP, Hold and Arm inside the window at 56 px in every workspace, size and theme
  (`tests/safety/test_touch_targets_mode_switch.py` repeats this across user-mode switches).

The audit (`tests/layout_audit.py`) reports four kinds of violation: `hscroll`, `vscroll` (on
a form or control panel), `clipped` (smaller than its minimum, text cut, or cut by a parent),
and `overlap` (siblings drawn over each other).

`MLASER_LAYOUT_SHOTS=<dir>` writes the reports (`*.json`, `*.md`) and screenshots of every view
at every sample size. `MLASER_LAYOUT_STYLE=<style>` runs under a Qt style: `macos`,
`windows11`, `windowsvista` or `fusion`. The platform's native styles are only available on
that platform, so `.github/workflows/native-check.yml` runs the harness on the macOS (`macOS`
style, cocoa), Windows (`windows11`, windows) and Linux (Fusion, X11 under Xvfb) runners. The
reports and screenshots are in the `native-<os>` artifacts, under `shots/layout/<os>`.

Violations in panels that another track owns are listed in `PENDING` with their owner. They
are reported but do not fail the test. `test_pending_still_needed` removes an entry once its
violations are gone. `PENDING` is empty: every violation fails.

`test_panels_with_a_selection` audits the panels that follow the selection with one: the
Properties dock in Design and in Process (also with a contour of a multi-contour part picked),
the layer inspector with a layer chosen and the Process › Library page, light and dark, at every
sample size; the window must keep the sample size (the Process page does not push it taller).
A checkable button with `responsive_views` in an exclusive group (Process *Drawing | Library*)
is visited as a view too.
