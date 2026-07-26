#!/bin/bash
#
# One-time migration: repoint an existing chrony.conf tempcomp directive from a
# raw /sys/class/thermal/thermal_zoneN/temp path to the reboot-stable symlink
# published by chrony-tempcomp-sensor.service.
#
# Idempotent: does nothing if there's no tempcomp line, or it already uses the
# stable link. The calibration coefficients are left untouched — they were fit
# against the sensor the symlink now resolves to (same physical zone, stable
# type), and auto-recal will refine them over time if needed.
#
# Run as root (from install.sh / update.sh).

set -u

LINK="/run/chrony-monitor/tempcomp-sensor"
RESOLVER="/usr/local/bin/update-tempcomp-symlink.sh"

CHRONY_CONF=""
for c in /etc/chrony/chrony.conf /etc/chrony.conf; do
    [ -f "$c" ] && { CHRONY_CONF="$c"; break; }
done
[ -z "$CHRONY_CONF" ] && { echo "migrate-tempcomp: chrony.conf not found"; exit 0; }

# Only act on a tempcomp line (active or commented) that names a raw thermal zone.
if ! grep -qE '^[[:space:]]*#?[[:space:]]*tempcomp[[:space:]]+/sys/class/thermal/thermal_zone[0-9]+/temp' "$CHRONY_CONF"; then
    echo "migrate-tempcomp: nothing to migrate"
    exit 0
fi

# Make sure the symlink exists before chrony is pointed at it.
if [ ! -e "$LINK" ] && [ -x "$RESOLVER" ]; then
    "$RESOLVER" "$LINK" || true
fi

cp "$CHRONY_CONF" "${CHRONY_CONF}.bak.pre-sensor-migration"

# Use | as the sed delimiter: the pattern contains '#' (commented lines) and the
# paths contain '/', so neither can serve as the delimiter.
sed -i -E \
    "s|(^[[:space:]]*#?[[:space:]]*tempcomp[[:space:]]+)/sys/class/thermal/thermal_zone[0-9]+/temp|\1${LINK}|" \
    "$CHRONY_CONF"

# Validate before restarting; roll back on failure.
if command -v chronyd >/dev/null 2>&1 && ! chronyd -p >/dev/null 2>&1; then
    echo "migrate-tempcomp: chrony config validation failed — rolling back" >&2
    cp "${CHRONY_CONF}.bak.pre-sensor-migration" "$CHRONY_CONF"
    exit 1
fi

systemctl restart chrony 2>/dev/null || systemctl restart chronyd 2>/dev/null || true
echo "migrate-tempcomp: repointed tempcomp sensor to $LINK"
