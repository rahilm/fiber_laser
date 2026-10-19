# Packaging mlaser

One-folder (onedir) builds with PyInstaller for Windows, macOS (`.app`) and Linux. CI builds them
in `.github/workflows/package.yml`. Signing hooks are included as placeholders.

## Build

```sh
uv sync --group packaging --all-extras         # or without --all-extras, see "Size"
uv run python packaging/build.py --smoke       # build + licences + LGPL check + frozen smoke test
uv run python packaging/build.py --slim        # without OR-Tools / spyrrow / Ruckig
uv run python packaging/build.py --skip-build --smoke --archive   # after signing: test and pack
```

Output goes to `packaging/build/` (git-ignored, through the `build/` rule in `.gitignore`):

| Path | Content |
|---|---|
| `dist/mlaser/` | the bundle: `mlaser` (GUI), `mlaser-console` (same program with a console), `_internal/`, `LICENSES/` |
| `dist/mlaser.app` | macOS only: the application bundle (`Contents/Resources/LICENSES`) |
| `build-info.json` | size report, bundled distributions, LGPL check, smoke-test output |
| `mlaser-<version>-<os>-<arch>.tar.gz/.zip` | with `--archive` (keeps symlinks and executable bits) |

Files in this folder:

| File | Purpose |
|---|---|
| `mlaser.spec` | PyInstaller spec (onedir, two executables, macOS `BUNDLE`) |
| `bundle_config.py` | what goes in: package data, hidden imports, excluded modules, dropped Qt libraries and plugins, GPL-only Qt check, orphan-library pruning |
| `build.py` | build driver: PyInstaller, then licences, checks, sizes, smoke test, archive |
| `gen_licenses.py` | writes `LICENSES/` from the installed distributions' licence metadata |
| `licenses/` | verbatim LGPL-3.0 and GPL-3.0 texts (FSF) |
| `sign_windows.ps1`, `sign_macos.sh`, `entitlements.plist` | code-signing hooks (no-ops without credentials) |

The frozen program accepts the GUI options (`--demo`, `--sim`, `--real`, `--ini` ...) plus the
launcher options in `mlaser/__main__.py`:

* `--selftest [--probe] [--json]`: the network self-test (below), then exit;
* `--smoke-test SECONDS`: start normally and quit cleanly after SECONDS (exit code 0);
* `--bundle-check [FILE.dxf ...]`: import every bundled library, read the package data, import
  the given drawings, then exit (0 = everything required is present).

On Windows use `mlaser-console.exe` for these: the GUI executable has no console, so output and
exit codes are not visible there.

## What is in the bundle

* All `mlaser` modules except the parity capture/compare tooling (`mlaser.parity`, which needs
  matplotlib and Wine captures). `mlaser.parity_ports` stays because the app uses it.
* The C emitter kernel `mlaser._native._emitter` (built by `uv sync` when a compiler is present;
  CI sets `MLASER_BUILD_NATIVE=1` so a compile error fails the build). `--smoke` runs the
  frozen program with `MLASER_NATIVE=1`: the bundle check fails when the kernel is missing or
  fails its self-check ([NATIVE_KERNEL.md](../docs/dev/NATIVE_KERNEL.md)). Set
  `MLASER_NATIVE=auto` to accept a bundle without it (the Python port then runs).
* Package data: `mlaser/ui/resources` (the `layer_colors.json` file and the Tabler icons with
  their licence), `mlaser/i18n/*.json`, `mlaser/formats/data/*`, any fonts under `mlaser/cad`.
  The help documents (`docs/user`, `docs/dev`, `docs/re` and the top-level `SAFETY.md`,
  `ASSETS.md`, `ROADMAP.md`) go to `mlaser/help/docs` (`bundle_config.help_datas`), where
  `diag/helpdocs.py` finds them; `docs/review` and `docs/re/history` stay out.
