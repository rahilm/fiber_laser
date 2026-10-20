# Labels, job tickets and part-ID marking

Parts from several orders often share one sheet and must be sorted after cutting. mlaser can
print a label for every part, a job ticket for every queued sheet, a label for every remnant,
and etch each part's id into the part itself. All labels are PDF files: print them on any
printer, including label printers that print PDF.

Commands (in the command palette):

| Command | Who | What it does |
|---|---|---|
| Part labels (PDF)... | operator | one label per part copy of the open document |
| Job tickets (PDF)... | operator | one ticket page for the selected queue entry, or for every entry still to run |
| Remnant labels (PDF)... | operator | one label per available remnant |
| Etch part IDs | engineer | adds each part's id as engraved text inside the part (one undo step) |
| Remove part ID marking | engineer | removes the engraved ids again |

## Part labels

A label carries:

* the part id and the copy, e.g. `3 #2/6` (part type 3, copy 2 of the 6 on this sheet);
* the part name;
* the order, customer, reference, part number and due date when the part has them;
* material and thickness (from the queue sheet being cut, else from the technology on the
  part's layer), the sheet size or remnant id and the queue entry;
* the part's size;
* a QR code.

One label is printed per placed copy. A nested part that consists of several drawing parts (an
outline and its holes) is one copy. The sheet outline gets no label.

**Order data.** The order fields are read from the part (`Part.extra["order"]`, a dictionary
with the keys `order`, `customer`, `reference`, `part_number`, `due`) and, for nested parts,
from the nest part's data with the same keys. The order import fills them; a drawing without
order data simply prints the other lines.

### Label sizes

Setting **Part label size** (`plugin.ext_labels.label_size`) and **Remnant label size**
(`plugin.ext_labels.remnant_size`):

| Size | Kind |
|---|---|
| 62 x 29 mm | roll (Brother DK-11209 and similar), one label per page |
| 62 x 100 mm | roll (Brother DK-11202) |
| 57 x 32 mm | roll |
| 100 x 50 mm | roll |
| 102 x 152 mm | roll, 4 x 6 inch shipping labels |
| A4, 3 x 8 labels 70 x 37 mm | label sheets |
| A4, 2 x 7 labels 99.1 x 38.1 mm | label sheets (L7163 layout) |
| A4, 3 x 7 labels 63.5 x 38.1 mm | label sheets (L7160 layout) |

On a roll size each label is one PDF page of exactly the label's size; print it at 100 %
("actual size"), not "fit to page". On the A4 sizes a thin frame marks each label so you can
check the alignment on plain paper first.

Text that does not fit is shortened with `...`. The PDF uses the standard Helvetica font:
characters outside the Western European set (for example Chinese) are printed as `?`.

## Job tickets

A job ticket is one A4 page (setting **Job ticket page size**: A4 or A5) per queue entry:
the entry's name and id, material and thickness, the sheet size or remnant id, the quantity
(and how many sheets are done), the estimated time per sheet, a drawing of the sheet with its
parts, the parts list (`count x name`) and a QR code of the queue entry. Put it on the sheet
stack or the pallet so the operator, the person who sorts the parts, and the person who loads
the next sheet all see which job it is.

## Remnant labels

A remnant label shows the remnant id, its size, material and thickness, its area, the rack
location and date, the job it came from and a QR code. With **Write a remnant label PDF when a
remnant is saved** (`plugin.ext_labels.remnant_auto`) the label of every new remnant is written
at once as `labels/remnant-<id>.pdf` in the data directory; otherwise print them with
*Remnant labels (PDF)...*.

## QR codes and barcode scanners

The QR codes hold plain text, so a USB barcode scanner (which types what it reads, like a
keyboard) reproduces them anywhere:

| Label | QR text |
|---|---|
| part | `mlaser:part:<part id>;n=<copy>;o=<order>;q=<queue entry>` (empty fields are left out) |
| job ticket | `mlaser:queue:<queue entry id>` |
| remnant | `mlaser:remnant:<remnant id>` |

Your own tools (an ERP, a spreadsheet) can read them; they never control the machine.

## Etching part IDs

*Etch part IDs* writes each part's id (its name, else `P` and its number) as single-line
text inside the part, so the parts can be sorted without labels:

* the text uses mlaser's own single-stroke font (letters A-Z, digits and `- _ . / # :`;
  lower case is engraved as upper case, other characters as `?`): the beam traces each stroke
  once;
* it is placed where it fits best: the largest height between **Smallest** and **Largest
  marking text height** (`mark_min_height`, `mark_max_height`, mm) that keeps **Marking
  clearance** (`mark_margin`, mm) from the outline and from every hole; it is turned by 90°
  when the part is tall and narrow;
* a part with no room for the smallest height is left without a mark, and the log says how
  many were skipped; the text never crosses an edge or a hole;
* the strokes go on the **Marking layer** (`mark_layer`, default 10) without lead-in, lead-out
  or kerf compensation. They lie inside the part, so they are engraved before the part's
  outline is cut and the part drops;
* running the command again replaces the earlier marks; *Remove part ID marking* removes them;
  both are one undo step.

The command refuses a marking layer that the drawing already uses for cutting.

### The marking layer's parameters

The marking layer takes its parameters from the technology database: a technology record whose
rule `kind` is `marking` (the same material and thickness first, then the same material, then
any marking record). Create one in the Technology workspace by copying a cutting record and
setting low power, no pierce stage and the gas you engrave with; add the rule
`"kind": "marking"` to it.

Without a marking record mlaser uses **example** values: speed 150 mm/s, peak power 20 %, duty
30 %, 5000 Hz, air at 0.5 bar, cut height 1 mm, no pierce. These are not tested values: the log
says so every time. **Check them on a scrap sheet first**: too much power cuts through instead
of marking, and a thin part may then fall out or tip.

The marked document is cut like any other: arming, the pre-flight check and the Start button
apply as always ([safety](safety.md)).
