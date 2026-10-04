"""Work dir (FP_HOME) and scenario lookup. Paths are fixed at import, so each case runs a fresh interpreter."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOW = """
import json
from forkpilot import battery, build, investigate, record, suites
print(json.dumps({"work": str(battery.WORK), "builds": str(build.CACHE), "inv": str(record.INVESTIGATIONS),
                  "base": str(investigate.BASELINES), "trees": str(suites.AUTOTEST_TREES),
                  "dur": str(suites.DURATIONS), "scen": str(battery.SCENARIOS),
                  "names": [p.stem for p in battery.scenario_paths()],
                  "hover": str(battery.scenario_file("hover")), "mine": str(battery.scenario_file("mine"))}))
"""


def show(**env) -> dict:
    e = {k: v for k, v in os.environ.items() if k not in ("FP_HOME", "FP_BUILD_JOBS")}
    e.update(env, PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-c", SHOW], env=e, capture_output=True, text=True,
                       cwd=tempfile.gettempdir())
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


class WorkDirTest(unittest.TestCase):
    def test_default_is_the_source_tree(self):
        s = show()
        root = str(ROOT.resolve())
        self.assertEqual(s["work"], root)
        self.assertEqual(s["builds"], f"{root}/builds/cache")
        self.assertEqual(s["inv"], f"{root}/investigations")
        self.assertEqual(s["base"], f"{root}/results/baselines")
        self.assertEqual(s["trees"], f"{root}/builds/autotest")
        self.assertEqual(s["dur"], f"{root}/results/autotest_durations.json")
        self.assertEqual(s["scen"], f"{root}/scenarios")

    def test_fp_home_moves_data_not_scenarios(self):
        home = Path(tempfile.mkdtemp()).resolve()
        s = show(FP_HOME=str(home))
        self.assertEqual(s["work"], str(home))
        for k, sub in (("builds", "builds/cache"), ("inv", "investigations"), ("base", "results/baselines"),
                       ("trees", "builds/autotest"), ("dur", "results/autotest_durations.json")):
            self.assertEqual(s[k], f"{home}/{sub}")
        self.assertEqual(s["scen"], f"{ROOT.resolve()}/scenarios")
        self.assertIn("hover", s["names"])

    def test_own_scenarios_in_fp_home(self):
        home = Path(tempfile.mkdtemp()).resolve()
        (home / "scenarios").mkdir()
        (home / "scenarios" / "mine.yaml").write_text("name: mine\n")
        s = show(FP_HOME=str(home))
        self.assertEqual(s["names"], sorted(set(s["names"])))
        self.assertIn("mine", s["names"])
        self.assertEqual(s["names"].count("hover"), 1)
        self.assertEqual(s["mine"], str(home / "scenarios" / "mine.yaml"))
        self.assertEqual(s["hover"], f"{ROOT.resolve()}/scenarios/hover.yaml")

    def test_same_name_in_both_is_an_error(self):
        # an own scenario must not silently replace a shipped one (cached baselines would be mixed up)
        home = Path(tempfile.mkdtemp()).resolve()
        (home / "scenarios").mkdir()
        (home / "scenarios" / "hover.yaml").write_text("name: hover\n")
        e = {k: v for k, v in os.environ.items() if k not in ("FP_HOME", "FP_BUILD_JOBS")}
        e.update(FP_HOME=str(home), PYTHONPATH=str(ROOT))
        r = subprocess.run([sys.executable, "-c", SHOW], env=e, capture_output=True, text=True,
                           cwd=tempfile.gettempdir())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("'hover' is in two directories", r.stderr)

    def test_default_scenario_list_is_the_shipped_dir(self):
        names = show()["names"]
        shipped = sorted(p.stem for p in (ROOT / "scenarios").glob("*.yaml")
                         if ("vehicle:" not in p.read_text() or "vehicle: copter" in p.read_text())
                         and "autopilot: px4" not in p.read_text())
        self.assertEqual(names, shipped)      # the default vehicle's (copter) scenarios


if __name__ == "__main__":
    unittest.main()
