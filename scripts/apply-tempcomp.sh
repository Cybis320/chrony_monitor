#!/bin/bash
# Apply a proposed tempcomp calibration to chrony.conf.
# Designed to be called via sudo from chrony-monitor.
#
# Usage: sudo apply-tempcomp.sh <proposed-conf-path>
#        sudo apply-tempcomp.sh --disable
#        sudo apply-tempcomp.sh --check
#
# The proposed file must contain exactly one tempcomp line.
# This script validates the input, updates chrony.conf, and restarts chrony.
# --disable comments out the active tempcomp line instead (a data reset
# discards the fit along with the data); the next applied calibration
# replaces the commented line and so re-enables it.
# --check does nothing and succeeds: setup-tempcomp-sudo.sh runs it to confirm
# sudo runs this without a password.

set -euo pipefail

if [ "${1:-}" = "--check" ]; then
    exit 0
fi

# Locate chrony.conf (Debian/Ubuntu use /etc/chrony/chrony.conf; others /etc/chrony.conf)
CHRONY_CONF=""
for c in /etc/chrony/chrony.conf /etc/chrony.conf; do
    if [ -f "$c" ]; then
        CHRONY_CONF="$c"
        break
    fi
done
if [ -z "$CHRONY_CONF" ]; then
    echo "Error: chrony.conf not found" >&2
    exit 1
fi

PROPOSED="$1"

if [ "$PROPOSED" = "--disable" ]; then
    if ! grep -qE '^\s*tempcomp\s+' "$CHRONY_CONF"; then
        echo "tempcomp already inactive"
        exit 0
    fi
    cp "$CHRONY_CONF" "${CHRONY_CONF}.bak"
    sed -i 's|^[[:space:]]*\(tempcomp[[:space:]]\)|#\1|' "$CHRONY_CONF"
    if ! chronyd -p >/dev/null 2>&1; then
        echo "Error: chrony config validation failed, restoring backup" >&2
        cp "${CHRONY_CONF}.bak" "$CHRONY_CONF"
        exit 1
    fi
    systemctl restart chrony
    echo "Disabled tempcomp"
    exit 0
fi

if [ ! -f "$PROPOSED" ]; then
    echo "Error: proposed file not found: $PROPOSED" >&2
    exit 1
fi

# Validate: file must contain exactly one line matching tempcomp directive
TEMPCOMP_LINE=$(grep -E '^tempcomp\s+' "$PROPOSED" 2>/dev/null || true)
LINECOUNT=$(echo "$TEMPCOMP_LINE" | grep -c . || true)

if [ "$LINECOUNT" -ne 1 ]; then
    echo "Error: proposed file must contain exactly one tempcomp line" >&2
    exit 1
fi

# Validate: the tempcomp line must have the expected format (6 params)
if ! echo "$TEMPCOMP_LINE" | grep -qE '^tempcomp\s+\S+\s+[0-9]+\s+[+-]?[0-9.]+\s+[+-]?[0-9.]+\s+[+-]?[0-9.e-]+\s+[+-]?[0-9.e-]+$'; then
    echo "Error: invalid tempcomp format" >&2
    exit 1
fi

# Backup current config
cp "$CHRONY_CONF" "${CHRONY_CONF}.bak"

# Replace or append tempcomp line
if grep -qE '^\s*#?\s*tempcomp\s+' "$CHRONY_CONF"; then
    # Replace existing (active or commented) tempcomp line
    sed -i "s|^[[:space:]]*#\?[[:space:]]*tempcomp .*|${TEMPCOMP_LINE}|" "$CHRONY_CONF"
else
    # Append
    echo "" >> "$CHRONY_CONF"
    echo "$TEMPCOMP_LINE" >> "$CHRONY_CONF"
fi

# Validate chrony config before restarting
if ! chronyd -p 2>/dev/null; then
    echo "Error: chrony config validation failed, restoring backup" >&2
    cp "${CHRONY_CONF}.bak" "$CHRONY_CONF"
    exit 1
fi

# Restart chrony
systemctl restart chrony

echo "Applied: $TEMPCOMP_LINE"
