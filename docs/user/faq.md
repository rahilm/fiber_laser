# FAQ

## Has mlaser been tested on a real machine?

Not yet. The card protocol is reverse-engineered and checked against the original program under
Wine and against the simulator. Follow [Safety](safety.md) for the first sessions.

## Can I go back to the original Mlaser?

Yes. `.chf` drawings and `.nestproj` nest projects are written exactly the way the original
writes them, and mlaser does not change the original's install. Use **Export .chf…** for a
drawing you keep as an mlaser project (`.mlz`); see [Saving your work](design.md#saving-your-work).

## Should I save as .mlz or .chf?

Save your own work as `.mlz`: it keeps everything (cooling points, micro-joint widths, raster
engravings, nest results, the queue). Use `.chf` to exchange drawings with the original program.

## mlaser crashed. Is my work lost?

Usually not. mlaser autosaves the project every two minutes when something changed, and when a
job is prepared. At the next start it offers to recover the last state; **System Recovery** does
the same at any time.

## Why is the interface in English only?

mlaser ships its own English texts. Other languages come from your own Mlaser install
([Importing your Mlaser setup](importing-mlaser-setup.md#languages)).

## Why does the text in my drawing look different from the original?

The original draws text with the Windows font SimSun, which differs between computers and is
not available on Linux or macOS. mlaser uses its bundled Noto Sans SC font, so text is the same
everywhere, but it is not identical to SimSun outlines. If you need the original's lookup (for
example to compare with a drawing made by the original), start mlaser with
`MLASER_TEXT_FONT=system`.

## Where are my settings and logs?

In the data folder: `~/.mlaser/`, or the folder in `$MLASER_DATA_DIR`.

## The help is empty

The help reads the documentation that is installed with mlaser. If you run from a source
checkout, it reads the `docs/` folder; `$MLASER_HELP_DIR` points it at another folder.

## How do I report a problem?

Attach the device report (**System Analysis → Device report**) and the log files from the data
folder.
