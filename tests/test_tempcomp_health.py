"""Health checks on what chronyd reports with the correction on: a calibration
that isn't flattening the frequency, and collection that has stalled."""
import os
import tempfile
import time
import unittest
from unittest import mock

from chrony_monitor import display, tempcomp

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
        _, line2 = display.format_tempcomp_lines(s)
        self.assertEqual(line2, "10 samples (10m) | after -0.20ppm/C R2=0.84 "
                                "| wander -45%")


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

    def test_lasting_step_is_followed_not_rejected(self):
        # A tight fit at 3 ppm; the frequency then steps to 6 ppm for good
        for i in range(400):
            self.c._temps.append(T0 + 100 * (i % 20))
            self.c._freqs.append(3.0 + 0.001 * (i % 3))
            self.c._timestamps.append(i)
        self.c._fit = (T0, 3.0, 0.0, 0.0)
        self.c._fit_residual_std = 0.001
        self.c._fit_time = time.time()          # don't refit during the test
        before = len(self.c._temps)
        minutes = tempcomp.OUTLIER_STREAK_LIMIT + 5
        for _ in range(minutes * 60):
            self.c.record(T0 + 500, 6.0)
        self.assertEqual(len(self.c._temps) - before, 5)
        self.assertEqual(len(self.c._reported), minutes)


if __name__ == "__main__":
    unittest.main()
