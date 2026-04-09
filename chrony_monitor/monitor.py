"""Main monitor loop for chrony sync monitoring."""

import curses
import time
from dataclasses import dataclass
from typing import Optional

from .status import get_status, get_gps_info, SyncState
from .display import Display
from .recovery import RecoveryManager, RecoveryConfig


@dataclass
class MonitorConfig:
    """Configuration for the monitor."""
    interval: float = 1.0           # Polling interval in seconds
    ntp_only: bool = False          # Force NTP-only mode
    recovery_enabled: bool = True   # Enable auto-recovery
    recovery_timeout: int = 60      # Seconds before recovery attempt
    recovery_cooldown: int = 300    # Seconds between recovery attempts


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
            # Get current status
            status = get_status(
                force_ntp_only=self.config.ntp_only,
                recovering=self.recovery_manager.is_recovering
            )
            status.gps = self._get_gps_cached()

            # Handle recovery logic for PPS mode
            if status.pps_expected and self.config.recovery_enabled:
                self._handle_recovery(status)

            # Render display
            lock_lost_seconds = None
            if status.sync_state in (SyncState.PPS_ISSUE, SyncState.RECOVERING, SyncState.NO_SYNC):
                lock_lost_seconds = self.recovery_manager.get_lock_lost_seconds()

            display.render(
                status=status,
                lock_lost_seconds=lock_lost_seconds,
                recovery_logs=self.recovery_manager.get_recent_logs()
            )

            time.sleep(self.config.interval)

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
    try:
        curses.wrapper(monitor.run)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nMonitor stopped.")
