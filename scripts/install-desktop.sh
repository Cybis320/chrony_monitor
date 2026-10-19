#!/bin/bash
# Install the Chrony Monitor launcher (Desktop + app menu) and its autostart
# entry, running the monitor from this checkout. Runs as the monitor user.
#
# With --stale-autostart, only disable other autostart entries that start the
# monitor (see below) and leave the launchers alone.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_TOOL=chrony_monitor
# shellcheck source=../cc-utils/lib.sh
. "$PROJECT_DIR/cc-utils/lib.sh"

# Older hand-made autostart entries (e.g. ChronyMonitor.desktop) start a second
# monitor next to chrony-monitor.desktop at every login, often from a stale
# checkout. Rename them to *.disabled -- GNOME only reads *.desktop -- so the
# change is reversible.
disable_stale_autostart() {
    local dir="$HOME/.config/autostart" f
    [ -d "$dir" ] || return 0
    for f in "$dir"/*.desktop; do
        [ -f "$f" ] || continue
        [ "$(basename "$f")" = chrony-monitor.desktop ] && continue
        grep -qE '^Exec=.*-m chrony_monitor' "$f" || continue
        mv -f "$f" "$f.disabled"
        cc_info "Disabled duplicate autostart entry $f"
    done
}

disable_stale_autostart
[ "${1:-}" = "--stale-autostart" ] && exit 0

CMD="python3 -m chrony_monitor"
EXEC="$(cc_terminal_exec "Chrony Monitor" "$CMD")"
TERMINAL=false
[ "$EXEC" = "$CMD" ] && TERMINAL=true      # no terminal emulator: let the DE open one

cc_desktop_entry chrony-monitor.desktop "Chrony Monitor" "$EXEC" \
    "$PROJECT_DIR/icon.png" "Monitor chrony time synchronization" \
    "System;Monitor;" "$TERMINAL" 10 "$PROJECT_DIR"
cc_info "Chrony Monitor launcher + autostart -> $PROJECT_DIR"
