import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from forkpilot import cli, impact, investigate as inv, scenario, suites, targeted
from forkpilot.config import SCENARIOS
from tests.impact_fixtures import DirGit


def report(case, vehicle):
    r = impact.analyse(DirGit(case), "good", "bad", vehicle)
    return {"impact": r, "coverage": impact.coverage(r, impact.scenario_facts_for(vehicle))}


def loiter_changed():
    """The loiter case as if LOIT_OPTIONS already existed: a template and a variant."""
    r = report("loiter", "copter")
    r["impact"]["params"][0]["status"] = "changed"
    return r


def tmp() -> Path:
    d = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(d.cleanup)
    return Path(d.name)


def param(name="P_X", status="changed", scope="library", default=None, **doc):
    return {"name": name, "key": name, "status": status, "scope": scope, "default": default, "doc": doc}


class ValuesTest(unittest.TestCase):
    def test_bits_flip_from_the_default(self):
        vals, why = targeted.param_values(param(default=0, bitmask=[[0, "A"], [2, "C"]]))
        self.assertIsNone(why)
        self.assertEqual([(v["value"], v["on"]) for v in vals], [(1, True), (4, True)])
        vals, _ = targeted.param_values(param(default=1, bitmask=[[0, "A"], [1, "B"]]))
        self.assertEqual([(v["value"], v["on"]) for v in vals], [(0, False), (3, True)])
        vals, _ = targeted.param_values(param(default=None, bitmask=[[3, "D"]]))
        self.assertEqual([v["value"] for v in vals], [8])          # unknown default: the bit alone

    def test_values_skip_the_default(self):
        vals, _ = targeted.param_values(param(default=0, values=[[0, "Disabled"], [1, "A"], [2, "B"]]))
        self.assertEqual([v["value"] for v in vals], [1, 2])
        vals, _ = targeted.param_values(param(default=None, values=[[0, "Disabled"], [1, "A"]]))
        self.assertEqual([v["value"] for v in vals], [1])
        vals, _ = targeted.param_values(param(default=2, values=[[0, "Off"], [2, "B"]]))
        self.assertEqual([v["value"] for v in vals], [0])           # 0 is not the default here

    def test_not_varied(self):
        self.assertEqual(targeted.param_values(param(range=["0", "1"]))[1], "range")
        self.assertEqual(targeted.param_values(param())[1], "nodoc")
        self.assertEqual(targeted.param_values(param(name=None, bitmask=[[0, "A"]]))[1], "prefix")
        self.assertEqual(targeted.param_values(param(status="removed", bitmask=[[0, "A"]]))[1], "removed")
        self.assertEqual(targeted.param_values(param(default=1, values=[[1, "A"]]))[1], "default_only")


