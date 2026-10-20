#!/bin/sh
# Install mlaser's freedesktop entry and icons for the current user, so the launcher, the task
# switcher and Wayland compositors show "mlaser" with its icon (the window's desktop file name
# is io.github.mlaser, mlaser/ui/native.py).
#
#   sh install-desktop-entry.sh [EXEC]
#
# EXEC: the command that starts mlaser (default: the bundle's executable next to this script's
# folder, else "mlaser" on the PATH, e.g. a pip/uv install).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
exec_cmd=${1:-}
if [ -z "$exec_cmd" ]; then
    for cand in "$here/../../mlaser" "$here/../mlaser"; do
        if [ -x "$cand" ]; then exec_cmd=$(cd "$(dirname "$cand")" && pwd)/mlaser; break; fi
    done
fi
[ -n "$exec_cmd" ] || exec_cmd=mlaser
data=${XDG_DATA_HOME:-$HOME/.local/share}
mkdir -p "$data/applications"
entry="$here/applications/io.github.mlaser.desktop"         # in the bundle (_internal/share)
[ -f "$entry" ] || entry="$here/io.github.mlaser.desktop"    # in the source tree (packaging/linux)
exec_line=$exec_cmd
[ -e "$exec_cmd" ] && exec_line="\"$exec_cmd\""                 # a path (may hold spaces): quoted
sed -e "s|^Exec=.*|Exec=$exec_line %F|" -e "/^TryExec=/d" "$entry" > "$data/applications/io.github.mlaser.desktop"
icons="$here/icons/hicolor"
if [ ! -d "$icons" ] && command -v python3 >/dev/null 2>&1 && [ -f "$here/../make_icons.py" ]; then
    # source tree: render the icons (needs PySide6 and Pillow, e.g. inside `uv run`)
    python3 "$here/../make_icons.py" "${TMPDIR:-/tmp}/mlaser-icons" >/dev/null 2>&1 && icons="${TMPDIR:-/tmp}/mlaser-icons/hicolor"
fi
if [ -d "$icons" ]; then
    mkdir -p "$data/icons"
    cp -R "$icons" "$data/icons/"
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$data/applications" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$data/icons/hicolor" 2>/dev/null || true
echo "installed $data/applications/io.github.mlaser.desktop (Exec=$exec_cmd)"
