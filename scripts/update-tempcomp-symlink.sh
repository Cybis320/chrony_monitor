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
# Usage: update-tempcomp-symlink.sh [link-path]

set -u

LINK="${1:-/run/chrony-monitor/tempcomp-sensor}"

# Where the package lives (for the authoritative Python resolver).
REPO_DIR="/opt/chrony_monitor"
if [ -f /etc/chrony-monitor/update.env ]; then
    # shellcheck disable=SC1091
    . /etc/chrony-monitor/update.env
fi

resolve_sensor() {
    # Authoritative path: reuse the monitor's own detect_temp_sensor() so the
    # boot service and the running monitor always agree on the sensor.
    if command -v python3 >/dev/null 2>&1; then
        local p
        p="$(cd "$REPO_DIR" 2>/dev/null && python3 -m chrony_monitor.tempcomp 2>/dev/null)"
        if [ -n "$p" ] && [ -r "$p" ]; then
            echo "$p"
            return 0
        fi
    fi

    # Fallback: dependency-free scan mirroring the Python priority order, in
    # case Python or the package is unavailable this early in boot.
    local pat z t
    for pat in 'pch' 'cpu[-_]?thermal' 'bcm2835' 'soc[-_]?thermal' 'x86_pkg_temp' 'coretemp'; do
        for z in /sys/class/thermal/thermal_zone*; do
            [ -r "$z/temp" ] || continue
            t="$(cat "$z/type" 2>/dev/null)" || continue
            if echo "$t" | grep -qiE "$pat"; then
                echo "$z/temp"
                return 0
            fi
        done
    done
    return 1
}

target="$(resolve_sensor)" || {
    echo "update-tempcomp-symlink: no suitable temperature sensor found" >&2
    exit 1
}

mkdir -p "$(dirname "$LINK")"
# Atomic replace so chrony never sees a half-written link.
tmp="${LINK}.tmp.$$"
ln -sfn "$target" "$tmp" && mv -Tf "$tmp" "$LINK"

ztype="$(cat "$(dirname "$target")/type" 2>/dev/null || true)"
echo "update-tempcomp-symlink: $LINK -> $target (${ztype:-unknown})"
