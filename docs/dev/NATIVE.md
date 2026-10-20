# Native platform integration

mlaser should feel like an application of the platform it runs on: macOS, Windows or Linux.
This page lists what each platform gets, where the code is, and how CI checks it on real
runners.

Code:

- `mlaser/ui/native.py`: identity, activation, recent files, Dock / taskbar / launcher
  progress, alerts, file-open events.
- `mlaser/ui/shell/menu_spec.py` (Qt-free) and `mlaser/ui/shell/menubar.py`: the menu bar.
- `mlaser/ui/shell/theme.py`: style, colour scheme, the small style sheet.
- `mlaser/ui/shell/keymap.py`: keys per platform and the shortcut text.
- `mlaser/_power.py`: sleep and App Nap.
- `packaging/mlaser.spec`, `packaging/make_icons.py`, `packaging/linux/`: bundle metadata.

## The platform matrix

| | macOS | Windows | Linux |
|---|---|---|---|
| Style | `macos` (Qt's default) | `windows11`, else `windowsvista` | the desktop's style, else Fusion |
| Light / dark | follows the system, live; Light / Dark in mlaser set the app's appearance (`QStyleHints.setColorScheme`) | the same, plus a dark title bar (DWM) for mlaser's own choice | the same through the xdg-desktop-portal colour scheme; without a portal, mlaser's palette |
| High contrast | Fusion with mlaser's high-contrast palette | the same | the same |
| Menu bar | the global menu bar. The app menu has About mlaser, Settings… ⌘,, Plugins…, Services, Hide, Hide Others, Show All and Quit mlaser ⌘Q (menu roles) | in the window, with Alt access keys (`&File`). Exit is at the end of File, Options… at the end of Tools | in the window (or the desktop's global menu where it has one). Quit is at the end of File, Preferences… at the end of Edit |
| Menus | File, Edit, View, Draw, Process, Workspace, Machine, Tools, Window, Help | same | same |
| Help | the system's help search field; Search commands (⌘K) | Search commands (Ctrl+K), F1 | Search commands (Ctrl+K), F1 |
| Redo | ⇧⌘Z | Ctrl+Y (also Ctrl+Shift+Z) | Ctrl+Shift+Z (also Ctrl+Y) |
| Full screen | Window > Full Screen ⌃⌘F | F11 | F11 |
| F-key commands | each also has a ⌘ key: Start ⌘R, Frame ⇧⌘F, Dry run ⇧⌘R, Stop ⌘. (Esc too) | F5, F6, Shift+F6, F12 | same as Windows |
| Shortcut text | ⌘K, ⇧⌘Z, …: every translated text and the shortcut sheet | Ctrl+K | Ctrl+K |
| Identity | the `.app` (`io.github.mlaser`), icon, the "mlaser" name. A source run renames the process and its LaunchServices display name, and sets the Dock icon | AppUserModelID `io.github.mlaser.mlaser`: taskbar grouping, icon, pinning | desktop file name `io.github.mlaser` (Wayland app id, `_KDE_NET_WM_DESKTOP_FILE`), `WM_CLASS` mlaser, icon |
| Activation | regular activation policy, brought to the front at start (a terminal keeps focus under macOS 14+ cooperative activation otherwise) | the system's | the window manager's |
| Recent files | File > Open Recent, the Dock menu | File > Open Recent, the jump list | File > Open Recent |
| Job progress | the Dock badge "42%" | taskbar button progress (red while an alarm is active) | launcher progress (Unity LauncherEntry: KDE, Ubuntu Dock) |
| Alarm | Dock bounce (`QApplication.alert`) | taskbar flash | urgency hint |
| Sleep during a job | App Nap off for the process; no idle sleep while a job runs | power request | a logind inhibitor (`systemd-inhibit --what=sleep:idle`, else `gnome-session-inhibit`) for the job's duration |
| File dialogs | native (Qt default) | IFileDialog (Qt default) | the portal's (the bundle selects the `xdgdesktopportal` platform theme, because it ships no GTK theme plugin) |
| Opening a file from the shell | Finder "Open With" and dropping on the Dock icon (`CFBundleDocumentTypes`, `QFileOpenEvent`) | command line | `.desktop` `Exec=mlaser %F` |
| Window title | the file name; the close button shows the modified dot (`setWindowModified`); the proxy icon (`setWindowFilePath`) | `name* - mlaser` | `name* — mlaser` |

## Why the macOS app had no Dock icon or menu bar (fixed)

The CI evidence is native-check run 39367733603, on the `.app` built from 9dacd50:
`lsappinfo` reported `type="BackgroundOnly"`, NSApp's activation policy was *prohibited*,
the app was never frontmost, the Dock had no mlaser item, and the menu bar kept Finder's
menus.

The spec builds two executables into one COLLECT: the windowed `mlaser` and `mlaser-console`
(console=True). PyInstaller's `BUNDLE` inherits `console` from the last executable it sees,
and it writes `LSBackgroundOnly = true` for a console bundle. The Info.plist now says
`LSBackgroundOnly = false` explicitly (`tests/test_native.py::test_bundle_metadata`). The app
also sets the regular activation policy when it finds another one (`native.mac_activate`).

Before the fix, a source run (`uv run mlaser`) showed as "Python" or "python3.12" with the
Python icon. It was also not made active when started from a terminal.

`argv_emulation` stays off. It runs an event loop before the application starts and is known
to break Qt apps. Files opened from Finder arrive as `QFileOpenEvent` instead (in the
`MlaserApplication` subclass, so no application-wide event filter is needed).

## The style sheet rule

Standard widgets (buttons, inputs, tabs, lists, docks, scroll bars, tool bars, menus) are drawn
by the platform style with the platform palette. `theme.style_sheet` styles only the command
palette's overlay frame (palette roles) and the empty-state title. Sizes are ratios of the
platform font (`theme.TYPE_SCALE`), never absolute `pt`: macOS points render about 25 % smaller.
Domain widgets keep their own colours: STOP, the run bar, the state banner and chips, and the
canvas. `tests/test_native.py` fails on a standard-widget selector, a `min-height` or a colour
literal in the shell style sheet.

## Checking on real runners

`.github/workflows/native-check.yml` runs on `workflow_dispatch`, on pushes to `ui-native` /
`native-check/**`, and on pull requests that touch the UI or packaging. It starts the real
application (`--demo`, with a throw-away config) on three runners:

- macos-latest: from source with `uv run mlaser` and `python -m mlaser`, with the system in dark
  mode, with mlaser's own dark theme, and as the built `.app` opened through LaunchServices.
- windows-latest: from source, with the system dark theme, with mlaser's dark theme, and as the
  bundle.
- ubuntu-latest: Xvfb with an Xfce session, from source and as the bundle.

`tools/native_check.py` collects the app's own report (`MLASER_NATIVE_REPORT`,
`mlaser/ui/native_report.py`) and what the OS says:

- macOS: `lsappinfo` type, the frontmost app, the menu bar titles and the Dock items through
  System Events.
- Windows: the foreground window, the windows' styles and the DWM dark title bar.
- Linux: `_NET_ACTIVE_WINDOW`, `WM_CLASS`, the desktop file, the icon.

It also takes a full-screen screenshot. `--expect` fails the step when the app is not
Foreground and frontmost with the native menu bar, the style and the identity. The summary
lists each platform's table, and the screenshots are the `native-<os>` artifacts.

## Linux desktop entry

The bundle carries `share/applications/io.github.mlaser.desktop`, the hicolor icons and
`share/install-desktop-entry.sh` (under `_internal/`). Run it once to add mlaser to the
launcher. A pip or uv install can use `packaging/linux/install-desktop-entry.sh mlaser`.
