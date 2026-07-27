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

# A failed fetch is not a reason to skip the verification pass below: repairing
# runtime artifacts needs no network, only the checkout that's already here, and
# an offline station is exactly the one that can least afford to stay broken.
if git -C "$REPO_DIR" fetch --quiet origin "$BRANCH"; then
    git -C "$REPO_DIR" reset --hard --quiet "origin/$BRANCH"
else
    log "git fetch failed — verifying runtime artifacts against the current checkout"
fi
NEW="$(git -C "$REPO_DIR" rev-parse HEAD)"

if [ "$OLD" = "$NEW" ]; then
    log "Already up to date ($NEW) — verifying runtime artifacts"
else
    log "Updated $OLD -> $NEW on $BRANCH"
fi
chmod +x "$REPO_DIR"/scripts/*.sh "$REPO_DIR"/install 2>/dev/null || true

# Deliberately no early exit when OLD==NEW: every install below is gated on
# whether the installed artifact still matches the checkout, so this run is also
# the repair pass. It has to be, because of how this script gets updated — the
# tick that pulls a commit is executing the PREVIOUS update.sh, so any install
# block introduced by that commit never runs for the pull that delivered it.
# Without a later verifying tick, the station keeps the new code with its runtime
# pieces missing, forever.
CHANGED="$(git -C "$REPO_DIR" diff --name-only "$OLD" "$NEW")"
changed() { echo "$CHANGED" | grep -qx "$1"; }

# Gate installs on content, not on `changed` or on mere existence. `changed` is
# empty on a verifying tick, and an existence test can't see a copy that is
# present but stale — which is what a failed install leaves behind, since every
# install here ends in `|| true`. Note this means a hand-edit to a managed file
# is reverted on the next tick; these are fleet-managed artifacts.
stale() { ! cmp -s "$1" "$2"; }
stale_tree() { ! diff -rq -x '__pycache__' -x '*.pyc' "$1" "$2" >/dev/null 2>&1; }

# Refresh the tempcomp helper + sudoers rule. setup-tempcomp-sudo.sh installs
# the helper at /usr/local/bin, so comparing against that covers both it and the
# sudoers rule it authorizes.
if stale "$REPO_DIR/scripts/apply-tempcomp.sh" /usr/local/bin/apply-tempcomp.sh; then
    log "apply-tempcomp.sh out of date — refreshing helper and sudoers"
    CHRONY_MONITOR_USER="$MONITOR_USER" \
        bash "$REPO_DIR/scripts/setup-tempcomp-sudo.sh" || log "sudo setup refresh failed"
fi

# Refresh the serial-PPS init helper.
if stale "$REPO_DIR/scripts/init-serial-pps.sh" /usr/local/bin/init-serial-pps.sh; then
    log "init-serial-pps.sh out of date — reinstalling"
    install -m 0755 -o root -g root \
        "$REPO_DIR/scripts/init-serial-pps.sh" /usr/local/bin/init-serial-pps.sh || true
fi

# Refresh systemd service units and reload if any changed.
RELOAD=0
if stale "$REPO_DIR/systemd/serial-pps.service" /etc/systemd/system/serial-pps.service; then
    log "serial-pps.service out of date — reinstalling"
    install -m 0644 -o root -g root \
        "$REPO_DIR/systemd/serial-pps.service" /etc/systemd/system/serial-pps.service || true
    RELOAD=1
fi

# Refresh the root-owned copy of the package that the boot resolver imports.
# Compares the whole package, not just tempcomp.py, so the resolver never
# imports a half-updated one.
RESOLVER_LIB="${RESOLVER_LIB:-/usr/local/lib/chrony-monitor}"
if stale_tree "$REPO_DIR/chrony_monitor" "$RESOLVER_LIB/chrony_monitor"; then
    log "refreshing root-owned resolver library"
    rm -rf "$RESOLVER_LIB/chrony_monitor"
    install -d -m 0755 -o root -g root "$RESOLVER_LIB" "$RESOLVER_LIB/chrony_monitor" || true
    install -m 0644 -o root -g root \
        "$REPO_DIR"/chrony_monitor/*.py "$RESOLVER_LIB/chrony_monitor/" || true
fi

# Install/refresh the stable tempcomp sensor resolver + boot service, and
# migrate any raw thermal_zone path in chrony.conf onto the stable symlink.
if stale "$REPO_DIR/scripts/update-tempcomp-symlink.sh" /usr/local/bin/update-tempcomp-symlink.sh \
   || stale "$REPO_DIR/systemd/chrony-tempcomp-sensor.service" /etc/systemd/system/chrony-tempcomp-sensor.service; then
    log "tempcomp sensor service out of date — reinstalling"
    install -m 0755 -o root -g root \
        "$REPO_DIR/scripts/update-tempcomp-symlink.sh" /usr/local/bin/update-tempcomp-symlink.sh || true
    install -m 0644 -o root -g root \
        "$REPO_DIR/systemd/chrony-tempcomp-sensor.service" /etc/systemd/system/chrony-tempcomp-sensor.service || true
    systemctl daemon-reload
    systemctl enable chrony-tempcomp-sensor.service 2>/dev/null || true
    systemctl start chrony-tempcomp-sensor.service 2>/dev/null || true
    RELOAD=1
fi
# Idempotent: repoints a stale raw-zone tempcomp path once, no-ops thereafter.
if [ -x /usr/local/bin/update-tempcomp-symlink.sh ] && [ -f "$REPO_DIR/scripts/migrate-tempcomp-sensor.sh" ]; then
    bash "$REPO_DIR/scripts/migrate-tempcomp-sensor.sh" || log "tempcomp sensor migration skipped"
fi

# Self-update the updater's own units. (Not the script: ExecStart points into
# the checkout, which git already updated.)
if stale "$REPO_DIR/systemd/chrony-monitor-update.service" /etc/systemd/system/chrony-monitor-update.service \
   || stale "$REPO_DIR/systemd/chrony-monitor-update.timer" /etc/systemd/system/chrony-monitor-update.timer; then
    log "Updater units out of date — reinstalling"
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

if [ "$OLD" != "$NEW" ]; then
    log "Update applied; running monitor will re-exec into the new code shortly"
else
    log "Runtime artifacts verified"
fi
