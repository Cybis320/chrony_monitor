#!/bin/bash
#
# Publish a reboot-stable symlink to the best temperature sensor for tempcomp.
#
# Thermal-zone indices (/sys/class/thermal/thermal_zoneN) renumber across kernel
# updates, so a raw zone path baked into chrony.conf silently starts reading the
# wrong sensor after a reboot. This resolves the right sensor by *type* every
# boot and points a fixed path at it, so chrony.conf and the monitor's
# calibration writer can reference a sensor that survives renumbering.
#
# Run at boot before chrony by chrony-tempcomp-sensor.service.
#
# Usage: update-tempcomp-symlink.sh [-n|--dry-run] [link-path]
#
#   -n, --dry-run   Resolve and print the sensor; don't touch the link.
#
# Exits non-zero WITHOUT touching the link if no usable sensor is found: a stale
# link beats one pointing at the wrong sensor, and migrate-tempcomp-sensor.sh
# refuses to repoint chrony.conf at a link that isn't there.

set -uo pipefail

DRY_RUN=0
case "${1:-}" in
    -n|--dry-run) DRY_RUN=1; shift ;;
esac

LINK="${1:-/run/chrony-monitor/tempcomp-sensor}"

# Overridable only so the resolver can be exercised against a fixture tree.
THERMAL_BASE="${THERMAL_BASE:-/sys/class/thermal}"

# Root-owned copy of the package, installed by install.sh. Deliberately NOT the
# git checkout: this runs as root at boot, and the checkout is user-writable.
RESOLVER_LIB="/usr/local/lib/chrony-monitor"

# How long to wait for a real board/SoC zone to appear. intel_pch_thermal is a
# udev-loaded module, so at boot the PCH zone can be seconds late; settling for
# acpitz then would mis-key the calibration for the rest of the uptime. On a Pi
# the SoC zone is built in, so this returns immediately.
WAIT_PREFERRED="${WAIT_PREFERRED:-15}"

# That race only exists at cold boot, when nothing has been published yet. This
# unit also re-runs on every chrony restart — including the ones the monitor's
# PPS recovery triggers — so once a link is up, resolve immediately rather than
# adding seconds to a recovery restart.
if [ -r "$LINK" ]; then
    WAIT_PREFERRED=0
fi

log() { echo "update-tempcomp-symlink: $1"; }
die() { echo "update-tempcomp-symlink: $1" >&2; exit 1; }

resolve_sensor() {
    # Authoritative path: the monitor's own ranking, so the boot service and the
    # running monitor always agree. Run from / so `python3 -m` doesn't put a
    # writable directory on sys.path.
    if [ -f "$RESOLVER_LIB/chrony_monitor/tempcomp.py" ] \
       && command -v python3 >/dev/null 2>&1; then
        local p
        p="$(cd / && PYTHONPATH="$RESOLVER_LIB" \
                python3 -m chrony_monitor.tempcomp \
                --wait-preferred "$WAIT_PREFERRED" 2>/dev/null)"
        if [ -n "$p" ] && [ -r "$p" ]; then
            echo "$p"
            return 0
        fi
    fi

    # Last resort, for an install whose root-owned copy isn't in place yet. It
    # has no wait, so at a cold boot it can settle for acpitz where the Python
    # tier would have waited for the PCH; the unit re-runs on the next chrony
    # restart, which corrects it.
    # Kept at parity with the Python ranking: same priority order, same
    # plausibility band, and acpitz accepted as a fallback so an acpitz-only
    # Intel box still gets a link. Non-board zones (INT3400, wifi, NVMe) are
    # never matched, so they can't be selected.
    local pat z t millideg
    for pat in 'pch' 'cpu[-_]?thermal' 'bcm2835' 'soc[-_]?thermal' \
               'x86_pkg_temp' 'coretemp' 'acpitz'; do
        for z in "$THERMAL_BASE"/thermal_zone*; do
            [ -r "$z/temp" ] || continue
            millideg="$(cat "$z/temp" 2>/dev/null)" || continue
            # Mirrors SENSOR_PLAUSIBLE_RANGE_C (5-110C): skips the 0 that
            # unpopulated zones report, and in-band error values.
            case "$millideg" in ''|*[!0-9-]*) continue ;; esac
            [ "$millideg" -ge 5000 ] && [ "$millideg" -le 110000 ] || continue
            t="$(cat "$z/type" 2>/dev/null)" || continue
            if echo "$t" | grep -qiE "$pat"; then
                echo "$z/temp"
                return 0
            fi
        done
    done
    return 1
}

target="$(resolve_sensor)" \
    || die "no suitable temperature sensor found; leaving $LINK untouched"

if [ "$DRY_RUN" = 1 ]; then
    echo "$target"
    exit 0
fi

mkdir -p "$(dirname "$LINK")" || die "cannot create $(dirname "$LINK")"

# Atomic replace so chrony never sees a half-written link.
tmp="${LINK}.tmp.$$"
ln -sfn "$target" "$tmp" || die "cannot stage $tmp"
if ! mv -Tf "$tmp" "$LINK"; then
    rm -f "$tmp"
    die "cannot install $LINK"
fi

ztype="$(cat "$(dirname "$target")/type" 2>/dev/null || true)"
log "$LINK -> $target (${ztype:-unknown})"
