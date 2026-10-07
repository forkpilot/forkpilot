"""Noise tolerances between builds (calibrate, oracle.noise_tol) and the low-confidence tag of a culprit."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forkpilot import calibrate, oracle
from forkpilot.investigate import caution


def week(root: Path, name: str, values: list[float], triage: float | None = None) -> str:
    d = root / name
    (d / "bad").mkdir(parents=True)
    for rep, v in enumerate(values):
        (d / "bad" / f"plane_mission.{rep}.metrics.json").write_text(json.dumps({"landing_offset_m": v}))
    if triage is not None:      # triage reruns (rep >= 100) are not part of the week's mean
        (d / "bad" / "plane_mission.100.metrics.json").write_text(json.dumps({"landing_offset_m": triage}))
    return str(d)


class CalibrateTest(unittest.TestCase):
    def test_between_builds_skips_localized_and_triage(self):
        root = Path(tempfile.mkdtemp())
        rows = [
            {"vehicle": "plane", "good": "a", "bad": "b", "outcome": "no_regression",
             "investigation": week(root, "1", [7.7, 7.7, 7.7])},
            {"vehicle": "plane", "good": "b", "bad": "c", "outcome": "no_regression",
             "investigation": week(root, "2", [10.4, 10.4, 10.4], triage=99.0)},
            # a localized range may be a real change: its difference is not noise
            {"vehicle": "plane", "good": "c", "bad": "d", "outcome": "localized",
             "investigation": week(root, "3", [30.0, 30.0, 30.0])},
        ]
        diffs = calibrate.between_builds(rows)
        self.assertEqual([round(x, 3) for x in diffs[("plane_mission", "landing_offset_m")]], [2.7])
        tol = calibrate.table(diffs, min_pairs=1)
        self.assertAlmostEqual(tol["plane_mission"]["landing_offset_m"], 2.7 * calibrate.FACTOR, places=3)
        self.assertEqual(calibrate.table(diffs, min_pairs=2), {})


class NoiseTolTest(unittest.TestCase):
    TABLE = {"plane_mission": {"landing_offset_m": 4.0}}

    def setUp(self):
        p = mock.patch.object(oracle, "_noise", lambda: self.TABLE)
        p.start()
        self.addCleanup(p.stop)

    def test_lookup(self):
        self.assertEqual(oracle.noise_tol("plane_mission", "landing_offset_m"), 4.0)
        self.assertEqual(oracle.noise_tol("plane_mission__X_1", "landing_offset_m"), 4.0)
        self.assertEqual(oracle.noise_tol("plane_mission", "other"), 0.05)
        self.assertEqual(oracle.noise_tol(None, "landing_offset_m"), 0.05)

    def test_cluster_jump_is_noise_but_a_bigger_shift_is_not(self):
        base = [{"completed": 1, "landing_offset_m": 7.7}] * 5
        self.assertEqual(oracle.verdict([{"completed": 1, "landing_offset_m": 10.4}] * 3, {}, base, "plane_mission")[0], "PASS")
        self.assertEqual(oracle.verdict([{"completed": 1, "landing_offset_m": 10.4}] * 3, {}, base)[0], "DRIFT")
        self.assertEqual(oracle.verdict([{"completed": 1, "landing_offset_m": 13.0}] * 3, {}, base, "plane_mission")[0], "DRIFT")


class NoiseFileTest(unittest.TestCase):
    def setUp(self):
        self.f = Path(tempfile.mkdtemp()) / "noise.json"
        p = mock.patch.object(oracle, "NOISE_FILE", self.f)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(oracle._noise.cache_clear)
        oracle._noise.cache_clear()

    def test_aa_raises_backfill_and_survives_write(self):
        self.f.write_text(json.dumps({"tolerance": {"hover": {"flight_time_s": 0.2}},
                                      "aa": {"hover": {"flight_time_s": 0.1, "x_m": 0.4},
                                             "guided_fast": {"landing_offset_m": 0.4}},
                                      "aa_source": "A/A"}))
        self.assertEqual(oracle.noise_tol("hover", "flight_time_s"), 0.2)
        self.assertEqual(oracle.noise_tol("hover", "x_m"), 0.4)
        self.assertEqual(oracle.noise_tol("guided_fast", "landing_offset_m"), 0.4)
        bf = self.f.with_name("backfill.jsonl")
        bf.write_text("")
        calibrate.main(bf, write=True, log=lambda *a: None)
        data = json.loads(self.f.read_text())
        self.assertEqual((data["tolerance"], data["aa_source"]), ({}, "A/A"))
        self.assertEqual(oracle.noise_tol("guided_fast", "landing_offset_m"), 0.4)


class CautionTest(unittest.TestCase):
    def test_sim_only(self):
        self.assertEqual(caution(["libraries/SITL/SIM_RF_X.cpp", "libraries/SITL/SIM_config.h"], "plane"), "sim")
        self.assertEqual(caution(["libraries/AP_HAL_SITL/sitl_airspeed.cpp"], "plane"), "sim")
        self.assertEqual(caution(["Tools/autotest/arduplane.py"], "copter"), "sim")

    def test_other_vehicle(self):
        self.assertEqual(caution(["ArduCopter/mode_acro.cpp"], "plane"), "other_vehicle")
        self.assertEqual(caution(["ArduCopter/mode_acro.cpp", "libraries/SITL/SIM_Frame.cpp"], "plane"),
                         "other_vehicle")

    def test_firmware_change_has_no_caution(self):
        self.assertIsNone(caution(["ArduPlane/Attitude.cpp"], "plane"))
        self.assertIsNone(caution(["libraries/AP_Compass/AP_Compass.cpp", "libraries/AP_Compass/AP_Compass_SITL.cpp"],
                                  "plane"))
        self.assertIsNone(caution(["ArduCopter/mode_acro.cpp"], "copter"))
        # unbuildable commits share the blame: their files are not known to be safe
        self.assertIsNone(caution(["libraries/SITL/SIM_X.cpp"], "plane", ambiguous=["abc"]))


if __name__ == "__main__":
    unittest.main()
