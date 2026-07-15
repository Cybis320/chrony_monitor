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
if sudo -u "$TARGET_USER" sudo -n "$DEST" /dev/null >/dev/null 2>&1; then
    echo "Verified: $TARGET_USER can run $DEST without a password"
else
    # /dev/null is not a valid proposed file, so the script exits non-zero —
    # but if sudo itself were still prompting we'd see a sudo error instead.
    if sudo -u "$TARGET_USER" sudo -n true 2>/dev/null; then
        echo "Verified: passwordless sudo is configured for $TARGET_USER"
    else
        echo "Warning: sudo -n still requires a password; check $SUDOERS" >&2
    fi
fi

echo "Done. Restart chrony-monitor, or apply the pending calibration now with:"
echo "  sudo $DEST \"\$HOME/.local/share/chrony-monitor/tempcomp.proposed\""
