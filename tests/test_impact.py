import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from forkpilot import impact, investigate
from tests.impact_fixtures import DirGit


def run(case, vehicle):
    return impact.analyse(DirGit(case), "good", "bad", vehicle)


def params(res):
    return {p["name"]: p for p in res["params"]}


class DiffTest(unittest.TestCase):
    DIFF = """diff --git a/libraries/X/X.h b/libraries/X/X.h
--- a/libraries/X/X.h
+++ b/libraries/X/X.h
@@ -10,2 +10,3 @@ class X
 keep
-old line
+new line
+another
 tail
@@ -40 +50,0 @@
-gone
diff --git a/new.cpp b/new.cpp
--- /dev/null
+++ b/new.cpp
@@ -0,0 +1 @@
+x
"""

    def test_lines_and_numbers(self):
        d = impact.parse_diff(self.DIFF)
        x = d["libraries/X/X.h"]
        self.assertEqual(x["added"], ["new line", "another"])
        self.assertEqual(x["removed"], ["old line", "gone"])
        self.assertEqual(x["lines"], {11, 12, 50})
        self.assertEqual(d["new.cpp"]["added"], ["x"])

    def test_identifiers_skip_comments_and_strings(self):
        self.assertEqual(impact.idents(["a = b; // c", "// d", " * e", 'f("g")']), {"a", "b", "f"})


class ClassTest(unittest.TestCase):
    def test_header_spans_by_braces(self):
        text = "namespace N {\nclass Alpha {\n  struct In {\n  };\n  int x;\n};\nclass Beta;\nenum class E { a };\nclass Gamma : public Alpha\n{\n};\n}\n"
        spans = {n: (a, b) for n, a, b in impact.class_spans(text)}
        self.assertEqual(spans, {"In": (3, 4), "Alpha": (2, 6), "Gamma": (9, 11)})

    def test_header_only_classes_with_a_changed_line(self):
        text = "class Alpha {\n int x;\n};\nclass Beta {\n int y;\n};\n"
        self.assertEqual(impact.classes_in("a.h", text, {5}), {"Beta"})
        self.assertEqual(impact.classes_in("a.h", text), {"Alpha", "Beta"})

    def test_cpp_definitions(self):
        text = ("#include <x.h>\nconst AP_Param::GroupInfo AC_Loiter::var_info[] = {\n"
                "void AC_Loiter::update()\n{\n}\nfloat Other::get(int a) const\n{\n}\nvoid free_fn()\n{\n}\n"
                "void free2()\n")
        self.assertEqual(impact.classes_in("a.cpp", text), {"AC_Loiter", "Other"})


class DocTest(unittest.TestCase):
    LINES = ["    // @Param: OPTIONS", "    // @Range: 0 10", "    // @Units: m",
             "    // @Bitmask: 0:A, 1:B,2:C", "    // @Bitmask{Copter}: 0:A, 2:C", "    // @Bitmask{Rover, Sub}: 0:A",
             "    // @Values{Plane}: 0:Off,1:On"]

    def test_vehicle_variant_wins(self):
        self.assertEqual(impact.parse_doc(self.LINES, "copter")["bitmask"], [[0, "A"], [2, "C"]])
        self.assertEqual(impact.parse_doc(self.LINES, "plane")["bitmask"], [[0, "A"], [1, "B"], [2, "C"]])

    def test_other_fields(self):
        d = impact.parse_doc(self.LINES, "plane")
        self.assertEqual((d["range"], d["units"], d["values"]), (["0", "10"], "m", [[0, "Off"], [1, "On"]]))
        self.assertNotIn("values", impact.parse_doc(self.LINES, "copter"))

    def test_prefixes_of_parameters_cpp(self):
        text = '''    GOBJECT(rollController,         "RLL",   AP_RollController),
    GOBJECTN(mode_auto.mission, mission, "MIS_", AP_Mission),
    GOBJECTPTR(loiter_nav, "LOIT_", AC_Loiter),
    GGROUP(g2, "", ParametersG2),
    GSCALAR(x, "X", 1),'''
        self.assertEqual(impact.vehicle_prefixes(text), {"AP_RollController": ["RLL"], "AP_Mission": ["MIS_"],
                                                         "AC_Loiter": ["LOIT_"], "ParametersG2": [""]})

    def test_table_entries_keep_docs_and_stop_at_group_end(self):
        text = ("const AP_Param::GroupInfo A::var_info[] = {\n// @Param: P\n// @Units: m\n"
                'AP_GROUPINFO("P", 0, A, _p, 1),\nAP_GROUPEND\n};\n'
                'const AP_Param::GroupInfo B::var_info[] = {\nAP_GROUPINFO_FLAGS("Q", 0, B, _q, 1, 0),\nAP_GROUPEND\n};\n')
        e = impact.table_entries(text, "A", "copter")
        self.assertEqual([(x.name, x.var, x.doc) for x in e], [("P", "_p", {"units": "m"})])
        self.assertEqual([x.name for x in impact.table_entries(text, "B", "copter")], ["Q"])


