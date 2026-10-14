"""Recalibration must fit chronyd's ±10 ppm limit by centering k0 rather than
silently giving up on a curve that only fails because of the crystal's offset."""
import os
import tempfile
import unittest

from chrony_monitor import tempcomp


def parabola(offset, k1, k2, T0=60000):
    """Raw crystal frequency (ppm) vs millidegrees: the crystal's offset plus
    a temperature curve."""
    return lambda t: offset + k1 * (t - T0) + k2 * (t - T0) ** 2


class CenterCompensationTest(unittest.TestCase):
    def test_offset_is_removed_and_span_kept(self):
        temps = [50000 + 10 * i for i in range(2000)]   # 50-70C
        f = parabola(-6.0, 4e-4, 0.0)                    # -10 .. -2 ppm
        T0, k0, k1, k2 = tempcomp._polyfit_quadratic(temps, [f(t) for t in temps])
        new_k0, half_span = tempcomp._center_compensation(temps, T0, k0, k1, k2)
        comps = [new_k0 + k1 * (t - T0) + k2 * (t - T0) ** 2 for t in temps]
        self.assertAlmostEqual(half_span, 4.0, delta=0.05)
        self.assertAlmostEqual(max(comps), -min(comps), delta=0.1)

    def test_rare_extremes_do_not_set_the_span(self):
        temps = [60000] * 1000 + [90000] * 2             # two stray hot minutes
        new_k0, half_span = tempcomp._center_compensation(
            temps, 60000, 0.0, 1e-3, 0.0)
        self.assertEqual(half_span, 0.0)


class RecalibrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = tempcomp.TempCompCollector(
            sensor_path=os.path.join(self.tmp.name, "no-sensor"),
            data_dir=self.tmp.name)
        self.applied = []
        self.c._apply_calibration = lambda *k: self.applied.append(k)

    def tearDown(self):
        self.tmp.cleanup()

    def fill(self, f, lo=45000, hi=83000):
        n = tempcomp.MIN_SAMPLES_FIT * 2
        for i in range(n):
            t = lo + (hi - lo) * i / (n - 1)
            self.c._temps.append(t)
            self.c._freqs.append(f(t))

    def test_offset_curve_is_applied_centered(self):
        # -7 ppm at the vertex, over +12 at the cold end: out of chronyd's
        # range as fitted, inside it once the offset is centered away.
        f = parabola(-7.0, 0.0, 6e-8, T0=63000)
        self.fill(f, hi=79000)
        self.assertGreater(max(f(t) for t in self.c._temps), tempcomp.CHRONY_MAX_COMP)
        self.c._check_recalibration()
        self.assertEqual(len(self.applied), 1)
        T0, k0, k1, k2 = self.applied[0]
        comps = [k0 + k1 * (t - T0) + k2 * (t - T0) ** 2 for t in self.c._temps]
        self.assertLessEqual(max(abs(x) for x in comps), tempcomp.CHRONY_MAX_COMP)

    def test_curve_wider_than_limit_is_skipped_and_logged(self):
        self.fill(parabola(0.0, 7e-4, 0.0))              # 26.6 ppm span
        self.c._check_recalibration()
        self.assertEqual(self.applied, [])
        self.assertEqual(len(self.c.recal_logs), 1)
        self.assertIn("Skipped", self.c.recal_logs[0])
        # Counted as an attempt: no second entry until the cooldown passes
        self.c._check_recalibration()
        self.assertEqual(len(self.c.recal_logs), 1)


class RecordTest(unittest.TestCase):
    def test_reconstruction_holds_compensation_at_chronyd_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            sensor = os.path.join(tmp, "temp")
            c = tempcomp.TempCompCollector(sensor_path=sensor, data_dir=tmp)
            c._config = tempcomp.TempCompConfig(
                sensor_path=sensor, interval=30, T0=60000,
                k0=0.0, k1=1e-3, k2=0.0, is_active=True)
            c.record(75000, 2.0)                         # formula says 15 ppm
            self.assertAlmostEqual(c._minute_freqs[-1], 12.0)


if __name__ == "__main__":
    unittest.main()
