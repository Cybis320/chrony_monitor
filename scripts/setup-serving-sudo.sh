#!/bin/bash
# Grant the monitor passwordless sudo for the two read-only chronyc queries
# behind its "Serving" line (client count, NTP request rate). chronyd answers
# them only over its root-only command socket.
#
# Idempotent: adds only the rules that are missing from
# /etc/sudoers.d/chrony-monitor and leaves the rest of the file alone.
#
# Usage: sudo bash scripts/setup-serving-sudo.sh

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUDOERS="/etc/sudoers.d/chrony-monitor"
# Exact argument lists: sudo matches them literally, so these grant nothing else.
CMDS=("/usr/bin/chronyc -n clients" "/usr/bin/chronyc serverstats")

# Same user resolution as setup-tempcomp-sudo.sh.
TARGET_USER="${CHRONY_MONITOR_USER:-${SUDO_USER:-}}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    TARGET_USER="$(stat -c '%U' "$SCRIPT_DIR")"
fi
if [ "$TARGET_USER" = "root" ]; then
    echo "Error: can't tell which user runs the monitor; set CHRONY_MONITOR_USER" >&2
    exit 1
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
[ -f "$SUDOERS" ] && cat "$SUDOERS" > "$TMP"
added=0
for cmd in "${CMDS[@]}"; do
    if ! grep -qF "NOPASSWD: $cmd" "$TMP"; then
        echo "$TARGET_USER ALL=(root) NOPASSWD: $cmd" >> "$TMP"
        added=1
    fi
done

if [ "$added" -eq 0 ]; then
    echo "Sudoers rules for chronyc already present in $SUDOERS"
    exit 0
fi

# Validate before installing — a broken sudoers file locks out sudo.
if ! visudo -c -f "$TMP" >/dev/null; then
    echo "Error: sudoers validation failed, not installing" >&2
    exit 1
fi
install -m 0440 -o root -g root "$TMP" "$SUDOERS"
echo "Added chronyc rules for $TARGET_USER to $SUDOERS"
