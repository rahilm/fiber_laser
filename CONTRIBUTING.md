# Contributing to mlaser

Thank you for helping. mlaser drives a real fiber laser and a moving gantry, so the rules
below are stricter than for most projects: **safety behaviour is never weakened**, and the
reproduction of the original program stays **bit-exact** unless a change is declared and
reviewed.

Security problems and anything that could fire the laser, move the axes or defeat a guard
unexpectedly are **not** reported in public issues: see [SECURITY.md](SECURITY.md).

## Setup

You need [uv](https://docs.astral.sh/uv/), git and, for the C emitter kernel, a C compiler
(Linux: `build-essential`; macOS: the Xcode command line tools; Windows: Visual Studio Build
Tools). CPython **3.12** is the reference interpreter (`.python-version`): the goldens are
bit-exact on it. 3.13 is supported and tested in CI.

```bash
git clone https://github.com/rahilm/fiber_laser.git && cd fiber_laser
uv sync --locked --all-extras        # .venv with every optional library, the C kernel and the dev tools
uv run python -m mlaser._native      # the C kernel is built and active
pre-commit install                   # optional: ruff on every commit
uv run mlaser --demo                 # the application against the in-process card simulator
```

Qt runs offscreen in the tests (`QT_QPA_PLATFORM=offscreen` is set by `tests/conftest.py`). On
Linux the Qt runtime needs `libegl1 libgl1 libxkbcommon0 libxkbcommon-x11-0 libfontconfig1
libdbus-1-3 libglib2.0-0` (see `.github/workflows/ci.yml`).

Read before changing code: [docs/dev/ARCHITECTURE.md](docs/dev/ARCHITECTURE.md) (package layers,
the parity fence), [docs/SAFETY.md](docs/SAFETY.md), [docs/dev/TESTING.md](docs/dev/TESTING.md)
and, for plugins, [docs/dev/PLUGINS.md](docs/dev/PLUGINS.md).

## The contract (every commit)

Every commit keeps all of these green. CI checks them on Linux, Windows and macOS; run them
locally before you push:

```bash
uv run python tools/make_goldens.py --check        # goldens bit-identical (CPython 3.12)
uv run pytest -q -n auto --dist loadgroup          # the default suite (unit, ui, sim, safety)
uv run pytest -q -m "safety or parity"             # safety (and parity, when the Wine harness is set up)
uv run python tools/command_coverage.py            # 0 unimplemented / 0 raising / 0 duplicate commands
uv run lint-imports                                # layer rules of ARCHITECTURE.md
uv run mypy                                        # the typed core
uv run python tools/i18n_check.py --no-legacy-scan # string catalogue and placeholders
uv run pytest -q tests/test_docs_links.py          # every relative Markdown link and anchor
uvx ruff@0.17.1 check <changed files>
```

* After a change to the emitter loop or `mlaser/_native`: also `MLASER_NATIVE=0 uv run python
  tools/make_goldens.py --check` and `uv run python tools/native_parity.py` (0 differences).
* A timing test may flake on a heavily loaded machine: rerun it once. Only a test that fails
  twice counts. Never loosen a bound to make a test pass.
* New user-visible text goes through the catalogue (`mlaser/i18n/en.json`,
  [docs/dev/i18n.md](docs/dev/i18n.md)). New commands must be bound and must not raise
  (`tools/command_coverage.py`).
* Every bug fix comes with a regression test.

## Parity and bit-exact rules

mlaser reproduces the original program's output bit for bit (the job's FIFO step stream, the
toolpath order, `.chf` bytes, planner and smoother samples), so that an owner can verify it
against their own machine ([docs/dev/PARITY.md](docs/dev/PARITY.md)).