class ModeTest(unittest.TestCase):
    def test_names_from_mode_h(self):
        table, _ = impact.mode_table(DirGit("loiter"), "bad", "ArduCopter")
        self.assertEqual(table["ModePosHold"], "POSHOLD")
        self.assertEqual(table["ModeAuto"], "AUTO")
        self.assertEqual(table["ModeSmartRTL"], "SMART_RTL")
        self.assertNotIn("Mode", table)

    def test_plane_names_are_upper_case(self):
        table, _ = impact.mode_table(DirGit("plane_training"), "bad", "ArduPlane")
        self.assertEqual(table["ModeTraining"], "TRAINING")
        self.assertEqual(table["ModeQLoiter"], "QLOITER")
        self.assertEqual(table["ModeLoiterAltQLand"], "LOITER_ALT_QLAND")
        self.assertNotIn("INITIALISING", table.values())

    def test_file_to_mode(self):
        table = {"ModeAltHold": "ALT_HOLD", "ModeQLoiter": "QLOITER"}
        self.assertEqual(impact.mode_of_file("ArduCopter/mode_alt_hold.cpp", table), "ALT_HOLD")
        self.assertEqual(impact.mode_of_file("ArduPlane/mode_qloiter.cpp", table), "QLOITER")
        self.assertEqual(impact.mode_of_file("ArduPlane/mode_new_thing.cpp", table), "NEW_THING")


class SetupFunctionTest(unittest.TestCase):
    TEXT = ("static void helper()\n{\n}\n\nvoid Copter::init_ardupilot()\n{\n    if (x) {\n"
            "        mission.init();\n    }\n}\n\nvoid Copter::update()\n{\n    mission.update();\n}\n"
            "int g = mission.n;\n")

    def test_enclosing_function(self):
        self.assertEqual(impact.enclosing_function(self.TEXT, 8), "init_ardupilot")
        self.assertEqual(impact.enclosing_function(self.TEXT, 14), "update")
        self.assertIsNone(impact.enclosing_function(self.TEXT, 16))

    def test_setup_names(self):
        for name in ("init_ardupilot", "setup", "rc_init", "load_parameters"):
            self.assertTrue(impact.SETUP_FN.search(name), name)
        for name in ("update", "fast_loop", "reinit_check"):
            self.assertFalse(impact.SETUP_FN.search(name), name)


