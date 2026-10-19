# Parity

mlaser keeps the original program's behaviour available bit for bit, so an owner can check the
rebuild against their own machine, while new behaviour can evolve next to it.

## The parity fence

The exact ports of the original (planner, smoother, quantisation, NC encoder, sort modes, start
point, CircleFit, DXF import rules, the `.chf` codec) live in `mlaser/parity_ports/`. They
change only together with a Wine parity re-run. Everything else must keep the goldens bit
identical ([Testing](TESTING.md)) or declare a new profile. See
[Architecture §3](ARCHITECTURE.md#3-the-parity-fence-mlaserparity_ports).

## Profiles

A `ParityProfile` selects the implementation of each slot. `"original"` (the default) is the
bit-exact one (its emitter loop runs on the C kernel when that is built and passes its
self-check, else on the Python port: bit-identical, [NATIVE_KERNEL.md](NATIVE_KERNEL.md));
`"original-python"`, `"modern-ortools"`, `"modern-ruckig"`, `"modern-dxf"` and
`"native-sampling"` swap single slots (`use_profile(name)`).

## The Wine harness

The original binaries run under 32-bit Wine in a loopback-only network namespace, unpatched
with their licence checks intact: DLL-level comparisons (motion, CAD, NC, DXF) and app-level
scenarios driven with xdotool. `MLASER_REBUILD` points the harness at the checkout under test.
The full report, the accepted differences and how to verify against your own machine are in
the [parity notes](../re/parity.md), in particular
[§8 Verifying against your machine](../re/parity.md#8-verifying-against-your-machine).
