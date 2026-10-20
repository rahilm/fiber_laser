# Cutting strategy: lead-ins, tipping, skeleton and remnant cuts

These options decide *how* a sheet is cut, not what is cut: where each contour starts, whether a
lead-in is safe, what happens to small parts that can tip in the slat bed, and how the skeleton
and the leftover strip leave the machine. **Every option is off until you turn it on**; with all
of them off mlaser cuts exactly as before (the frozen test jobs are bit-identical).

You find them in two places:

* the **Process** workspace, tab **Cutting strategy**: works on the open drawing;
* the **Nest** workspace, **Options → Cutting strategy**: applied to every sheet that goes to
  the queue (and to *Copy sheet to drawing*).

**Where the settings live** (one place for each, [settings.md](settings.md)):

* the **rules** (where starts go, the smallest feature that may carry a start, the lead
  clearance, the tipping strategy) belong to the material: they are part of the technology
  record, edited in **Process › Library**, section *Cutting strategy*. The Cutting strategy tab
  shows the rules of the layer's recipe read-only, with a link to edit them; it keeps the
  *Check*, *Fix* and *Place starts* actions;
* the **bed** (slats) belongs to the machine: **Machine › Setup › Bed (slats)**. The tab shows it
  read-only, with a link there;
* the **kerf** is the layer's (or the part's): the Nest's common line uses it (the cut width is
  twice the kerf compensation; with no kerf on the parts' layers, the default 0.2 mm beam).

## 1. Lead-in collision check

A lead-in pierces in the scrap next to its contour. It is wrong when it

* reaches another contour (a neighbouring part, a hole, a part nested in a hole), so its kerf
  cuts into the other part;
* crosses its own contour (a lead longer than the hole it starts in);
* pierces at the sheet edge (needs a known sheet: nest sheets carry it).

The check uses the toolpath exactly as it is cut: the same start points, kerf compensation and
lead shapes.

| Setting | Default | Meaning |
|---|---|---|
| Clearance to other contours | 0.5 mm | the lead's kerf keeps this distance from another contour (recipe rule `rules.lead_check.clearance_mm`) |
| Kerf | the craft's kerf width, else 0.2 mm | width of the lead's own cut |
| Sheet edge clearance | 2 mm | the pierce point keeps this distance from the sheet edge |
| Shortest lead | 0.5 mm | a lead that would be shorter is flagged instead |

