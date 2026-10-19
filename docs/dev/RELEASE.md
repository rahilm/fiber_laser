# Release

## Version

The version is `[project] version` in `pyproject.toml`. Tag releases `vX.Y.Z` on the main
branch after the contract in [Testing](TESTING.md) is green on CI.

## Packages

* **Wheels:** platform wheels with the C emitter kernel (`mlaser._native._emitter`,
  [NATIVE_KERNEL.md](NATIVE_KERNEL.md)), built by cibuildwheel in the `wheels` job of
  `.github/workflows/package.yml` (`[tool.cibuildwheel]` in pyproject.toml): CPython 3.12 and
  3.13 on manylinux x86_64 / aarch64, Windows AMD64, macOS arm64 (Apple silicon only; there is
  no Intel macOS build). Each wheel is installed in a clean environment and must report the
  kernel active after its self-check (`python -m mlaser._native --require`), and its machine
  code must hold no fused multiply-add (`tools/build_native.py --check --installed`). The
  Linux wheels are built in the manylinux_2_28 container and tested on the Ubuntu 24.04 runner
  (PySide6 6.12 needs glibc 2.34 / 2.39, which the build container does not have). Locally: `MLASER_BUILD_NATIVE=1 uv build` (one wheel
  for the running interpreter and platform). The `build_py` hook (`tools/help_build.py`, named
  in `[tool.setuptools.cmdclass]`) copies the help documents into the wheel as
  `mlaser/help/docs`.
* **sdist:** `uv build --sdist`; `MANIFEST.in` puts `docs/`, `setup.py` and the kernel's C
  source into it. Installed without a C compiler it gives mlaser without the kernel (the Python
  port runs; checked by the `sdist` job with `CC=/bin/false`).
* **One-folder bundles** for Windows, macOS and Linux: PyInstaller, see
  [packaging/README.md](../../packaging/README.md), job `bundle` of package.yml. The bundle
  carries the C kernel (built with `MLASER_BUILD_NATIVE=1`), the help documents
  (`bundle_config.help_datas`), the icons, the catalogue, the parameter registry and the
  bundled fonts; `--bundle-check` verifies them in the frozen program, with
  `MLASER_NATIVE=1` in the smoke test so a bundle whose kernel is missing or fails its
  self-check is refused.
* **Signing:** `sign_windows.ps1` / `sign_macos.sh` run only when their secrets are configured
  (`HAVE_WIN_SIGNING`, `HAVE_MAC_SIGNING`); without them the steps are skipped and the
  artifacts are unsigned. Wheels are not signed.

All of these are uploaded as workflow artifacts (14 days): `wheels-<runner>`, `sdist`,
`mlaser-<OS>` (archive + `build-info.json`). Publishing to PyPI is not automated.

## Release page

The owner publishes; the assets come from a green Package run on main (workflow_dispatch:
`gh workflow run package.yml -R rahilm/fiber_laser --ref main`):

```sh
run=<id of the green Package run on main>
gh run download $run -R rahilm/fiber_laser -D assets/
mkdir upload
cp assets/wheels-*/*.whl assets/sdist/*.tar.gz upload/
for os in Linux Windows macOS; do
  cp assets/mlaser-$os/mlaser-*.zip assets/mlaser-$os/mlaser-*.tar.gz upload/ 2>/dev/null
  cp assets/mlaser-$os/build-info.json upload/build-info-$os.json
done
gh release create vX.Y.Z-rcN --draft --prerelease --target main -t "mlaser X.Y.Z rcN" \
  --notes-file notes.md upload/*
```

A draft release creates its tag only when it is published. Assets: 8 wheels (cp312 / cp313 x
manylinux x86_64, manylinux aarch64, win_amd64, macosx_11_0_arm64), the sdist, the Windows
(`.zip`), Linux and macOS (`.tar.gz`, the `.app` inside) one-folder bundles and their
`build-info-<OS>.json` (bundled distributions, sizes, LGPL check, smoke-test output). Without
the signing secrets the bundles are unsigned (say so in the notes: SmartScreen on Windows,
right-click > Open on macOS).

## CI runners and minutes

The repository is private, so every Actions minute counts against the plan, macOS minutes at
10x and Windows at 2x the Linux rate (GitHub's multipliers for private repositories).
Measured 2026-10-19:

* `ci.yml`, per push: 2 Linux jobs (~7 min each), 2 Windows jobs (~10-18 min), 2 macOS arm64
  jobs (~10-12 min, the `macos-latest` runners are Apple M2 Pro VMs), lint and architecture
  (< 1 min). The macOS jobs are most of the billed cost (~200+ Linux-equivalent minutes per push).
* `package.yml`: wheels (Linux x86_64 and the `ubuntu-24.04-arm` runner, Windows, macOS
  arm64; ~1-4 min each), sdist (< 1 min), bundles (Linux on ubuntu-22.04, Windows, macOS;
  ~2-4 min each). It runs on `v*` tags, on pull requests that touch the packaging inputs and on
  workflow_dispatch only.
* The arm64 Linux runner (`ubuntu-24.04-arm`) is available to this private repository (it ran
  the aarch64 wheels natively; no QEMU needed). No Intel macOS runner is used.

## Checklist

1. Contract green (goldens on 3.12 with `MLASER_NATIVE=1` and `=0`, `tools/native_parity.py`
   on every OS, default suite, safety, command coverage, imports, mypy, docs links).
2. `docs/ROADMAP.md` ticked, user guide updated, screenshots refreshed.
3. `python packaging/build.py --smoke` on each OS (CI job "bundle" of package.yml), signed
   where credentials exist (`sign_windows.ps1`, `sign_macos.sh`); the `wheels` and `sdist`
   jobs green.
4. Licence notices (`LICENSES/` from `gen_licenses.py`) and [ASSETS.md](../ASSETS.md) up to date.
5. Release notes: what changed for operators, any new or changed `ParityProfile`.
