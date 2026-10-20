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
sed -e "s|^Exec=.*|Exec=\"$exec_cmd\" %F|" -e "/^TryExec=/d" \
    "$here/applications/io.github.mlaser.desktop" > "$data/applications/io.github.mlaser.desktop"
if [ -d "$here/icons/hicolor" ]; then
    cp -R "$here/icons/hicolor" "$data/icons/" 2>/dev/null || { mkdir -p "$data/icons"; cp -R "$here/icons/hicolor" "$data/icons/"; }
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$data/applications" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$data/icons/hicolor" || true
echo "installed $data/applications/io.github.mlaser.desktop (Exec=$exec_cmd)"
