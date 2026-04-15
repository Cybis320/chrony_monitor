"""Curses-based display for chrony sync monitoring."""

import curses
from typing import Optional

from .status import ChronyStatus, SyncState, SyncQuality
from .tempcomp import TempCompStatus


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


def format_accuracy_line(status: ChronyStatus) -> str:
    """Format accuracy metrics: StdDev, RMS, Skew."""
    parts = []
    if status.tracking:
        t = status.tracking
        if status.selected_source and status.selected_source.std_dev > 0:
            parts.append(f"StdDev {format_us(status.selected_source.std_dev * 1000)}")
        else:
            parts.append(f"Bound ±{format_us(t.root_dispersion_us)}")
        parts.append(f"RMS {format_us(t.rms_offset_us)}")
        parts.append(f"Skew {t.skew_ppm:.3f}ppm")
    if parts:
        return " | ".join(parts)
    if status.error_message:
        return status.error_message
    return "No tracking data"


def format_clock_line(status: ChronyStatus) -> str:
    """Format clock health: Freq, Poll, Stratum."""
    parts = []
    if status.tracking:
        t = status.tracking
        parts.append(f"Freq {t.frequency_ppm:.3f}ppm")
        parts.append(f"Poll {t.update_interval:.0f}s")
        parts.append(f"Stratum {t.stratum}")
    return " | ".join(parts)


def format_source_line(status: ChronyStatus) -> str:
    """Format source info with GPS if available."""
    parts = []
    if status.selected_source:
        src = status.selected_source
        parts.append(src.name)
        parts.append(f"Reach {src.reach}")
        parts.append(f"LastRx {src.last_rx}")
    if status.gps:
        g = status.gps
        parts.append(f"Sats {g.satellites_used}/{g.satellites_visible}")
        if g.tdop > 0:
            parts.append(f"TDOP {g.tdop:.2f}")
    if parts:
        return " | ".join(parts)
    if status.error_message:
        return status.error_message
    return "No source selected"


def format_us(us: float) -> str:
    """Format microseconds for display."""
    if us < 1.0:
        return f"{us * 1000:.0f}ns"
    if us < 1000.0:
        return f"{us:.1f}us"
    if us < 1000000.0:
        return f"{us / 1000:.1f}ms"
    return f"{us / 1000000:.2f}s"


