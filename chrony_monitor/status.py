"""Status checking and mode detection for chrony sync monitoring."""

import glob
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SyncState(Enum):
    """Overall synchronization state."""
    GPPS_LOCKED = "gpps_locked"
    NTP_SYNCED = "ntp_synced"
    PPS_ISSUE = "pps_issue"
    RECOVERING = "recovering"
    NO_SYNC = "no_sync"
    DAEMON_ERROR = "daemon_error"


class SyncQuality(Enum):
    """Quality level of synchronization."""
    EXCELLENT = "excellent"  # <1ms offset
    GOOD = "good"            # <50ms offset
    DEGRADED = "degraded"    # >50ms offset
    NONE = "none"            # No sync


@dataclass
class SourceInfo:
    """Information about a chrony time source."""
    name: str
    mode: str           # '^' for server, '#' for local refclock
    state: str          # '*' selected, '+' combined, '-' not combined, etc.
    stratum: int
    poll: int
    reach: str
    last_rx: str
    offset: float       # in milliseconds
    error: float        # in milliseconds
    std_dev: float      # in milliseconds, from sourcestats
    is_selected: bool
    is_pps: bool
    is_gps: bool


@dataclass
class TrackingInfo:
    """Chrony tracking data for time confidence."""
    stratum: int = 0
    root_dispersion_us: float = 0.0   # Error bound in microseconds
    rms_offset_us: float = 0.0        # Typical offset in microseconds
    frequency_ppm: float = 0.0
    skew_ppm: float = 0.0
    update_interval: float = 0.0      # seconds


@dataclass
class GpsInfo:
    """GPS satellite and geometry info."""
    satellites_used: int = 0
    satellites_visible: int = 0
    tdop: float = 0.0    # Time DOP
    hdop: float = 0.0
    pdop: float = 0.0


@dataclass
class ChronyStatus:
    """Complete status of chrony synchronization."""
    sources: list
    selected_source: Optional[SourceInfo]
    sync_state: SyncState
    sync_quality: SyncQuality
    offset_ms: Optional[float]
    error_message: Optional[str]
    usb_gps_detected: bool
    pps_expected: bool
    tracking: Optional[TrackingInfo] = None
    gps: Optional[GpsInfo] = None


# Regex to match the selected source line (starts with * after mode char)
SELECTED_PATTERN = re.compile(r'^\s*[#^]\*')
STAR_PATTERN = re.compile(r'^\s*[#^=~?+-]*\*')


def parse_lastrx(field: str) -> float:
    """Convert chronyc LastRx field to seconds."""
    if not field or field == '-':
        return float('inf')
    field = field.strip()
    if field.endswith('m'):
        return float(field[:-1]) * 60
    if field.endswith('h'):
        return float(field[:-1]) * 3600
    if field.endswith('d'):
        return float(field[:-1]) * 86400
    return float(field.rstrip('s') or 0)


def parse_offset(field: str) -> float:
    """Parse offset field to milliseconds."""
    if not field or field == '-':
        return float('inf')
    field = field.strip()
    # Offset is typically in format like "+123us", "-45ms", "+1.2s"
    multiplier = 1.0
    if field.endswith('ns'):
        multiplier = 0.000001
        field = field[:-2]
    elif field.endswith('us'):
        multiplier = 0.001
        field = field[:-2]
    elif field.endswith('ms'):
        multiplier = 1.0
        field = field[:-2]
    elif field.endswith('s'):
        multiplier = 1000.0
        field = field[:-1]
    try:
        return float(field) * multiplier
    except ValueError:
        return float('inf')


def has_usb_gps() -> bool:
    """Check if a USB GPS device is present."""
    # Check for common USB GPS device paths
    gps_patterns = [
        '/dev/ttyACM*',
        '/dev/ttyUSB*',
        '/dev/gps*',
    ]
    for pattern in gps_patterns:
        if glob.glob(pattern):
            return True

    # Also check if gpsd is configured with a device
    try:
        with open('/etc/default/gpsd', 'r') as f:
            content = f.read()
            if '/dev/tty' in content and 'DEVICES=' in content:
                return True
    except (FileNotFoundError, PermissionError):
        pass

    return False


def has_pps_device() -> bool:
    """Check if a GPS PPS device exists (GPIO or serial)."""
    if os.path.exists('/dev/gps-pps') or os.path.exists('/dev/serial-pps'):
        return True
    # Check sysfs for GPIO PPS (RPi) or serial PPS
    for name_path in glob.glob('/sys/class/pps/pps*/name'):
        try:
            with open(name_path) as f:
                name = f.read().strip()
            if name.startswith(('pps@', 'pps-gpio', 'serial')):
                return True
        except (OSError, PermissionError):
            continue
    return False


