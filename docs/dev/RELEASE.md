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
  In a bundle, the optional backends it ships (listed in `mlaser/bundle_manifest.json`, which
  the spec writes) are required too, and each one must solve a small problem: OR-Tools (sort
  mode 20, through the app's own `shortest_path_ortools`), spyrrow, Ruckig, hidapi and pyserial.
  svgelements and the bundled cutting-text fonts are always checked. A missing `ortools.dll`
  now fails the smoke test instead of falling back silently.
* **Signing:** `sign_windows.ps1` / `sign_macos.sh` run only when their secrets are configured
  (`HAVE_WIN_SIGNING`, `HAVE_MAC_SIGNING`); without them the steps are skipped and the
  artifacts are unsigned. Wheels are not signed.

All of these are uploaded as workflow artifacts (14 days): `wheels-<runner>`, `sdist`,
`mlaser-<OS>` (archive + `build-info.json`). Publishing to PyPI is not automated.

## Bundle size

Uncompressed folder and archive size. "Before" is Package run 39318015663 (main @ ae49e02),
measured in the second review. "After" is Package run 39344495395 (main, Review 2 packaging
fixes), from the bundle jobs' logs and the uploaded artifact sizes.

| OS | before | after | change |
|---|---|---|---|
| Linux x86_64 | 360.8 MB, 142.2 MB `.tar.gz`, 863 entries | 276.3 MB, 105.1 MB `.tar.gz` | -23 % folder, -26 % archive (wheel-vendored `*.libs` stay unstripped) |
| Windows amd64 | 195.6 MB, 88.5 MB `.zip`, 570 entries | 213.1 MB, 88.3 MB `.zip` | +9 %: -55 MB of trims, +73 MB for OR-Tools' DLLs, which were missing before |
| macOS arm64 | 203.1 MB, 83.5 MB `.tar.gz`, 1029 entries | 187.2 MB, 77.7 MB `.tar.gz` | -8 % |

What was removed and why (`packaging/bundle_config.py` has the rules and the reasons):

* all OSes: Qt's own translations (7.0 MB; mlaser loads no Qt translator); QtNetwork and the
  TUIO/VNC plugins, which were its only users; the QtDBus binding (on Linux `libQt6DBus`
  itself stays, because `libQt6Gui` links it); the Qt image formats nothing reads through Qt
  (TIFF, WebP, TGA, WBMP; raster import goes through Pillow); Pillow's AVIF codec (5-8 MB; not
  an engrave input format).
* Windows: `opengl32sw.dll` (20.6 MB). This is Qt's Mesa llvmpipe software OpenGL, which Qt
  loads only when the app creates an OpenGL context and the driver has no OpenGL 2. mlaser
  paints with the raster engine (QPainter on QGraphicsView/QImage, no OpenGL viewport) and the
  QtOpenGL bindings are excluded, so it never creates a context, with or without a GPU driver.
  Also removed: PySide6's `libcrypto-3-x64.dll`/`libssl-3-x64.dll` (8.5 MB, only for the Qt
  TLS plugin, which is not shipped) and `qdirect2d`. Added: `ortools/.libs` (73 MB:
  ortools.dll links HiGHS, SCIP, protobuf, abseil and re2, all required).
* Linux: the build host's system libraries (below), the embedded platforms (eglfs, linuxfb,
  Vulkan KHR display, evdev input), `strip --strip-unneeded` outside PySide6/shiboken6
  (40.7 MB), and the second executable (`mlaser-console` is a symlink).
* OR-Tools is the largest optional part (72 MB on Linux and Windows, 59 MB on macOS):
  `python packaging/build.py --slim` leaves it, spyrrow and Ruckig out.

## Linux: glibc floor and target libraries

