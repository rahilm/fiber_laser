# Importing your Mlaser setup

If your machine was run with the original Mlaser program, mlaser can take over its machine
parameters, its translations and its drawings. Nothing is changed in the Mlaser install.

## Machine parameters (`dat` folder)

The original keeps the machine description in `<Mlaser>/dat/HardPara.xml`, `ManuPara.xml` (and
the older `SystemPara.xml`): axis scale (pulses per revolution and lead), travel, speeds, IO
wiring, the follower and the gas.

* `ConfigService.import_dat("<Mlaser>/dat")` (`mlaser.services.config`) takes every value that
  differs from the default as one of your settings and switches the machine profile to it. A
  button for it belongs to the [Machine](machine.md) workspace (Wave 3).
* `Machine.from_dat_dir("<Mlaser>/dat")` (`mlaser.nc`) builds a machine for compiling jobs from
  the same files.

After the import, check the step scale with a short jog in dry-run mode (see [Safety](safety.md)).
`export_dat` writes the three XML files back in the original's format.

## Card address

The original stores the card's IP address in `ipAdd.ini`; pass it with
`mlaser --real --ini <Mlaser>/ipAdd.ini`. The card normally lives on 10.1.1.x; your network
adapter needs an address in that subnet.

## Languages

mlaser ships English. To use the original's translations (Simplified Chinese, Russian, German,
Spanish, Portuguese, French, Italian, Polish, Vietnamese, Turkish):

* in the app: **File → Import Language Files…**, then pick the language at the top right;
* on the command line: `mlaser --import-lang "C:/Program Files/Mlaser" --lang zh`.

The texts are copied once into the data folder; afterwards the install is no longer needed.

## Drawings and nest projects

`.chf` drawings and `.nestproj` nest projects open directly and are saved byte for byte the way
the original writes them, so you can go back and forth between the two programs.

## Layer and process parameters

Layer parameters (speed, power, frequency, gas, height, pierce stages) are stored in the `.chf`
drawing and in the original's parameter files, so they come along with the drawing and the
`dat` import. Check every layer before the first real cut.