def get_tracking_info() -> Optional[TrackingInfo]:
    """Get chrony tracking data."""
    try:
        out = subprocess.check_output(
            ["chronyc", "tracking"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5
        )
    except Exception:
        return None

    info = TrackingInfo()
    for line in out.splitlines():
        if ':' not in line:
            continue
        key, val = line.split(':', 1)
        key = key.strip()
        val = val.strip()

        if key == 'Stratum':
            try:
                info.stratum = int(val)
            except ValueError:
                pass
        elif key == 'Root dispersion':
            info.root_dispersion_us = _parse_seconds_to_us(val)
        elif key == 'RMS offset':
            info.rms_offset_us = _parse_seconds_to_us(val)
        elif key == 'Frequency':
            match = re.match(r'([+-]?\d+\.?\d*)\s*ppm\s*(fast|slow)?', val)
            if match:
                freq = float(match.group(1))
                if match.group(2) == 'slow':
                    freq = -freq
                info.frequency_ppm = freq
        elif key == 'Skew':
            match = re.match(r'([+-]?\d+\.?\d*)', val)
            if match:
                info.skew_ppm = float(match.group(1))
        elif key == 'Update interval':
            match = re.match(r'([+-]?\d+\.?\d*)', val)
            if match:
                info.update_interval = float(match.group(1))

    return info


def _parse_seconds_to_us(val: str) -> float:
    """Parse chrony seconds field to microseconds."""
    match = re.match(r'([+-]?\d+\.?\d*)\s*seconds', val)
    if match:
        return abs(float(match.group(1))) * 1_000_000
    return 0.0


def get_gps_info() -> Optional[GpsInfo]:
    """Get GPS satellite info from gpsd."""
    try:
        out = subprocess.check_output(
            ["gpspipe", "-w", "-n", "15"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5
        )
    except Exception:
        return None

    info = GpsInfo()
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if msg.get('class') == 'SKY':
            if 'uSat' in msg:
                info.satellites_used = msg['uSat']
            if 'nSat' in msg:
                info.satellites_visible = msg['nSat']
            if 'tdop' in msg:
                info.tdop = msg['tdop']
            if 'hdop' in msg:
                info.hdop = msg['hdop']
            if 'pdop' in msg:
                info.pdop = msg['pdop']

    return info if info.satellites_used > 0 else None


def get_sourcestats() -> dict:
    """
    Get source statistics from chronyc sourcestats.
    Returns: dict mapping source name to std_dev in milliseconds.
    """
    try:
        out = subprocess.check_output(
            ["chronyc", "sourcestats", "-n"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}

    # Example: GPPS                        7   4    96     -0.001      0.038     -7ns   464ns
    # Columns: Name NP NR Span Frequency FreqSkew Offset StdDev
    stats = {}
    for line in out.splitlines():
        if not line.strip() or line.startswith('Name') or line.startswith('=='):
            continue
        cols = line.split()
        if len(cols) >= 8:
            name = cols[0]
            std_dev = parse_offset(cols[-1])  # last column is Std Dev
            stats[name] = abs(std_dev)
    return stats


def get_chrony_sources() -> tuple:
    """
    Get chrony sources information.
    Returns: (success: bool, sources: list[SourceInfo], error: str|None)
    """
    try:
        out = subprocess.check_output(
            ["chronyc", "sources", "-n"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5
        )
    except FileNotFoundError:
        return False, [], "chronyc not found"
    except subprocess.CalledProcessError as e:
        return False, [], e.output.strip() or "chronyd not running"
    except subprocess.TimeoutExpired:
        return False, [], "chronyc timeout"

    # Get sourcestats for std_dev values
    stats = get_sourcestats()

    sources = []
    for line in out.splitlines():
        # Skip header lines
        if not line.strip() or line.startswith('MS') or line.startswith('=='):
            continue

        # Parse source line
        # Format: MS Name/IP address         Stratum Poll Reach LastRx Last sample
        # Example: #* GPPS                         0   4   377     1   +123ns[+456ns] +/- 789ns
        cols = line.split()
        if len(cols) < 8:
            continue

        mode_state = cols[0]
        if len(mode_state) < 2:
            continue

        mode = mode_state[0]  # '^' or '#'
        state = mode_state[1] if len(mode_state) > 1 else ' '

        name = cols[1]

        try:
            stratum = int(cols[2])
            poll = int(cols[3])
        except ValueError:
            continue

        reach = cols[4]
        last_rx = cols[5]

        # Parse offset from "Last sample" column
        # Format: +123ns[+456ns] or similar
        offset_str = cols[6] if len(cols) > 6 else '0'
        # Extract the first offset value (before '[')
        offset_match = re.match(r'([+-]?\d+\.?\d*[a-z]*)', offset_str)
        offset = parse_offset(offset_match.group(1)) if offset_match else 0.0

        # Error margin from last column
        error_str = cols[-1] if len(cols) > 7 else '0'
        error = parse_offset(error_str)

        is_selected = state == '*'
        is_pps = 'PPS' in name.upper() or 'GPPS' in name.upper()
        is_gps = 'GPS' in name.upper() or 'GPPS' in name.upper() or 'NMEA' in name.upper()

        source = SourceInfo(
            name=name,
            mode=mode,
            state=state,
            stratum=stratum,
            poll=poll,
            reach=reach,
            last_rx=last_rx,
            offset=abs(offset),
            error=abs(error),
            std_dev=stats.get(name, 0.0),
            is_selected=is_selected,
            is_pps=is_pps,
            is_gps=is_gps
        )
        sources.append(source)

    return True, sources, None


def determine_sync_quality(offset_ms: float) -> SyncQuality:
    """Determine sync quality based on offset."""
    if offset_ms == float('inf'):
        return SyncQuality.NONE
    if offset_ms < 1.0:  # < 1ms
        return SyncQuality.EXCELLENT
    if offset_ms < 50.0:  # < 50ms
        return SyncQuality.GOOD
    return SyncQuality.DEGRADED


def get_status(force_ntp_only: bool = False, recovering: bool = False) -> ChronyStatus:
    """
    Get complete chrony synchronization status.

    Args:
        force_ntp_only: If True, don't expect PPS even if USB GPS detected
        recovering: If True, indicate recovery is in progress
    """
    usb_gps = has_usb_gps()
    pps_device = has_pps_device()
    pps_expected = usb_gps and not force_ntp_only

    success, sources, error = get_chrony_sources()
    tracking = get_tracking_info()

    if not success:
        return ChronyStatus(
            sources=[],
            selected_source=None,
            sync_state=SyncState.DAEMON_ERROR,
            sync_quality=SyncQuality.NONE,
            offset_ms=None,
            error_message=error,
            usb_gps_detected=usb_gps,
            pps_expected=pps_expected,
            tracking=tracking
        )

    # Find selected source
    selected = next((s for s in sources if s.is_selected), None)

    if not selected:
        # No selected source
        if pps_expected:
            return ChronyStatus(
                sources=sources,
                selected_source=None,
                sync_state=SyncState.PPS_ISSUE if not recovering else SyncState.RECOVERING,
                sync_quality=SyncQuality.NONE,
                offset_ms=None,
                error_message="No active time source",
                usb_gps_detected=usb_gps,
                pps_expected=pps_expected,
                tracking=tracking
            )
        return ChronyStatus(
            sources=sources,
            selected_source=None,
            sync_state=SyncState.NO_SYNC,
            sync_quality=SyncQuality.NONE,
            offset_ms=None,
            error_message="No active time source",
            usb_gps_detected=usb_gps,
            pps_expected=pps_expected,
            tracking=tracking
        )

    offset_ms = selected.offset
    quality = determine_sync_quality(offset_ms)

    # Determine state based on source type and expectations
    source_stale = selected.reach == '0' or parse_lastrx(selected.last_rx) > 120

    if selected.is_pps:
        # PPS source selected but check if it's still reachable
        if source_stale:
            state = SyncState.PPS_ISSUE if not recovering else SyncState.RECOVERING
        else:
            state = SyncState.GPPS_LOCKED
    elif pps_expected:
        # We expect PPS but it's not the selected source
        if recovering:
            state = SyncState.RECOVERING
        else:
            state = SyncState.PPS_ISSUE
    else:
        # NTP-only mode
        state = SyncState.NTP_SYNCED

    return ChronyStatus(
        sources=sources,
        selected_source=selected,
        sync_state=state,
        sync_quality=quality,
        offset_ms=offset_ms,
        error_message=None,
        usb_gps_detected=usb_gps,
        pps_expected=pps_expected,
        tracking=tracking
    )
