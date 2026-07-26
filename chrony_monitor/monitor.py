"""Main monitor loop for chrony sync monitoring."""

import curses
import os
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from .status import get_status, get_gps_info, SyncState
from .display import Display
from .recovery import RecoveryManager, RecoveryConfig
from .tempcomp import TempCompCollector, read_temperature


def _repo_dir() -> str:
    """Directory of the git checkout this package runs from."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _current_head(repo_dir: str) -> Optional[str]:
    """Return the current git commit of the checkout, or None if not a repo."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


@dataclass
class MonitorConfig:
    """Configuration for the monitor."""
    interval: float = 1.0           # Polling interval in seconds
    ntp_only: bool = False          # Force NTP-only mode
    recovery_enabled: bool = True   # Enable auto-recovery
    recovery_timeout: int = 60      # Seconds before recovery attempt
    recovery_cooldown: int = 300    # Seconds between recovery attempts
    tempcomp_enabled: bool = True   # Enable tempcomp monitoring
    tempcomp_sensor: Optional[str] = None  # None = auto-detect best sensor for this machine
    tempcomp_auto_recal: bool = True  # Enable auto-recalibration
    self_update: bool = True        # Re-exec when the git checkout is updated
    self_update_interval: int = 300  # Seconds between update checks


class Monitor:
    """Main chrony sync monitor."""

    def __init__(self, config: MonitorConfig = None):
        self.config = config or MonitorConfig()
        self.recovery_manager = RecoveryManager(
            RecoveryConfig(
                timeout_seconds=self.config.recovery_timeout,
                cooldown_seconds=self.config.recovery_cooldown,
                enabled=self.config.recovery_enabled
            )
        )
        self.running = False
        self._gps_cache = None
        self._gps_last_fetch = 0
        self.rms_recent = deque(maxlen=600)    # last 10 min at 1s resolution
        self.rms_minutes = deque(maxlen=1430)  # ~24h at 1-min resolution
        self._rms_minute_bucket = []
        self._rms_minute_count = 0
        self._last_good_status = None
        self._error_count = 0
        # Self-update: remember the commit we started on; re-exec if it moves.
        self._repo_dir = _repo_dir()
        self._start_head = _current_head(self._repo_dir) if self.config.self_update else None
        self._last_update_check = time.time()
        self._restart_requested = False
        self.tempcomp = None
        if self.config.tempcomp_enabled:
            self.tempcomp = TempCompCollector(
                sensor_path=self.config.tempcomp_sensor,
                auto_recal=self.config.tempcomp_auto_recal
            )
            self.tempcomp.load()

    def _get_gps_cached(self):
        """Get GPS info, refreshing every 10 seconds."""
        now = time.time()
        if now - self._gps_last_fetch >= 10:
            self._gps_cache = get_gps_info()
            self._gps_last_fetch = now
        return self._gps_cache

    def run(self, scr):
        """Main monitor loop (called by curses.wrapper)."""
        self.running = True
        display = Display(scr)

        while self.running:
            # Get current status — tolerate transient chronyc timeouts
            status = get_status(
                force_ntp_only=self.config.ntp_only,
                recovering=self.recovery_manager.is_recovering
            )
            if status.sync_state == SyncState.DAEMON_ERROR:
                self._error_count += 1
                if self._last_good_status and self._error_count < 3:
                    status = self._last_good_status
            else:
                self._error_count = 0
                self._last_good_status = status
            status.gps = self._get_gps_cached()

            # Record RMS history (1s recent + 1-min long-term)
            if status.tracking and status.tracking.rms_offset_us > 0:
                rms = status.tracking.rms_offset_us
                self.rms_recent.append(rms)
                self._rms_minute_bucket.append(rms)
                self._rms_minute_count += 1
                if self._rms_minute_count >= 60:
                    avg = sum(self._rms_minute_bucket) / len(self._rms_minute_bucket)
                    self.rms_minutes.append(avg)
                    self._rms_minute_bucket.clear()
                    self._rms_minute_count = 0

            # Record tempcomp data
            tempcomp_status = None
            if self.tempcomp and status.tracking:
                temp = read_temperature(self.tempcomp.sensor_path)
                if temp is not None and status.tracking.frequency_ppm != 0:
                    self.tempcomp.record(temp, status.tracking.frequency_ppm)
                tempcomp_status = self.tempcomp.get_status()

            # Handle recovery logic for PPS mode
            if status.pps_expected and self.config.recovery_enabled:
                self._handle_recovery(status)

            # Render display
            lock_lost_seconds = None
            if status.sync_state in (SyncState.PPS_ISSUE, SyncState.RECOVERING, SyncState.NO_SYNC):
                lock_lost_seconds = self.recovery_manager.get_lock_lost_seconds()

            # Combine long-term (1-min avg) + recent (1s) for the graph
            rms_combined = list(self.rms_minutes) + list(self.rms_recent)
            # Total elapsed seconds: 60s per minute sample + 1s per recent sample
            rms_duration = len(self.rms_minutes) * 60 + len(self.rms_recent)

            display.render(
                status=status,
                lock_lost_seconds=lock_lost_seconds,
                recovery_logs=self.recovery_manager.get_recent_logs(),
                rms_history=rms_combined,
                rms_duration=rms_duration,
                tempcomp_status=tempcomp_status
            )

            # Pick up code updates pulled by the background updater by re-exec'ing.
            if self._check_for_update():
                self._restart_requested = True
                self.running = False
                break

            time.sleep(self.config.interval)

    def _check_for_update(self) -> bool:
        """Return True if the git checkout has moved past our start commit."""
        if not self.config.self_update or self._start_head is None:
            return False
        now = time.time()
        if now - self._last_update_check < self.config.self_update_interval:
            return False
        self._last_update_check = now
        head = _current_head(self._repo_dir)
        return head is not None and head != self._start_head

    def _handle_recovery(self, status):
        """Handle recovery state machine."""
        from .status import SyncQuality
        is_healthy = (status.sync_state == SyncState.GPPS_LOCKED
                      and status.sync_quality != SyncQuality.DEGRADED)

        if is_healthy:
            self.recovery_manager.reset()
        else:
            self.recovery_manager.on_lock_lost()

            if self.recovery_manager.should_attempt_recovery():
                self.recovery_manager.attempt_recovery()

    def stop(self):
        """Stop the monitor loop."""
        self.running = False


def run_monitor(config: MonitorConfig = None):
    """Run the monitor with curses wrapper."""
    monitor = Monitor(config)
    restart = False
    try:
        curses.wrapper(monitor.run)
        restart = monitor._restart_requested
    except KeyboardInterrupt:
        pass
    finally:
        print("\nMonitor stopped.")

    # curses.wrapper has restored the terminal by now — safe to re-exec into
    # the freshly pulled code, keeping the same terminal/session and args.
    if restart:
        print("Update detected — restarting monitor...")
        os.execv(sys.executable, [sys.executable, "-m", "chrony_monitor"] + sys.argv[1:])
