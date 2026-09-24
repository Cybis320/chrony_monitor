#!/bin/bash
# Install the Chrony Monitor launcher (Desktop + app menu) and its autostart
# entry, running the monitor from this checkout. Runs as the monitor user.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_TOOL=chrony_monitor
# shellcheck source=../cc-utils/lib.sh
. "$PROJECT_DIR/cc-utils/lib.sh"

CMD="python3 -m chrony_monitor"
EXEC="$(cc_terminal_exec "Chrony Monitor" "$CMD")"
TERMINAL=false
[ "$EXEC" = "$CMD" ] && TERMINAL=true      # no terminal emulator: let the DE open one

cc_desktop_entry chrony-monitor.desktop "Chrony Monitor" "$EXEC" \
    "$PROJECT_DIR/icon.png" "Monitor chrony time synchronization" \
    "System;Monitor;" "$TERMINAL" 10 "$PROJECT_DIR"
cc_info "Chrony Monitor launcher + autostart -> $PROJECT_DIR"
