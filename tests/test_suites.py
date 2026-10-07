"""Offline tests for autotest scheduling (no flights)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forkpilot import suites


class OrderTest(unittest.TestCase):
    def test_longest_first(self):
        f = Path(tempfile.mkdtemp()) / "d.json"
        with mock.patch.object(suites, "DURATIONS", f):
            self.assertEqual(suites.longest_first(["a", "b"]), ["a", "b"])     # no file: as given
            suites.record_durations([("a", 0, True, None, 10.0), ("b", 0, True, None, 300.0),
                                     ("c", 0, False, "infra", 999.0)])
            # unknown first, failed runs not recorded, then longest
            self.assertEqual(suites.longest_first(["a", "b", "c"]), ["c", "b", "a"])


# Baselines cached under this fingerprint are hours of flights: if this fails, the hash input
# changed (a scenario, runner.py or sitl.py edit, or the hashing code) and every cached default
# baseline is discarded. Last changed on purpose by the four coverage scenarios (auto_mission_cmds, fence_avoid,
# guided_fast, flow_rangefinder); before it 93600710... (quiet SITL connect), 621a2fba... (mission-upload fix), the Plane batch 650b7e4a... / 5422a665..., and before that 03397cfc... / 1a5a7c63...
DEFAULT_FP = "a6ffcb13d11dd0601d5a9e3e4ce02ca607627dc4904e0e3524fde7b2c1379300"
DEFAULT_FP_TWO = "285f3d7f80c09523c4aefb2492091315bf493ca22b546b855ff4e262a86e2f0d"   # only hover, circle


class FingerprintTest(unittest.TestCase):
    def test_default_scenarios_keep_their_fingerprint(self):
        s = suites.Scenarios(Path("."), "x")
        self.assertEqual(s.fingerprint(None).hex(), DEFAULT_FP)
        self.assertEqual(s.fingerprint(["hover", "circle"]).hex(), DEFAULT_FP_TWO)


if __name__ == "__main__":
    unittest.main()