def format_tempcomp_line(tc: TempCompStatus) -> str:
    """Format temperature compensation status line."""
    parts = []

    # State and calibration range
    if tc.config and tc.config.is_active:
        if tc.is_extrapolating and tc.cal_range:
            parts.append(f"OUTSIDE CAL ({tc.current_temp_c:.0f}C > {tc.cal_range[0]:.0f}-{tc.cal_range[1]:.0f}C)")
        elif tc.cal_range:
            parts.append(f"Active {tc.cal_range[0]:.0f}-{tc.cal_range[1]:.0f}C")
        else:
            parts.append("Active")
    else:
        parts.append("Off")

    # Current temperature and data range
    if tc.current_temp_c is not None:
        if tc.temp_range:
            parts.append(f"{tc.current_temp_c:.1f}C (data {tc.temp_range[0]:.0f}-{tc.temp_range[1]:.0f}C)")
        else:
            parts.append(f"{tc.current_temp_c:.1f}C")

    # Collection status
    if tc.sample_count > 0:
        dur = tc.collection_duration_s
        if dur >= 3600:
            dur_str = f"{dur // 3600}h{(dur % 3600) // 60:02d}m"
        elif dur >= 60:
            dur_str = f"{dur // 60}m"
        else:
            dur_str = f"{dur}s"
        parts.append(f"{tc.sample_count} samples ({dur_str})")
    else:
        parts.append("Collecting")

    # Correlation
    if tc.correlation is not None:
        parts.append(f"R2={tc.correlation:.4f}")

    return " | ".join(parts)


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
        recovery_logs: Optional[list] = None,
        rms_history: Optional[list] = None,
        rms_duration: int = 0,
        tempcomp_status: Optional[TempCompStatus] = None,
        converging: bool = False
    ):
        """Render the display with current status."""
        color = get_color_for_status(status)
        self.scr.bkgd(" ", curses.color_pair(color))
        self.scr.erase()

        h, w = self.scr.getmaxyx()
        row = 1

        # Main banner
        banner = get_banner_text(status)
        if converging:
            banner += " - CONVERGING"
        self._addstr_centered(row, banner, curses.A_BOLD)
        row += 2

        # Accuracy section
        row = self._render_section(row, w, "Accuracy", format_accuracy_line(status))

        # Clock section
        row = self._render_section(row, w, "Clock", format_clock_line(status))

        # Source section
        row = self._render_section(row, w, "Source", format_source_line(status))

        # TempComp section
        if tempcomp_status is not None:
            tc_line = format_tempcomp_line(tempcomp_status)
            tc_attr = curses.A_DIM
            if tempcomp_status.is_extrapolating:
                tc_attr = curses.color_pair(Color.YELLOW)
            row = self._render_section(row, w, "TempComp", tc_line, tc_attr)

        # Lock lost timer (if applicable)
        if lock_lost_seconds is not None and lock_lost_seconds > 0:
            timer_info = f"Lock lost for: {lock_lost_seconds}s"
            if status.sync_state == SyncState.RECOVERING:
                timer_info += " [AUTO-RECOVERY]"
            self._addstr_centered(row, timer_info)
            row += 1

        # Recovery logs (if any)
        if recovery_logs:
            row += 1
            if row < h - 4:
                self._addstr(row, 2, "Recovery Log:", curses.A_BOLD)
                for i, log in enumerate(recovery_logs[-4:], 1):
                    if row + i < h - 2:
                        self._addstr(row + i, 4, log[:w - 5])
                row += min(len(recovery_logs), 4) + 1

        # RMS offset graph — fills remaining space
        if rms_history and len(rms_history) >= 2:
            row += 1
            self._render_rms_graph(rms_history, row, h - 2, w, rms_duration)

        # Footer instructions
        if h > 10:
            footer = "Ctrl+C to exit"
            if status.pps_expected:
                footer = "Auto-recovery enabled | " + footer
            self._addstr(h - 1, 2, footer, curses.A_DIM)

        self.scr.refresh()

    def _render_rms_graph(self, history: list, top: int, bottom: int, width: int, duration: int = 0):
        """Render ASCII graph of RMS offset history."""
        graph_height = bottom - top
        if graph_height < 3:
            return

        # Layout: label_width + 1 (axis) + graph_width
        label_width = 7  # e.g. "100ns "
        graph_width = width - label_width - 3
        if graph_width < 10:
            return

        # Downsample to fit graph width — each column is the avg of a bucket
        all_samples = history
        n = len(all_samples)
        if n <= graph_width:
            samples = all_samples
        else:
            bucket_size = n / graph_width
            samples = []
            for i in range(graph_width):
                start = int(i * bucket_size)
                end = int((i + 1) * bucket_size)
                bucket = all_samples[start:end]
                samples.append(sum(bucket) / len(bucket))

        # Auto-scale Y axis
        max_val = max(samples)
        min_val = min(samples)
        if max_val == min_val:
            max_val = min_val + 1

        # Add 10% headroom
        y_range = max_val - min_val
        y_min = max(0, min_val - y_range * 0.05)
        y_max = max_val + y_range * 0.05
        y_range = y_max - y_min

        # Block characters for sub-row resolution (8 levels per row)
        blocks = " ▁▂▃▄▅▆▇█"

        # Header
        if duration <= 0:
            duration = len(all_samples)
        if duration >= 3600:
            dur_str = f"{duration // 3600}h{(duration % 3600) // 60:02d}m"
        elif duration >= 60:
            dur_str = f"{duration // 60}m{duration % 60:02d}s"
        else:
            dur_str = f"{duration}s"
        header = f"RMS Offset ({dur_str})"
        self._addstr(top, label_width + 1, header, curses.A_DIM)

        # Draw graph — medium shade fill style
        graph_top = top + 1
        graph_rows = graph_height - 2  # leave room for header and bottom label
        if graph_rows < 2:
            return

        for row_idx in range(graph_rows):
            row = graph_top + row_idx
            row_max = y_max - (row_idx * y_range / graph_rows)
            row_min = y_max - ((row_idx + 1) * y_range / graph_rows)

            # Y-axis label (top and bottom rows only)
            if row_idx == 0:
                label = format_us(y_max)
            elif row_idx == graph_rows - 1:
                label = format_us(y_min)
            else:
                label = ""
            self._addstr(row, 0, f"{label:>{label_width}}", curses.A_DIM)

            # Draw fill for each sample
            line = ""
            for val in samples:
                if val >= row_max:
                    line += "▒"
                elif val <= row_min:
                    line += " "
                else:
                    line += "▒"
            self._addstr(row, label_width + 1, line)

    LABEL_WIDTH = 10  # right-align labels to this width
    CONTENT_COL = 15  # content starts at this column (after "Label >>> ")

    def _render_section(self, row: int, w: int, label: str, content: str,
                        content_attr: int = 0) -> int:
        """Render a section as: 'Label >>>  content' on one line."""
        prefix = f"{label:>{self.LABEL_WIDTH}} >>> "
        self._addstr(row, 1, prefix, curses.A_DIM)
        self._addstr(row, 2 + len(prefix), content, content_attr)
        row += 1
        return row

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
