# Testing

## Suites

```bash
uv run pytest -q                 # default run: unit, ui, sim and safety tests
uv run pytest -q -m unit         # one suite: unit / ui / sim / safety
uv run pytest -q -m golden       # frozen artefacts (about 40 s), Python 3.12 only
uv run pytest -q -m parity       # Wine parity suite (needs the external harness)
uv run pytest -q -m "safety or parity"
```

Markers are assigned by `tests/conftest.py` from the directory and module name, so a new test
file needs no decorator: `parity` (tests/parity/), `golden` (tests/golden/), `safety`
(tests/safety/), `ui` (modules listed in `UI_MODULES`, Qt offscreen), `sim` (modules that start
the UDP card simulator), `unit` (everything else). Qt runs with `QT_QPA_PLATFORM=offscreen`.

## Goldens

`tools/make_goldens.py` writes the frozen artefacts in `tests/golden/` (`.chf` round trips,
random and sample jobs, motion, orders). `tools/make_goldens.py --check` must stay bit
identical on CPython 3.12 after every change outside a declared new `ParityProfile`; regenerate
only after a reviewed behaviour change. 3.13 is checked as information
(`MLASER_GOLDEN_ANY_PYTHON=1`).

## Contract for every commit

* `tools/make_goldens.py --check` bit identical (3.12);
* the default suite green;
* `pytest -m "safety or parity"` green (a timing test may flake on a loaded host: rerun once);
* `tools/command_coverage.py`: no unimplemented, raising or duplicate command;
* `lint-imports` (layer rules, [Architecture](ARCHITECTURE.md)) and `mypy` green;
* `uvx ruff@0.17.1 check` on the changed files;
* `tests/test_docs_links.py`: documentation links resolve.

## Writing tests

* Domain logic is Qt-free and tested as `unit`; widget tests build the window through
  `tests/wiring.py`, the application's real wiring.
* Safety behaviour is tested against the simulator in pessimistic mode
  (`CardSimulator(pessimistic=True)`, `tests/safety/safety_helpers.py`).
* Randomised equivalence tests (hypothesis) compare a new implementation with the reference one
  before the reference is retired.
* Screenshots of new screens: offscreen, at 1280×800 and at 150 % of 1920×1080, light and dark.


## Timing-sensitive tests

The safety suite asserts real-time bounds (stop latency, link-loss detection, motion after a loss).
Two rules keep those assertions meaningful on busy CI or development hosts:

- **The in-process simulator gets its own link deadline.** `connect_simulator()` streams with
  `SIM_LINK_DEADLINE_MS` (1 s) because the simulated card shares the Python process (and GIL)
  with the streamer. A real card always gets the configured deadline (150 ms default), and a test
  proves the simulator value never leaks to a real connection.
- **Tests of the real 150 ms deadline may rerun once.** They are marked `@pytest.mark.flaky(reruns=1)`
  (pytest-rerunfailures). A test that fails twice is a real failure. Bounds are never loosened to
  make a test pass.
