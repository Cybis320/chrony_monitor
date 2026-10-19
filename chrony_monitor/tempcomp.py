"""Temperature compensation calibration for chrony."""

import bisect
import glob
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger(__name__)

# Minimum thresholds for statistical analysis
MIN_SAMPLES_CORRELATION = 60    # 1 hour of minute-averaged data
MIN_TEMP_RANGE_CORRELATION = 2.0  # °C
MIN_SAMPLES_FIT = 360           # 6 hours
MIN_TEMP_RANGE_FIT = 5.0        # °C
MAX_DATA_DAYS = 30
MAX_SAMPLES = 43200             # 30 days at 1-min resolution

# Stuck-sensor detection: no temperature movement at all across a fit's worth of
# samples, while the frequency is visibly drifting.
STUCK_SENSOR_TEMP_RANGE_C = 0.5
STUCK_SENSOR_MIN_FREQ_SPAN = 0.1  # ppm

# Auto-recalibration safeguards
MIN_RECAL_INTERVAL = 86400      # 24 hours between recalibrations
MIN_IMPROVEMENT = 0.3           # New fit must reduce residual slope by 30%

# chronyd rejects a compensation beyond ±10 ppm (MAX_COMP in its tempcomp.c)
# and keeps applying the last value it accepted.
CHRONY_MAX_COMP = 10.0          # ppm
# A calibration must stay within that limit over all but this share of
# samples at each end of the operating range; in the rare excursions past it
# chronyd holds the compensation at about the limit.
COMP_LIMIT_TAIL = 0.005

# Version of tempcomp.csv's frequency column. Format 1 recorded rows taken while
# tempcomp was active with its sign reversed; see _repair_reversed_sign. Format
# 2 files may hold that repair's first version, which over-corrected rows
# chronyd never compensated; see _undo_uncompensated_repair.
CSV_FORMAT = 3
# _repair_reversed_sign's log entry; only the corrected repair ends with the end.
REPAIR_LOG_MARKER = "rows recorded with tempcomp's sign reversed"
REPAIR_LOG_END = "of unknown calibration or compensation"
DEFAULT_SENSOR_INTERVAL = 30    # s, the interval _apply_calibration writes

# Post-correction check, on what chronyd actually reports rather than the raw
# frequency reconstructed from it (which assumes the compensation is in effect
# as written, and so can't catch it not being). With a working calibration the
# reported frequency no longer follows temperature, and wanders less than the
# raw frequency does.
RESIDUAL_WINDOW_S = 86400
RESIDUAL_MIN_SAMPLES = 360      # 6 h of minute averages
RESIDUAL_MIN_TEMP_RANGE = 2.0   # °C
RESIDUAL_WARN_R2 = 0.5
RESIDUAL_WARN_SHARE = 0.5       # of the calibration's own slope
RESIDUAL_WARN_MIN_SLOPE = 0.02  # ppm/°C

# No sample accepted for this long while the monitor runs: collection stalled
STALL_WARN_S = 6 * 3600
# Minutes are kept only once chronyd's frequency estimate can be trusted again
# after it lost its reference or restarted (it reloads the drift file and
# re-converges; the first minute after the reset read -0.83 against -1.21 ppm).
SETTLE_S = 600
CONF_PATHS = ["/etc/chrony/chrony.conf", "/etc/chrony.conf"]

# Learning phase. Which sensor tracks the crystal depends on the board (on a
# dual-socket Z840 one package sensor explained the drift and the other, of
# the same type, didn't), so the type ranking only picks where to start. Until
# a sensor is chosen, every candidate is logged beside the raw frequency each
# minute; once there is enough evidence the one, and the smoothing, that best
# predicts held-out data is chosen and kept until a data reset.
LEARN_TAUS_MIN = (2, 5, 10, 20, 40, 80)  # smoothing to try; 2 min is the floor
LEARN_MIN_S = 48 * 3600         # learn for at least this long
LEARN_MIN_MINUTES = 1440        # and on at least this many trusted minutes
LEARN_WINDOW_S = 7 * 86400      # keep learning on the last week until decided
LEARN_TRAIN_SHARE = 0.6         # fit on the older share, score on the rest
LEARN_MIN_TEST_R2 = 0.5         # the winner must explain this much, held out
LEARN_MIN_TEST_RANGE = 2.0      # °C, over the held-out minutes
LEARN_KEEP_MARGIN = 0.03        # keep the sensor in use unless beaten by this
# A single core's sensor follows what is scheduled on that core; the package
# (or zone) reading averages the die, so it is taken over a core this close.
LEARN_PACKAGE_MARGIN = 0.05
LEARN_SCORE_S = 3600            # how often to score
LEARN_SCORE_FROM_S = 12 * 3600  # interim scores for the display from here
LEARN_GAP_S = 600               # restart the smoothing across a gap this long
LEARN_MIN_COVERAGE = 0.95       # a sensor must have read in this share of minutes


@dataclass
class TempCompConfig:
    """Parsed tempcomp directive from chrony.conf."""
    sensor_path: str
    interval: int
    T0: float       # millidegrees
    k0: float
    k1: float
    k2: float
    is_active: bool  # True if uncommented
    conf_path: str = ""  # which file it was found in


@dataclass
class TempCompStatus:
    """Current tempcomp calibration status for display."""
    current_temp_c: Optional[float] = None
    config: Optional[TempCompConfig] = None
    sample_count: int = 0
    collection_duration_s: int = 0
    temp_range: Optional[tuple] = None      # (min_C, max_C)
    correlation: Optional[float] = None
    is_extrapolating: bool = False
    cal_range: Optional[tuple] = None       # (min_C, max_C) from calibration
    last_recal: Optional[str] = None        # human-readable last recalibration
    warning: Optional[str] = None           # e.g. sensor appears stuck
    # Post-correction metrics over the last day under the active calibration:
    # reported frequency's slope against temperature and its R², and how much
    # less it wanders than the raw frequency (1 - std(reported)/std(raw)).
    residual_slope: Optional[float] = None  # ppm/°C
    residual_r2: Optional[float] = None
    wander_reduction: Optional[float] = None
    # Learning phase: how long so far, and the best sensor yet (label, tau
    # minutes, held-out R²); or the choice it made
    learning_s: Optional[int] = None
    learning_best: Optional[tuple] = None
    sensor_choice: Optional[tuple] = None   # (label, tau minutes)


