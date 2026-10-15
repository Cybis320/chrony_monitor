#!/bin/bash
# Install the tempcomp apply helper and grant passwordless sudo for it.
#
# Idempotent: safe to run repeatedly. Does NOT clobber existing rules in
# /etc/sudoers.d/chrony-monitor (e.g. GPS/PPS recovery); it only adds the
# apply-tempcomp line if missing.
#
# Usage: sudo bash scripts/setup-tempcomp-sudo.sh

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/apply-tempcomp.sh"
DEST="/usr/local/bin/apply-tempcomp.sh"
SUDOERS="/etc/sudoers.d/chrony-monitor"

# Determine the unprivileged user the monitor runs as.
# CHRONY_MONITOR_USER wins (set by the auto-updater, where SUDO_USER is unset
# and the /opt checkout is root-owned); then SUDO_USER; then the dir owner.
TARGET_USER="${CHRONY_MONITOR_USER:-${SUDO_USER:-}}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    TARGET_USER="$(stat -c '%U' "$SCRIPT_DIR")"
fi
echo "Target user: $TARGET_USER"

# 1. Install the root-owned helper script.
install -m 0755 -o root -g root "$SRC" "$DEST"
echo "Installed: $DEST"

# 2. Add the NOPASSWD rule if not already present.
RULE="$TARGET_USER ALL=(root) NOPASSWD: $DEST"
if [ -f "$SUDOERS" ] && grep -qF "$DEST" "$SUDOERS"; then
    echo "Sudoers rule already present in $SUDOERS"
else
    TMP="$(mktemp)"
    [ -f "$SUDOERS" ] && cat "$SUDOERS" > "$TMP"
    echo "$RULE" >> "$TMP"
    # Validate before installing — a broken sudoers file locks out sudo.
    if visudo -c -f "$TMP" >/dev/null; then
        install -m 0440 -o root -g root "$TMP" "$SUDOERS"
        echo "Added sudoers rule to $SUDOERS"
    else
        echo "Error: sudoers validation failed, not installing" >&2
        rm -f "$TMP"
        exit 1
    fi
    rm -f "$TMP"
fi

# 3. Verify the rule works non-interactively.
# --check succeeds without touching anything, so a failure here is sudo's. (The
# rules grant only specific commands, so `sudo -n true` always fails, and
# `sudo -l` reports a command that needs a password as allowed.)
if sudo -u "$TARGET_USER" sudo -n "$DEST" --check >/dev/null 2>&1; then
    echo "Verified: $TARGET_USER can run $DEST without a password"
else
    echo "Warning: $TARGET_USER can't run $DEST without a password; check $SUDOERS" >&2
fi

echo "Done. Restart chrony-monitor, or apply the pending calibration now with:"
echo "  sudo $DEST \"\$HOME/.local/share/chrony-monitor/tempcomp.proposed\""
