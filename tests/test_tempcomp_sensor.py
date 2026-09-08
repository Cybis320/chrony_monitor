"""The tempcomp sensor node must be one chronyd can read under AppArmor, and
its identity must follow the zone type whichever node form is used."""
import os
import tempfile
import unittest

from chrony_monitor import tempcomp


class ThermalFixture:
    """A fake /sys/class/thermal tree."""

    def __init__(self, base):
        self.base = base

    def zone(self, n, ztype, millideg, hwmon=None):
        zdir = os.path.join(self.base, f"thermal_zone{n}")
        os.makedirs(zdir, exist_ok=True)
        with open(os.path.join(zdir, "type"), "w") as f:
            f.write(ztype + "\n")
        with open(os.path.join(zdir, "temp"), "w") as f:
            f.write(f"{millideg}\n")
        if hwmon is not None:
            hdir = os.path.join(zdir, f"hwmon{hwmon}")
            os.makedirs(hdir, exist_ok=True)
            with open(os.path.join(hdir, "temp1_input"), "w") as f:
                f.write(f"{millideg}\n")
        return zdir


class SensorNodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "thermal")
        os.makedirs(self.base)
        self.fx = ThermalFixture(self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ranking_prefers_hwmon_mirror_of_best_zone(self):
        self.fx.zone(0, "pch_cometlake", 72000, hwmon=1)
        self.fx.zone(1, "iwlwifi_1", 60000, hwmon=3)
        self.fx.zone(2, "x86_pkg_temp", 53000)          # no hwmon mirror
        self.fx.zone(3, "acpitz", 0)                     # implausible, dropped
        ranked = tempcomp.rank_temp_sensors(self.base)
        paths = [p for _, p in ranked]
        self.assertEqual(paths[0],
                         os.path.join(self.base, "thermal_zone0", "hwmon1", "temp1_input"))
        self.assertIn(os.path.join(self.base, "thermal_zone2", "temp"), paths)
        self.assertEqual(len(ranked), 3)
        self.assertEqual(paths[-1],
                         os.path.join(self.base, "thermal_zone1", "hwmon3", "temp1_input"))

    def test_identity_is_zone_type_for_both_node_forms(self):
        zdir = self.fx.zone(5, "pch_cometlake", 50000, hwmon=2)
        self.assertEqual(
            tempcomp.sensor_identity(os.path.join(zdir, "temp"), self.base),
            "type:pch_cometlake")
        self.assertEqual(
            tempcomp.sensor_identity(os.path.join(zdir, "hwmon2", "temp1_input"), self.base),
            "type:pch_cometlake")

    def test_identity_follows_stable_link(self):
        zdir = self.fx.zone(0, "cpu-thermal", 45000, hwmon=0)
        link = os.path.join(self.tmp.name, "tempcomp-sensor")
        os.symlink(os.path.join(zdir, "hwmon0", "temp1_input"), link)
        self.assertEqual(tempcomp.sensor_identity(link, self.base), "type:cpu-thermal")

    def test_non_zone_path_keeps_path_identity(self):
        path = os.path.join(self.tmp.name, "hwmon9", "temp1_input")
        self.assertEqual(tempcomp.sensor_identity(path, self.base), "path:" + path)

    def test_zone_sensor_path_falls_back_to_temp(self):
        zdir = self.fx.zone(2, "x86_pkg_temp", 53000)
        self.assertEqual(tempcomp.zone_sensor_path(zdir), os.path.join(zdir, "temp"))


if __name__ == "__main__":
    unittest.main()
