"""Temperature compensation calibration for chrony."""

import logging
import math
import os
import re
import subprocess
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

# Auto-recalibration safeguards
MIN_RECAL_INTERVAL = 86400      # 24 hours between recalibrations
MIN_IMPROVEMENT = 0.3           # New fit must reduce residual slope by 30%
CONF_PATHS = ["/etc/chrony/chrony.conf", "/etc/chrony.conf"]


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


def read_temperature(sensor_path: str) -> Optional[float]:
    """Read temperature from sysfs sensor. Returns millidegrees or None."""
    try:
        with open(sensor_path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


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


class TempCompCollector:
    """Collects temperature/frequency data for tempcomp calibration."""

    def __init__(self, sensor_path: str = "/sys/class/thermal/thermal_zone0/temp",
                 data_dir: str = None, auto_recal: bool = True):
        self.sensor_path = sensor_path
        self.auto_recal = auto_recal
        if data_dir is None:
            data_dir = os.path.join(
                os.path.expanduser("~"), ".local", "share", "chrony-monitor"
            )
        self._data_dir = data_dir
        self._csv_path = os.path.join(data_dir, "tempcomp.csv")
        self._recal_log_path = os.path.join(data_dir, "recalibrations.log")

        self._temps = deque(maxlen=MAX_SAMPLES)
        self._freqs = deque(maxlen=MAX_SAMPLES)
        self._timestamps = deque(maxlen=MAX_SAMPLES)

        # Minute bucketing
        self._minute_temps = []
        self._minute_freqs = []
        self._minute_count = 0

        self._config: Optional[TempCompConfig] = None
        self._start_time: float = 0
        self._last_recal_time: float = 0
        self._last_attempt_time: float = 0
        self._last_prune_time: float = 0
        self._cal_range_path = os.path.join(data_dir, "cal_range")
        self._cal_range: Optional[tuple] = None  # (min_C, max_C) from last calibration

        # Cached correlation and fit
        self._cached_correlation: Optional[float] = None
        self._correlation_time: float = 0
        self._fit: Optional[tuple] = None    # (T0, k0, k1, k2)
        self._fit_residual_std: float = 0.0  # σ of residuals
        self._fit_time: float = 0

        # Recalibration logs (shown in display)
        self.recal_logs: list = []

    def _update_fit(self):
        """Recompute the quadratic fit and residual std dev. Called periodically."""
        now = time.time()
        if now - self._fit_time < 300:  # refit every 5 min at most
            return
        temps_md, freqs = _filter_outliers(list(self._temps), list(self._freqs))
        if len(freqs) < MIN_SAMPLES_CORRELATION:
            return
        T0, k0, k1, k2 = _polyfit_quadratic(temps_md, freqs)
        residuals = [f - (k0 + k1 * (t - T0) + k2 * (t - T0) ** 2)
                     for t, f in zip(temps_md, freqs)]
        n = len(residuals)
        self._fit = (T0, k0, k1, k2)
        self._fit_residual_std = math.sqrt(sum(r * r for r in residuals) / n)
        self._fit_time = now

    def _is_outlier(self, temp_millideg: float, freq_ppm: float) -> bool:
        """Check if a sample is an outlier based on the current fit."""
        if self._fit is None:
            return False  # no fit yet, accept everything
        T0, k0, k1, k2 = self._fit
        predicted = k0 + k1 * (temp_millideg - T0) + k2 * (temp_millideg - T0) ** 2
        residual = abs(freq_ppm - predicted)
        # Reject if residual > 3σ (99.7% of good data passes)
        return residual > 3.0 * self._fit_residual_std if self._fit_residual_std > 0 else False

    def load(self):
        """Load persistent data and parse chrony.conf. Call once at startup."""
        self._start_time = time.time()
        self._config = parse_chrony_tempcomp()
        self._load_csv()
        self._load_recal_time()
        self._load_cal_range()
        self._update_fit()  # build initial fit from loaded data

    def record(self, temp_millideg: float, frequency_ppm: float):
        """Record a (temp, freq) pair. Called every 1s from the monitor loop.

        If tempcomp is active, reconstructs the raw crystal frequency by
        adding back the current compensation. This allows calibration data
        collection while tempcomp is running.
        """
        # Reconstruct raw frequency if tempcomp is active
        raw_freq = frequency_ppm
        if self._config and self._config.is_active:
            raw_freq = frequency_ppm + _compute_compensation(self._config, temp_millideg)

        self._minute_temps.append(temp_millideg)
        self._minute_freqs.append(raw_freq)
        self._minute_count += 1

        if self._minute_count >= 60:
            avg_temp = sum(self._minute_temps) / len(self._minute_temps)
            avg_freq = sum(self._minute_freqs) / len(self._minute_freqs)
            self._minute_temps.clear()
            self._minute_freqs.clear()
            self._minute_count = 0

            # Reject outliers based on current fit
            if self._is_outlier(avg_temp, avg_freq):
                return

            ts = int(time.time())
            self._temps.append(avg_temp)
            self._freqs.append(avg_freq)
            self._timestamps.append(ts)

            self._append_csv(ts, avg_temp, avg_freq)
            self._cached_correlation = None
            self._update_fit()

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
        if temp is not None:
            status.current_temp_c = temp / 1000.0

        status.config = self._config
        status.sample_count = len(self._temps)
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

        # Extrapolation check
        if self._config and self._config.is_active and status.current_temp_c is not None:
            cal_range = self._get_calibration_range()
            if cal_range:
                cal_min, cal_max = cal_range
                status.cal_range = cal_range
                if status.current_temp_c < cal_min - 1.0 or status.current_temp_c > cal_max + 1.0:
                    status.is_extrapolating = True

        if self._last_recal_time > 0:
            ago = int(time.time() - self._last_recal_time)
            if ago < 3600:
                status.last_recal = f"{ago // 60}m ago"
            elif ago < 86400:
                status.last_recal = f"{ago // 3600}h ago"
            else:
                status.last_recal = f"{ago // 86400}d ago"

        return status

    def _check_recalibration(self):
        """Check if recalibration is warranted and apply if so."""
        # Enforce minimum interval between recalibrations (also covers failed attempts)
        now = time.time()
        if self._last_attempt_time > 0 and now - self._last_attempt_time < MIN_RECAL_INTERVAL:
            return

        # Need enough data
        if len(self._temps) < MIN_SAMPLES_FIT:
            return

        temps_c = [t / 1000.0 for t in self._temps]
        temp_range = max(temps_c) - min(temps_c)
        if temp_range < MIN_TEMP_RANGE_FIT:
            return

        # Compute new fit from raw data (outliers filtered)
        temps_md, freqs = _filter_outliers(list(self._temps), list(self._freqs))
        if len(freqs) < MIN_SAMPLES_FIT:
            return
        new_T0, new_k0, new_k1, new_k2 = _polyfit_quadratic(temps_md, freqs)

        # Check compensation value is within chrony's ±10 ppm limit
        for t in temps_md:
            comp = new_k0 + new_k1 * (t - new_T0) + new_k2 * (t - new_T0) ** 2
            if abs(comp) > 10.0:
                return  # coefficients would be rejected by chrony

        if self._config and self._config.is_active:
            # Compare residual slope: current vs proposed
            # Simulate what the residual frequency would be with each compensation
            current_residuals = []
            new_residuals = []
            for t, f in zip(temps_md, freqs):
                current_comp = _compute_compensation(self._config, t)
                new_comp = new_k0 + new_k1 * (t - new_T0) + new_k2 * (t - new_T0) ** 2
                current_residuals.append(f - current_comp)
                new_residuals.append(f - new_comp)

            current_slope = abs(_residual_slope(temps_c, current_residuals))
            new_slope = abs(_residual_slope(temps_c, new_residuals))

            # Only recalibrate if significant improvement
            if current_slope < 0.001:
                return  # current calibration is already excellent
            improvement = 1.0 - (new_slope / current_slope) if current_slope > 0 else 0
            if improvement < MIN_IMPROVEMENT:
                return
        else:
            # No active tempcomp — check if quadratic fit explains enough variance
            predicted = [new_k0 + new_k1 * (t - new_T0) + new_k2 * (t - new_T0) ** 2
                         for t in temps_md]
            ss_res = sum((f - p) ** 2 for f, p in zip(freqs, predicted))
            mean_f = sum(freqs) / len(freqs)
            ss_tot = sum((f - mean_f) ** 2 for f in freqs)
            r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0
            if r_squared < 0.6:
                return  # temperature doesn't explain enough frequency variance

        # Apply the new calibration
        self._apply_calibration(new_T0, new_k0, new_k1, new_k2)

    def _apply_calibration(self, T0: float, k0: float, k1: float, k2: float):
        """Write proposed tempcomp and apply via the helper script."""
        self._last_attempt_time = time.time()
        sensor = self.sensor_path
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

        # Find the helper script (installed alongside the package or in scripts/)
        script = None
        for candidate in [
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "apply-tempcomp.sh"),
            "/usr/local/bin/apply-tempcomp.sh",
            os.path.join(self._data_dir, "apply-tempcomp.sh"),
        ]:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                script = candidate
                break

        if not script:
            self._log_recal("Failed: apply-tempcomp.sh not found")
            return

        # Apply via sudo
        try:
            proc = subprocess.run(
                ["sudo", "-n", script, proposed_path],
                capture_output=True, text=True, timeout=30
            )
            if proc.returncode != 0:
                self._log_recal(f"Failed: {proc.stderr.strip()}")
                return
        except (subprocess.TimeoutExpired, OSError) as e:
            self._log_recal(f"Failed: {e}")
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
        self._log_recal(f"Recalibrated: T0={T0:.0f} k1={k1:.6e} range={cal_min:.0f}-{cal_max:.0f}C")

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

    def _load_recal_time(self):
        """Load the last recalibration timestamp from the log file."""
        if not os.path.exists(self._recal_log_path):
            return
        try:
            with open(self._recal_log_path) as f:
                for line in f:
                    if 'Recalibrated:' in line:
                        # Extract timestamp from [YYYY-MM-DD HH:MM:SS]
                        match = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
                        if match:
                            import calendar
                            t = time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                            self._last_recal_time = calendar.timegm(t)
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
        # Fallback: estimate ±10°C around T0
        T0_c = self._config.T0 / 1000.0
        return (T0_c - 10.0, T0_c + 10.0)

    def _load_csv(self):
        """Read existing CSV into deques. Prune old entries and outliers."""
        if not os.path.exists(self._csv_path):
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

        # Rewrite file if we pruned anything
        if len(rows_to_keep) < len(raw_rows):
            self._rewrite_csv(rows_to_keep)

    def _append_csv(self, ts: int, temp: float, freq: float):
        """Append one row to the CSV file."""
        os.makedirs(self._data_dir, exist_ok=True)
        try:
            with open(self._csv_path, 'a') as f:
                f.write(f"{ts},{temp:.0f},{freq:.6f}\n")
        except OSError:
            pass

    def _rewrite_csv(self, rows: list):
        """Atomically rewrite the CSV file (for pruning)."""
        tmp_path = self._csv_path + '.tmp'
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(tmp_path, 'w') as f:
                for ts, temp, freq in rows:
                    f.write(f"{ts},{temp:.0f},{freq:.6f}\n")
            os.replace(tmp_path, self._csv_path)
        except OSError:
            pass