class GroundTruthTest(unittest.TestCase):
    """The four holdout2 culprits, from fixtures of the upstream sources."""

    def test_loiter(self):
        r = run("loiter", "copter")
        self.assertEqual(r["classes"], ["AC_Loiter"])
        self.assertEqual(list(r["modes"]), ["LOITER", "POSHOLD", "ZIGZAG"])
        self.assertEqual(r["core"], [])
        obj = next(o for o in r["objects"] if o["name"] == "loiter_nav")
        self.assertEqual(obj["decl"], "ArduCopter/Copter.h")
        # mode.cpp (Mode base class) and tuning.cpp also use it: reported, not core
        self.assertEqual(obj["other_files"], ["mode.cpp", "tuning.cpp"])
        p = params(r)["LOIT_OPTIONS"]
        self.assertEqual(p["status"], "new")
        self.assertEqual(p["doc"]["bitmask"], [[0, "Enable Coordinated turns"]])

    def test_mis_options(self):
        r = run("mis_options", "copter")
        p = params(r)["MIS_OPTIONS"]
        self.assertEqual(p["var"], "_options")
        self.assertEqual(p["doc"]["bitmask"], [[0, "Clear Mission on reboot"], [2, "ContinueAfterLand"]])
        self.assertEqual(p["status"], "changed")
        # system.cpp uses the mission only in init_ardupilot(): boot-time setup, not core
        self.assertEqual(r["core"], [])
        self.assertEqual(list(r["modes"]), ["AUTO"])
        obj = next(o for o in r["objects"] if o["name"] == "mission")
        self.assertIn("system.cpp", obj["other_files"])

    def test_plane_training(self):
        r = run("plane_training", "plane")
        self.assertEqual({o["name"] for o in r["objects"] if o["core_files"]}, {"rollController", "pitchController"})
        self.assertIn("Attitude.cpp", r["core"][0]["reason"])
        self.assertIn("TRAINING", r["all_modes"])
        c = impact.coverage(r, [])
        self.assertIn("TRAINING", c["modes_not_flown"])
        self.assertEqual(params(r)["RLL_ANGLE_P"]["status"], "new")
        self.assertIn("RLL2SRV_TCONST", params(r))

    def test_poshold(self):
        r = run("poshold", "copter")
        self.assertEqual(list(r["modes"]), ["POSHOLD"])
        self.assertEqual(r["core"], [])
        p = params(r)["PHLD_BRAKE_RATE"]
        self.assertEqual(p["scope"], "vehicle")
        self.assertEqual(p["doc"]["range"], ["4", "12"])
        self.assertEqual(p["doc"]["units"], "deg/s")

    def test_defaults_from_table_and_define(self):
        self.assertEqual(params(run("loiter", "copter"))["LOIT_OPTIONS"]["default"], 1)       # LOITER_DEFAULT_OPTIONS
        self.assertEqual(params(run("mis_options", "copter"))["MIS_OPTIONS"]["default"], 0)
        self.assertEqual(params(run("plane_training", "plane"))["RLL2SRV_TCONST"]["default"], 0.5)
        self.assertIsNone(impact.default_value(None, "bad", "FN(2)", []))
        self.assertEqual(impact.default_value(None, "bad", "0x10", []), 16)

    def test_vehicle_name_checked(self):
        with self.assertRaises(impact.ImpactError):
            impact.analyse(DirGit("loiter"), "good", "bad", "rover")


class CoverageTest(unittest.TestCase):
    def facts(self, vehicle):
        return impact.scenario_facts_for(vehicle)

    def test_loiter_zigzag_not_flown(self):
        r = run("loiter", "copter")
        c = impact.coverage(r, self.facts("copter"))
        self.assertEqual(sorted(c["modes_flown"]), ["LOITER", "POSHOLD"])
        self.assertEqual(c["modes_flown"]["LOITER"], ["fence_avoid", "flow_rangefinder", "pilot_sticks"])
        self.assertEqual(c["modes_not_flown"], ["ZIGZAG"])
        self.assertEqual(c["params_default"], ["LOIT_OPTIONS"])
        self.assertFalse(c["core"])

    def test_core_change_lists_every_unflown_mode(self):
        c = impact.coverage(run("plane_training", "plane"), self.facts("plane"))
        self.assertTrue(c["core"])
        self.assertIn("TRAINING", c["modes_not_flown"])
        self.assertIn("QRTL", c["modes_flown"])

    def test_param_set_by_a_scenario(self):
        r = run("mis_options", "copter")
        c = impact.coverage(r, [{"name": "s", "modes": set(), "params": {"MIS_OPTIONS": 4}}])
        self.assertEqual(c["params_set"], {"MIS_OPTIONS": [{"scenario": "s", "value": 4}]})
        self.assertEqual(c["params_default"], [])

    def test_implicit_modes_of_shipped_scenarios(self):
        by = {f["name"]: f for f in self.facts("copter")}
        self.assertEqual(by["pilot_sticks"]["modes"], {"GUIDED", "LOITER", "POSHOLD", "ALT_HOLD", "LAND"})
        self.assertEqual(by["rc_loss"]["modes"], {"GUIDED", "RTL"})        # FS_THR_ENABLE default 1
        self.assertEqual(by["battery_low"]["modes"], {"GUIDED", "RTL"})    # BATT_FS_LOW_ACT 2
        self.assertEqual(by["gps_loss"]["modes"], {"GUIDED", "LAND"})      # FS_EKF_ACTION default 1
        self.assertEqual(by["battery_low"]["params"]["BATT_FS_LOW_ACT"], 2)
        plane = {f["name"]: f for f in self.facts("plane")}
        self.assertEqual(plane["plane_mission"]["modes"], {"AUTO"})
        self.assertEqual(plane["quadplane_modes"]["modes"], {"QLOITER", "CRUISE", "QLAND"})
        self.assertEqual(plane["quadplane_rc_loss"]["modes"], {"AUTO", "RTL", "QRTL"})