* Libraries: PySide6 (QtCore, QtGui, QtWidgets, QtSvg, QtPrintSupport, and QtDBus/QtNetwork,
  which the bindings pull in), numpy, shapely (GEOS), ezdxf with its fonts and resources,
  svgelements, pyclipper, uharfbuzz, fontTools, segno, Pillow, platformdirs. When they are
  installed, the build also includes hidapi and pyserial (devices) and OR-Tools, spyrrow and
  Ruckig (modern options).
* Left out: QtWebEngine, Qt Quick/QML, Qt3D, Multimedia, PDF, Charts and the other unused Qt
  modules. Also left out: the GTK3 platform theme (it would ship the build host's GTK, cairo and
  pango), the TLS and network-information plugins (no OpenSSL; nothing uses QtNetwork), and
  matplotlib, pytest, hypothesis and tkinter. Every Qt module that is GPL-only in Qt 6 open source
  (Charts, Data Visualization, Graphs, Quick 3D, Virtual Keyboard, Lottie, Network Authorization,
  Designer, HTTP Server ...) is removed. If one of them slips in, `build.py` fails.

## Size (Linux x86_64, Python 3.13, PySide6 6.12.0, local build 2026-10-18)

| Build | Folder size | Archive |
|---|---|---|
| default, with all extras (`--all-extras`) | 357 MB, 657 files | 139 MB `.tar.gz` |
| `--slim` (no OR-Tools / spyrrow / Ruckig) | 281 MB | 111 MB `.tar.gz` |

The largest parts are the Qt libraries (74 MB), OR-Tools (71 MB with its absl/protobuf/SCIP
libraries), libpython (32 MB), OpenBLAS in numpy (28 MB), numpy (14 MB), ezdxf (12 MB) and
Pillow's image libraries (12 MB). CI builds with all extras. Use `--slim` for a smaller
download if the optional modern options (OR-Tools sort mode, spyrrow nester, Ruckig profiles) are
not needed.

## Qt, PySide6 and the LGPL-3.0

Qt and PySide6/Shiboken6 are used under the GNU LGPL-3.0. The bundle meets its conditions for a
"Combined Work" as follows:

1. **Replaceable shared libraries (LGPL-3.0 §4(d)(1)).** Use onedir builds only, never onefile.
   Qt stays as ordinary shared libraries that are loaded at start-up:
   * Linux: `mlaser/_internal/PySide6/Qt/lib/libQt6*.so.6`, with symlinks in `_internal/`;
   * Windows: `mlaser\_internal\PySide6\Qt6*.dll`, with plugins in `...\PySide6\plugins`;
   * macOS: `mlaser.app/Contents/Frameworks/PySide6/Qt/lib/Qt*.framework`.

   Users can swap in their own build of the same Qt 6.12.x series. After replacing files on
   macOS, they re-sign the bundle (`codesign --force --deep --sign - mlaser.app`). The Qt
   libraries are not modified, and UPX is off because it would rewrite them.
2. **Notice and licence texts (§4(a), (b)).** `LICENSES/Qt-PySide6-NOTICE.txt` names the Qt and
   PySide6 versions, links the source code and explains how to replace the libraries.
   `LGPL-3.0.txt` and `GPL-3.0.txt` sit next to it.
3. **Source of the library (GPL-3.0 §6, through LGPL-3.0 §4).** The notice links the official
   Qt and PySide6 source archives for the exact version. For public releases, also put those
   two archives (`qt-everywhere-src-6.12.0`, `pyside-setup-everywhere-src-6.12.0`) on the
   release page, or keep a written offer. Then the source stays available even if download.qt.io
   moves it.
4. **Copyright display (§4(c)).** If the program shows copyright notices while it runs (an
   About dialog), the Qt notice must be among them. See "Merge notes" (`QMessageBox.aboutQt`).
5. §4(e) (installation information) applies only to "User Products" (consumer devices). It does
   not apply to a PC application.

