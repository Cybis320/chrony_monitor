#!/bin/bash
#
# Keep the monitor user's ~/source/CC_Utils/chrony_monitor checkout on the
# commit of this root-owned checkout, and its launchers current.
#
# Run as root from the root-owned checkout (/opt/chrony_monitor) by install.sh
# and by the daily update.sh. Root never executes anything from the user's
# checkout: every git command and the launcher install below run AS the
# monitor user. The user checkout is what the desktop launcher and autostart
# run (the monitor is an unprivileged TUI); pinning it to the exact commit
# whose root-side helpers (sudoers, tempcomp, resolver) are installed keeps the
# two from skewing.
#
# Launchers: with --launchers (install.sh) they are (re)written to run from the
# user checkout; without it (the daily update.sh) only when this run created
# the checkout, which migrates installs whose launchers still point at /opt.
# A launcher someone edited is otherwise never touched.
#
# Exit status: 0 when the launchers run from the user checkout, non-zero when
# they were not installed (install.sh then falls back to its own).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_USER="${MONITOR_USER:-${SUDO_USER:-}}"
LAUNCHERS=0
[ "${1:-}" = "--launchers" ] && LAUNCHERS=1

log() { echo "[chrony-monitor-sync] $1"; }

if [ "$(id -u)" -ne 0 ]; then
    log "must run as root"
    exit 1
fi
if [ -z "$MONITOR_USER" ] || [ "$MONITOR_USER" = "root" ]; then
    log "no monitor user configured -- skipping the user checkout"
    exit 2
fi
USER_HOME="$(getent passwd "$MONITOR_USER" | cut -d: -f6)"
if [ -z "$USER_HOME" ] || [ ! -d "$USER_HOME" ]; then
    log "no home directory for $MONITOR_USER -- skipping the user checkout"
    exit 2
fi
DEST="${CHRONY_MONITOR_USER_DIR:-$USER_HOME/source/CC_Utils/chrony_monitor}"

as_user() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$MONITOR_USER" -- env HOME="$USER_HOME" "$@"
    else
        sudo -u "$MONITOR_USER" -H "$@"
    fi
}

# A manual install run from the user checkout itself: nothing to sync.
if [ "$(readlink -f "$DEST")" != "$(readlink -f "$REPO_DIR")" ]; then
    URL="$(git -C "$REPO_DIR" remote get-url origin)"
    BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"
    TARGET="$(git -C "$REPO_DIR" rev-parse HEAD)"
    # The root-owned checkout trips git's ownership check when the user reads
    # it ("dubious ownership"). The upload-pack serving it is a separate process
    # that ignores a plain `git -c`, so the exception has to ride on it. Root's
    # own checkout is trusted by construction.
    UPLOAD_PACK="git -c safe.directory=$REPO_DIR/.git -c safe.directory=$REPO_DIR upload-pack"

    if [ ! -d "$DEST/.git" ]; then
        if [ -e "$DEST" ]; then
            log "$DEST exists but is not a git checkout -- leaving it alone"
            exit 2
        fi
        log "Creating $DEST"
        as_user mkdir -p "$(dirname "$DEST")"
        # From the root copy: no network needed, and it has TARGET by construction.
        as_user git clone --quiet --no-local --upload-pack "$UPLOAD_PACK" "$REPO_DIR" "$DEST"
        as_user git -C "$DEST" remote set-url origin "$URL"
        LAUNCHERS=1
    fi

    if [ "$(as_user git -C "$DEST" rev-parse HEAD 2>/dev/null || true)" != "$TARGET" ]; then
        as_user git -C "$DEST" fetch --quiet --upload-pack "$UPLOAD_PACK" "$REPO_DIR" HEAD
        if ! as_user git -C "$DEST" diff --quiet 2>/dev/null; then
            as_user git -C "$DEST" stash push --quiet -m "chrony-monitor sync $(date -u +%Y%m%d-%H%M%S)" || true
            log "local edits in $DEST stashed"
        fi
        as_user git -C "$DEST" checkout --quiet -B "$BRANCH" "$TARGET"
        log "$DEST -> ${TARGET:0:8}"
    fi
fi

if [ ! -x "$DEST/scripts/install-desktop.sh" ]; then
    exit 2
fi
if [ "$LAUNCHERS" = "1" ]; then
    as_user bash "$DEST/scripts/install-desktop.sh"
fi
