"""Health checks on what chronyd reports with the correction on: a calibration
that isn't flattening the frequency, and collection that has stalled."""
import os
import tempfile
import time
import unittest
from unittest import mock

from types import SimpleNamespace

from chrony_monitor import display, monitor, tempcomp
from chrony_monitor.status import SyncQuality, SyncState

T0 = 40000
RAW_SLOPE = -0.2e-3                     # ppm per millidegree: -0.2 ppm/°C


def raw_at(t):
    return 3.0 + RAW_SLOPE * (t - T0)


class ResidualTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sensor = os.path.join(self.tmp.name, "temp")
        self.c = tempcomp.TempCompCollector(sensor_path=self.sensor,
                                            data_dir=self.tmp.name)
        self.c._start_time = time.time()
        patcher = mock.patch.object(tempcomp, "chrony_sensor_read_failing",
                                    return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def calibrate(self, k1):
        self.c._config = tempcomp.TempCompConfig(
            sensor_path=self.sensor, interval=30, T0=T0,
            k0=0.0, k1=k1, k2=0.0, is_active=True)

    def minutes(self, reported_of, comp_of, n=720):
        """n minutes sweeping 36-44 °C and back, with a little noise."""
        now = int(time.time())
        for i in range(n):
            t = T0 + 4000 * (1 - abs(2 * i / n - 1) * 2)
            noise = 0.01 * ((i * 7919) % 11 - 5) / 5
            self.c._reported.append((now - (n - i) * 60, t,
                                     reported_of(t) + noise, comp_of(t)))

    def comp(self, t):
        cfg = self.c._config
        return cfg.k1 * (t - T0)

    def test_working_calibration_is_quiet(self):
        self.calibrate(-RAW_SLOPE)              # cancels the raw slope
        self.minutes(lambda t: raw_at(t) + self.comp(t), self.comp)
        s = self.c.get_status()
        self.assertAlmostEqual(s.residual_slope, 0.0, places=2)
        self.assertGreater(s.wander_reduction, 0.8)
        self.assertIsNone(s.warning)

    def test_reversed_calibration_warns(self):
        self.calibrate(RAW_SLOPE)               # doubles the raw slope
        self.minutes(lambda t: raw_at(t) + self.comp(t), self.comp)
        s = self.c.get_status()
        self.assertAlmostEqual(s.residual_slope, -0.4, places=2)
        self.assertLess(s.wander_reduction, 0)
        self.assertEqual(s.warning, "freq still tracks temp!")

    def test_calibration_not_in_effect_warns(self):
        # chronyd applies nothing, but the monitor believes it does
        self.calibrate(-RAW_SLOPE)
        self.minutes(raw_at, self.comp)
        s = self.c.get_status()
        self.assertAlmostEqual(s.residual_slope, -0.2, places=2)
        self.assertEqual(s.warning, "freq still tracks temp!")

    def test_calibration_adding_wander_warns(self):
        # A curve where the drift is flat (a sensor that doesn't follow the
        # crystal, say): no linear slope left, but more wander than without it
        self.c._config = tempcomp.TempCompConfig(
            sensor_path=self.sensor, interval=30, T0=T0,
            k0=0.0, k1=0.0, k2=1e-7, is_active=True)
        comp = lambda t: 1e-7 * (t - T0) ** 2
        self.minutes(lambda t: 3.0 + comp(t), comp)
        s = self.c.get_status()
        self.assertLess(abs(s.residual_slope), 0.05)
        self.assertLess(s.wander_reduction, 0)
        self.assertEqual(s.warning, "comp adds wander!")

    def test_too_little_temperature_range_is_not_judged(self):
        self.calibrate(RAW_SLOPE)
        now = int(time.time())
        for i in range(720):
            self.c._reported.append((now - i * 60, T0 + 500 * (i % 2), 3.0, 0.0))
        s = self.c.get_status()
        self.assertIsNone(s.residual_slope)
        self.assertIsNone(s.warning)

    def test_no_active_calibration_is_not_judged(self):
        self.minutes(raw_at, lambda t: 0.0)
        self.assertIsNone(self.c.get_status().residual_slope)

    def test_reported_minutes_persist_until_a_recalibration(self):
        self.c._append_reported(int(time.time()) - 60, T0, 3.0, 0.5)
        again = tempcomp.TempCompCollector(sensor_path=self.sensor,
                                           data_dir=self.tmp.name)
        again._load_reported()
        self.assertEqual(len(again._reported), 1)
        again._last_recal_time = time.time()
        again._reported.clear()
        again._load_reported()                  # older than the recalibration
        self.assertEqual(len(again._reported), 0)
        self.c._reset_reported()
        self.assertFalse(os.path.exists(self.c._reported_path))

    def test_metrics_are_displayed(self):
        s = tempcomp.TempCompStatus(sample_count=10, collection_duration_s=600,
                                    residual_slope=-0.195, residual_r2=0.84,
                                    wander_reduction=0.45)
        lines = display.format_tempcomp_lines(s)
        self.assertEqual(lines[1:], ("uncorrected: 10 min over 10m",
                                     "corrected: wander -45%, slope -0.20ppm/C R2=0.84"))


class StallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = tempcomp.TempCompCollector(
            sensor_path=os.path.join(self.tmp.name, "temp"),
            data_dir=self.tmp.name, auto_recal=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_samples_for_hours_warns(self):
        self.c._start_time = time.time() - 7 * 3600
        self.c._last_sample_time = time.time() - 30 * 86400
        self.assertEqual(self.c.get_status().warning, "no new samples for 7h")

    def test_recent_sample_is_quiet(self):
        self.c._start_time = time.time() - 7 * 3600
        self.c._last_sample_time = time.time() - 600
        self.assertIsNone(self.c.get_status().warning)

    def feed(self, minutes, freq=3.0, trusted=True):
        for _ in range(minutes * 60):
            self.c.record(T0, freq, trusted=trusted)
            self.clock[0] += 1.0

    def _clock(self):
        self.clock = [1790000000.0]
        patcher = mock.patch.object(tempcomp.time, "time", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_step_is_kept_from_its_first_minute(self):
        # No fit gets to veto a minute: a real step in frequency is data.
        self._clock()
        self.feed(400)
        self.feed(5, freq=6.0)
        self.assertEqual(len(self.c._temps), 405)
        self.assertEqual(list(self.c._freqs)[-5:], [6.0] * 5)

    def test_lost_lock_drops_minutes_until_settled(self):
        self._clock()
        self.feed(10)
        self.feed(1, trusted=False)
        self.feed(20)
        # the untrusted minute and the SETTLE_S after it are dropped
        self.assertEqual(len(self.c._temps), 10 + 20 - tempcomp.SETTLE_S // 60)
        self.assertEqual(len(self.c._reported), len(self.c._temps))

    def test_chronyd_restart_drops_minutes_until_settled(self):
        self._clock()
        started = self.clock[0]
        with mock.patch.object(tempcomp, "chronyd_age",
                               lambda: self.clock[0] - started):
            self.feed(20)
        self.assertEqual(len(self.c._temps), 20 - tempcomp.SETTLE_S // 60)


class TrustedTest(unittest.TestCase):
    def status(self, state=SyncState.GPPS_LOCKED, pps=True, skew=0.04,
               quality=SyncQuality.EXCELLENT):
        return SimpleNamespace(sync_state=state, pps_expected=pps, sync_quality=quality,
                               tracking=SimpleNamespace(skew_ppm=skew))

    def test_locked(self):
        self.assertTrue(monitor.tempcomp_trusted(self.status(), False))

    def test_pps_station_on_ntp_fallback_is_not(self):
        self.assertFalse(monitor.tempcomp_trusted(
            self.status(SyncState.NTP_SYNCED), False))

    def test_ntp_only_station_on_ntp_is(self):
        self.assertTrue(monitor.tempcomp_trusted(
            self.status(SyncState.NTP_SYNCED, pps=False), False))

    def test_recovering_or_unsure_is_not(self):
        self.assertFalse(monitor.tempcomp_trusted(self.status(), True))
        self.assertFalse(monitor.tempcomp_trusted(self.status(skew=2.0), False))
        self.assertFalse(monitor.tempcomp_trusted(
            self.status(quality=SyncQuality.DEGRADED), False))


if __name__ == "__main__":
    unittest.main()