class PlanTest(unittest.TestCase):
    def names(self, p):
        return [i["name"] for i in p["add"]]

    def test_loiter(self):
        p = targeted.plan(report("loiter", "copter"), "copter")
        # LOIT_OPTIONS is new: the good commit refuses to set it, so its baseline could not fly
        self.assertEqual(self.names(p), ["copter_sticks__ZIGZAG"])
        self.assertEqual(p["not_varied"], [{"param": "LOIT_OPTIONS", "why": "new"}])
        self.assertTrue(p["add"][0]["direct"])

    def test_variant_is_a_copy_with_the_parameter(self):
        v = targeted.plan(loiter_changed(), "copter")["add"][1]
        self.assertEqual((v["name"], v["base"], v["value"], v["on"]), ("pilot_sticks__LOIT_OPTIONS_0", "pilot_sticks", 0, False))
        self.assertEqual(v["spec"]["params"], {"LOIT_OPTIONS": 0})
        self.assertEqual(v["spec"]["steps"], scenario.load(SCENARIOS / "pilot_sticks.yaml")[0]["steps"])
        text = "\n".join(targeted.lines({"add": [v], "cap": 12, "dropped": [], "not_varied": [], "no_template": []}))
        self.assertIn("LOIT_OPTIONS=0 (bit 0 off: Enable Coordinated turns) on pilot_sticks", text)

    def test_mis_options(self):
        p = targeted.plan(report("mis_options", "copter"), "copter")
        self.assertEqual(self.names(p), ["auto_mission__MIS_OPTIONS_1", "auto_mission_cmds__MIS_OPTIONS_1",
                                         "auto_mission__MIS_OPTIONS_4", "auto_mission_cmds__MIS_OPTIONS_4"])
        self.assertEqual(p["no_template"], [])

    def test_plane_training(self):
        p = targeted.plan(report("plane_training", "plane"), "plane")
        # ACRO uses the controllers directly; the others only through core code
        self.assertEqual(self.names(p), ["plane_sticks__ACRO", "plane_sticks__FBWB",
                                         "plane_sticks__STABILIZE", "plane_sticks__TRAINING"])
        self.assertEqual({x["why"] for x in p["not_varied"]}, {"range", "new"})
        self.assertIn("MANUAL", p["no_template"])
        self.assertEqual(p["add"][3]["spec"]["vehicle"], "plane")

    def test_cap_and_priority(self):
        r = {"impact": {"modes": {"ZIGZAG": ["x"]}, "params": [
                 param("OLD_OPT", values=[[i, f"v{i}"] for i in range(1, 20)], default=0),
                 param("NEW_OPT", status="new", bitmask=[[0, "A"]], default=0),
                 param("LIB_OPT", bitmask=[[0, "A"], [1, "B"]], default=0),
                 param("VEH_OPT", scope="vehicle", values=[[1, "x"]], default=0)]},
             "coverage": {"modes_not_flown": ["ZIGZAG", "FLOWHOLD"]}}
        p = targeted.plan(r, "copter", cap=6)
        # vehicle parameters first, then library ones taking turns
        self.assertEqual(self.names(p), ["copter_sticks__ZIGZAG", "hover__VEH_OPT_1", "hover__LIB_OPT_1",
                                         "hover__OLD_OPT_1", "hover__LIB_OPT_2", "hover__OLD_OPT_2"])
        self.assertEqual(len(p["dropped"]), 17)
        self.assertIn({"param": "NEW_OPT", "why": "new"}, p["not_varied"])
        self.assertEqual(p["no_template"], ["FLOWHOLD"])
        self.assertEqual(targeted.plan(r, "copter", cap=6), p)            # deterministic

    def test_bases(self):
        facts = [{"name": "a", "modes": {"AUTO"}}, {"name": "b", "modes": {"AUTO", "RTL"}},
                 {"name": "c", "modes": {"RTL"}}, {"name": "hover", "modes": {"GUIDED"}}]
        self.assertEqual(targeted.bases({"AUTO", "RTL"}, facts, "copter"), ["b", "a"])
        self.assertEqual(targeted.bases({"FLIP"}, facts, "copter"), ["hover"])
        # coverage wins over modes; scenarios that ran nothing, or are unknown, are not bases
        self.assertEqual(targeted.bases({"AUTO", "RTL"}, facts, "copter", {"c": 9, "hover": 4, "a": 0, "zz": 99}),
                         ["c", "hover"])
        self.assertEqual(targeted.bases({"AUTO", "RTL"}, facts, "copter", {"a": 0}), ["b", "a"])
        self.assertEqual(targeted.bases({"AUTO", "RTL"}, facts, "copter", {"c": 5}), ["c", "b"])


class WriteTest(unittest.TestCase):
    def test_written_set_lints_and_is_stable(self):
        root = tmp()
        p = targeted.plan(loiter_changed(), "copter")
        d = targeted.write(p, root)
        self.assertEqual(sorted(x.name for x in d.glob("*.yaml")),
                         ["copter_sticks__ZIGZAG.yaml", "fence_avoid__LOIT_OPTIONS_0.yaml",
                          "pilot_sticks__LOIT_OPTIONS_0.yaml"])
        self.assertEqual(targeted.write(p, root), d)
        scenario.check([SCENARIOS, d], log=None)              # no duplicate names, no lint errors
        self.assertIsNone(targeted.write({**p, "add": []}, root))

    def test_every_template_lints(self):
        d = tmp()
        for v, modes in targeted.TEMPLATE_MODES.items():
            for m in modes:
                spec = {"name": targeted.template_name(v, m), **targeted.TEMPLATES[v](m)}
                (d / f"{spec['name']}.yaml").write_text(targeted.yaml_text(spec))
        issues = scenario.lint_paths([d])
        self.assertEqual([str(i) for i in issues], [])

    def test_baseline_key_changes_with_the_targeted_set(self):
        root = tmp()
        d = targeted.write(targeted.plan(report("loiter", "copter"), "copter"), root)
        plain = suites.Scenarios(Path("."), "g", [SCENARIOS])
        more = suites.Scenarios(Path("."), "g", [SCENARIOS, d])
        self.assertNotEqual(inv.baseline_dir(plain, "a" * 40, None), inv.baseline_dir(more, "a" * 40, None))
        # a different targeted set (other contents) gets yet another baseline
        other = targeted.write(targeted.plan(report("mis_options", "copter"), "copter"), root)
        self.assertNotEqual(inv.baseline_dir(more, "a" * 40, None),
                            inv.baseline_dir(suites.Scenarios(Path("."), "g", [SCENARIOS, other]), "a" * 40, None))


