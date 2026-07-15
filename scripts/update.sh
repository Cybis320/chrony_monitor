#!/bin/bash
# Daily auto-updater for chrony-monitor (run as root by the systemd timer).
#
# Pulls the latest code into the /opt checkout and, only when the relevant
# root-owned files actually changed, refreshes them. It deliberately does NOT
# rewrite /etc/chrony/chrony.conf — that would wipe the live tempcomp
# calibration — and does NOT restart the monitor: the running TUI notices the
# new commit and re-execs itself.

set -euo pipefail

ENV_FILE="/etc/chrony-monitor/update.env"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

REPO_DIR="${REPO_DIR:-/opt/chrony_monitor}"
MONITOR_USER="${MONITOR_USER:-}"

log() { echo "[chrony-monitor-update] $1"; }

if [ ! -d "$REPO_DIR/.git" ]; then
    log "No git checkout at $REPO_DIR — nothing to update"
    exit 0
fi

BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
OLD="$(git -C "$REPO_DIR" rev-parse HEAD)"

if ! git -C "$REPO_DIR" fetch --quiet origin "$BRANCH"; then
    log "git fetch failed — will retry next run"
    exit 0
fi
git -C "$REPO_DIR" reset --hard --quiet "origin/$BRANCH"
NEW="$(git -C "$REPO_DIR" rev-parse HEAD)"

if [ "$OLD" = "$NEW" ]; then
    log "Already up to date ($NEW)"
    exit 0
fi

log "Updated $OLD -> $NEW on $BRANCH"
chmod +x "$REPO_DIR"/scripts/*.sh "$REPO_DIR"/install 2>/dev/null || true

CHANGED="$(git -C "$REPO_DIR" diff --name-only "$OLD" "$NEW")"
changed() { echo "$CHANGED" | grep -qx "$1"; }

# Refresh the tempcomp helper + sudoers rule only if the helper changed.
if changed "scripts/apply-tempcomp.sh"; then
    log "apply-tempcomp.sh changed — refreshing helper and sudoers"
    CHRONY_MONITOR_USER="$MONITOR_USER" \
        bash "$REPO_DIR/scripts/setup-tempcomp-sudo.sh" || log "sudo setup refresh failed"
fi

# Refresh the serial-PPS init helper.
if changed "scripts/init-serial-pps.sh"; then
    log "init-serial-pps.sh changed — reinstalling"
    install -m 0755 -o root -g root \
        "$REPO_DIR/scripts/init-serial-pps.sh" /usr/local/bin/init-serial-pps.sh || true
fi

# Refresh systemd service units and reload if any changed.
RELOAD=0
if changed "systemd/serial-pps.service"; then
    log "serial-pps.service changed — reinstalling"
    install -m 0644 -o root -g root \
        "$REPO_DIR/systemd/serial-pps.service" /etc/systemd/system/serial-pps.service || true
    RELOAD=1
fi

# Self-update the updater's own units/script.
if changed "systemd/chrony-monitor-update.service" || changed "systemd/chrony-monitor-update.timer"; then
    log "Updater units changed — reinstalling"
    install -m 0644 -o root -g root \
        "$REPO_DIR/systemd/chrony-monitor-update.service" /etc/systemd/system/chrony-monitor-update.service || true
    install -m 0644 -o root -g root \
        "$REPO_DIR/systemd/chrony-monitor-update.timer" /etc/systemd/system/chrony-monitor-update.timer || true
    RELOAD=1
fi

if [ "$RELOAD" -eq 1 ]; then
    systemctl daemon-reload
    systemctl enable --now chrony-monitor-update.timer 2>/dev/null || true
fi

# Warn (don't auto-run) if the full provisioner changed — re-running it would
# rewrite chrony.conf, so a human should decide.
if changed "scripts/install.sh"; then
    log "NOTE: install.sh changed; a manual re-provision may be warranted (it rewrites chrony.conf)"
fi

log "Update applied; running monitor will re-exec into the new code shortly"
