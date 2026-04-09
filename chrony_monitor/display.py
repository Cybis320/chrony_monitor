"""Curses-based display for chrony sync monitoring."""

import curses
from typing import Optional

from .status import ChronyStatus, SyncState, SyncQuality


class Color:
    """Color pair indices."""
    GREEN = 1   # Excellent sync (GPPS or exceptional NTP)
    BLUE = 2    # Good NTP sync
    YELLOW = 3  # Degraded/Warning/Recovering
    RED = 4     # Error/No sync


def init_colors():
    """Initialize curses color pairs."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(Color.GREEN, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(Color.BLUE, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(Color.YELLOW, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    curses.init_pair(Color.RED, curses.COLOR_WHITE, curses.COLOR_RED)


def get_color_for_status(status: ChronyStatus) -> int:
    """Determine display color based on sync status."""
    state = status.sync_state
    quality = status.sync_quality

    if state == SyncState.DAEMON_ERROR:
        return Color.RED

    if state == SyncState.NO_SYNC:
        return Color.RED

    if state == SyncState.PPS_ISSUE:
        return Color.YELLOW

    if state == SyncState.RECOVERING:
        return Color.YELLOW

    if state == SyncState.GPPS_LOCKED:
        # GPPS locked - green for excellent, yellow for degraded
        if quality == SyncQuality.EXCELLENT:
            return Color.GREEN
        if quality == SyncQuality.GOOD:
            return Color.GREEN
        return Color.YELLOW

    if state == SyncState.NTP_SYNCED:
        # NTP mode - green only for excellent, otherwise blue
        if quality == SyncQuality.EXCELLENT:
            return Color.GREEN
        if quality == SyncQuality.GOOD:
            return Color.BLUE
        return Color.YELLOW

    return Color.RED


def get_banner_text(status: ChronyStatus) -> str:
    """Get main banner text for display."""
    state = status.sync_state

    if state == SyncState.DAEMON_ERROR:
        return "CHRONY DOWN"

    if state == SyncState.NO_SYNC:
        return "NO SYNC"

    if state == SyncState.PPS_ISSUE:
        return "PPS ISSUE"

    if state == SyncState.RECOVERING:
        return "RECOVERING"

    if state == SyncState.GPPS_LOCKED:
        return "GPPS LOCKED"

    if state == SyncState.NTP_SYNCED:
        return "NTP SYNCED"

    return "UNKNOWN"


def format_offset(offset_ms: Optional[float]) -> str:
    """Format offset for display."""
    if offset_ms is None or offset_ms == float('inf'):
        return "-"

    if offset_ms < 0.001:
        return f"{offset_ms * 1000000:.0f}ns"
    if offset_ms < 1.0:
        return f"{offset_ms * 1000:.0f}us"
    if offset_ms < 1000.0:
        return f"{offset_ms:.1f}ms"
    return f"{offset_ms / 1000:.2f}s"


def format_confidence_line(status: ChronyStatus) -> str:
    """Format time confidence line."""
    parts = []
    if status.tracking:
        t = status.tracking
        parts.append(f"Bound ±{format_us(t.root_dispersion_us)}")
        parts.append(f"RMS {format_us(t.rms_offset_us)}")
        parts.append(f"Skew {t.skew_ppm:.2f}ppm")
        parts.append(f"Poll {t.update_interval:.0f}s")
        parts.append(f"Stratum {t.stratum}")
    if parts:
        return " | ".join(parts)
    if status.error_message:
        return status.error_message
    return "No tracking data"


def format_us(us: float) -> str:
    """Format microseconds for display."""
    if us < 1.0:
        return f"{us * 1000:.0f}ns"
    if us < 1000.0:
        return f"{us:.1f}us"
    if us < 1000000.0:
        return f"{us / 1000:.1f}ms"
    return f"{us / 1000000:.2f}s"


def format_gps_line(status: ChronyStatus) -> str:
    """Format GPS satellite info line."""
    if not status.gps:
        return ""
    g = status.gps
    parts = [f"Sats {g.satellites_used}/{g.satellites_visible}"]
    if g.tdop > 0:
        parts.append(f"TDOP {g.tdop:.2f}")
    return " | ".join(parts)


def format_source_info(status: ChronyStatus) -> str:
    """Format source information line."""
    if status.selected_source:
        src = status.selected_source
        return f"Source: {src.name} | Reach {src.reach} | LastRx {src.last_rx}"
    if status.error_message:
        return status.error_message
    return "No source selected"


class Display:
    """Curses display manager."""

    def __init__(self, scr):
        self.scr = scr
        curses.curs_set(0)
        init_colors()

    def render(
        self,
        status: ChronyStatus,
        lock_lost_seconds: Optional[int] = None,
        recovery_logs: Optional[list] = None
    ):
        """Render the display with current status."""
        color = get_color_for_status(status)
        self.scr.bkgd(" ", curses.color_pair(color))
        self.scr.erase()

        h, w = self.scr.getmaxyx()
        mid = h // 2

        # Main banner
        banner = get_banner_text(status)
        self._addstr_centered(mid - 2, banner, curses.A_BOLD)

        # Confidence line
        confidence = format_confidence_line(status)
        self._addstr_centered(mid, confidence)

        # Source info line
        info = format_source_info(status)
        self._addstr_centered(mid + 1, info, curses.A_DIM)

        # GPS satellite info
        gps_line = format_gps_line(status)
        if gps_line:
            self._addstr_centered(mid + 2, gps_line, curses.A_DIM)

        # Lock lost timer (if applicable)
        if lock_lost_seconds is not None and lock_lost_seconds > 0:
            timer_line = mid + 4
            timer_info = f"Lock lost for: {lock_lost_seconds}s"
            if status.sync_state == SyncState.RECOVERING:
                timer_info += " [AUTO-RECOVERY]"
            self._addstr_centered(timer_line, timer_info)

        # Recovery logs (if any)
        if recovery_logs:
            log_start = mid + 6
            if log_start < h - 4:
                self._addstr(log_start, 2, "Recovery Log:", curses.A_BOLD)
                for i, log in enumerate(recovery_logs[-4:], 1):
                    if log_start + i < h - 2:
                        self._addstr(log_start + i, 4, log[:w - 5])

        # Footer instructions
        if h > 10:
            footer = "Ctrl+C to exit"
            if status.pps_expected:
                footer = "Auto-recovery enabled | " + footer
            self._addstr(h - 2, 2, footer, curses.A_DIM)

        self.scr.refresh()

    def _addstr_centered(self, row: int, text: str, attr: int = 0):
        """Add string centered on screen."""
        h, w = self.scr.getmaxyx()
        if row < 0 or row >= h:
            return
        col = max(0, (w - len(text)) // 2)
        try:
            self.scr.addstr(row, col, text[:w - 1], attr)
        except curses.error:
            pass

    def _addstr(self, row: int, col: int, text: str, attr: int = 0):
        """Add string at position with bounds checking."""
        h, w = self.scr.getmaxyx()
        if row < 0 or row >= h or col < 0 or col >= w:
            return
        try:
            self.scr.addstr(row, col, text[:w - col - 1], attr)
        except curses.error:
            pass