The Linux bundle needs **glibc 2.34 or newer and libstdc++ with GLIBCXX_3.4.29 (GCC 11) or
newer**, for example Ubuntu 22.04+, Debian 12+, RHEL/Rocky/Alma 9+ or Fedora 35+. The floor
comes from PySide6 6.12's manylinux_2_34 wheels: `libQt6Core.so.6` is the newest requirement
(GLIBC_2.34, GLIBCXX_3.4.29, CXXABI_1.3.13). `build.py` reads the version references of every
ELF file (`symbol_floor` in `build-info.json`), and CI fails when a file needs more than 2.34
(`--glibc-floor 2.34`). The bundle is built on `ubuntu-22.04`, the oldest GitHub runner
(20.04 was retired), so the few host libraries it keeps come from the oldest release. On
22.04 they need glibc 2.14 at most. On a newer host they can need more: Ubuntu 24.04's
`libxcb-cursor.so.0` needs GLIBC_2.38, so a local `--glibc-floor 2.34` build there fails. That
is intended: build the bundles you publish on 22.04.

PyInstaller copies every library the bundled binaries link to, including the host's. Following
PyInstaller (no glibc, GL or libxcb), the manylinux policy (no C/C++ runtime, X11 core, glib)
and Qt's "Qt for X11 Requirements" (the system provides xcb, xkbcommon, fontconfig, freetype
and D-Bus), the bundle drops every library resolved from `/lib`, `/usr/lib` (and `lib64`,
`/usr/local/lib`). Only the X11 helper libraries that a minimal desktop may lack stay:
`libxcb-cursor`, `-icccm`, `-image`, `-keysyms`, `-render-util`, `-util` and
`libxkbcommon-x11` (`HOST_LIB_KEEP`). Any other host library fails the build. So the target
provides, like any desktop that runs a pip-installed PySide6 (Debian/Ubuntu package names):
`libc6 libstdc++6 libgcc-s1 libglib2.0-0 libdbus-1-3 libfontconfig1 libfreetype6 libx11-6
libx11-xcb1 libxcb1 libxkbcommon0 libegl1 libgl1 libzstd1`, plus `libcups2` for printing to a
printer (without it, the print dialog offers only PDF) and `libwayland-client0` on Wayland.
Before this change the bundle shipped the build host's CUPS, gnutls, p11-kit, nettle, krb5,
OpenSSL, systemd and libstdc++.

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

The repository is public, so GitHub's standard runners cost nothing. While it was private
(until 2026-10-19), macOS minutes counted 10x and Windows 2x against the plan, and the
included minutes ran out after a day of CI work. Measured 2026-10-19:

* `ci.yml`, per push: 2 Linux jobs (~5-7 min each), 2 Windows jobs (~8-10 min), 2 macOS arm64
  jobs (~8-10 min; `macos-latest` is an Apple silicon VM whose timers fire late, see
  [TESTING.md](TESTING.md)), lint and architecture (< 1 min). On a private repository the macOS
  jobs are most of the billed cost (~200 Linux-equivalent minutes per push).
* `package.yml`: wheels (Linux x86_64 and the `ubuntu-24.04-arm` runner, Windows, macOS
  arm64; ~1-4 min each), sdist (< 1 min), bundles (Linux on ubuntu-22.04, Windows, macOS;
  ~2-4 min each). It runs on `v*` tags, on pull requests that touch the packaging inputs and on
  workflow_dispatch only.
* The arm64 Linux runner (`ubuntu-24.04-arm`) builds and tests the aarch64 wheels natively (no
  QEMU). No Intel macOS runner is used.

## Checklist

1. Contract green (goldens on 3.12 with `MLASER_NATIVE=1` and `=0`, `tools/native_parity.py`
   on every OS, default suite, safety, command coverage, imports, mypy, docs links).
2. `docs/ROADMAP.md` ticked, user guide updated, screenshots refreshed.
3. `python packaging/build.py --smoke` on each OS (CI job "bundle" of package.yml), signed
   where credentials exist (`sign_windows.ps1`, `sign_macos.sh`); the `wheels` and `sdist`
   jobs green. Each bundle job's summary shows the bundle check: every shipped backend `ok`,
   in particular `OR-Tools (sort mode 20)` on Windows. It also shows the sizes (update "Bundle
   size" above) and, on Linux, the symbol floor and the host libraries left.
4. Licence notices (`LICENSES/` from `gen_licenses.py`) and [ASSETS.md](../ASSETS.md) up to date.
5. Release notes: what changed for operators, any new or changed `ParityProfile`.