def read_temperature(sensor_path: str) -> Optional[float]:
    """Read temperature from sysfs sensor. Returns millidegrees or None."""
    try:
        with open(sensor_path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


THERMAL_BASE = "/sys/class/thermal"
HWMON_BASE = "/sys/class/hwmon"
DEFAULT_SENSOR = "/sys/class/thermal/thermal_zone0/temp"

# The sensor the learning phase chose, by sensor_identity(); root-owned, written
# by apply-tempcomp.sh --sensor, and honored by the boot-time resolver ahead of
# the type ranking.
SENSOR_PIN_PATH = "/etc/chrony-monitor/tempcomp-sensor"

# Stable, reboot-proof alias for the tempcomp sensor. Thermal-zone indices
# renumber across kernel updates, so a raw thermal_zoneN path baked into
# chrony.conf (or written by _apply_calibration) silently starts pointing at
# the wrong sensor. A boot-time service (chrony-tempcomp-sensor.service) runs
# detect_temp_sensor() and repoints this symlink at the right zone every boot;
# chrony.conf and the calibration writer reference the symlink instead of a
# zone number. Absent (e.g. service not installed), callers fall back to the
# concrete path.
STABLE_SENSOR_LINK = "/run/chrony-monitor/tempcomp-sensor"

# What chronyd reads. Sensors such as x86_pkg_temp step by a whole degree and
# jump between neighbouring degrees from one second to the next; chronyd takes a
# single reading per interval, so the compensation it applies jumps by k1 per
# degree every interval, and the clock wanders with it. The monitor reads the
# sensor every second and publishes a smoothed value at SMOOTHED_TEMP_PATH (in a
# directory the boot service creates for the monitor user); a root timer
# (update-tempcomp-input.sh) points this link at it while it is fresh, and back
# at STABLE_SENSOR_LINK when the monitor stops publishing. Both are in the same
# millidegrees, so one calibration serves either.
TEMPCOMP_INPUT_LINK = "/run/chrony-monitor/tempcomp-input"
SMOOTHED_TEMP_PATH = "/run/chrony-monitor/smoothed/temp"
SMOOTH_TAU_S = 120              # exponential smoothing time constant
SMOOTH_PUBLISH_S = 5            # how often the smoothed value is written
SMOOTH_GAP_S = 60               # restart the smoothing after a gap this long

# The compensation evaluated each second at what chronyd reads, its standard
# deviation within each minute: real temperature change moves it by a few
# thousandths of a ppm a minute, so more is sensor noise chronyd steers by.
COMP_NOISE_WARN_PPM = 0.05
COMP_NOISE_MINUTES = 60

# Suitability ranking for crystal tempcomp; lower number = better. We want a
# sensor that tracks board/ambient temperature (what the clock crystal sees),
# is not stuck at a constant, and is not dominated by CPU load.
_SENSOR_TYPE_PRIORITY = [
    (re.compile(r'pch', re.I),             10),  # Intel PCH (chipset) — best proxy for board temp
    (re.compile(r'cpu[-_]?thermal', re.I), 20),  # Raspberry Pi / ARM SoC — the right sensor there
    (re.compile(r'bcm2835', re.I),         20),  # Raspberry Pi thermal
    (re.compile(r'soc[-_]?thermal', re.I), 25),
    (re.compile(r'x86_pkg_temp', re.I),    40),  # CPU package — noisy (load), fallback only
    (re.compile(r'coretemp', re.I),        45),
    # acpitz is frequently stuck at a constant on Intel, but it is at least a
    # board-level sensor: still preferred over an unrecognized zone, which is as
    # likely to be wifi/NVMe/battery as anything tracking the crystal.
    (re.compile(r'acpitz', re.I),          90),
    # Not board/ambient proxies at all — ranked below even an unknown zone.
    # INT3400 is the ACPI DPTF virtual zone, which typically reports a constant
    # (often exactly 20.0C, so a plausibility check can't catch it).
    (re.compile(r'int3400|iwlwifi|nvme|battery|charger', re.I), 120),
]

# A readable zone outside this band isn't reporting board temperature: 0 shows up
# on unpopulated zones, and negatives on ones that report an error in-band.
SENSOR_PLAUSIBLE_RANGE_C = (5.0, 110.0)

# Priorities at or below this are real board/SoC sensors; above it we're into
# acpitz and unrecognized zones, which we accept only because nothing better
# exists. The boot-time resolver waits a moment for one of these to appear:
# intel_pch_thermal is a module loaded by udev, so at the point the resolver
# runs the PCH zone may not exist yet even though it will seconds later.
# (On a Pi the SoC sensor is built in and present immediately.)
PREFERRED_PRIORITY = 45
UNKNOWN_ZONE_PRIORITY = 100


def _read_zone_type(zone_dir: str) -> str:
    """Read a thermal zone's `type` string (e.g. 'pch_cometlake'), or ''."""
    try:
        with open(os.path.join(zone_dir, "type")) as f:
            return f.read().strip()
    except OSError:
        return ""


_ZONE_NODE_RE = r'/thermal_zone\d+)/(?:temp|hwmon\d+/temp\d+_input)$'


def _zone_dir_of(path: str, thermal_base: str = THERMAL_BASE) -> Optional[str]:
    """The thermal-zone directory a sensor node belongs to, or None.

    Accepts both node forms a zone exposes: its own `temp`, and the hwmon
    mirror `hwmonM/tempK_input` that zone_sensor_path() prefers.
    """
    if not path:
        return None
    m = re.match(r'^(' + re.escape(thermal_base) + _ZONE_NODE_RE, path)
    return m.group(1) if m else None


def _zone_type_of(path: str, thermal_base: str = THERMAL_BASE) -> str:
    zone_dir = _zone_dir_of(path, thermal_base)
    return _read_zone_type(zone_dir) if zone_dir else ""


def zone_sensor_path(zone_dir: str) -> str:
    """The node chronyd should read for a thermal zone.

    Prefer the zone's hwmon mirror (`hwmonM/temp1_input`) over its `temp` node.
    Both report the same millidegrees, but the stock chronyd AppArmor profile on
    Debian/Ubuntu only allows the hwmon form (`.../thermal_zone*/hwmon*/temp*_input`)
    and denies `thermal_zone*/temp` outright. A denied sensor fails silently:
    chronyd logs "Could not read temperature" every interval and applies no
    compensation at all. Zones with no hwmon mirror (x86_pkg_temp, some SoCs)
    fall back to `temp`, which setup-chronyd-apparmor.sh makes readable.
    """
    for cand in sorted(glob.glob(os.path.join(zone_dir, "hwmon*", "temp1_input"))):
        if read_temperature(cand) is not None:
            return cand
    return os.path.join(zone_dir, "temp")


def rank_temp_sensors(thermal_base: str = THERMAL_BASE) -> list:
    """Rank readable thermal zones for tempcomp use.

    Returns [(priority, path), ...] best first. Zones reporting an implausible
    temperature are omitted entirely; the rest are scored by `type`, so the
    chipset (PCH) wins on Intel and the SoC sensor on ARM/Pi, while acpitz
    (frequently stuck at a constant), CPU-package sensors (noisy, driven by
    load), and non-board zones (wifi, NVMe, the INT3400 virtual zone) sink.
    """
    ranked = []
    try:
        zones = sorted(d for d in os.listdir(thermal_base)
                       if d.startswith("thermal_zone"))
    except OSError:
        return ranked
    lo, hi = SENSOR_PLAUSIBLE_RANGE_C
    for z in zones:
        zone_dir = os.path.join(thermal_base, z)
        millideg = read_temperature(os.path.join(zone_dir, "temp"))
        if millideg is None or not (lo <= millideg / 1000.0 <= hi):
            continue
        path = zone_sensor_path(zone_dir)
        ztype = _read_zone_type(zone_dir)
        prio = UNKNOWN_ZONE_PRIORITY  # readable but unrecognized: usable, last
        for rx, p in _SENSOR_TYPE_PRIORITY:
            if rx.search(ztype):
                prio = p
                break
        ranked.append((prio, path))
    ranked.sort(key=lambda pp: pp[0])
    return ranked


def detect_temp_sensor(default: str = DEFAULT_SENSOR) -> str:
    """Pick the best tempcomp sensor for this machine by thermal-zone type.

    Falls back to `default` if nothing usable is found. An explicit sensor path
    always takes precedence over this. See rank_temp_sensors() for the ranking.
    """
    ranked = rank_temp_sensors()
    return ranked[0][1] if ranked else default


def preferred_sensor_path() -> str:
    """The sensor the monitor should read: the stable alias when it's usable.

    Reading through STABLE_SENSOR_LINK keeps the monitor, chrony.conf and the
    persisted sensor identity all naming the same thing by construction rather
    than by coincidence -- sensor_identity() resolves the link to its zone type,
    so the identity is unchanged either way. Falls back to detecting a concrete
    zone when the boot service isn't installed or its link is unusable.
    """
    if read_temperature(STABLE_SENSOR_LINK) is not None:
        return STABLE_SENSOR_LINK
    return detect_temp_sensor()


def sensor_identity(sensor_path: str, thermal_base: str = THERMAL_BASE) -> str:
    """Return a reboot-stable identity for a sensor.

    Thermal-zone indices renumber across kernel updates, so the *path* is not a
    reliable key for "is this the same physical sensor". When the path (or, for
    the stable STABLE_SENSOR_LINK alias, its symlink target) is a thermal-zone
    node -- `thermal_zoneN/temp` or its hwmon mirror `thermal_zoneN/hwmonM/tempK_input`,
    which are the same sensor -- key on the zone's `type` (e.g. 'pch_cometlake')
    instead, which follows the hardware. Otherwise fall back to the path.

    Only the alias's own link is resolved — not os.path.realpath, which would
    also chase the /sys/class/thermal/thermal_zoneN class symlink down into
    /sys/devices and defeat the zone-number match.
    """
    # The smoothed value, and the link that serves it or the raw sensor, are
    # the stable sensor's temperature
    if sensor_path in (TEMPCOMP_INPUT_LINK, SMOOTHED_TEMP_PATH):
        sensor_path = STABLE_SENSOR_LINK
    candidates = []
    if sensor_path:
        candidates.append(sensor_path)
        try:
            if os.path.islink(sensor_path):
                tgt = os.readlink(sensor_path)
                if not os.path.isabs(tgt):
                    tgt = os.path.normpath(
                        os.path.join(os.path.dirname(sensor_path), tgt))
                candidates.append(tgt)
        except OSError:
            pass
    for cand in candidates:
        zone_dir = _zone_dir_of(cand, thermal_base)
        if zone_dir:
            ident = _zone_identity(zone_dir)
            if ident:
                return ident
    for cand in candidates:
        ident = _hwmon_identity(cand)
        if ident:
            return ident
    return "path:" + (sensor_path or "")


def _zone_number(name: str) -> int:
    m = re.search(r'(\d+)$', name)
    return int(m.group(1)) if m else -1


def _zone_identity(zone_dir: str) -> str:
    """'type:<zone type>', with '#k' for the k-th later zone of the same type.

    The first zone of a type keeps the bare type, as identities always were;
    a second one (each socket of a dual-socket machine has an x86_pkg_temp)
    must not pass for it.
    """
    ztype = _read_zone_type(zone_dir)
    if not ztype:
        return ""
    base = os.path.dirname(zone_dir)
    try:
        same = sorted((z for z in os.listdir(base) if z.startswith("thermal_zone")
                       and _read_zone_type(os.path.join(base, z)) == ztype),
                      key=_zone_number)
    except OSError:
        same = []
    k = same.index(os.path.basename(zone_dir)) if os.path.basename(zone_dir) in same else 0
    return f"type:{ztype}#{k}" if k else "type:" + ztype


def _read_text(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _hwmon_identity(path: str) -> str:
    """'hwmon:<device>:<label>' for an hwmon temperature node, or ''.

    hwmonN renumbers across boots, so key on the device it belongs to (e.g.
    coretemp.1, one per socket) and the channel's label (e.g. 'Core 3').
    """
    m = re.search(r'/temp(\d+)_input$', path or "")
    if not m:
        return ""
    hdir = os.path.dirname(path)
    name = _read_text(os.path.join(hdir, "name"))
    if not name:
        return ""
    dev = os.path.join(hdir, "device")
    dev = os.path.basename(os.path.realpath(dev)) if os.path.exists(dev) else name
    label = (_read_text(os.path.join(hdir, f"temp{m.group(1)}_label"))
             or f"temp{m.group(1)}")
    # Only characters apply-tempcomp.sh --sensor accepts in a pinned name
    return re.sub(r'[^A-Za-z0-9 _.:#()+-]', '_', f"hwmon:{dev}:{label}")[:102]


def list_temp_sensors(thermal_base: str = THERMAL_BASE,
                      hwmon_base: str = HWMON_BASE) -> dict:
    """{identity: path} of every temperature sensor reading plausibly.

    Thermal zones, by the node zone_sensor_path() picks, and hwmon channels
    that aren't a zone's mirror (coretemp's per-core and package channels, for
    instance). The learning phase logs all of them.
    """
    lo, hi = SENSOR_PLAUSIBLE_RANGE_C
    found = {}

    def add(ident, path):
        t = read_temperature(path)
        if ident and ident not in found and t is not None and lo <= t / 1000.0 <= hi:
            found[ident] = path

    try:
        zones = sorted((z for z in os.listdir(thermal_base)
                        if z.startswith("thermal_zone")), key=_zone_number)
    except OSError:
        zones = []
    for z in zones:
        zone_dir = os.path.join(thermal_base, z)
        add(_zone_identity(zone_dir), zone_sensor_path(zone_dir))
    try:
        hwmons = sorted((h for h in os.listdir(hwmon_base) if h.startswith("hwmon")),
                        key=_zone_number)
    except OSError:
        hwmons = []
    for h in hwmons:
        hdir = os.path.join(hwmon_base, h)
        if "/thermal_zone" in os.path.realpath(hdir):
            continue                    # a zone's mirror, already listed
        for node in sorted(glob.glob(os.path.join(hdir, "temp*_input")),
                           key=lambda p: _zone_number(p[:-len("_input")])):
            add(_hwmon_identity(node), node)
    return found


def sensor_label(identity: str) -> str:
    """A short display name for a sensor identity."""
    return identity.split(":", 1)[1] if identity.startswith(("type:", "hwmon:")) else identity


def read_sensor_pin(pin_path: str = SENSOR_PIN_PATH) -> str:
    """The sensor identity the learning phase chose, or ''."""
    return _read_text(pin_path)


SENSOR_READ_ERROR = "Could not read temperature"


def chrony_sensor_read_failing(since: str = "-3min") -> Optional[bool]:
    """True if chronyd recently failed to read its tempcomp sensor; None if unknown.

    chronyd reports a sensor it cannot open only to the journal -- an AppArmor
    denial, a renumbered zone, or a removed hwmon all look the same -- and
    silently applies no compensation. Watching the journal is the only way the
    monitor can tell that the directive it wrote is not actually in effect.
    """
    try:
        proc = subprocess.run(
            ["journalctl", "-q", "--no-pager", "-o", "cat", "--since", since,
             "-u", "chrony", "-u", "chronyd"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return SENSOR_READ_ERROR in proc.stdout


def chronyd_age() -> Optional[float]:
    """Seconds since the youngest chronyd process started; None if not running."""
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        tick = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None
    ages = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        # comm is parenthesised and may hold spaces; fields resume after it
        name, rest = stat[stat.find("(") + 1:stat.rfind(")")], stat[stat.rfind(")") + 2:]
        if name == "chronyd":
            try:
                ages.append(uptime - int(rest.split()[19]) / tick)
            except (ValueError, IndexError):
                continue
    return min(ages) if ages else None


def _journal_unix_time(line: str) -> Optional[float]:
    try:
        return float(line.split(None, 1)[0])
    except (ValueError, IndexError):
        return None


def chrony_sensor_read_failures(since_ts: float) -> Optional[tuple]:
    """(journal_start, failure_times) for chronyd since since_ts; None if unknown.

    failure_times are when chronyd logged it couldn't read its tempcomp sensor,
    sorted. journal_start is chronyd's oldest journal entry: before it, whether
    chronyd was compensating can't be told.
    """
    units = ["-u", "chrony", "-u", "chronyd"]
    try:
        # Only the oldest entry is wanted; stop reading after it.
        with subprocess.Popen(
                ["journalctl", "-q", "--no-pager", "-o", "short-unix"] + units,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True) as head:
            first = head.stdout.readline()
            head.kill()
        proc = subprocess.run(
            ["journalctl", "-q", "--no-pager", "-o", "short-unix",
             "--since", f"@{int(since_ts)}"] + units,
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    start = _journal_unix_time(first)
    if proc.returncode != 0 or start is None:
        return None
    failures = sorted(t for t in (_journal_unix_time(line)
                                  for line in proc.stdout.splitlines()
                                  if SENSOR_READ_ERROR in line)
                      if t is not None)
    return start, failures


def _chronyd_compensating(journal: Optional[tuple], ts: float,
                          interval: float) -> Optional[bool]:
    """Whether chronyd could read its sensor for the row ending at ts; None if
    the journal (from chrony_sensor_read_failures) doesn't say."""
    if journal is None:
        return None
    start, failures = journal
    # A row averages the minute before ts; chronyd logs each failed read, once
    # per sensor interval.
    lo, hi = ts - 60 - 2 * interval, ts + 2 * interval
    if lo < start:
        return None
    i = bisect.bisect_left(failures, lo)
    return not (i < len(failures) and failures[i] <= hi)


def _clamped_compensation(config: "TempCompConfig", temp_millideg: float) -> float:
    # Past its limit chronyd keeps the last value it accepted, about the limit
    comp = _compute_compensation(config, temp_millideg)
    return max(-CHRONY_MAX_COMP, min(CHRONY_MAX_COMP, comp))


def parse_chrony_tempcomp(conf_paths=None) -> Optional[TempCompConfig]:
    """Parse tempcomp directive from chrony.conf.

    Returns TempCompConfig if found, None if no tempcomp directive exists.
    """
    if conf_paths is None:
        conf_paths = CONF_PATHS

    for conf_path in conf_paths:
        try:
            with open(conf_path) as f:
                for line in f:
                    stripped = line.strip()
                    is_commented = stripped.startswith('#')
                    if is_commented:
                        stripped = stripped.lstrip('#').strip()

                    match = re.match(
                        r'tempcomp\s+(\S+)\s+(\d+)\s+'
                        r'([+-]?\d+\.?\d*)\s+'
                        r'([+-]?\d+\.?\d*)\s+'
                        r'([+-]?\d+\.?\d*)\s+'
                        r'([+-]?\d+\.?\d*)',
                        stripped
                    )
                    if match:
                        return TempCompConfig(
                            sensor_path=match.group(1),
                            interval=int(match.group(2)),
                            T0=float(match.group(3)),
                            k0=float(match.group(4)),
                            k1=float(match.group(5)),
                            k2=float(match.group(6)),
                            is_active=not is_commented,
                            conf_path=conf_path,
                        )
        except OSError:
            continue

    return None


def _compute_compensation(config: TempCompConfig, temp_millideg: float) -> float:
    """Compute the tempcomp correction in ppm for a given temperature."""
    dT = temp_millideg - config.T0
    return config.k0 + config.k1 * dT + config.k2 * dT * dT


def _pearson_r(x: list, y: list) -> float:
    """Compute Pearson correlation coefficient."""
    n = len(x)
    if n < 2:
        return 0.0
    sx = sum(x)
    sy = sum(y)
    sxy = sum(a * b for a, b in zip(x, y))
    sx2 = sum(a * a for a in x)
    sy2 = sum(b * b for b in y)
    denom = math.sqrt((n * sx2 - sx * sx) * (n * sy2 - sy * sy))
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


def _polyfit_quadratic(x: list, y: list) -> tuple:
    """Fit y = k0 + k1*(x-T0) + k2*(x-T0)^2 using normal equations.

    Returns (T0, k0, k1, k2) where T0 is the mean of x.
    """
    n = len(x)
    T0 = sum(x) / n
    dx = [xi - T0 for xi in x]

    powers = [0.0] * 5
    m = [0.0] * 3
    for i in range(n):
        d = dx[i]
        d2 = d * d
        powers[0] += 1
        powers[1] += d
        powers[2] += d2
        powers[3] += d * d2
        powers[4] += d2 * d2
        m[0] += y[i]
        m[1] += d * y[i]
        m[2] += d2 * y[i]

    aug = [
        [powers[0], powers[1], powers[2], m[0]],
        [powers[1], powers[2], powers[3], m[1]],
        [powers[2], powers[3], powers[4], m[2]],
    ]

    for col in range(3):
        max_row = col
        for row in range(col + 1, 3):
            if abs(aug[row][col]) > abs(aug[max_row][col]):
                max_row = row
        aug[col], aug[max_row] = aug[max_row], aug[col]

        pivot = aug[col][col]
        if abs(pivot) < 1e-15:
            return T0, 0.0, 0.0, 0.0

        for row in range(col + 1, 3):
            factor = aug[row][col] / pivot
            for j in range(col, 4):
                aug[row][j] -= factor * aug[col][j]

    coeffs = [0.0] * 3
    for row in range(2, -1, -1):
        coeffs[row] = aug[row][3]
        for col in range(row + 1, 3):
            coeffs[row] -= aug[row][col] * coeffs[col]
        coeffs[row] /= aug[row][row]

    return T0, coeffs[0], coeffs[1], coeffs[2]


def _residual_slope(temps: list, freqs: list) -> float:
    """Compute the linear slope of freq vs temp (ppm per °C).

    A perfect compensation would give slope ≈ 0.
    """
    n = len(temps)
    if n < 2:
        return 0.0
    sx = sum(temps)
    sy = sum(freqs)
    sxy = sum(a * b for a, b in zip(temps, freqs))
    sx2 = sum(a * a for a in temps)
    denom = n * sx2 - sx * sx
    if abs(denom) < 1e-15:
        return 0.0
    return (n * sxy - sx * sy) / denom


def _center_compensation(temps: list, T0: float, k0: float, k1: float,
                         k2: float) -> tuple:
    """Shift k0 so the compensation is centered on the operating range.

    The fitted k0 carries the crystal's absolute frequency offset, which
    chronyd's own frequency estimate absorbs anyway; left in, it spends
    chronyd's ±10 ppm budget on a constant. Returns (k0, half_span): the
    shifted k0, and the largest |compensation| it gives over all but the
    outer COMP_LIMIT_TAIL of samples at each end.
    """
    comps = sorted(k0 + k1 * (t - T0) + k2 * (t - T0) ** 2 for t in temps)
    tail = int(len(comps) * COMP_LIMIT_TAIL)
    lo, hi = comps[tail], comps[-1 - tail]
    return k0 - (lo + hi) / 2, (hi - lo) / 2


def _filter_outliers(temps: list, freqs: list) -> tuple:
    """Remove frequency outliers using IQR method.

    Returns filtered (temps, freqs) lists with outliers removed.
    """
    if len(freqs) < 20:
        return temps, freqs
    freqs_sorted = sorted(freqs)
    q1 = freqs_sorted[len(freqs_sorted) // 4]
    q3 = freqs_sorted[3 * len(freqs_sorted) // 4]
    iqr = q3 - q1
    lower = q1 - 3.0 * iqr
    upper = q3 + 3.0 * iqr
    filtered = [(t, f) for t, f in zip(temps, freqs) if lower <= f <= upper]
    if not filtered:
        return temps, freqs
    return [t for t, f in filtered], [f for t, f in filtered]


class SensorSmoother:
    """Exponentially smoothed temperature, published for chronyd to read.

    Until tau has passed since a (re)start the value is the plain mean of the
    readings so far, so a single quantized first reading doesn't linger.
    """

    def __init__(self, path: str = SMOOTHED_TEMP_PATH, tau: float = SMOOTH_TAU_S):
        self.path = path
        self.tau = tau
        self.value: Optional[float] = None
        self._n = 0
        self._last = 0.0
        self._published = 0.0       # when the last write succeeded

    def update(self, temp_millideg: float, now: float = None) -> float:
        now = time.time() if now is None else now
        if self.value is None or now - self._last > SMOOTH_GAP_S:
            self.value, self._n = temp_millideg, 1
        else:
            self._n += 1
            a = max(1.0 / self._n, 1.0 - math.exp(-(now - self._last) / self.tau))
            self.value += a * (temp_millideg - self.value)
        self._last = now
        if now - self._published >= SMOOTH_PUBLISH_S:
            self._publish(now)
        return self.value

    def _publish(self, now: float):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(f"{self.value:.0f}\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
            self._published = now
        except OSError:
            self._published = 0.0

    def publishing(self, now: float = None) -> bool:
        """True while chronyd can be reading the smoothed value."""
        now = time.time() if now is None else now
        return self._published > 0 and now - self._published < 3 * SMOOTH_PUBLISH_S

    def resume(self, now: float = None):
        """Carry on from the value published before a monitor restart, so a
        long smoothing doesn't start over from one raw reading."""
        now = time.time() if now is None else now
        try:
            mtime = os.stat(self.path).st_mtime
            with open(self.path) as f:
                value = float(f.read().strip())
        except (OSError, ValueError):
            return
        if now - mtime <= SMOOTH_GAP_S:
            self.value, self._n, self._last = value, 1 << 30, mtime


def _ema(ts: list, xs: list, tau_min: float, gap_s: float = LEARN_GAP_S) -> list:
    """xs smoothed by an exponential filter, restarted across gaps."""
    tau = tau_min * 60.0
    out, s, last = [], None, None
    for t, x in zip(ts, xs):
        if s is None or t - last > gap_s:
            s = x
        else:
            s += (1.0 - math.exp(-(t - last) / tau)) * (x - s)
        out.append(s)
        last = t
    return out


def _r2(model: tuple, xs: list, ys: list) -> float:
    """Share of ys's variance a quadratic (T0, k0, k1, k2) explains."""
    T0, k0, k1, k2 = model
    my = sum(ys) / len(ys)
    ss_res = sum((y - (k0 + k1 * (x - T0) + k2 * (x - T0) ** 2)) ** 2
                 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("-inf")


def score_sensors(rows: list, sensors: list) -> list:
    """Rank sensors by how well each predicts the raw frequency.

    rows are (ts, raw freq, [millideg per sensor, None where unread]). For
    each sensor and smoothing in LEARN_TAUS_MIN, fits frequency = quadratic
    (temperature) on the older LEARN_TRAIN_SHARE of the minutes and scores it
    on the rest: a sensor that only fits by chance, or that the crystal stopped
    following, can't win that. Returns [(test R², identity, tau min, held-out
    temperature range °C, full range °C)], best first.
    """
    results = []
    for i, ident in enumerate(sensors):
        pts = [(r[0], r[1], r[2][i]) for r in rows
               if i < len(r[2]) and r[2][i] is not None]
        if len(pts) < LEARN_MIN_COVERAGE * len(rows) or len(pts) < 60:
            continue
        ts = [p[0] for p in pts]
        freqs = [p[1] for p in pts]
        n_train = int(len(pts) * LEARN_TRAIN_SHARE)
        best = None
        for tau in LEARN_TAUS_MIN:
            x = _ema(ts, [p[2] for p in pts], tau)
            model = _polyfit_quadratic(x[:n_train], freqs[:n_train])
            r2 = _r2(model, x[n_train:], freqs[n_train:])
            if best is None or r2 > best[0]:
                test = x[n_train:]
                best = (r2, ident, tau, (max(test) - min(test)) / 1000.0,
                        (max(x) - min(x)) / 1000.0)
        results.append(best)
    results.sort(key=lambda r: -r[0])
    return results


def _per_core(identity: str) -> bool:
    """A single CPU core's sensor, such as coretemp's 'Core 3'."""
    return bool(re.search(r':Core \d+$', identity))


class SensorLearner:
    """Logs every candidate sensor beside the raw frequency until one is chosen.

    learning.csv holds one row a minute, `ts,freq,<millideg per sensor>`, under
    a header naming the sensors; sensor_choice holds the decision,
    `<identity>\\t<tau minutes>\\t<test R²>\\t<when>`, once made.
    """

    def __init__(self, data_dir: str, thermal_base: str = THERMAL_BASE,
                 hwmon_base: str = HWMON_BASE):
        self._path = os.path.join(data_dir, "learning.csv")
        self._choice_path = os.path.join(data_dir, "sensor_choice")
        self._data_dir = data_dir
        self._thermal_base = thermal_base
        self._hwmon_base = hwmon_base
        self.sensors: list = []         # identities, the CSV's column order
        self._paths: list = []
        self.rows = deque()             # (ts, raw freq, [millideg or None])
        self._sums: list = []
        self._counts: list = []
        self.choice: Optional[tuple] = None     # (identity, tau min, test R²)
        self.ranking: list = []
        self._scored_time: float = 0
        self._scoring: Optional[threading.Thread] = None
        self._pruned_time: float = 0

    @property
    def learning(self) -> bool:
        return self.choice is None

    def load(self):
        """Read the decision, or the minutes logged so far."""
        parts = _read_text(self._choice_path).split("\t")
        if len(parts) >= 3:
            try:
                self.choice = (parts[0], float(parts[1]), float(parts[2]))
                return
            except ValueError:
                pass
        current = list_temp_sensors(self._thermal_base, self._hwmon_base)
        self.sensors = list(current)
        self._paths = list(current.values())
        since = time.time() - LEARN_WINDOW_S
        header = None
        try:
            with open(self._path) as f:
                for line in f:
                    parts = line.rstrip("\n").split(",")
                    if parts[0] == "ts":
                        header = parts[2:]
                        continue
                    if header is None or len(parts) != len(header) + 2:
                        continue
                    try:
                        ts, freq = int(parts[0]), float(parts[1])
                        vals = {h: float(v) for h, v in zip(header, parts[2:]) if v}
                    except ValueError:
                        continue
                    if ts >= since:
                        self.rows.append((ts, freq, [vals.get(s.replace(",", ";"))
                                                     for s in self.sensors]))
        except OSError:
            pass
        # The file's columns are rewritten to the sensors found now
        if header != [s.replace(",", ";") for s in self.sensors]:
            self._write()
        self._start_minute()

    def _start_minute(self):
        self._sums = [0.0] * len(self.sensors)
        self._counts = [0] * len(self.sensors)

    def sample(self):
        """Read every candidate; called each second while learning."""
        for i, path in enumerate(self._paths):
            t = read_temperature(path)
            if t is not None:
                self._sums[i] += t
                self._counts[i] += 1

    def close_minute(self, ts: int, raw_freq: float, keep: bool):
        """End the minute; keep it only if chronyd's frequency was trusted."""
        temps = [s / n if n else None for s, n in zip(self._sums, self._counts)]
        self._start_minute()
        if not keep or not self.sensors:
            return
        self.rows.append((ts, raw_freq, temps))
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            new = not os.path.exists(self._path)
            with open(self._path, "a") as f:
                if new:
                    f.write(self._header())
                f.write(self._line(ts, raw_freq, temps))
        except OSError:
            pass
        if time.time() - self._pruned_time > 86400:
            self._pruned_time = time.time()
            since = ts - LEARN_WINDOW_S
            while self.rows and self.rows[0][0] < since:
                self.rows.popleft()
            self._write()

    def _header(self) -> str:
        return ",".join(["ts", "freq"] + [s.replace(",", ";") for s in self.sensors]) + "\n"

    @staticmethod
    def _line(ts, freq, temps) -> str:
        return ",".join([str(ts), f"{freq:.6f}"]
                        + ["" if t is None else f"{t:.0f}" for t in temps]) + "\n"

    def _write(self):
        tmp = self._path + ".tmp"
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(tmp, "w") as f:
                f.write(self._header())
                for ts, freq, temps in self.rows:
                    f.write(self._line(ts, freq, temps))
            os.replace(tmp, self._path)
        except OSError:
            pass

    def span_s(self) -> int:
        return int(self.rows[-1][0] - self.rows[0][0]) if len(self.rows) >= 2 else 0

    def due(self, now: float) -> bool:
        return (self.learning and self.span_s() >= LEARN_SCORE_FROM_S
                and now - self._scored_time >= LEARN_SCORE_S
                and not (self._scoring and self._scoring.is_alive()))

    def score(self, now: float, background: bool = True):
        """Rank the sensors on the minutes so far, into self.ranking.

        Scoring a week of a few dozen sensors takes seconds, so it runs in a
        thread rather than stall the display.
        """
        self._scored_time = now
        rows, sensors = list(self.rows), list(self.sensors)

        def job():
            self.ranking = score_sensors(rows, sensors)
        if background:
            self._scoring = threading.Thread(target=job, daemon=True)
            self._scoring.start()
        else:
            job()

    def preferred(self, incumbent: str) -> Optional[tuple]:
        """The ranking's entry to choose, by score and then by kind.

        The sensor in use is kept unless another beats it by LEARN_KEEP_MARGIN:
        switching costs the fit its data's continuity for no real gain. Failing
        that, a package or zone sensor within LEARN_PACKAGE_MARGIN of the best
        is taken over a single core's.
        """
        if not self.ranking:
            return None
        top = self.ranking[0]
        for r in self.ranking:
            if r[1] == incumbent and r[0] >= top[0] - LEARN_KEEP_MARGIN:
                return r
        for r in self.ranking:
            if r[0] < top[0] - LEARN_PACKAGE_MARGIN:
                break
            if not _per_core(r[1]):
                return r
        return top

    def winner(self, incumbent: str) -> Optional[tuple]:
        """(identity, tau min, test R²) once the evidence suffices, else None."""
        if self.span_s() < LEARN_MIN_S or len(self.rows) < LEARN_MIN_MINUTES:
            return None
        best = self.preferred(incumbent)
        if best is None:
            return None
        r2, ident, tau, test_range, full_range = best
        if (r2 < LEARN_MIN_TEST_R2 or test_range < LEARN_MIN_TEST_RANGE
                or full_range < MIN_TEMP_RANGE_FIT):
            return None
        return ident, tau, r2

    def fit_rows(self, ident: str, tau: float) -> list:
        """(ts, smoothed millideg, raw freq) of the chosen sensor's minutes."""
        if ident not in self.sensors:
            return []
        i = self.sensors.index(ident)
        pts = [(r[0], r[2][i], r[1]) for r in self.rows if r[2][i] is not None]
        smoothed = _ema([p[0] for p in pts], [p[1] for p in pts], tau)
        return [(p[0], s, p[2]) for p, s in zip(pts, smoothed)]

    def decide(self, ident: str, tau: float, r2: float):
        self.choice = (ident, tau, r2)
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._choice_path, "w") as f:
                f.write(f"{ident}\t{tau:g}\t{r2:.3f}\t{int(time.time())}\n")
        except OSError:
            pass
        self.rows.clear()
        try:
            os.replace(self._path, self._path + ".decided")
        except OSError:
            pass

    def reset(self):
        """Forget the decision and the minutes: learn again from scratch."""
        self.choice = None
        self.ranking = []
        self.rows.clear()
        self._scored_time = 0
        for p in (self._choice_path, self._path):
            try:
                os.remove(p)
            except OSError:
                pass
        self.load()


class TempCompCollector:
    """Collects temperature/frequency data for tempcomp calibration."""

    def __init__(self, sensor_path: str = None,
                 data_dir: str = None, auto_recal: bool = True):
        # sensor_path=None (the default) means use the stable alias, or detect a
        # concrete zone if it isn't published, and learn which sensor to keep.
        # An explicit path is honored as-is.
        learn = sensor_path is None and auto_recal
        if sensor_path is None:
            sensor_path = preferred_sensor_path()
        self.sensor_path = sensor_path
        self.auto_recal = auto_recal
        if data_dir is None:
            data_dir = os.path.join(
                os.path.expanduser("~"), ".local", "share", "chrony-monitor"
            )
        self._data_dir = data_dir
        self._csv_path = os.path.join(data_dir, "tempcomp.csv")
        self._recal_log_path = os.path.join(data_dir, "recalibrations.log")
        self._sensor_meta_path = os.path.join(data_dir, "sensor")
        self._reported_path = os.path.join(data_dir, "reported.csv")

        self._temps = deque(maxlen=MAX_SAMPLES)
        self._freqs = deque(maxlen=MAX_SAMPLES)
        self._timestamps = deque(maxlen=MAX_SAMPLES)
        # (ts, temp, reported freq, comp chronyd applied) per minute, for the
        # post-correction check; every minute chronyd was trusted
        self._reported = deque(maxlen=RESIDUAL_WINDOW_S // 60)
        self._last_sample_time: float = 0   # last row accepted into the fit data
        self._settle_until: float = 0       # minutes before this are discarded
        self._chronyd_check_time: float = 0

        # Minute bucketing
        self._minute_temps = []
        self._minute_freqs = []
        self._minute_reported = []
        self._minute_comps = []
        self._minute_count = 0
        self._minute_start: float = 0

        self._config: Optional[TempCompConfig] = None
        self._start_time: float = 0
        self._last_recal_time: float = 0
        self._last_attempt_time: float = 0
        self._last_prune_time: float = 0
        self._csv_format_path = os.path.join(data_dir, "csv_format")
        # Present while the active calibration is one fitted with the reversed
        # sign, i.e. known to amplify drift instead of cancelling it
        self._cal_reversed_path = os.path.join(data_dir, "cal_reversed")
        self._cal_range_path = os.path.join(data_dir, "cal_range")
        self._cal_range: Optional[tuple] = None  # (min_C, max_C) from last calibration
        self._was_extrapolating: bool = False

        # Cached correlation and post-correction check
        self._cached_correlation: Optional[float] = None
        self._correlation_time: float = 0
        self._cached_residual: Optional[tuple] = None
        self._residual_time: float = 0

        # Recalibration logs (shown in display)
        self.recal_logs: list = []

        # Whether chronyd itself can read the configured sensor (journal check)
        self._read_failing: Optional[bool] = None
        self._read_failing_time: float = 0

        # Smoothed value for chronyd, only of the sensor the input link falls
        # back to, and only once the boot service has made a place for it
        self.smoother: Optional[SensorSmoother] = None
        self._smoothable = (sensor_identity(self.sensor_path)
                            == sensor_identity(STABLE_SENSOR_LINK))
        # Per-minute std of the compensation at what chronyd reads
        self._minute_chrony_comps = []
        self._comp_noise = deque(maxlen=COMP_NOISE_MINUTES)

        # Learning phase: every candidate sensor logged until one is chosen
        self.learner: Optional[SensorLearner] = SensorLearner(data_dir) if learn else None
        self._smooth_tau = SMOOTH_TAU_S
        self._resume_smoother = True        # only across a restart, same sensor
        self._choice_pending = ""           # why the chosen sensor isn't in use yet

    def read_temperature(self) -> Optional[float]:
        """The temperature to record: the smoothed value while it's published
        (it's what chronyd then compensates with), else the sensor's own."""
        temp = read_temperature(self.sensor_path)
        if (self.smoother is None and self._smoothable
                and os.path.isdir(os.path.dirname(SMOOTHED_TEMP_PATH))):
            self.smoother = SensorSmoother(tau=self._smooth_tau)
            if self._resume_smoother:
                self.smoother.resume()
        if temp is None or self.smoother is None:
            return temp
        smoothed = self.smoother.update(temp)
        return smoothed if self.smoother.publishing() else temp

    def load(self):
        """Load persistent data and parse chrony.conf. Call once at startup."""
        self._start_time = time.time()
        self._config = parse_chrony_tempcomp()
        self._invalidate_data_if_sensor_changed()
        self._load_csv()
        self._load_recal_time()
        self._load_cal_range()
        self._load_reported()
        if self._timestamps:
            self._last_sample_time = self._timestamps[-1]
        if self.learner:
            self.learner.load()
            if self.learner.choice:
                self._smooth_tau = max(SMOOTH_TAU_S, self.learner.choice[1] * 60)

    def _config_matches_sensor(self) -> bool:
        """True if chrony.conf's tempcomp uses the sensor we collect against.

        When it doesn't, its T0/k coefficients live in a different temperature
        scale and cannot be compared with, or reused alongside, our own.
        """
        if not self._config:
            return False
        return sensor_identity(self._config.sensor_path) == \
            sensor_identity(self.sensor_path)

    @staticmethod
    def _archive(path: str, owner: str):
        """Move `path` aside, tagged with the sensor identity that produced it.

        Tagging rather than a plain '.old' suffix means a second sensor change
        doesn't destroy the first station's archive.
        """
        if not os.path.exists(path):
            return
        tag = owner or "unknown"
        if tag.startswith("type:"):
            tag = tag[5:]
        elif tag.startswith("path:"):
            # e.g. '/sys/class/thermal/thermal_zone7/temp' (or its
            # hwmon mirror) -> 'thermal_zone7'
            zone_dir = _zone_dir_of(tag[5:])
            tag = (os.path.basename(zone_dir) if zone_dir
                   else os.path.basename(os.path.dirname(tag[5:])) or tag[5:])
        tag = re.sub(r'[^A-Za-z0-9_.-]', '_', tag)[:48] or "unknown"
        try:
            os.replace(path, f"{path}.{tag}.old")
        except OSError:
            pass

    def _invalidate_data_if_sensor_changed(self):
        """Discard collected data if it was gathered with a different sensor.

        Temperatures recorded against one sensor are meaningless for another
        (e.g. a station upgrading from a stuck acpitz reading to the PCH
        sensor), and mixing them corrupts every fit. If the sensor in use
        differs from the one the data was gathered with, archive the CSV and the
        recalibration log and reset calibration so collection starts clean.

        Identity is keyed on the sensor *type* rather than its path, so a
        thermal-zone renumber across a kernel update (same physical sensor,
        new index) does not needlessly discard good data. Where no sensor was
        recorded at all the old hardcoded default is assumed, which is what any
        pre-upgrade CSV was necessarily collected against.
        """
        cur = sensor_identity(self.sensor_path)
        prev = None
        try:
            with open(self._sensor_meta_path) as f:
                prev = f.read().strip()
        except OSError:
            pass

        if prev and not prev.startswith(("type:", "path:")):
            # Written by the first version of this check, which stored a bare
            # path. Resolve it to an identity so an unchanged sensor is
            # recognized. (If the zone renumbered since, the type at that index
            # is now someone else's and we correctly treat it as a change.)
            prev = sensor_identity(prev)

        has_csv = os.path.exists(self._csv_path)
        if prev is None and has_csv:
            # Pre-upgrade install: no sensor was recorded, but the CSV can only
            # have been collected against the old hardcoded default, so key off
            # that rather than discarding it as unknown provenance. On ARM/Pi
            # auto-detect picks that same zone, so those stations keep their data.
            prev = sensor_identity(DEFAULT_SENSOR)

        stale = prev is not None and prev != cur

        if stale:
            # Archive under the sensor that produced them, so a later change (or
            # a flip back) can't overwrite an earlier station's history. The
            # calibration range is derived data, so it's simply dropped.
            self._archive(self._csv_path, prev)
            self._archive(self._recal_log_path, prev)
            for derived in (self._cal_range_path, self._reported_path):
                try:
                    os.remove(derived)
                except OSError:
                    pass
            self._cal_range = None
            self._last_recal_time = 0
            self.recal_logs = []
            log.info("tempcomp: sensor changed (%s -> %s); archived old data",
                     prev, cur)

        # Record the sensor now in use for the next startup.
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._sensor_meta_path, 'w') as f:
                f.write(cur + '\n')
        except OSError:
            pass

    def reset_data(self) -> Optional[str]:
        """Start collection over, discarding the data and the fit made from it.

        For data that no restart or repair can vouch for: gathered across a
        sensor switch auto-detect didn't catch, through service-restart loops,
        or under a bug since fixed. A calibration fitted to such data is just
        as suspect, so chrony.conf's tempcomp line is commented out too;
        auto-recalibration re-enables it with a fit to the fresh data. The CSV
        is archived, not deleted.

        Returns None, or why tempcomp couldn't be disabled (the data is reset
        either way).
        """
        error = None
        if self._config and self._config.is_active:
            error = self._run_apply_helper("--disable")
            if error is None:
                self._config = parse_chrony_tempcomp()
        if error is None:
            # The range and the reversed-sign flag describe the fit just dropped.
            for derived in (self._cal_range_path, self._cal_reversed_path):
                try:
                    os.remove(derived)
                except OSError:
                    pass
            self._cal_range = None

        self._archive(self._csv_path, time.strftime("reset-%Y%m%d-%H%M%S"))
        # A missing marker reads as format 1, which would "repair" fresh rows.
        self._mark_csv_format()
        self._reset_reported()
        for d in (self._temps, self._freqs, self._timestamps, self._minute_temps,
                  self._minute_freqs, self._minute_reported, self._minute_comps,
                  self._minute_chrony_comps, self._comp_noise):
            d.clear()
        self._minute_count = 0
        self._last_sample_time = 0
        self._start_time = time.time()
        self._cached_correlation = None
        if self.learner:
            # The sensor and smoothing are chosen again from fresh evidence;
            # the pin stays until then, and tempcomp is off meanwhile anyway
            self.learner.reset()
            self._choice_pending = ""
        if error:
            self._log_recal(f"Data reset by operator; tempcomp left on: {error}")
        else:
            self._log_recal("Data reset by operator; tempcomp off until refitted")
        return error

    def record(self, temp_millideg: float, frequency_ppm: float,
               trusted: bool = True):
        """Record a (temp, freq) pair. Called every 1s from the monitor loop.

        trusted is the monitor's word that chronyd is locked to its reference
        right now. Minutes are judged by that and by chronyd's uptime, never
        against a fit of earlier minutes: a fit that filters its own input
        only learns what it already believes, and once it has locked onto bad
        data it refuses the good minutes that would correct it.

        If tempcomp is active, reconstructs the raw crystal frequency -- what
        chronyc would report without it -- by taking the current compensation
        back out. chronyd steers the clock by `reported - comp`, so the
        frequency it reports with tempcomp on is `raw + comp`. This allows
        calibration data collection while tempcomp is running.
        """
        # Reconstruct raw frequency if tempcomp is active
        raw_freq = frequency_ppm
        comp = 0.0
        # A chronyd that can't read its sensor isn't compensating at all
        if self._config and self._config.is_active and not self._read_failing:
            # chrony's compensation is a function of what *it* reads: the
            # smoothed value or the raw sensor behind the input link, or another
            # sensor altogether (e.g. right after auto-detect moves off a stuck
            # acpitz while chrony.conf still names the old zone). Evaluating
            # the polynomial at our temperature would inject a spurious
            # temperature-dependent term with the old k1 slope.
            comp_temp = read_temperature(self._config.sensor_path)
            if comp_temp is None and self._config_matches_sensor():
                comp_temp = temp_millideg
            # If chrony's sensor is unreadable, chrony isn't compensating
            # either, so the measured frequency is already the raw one.
            if comp_temp is not None:
                # Past its limit chronyd keeps the last value it accepted,
                # which is about the limit since temperature crosses it slowly.
                comp = _compute_compensation(self._config, comp_temp)
                comp = max(-CHRONY_MAX_COMP, min(CHRONY_MAX_COMP, comp))
                raw_freq = frequency_ppm - comp
                self._minute_chrony_comps.append(comp)

        if self.learner and self.learner.learning:
            self.learner.sample()
        self._minute_temps.append(temp_millideg)
        self._minute_freqs.append(raw_freq)
        self._minute_reported.append(frequency_ppm)
        self._minute_comps.append(comp)
        now = time.time()
        self._check_settled(now, trusted)
        if self._minute_count == 0:
            self._minute_start = now
        self._minute_count += 1

        # A minute of wall clock, not 60 calls: the monitor loop takes more
        # than its 1 s interval (chronyc calls), so 60 calls ran ~77 s and
        # "6 hours" of samples took nearly 8.
        if self._minute_count >= 60 or now - self._minute_start >= 60:
            n = len(self._minute_temps)
            avg_temp = sum(self._minute_temps) / n
            avg_freq = sum(self._minute_freqs) / n
            avg_reported = sum(self._minute_reported) / n
            avg_comp = sum(self._minute_comps) / n
            cc = self._minute_chrony_comps
            if len(cc) >= 30:
                m = sum(cc) / len(cc)
                self._comp_noise.append(
                    math.sqrt(sum((c - m) ** 2 for c in cc) / len(cc)))
            for bucket in (self._minute_temps, self._minute_freqs,
                           self._minute_reported, self._minute_comps, cc):
                bucket.clear()
            self._minute_count = 0

            # Any untrusted second in the minute extends the settle window
            # past its end, so this drops that minute too.
            if self.learner and self.learner.learning:
                self.learner.close_minute(int(now), avg_freq,
                                          keep=now > self._settle_until)
                self._check_learning(now)
            if now <= self._settle_until:
                return

            ts = int(now)
            self._append_reported(ts, avg_temp, avg_reported, avg_comp)
            self._last_sample_time = ts
            self._temps.append(avg_temp)
            self._freqs.append(avg_freq)
            self._timestamps.append(ts)

            self._append_csv(ts, avg_temp, avg_freq)
            self._cached_correlation = None

            # Prune CSV daily to match deque contents
            now = time.time()
            if now - self._last_prune_time > 86400:
                rows = list(zip(self._timestamps, self._temps, self._freqs))
                self._rewrite_csv(rows)
                self._last_prune_time = now

            # Check if auto-recalibration is warranted
            if self.auto_recal:
                self._check_recalibration()

    def get_status(self) -> TempCompStatus:
        """Build current status for display."""
        status = TempCompStatus()

        temp = read_temperature(self.sensor_path)
        if self.smoother and self.smoother.publishing():
            temp = self.smoother.value
        if temp is not None:
            status.current_temp_c = temp / 1000.0

        status.config = self._config
        status.sample_count = len(self._temps)
        if self.learner:
            if self.learner.learning:
                status.learning_s = self.learner.span_s()
                best = self.learner.preferred(sensor_identity(self.sensor_path))
                if best:
                    r2, ident, tau = best[:3]
                    status.learning_best = (sensor_label(ident), tau, r2)
            else:
                ident, tau = self.learner.choice[:2]
                status.sensor_choice = (sensor_label(ident), tau)
        if len(self._timestamps) >= 2:
            status.collection_duration_s = int(self._timestamps[-1] - self._timestamps[0])
        elif self._start_time:
            status.collection_duration_s = int(time.time() - self._start_time)
        else:
            status.collection_duration_s = 0

        if len(self._temps) > 0:
            min_t = min(self._temps) / 1000.0
            max_t = max(self._temps) / 1000.0
            status.temp_range = (min_t, max_t)

            temp_range_c = max_t - min_t

            # Stuck-sensor guard: clearly-varying frequency, yet the temperature
            # never moves -> the sensor is almost certainly reporting a constant
            # (e.g. a broken acpitz). Gated on a full fit's worth of samples,
            # not an hour's: a climate-controlled site really can hold under
            # half a degree for an hour, and crying wolf on every data reset
            # would train the warning to be ignored.
            if (len(self._temps) >= MIN_SAMPLES_FIT
                    and temp_range_c < STUCK_SENSOR_TEMP_RANGE_C):
                freq_span = max(self._freqs) - min(self._freqs)
                if freq_span > STUCK_SENSOR_MIN_FREQ_SPAN:
                    status.warning = "sensor stuck? (no temp variation)"
            if (len(self._temps) >= MIN_SAMPLES_CORRELATION
                    and temp_range_c >= MIN_TEMP_RANGE_CORRELATION):
                now = time.time()
                if self._cached_correlation is None or now - self._correlation_time > 60:
                    temps_md, freqs = _filter_outliers(list(self._temps), list(self._freqs))
                    if len(freqs) >= MIN_SAMPLES_CORRELATION:
                        T0, k0, k1, k2 = _polyfit_quadratic(temps_md, freqs)
                        predicted = [k0 + k1 * (t - T0) + k2 * (t - T0) ** 2
                                     for t in temps_md]
                        ss_res = sum((f - p) ** 2 for f, p in zip(freqs, predicted))
                        mean_f = sum(freqs) / len(freqs)
                        ss_tot = sum((f - mean_f) ** 2 for f in freqs)
                        self._cached_correlation = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0
                    self._correlation_time = now
                status.correlation = self._cached_correlation

        # Collection stalled: the monitor is running but nothing has made it
        # into the fit data for hours (no frequency, or never locked long enough)
        now = time.time()
        if self._start_time and now - self._start_time > STALL_WARN_S:
            idle = now - max(self._last_sample_time, self._start_time)
            if idle > STALL_WARN_S:
                status.warning = f"no new samples for {int(idle // 3600)}h"

        # Post-correction check, only meaningful against chronyd's own sensor
        if (self._config and self._config.is_active
                and self._config_matches_sensor()):
            if self._cached_residual is None or now - self._residual_time > 60:
                self._cached_residual = self._residual_metrics() or ()
                self._residual_time = now
            if self._cached_residual:
                slope, r2, reduction = self._cached_residual
                status.residual_slope, status.residual_r2 = slope, r2
                status.wander_reduction = reduction
                # The calibration's own slope where the window sat: a working
                # one leaves only a small share of it behind
                mean_t = sum(r[1] for r in self._reported) / len(self._reported)
                cfg = self._config
                cal_slope = abs(cfg.k1 + 2 * cfg.k2 * (mean_t - cfg.T0)) * 1000.0
                tracks = (r2 > RESIDUAL_WARN_R2 and abs(slope) > max(
                    RESIDUAL_WARN_MIN_SLOPE, RESIDUAL_WARN_SHARE * cal_slope))
                if tracks:
                    status.warning = "freq still tracks temp!"
                elif reduction is not None and reduction < 0:
                    status.warning = "comp adds wander!"

        # chronyd reads a sensor too noisy to steer by, as when it is fed the
        # raw sensor instead of the smoothed value. Kept to the last hour, so it
        # clears soon after the cause is fixed.
        if (self._config and self._config.is_active and not status.warning
                and len(self._comp_noise) >= COMP_NOISE_MINUTES // 2):
            noise = sorted(self._comp_noise)[len(self._comp_noise) // 2]
            if noise > COMP_NOISE_WARN_PPM:
                status.warning = "noisy sensor, comp jitters!"

        # Extrapolation check with hysteresis
        if self._config and self._config.is_active and status.current_temp_c is not None:
            cal_range = self._get_calibration_range()
            if cal_range:
                cal_min, cal_max = cal_range
                status.cal_range = cal_range
                t = status.current_temp_c
                if self._was_extrapolating:
                    # Clear only when well inside range
                    if cal_min <= t <= cal_max:
                        self._was_extrapolating = False
                else:
                    # Trigger when clearly outside range
                    if t < cal_min - 2.0 or t > cal_max + 2.0:
                        self._was_extrapolating = True
                status.is_extrapolating = self._was_extrapolating

        # chronyd cannot read the sensor it was given: the directive is inert
        # no matter how good the fit is, so this outranks every other warning.
        if self._config and self._config.is_active:
            now = time.time()
            if now - self._read_failing_time > 60:
                self._read_failing = chrony_sensor_read_failing()
                self._read_failing_time = now
            if self._read_failing:
                # Kept short: the TempComp line already carries four fields and
                # this must survive an 80-column terminal. README explains the
                # usual cause (AppArmor) and how to check.
                status.warning = "chronyd can't read sensor!"

        if self._last_recal_time > 0:
            ago = int(time.time() - self._last_recal_time)
            if ago < 3600:
                status.last_recal = f"{ago // 60}m ago"
            elif ago < 86400:
                status.last_recal = f"{ago // 3600}h ago"
            else:
                status.last_recal = f"{ago // 86400}d ago"

        return status

    def _check_learning(self, now: float):
        """Score the candidates now and then; choose once the evidence suffices."""
        if self.learner.due(now):
            self.learner.score(now)
            return              # the winner is looked at once the score is in
        if time.time() - self._last_attempt_time < LEARN_SCORE_S:
            return              # a failed choice is retried at most hourly
        win = self.learner.winner(sensor_identity(self.sensor_path))
        if win:
            self._choose_sensor(*win)

    def _choose_sensor(self, ident: str, tau: float, r2: float):
        """Make the learning phase's choice: point the stable link at the
        sensor, refit from its logged minutes, and keep it until a reset."""
        self._last_attempt_time = time.time()
        incumbent = sensor_identity(self.sensor_path)
        if ident != incumbent:
            error = self._run_apply_helper("--sensor", ident)
            self.sensor_path = preferred_sensor_path()
            if error is None and sensor_identity(self.sensor_path) != ident:
                # e.g. the root-owned resolver predates pins; the updater
                # brings it up to date
                error = f"the sensor link still reads {sensor_label(incumbent)}"
            if error:
                msg = f"Sensor choice {sensor_label(ident)} pending: {error}"
                if msg != self._choice_pending:
                    self._choice_pending = msg
                    self._log_recal(msg)
                return
        self._choice_pending = ""

        ranking = {r[1]: r[0] for r in self.learner.ranking}
        rows = self.learner.fit_rows(ident, tau)
        self.learner.decide(ident, tau, r2)

        # The new smoothing starts over: the old value is another sensor's,
        # or smoothed differently
        self._smooth_tau = max(SMOOTH_TAU_S, tau * 60)
        self.smoother = None
        self._resume_smoother = False

        # Fit data from now on is the chosen sensor's, smoothed as chronyd
        # will read it; what was collected before is archived
        self._archive(self._csv_path, time.strftime("prechoice-%Y%m%d-%H%M%S"))
        if ident != incumbent:
            self._archive(self._recal_log_path, incumbent)
            self.recal_logs = []
            for derived in (self._cal_range_path, self._cal_reversed_path):
                try:
                    os.remove(derived)
                except OSError:
                    pass
            self._cal_range = None
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._sensor_meta_path, 'w') as f:
                f.write(ident + '\n')
        except OSError:
            pass
        self._reset_reported()
        self._comp_noise.clear()
        for d in (self._temps, self._freqs, self._timestamps):
            d.clear()
        for ts, temp, freq in rows[-MAX_SAMPLES:]:
            self._timestamps.append(ts)
            self._temps.append(temp)
            self._freqs.append(freq)
        self._rewrite_csv(list(zip(self._timestamps, self._temps, self._freqs)))
        self._mark_csv_format()
        self._cached_correlation = None

        was = ranking.get(incumbent)
        was = f"; {sensor_label(incumbent)} R2={was:.2f}" if (
            was is not None and ident != incumbent) else ""
        self._log_recal(f"Sensor chosen: {sensor_label(ident)}, smoothed "
                        f"{tau:g} min (held-out R2={r2:.2f}{was})")

        # Refit now, from the logged minutes, rather than a day from now
        self._last_recal_time = 0
        self._last_attempt_time = 0
        if self.auto_recal:
            self._check_recalibration()

    def _check_settled(self, now: float, trusted: bool):
        """Push the settle window out after lost lock or a chronyd (re)start."""
        if not trusted:
            self._settle_until = max(self._settle_until, now + SETTLE_S)
        if now - self._chronyd_check_time >= 30:
            self._chronyd_check_time = now
            age = chronyd_age()
            if age is not None and age < SETTLE_S:
                self._settle_until = max(self._settle_until, now - age + SETTLE_S)

    def _check_recalibration(self):
        """Check if recalibration is warranted and apply if so."""
        # Enforce minimum interval between recalibrations (also covers failed
        # attempts). Include the last *applied* recalibration loaded from the log
        # at startup, not just attempts made by this process: every apply
        # restarts chrony, and without this a monitor restart (self-update,
        # relogin) could re-apply within minutes of the previous one.
        # The one exception is replacing a calibration known to be wrong (see
        # _repair_reversed_sign), or having none active at all (after a data
        # reset), which needn't wait out the cooldown -- though failed attempts
        # still do.
        # No calibration until the learning phase has chosen what to fit to
        if self.learner and self.learner.learning:
            return
        now = time.time()
        cal_bad = (os.path.exists(self._cal_reversed_path)
                   or not (self._config and self._config.is_active))
        last = max(self._last_attempt_time,
                   0 if cal_bad else self._last_recal_time)
        if last > 0 and now - last < MIN_RECAL_INTERVAL:
            return

        # Need enough data
        if len(self._temps) < MIN_SAMPLES_FIT:
            return

        temps_c = [t / 1000.0 for t in self._temps]
        temp_range = max(temps_c) - min(temps_c)
        if temp_range < MIN_TEMP_RANGE_FIT:
            return

        # Compute new fit from raw data (outliers filtered). chronyd reports
        # raw + comp, so the compensation that flattens it is the fitted raw
        # curve negated.
        temps_md, freqs = _filter_outliers(list(self._temps), list(self._freqs))
        if len(freqs) < MIN_SAMPLES_FIT:
            return
        new_T0, a0, a1, a2 = _polyfit_quadratic(temps_md, freqs)
        new_k0, new_k1, new_k2 = -a0, -a1, -a2

        if self._config and self._config.is_active and self._config_matches_sensor():
            # Compare residual slope: current vs proposed
            # Simulate the frequency chronyd would report with each compensation
            current_residuals = []
            new_residuals = []
            for t, f in zip(temps_md, freqs):
                current_comp = _compute_compensation(self._config, t)
                new_comp = new_k0 + new_k1 * (t - new_T0) + new_k2 * (t - new_T0) ** 2
                current_residuals.append(f + current_comp)
                new_residuals.append(f + new_comp)

            current_slope = abs(_residual_slope(temps_c, current_residuals))
            new_slope = abs(_residual_slope(temps_c, new_residuals))

            # Only recalibrate if significant improvement
            if current_slope < 0.001:
                return  # current calibration is already excellent
            improvement = 1.0 - (new_slope / current_slope) if current_slope > 0 else 0
            if improvement < MIN_IMPROVEMENT:
                return
        else:
            # No active tempcomp, or one keyed to a sensor we're not collecting
            # against (its coefficients aren't comparable to ours, so there is
            # nothing meaningful to improve on) — check instead that the
            # quadratic fit explains enough variance
            predicted = [a0 + a1 * (t - new_T0) + a2 * (t - new_T0) ** 2
                         for t in temps_md]
            ss_res = sum((f - p) ** 2 for f, p in zip(freqs, predicted))
            mean_f = sum(freqs) / len(freqs)
            ss_tot = sum((f - mean_f) ** 2 for f in freqs)
            r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0
            if r_squared < 0.6:
                return  # temperature doesn't explain enough frequency variance

        # Fit chronyd's ±10 ppm limit. Done after the quality checks, which
        # need the uncentered fit, so a skip is only logged for a calibration
        # that would otherwise have been applied; it counts as an attempt so
        # the log gets one entry per cooldown rather than one a minute.
        new_k0, half_span = _center_compensation(
            temps_md, new_T0, new_k0, new_k1, new_k2)
        if half_span > CHRONY_MAX_COMP:
            self._last_attempt_time = time.time()
            self._log_recal(f"Skipped: needs ±{half_span:.1f}ppm, beyond "
                            f"chronyd's ±{CHRONY_MAX_COMP:.0f}ppm limit")
            return

        # Apply the new calibration
        self._apply_calibration(new_T0, new_k0, new_k1, new_k2)

    def _apply_calibration(self, T0: float, k0: float, k1: float, k2: float):
        """Write proposed tempcomp and apply via the helper script."""
        self._last_attempt_time = time.time()
        # Prefer the reboot-stable symlink so the directive survives thermal-zone
        # renumbering; fall back to the concrete path if the symlink service
        # isn't installed. Gate on being able to *read* it, not just on it
        # existing: writing a sensor chrony can't read silently disables
        # compensation, which is the failure this whole mechanism exists to stop.
        # The input link serves the smoothed value, or the stable link's sensor
        # when the monitor isn't publishing it.
        sensor = self.sensor_path
        if read_temperature(TEMPCOMP_INPUT_LINK) is not None:
            sensor = TEMPCOMP_INPUT_LINK
        elif read_temperature(STABLE_SENSOR_LINK) is not None:
            sensor = STABLE_SENSOR_LINK
        new_line = f"tempcomp {sensor} 30 {T0:.0f} {k0:.6f} {k1:.10f} {k2:.12f}"

        # Write proposed config to staging file
        proposed_path = os.path.join(self._data_dir, "tempcomp.proposed")
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(proposed_path, 'w') as f:
                f.write(new_line + '\n')
        except OSError as e:
            self._log_recal(f"Failed: cannot write proposed: {e}")
            return

        error = self._run_apply_helper(proposed_path)
        if error:
            self._log_recal(f"Failed: {error}")
            return

        # Success — update in-memory config and save calibration range
        self._last_recal_time = time.time()
        conf_path = self._config.conf_path if self._config else ""
        self._config = TempCompConfig(
            sensor_path=sensor, interval=30,
            T0=T0, k0=k0, k1=k1, k2=k2,
            is_active=True, conf_path=conf_path
        )
        cal_min = min(self._temps) / 1000.0
        cal_max = max(self._temps) / 1000.0
        self._cal_range = (cal_min, cal_max)
        self._save_cal_range()
        self._reset_reported()  # judge the new calibration on its own minutes
        self._comp_noise.clear()
        try:
            os.remove(self._cal_reversed_path)
        except OSError:
            pass
        self._log_recal(f"Recalibrated: T0={T0:.0f} k1={k1:.6e} range={cal_min:.0f}-{cal_max:.0f}C")

    def _run_apply_helper(self, *args: str) -> Optional[str]:
        """Run apply-tempcomp.sh <args> as root. None on success, else why not."""
        # Prefer the root-owned installed copy: the sudoers NOPASSWD rule
        # authorizes exactly /usr/local/bin/apply-tempcomp.sh, so `sudo -n` only
        # succeeds for that path. The repo/source copy lives in a user-writable
        # directory and is both unauthorized and unsafe to run as root, so it's
        # only a last-resort fallback for dev environments without an install.
        script = None
        for candidate in [
            "/usr/local/bin/apply-tempcomp.sh",
            os.path.join(self._data_dir, "apply-tempcomp.sh"),
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "apply-tempcomp.sh"),
        ]:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                script = candidate
                break
        if not script:
            return "apply-tempcomp.sh not found"
        try:
            proc = subprocess.run(["sudo", "-n", script, *args],
                                  capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError) as e:
            return str(e)
        if proc.returncode != 0:
            return proc.stderr.strip() or f"exit {proc.returncode}"
        return None

    def _log_recal(self, msg: str):
        """Log a recalibration event."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.recal_logs.append(entry)
        # Keep last 10
        if len(self.recal_logs) > 10:
            self.recal_logs = self.recal_logs[-10:]
        # Persist
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._recal_log_path, 'a') as f:
                f.write(entry + '\n')
        except OSError:
            pass

    def _log_entries(self, marker: str) -> list:
        """(time, line) of the log entries containing marker, oldest first."""
        entries = []
        try:
            with open(self._recal_log_path) as f:
                for line in f:
                    if marker in line:
                        # Extract timestamp from [YYYY-MM-DD HH:MM:SS], which
                        # _log_recal writes in local time
                        match = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
                        if match:
                            t = time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                            entries.append((time.mktime(t), line.rstrip()))
        except (OSError, ValueError, OverflowError):
            pass
        return entries

    def _recal_times(self) -> list:
        """Times of the applied recalibrations in the log, oldest first."""
        return [t for t, _ in self._log_entries('Recalibrated:')]

    def _load_recal_time(self):
        """Load the last recalibration timestamp from the log file."""
        times = self._recal_times()
        if times:
            self._last_recal_time = times[-1]

    def _repair_reversed_sign(self, rows: list) -> list:
        """Correct rows recorded with tempcomp's sign reversed.

        Before CSV_FORMAT 2, a row recorded while tempcomp was active took the
        raw frequency as reported + comp; chronyd reports raw + comp, so the row
        holds raw + 2*comp. Recover raw wherever the calibration chronyd was
        applying is still known: chrony.conf's since the last logged
        recalibration, and before that the one apply-tempcomp.sh saved to
        chrony.conf.bak, back to the recalibration before. Older rows, and rows
        under a calibration keyed to another sensor, are dropped.

        A row taken while chronyd couldn't read its sensor (an AppArmor denial,
        say) holds raw + comp instead: chronyd applied nothing, but the comp
        was still added. The journal tells the two apart; a compensated row it
        doesn't cover is dropped, since either guess could be off by a whole
        comp.

        Every calibration this monitor applied was fitted to such rows with the
        reversed sign, so one still active is flagged for prompt replacement.
        """
        times = self._recal_times()
        last_t = times[-1] if times else None
        prev_t = times[-2] if len(times) >= 2 else None
        cur = self._config
        bak, bak_known = self._bak_config()
        journal = self._sensor_read_journal(rows)

        repaired, fixed, dropped = [], 0, 0
        for ts, temp, freq in rows:
            if last_t is None or ts >= last_t:
                cfg, ok = cur, self._cal_known(cur)
            elif prev_t is None or ts >= prev_t:
                cfg, ok = bak, bak_known and self._cal_known(bak)
            else:
                cfg, ok = None, False
            if not ok:
                dropped += 1
                continue
            comp, mult = 0.0, 0
            if cfg and cfg.is_active:
                compensating = _chronyd_compensating(journal, ts, cfg.interval)
                if compensating is None:
                    dropped += 1
                    continue
                comp = _clamped_compensation(cfg, temp)
                mult = 2 if compensating else 1
                fixed += 1
            repaired.append((ts, temp, freq - mult * comp))

        if last_t is not None and cur and cur.is_active:
            self._flag_cal_bad()
        if fixed or dropped:
            self._log_recal(f"Repaired {fixed} {REPAIR_LOG_MARKER}; dropped "
                            f"{dropped} {REPAIR_LOG_END}")
        return repaired

    def _undo_uncompensated_repair(self, rows: list) -> list:
        """Give back the comp 93b132c's repair took from uncompensated rows.

        That repair (the CSV_FORMAT 2 one before this was fixed) took 2*comp
        off every row recorded under an active calibration, so a row taken
        while chronyd couldn't read its sensor now holds raw - comp. Its log
        entry lacks REPAIR_LOG_END, which the corrected repair writes. Rows
        recorded after it are correct, as are rows the journal shows chronyd
        compensating for.

        The repair used chrony.conf and its .bak as they were then; a
        recalibration since has moved the first to .bak and lost the second.
        A row that needs its comp back under a calibration no longer known, or
        that the journal can't vouch for, is dropped. A calibration fitted to
        the uncorrected rows is flagged for prompt replacement.
        """
        repairs = self._log_entries(REPAIR_LOG_MARKER)
        if not repairs or repairs[-1][1].endswith(REPAIR_LOG_END):
            return rows             # nothing repaired, or repaired correctly
        repaired_at = repairs[-1][0]
        times = self._recal_times()
        before = [t for t in times if t <= repaired_at]
        since = len(times) - len(before)
        last_t = before[-1] if before else None
        prev_t = before[-2] if len(before) >= 2 else None
        bak, bak_known = self._bak_config()
        if since == 0:
            then_cur, then_bak = (self._config, True), (bak, bak_known)
        elif since == 1:
            then_cur, then_bak = (bak, bak_known), (None, False)
        else:
            then_cur = then_bak = (None, False)
        journal = self._sensor_read_journal([r for r in rows if r[0] < repaired_at])

        out, fixed, dropped = [], 0, 0
        for ts, temp, freq in rows:
            if ts >= repaired_at:
                out.append((ts, temp, freq))
                continue
            if last_t is None or ts >= last_t:
                cfg, ok = then_cur
            elif prev_t is None or ts >= prev_t:
                cfg, ok = then_bak
            else:
                cfg, ok = None, False
            if ok and not (cfg and cfg.is_active):
                out.append((ts, temp, freq))        # nothing was taken off
                continue
            compensating = _chronyd_compensating(
                journal, ts, cfg.interval if cfg else DEFAULT_SENSOR_INTERVAL)
            if compensating:
                out.append((ts, temp, freq))        # 2*comp was right
                continue
            if compensating is None or not ok:
                dropped += 1
                continue
            out.append((ts, temp, freq + _clamped_compensation(cfg, temp)))
            fixed += 1

        if fixed and since:
            self._flag_cal_bad()
        if fixed or dropped:
            self._log_recal(f"Restored the comp to {fixed} rows chronyd never "
                            f"compensated; dropped {dropped} it can't account for")
        return out

    def _bak_config(self) -> tuple:
        """(calibration in chrony.conf.bak, whether that file exists)."""
        cur = self._config
        conf_path = cur.conf_path if cur and cur.conf_path else None
        if conf_path is None:
            conf_path = next((p for p in CONF_PATHS if os.path.exists(p)), None)
        bak_path = conf_path + ".bak" if conf_path else None
        if bak_path is None or not os.path.exists(bak_path):
            return None, False
        return parse_chrony_tempcomp([bak_path]), True

    def _cal_known(self, cfg) -> bool:
        # A calibration on another sensor ran off temperatures we never saw
        return not (cfg and cfg.is_active) or sensor_identity(
            cfg.sensor_path) == sensor_identity(self.sensor_path)

    @staticmethod
    def _sensor_read_journal(rows: list) -> Optional[tuple]:
        return chrony_sensor_read_failures(min(r[0] for r in rows)) if rows else None

    def _flag_cal_bad(self):
        """Mark the active calibration as fitted to bad data (see cal_reversed)."""
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._cal_reversed_path, 'w'):
                pass
        except OSError:
            pass

    def _csv_format(self) -> int:
        try:
            with open(self._csv_format_path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return 1

    def _mark_csv_format(self):
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._csv_format_path, 'w') as f:
                f.write(f"{CSV_FORMAT}\n")
        except OSError:
            pass

    def _save_cal_range(self):
        """Persist the calibration temperature range."""
        if self._cal_range is None:
            return
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._cal_range_path, 'w') as f:
                f.write(f"{self._cal_range[0]:.1f},{self._cal_range[1]:.1f}\n")
        except OSError:
            pass

    def _load_cal_range(self):
        """Load the calibration temperature range."""
        try:
            with open(self._cal_range_path) as f:
                parts = f.read().strip().split(',')
                if len(parts) == 2:
                    self._cal_range = (float(parts[0]), float(parts[1]))
        except (OSError, ValueError):
            pass

    def _get_calibration_range(self) -> Optional[tuple]:
        """Get the temperature range the current calibration covers."""
        if self._cal_range:
            return self._cal_range
        if not self._config:
            return None
        if not self._config_matches_sensor():
            # T0 is in the other sensor's scale, so a range derived from it says
            # nothing about the temperatures we read. Better no range than a
            # wrong one that would flag a bogus OUTSIDE.
            return None
        # Fallback: estimate ±10°C around T0
        T0_c = self._config.T0 / 1000.0
        return (T0_c - 10.0, T0_c + 10.0)

    def _load_csv(self):
        """Read existing CSV into deques. Prune old entries and outliers."""
        if not os.path.exists(self._csv_path):
            self._mark_csv_format()  # whatever is recorded now is current
            return

        cutoff = int(time.time()) - MAX_DATA_DAYS * 86400
        raw_rows = []

        try:
            with open(self._csv_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(',')
                    if len(parts) != 3:
                        continue
                    try:
                        ts = int(parts[0])
                        temp = float(parts[1])
                        freq = float(parts[2])
                    except ValueError:
                        continue
                    if ts >= cutoff:
                        raw_rows.append((ts, temp, freq))
        except OSError:
            return

        fmt = self._csv_format()
        repaired = fmt < CSV_FORMAT
        if fmt < 2:
            raw_rows = self._repair_reversed_sign(raw_rows)
        elif repaired:
            raw_rows = self._undo_uncompensated_repair(raw_rows)

        # Filter outliers using IQR on frequency
        rows_to_keep = raw_rows
        if len(raw_rows) >= 20:
            freqs_sorted = sorted(r[2] for r in raw_rows)
            q1 = freqs_sorted[len(freqs_sorted) // 4]
            q3 = freqs_sorted[3 * len(freqs_sorted) // 4]
            iqr = q3 - q1
            lower = q1 - 3.0 * iqr
            upper = q3 + 3.0 * iqr
            rows_to_keep = [r for r in raw_rows if lower <= r[2] <= upper]

        for ts, temp, freq in rows_to_keep:
            self._timestamps.append(ts)
            self._temps.append(temp)
            self._freqs.append(freq)

        # Rewrite file if we repaired or pruned anything
        if repaired or len(rows_to_keep) < len(raw_rows):
            if self._rewrite_csv(rows_to_keep) and repaired:
                self._mark_csv_format()

    def _append_reported(self, ts: int, temp: float, reported: float, comp: float):
        """Record a minute of what chronyd reported, and the comp it applied."""
        self._reported.append((ts, temp, reported, comp))
        self._cached_residual = None
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._reported_path, 'a') as f:
                f.write(f"{ts},{temp:.0f},{reported:.6f},{comp:.6f}\n")
        except OSError:
            pass
        # Keep the file about a window long
        if len(self._reported) == self._reported.maxlen and ts % 3600 < 60:
            self._write_reported()

    def _write_reported(self):
        tmp_path = self._reported_path + '.tmp'
        try:
            with open(tmp_path, 'w') as f:
                for ts, temp, reported, comp in self._reported:
                    f.write(f"{ts},{temp:.0f},{reported:.6f},{comp:.6f}\n")
            os.replace(tmp_path, self._reported_path)
        except OSError:
            pass

    def _reset_reported(self):
        self._reported.clear()
        self._cached_residual = None
        try:
            os.remove(self._reported_path)
        except OSError:
            pass

    def _load_reported(self):
        """Load the last window of reported minutes under the active calibration."""
        since = max(time.time() - RESIDUAL_WINDOW_S, self._last_recal_time)
        try:
            with open(self._reported_path) as f:
                for line in f:
                    parts = line.strip().split(',')
                    if len(parts) != 4:
                        continue
                    try:
                        row = (int(parts[0]), float(parts[1]),
                               float(parts[2]), float(parts[3]))
                    except ValueError:
                        continue
                    if row[0] >= since:
                        self._reported.append(row)
        except OSError:
            pass

    def _residual_metrics(self) -> Optional[tuple]:
        """(slope ppm/°C, R², wander reduction) of the reported frequency over
        the window, or None without enough of it to judge."""
        rows = list(self._reported)
        if len(rows) < RESIDUAL_MIN_SAMPLES:
            return None
        temps_c = [r[1] / 1000.0 for r in rows]
        if max(temps_c) - min(temps_c) < RESIDUAL_MIN_TEMP_RANGE:
            return None
        reported = [r[2] for r in rows]
        raw = [r[2] - r[3] for r in rows]
        n = len(rows)
        mt, mr = sum(temps_c) / n, sum(reported) / n
        sxx = sum((t - mt) ** 2 for t in temps_c)
        sxy = sum((t - mt) * (f - mr) for t, f in zip(temps_c, reported))
        syy = sum((f - mr) ** 2 for f in reported)
        if sxx <= 0:
            return None
        slope = sxy / sxx
        r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
        mraw = sum(raw) / n
        sraw = math.sqrt(sum((f - mraw) ** 2 for f in raw) / n)
        reduction = 1.0 - math.sqrt(syy / n) / sraw if sraw > 0 else None
        return slope, r2, reduction

    def _append_csv(self, ts: int, temp: float, freq: float):
        """Append one row to the CSV file."""
        os.makedirs(self._data_dir, exist_ok=True)
        try:
            with open(self._csv_path, 'a') as f:
                f.write(f"{ts},{temp:.0f},{freq:.6f}\n")
        except OSError:
            pass

    def _rewrite_csv(self, rows: list) -> bool:
        """Atomically rewrite the CSV file (for pruning). True on success."""
        tmp_path = self._csv_path + '.tmp'
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(tmp_path, 'w') as f:
                for ts, temp, freq in rows:
                    f.write(f"{ts},{temp:.0f},{freq:.6f}\n")
            os.replace(tmp_path, self._csv_path)
        except OSError:
            return False
        return True


def _cli(argv=None) -> int:
    """Print the sensor to point STABLE_SENSOR_LINK at, or fail if there is none.

    This is the single source of truth for the boot-time symlink service
    (update-tempcomp-symlink.sh), which runs it as root from a root-owned copy of
    the package. Exits non-zero when no usable zone exists, so the caller can
    refuse to publish a link rather than publishing a wrong one.
    """
    import argparse

    p = argparse.ArgumentParser(
        prog="python3 -m chrony_monitor.tempcomp",
        description="Print the best temperature sensor for tempcomp.")
    p.add_argument(
        "--wait-preferred", type=float, default=0.0, metavar="SECONDS",
        help="Wait up to SECONDS for a real board/SoC sensor to appear before "
             "settling for acpitz or an unrecognized zone. Thermal drivers are "
             "loaded by udev, so at boot the good zone may not exist yet.")
    p.add_argument("--verbose", action="store_true",
                   help="Report the full ranking on stderr.")
    p.add_argument("--list", action="store_true",
                   help="List every candidate sensor (identity, path) and exit.")
    args = p.parse_args(argv)

    if args.list:
        for ident, path in list_temp_sensors().items():
            print(f"{ident}\t{path}")
        return 0

    # The sensor the learning phase chose wins while it exists; at boot its
    # driver may still be loading, so it gets the same wait.
    pin = read_sensor_pin()
    deadline = time.monotonic() + max(0.0, args.wait_preferred)
    while True:
        if pin:
            path = list_temp_sensors().get(pin)
            if path:
                print(path)
                return 0
        ranked = rank_temp_sensors()
        if args.verbose:
            for prio, path in ranked:
                print(f"{prio:4d}  {path}  ({_zone_type_of(path) or 'unknown'})",
                      file=sys.stderr)
        if ranked and ranked[0][0] <= PREFERRED_PRIORITY and not pin:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    if pin:
        print(f"warning: chosen sensor {pin} not found; using the ranking",
              file=sys.stderr)
    if not ranked:
        print("no usable thermal zone found", file=sys.stderr)
        return 1
    prio, path = ranked[0]
    if prio > PREFERRED_PRIORITY:
        print(f"warning: settling for a fallback sensor ({path}, "
              f"{_zone_type_of(path) or 'unknown'}); no "
              f"board/SoC zone appeared", file=sys.stderr)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