**Check** (Process, `cam.lead_check`) lists the colliding lead-ins and marks them: they are drawn
**red** on the canvas (the original's lead-check mark; *Clear mark* removes it).

**Fix** (`cam.lead_fix`) repairs each one in this order:

1. **Move the start** to the best other place on the contour (the candidates of the lead
   placement below: corners, long straight segments, then eight evenly spaced points). The first
   place whose lead is clear wins; the lead keeps its length.
2. **Shorten the lead**, with the original program's rule: the new length is the distance from
   the contour to the first conflict along the lead, **× 0.7 when the conflict is another
   contour or the sheet edge and the lead is not a straight line** (an arc lead reaches further
   sideways than its length). Repeated (× 0.7) until it is clear.
3. **Flag it**: it stays red and the length is kept. Change it by hand (Properties panel or the
   manual lead tool).

A fixed lead loses its red mark. Every fix is one undo step.

The **pre-flight check** of a Start or Dry run warns about colliding lead-ins
(`lead_collision`, a warning you acknowledge, not an error). Turn it off with the setting
*Pre-flight: check lead-in collisions* (`cam.preflight_lead_check`, on by default).

## 2. Automatic lead placement

**Place starts** (`cam.lead_place`) puts the start of every contour that has a lead-in on the best
candidate, then runs the collision fix.

| Rule | Default | Meaning |
|---|---|---|
| Start on | automatic | *automatic*: a corner when the contour has a sharp corner, else the longest straight segment; *corners*; *long straight segments* |
| Sharp corner | ≤ 100° | corners at most this angle count as sharp |
| Avoid features shorter than | 5 mm | segments shorter than this (small arcs, notches, flattened curves) never carry a start, and a corner needs this length on both sides |
| Position on a straight | middle, 2 mm from its ends | |
| Side | any | *left / right / bottom / top*: starts on that side of the part first |
| Finished edges | none | segments listed in the contour's `finished_edges` (indices) carry no start; the start goes to the side away from them |

A start in a corner hides the pierce mark in the corner; a start on a long straight segment
keeps it away from small features where the heat would collect.

**Per material.** *Start on* and *Avoid features shorter than* are rules of the technology
(Process › Library › Cutting strategy; `rules.lead_place` in the technology database); with
`enabled` the starts are also placed when the recipe is assigned, for example

```json
"rules": {"lead_place": {"enabled": true, "prefer": "corner", "min_feature_mm": 3,
                         "corner_max_angle_deg": 120}}
```

## 3. Tipping on the slats

When the last cut frees a part (or a *slug*, the inside of a hole), it rests on the slats of the
bed. If it rests on one slat only, or its support does not surround its centre of gravity, it
tips, stands up out of the sheet and the head hits it.

**The bed model** (machine settings `bed.*`, edited in **Machine › Setup › Bed (slats)**; the
*Tipping* group of the Process *Cutting strategy* tab shows it):

| Setting | Default | Meaning |
|---|---|---|
| `bed.slat_pitch` | 100 mm | slat pitch, centre to centre |
| `bed.slat_direction` | Y | the slats run along Y (spaced along X), or along X |
| `bed.slat_offset` | -1 (unknown) | where one slat centre is in sheet coordinates; unknown = every position of the sheet is tried and the worst one counts |
| `bed.slat_width` | 3 mm | slat (tooth) width |
| `bed.tip_min_size` | 10 mm | smaller pieces are ignored (they fall through or are too small to matter) |
| `bed.stability_margin` | 2 mm | the centre of gravity must lie this far inside the support |

**Check** (`cam.tip_check`) lists every piece that can tip. Pieces with micro-joints stay in the
skeleton and are not at risk; a piece that fits between two slats falls through.

**Fix** (`cam.tip_fix`) applies the strategy:

* **micro-joint on the heavy side** (default): one joint (0.5 mm) on the contour on the side of
  the centre of gravity, away from the lead-in. The piece stays in the sheet; break it out
  after the sheet.
* **cut last, rapids around**: the part is cut after every other part, and the rapids after it
  go around it.
* **rapids around only**: the order stays; the rapids go around the piece once it is cut.

Rapids always lift the head first. The rapid avoidance is the one of *Avoid cut holes* (cut holes
are avoided too when that is on).

The strategy is a rule of the material: *Tipping on the slats* in Process › Library › Cutting
strategy (`rules.tip` in the technology, `{"strategy": "last"}` for example; thin sheets tip more
easily than thick ones). The Nest uses the rule of the recipe of the job's layer. *Pre-flight: check tipping on the slats*
(`cam.preflight_tip_check`, off by default) adds a `tip_risk` warning to the pre-flight.

## 4. Skeleton cut-up

After the parts are cut, the skeleton is one large, sharp piece. The cut-up lines cut it into
pieces you can lift out safely.

| Setting | Default | Meaning |
|---|---|---|
| Skeleton cut-up | off | *grid*: lines at a fixed pitch; *in the gaps*: lines moved into the free columns and rows between the parts (fewer, longer lines) |
| Pitch | 500 mm | the largest scrap piece |
| Clearance | 0 mm | extra distance from the parts; with 0 a line ends in the part's kerf, so the piece comes free without touching the part |
| Shortest line | 1 mm | shorter pieces of line are dropped |

The lines are cut **last**, on the layer of the parts, without leads. A line that ends inside
the sheet starts there, in a kerf that is already cut: no pierce in solid material. A line from
sheet edge to sheet edge is pierced in its middle and cut outwards, so no line starts off the
sheet.

## 5. Remnant separation cut

A straight cut across the whole sheet along the inner edge of the leftover strip (the gravity
side's remnant, at the part spacing from the parts). It frees the remnant, which then goes to
the remnant store as it was cut (not the staircase estimate) when the sheet is finished.

| Setting | Default | Meaning |
|---|---|---|
| Remnant separation cut | off | |
| Narrowest remnant | 100 mm | a narrower strip gets no separation cut |

It is cut after the parts and before the skeleton cut-up (so the cut-up lines can start in its
kerf). It is pierced in the middle of the sheet and cut out to both edges.

Sheets with cut-up or separation lines, or a tip-avoiding order, are queued as `.mlz` project
files (a `.chf` file cannot hold the cut order).

## 6. For developers

`mlaser.cam.collision` (`analyse`, `fix`), `mlaser.cam.leadplace` (`LeadRules`, `candidates`,
`place_leads`), `mlaser.cam.tipping` (`BedModel`, `analyse`, `apply`), `mlaser.nest.skeleton`
(`strategy_lines`, `sheet_stats`) and `mlaser.services.nesting.sheet_document`. The toolpath reads
two document tags: `Part.extra['cut_phase']` (0 parts, 1 tip-risk parts, 2 separation cut, 3
cut-up; a stable reorder that keeps holes before their part) and `Part.extra['tip_risk']` /
`Contour.extra['tip_risk']` (rapid avoidance). Tests: `tests/test_cutting_strategy.py`,
`tests/test_ws_cutting_strategy.py`.
