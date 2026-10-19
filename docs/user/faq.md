# FAQ

## Has mlaser been tested on a real machine?

Not yet. The card protocol is reverse-engineered and checked against the original program under
Wine and against the simulator. Follow [Safety](safety.md) for the first sessions.

## Can I go back to the original Mlaser?

Yes. `.chf` drawings and `.nestproj` nest projects are written exactly the way the original
writes them, and mlaser does not change the original's install.

## Why is the interface in English only?

mlaser ships its own English texts. Other languages come from your own Mlaser install
([Importing your Mlaser setup](importing-mlaser-setup.md#languages)).

## Why does the text in my drawing look different from the original?

The original draws text with the Windows font SimSun, which differs between computers and is
not available on Linux or macOS. mlaser uses its bundled Noto Sans SC font, so text is the same
everywhere, but it is not identical to SimSun outlines.

## Where are my settings and logs?

In the data folder: `~/.mlaser/`, or the folder in `$MLASER_DATA_DIR`.

## The help is empty

The help reads the documentation that is installed with mlaser. If you run from a source
checkout, it reads the `docs/` folder; `$MLASER_HELP_DIR` points it at another folder.

## How do I report a problem?

Attach the device report (**System Analysis → Device report**) and the log files from the data
folder.
