# Importing your Mlaser setup

If your machine was run with the original Mlaser program, mlaser can take over its machine
parameters, its translations and its drawings. Nothing is changed in the Mlaser install.

## Machine parameters (`dat` folder)

The original keeps the machine description in `<Mlaser>/dat/HardPara.xml`, `ManuPara.xml` (and
the older `SystemPara.xml`): axis scale (pulses per revolution and lead), travel, speeds, IO
wiring, the follower and the gas.

In the app there is one import, the same in every user mode: **File › Parameters › Import
parameters from an Mlaser installation…** (or **Machine › Import from my Mlaser installation**,
or *Import parameters…* on the welcome sheet).
Choose the `dat` folder (or the install folder and `%LOCALAPPDATA%\NexCut`); then:

1. **Preview.** Every value that would change is listed with its current and its new value,
   grouped by area. The values that change how the machine moves (travel limits, axis scaling,
   homing, limit switches, axis direction) come first, in bold, with the word in the *Check*
   column. Untick an area to leave it out. Nothing has changed yet.
2. **Apply changes** asks first (Cancel is the default) and names how many of the changes affect
   motion; *Show Details* lists them old → new.
3. Before anything is written, the current configuration is saved next to it as
   `config.json.<date>-<time>.bak`.
4. If the motion card is connected, it keeps the old values until it reconnects: **Reconnect
   now** appears for that.

**Compare with backup or file…** in the same window lists the differences between the current
values and a backup (or another mlaser `config.json`); **Restore these values** takes them back
the same way. **Apply** on the Machine workspace's *Setup* page asks, backs up and offers the
reconnect the same way.

For developers' scripts (no preview, no backup): `ConfigService.import_dat` and
`Machine.from_dat_dir` in the [developer docs](../dev/ARCHITECTURE.md). Use the menu entry above
on a machine you cut with.

After the import, check the step scale with a short jog in dry-run mode (see [Safety](safety.md)).

**Back up / restore parameters** (**File › Parameters**) are separate: *Back up parameters to the motion
card…* / *to a file…* store this PC's parameters there; *Restore parameters from the motion
card…* / *from a file…* replace this PC's parameters with that copy. A restore asks first (Cancel
the default) and saves the current parameters as `File/params-before-restore-<date>-<time>.xml`.

## Card address

The original stores the card's IP address in `ipAdd.ini`, with its port (`CardPort`), the link
(`AccessType`: UDP or TCP) and the reply timeouts.  The import above (**Machine › Import from my
Mlaser installation**) stores all of them in the configuration (`ip` in `config.json`), together with the follower and pierce
timeouts and `DAMinVal` of the same file (as machine-profile values).  From then on `mlaser --real`,
Reconnect, the Machine workspace's connection page and the network self-test all use that one
address; `--host`/`--port` on the command line override it for one start, and
`mlaser --real --ini <Mlaser>/ipAdd.ini` still reads a file directly.  The card normally lives on
10.1.1.x; your network adapter needs an address in that subnet.

## Languages

mlaser ships English. To use the original's translations (Simplified Chinese, Russian, German,
Spanish, Portuguese, French, Italian, Polish, Vietnamese, Turkish):

* in the app: **File › Parameters › Import language files…** (or *Import languages…* on the
  welcome sheet), then pick the language at the top right;
* on the command line: `mlaser --import-lang "C:/Program Files/Mlaser" --lang zh`.

The texts are copied once into `~/.mlaser/lang/` (or `lang/` in `$MLASER_DATA_DIR`); afterwards
the install is no longer needed.

## Drawings and nest projects

`.chf` drawings and `.nestproj` nest projects open directly and are saved byte for byte the way
the original writes them, so you can go back and forth between the two programs.

## Layer and process parameters

Layer parameters (speed, power, frequency, gas, height, pierce stages) are stored in the `.chf`
drawing and in the original's parameter files, so they come along with the drawing and the
`dat` import. Check every layer before the first real cut.
