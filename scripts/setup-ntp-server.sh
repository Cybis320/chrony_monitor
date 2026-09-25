#!/bin/bash
#
# Make chronyd serve NTP to the local network.
#
# chronyd only answers NTP clients whose address matches an `allow` directive;
# with none it is a client only. install.sh rewrites chrony.conf from a template
# on every re-provision, so an `allow` line added by hand there is lost. This
# script keeps it in a drop-in under /etc/chrony/conf.d instead, which
# install.sh never touches, and update.sh re-asserts it daily.
#
# Serves the private ranges (RFC 1918, IPv6 ULA and link-local): whatever LAN
# the station sits on, without making it an open public server. Site-specific
# rules can go in another conf.d file; chronyd merges them.
#
# Applies the rules to a running chronyd with `chronyc allow` rather than a
# restart, which would drop the PPS lock. Also opens UDP 123 for the same
# ranges if ufw is active. Idempotent.
#
# Usage: sudo setup-ntp-server.sh [-n|--dry-run]

set -uo pipefail

CHRONY_CONF="${CHRONY_CONF:-/etc/chrony/chrony.conf}"
CONF_DIR="${CONF_DIR:-/etc/chrony/conf.d}"
DROPIN="${DROPIN:-$CONF_DIR/ntp-server.conf}"

NETS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 fc00::/7 fe80::/10"

DRY_RUN=0
case "${1:-}" in
    -n|--dry-run) DRY_RUN=1 ;;
    "") ;;
    *) echo "usage: $0 [-n|--dry-run]" >&2; exit 2 ;;
esac

log() { echo "setup-ntp-server: $1"; }
die() { echo "setup-ntp-server: $1" >&2; exit 1; }

desired="# Managed by chrony-monitor (scripts/setup-ntp-server.sh); rewritten on update.
# Serve NTP to clients on private networks. Put site-specific rules in
# another file in this directory rather than editing this one."
for net in $NETS; do
    desired="$desired
allow $net"
done

if [ ! -f "$CHRONY_CONF" ]; then
    log "no $CHRONY_CONF -- nothing to do"
    exit 0
fi
# The drop-in is only read if chrony.conf includes the directory.
if ! grep -qE "^[[:space:]]*confdir[[:space:]]+$CONF_DIR/?[[:space:]]*$" "$CHRONY_CONF"; then
    log "$CHRONY_CONF has no 'confdir $CONF_DIR' -- add it, or an allow line, by hand"
    exit 1
fi

changed=1
if [ "$(cat "$DROPIN" 2>/dev/null)" = "$desired" ]; then
    changed=0
fi

if [ "$DRY_RUN" = 1 ]; then
    if [ "$changed" = 1 ]; then
        log "would write $DROPIN:"
        printf '%s\n' "$desired"
    else
        log "already configured in $DROPIN"
    fi
    exit 0
fi

[ "$(id -u)" -eq 0 ] || die "must run as root"

if [ "$changed" = 1 ]; then
    mkdir -p "$CONF_DIR" || die "cannot create $CONF_DIR"
    backup=""
    if [ -f "$DROPIN" ]; then
        backup="${DROPIN}.bak.chrony-monitor"
        cp -p "$DROPIN" "$backup" || die "cannot back up $DROPIN"
    fi

    tmp="$(mktemp)" || die "mktemp failed"
    printf '%s\n' "$desired" > "$tmp" || die "cannot stage $DROPIN"
    install -m 0644 -o root -g root "$tmp" "$DROPIN" || { rm -f "$tmp"; die "cannot write $DROPIN"; }
    rm -f "$tmp"

    if ! chronyd -p >/dev/null 2>&1; then
        log "chrony config validation failed -- restoring previous $DROPIN" >&2
        if [ -n "$backup" ]; then
            cp -p "$backup" "$DROPIN"
        else
            rm -f "$DROPIN"
        fi
        exit 1
    fi
    log "wrote $DROPIN"

    # conf.d is read only at startup; push the rules into the running daemon.
    if systemctl is-active --quiet chrony 2>/dev/null || systemctl is-active --quiet chronyd 2>/dev/null; then
        applied=1
        for net in $NETS; do
            chronyc allow "$net" >/dev/null 2>&1 || applied=0
        done
        if [ "$applied" = 1 ]; then
            log "applied to running chronyd"
        else
            log "could not apply to running chronyd; takes effect on next chrony restart"
        fi
    fi
else
    log "already configured in $DROPIN"
fi

# A host firewall would drop the requests before chronyd sees them.
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
    for net in $NETS; do
        ufw allow proto udp from "$net" to any port 123 >/dev/null 2>&1 \
            || log "ufw: could not open UDP 123 from $net"
    done
    log "ufw: UDP 123 open to private networks"
fi
