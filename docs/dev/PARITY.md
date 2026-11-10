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

## Your own captures

`tools/compare_capture.py` compares the original's job stream in a capture of your machine with
the rebuild's ([parity.md §8](../re/parity.md#8-verifying-against-your-machine)).
`tests/test_capture_replay.py` replays any capture of the card traffic (of the original or of
mlaser) through the production decoder and checks the documented invariants; it reads the folder
named by `MLASER_CAPTURE_DIR` and skips without it ([Testing](TESTING.md#replaying-your-own-captures)).
Captures stay on your machine: the repository contains none. Registers 59500 and 59503 (licence
traffic) are skipped. What the card does with a FIFO frame it receives twice is settled with
`tools/bench_duplicate_frame.py` ([Bench checks](BENCH_CHECKS.md)).