class InvestigateTest(unittest.TestCase):
    def test_autotest_and_px4_refused(self):
        with mock.patch.object(inv, "lock_checkout", side_effect=AssertionError("started")):
            with self.assertRaisesRegex(scenario.ScenarioSetError, "autotest"):
                inv.investigate(Path("nowhere"), "a", "b", suite="autotest", targeted=True)
            with self.assertRaisesRegex(scenario.ScenarioSetError, "PX4"):
                inv.investigate(Path("nowhere"), "a", "b", vehicle="px4", targeted=True)

    def test_targeted_dir_added_checked_and_recorded(self):
        root = tmp()
        p = targeted.plan(loiter_changed(), "copter")
        made = []

        def prepare(repo, good, bad, vehicle, dirs):
            self.assertEqual(dirs, [SCENARIOS.resolve()])
            made.append(targeted.write(p, root))
            return p, made[-1]

        class Rec:
            def __init__(self, **meta):
                self.data = meta
                self.logs = []

            def log(self, msg):
                self.logs.append(msg)

            def step(self, *a, **k):
                pass

            def set(self, **k):
                self.data.update(k)

        checked = []
        with mock.patch.object(targeted, "prepare", side_effect=prepare), \
                mock.patch.object(inv.scenario, "check", side_effect=lambda d, o: checked.append((d, o))), \
                mock.patch.object(inv.scenario, "as_dirs", return_value=[SCENARIOS]), \
                mock.patch.object(inv, "lock_checkout"), mock.patch.object(inv, "git", return_value="main"), \
                mock.patch.object(inv, "resolve", side_effect=lambda r, x: x), \
                mock.patch.object(inv, "build", return_value=(None, {})), \
                mock.patch.object(inv, "Record", Rec):
            rec = inv.investigate(Path("."), "g", "b", targeted=True, only=["hover"])
        d = made[0].resolve()
        self.assertEqual(checked, [([SCENARIOS.resolve(), d],
                                    ["hover", "copter_sticks__ZIGZAG", "pilot_sticks__LOIT_OPTIONS_0",
                                     "fence_avoid__LOIT_OPTIONS_0"])])
        # fix reads scenario_dirs from the record, so it flies the same set
        self.assertEqual(rec.data["scenario_dirs"], [str(SCENARIOS.resolve()), str(d)])
        self.assertEqual([i["name"] for i in rec.data["targeted"]["add"]],
                         ["copter_sticks__ZIGZAG", "pilot_sticks__LOIT_OPTIONS_0", "fence_avoid__LOIT_OPTIONS_0"])
        self.assertNotIn("spec", rec.data["targeted"]["add"][0])
        json.dumps(rec.data)
        section = inv._targeted_section(rec)
        self.assertEqual(section[0], "## Targeted flights (--targeted)")
        self.assertIn("  copter_sticks__ZIGZAG: ZIGZAG uses the changed code", "\n".join(section))

    def test_no_section_without_targeting(self):
        self.assertEqual(inv._targeted_section(SimpleNamespace(data={})), [])


class CliTest(unittest.TestCase):
    def test_impact_plan(self):
        rep = loiter_changed()
        out = io.StringIO()
        with mock.patch.object(impact, "impact", return_value=rep), redirect_stdout(out):
            cli.main(["impact", "--repo", str(Path(__file__).parents[1]), "--good", "a", "--bad", "b", "--plan"])
        self.assertIn("Targeted flights added (3, at most 12):", out.getvalue())
        self.assertIn("copter_sticks__ZIGZAG", out.getvalue())


if __name__ == "__main__":
    unittest.main()
