# Release

## Version

The version is `[project] version` in `pyproject.toml`. Tag releases `vX.Y.Z` on the main
branch after the contract in [Testing](TESTING.md) is green on CI.

## Packages

* **Wheels:** platform wheels with the C emitter kernel (`mlaser._native._emitter`,
  [NATIVE_KERNEL.md](NATIVE_KERNEL.md)), built by cibuildwheel in the `wheels` job of
  `.github/workflows/package.yml` (`[tool.cibuildwheel]` in pyproject.toml): CPython 3.12 and
  3.13 on manylinux x86_64 / aarch64, Windows AMD64, macOS arm64 / x86_64. Each wheel is
  installed in a clean environment and must report the kernel active after its self-check
  (`python -m mlaser._native --require`). Locally: `MLASER_BUILD_NATIVE=1 uv build` (one wheel
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
`mlaser-<OS>` (archive + `build-info.json`). Publishing to PyPI or a release page is not
automated.

## Checklist

1. Contract green (goldens on 3.12 with `MLASER_NATIVE=1` and `=0`, `tools/native_parity.py`
   on every OS, default suite, safety, command coverage, imports, mypy, docs links).
2. `docs/ROADMAP.md` ticked, user guide updated, screenshots refreshed.
3. `python packaging/build.py --smoke` on each OS (CI job "bundle" of package.yml), signed
   where credentials exist (`sign_windows.ps1`, `sign_macos.sh`); the `wheels` and `sdist`
   jobs green.
4. Licence notices (`LICENSES/` from `gen_licenses.py`) and [ASSETS.md](../ASSETS.md) up to date.
5. Release notes: what changed for operators, any new or changed `ParityProfile`.
