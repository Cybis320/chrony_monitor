#!/bin/bash
#
# Point chronyd's tempcomp input at the monitor's smoothed temperature while it
# is fresh, and back at the raw sensor when it isn't.
#
# Sensors such as x86_pkg_temp read in whole degrees and jump between
# neighbouring degrees second to second. chronyd takes one reading per
# interval, so with the raw sensor its compensation jumps by k1 per degree
# every interval and the clock wanders with it. The monitor reads the sensor
# every second and publishes a smoothed value; chrony.conf names
# /run/chrony-monitor/tempcomp-input, which this keeps pointed at:
#
#   /run/chrony-monitor/smoothed/temp   written in the last FRESH_S seconds
#   /run/chrony-monitor/tempcomp-sensor otherwise (the monitor stopped, or
#                                       never ran since boot)
#
# Both are in the same millidegrees, so one calibration serves either. chronyd
# opens its tempcomp file on every reading, so a switch takes effect at the
# next one, without a restart.
#
# Also creates the smoothed/ directory, owned by the monitor user, so the
# monitor (which runs unprivileged) can publish there.
#
# Run as root at boot by chrony-tempcomp-sensor.service, and every 30 s by
# chrony-tempcomp-input.timer.
#
# Usage: update-tempcomp-input.sh [-n|--dry-run]

set -uo pipefail

DRY_RUN=0
case "${1:-}" in
    -n|--dry-run) DRY_RUN=1 ;;
    "") ;;
    *) echo "usage: $0 [-n|--dry-run]" >&2; exit 2 ;;
esac

# Overridable so the switch can be exercised against a fixture tree.
RUN_DIR="${RUN_DIR:-/run/chrony-monitor}"
ENV_FILE="${ENV_FILE:-/etc/chrony-monitor/update.env}"
FRESH_S="${FRESH_S:-120}"

LINK="$RUN_DIR/tempcomp-input"
RAW="$RUN_DIR/tempcomp-sensor"
SMOOTHED_DIR="$RUN_DIR/smoothed"
SMOOTHED="$SMOOTHED_DIR/temp"

log() { echo "update-tempcomp-input: $1"; }

MONITOR_USER="${MONITOR_USER:-}"
if [ -z "$MONITOR_USER" ] && [ -f "$ENV_FILE" ]; then
    MONITOR_USER="$(sed -n 's/^MONITOR_USER=//p' "$ENV_FILE" | tail -n 1)"
fi

# The monitor user's directory. Without a known user, nothing can publish, and
# chronyd keeps reading the raw sensor.
if [ "$DRY_RUN" = 0 ] && [ -n "$MONITOR_USER" ] && [ "$MONITOR_USER" != root ] \
   && id -u "$MONITOR_USER" >/dev/null 2>&1; then
    if [ ! -d "$SMOOTHED_DIR" ] || [ -L "$SMOOTHED_DIR" ]; then
        rm -f "$SMOOTHED_DIR"
        install -d -m 0755 -o "$MONITOR_USER" -g "$(id -gn "$MONITOR_USER")" \
            "$SMOOTHED_DIR" || log "cannot create $SMOOTHED_DIR"
    fi
fi

# Fresh: a regular file the monitor wrote recently, holding a plausible
# temperature (5-110 C, as the sensor ranking requires). The file is the
# user's, so its content is only tested, never printed.
fresh() {
    [ -f "$SMOOTHED" ] && [ ! -L "$SMOOTHED" ] || return 1
    local mtime now v
    mtime="$(stat -c %Y "$SMOOTHED" 2>/dev/null)" || return 1
    now="$(date +%s)"
    [ $((now - mtime)) -le "$FRESH_S" ] || return 1
    v="$(head -c 16 "$SMOOTHED" 2>/dev/null | tr -d '[:space:]')"
    case "$v" in ''|*[!0-9]*) return 1 ;; esac
    [ "$v" -ge 5000 ] && [ "$v" -le 110000 ]
}

if fresh; then
    target="$SMOOTHED"
elif [ -e "$RAW" ]; then
    target="$RAW"
else
    log "neither $SMOOTHED nor $RAW is available; leaving $LINK untouched"
    exit 1
fi

if [ "$DRY_RUN" = 1 ]; then
    echo "$target"
    exit 0
fi

[ "$(readlink "$LINK" 2>/dev/null)" = "$target" ] && exit 0

# Atomic replace so chronyd never sees a missing link.
tmp="${LINK}.tmp.$$"
ln -sfn "$target" "$tmp" || { log "cannot stage $tmp"; exit 1; }
if ! mv -Tf "$tmp" "$LINK"; then
    rm -f "$tmp"
    log "cannot install $LINK"
    exit 1
fi
log "$LINK -> $target"
