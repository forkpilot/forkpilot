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
# baseline is discarded. Last changed on purpose by the mission-upload fix (runner: wall-clock deadline); before it
# the Plane batch 650b7e4a... / 5422a665..., and before that 03397cfc... / 1a5a7c63...
DEFAULT_FP = "621a2fbaaaae1ad5a14bab4ebee9bb438266e40167aa5370f1f160dcce618daf"
DEFAULT_FP_TWO = "794a6dce62dbccffddd504cf0c2b8a661f87e9de9afb8ead9a9950c1c3d36880"   # only hover, circle


class FingerprintTest(unittest.TestCase):
    def test_default_scenarios_keep_their_fingerprint(self):
        s = suites.Scenarios(Path("."), "x")
        self.assertEqual(s.fingerprint(None).hex(), DEFAULT_FP)
        self.assertEqual(s.fingerprint(["hover", "circle"]).hex(), DEFAULT_FP_TWO)


if __name__ == "__main__":
    unittest.main()