class OutputTest(unittest.TestCase):
    def report(self):
        r = run("loiter", "copter")
        return {"impact": r, "coverage": impact.coverage(r, [{"name": "s", "modes": {"LOITER"}, "params": {}}])}

    def test_text(self):
        text = "\n".join(impact.lines(self.report()))
        self.assertIn("Affected modes: LOITER, POSHOLD, ZIGZAG", text)
        self.assertIn("LOIT_OPTIONS (new; bits 0:Enable Coordinated turns)", text)
        self.assertIn("Affected modes flown: LOITER (s)", text)
        self.assertIn("no scenario flies: POSHOLD, ZIGZAG", text)
        self.assertIn("does not prove", text)

    def test_json_serialisable(self):
        json.dumps(self.report())

    def test_turkish(self):
        from forkpilot import i18n
        i18n.set_lang("tr")
        try:
            self.assertIn("Etkilenen modlar", "\n".join(impact.lines(self.report())))
        finally:
            i18n._lang = None
            os.environ.pop("FP_LANG", None)

    def test_unknown_prefix_is_not_guessed(self):
        p = {"name": None, "key": "X", "status": "changed", "doc": {}}
        self.assertEqual(impact._param_line(p), "  ?X (prefix unknown)")


class EvidenceTest(unittest.TestCase):
    def test_section_added(self):
        rec = SimpleNamespace(data={"vehicle": "copter"}, log=mock.Mock())
        rep = {"impact": run("loiter", "copter")}
        rep["coverage"] = impact.coverage(rep["impact"], [])
        with mock.patch.object(impact, "impact", return_value=rep) as m:
            out = investigate._impact_section(rec, Path("."), "abc")
        m.assert_called_once_with(Path("."), "abc^", "abc", "copter")
        self.assertEqual(out[0], "## Impact (static analysis of the diff)")

    def test_failure_is_logged_not_raised(self):
        rec = SimpleNamespace(data={"vehicle": "copter"}, log=mock.Mock())
        with mock.patch.object(impact, "impact", side_effect=RuntimeError("boom")):
            self.assertEqual(investigate._impact_section(rec, Path("."), "abc"), [])
        self.assertIn("boom", rec.log.call_args[0][0])

    def test_px4_skipped(self):
        rec = SimpleNamespace(data={"vehicle": "px4"}, log=mock.Mock())
        with mock.patch.object(impact, "impact") as m:
            self.assertEqual(investigate._impact_section(rec, Path("."), "abc"), [])
        m.assert_not_called()


@unittest.skipUnless(os.environ.get("FP_TEST_ARDUPILOT"), "set FP_TEST_ARDUPILOT to an ArduPilot repository")
class IntegrationTest(unittest.TestCase):
    """The same four cases against a real repository; read-only (git show/diff/grep)."""

    def real(self, sha, vehicle):
        return impact.analyse(impact.RepoGit(os.environ["FP_TEST_ARDUPILOT"]), sha + "^", sha, vehicle)

    def test_loiter(self):
        r = self.real("275c54a24b", "copter")
        self.assertEqual(list(r["modes"]), ["LOITER", "POSHOLD", "ZIGZAG"])
        self.assertIn("LOIT_OPTIONS", params(r))

    def test_mis_options(self):
        p = params(self.real("317fab2f19", "copter"))["MIS_OPTIONS"]
        self.assertEqual(p["doc"]["bitmask"], [[0, "Clear Mission on reboot"], [2, "ContinueAfterLand"]])

    def test_plane_training(self):
        r = self.real("7e7960e032", "plane")
        self.assertTrue(r["core"])
        self.assertIn("TRAINING", r["all_modes"])

    def test_poshold(self):
        r = self.real("d56e91bbe2", "copter")
        self.assertEqual(list(r["modes"]), ["POSHOLD"])
        self.assertIn("PHLD_BRAKE_RATE", params(r))


if __name__ == "__main__":
    unittest.main()
