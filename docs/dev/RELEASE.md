# Release

## Version

The version is `[project] version` in `pyproject.toml`. Tag releases `vX.Y.Z` on the main
branch after the contract in [Testing](TESTING.md) is green on CI.

## Packages

* **Wheel / sdist:** `uv build`. The `build_py` hook (`tools/help_build.py`, named in
  `[tool.setuptools.cmdclass]`) copies the help documents into the wheel as
  `mlaser/help/docs`; `MANIFEST.in` puts `docs/` into the sdist.
* **One-folder bundles** for Windows, macOS and Linux: PyInstaller, see
  [packaging/README.md](../../packaging/README.md). The bundle carries the help documents
  (`bundle_config.help_datas`), the icons, the catalogue, the parameter registry and the
  bundled fonts; `--bundle-check` verifies them in the frozen program.

## Checklist

1. Contract green (goldens on 3.12, default suite, safety, command coverage, imports, mypy, docs
   links).
2. `docs/ROADMAP.md` ticked, user guide updated, screenshots refreshed.
3. `python packaging/build.py --smoke` on each OS (CI job "package"), signed where credentials
   exist (`sign_windows.ps1`, `sign_macos.sh`).
4. Licence notices (`LICENSES/` from `gen_licenses.py`) and [ASSETS.md](../ASSETS.md) up to date.
5. Release notes: what changed for operators, any new or changed `ParityProfile`.
