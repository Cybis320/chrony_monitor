"""Recalibration: chronyd reports raw + comp, so a calibration must be the
negated raw curve, centered to fit chronyd's ±10 ppm limit; and data recorded
with the sign reversed must be repaired before anything is fitted to it."""
import os
import tempfile
import time
import unittest
from unittest import mock

from chrony_monitor import tempcomp


def parabola(offset, k1, k2, T0=60000):
    """Raw crystal frequency (ppm) vs millidegrees: the crystal's offset plus
    a temperature curve."""
    return lambda t: offset + k1 * (t - T0) + k2 * (t - T0) ** 2


def comp_of(T0, k0, k1, k2):
    return lambda t: k0 + k1 * (t - T0) + k2 * (t - T0) ** 2


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
        self.sensor = os.path.join(self.tmp.name, "no-sensor")
        self.c = tempcomp.TempCompCollector(
            sensor_path=self.sensor, data_dir=self.tmp.name)
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

    def activate(self, T0, k0, k1, k2):
        self.c._config = tempcomp.TempCompConfig(
            sensor_path=self.sensor, interval=30, T0=T0,
            k0=k0, k1=k1, k2=k2, is_active=True)

    def test_applied_compensation_flattens_reported_frequency(self):
        # -7 ppm at the vertex, over +12 at the cold end: its negation is out
        # of chronyd's range as fitted, inside it once centered.
        f = parabola(-7.0, 0.0, 6e-8, T0=63000)
        self.fill(f, hi=79000)
        self.assertGreater(max(abs(f(t)) for t in self.c._temps),
                           tempcomp.CHRONY_MAX_COMP)
        self.c._check_recalibration()
        self.assertEqual(len(self.applied), 1)
        comp = comp_of(*self.applied[0])
        reported = [f(t) + comp(t) for t in self.c._temps]
        self.assertLess(max(reported) - min(reported), 0.01)
        self.assertLessEqual(max(abs(comp(t)) for t in self.c._temps),
                             tempcomp.CHRONY_MAX_COMP)

    def test_curve_wider_than_limit_is_skipped_and_logged(self):
        self.fill(parabola(0.0, 7e-4, 0.0))              # 26.6 ppm span
        self.c._check_recalibration()
        self.assertEqual(self.applied, [])
        self.assertEqual(len(self.c.recal_logs), 1)
        self.assertIn("Skipped", self.c.recal_logs[0])
        # Counted as an attempt: no second entry until the cooldown passes
        self.c._check_recalibration()
        self.assertEqual(len(self.c.recal_logs), 1)

    def test_reversed_calibration_is_replaced(self):
        self.fill(parabola(0.0, -3e-4, 0.0))
        self.activate(60000, 0.0, -3e-4, 0.0)            # doubles the slope
        self.c._check_recalibration()
        self.assertEqual(len(self.applied), 1)

    def test_correct_calibration_is_kept(self):
        self.fill(parabola(0.0, -3e-4, 0.0))
        self.activate(60000, 0.0, 3e-4, 0.0)             # cancels it exactly
        self.c._check_recalibration()
        self.assertEqual(self.applied, [])

    def test_reversed_calibration_skips_cooldown(self):
        self.fill(parabola(0.0, -3e-4, 0.0))
        self.activate(60000, 0.0, -3e-4, 0.0)
        self.c._last_recal_time = time.time() - 60
        self.c._check_recalibration()
        self.assertEqual(self.applied, [])
        open(self.c._cal_reversed_path, 'w').close()
        self.c._check_recalibration()
        self.assertEqual(len(self.applied), 1)


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sensor = os.path.join(self.tmp.name, "temp")
        self.c = tempcomp.TempCompCollector(sensor_path=sensor,
                                            data_dir=self.tmp.name)
        self.c._config = tempcomp.TempCompConfig(
            sensor_path=sensor, interval=30, T0=60000,
            k0=0.0, k1=1e-4, k2=0.0, is_active=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_is_reported_minus_compensation(self):
        self.c.record(70000, 2.0)                        # comp 1 ppm
        self.assertAlmostEqual(self.c._minute_freqs[-1], 1.0)

    def test_compensation_held_at_chronyd_limit(self):
        self.c.record(210000, 2.0)                       # formula says 15 ppm
        self.assertAlmostEqual(self.c._minute_freqs[-1], -8.0)

    def test_no_compensation_while_chronyd_cannot_read_sensor(self):
        self.c._read_failing = True
        self.c.record(70000, 2.0)
        self.assertAlmostEqual(self.c._minute_freqs[-1], 2.0)


class RepairFixture(unittest.TestCase):
    """A data dir, chrony.conf and a stubbed journal for the repair tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.sensor = os.path.join(self.dir, "temp")
        self.conf = os.path.join(self.dir, "chrony.conf")
        self.now = time.time()
        # The journal covers every row and chronyd never failed a read
        self.journal = (0.0, [])
        patcher = mock.patch.object(tempcomp, "chrony_sensor_read_failures",
                                    side_effect=lambda since: self.journal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text)

    def recal(self, *ages_days):
        self.write("recalibrations.log", "".join(
            time.strftime("[%Y-%m-%d %H:%M:%S] Recalibrated: x\n",
                          time.localtime(self.now - d * 86400))
            for d in ages_days))

    def collector(self):
        c = tempcomp.TempCompCollector(sensor_path=self.sensor,
                                       data_dir=self.dir)
        c._config = tempcomp.parse_chrony_tempcomp([self.conf])
        c._load_csv()
        return c

    def rows(self, *rows):
        self.write("tempcomp.csv", "".join(
            f"{int(self.now - d * 86400)},{t},{f}\n" for d, t, f in rows))



class RepairTest(RepairFixture):
    """Rows recorded before CSV_FORMAT 2 hold raw + 2*comp, or raw + comp
    while chronyd couldn't read its sensor."""

    def test_rows_repaired_by_the_calibration_active_when_recorded(self):
        self.write("chrony.conf",
                   f"tempcomp {self.sensor} 30 60000 1.0 0.0001 0.0\n")
        self.write("chrony.conf.bak",
                   f"tempcomp {self.sensor} 30 60000 -2.0 0.0 0.0\n")
        self.recal(3, 1)
        self.rows((5.0, 70000, 9.0),     # before the previous recal: unknown
                  (2.0, 70000, 9.0),     # under .bak: comp -2
                  (0.5, 70000, 9.0))     # under chrony.conf: comp 2
        c = self.collector()
        self.assertEqual(list(c._freqs), [13.0, 5.0])
        self.assertTrue(os.path.exists(c._cal_reversed_path))
        # Done once: the rewritten file loads as is
        self.assertEqual(list(self.collector()._freqs), [13.0, 5.0])

    def test_rows_chronyd_could_not_compensate_lose_one_comp(self):
        self.write("chrony.conf",
                   f"tempcomp {self.sensor} 30 60000 1.0 0.0001 0.0\n")
        self.recal(1)
        failing = int(self.now - 0.5 * 86400)
        self.journal = (0.0, [failing - 30, failing])
        self.rows((0.5, 70000, 9.0),     # chronyd failing: applied nothing
                  (0.2, 70000, 9.0))     # chronyd compensating
        self.assertEqual(list(self.collector()._freqs), [7.0, 5.0])

    def test_rows_before_the_journal_are_dropped(self):
        self.write("chrony.conf",
                   f"tempcomp {self.sensor} 30 60000 1.0 0.0001 0.0\n")
        self.recal(1)
        self.journal = (self.now - 0.3 * 86400, [])
        self.rows((0.5, 70000, 9.0),     # older than the journal: unknown
                  (0.2, 70000, 9.0))
        self.assertEqual(list(self.collector()._freqs), [5.0])

    def test_compensated_rows_dropped_without_a_journal(self):
        self.write("chrony.conf",
                   f"tempcomp {self.sensor} 30 60000 1.0 0.0001 0.0\n")
        self.recal(1)
        self.journal = None
        self.rows((0.5, 70000, 9.0))
        self.assertEqual(list(self.collector()._freqs), [])

    def test_rows_never_compensated_are_kept(self):
        self.write("chrony.conf",
                   f"#tempcomp {self.sensor} 30 60000 1.0 0.0001 0.0\n")
        self.rows((2.0, 70000, 9.0))
        c = self.collector()
        self.assertEqual(list(c._freqs), [9.0])
        self.assertFalse(os.path.exists(c._cal_reversed_path))

    def test_calibration_on_another_sensor_drops_its_rows(self):
        self.write("chrony.conf", "tempcomp /elsewhere 30 60000 1.0 0.0 0.0\n")
        self.recal(1)
        self.rows((0.5, 70000, 9.0))
        self.assertEqual(list(self.collector()._freqs), [])

    def test_new_data_dir_needs_no_repair(self):
        c = self.collector()
        self.assertEqual(c._csv_format(), tempcomp.CSV_FORMAT)


class UndoRepairTest(RepairFixture):
    """Format 2 files repaired by the first version of the repair took 2*comp
    off rows chronyd never compensated, which hold raw - comp."""

    OLD_REPAIR = "Repaired 3 rows recorded with tempcomp's sign reversed; " \
                 "dropped 0 of unknown calibration"
    NEW_REPAIR = OLD_REPAIR + " or compensation"

    def setUp(self):
        super().setUp()
        self.write("csv_format", "2\n")
        # chrony.conf: comp 2 at 70000; .bak: comp -2
        self.write("chrony.conf",
                   f"tempcomp {self.sensor} 30 60000 1.0 0.0001 0.0\n")
        self.write("chrony.conf.bak",
                   f"tempcomp {self.sensor} 30 60000 -2.0 0.0 0.0\n")

    def log(self, *entries):
        self.write("recalibrations.log", "".join(
            time.strftime("[%Y-%m-%d %H:%M:%S] ",
                          time.localtime(self.now - d * 86400)) + msg + "\n"
            for d, msg in entries))

    def failing_at(self, *ages_days):
        self.journal = (0.0, [int(self.now - d * 86400) for d in ages_days])

    def test_uncompensated_rows_get_their_comp_back(self):
        self.log((3, "Recalibrated: x"), (1, self.OLD_REPAIR))
        self.failing_at(2.0, 0.5)
        self.rows((2.0, 70000, 5.0),     # failing, under chrony.conf: +2
                  (1.5, 70000, 5.0),     # compensating: 2*comp was right
                  (0.5, 70000, 5.0))     # recorded after the repair
        c = self.collector()
        self.assertEqual(list(c._freqs), [7.0, 5.0, 5.0])
        self.assertFalse(os.path.exists(c._cal_reversed_path))
        self.assertEqual(c._csv_format(), tempcomp.CSV_FORMAT)
        # Done once
        self.assertEqual(list(self.collector()._freqs), [7.0, 5.0, 5.0])

    def test_corrected_repair_is_left_alone(self):
        self.log((3, "Recalibrated: x"), (1, self.NEW_REPAIR))
        self.failing_at(2.0)
        self.rows((2.0, 70000, 5.0))
        self.assertEqual(list(self.collector()._freqs), [5.0])

    def test_recalibration_since_moved_the_calibration_to_bak(self):
        self.log((3, "Recalibrated: x"), (1, self.OLD_REPAIR),
                 (0.8, "Recalibrated: y"))
        self.failing_at(2.0)
        self.rows((2.0, 70000, 5.0))     # failing, under what is now .bak: -2
        c = self.collector()
        self.assertEqual(list(c._freqs), [3.0])
        # The new calibration was fitted to the uncorrected row
        self.assertTrue(os.path.exists(c._cal_reversed_path))

    def test_rows_under_a_lost_calibration_are_dropped(self):
        self.log((3, "Recalibrated: x"), (1, self.OLD_REPAIR),
                 (0.8, "Recalibrated: y"), (0.6, "Recalibrated: z"))
        self.failing_at(2.0)
        self.rows((2.0, 70000, 5.0),     # failing, calibration unknown
                  (1.5, 70000, 5.0))     # compensating: right either way
        self.assertEqual(list(self.collector()._freqs), [5.0])

    def test_rows_dropped_without_a_journal(self):
        self.log((3, "Recalibrated: x"), (1, self.OLD_REPAIR))
        self.journal = None
        self.rows((2.0, 70000, 5.0),
                  (0.5, 70000, 5.0))     # after the repair: kept
        self.assertEqual(list(self.collector()._freqs), [5.0])


if __name__ == "__main__":
    unittest.main()