mlaser's own code is MIT, so the whole program can be rebuilt from source against any Qt.
OpenBLAS (numpy), the GEOS library in shapely (LGPL-2.1) and Pillow's image libraries are also
shipped as separate shared libraries, as they come in their wheels. Their licence texts are in
`LICENSES/THIRD_PARTY_NOTICES.txt` and `LICENSES/third_party/`. The PyInstaller bootloader is
GPL-2.0 with the bootloader exception, which places no obligations on the bundled program.

`gen_licenses.py` runs on every build. It lists only the distributions whose files are actually
in the bundle (extension modules, libraries or data on disk, plus the Python modules in the PYZ
archive). For each one it records the name, version and licence (`License-Expression`,
`License` or the classifiers) and copies the licence files from its `.dist-info`. hidapi is
dual-licensed and is used under its BSD option.

On Linux, PyInstaller also copies the system libraries the Qt plugins link to from the build
host. They are mostly X11/xcb, fontconfig, freetype, glib, dbus and cups: MIT, LGPL or similar.
`THIRD_PARTY_NOTICES.txt` does not list them. If a Linux release is published as an archive,
add a note about them, or build an AppImage on top of this folder.

## Network and firewall self-test

`mlaser/netcheck.py` (standard library only) checks:

* that a UDP socket can be bound;
* that the OS has a route to the card address (a UDP `connect()`, which sends nothing);
* that a local interface is on the card's /24 subnet;
* when asked (`--probe`), whether the card answers one status read (the checkMCStatus frame
  of `mlaser.controller.nettest`).

It also prints per-OS firewall hints.

The card address comes from the same places the app reads it: `--ini` (an `ipAdd.ini`), then
`--host/--port`, then `<app dir>/File/ipAdd.ini`, then 10.1.1.168:502.

The first time a user starts the app, the check runs once with no card traffic. It runs in a
background thread and holds startup for at most 1 s. The result goes to stderr and to
`<user config dir>/netcheck.json` (platformdirs `user_config_dir("mlaser")`, or
`$MLASER_CONFIG_DIR`). That file is also the "already ran" marker. Set `MLASER_NO_FIRSTRUN=1` to
skip the first-run check.

## Signing

Both hooks exit 0 with a message when their credentials are missing. CI calls them only when the
secrets are set.

* **Windows:** `sign_windows.ps1` uses Azure Trusted Signing (Artifact Signing) through signtool
  and the `Azure.CodeSigning.Dlib`. It needs `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
  `AZURE_CLIENT_SECRET`, `TRUSTED_SIGNING_ENDPOINT`, `TRUSTED_SIGNING_ACCOUNT` and
  `TRUSTED_SIGNING_PROFILE`. It signs both executables and every unsigned `.dll`/`.pyd`.
* **macOS:** `sign_macos.sh` runs codesign with the hardened runtime (`entitlements.plist`),
  inside out, then `notarytool submit --wait` and `stapler staple`. It needs
  `MACOS_SIGN_IDENTITY`, plus optionally `MACOS_CERT_P12_BASE64`/`MACOS_CERT_PASSWORD` for a
  temporary CI keychain. Notarisation needs either the App Store Connect API key variables
  (`NOTARY_KEY_ID`, `NOTARY_ISSUER_ID`, `NOTARY_KEY_P8_BASE64`) or `APPLE_ID`, `APPLE_TEAM_ID`
  and `APPLE_APP_PASSWORD`.

The order is: build (it writes `LICENSES/`), sign, then `build.py --skip-build --smoke
--archive`. `--skip-build` does not touch the bundle, so the signature stays valid.

## Not done yet

* Installers: Inno Setup or MSIX on Windows, a dmg on macOS, an AppImage on Linux. They should
  wrap this folder unchanged.
* An app icon (`packaging/icons/mlaser.ico` / `.icns`; the spec uses it when present).
* Help topics in a frozen build (see "What is in the bundle").
* Automatic updates (tufup, or WinSparkle/Sparkle).
