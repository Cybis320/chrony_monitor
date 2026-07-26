#!/bin/bash
#
# One-time migration: repoint an existing chrony.conf tempcomp directive from a
# raw /sys/class/thermal/thermal_zoneN/temp path to the reboot-stable symlink
# published by chrony-tempcomp-sensor.service.
#
# Idempotent: does nothing if there's no tempcomp line, or it already uses the
# stable link.
#
# The calibration coefficients are left in place. On the station this was written
# for that's exactly right — the zone had renumbered under a correct fit, and
# repointing restores the pairing. It is NOT guaranteed in general: if the
# coefficients were fit against a different sensor than the link resolves to,
# they'll drive compensation off the wrong temperature scale until auto-recal
# replaces them (within a day, and bounded by chrony's +/-10ppm clamp). Keeping
# them beats dropping compensation entirely, and the monitor detects the
# mismatch on its side (see _config_matches_sensor) rather than folding the two
# scales together.
#
# Run as root (from install.sh / update.sh).

set -u

# Overridable so the migration can be exercised against a fixture config.
LINK="${LINK:-/run/chrony-monitor/tempcomp-sensor}"
RESOLVER="${RESOLVER:-/usr/local/bin/update-tempcomp-symlink.sh}"

CHRONY_CONF="${CHRONY_CONF:-}"
if [ -z "$CHRONY_CONF" ]; then
    for c in /etc/chrony/chrony.conf /etc/chrony.conf; do
        [ -f "$c" ] && { CHRONY_CONF="$c"; break; }
    done
fi
[ -z "$CHRONY_CONF" ] && { echo "migrate-tempcomp: chrony.conf not found"; exit 0; }

# Only act on a tempcomp line (active or commented) that names a raw thermal zone.
if ! grep -qE '^[[:space:]]*#?[[:space:]]*tempcomp[[:space:]]+/sys/class/thermal/thermal_zone[0-9]+/temp' "$CHRONY_CONF"; then
    echo "migrate-tempcomp: nothing to migrate"
    exit 0
fi

# The link must exist and be readable before chrony is pointed at it. Bail out
# otherwise: repointing at a missing link would silently stop compensation, and
# nothing would retry it — the next run finds no raw zone path left to migrate.
if [ ! -e "$LINK" ] && [ -x "$RESOLVER" ]; then
    "$RESOLVER" "$LINK" || echo "migrate-tempcomp: resolver failed" >&2
fi
if [ ! -r "$LINK" ]; then
    echo "migrate-tempcomp: $LINK not published — leaving chrony.conf alone" >&2
    exit 1
fi

cp "$CHRONY_CONF" "${CHRONY_CONF}.bak.pre-sensor-migration" || {
    echo "migrate-tempcomp: cannot write backup — aborting" >&2
    exit 1
}

# Use | as the sed delimiter: the pattern contains '#' (commented lines) and the
# paths contain '/', so neither can serve as the delimiter.
#
# Rewrite via a temp file rather than `sed -i`: GNU takes the backup suffix
# attached to -i while BSD takes it as the next argument, so `-i -E` silently
# means different things on different hosts. Writing back with `cat` (not `mv`)
# keeps chrony.conf's own inode, mode and owner.
tmp="$(mktemp)" || { echo "migrate-tempcomp: mktemp failed" >&2; exit 1; }
if ! sed -E \
    "s|(^[[:space:]]*#?[[:space:]]*tempcomp[[:space:]]+)/sys/class/thermal/thermal_zone[0-9]+/temp|\1${LINK}|" \
    "$CHRONY_CONF" > "$tmp"; then
    rm -f "$tmp"
    echo "migrate-tempcomp: rewrite failed — config untouched" >&2
    exit 1
fi
if ! cat "$tmp" > "$CHRONY_CONF"; then
    cp "${CHRONY_CONF}.bak.pre-sensor-migration" "$CHRONY_CONF"
    rm -f "$tmp"
    echo "migrate-tempcomp: cannot write $CHRONY_CONF — restored backup" >&2
    exit 1
fi
rm -f "$tmp"

# Validate before restarting; roll back on failure.
if command -v chronyd >/dev/null 2>&1 && ! chronyd -p >/dev/null 2>&1; then
    echo "migrate-tempcomp: chrony config validation failed — rolling back" >&2
    cp "${CHRONY_CONF}.bak.pre-sensor-migration" "$CHRONY_CONF"
    exit 1
fi

systemctl restart chrony 2>/dev/null || systemctl restart chronyd 2>/dev/null || true
echo "migrate-tempcomp: repointed tempcomp sensor to $LINK"