* **`mlaser/parity_ports/`** holds the exact ports of the original. They change only together
  with a Wine parity re-run (`../parity/run_parity.sh`, the maintainer's harness): say in the
  pull request what you changed so a maintainer can run it.
* **Everything else** keeps `tools/make_goldens.py --check` bit-identical on CPython 3.12 with
  the default profile, or puts the new behaviour behind a **new `ParityProfile`** (an opt-in
  slot such as `modern-ortools`), leaving `"original"` untouched.
* "Bit-identical" means exactly that: the same floating-point operations in the same order. A
  refactor that reorders a sum, fuses a multiply-add (`-ffp-contract`), changes a libm call or
  uses numpy where the reference uses Python floats is a behaviour change.
* The goldens are pinned to Linux x86-64, glibc and CPython 3.12. On Windows and macOS they
  differ by 1-ulp libm results, which CI reports but does not fail on
  ([TESTING.md](docs/dev/TESTING.md#goldens-on-windows-and-macos)).

### Regenerating the goldens

`tests/golden/` is regenerated only after a **reviewed behaviour change**: a bug fix that is
meant to change the compiled output, or a deliberate change of the original's behaviour that
the owner accepted. Never regenerate to make a failing check pass.

```bash
uv run python tools/make_goldens.py          # writes tests/golden/ (CPython 3.12, Linux x86-64)
uv run python tools/make_goldens.py --check  # identical afterwards, also with MLASER_NATIVE=0
git diff --stat tests/golden/                # only the records you expect
```

The commit that changes the goldens says why in its message, which records changed and how the
change was verified (for example a Wine re-run, a capture from a real machine, or the reviewed
issue), and declares it with a trailer:

```text
Kerf corner fix changes the toolpath of 4 sample jobs

...why, which records, how verified...

Parity-Change: kerf corner fix (#123); 4 jobs_samples records; Wine re-run 2026-11-02
```

The **parity guard** (`.github/workflows/parity-guard.yml`, `tools/parity_guard.py`) fails a pull
request that changes `tests/golden/` or `mlaser/parity_ports/` unless one of its commits has a
non-empty `Parity-Change:` trailer (in the trailer block at the end of the message, as `git
interpret-trailers` reads it) or a maintainer adds the label **`parity-change`** to the pull
request. Both mean "this change of the frozen output is intended and reviewed"; the label is
for a maintainer's decision after review, the trailer keeps the reason in the history. Run it
locally with `python tools/parity_guard.py --base origin/main --head HEAD`.

## Safety-relevant changes

These paths are safety-relevant: `mlaser/controller/` (supervisor, streamer, interlocks, card
client), `mlaser/machine/` (safety state machine, command policy, pre-flight, lifecycle),
`mlaser/protocol/`, the laser and gas bytes in `mlaser/nc/`, `mlaser/devices/` (laser source,
follower), `mlaser/ui/safety_ui.py` and the run controls. A change there:

* never weakens a guard, a bound, an interlock or the arming rules; off-commands (laser off,
  gas off, halt) always pass the guard;
* comes with tests in `tests/safety/` against the simulator in pessimistic mode
  (`tests/safety/safety_helpers.py`), including the failure path (link loss, fault injection,
  Stop during the action);
* updates [docs/SAFETY.md](docs/SAFETY.md) when the behaviour or a configuration changes;
* says in the pull request whether it was tested on real hardware (so far nothing has been).

[.github/CODEOWNERS](.github/CODEOWNERS) asks the owner (@rahilm) to review the parity fence,
the goldens, the kernel, the NC compiler, the protocol, the controller, the machine safety
layer, `tests/safety/` and docs/SAFETY.md.

## Pull requests

* One topic per pull request, small commits, a clear message (what and why).
* Fill in what you checked: the contract above, goldens unchanged or regenerated (why), parity
  fence touched (Wine re-run requested), safety path touched (tests/safety added), hardware
  tested or not.
* Do not commit machine-specific absolute paths, credentials, vendor files from the original
  install (docs/ASSETS.md) or large binary files (pre-commit refuses files above 1 MB).
* Documentation: user guides in `docs/user/` (shipped as the in-app help), developer notes in
  `docs/dev/`; notable changes in [CHANGELOG.md](CHANGELOG.md) under "Unreleased".

## Branch protection (recommended)

The branch protection of `main` is the owner's setting on GitHub; mlaser does not change it.
The recommended rules (Settings > Branches > Branch protection rules, or a ruleset, for `main`):

* **Require a pull request before merging**, with **Require review from Code Owners** (one
  approval) and dismissal of stale approvals on new commits.
* **Require status checks to pass**, branches up to date before merging. Required checks:
  `ubuntu-latest / Python 3.12` (the reference platform: default suite and goldens),
  `import contracts, types, i18n, docs`, and `parity guard (goldens, parity fence)`. The
  Windows, macOS and 3.13 jobs are recommended as required too once they have been stable.
* **Require linear history**; **do not allow force pushes or deletions**.
* **Include administrators** (or "Do not allow bypassing the above settings"), so the parity
  guard and code-owner review also apply to the owner's own changes; a direct push to `main`
  would bypass the parity guard, which runs on pull requests only.
* Enable **private vulnerability reporting** (Settings > Code security) for SECURITY.md.

See also [docs/dev/RELEASE.md](docs/dev/RELEASE.md).
