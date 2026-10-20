# Design system

`mlaser/ui/style.py` holds the tokens and a helper for every rule of the UI, so code follows
the rules by calling the helper instead of remembering them. `tests/test_style_lint.py` keeps
the code on them. Layout rules that depend on the space a panel gets are in
[Responsive layout](RESPONSIVE.md); platform rules are in [Native platform](NATIVE.md).

Three principles:

1. **Native first.** Standard widgets (buttons, inputs, tabs, lists, dialogs) are drawn by the
   platform's style with its palette. Only the app's own domain widgets get style sheets: STOP,
   the run bar, the state banner and chips, the canvas and its overlays, the Run screen.
2. **Content and space decide the layout**, never a screen size.
3. **One way per thing**: one button factory, one form row, one section header, one jog pad,
   one run-button set, one word per concept.

## Tokens

| Token | Value | Use |
|---|---|---|
| `SP0`..`SP5` | 0, 4, 8, 16, 24, 32 px | spacing on an 8 px grid; `style.margins(w, "panel" / "dialog" / "dense" / "dock" / "tight" / "none")` |
| `ICON_S`, `ICON_M`, `ICON_L` | 16, 20, 28 px | menus and inline / toolbars and buttons / touch, rail, Run screen |
| `TARGET`, `TARGET_TOUCH`, `TARGET_RUN` | 32, 44, 56 px | mouse minimum / touch (jog pad, outputs, wizards) / run bar and Run screen (STOP wider) |

`responsive.TARGET` / `TARGET_MIN`, `safety_ui.MIN_TARGET`, `workspaces.TOUCH` / `TOUCH_MIN` and
`wizards.common.MIN_TARGET` are these constants.

**Type scale** (`style.font(role)`, `style.set_font(widget, role)`): sizes are ratios of the
platform font (SF 13 pt on macOS, Segoe UI 9 pt on Windows, the desktop font on Linux), never an
absolute `pt`, because a macOS point is about 25 % smaller than a Windows or Linux one and
because a user who enlarges the system font should get a larger UI.

| Role | Size | Weight |
|---|---|---|
| `caption` | 0.85× | regular, muted colour (`style.caption()`) |
| `body` | 1.0× | regular |
| `label_strong` | 1.0× | semibold |
| `title` | 1.25× | semibold |
| `display` | 1.6× | bold |
| `display_run` | 2.0× | bold (the Run screen's state) |

**Case:** sentence case everywhere. Capitals only for the state pill words (READY, ARMED,
FAULT...) and STOP. `style.menu_text()` gives Title Case for macOS menus only.

**Colour roles** (`style.color(role)`, `style.css(role)`): every theme in
`mlaser/ui/shell/theme.py` defines `danger`, `warning`, `info`, `success`, each with a `*_fill`
and an `on_*` text colour (at least 4.5:1, checked by `tests/test_style.py`), plus `muted`,
`text`, `border` and the palette tokens. `danger_fill` (STOP) is the same in every theme.
The machine and safety states (FAULT, ALARM, ARMED, DRY-RUN, SIMULATOR, DISARMED, OFFLINE,
READY, RUNNING, HOLD) have one fixed colour table, `mlaser.ui.safety_ui.COLORS`
(`style.state_colors(kind)`), read by the banner, the chips, the run bar's pill and the Run
screen; every state is also written out, never shown by colour alone.

A domain widget's style sheet is built from roles with `style.styled(widget, build)`, which
rebuilds it when the theme changes; `style.chip(label, role)` is a notice chip.

## Buttons

| Kind | Helper | When |
|---|---|---|
| Primary | `style.primary_button(text)` | the one action a dialog or card exists for |
| Secondary | `style.button(text)` | everything else |
| Danger | `style.danger_button(text)` | Delete, Reset, Disarm (text in the danger colour; STOP is the only filled red button, and the wizard's Abort, which stops the machine like STOP) |
| Tool | `style.tool_button(text, icon)` | icon only, in toolbars and dense rows; the text is the accessible name and the tooltip |
| Run | `style.run_button(kind, tr)` | Start, Hold/Continue, STOP, Frame, Dry run, Arm laser: the run bar and the Run screen |

A button gets an icon if and only if every button of its group has one and the icons differ;
inside a text row every button is text. Buttons in a row are equal width (a choice set) or
content width aligned to the trailing edge (dialog actions), never a mix
(`style.button_row`). Dialog actions always come from `QDialogButtonBox`.

A disabled control says why: `style.set_enabled(widget, ok, reason)` puts the reason in its
tooltip and restores the normal tooltip when it is enabled again.

## Forms, sections, dialogs, tables

* `style.form(parent)` is a `QFormLayout` (`style.Form`) with the platform's label alignment,
  SP2 rows, and labels that move above their fields when the form is narrower than its widest
  label plus field.
* `style.form_row(form, label, field, unit_kind=..., hint=..., disabled_reason=...)`: the unit
  is in the field's suffix (`units_qt.QuantitySpinBox`), never also in the label; the label
  takes the platform's colon (Windows, Linux) or none (macOS).
* `style.section(title)`: a `label_strong` heading and an inset body, no group-box frame.
* `style.Dialog` / `style.dialog(...)`: the one dialog frame: window title, optional heading and
  explanation, the body (`dialog.body`, `dialog.form()`), a `QDialogButtonBox` in the platform's
  order, SP3 margins, size from the content, a window-modal sheet on macOS.
* `style.table(columns)`: a stretched key column that elides, the others hidden by priority when
  narrow.

## The jog pad and the run controls

`mlaser/ui/jogpad.py` is the one jog pad of the Run Control dock, the Machine workspace and the
wizards: the original's cross (Y+ top, X− left, X+ right, Y− bottom, Z+/Z− in a column on the
right), every cell the same touch square with its icon and axis, **Go to origin** (program zero)
in the centre where the page has no other Go-to-origin control, **Home** (the reference
switches) only in its own row below, one step choice (Continuous · 0.1 · 1 · 10 mm, inch steps
in imperial) and Fast, hold-to-jog with the dead-man.

The run bar and the Run screen build their controls with `style.run_button`, and their enable
rules and reasons come from `safety_ui.run_enabled`.

## The lint

`tests/test_style_lint.py` scans `mlaser/ui` and the bundled plugin UI for colour literals
(outside `shell/theme.py`, `style.py` and the safety colour table between the
`# style-lint: colour table` and `# style-lint: end` comments), absolute font sizes, and literal
layout margins and spacings other than 0. Files not migrated yet are in its `ALLOWLIST` with
their counts, which may only go down.
