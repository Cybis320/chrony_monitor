#!/bin/bash
#
# Let chronyd read whichever sysfs temperature node the tempcomp resolver picks.
#
# Debian/Ubuntu confine chronyd with AppArmor (/etc/apparmor.d/usr.sbin.chronyd).
# The stock profile allows only hwmon nodes that hang off a thermal zone
# (.../thermal_zone*/hwmon*/temp*_input). A zone's own `temp` node, and hwmon
# devices that are not thermal zones (coretemp, k10temp, ...), are denied.
# AppArmor mediates the *resolved* sysfs path, so pointing chrony.conf at the
# /run/chrony-monitor/tempcomp-sensor symlink does not help by itself.
#
# A denied sensor fails silently: chronyd logs "Could not read temperature"
# every interval and applies no compensation -- exactly the failure this
# project exists to prevent, and it looks different on every machine because
# which zone wins the ranking (and whether it has an hwmon mirror) does.
#
# Adds a managed block to the profile's local include -- the hook the stock
# profile provides for site rules -- and reloads the profile. Idempotent, and a
# no-op on machines without AppArmor or without a chronyd profile.
#
# Usage: sudo setup-chronyd-apparmor.sh [-n|--dry-run]

set -uo pipefail

PROFILE="${PROFILE:-/etc/apparmor.d/usr.sbin.chronyd}"
LOCAL="${LOCAL:-/etc/apparmor.d/local/usr.sbin.chronyd}"

BEGIN_MARK="# BEGIN chrony-monitor tempcomp (managed by setup-chronyd-apparmor.sh)"
END_MARK="# END chrony-monitor tempcomp"
BLOCK="$BEGIN_MARK
# chronyd reads its tempcomp sensor through /run/chrony-monitor/tempcomp-sensor.
# AppArmor checks the resolved sysfs node; allow every thermal-zone and hwmon
# temperature node so whichever sensor the resolver picks is readable.
@{sys}/devices/**/thermal_zone[0-9]*/temp r,
@{sys}/devices/**/hwmon[0-9]*/temp[0-9]*_input r,
$END_MARK"

DRY_RUN=0
case "${1:-}" in
    -n|--dry-run) DRY_RUN=1 ;;
    "") ;;
    *) echo "usage: $0 [-n|--dry-run]" >&2; exit 2 ;;
esac

log() { echo "setup-chronyd-apparmor: $1"; }
die() { echo "setup-chronyd-apparmor: $1" >&2; exit 1; }

if [ ! -f "$PROFILE" ]; then
    log "no chronyd AppArmor profile at $PROFILE -- nothing to do"
    exit 0
fi
if ! command -v apparmor_parser >/dev/null 2>&1; then
    log "apparmor_parser not installed -- nothing to do"
    exit 0
fi

# What the local include should contain: whatever is there now, minus any
# previous copy of our block, plus the current block at the end.
existing="$(cat "$LOCAL" 2>/dev/null || true)"
desired="$(printf '%s\n' "$existing" \
    | sed '/^# BEGIN chrony-monitor tempcomp/,/^# END chrony-monitor tempcomp$/d' \
    | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')"
if [ -n "$desired" ]; then
    desired="$desired
$BLOCK"
else
    desired="$BLOCK"
fi

if [ "$existing" = "$desired" ]; then
    log "already configured in $LOCAL"
    exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
    log "would write $LOCAL:"
    printf '%s\n' "$desired"
    exit 0
fi

[ "$(id -u)" -eq 0 ] || die "must run as root"

# Only reload into a kernel that is actually running AppArmor; on one that
# isn't, a parser reload fails and the local file would still be right for the
# next boot, so just write it.
aa_live=0
[ -d /sys/kernel/security/apparmor ] && aa_live=1

mkdir -p "$(dirname "$LOCAL")" || die "cannot create $(dirname "$LOCAL")"
backup=""
if [ -f "$LOCAL" ]; then
    backup="${LOCAL}.bak.chrony-monitor"
    cp -p "$LOCAL" "$backup" || die "cannot back up $LOCAL"
fi

tmp="$(mktemp)" || die "mktemp failed"
printf '%s\n' "$desired" > "$tmp" || die "cannot stage new local include"
install -m 0644 -o root -g root "$tmp" "$LOCAL" || { rm -f "$tmp"; die "cannot write $LOCAL"; }
rm -f "$tmp"

if [ "$aa_live" = 1 ]; then
    if ! apparmor_parser -r "$PROFILE"; then
        log "profile reload failed -- restoring previous local include" >&2
        if [ -n "$backup" ]; then
            cp -p "$backup" "$LOCAL"
        else
            : > "$LOCAL"
        fi
        apparmor_parser -r "$PROFILE" >/dev/null 2>&1 || true
        exit 1
    fi
    log "installed sensor rules in $LOCAL and reloaded $PROFILE"
else
    log "installed sensor rules in $LOCAL (AppArmor not active; applies at next boot)"
fi
