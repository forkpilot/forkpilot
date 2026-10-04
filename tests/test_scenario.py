"""Offline tests for company scenarios: validation, directories, fingerprints, reference."""
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pymavlink import mavutil

from forkpilot import battery, cli, metrics, scenario, suites
from forkpilot import fix as fx
from forkpilot import investigate as inv
from forkpilot.runner import Runner

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "scenarios"


def write(d: Path, name: str, text: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.yaml"
    p.write_text(text)
    return p


def tmp() -> Path:
    return Path(tempfile.mkdtemp())


def lint(text: str, name="s"):
    return scenario.lint_file(write(tmp(), name, text))


def errors(issues):
    return [i for i in issues if i.level == "error"]


GOOD = """name: s
steps:
  - takeoff: 10
  - hold: 20
  - mode: LAND
  - wait_disarm: 120
expect:
  hold_drift_max_m: {lt: 1.5}
  landing_offset_m: {lt: 2.0}
"""


class LintTest(unittest.TestCase):
    def test_shipped_scenarios_are_clean(self):
        self.assertEqual([str(i) for i in scenario.lint_paths([SHIPPED])], [])

    def test_good_file(self):
        self.assertEqual(lint(GOOD), [])

    def test_yaml_error_has_a_line(self):
        i, = lint("name: s\nsteps:\n  - takeoff: [1\n  - hold: 2\n")
        self.assertEqual((i.level, i.line > 1), ("error", True))
        self.assertIn("YAML", i.msg)

    def test_top_level(self):
        i = lint("name: s\nstep:\n  - takeoff: 5\n")
        self.assertTrue(any("unknown key 'step' (did you mean 'steps'?)" in x.msg and x.line == 2 for x in i))
        self.assertTrue(any("missing 'steps'" in x.msg for x in i))
        self.assertIn("must equal the file name", lint("name: other\nsteps: [{takeoff: 5}]\n")[0].msg)
        self.assertIn("missing 'name'", lint("steps: [{takeoff: 5}]\n")[0].msg)
        self.assertIn("mapping", lint("- a\n- b\n")[0].msg)
        self.assertIn("mapping", lint("")[0].msg)
        self.assertIn("non-empty list", lint("name: s\nsteps: []\n")[0].msg)

    def test_duplicate_key(self):
        i = errors(lint(GOOD + "  hold_drift_max_m: {lt: 3}\n"))
        self.assertEqual(len(i), 1)
        self.assertIn("appears twice", i[0].msg)
        self.assertEqual(i[0].line, 10)

    def test_step_names_and_shapes(self):
        cases = {
            "- gotoo: [1, 2, 3]": ("unknown step 'gotoo' (did you mean 'goto'?)", 3),
            "- hold": ("needs an argument", 3),
            "- takeoff: ten": ("must be a number", 3),
            "- takeoff: -5": ("must be > 0", 3),
            "- takeoff: true": ("must be a number", 3),
            "- goto: [1, 2]": ("[north, east, up]", 3),
            "- goto: {n: 1}": ("[north, east, up]", 3),
            "- goto: [1, 2, x]": ("must be a number", 3),
            "- mode: Loiter": ("did you mean 'LOITER'", 3),
            "- mode: [LOITER]": ("flight mode name", 3),
            "- mission: []": ("non-empty list", 3),
            "- mission: [[1, 2]]": ("waypoint 1", 3),
            "- sticks: {thrust: 1500}": ("unknown stick channel 'thrust'", 3),
            "- sticks: {pitch: 2500}": ("<= 2000", 3),
            "- sticks: 1500": ("mapping", 3),
            "- velocity: {vx: 1}": ("needs 'seconds'", 3),
            "- velocity: {vx: 1, seconds: 4, frame: bodyy}": ("did you mean 'body'", 3),
            "- velocity: {vx: 1, secs: 4}": ("unknown velocity key 'secs'", 3),
            "- set_param: {sim_x: 1}": ("not a parameter name", 3),
            "- set_param: {ABCDEFGHIJKLMNOPQ: 1}": ("longer than 16", 3),
            "- set_param: {SIM_X: yes}": ("write 1 or 0", 3),
            "- set_param: []": ("mapping", 3),
            "- yaw: 400": ("<= 360", 3),
            "- wait_disarm: 0": ("> 0", 3),
            "- release: abc": ("must be a number", 3),
            "- takeoff: 5\n    hold: 3": ("single `name: argument`", 3),
        }
        for step, (msg, line) in cases.items():
            with self.subTest(step):
                issues = errors(lint(f"name: s\nsteps:\n  {step}\n"))
                self.assertTrue(any(msg in i.msg and i.line == line for i in issues),
                                [str(i) for i in issues])

    def test_warnings(self):
        w = lambda text: [i.msg for i in lint(text) if i.level == "warning"]       # noqa: E731
        self.assertTrue(any("before any takeoff" in m for m in w("name: s\nsteps:\n  - hold: 5\n")))
        self.assertTrue(any("no wait_disarm" in m for m in w("name: s\nsteps:\n  - takeoff: 5\n")))
        self.assertTrue(any("AUTO has nothing" in m
                            for m in w("name: s\nsteps:\n  - takeoff: 5\n  - mode: AUTO\n  - wait_disarm: 9\n")))
        self.assertTrue(any("GUIDED command and the vehicle is in LOITER" in m for m in w(
            "name: s\nsteps:\n  - takeoff: 5\n  - mode: LOITER\n  - goto: [5, 0, 5]\n  - wait_disarm: 9\n")))
        self.assertEqual(w("name: s\nsteps:\n  - takeoff: 5\n  - mode: LOITER\n  - mode: GUIDED\n"
                           "  - goto: [5, 0, 5]\n  - wait_disarm: 9\n"), [])
        self.assertTrue(any("never be satisfied" in m for m in w(GOOD.replace("{lt: 1.5}", "{gt: 5, lt: 3}"))))

    def test_params(self):
        self.assertEqual(lint(GOOD.replace("steps:", "params: {SIM_WIND_SPD: 5, FENCE_RADIUS: 60.5}\nsteps:")), [])
        self.assertEqual(lint(GOOD.replace("steps:", "params:\nsteps:")), [])      # empty params: null
        bad = errors(lint(GOOD.replace("steps:", "params: [A]\nsteps:")))
        self.assertIn("params must be a mapping", bad[0].msg)
        bad = errors(lint(GOOD.replace("steps:", "params: {sim_wind_spd: 5}\nsteps:")))
        self.assertIn("did you mean 'SIM_WIND_SPD'", bad[0].msg)

    def test_expect_rules(self):
        e = lambda rule: errors(lint(GOOD.replace("hold_drift_max_m: {lt: 1.5}", rule)))      # noqa: E731
        self.assertIn("did you mean 'le'", e("hold_drift_max_m: {lte: 1.5}")[0].msg)
        self.assertIn("unknown operator", e("hold_drift_max_m: {eq: 1.5}")[0].msg)
        self.assertIn("must be a mapping", e("hold_drift_max_m: 1.5")[0].msg)
        self.assertIn("must be a number", e("hold_drift_max_m: {lt: small}")[0].msg)
        self.assertIn("did you mean 'hold_drift_max_m'", e("hold_drif_max_m: {lt: 1.5}")[0].msg)
        self.assertEqual(errors(lint(GOOD.replace("expect:", "expect: {}\nx:"))) [0].msg.split("'")[1], "x")
        self.assertEqual(lint(GOOD.replace("hold_drift_max_m: {lt: 1.5}", "completed: {ge: 1}")), [])

    def test_metric_availability(self):
        steps = "name: s\nsteps:\n  - takeoff: 10\n  - hold: 5\n  - mode: LAND\n  - wait_disarm: 60\nexpect:\n"
        ok = ["takeoff_time_s", "hold_drift_max_m", "landing_offset_m", "alt_max_m", "range_max_m",
              "flight_time_s"]
        for m in ok:
            self.assertEqual(lint(steps + f"  {m}: {{lt: 100}}\n"), [], m)
        # produced only when the scenario has the step that feeds it
        for m, why in [("failsafe_reaction_s", "set_param"), ("goto_time_total_s", "goto"),
                       ("mission_wp_reached", "mission"), ("stop_dist_m_loiter", "release"),
                       ("vel_err_mps_1", "velocity")]:
            with self.subTest(m):
                i = errors(lint(steps + f"  {m}: {{lt: 100}}\n"))
                self.assertEqual(len(i), 1, [str(x) for x in i])
                self.assertIn(why, i[0].msg)
                self.assertEqual(i[0].line, 8)
        self.assertIn("did you mean 'hold_drift_max_m'", errors(lint(steps + "  hold_drift_m: {lt: 1}\n"))[0].msg)

    def test_dynamic_metric_names(self):
        base = ("name: s\nsteps:\n  - takeoff: 10\n  - sticks: {throttle: 1500}\n  - mode: LOITER\n"
                "  - fly: 6\n  - sticks: {pitch: 1300}\n  - fly: 6\n  - release: 10\n"
                "  - mode: POSHOLD\n  - fly: 3\n  - release: 10\n  - release: 10\n  - wait_disarm: 60\nexpect:\n")
        for m in ["stop_dist_m_loiter", "stop_time_s_poshold", "stop_dist_m_poshold2", "stick_speed_mps_loiter",
                  "mode_speed_mps_loiter", "mode_yaw_rate_dps_loiter2", "mode_speed_mps_poshold",
                  "mode_speed_mps_poshold2"]:
            self.assertEqual(lint(base + f"  {m}: {{lt: 100}}\n"), [], m)
        for m, msg in [("stop_dist_m_poshold3", "needs a release step in POSHOLD"),
                       ("stop_dist_m_alt_hold", "needs a release step in ALT_HOLD"),
                       ("stop_dist_m_poshhold", "'poshhold' is not a flight mode name"),
                       ("mode_speed_mps_loiter9", "never produced"),
                       ("mode_speed_mps_alt_hold", "never produced")]:
            with self.subTest(m):
                i = errors(lint(base + f"  {m}: {{lt: 100}}\n"))
                self.assertTrue(i and msg in i[0].msg, [str(x) for x in i])

    def test_undecidable_mode_is_a_warning(self):
        text = ("name: s\nsteps:\n  - takeoff: 10\n  - set_param: {SIM_RC_FAIL: 1}\n  - release: 10\n"
                "  - wait_disarm: 60\nexpect:\n  stop_dist_m_rtl: {lt: 5}\n  stop_dist_m_zigzag: {lt: 5}\n")
        issues = lint(text)
        self.assertEqual([i.level for i in issues], ["warning", "warning"])
        self.assertIn("cannot be decided", issues[0].msg)

    def test_velocity_numbering(self):
        text = ("name: s\nsteps:\n  - takeoff: 10\n  - velocity: {vx: 1, seconds: 2}\n"
                "  - velocity: {vx: 1, seconds: 8}\n  - wait_disarm: 60\nexpect:\n  vel_err_mps_1: {lt: 1}\n"
                "  vel_err_mps_2: {lt: 1}\n")
        i = errors(lint(text))       # the 2 s command is too short to measure: the 8 s one is number 1
        self.assertEqual([x.msg.split("'")[1] for x in i], ["vel_err_mps_2"])


class VocabularyTest(unittest.TestCase):
    def test_every_runner_step_has_a_shape(self):
        self.assertEqual(set(scenario.step_names()), set(scenario.STEPS))
        self.assertEqual(set(scenario.step_names()),
                         {n[5:] for n in dir(Runner) if n.startswith("step_")})

    def test_examples_are_valid(self):
        for name, st in scenario.STEPS.items():
            with self.subTest(name):
                self.assertTrue(st.example.startswith(name + ":"))
                text = f"name: s\nsteps:\n  - takeoff: 5\n  - {st.example}\n  - wait_disarm: 9\n"
                self.assertEqual(errors(lint(text)), [])

    def test_shapes_follow_the_runner(self):
        for ch in Runner.STICKS:
            self.assertEqual(errors(lint(f"name: s\nsteps:\n  - sticks: {{{ch}: 1500}}\n")), [])
        for fr in Runner.VEL_FRAMES:
            self.assertEqual(errors(lint(f"name: s\nsteps:\n  - velocity: {{vx: 1, seconds: 4, frame: {fr}}}\n")), [])

    def test_metric_table_covers_metrics_py(self):
        src = (ROOT / "forkpilot" / "metrics.py").read_text().split("def compute_generic")[0]
        used = set(re.findall(r'm\[f?"([a-z_]+?)_?(?:\{[a-z]+\})?"\]', src))
        known = {re.sub(r"_?<\w+>$", "", n) for n, *_ in scenario.METRICS}
        self.assertEqual(used - known, set())
        self.assertEqual(known - used, set(), "in the table but no longer in metrics.py")
        self.assertGreaterEqual(len(used), 20)

    def test_every_dynamic_family_is_listed(self):
        stems = {s for stems in scenario.FAMILIES.values() for s in stems}
        self.assertEqual(stems, {re.sub(r"_<mode>$", "", n) for n, *_ in scenario.METRICS
                                 if n.endswith("_<mode>")})


def fake_run(steps):
    """Telemetry shaped like a flight of these steps (the runner's events, 25 Hz samples), to
    check that the static prediction agrees with what metrics.compute produces."""
    st = {"t": 0.0, "x": 0.0, "num": {v: k for k, v in mavutil.mode_mapping_acm.items()}}
    ev, samples = [(0.0, "ready", None)], []
    state = {"vx": 0.0, "vz": 0.0, "z": 0.0, "mode": "STABILIZE"}

    def advance(seconds):
        end = st["t"] + seconds
        while st["t"] < end - 1e-9:
            st["t"] = round(st["t"] + 0.04, 6)
            st["x"] += state["vx"] * 0.04
            state["z"] += state["vz"] * 0.04
            t = st["t"]
            samples.append({"mavpackettype": "LOCAL_POSITION_NED", "t": t, "x": st["x"], "y": 0.0,
                            "z": state["z"], "vx": state["vx"], "vy": 0.0, "vz": state["vz"]})
            samples.append({"mavpackettype": "ATTITUDE", "t": t, "roll": 0.01, "pitch": 0.02, "yaw": 0.1,
                            "yawspeed": 0.05})
            samples.append({"mavpackettype": "GLOBAL_POSITION_INT", "t": t, "relative_alt": int(-state["z"] * 1000),
                            "vx": 0, "vy": 0, "vz": 0})
            if round(t / 1.0, 6) == int(t):
                samples.append({"mavpackettype": "HEARTBEAT", "t": t, "type": 2,
                                "custom_mode": st["num"].get(state["mode"], 0)})

    def event(kind, detail=None):
        ev.append((st["t"], kind, detail))

    advance(2)
    for name, arg in steps:
        if name == "takeoff":
            event("armed"); event("statustext", "Arming motors"); event("takeoff_cmd", arg)
            state["vz"] = -1.25
            advance(arg / 1.25)
            state["vz"] = 0.0
            event("takeoff_done", arg)
            state["mode"] = "GUIDED"
        elif name == "hold":
            event("hold_start", arg); advance(arg)
        elif name == "epoch":
            st["epoch"] = st["t"]
        elif name == "fly":
            advance(arg["until"] - (st["t"] - st.get("epoch", 0.0)) if isinstance(arg, dict) else arg)
        elif name == "goto":
            event("goto", list(arg)); state["vx"] = 2.0; advance(6); state["vx"] = 0.0; event("arrived", list(arg))
        elif name == "send_goto":
            event("send_goto", list(arg))
        elif name == "yaw":
            event("yaw", arg); advance(2)
        elif name == "velocity":
            event("velocity", {"frame": arg.get("frame", "local"),
                               "v": [float(arg.get(k, 0)) for k in ("vx", "vy", "vz")]})
            state["vx"] = float(arg.get("vx", 0)); advance(arg["seconds"]); state["vx"] = 0.0
            event("velocity_end")
        elif name == "set_param":
            for k, v in arg.items():
                event("set_param", [k, v])
            advance(1)
        elif name == "mode":
            state["mode"] = arg; event("mode", arg); advance(1)
        elif name == "mission":
            event("mission", len(arg))
        elif name == "sticks":
            event("sticks", dict(arg))
        elif name == "release":
            event("release", state["mode"]); state["vx"] = 0.0
            advance(arg["until"] - (st["t"] - st.get("epoch", 0.0)) if isinstance(arg, dict) else arg)
            event("release_end", state["mode"])
        elif name == "wait_disarm":
            advance(10); event("statustext", "Disarming motors"); event("disarmed")
    return {"scenario": "x", "ok": True, "events": ev, "samples": samples}


def steps_of(path):
    spec, _ = scenario.load(path)
    return [next(iter(s.items())) for s in spec["steps"]]


EXTRA_FLIGHTS = {
    "segments": [("takeoff", 10), ("sticks", {"throttle": 1500}), ("mode", "LOITER"), ("fly", 4),
                 ("sticks", {"pitch": 1300}), ("fly", 6), ("release", 10), ("mode", "POSHOLD"), ("fly", 6),
                 ("release", 10), ("release", 10), ("mode", "LAND"), ("wait_disarm", 60)],
    "velocities": [("takeoff", 10), ("velocity", {"vx": 1, "seconds": 2}),
                   ("velocity", {"vx": 1, "seconds": 3.4}), ("velocity", {"vx": 1, "seconds": 8}),
                   ("wait_disarm", 60)],
    "short_hold": [("takeoff", 10), ("hold", 0.5), ("mode", "RTL"), ("wait_disarm", 60)],
    "mission": [("mission", [[10, 0, 10]]), ("takeoff", 10), ("mode", "AUTO"), ("wait_disarm", 60)],
    "epoch": [("takeoff", 10), ("epoch", None), ("sticks", {"throttle": 1500}), ("mode", "LOITER"),
              ("fly", {"until": 12}), ("sticks", {"pitch": 1300}), ("fly", {"until": 20}),
              ("release", {"until": 30}), ("mode", "LAND"), ("wait_disarm", 60)],
    "no_land": [("takeoff", 10), ("goto", [10, 0, 10]), ("send_goto", [20, 0, 10]), ("yaw", 90),
                ("set_param", {"SIM_X": 1}), ("fly", 8)],
}


class PredictionTest(unittest.TestCase):
    """The static prediction against metrics.compute on synthetic telemetry of the same steps."""

    def check(self, steps):
        pred = scenario.predict(steps)
        got = set(metrics.compute(fake_run(steps)))
        self.assertEqual(pred.certain - got, set(), "predicted as certain but not produced")
        self.assertEqual(got - pred.certain - pred.possible, set(), "produced but not predicted")

    def test_shipped_scenarios(self):
        for p in sorted(SHIPPED.glob("*.yaml")):
            if scenario.load(p)[0].get("vehicle", "copter") != "copter":
                continue        # plane metrics are checked against a table, not predicted
            with self.subTest(p.stem):
                self.check(steps_of(p))

    def test_other_flights(self):
        for name, steps in EXTRA_FLIGHTS.items():
            with self.subTest(name):
                self.check(steps)


class EpochTest(unittest.TestCase):
    def runner(self, t):
        r = Runner.__new__(Runner)
        r.t, r.t_epoch, r.waits = t, 0.0, []
        r.wait = lambda cond, timeout, what: r.waits.append((cond, timeout))
        return r

    def test_plain_fly_is_a_duration(self):
        r = self.runner(100.0)
        r.step_fly(8)
        cond, timeout = r.waits[0]
        self.assertEqual(timeout, 13)
        r.t = 107.9
        self.assertFalse(cond())
        r.t = 108.0
        self.assertTrue(cond())

    def test_until_counts_from_the_epoch(self):
        r = self.runner(100.0)
        r.step_epoch(None)
        r.t = 104.0
        r.step_fly({"until": 10})
        cond, timeout = r.waits[0]
        self.assertAlmostEqual(timeout, 11)
        r.t = 109.9
        self.assertFalse(cond())
        r.t = 110.0
        self.assertTrue(cond())
        r.step_fly({"until": 5})           # already past: returns at once, no negative timeout
        self.assertTrue(r.waits[1][0]())
        self.assertEqual(r.waits[1][1], 5)

    def test_lint_shapes(self):
        ok = "name: s\nsteps:\n  - takeoff: 5\n  - epoch:\n  - fly: {until: 30}\n  - release: {until: 40}\n" \
             "  - wait_disarm: 60\n"
        self.assertEqual(lint(ok), [])
        bad = lint("name: s\nsteps:\n  - takeoff: 5\n  - epoch: 3\n  - fly: {to: 30}\n  - release: {until: x}\n"
                   "  - wait_disarm: 60\n")
        self.assertEqual(len(errors(bad)), 4)     # fly {to}: an unknown key and no until
        late = lint("name: s\nsteps:\n  - takeoff: 5\n  - fly: {until: 30}\n  - wait_disarm: 60\n")
        self.assertEqual([i.level for i in late], ["warning"])
        self.assertIn("no epoch step", late[0].msg)


class SetTest(unittest.TestCase):
    def test_collect_and_clash(self):
        a, b = tmp(), tmp()
        write(a, "one", GOOD.replace("name: s", "name: one"))
        write(b, "two", GOOD.replace("name: s", "name: two"))
        self.assertEqual(list(scenario.collect([a, b])), ["one", "two"])
        write(b, "one", GOOD.replace("name: s", "name: one"))
        with self.assertRaisesRegex(scenario.ScenarioSetError, "'one' is in two directories"):
            scenario.collect([a, b])
        with self.assertRaisesRegex(scenario.ScenarioSetError, "not found"):
            scenario.collect([a / "nope"])
        self.assertTrue(any("also used by" in str(i) for i in scenario.lint_paths([a, b])))
        self.assertEqual(len(scenario.collect()), len(list(SHIPPED.glob("*.yaml"))))     # default

    def test_shipped_and_custom_together(self):
        c = tmp()
        write(c, "mine", GOOD.replace("name: s", "name: mine"))
        names = list(scenario.collect([SHIPPED, c]))
        self.assertEqual(names, sorted(names))
        self.assertIn("mine", names)
        clash = write(c, "hover", GOOD.replace("name: s", "name: hover"))
        with self.assertRaises(scenario.ScenarioSetError):
            scenario.collect([SHIPPED, c])
        clash.unlink()

    def test_unknown_only(self):
        with self.assertRaisesRegex(scenario.ScenarioSetError, "unknown scenario 'hoover' \\(did you mean 'hover'"):
            scenario.paths(["hoover"])

    def test_check_blocks_invalid(self):
        d = tmp()
        write(d, "bad", "name: bad\nsteps:\n  - takeoff: ten\n")
        write(d, "fine", GOOD.replace("name: s", "name: fine"))
        with self.assertRaisesRegex(scenario.ScenarioSetError, "(?s)nothing was flown.*bad.yaml:3"):
            scenario.check([d])
        scenario.check([d], ["fine"])           # only what is selected has to be valid
        logged = []
        write(d, "warn", "name: warn\nsteps:\n  - hold: 3\n")
        scenario.check([d], ["warn"], log=logged.append)
        self.assertEqual(len(logged), 1)
        self.assertIn("warning", logged[0])

    def test_non_yaml_ignored_warning(self):
        d = tmp()
        write(d, "a", GOOD.replace("name: s", "name: a"))
        (d / "b.yml").write_text("x")
        self.assertTrue(any("ignored" in str(i) for i in scenario.lint_paths([d])))


class FingerprintTest(unittest.TestCase):
    def fp(self, dirs, only=None):
        return suites.Scenarios(Path("."), "x", dirs).fingerprint(only)

    def test_default_is_unchanged_by_dirs_argument(self):
        self.assertEqual(self.fp(None), self.fp([SHIPPED]))
        self.assertEqual(self.fp(None, ["hover"]), self.fp([SHIPPED], ["hover"]))

    def test_directory_does_not_matter_only_names_and_bytes(self):
        d = tmp()
        for p in SHIPPED.glob("*.yaml"):
            (d / p.name).write_bytes(p.read_bytes())
        self.assertEqual(self.fp([d]), self.fp(None))
        # split over two directories: same set, same fingerprint
        a, b = tmp(), tmp()
        for i, p in enumerate(sorted(SHIPPED.glob("*.yaml"))):
            ((a, b)[i % 2] / p.name).write_bytes(p.read_bytes())
        self.assertEqual(self.fp([a, b]), self.fp(None))
        self.assertEqual(self.fp([b, a]), self.fp(None))

    def test_custom_content_participates(self):
        a, b = tmp(), tmp()
        write(a, "mine", GOOD.replace("name: s", "name: mine"))
        write(b, "mine", GOOD.replace("name: s", "name: mine").replace("1.5", "1.6"))
        self.assertNotEqual(self.fp([a]), self.fp([b]))
        self.assertNotEqual(self.fp([a]), self.fp(None))
        c = tmp()
        write(c, "other", GOOD.replace("name: s", "name: other"))
        self.assertNotEqual(self.fp([a]), self.fp([c]))              # the name is part of it
        self.assertNotEqual(self.fp([SHIPPED, a]), self.fp(None))    # an added scenario changes the set

    def test_only_selects_across_dirs(self):
        a = tmp()
        write(a, "mine", GOOD.replace("name: s", "name: mine"))
        self.assertEqual(self.fp([SHIPPED, a], ["mine"]), self.fp([a], ["mine"]))
        s = suites.Scenarios(Path("."), "x", [SHIPPED, a])
        self.assertIn("mine", s.tests())
        self.assertEqual(s.tests(["mine"]), ["mine"])

    def test_baseline_dir_follows_fingerprint(self):
        a = tmp()
        write(a, "mine", GOOD.replace("name: s", "name: mine"))
        d1 = inv.baseline_dir(suites.Scenarios(Path("."), "x"), "a" * 40, None)
        d2 = inv.baseline_dir(suites.Scenarios(Path("."), "x", [a]), "a" * 40, None)
        self.assertNotEqual(d1, d2)
        self.assertEqual(d1, inv.baseline_dir(suites.Scenarios(Path("."), "x", [SHIPPED]), "a" * 40, None))

    def test_autotest_rejects_scenario_dirs(self):
        with self.assertRaises(scenario.ScenarioSetError):
            suites.Autotest(Path("."), "x", [SHIPPED])


class RulesFromDirsTest(unittest.TestCase):
    def test_expect_for_and_judge(self):
        d = tmp()
        write(d, "mine", GOOD.replace("name: s", "name: mine"))
        self.assertEqual(battery.expect_for("mine", [d]), {"hold_drift_max_m": {"lt": 1.5},
                                                           "landing_offset_m": {"lt": 2.0}})
        self.assertEqual(battery.expect_for("mine"), {})                  # not in the shipped set
        self.assertEqual(battery.expect_for("hover", [d, SHIPPED])["hold_drift_max_m"], {"lt": 1.5})
        out = tmp()
        (out / "mine.0.metrics.json").write_text(json.dumps(
            {"completed": 1.0, "hold_drift_max_m": 3.0, "landing_offset_m": 1.0}))
        self.assertEqual(battery.judge(out, dirs=[d])[0], "FAIL")
        self.assertEqual(battery.judge(out)[0], "PASS")                   # without the rules: no rule to break

    def test_empty_expect_is_empty(self):
        d = tmp()
        write(d, "e", "name: e\nsteps:\n  - takeoff: 5\nexpect:\n")
        self.assertEqual(battery.expect_for("e", [d]), {})


class FlowTest(unittest.TestCase):
    """The scenario set reaches investigate and fix, and is checked before anything is built."""

    def test_investigate_validates_before_building(self):
        d = tmp()
        write(d, "bad", "name: bad\nsteps:\n  - takeoff: ten\n")
        with mock.patch.object(inv, "lock_checkout", side_effect=AssertionError("started")):
            with self.assertRaises(scenario.ScenarioSetError):
                inv.investigate(Path("nowhere"), "a", "b", scenario_dirs=[d])
            with self.assertRaises(scenario.ScenarioSetError):
                inv.investigate(Path("nowhere"), "a", "b", suite="autotest", scenario_dirs=[d])

    def test_fix_uses_the_recorded_dirs(self):
        d, inv_dir = tmp(), tmp()
        write(d, "mine", GOOD.replace("name: s", "name: mine"))
        rec = {"outcome": "localized", "repo": ".", "bad": "b", "good": "g", "culprit": {},
               "baseline_dir": str(inv_dir), "suite": "scenarios", "scenario_dirs": [str(d)]}
        (inv_dir / "record.json").write_text(json.dumps(rec))
        seen = []

        class Stop(Exception):
            pass

        def fake_suite(repo, good, dirs, **kw):
            seen.append(dirs)
            raise Stop

        with mock.patch.dict(fx.SUITES, {"scenarios": fake_suite}):
            with self.assertRaises(Stop):
                fx.fix(inv_dir, repo=Path("."))
            self.assertEqual(seen, [[str(d)]])
            with self.assertRaises(Stop):               # an explicit override wins
                fx.fix(inv_dir, repo=Path("."), scenario_dirs=[SHIPPED])
            self.assertEqual(seen[-1], [SHIPPED])
        # the recorded directory is gone or broken: stop before any flight
        (d / "mine.yaml").write_text("name: mine\nsteps:\n  - takeoff: ten\n")
        with self.assertRaises(scenario.ScenarioSetError):
            fx.fix(inv_dir, repo=Path("."))


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["forkpilot", *map(str, argv)]), contextlib.redirect_stdout(out):
            try:
                cli.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_lint(self):
        d = tmp()
        write(d, "good", GOOD.replace("name: s", "name: good"))
        self.assertEqual(self.run_cli("lint", d)[0], 0)
        self.assertEqual(self.run_cli("lint")[0], 0)                      # the shipped scenarios
        write(d, "bad", "name: bad\nsteps:\n  - takeoff: ten\n")
        code, out = self.run_cli("lint", d)
        self.assertEqual(code, 1)
        self.assertIn("bad.yaml:3: error", out)
        write(d, "warn", "name: warn\nsteps:\n  - hold: 3\n")
        self.assertEqual(self.run_cli("lint", d / "warn.yaml")[0], 0)
        self.assertEqual(self.run_cli("lint", "--strict", d / "warn.yaml")[0], 1)

    def test_run_refuses_invalid_scenarios_before_flying(self):
        d = tmp()
        write(d, "bad", "name: bad\nsteps:\n  - takeoff: ten\n")
        with mock.patch.object(battery, "run_battery", side_effect=AssertionError("flew")):
            code, _ = self.run_cli("run", "--scenarios", d, "--out", tmp(), "-n", 1)
        self.assertIn("bad.yaml:3", str(code))

    def test_run_passes_the_dirs_on(self):
        d = tmp()
        write(d, "mine", GOOD.replace("name: s", "name: mine"))
        with mock.patch.object(battery, "run_battery") as rb:
            self.run_cli("run", "--scenarios", d, SHIPPED, "--only", "mine", "hover", "--out", tmp(), "-n", 1)
        self.assertEqual(rb.call_args.kwargs["dirs"], [d.resolve(), SHIPPED.resolve()])
        with mock.patch.object(battery, "run_battery") as rb:
            self.run_cli("run", "--out", tmp(), "-n", 1)
        self.assertIsNone(rb.call_args.kwargs["dirs"])

    def test_reference_matches_the_doc(self):
        code, out = self.run_cli("lint", "--reference")
        self.assertEqual(code, 0)
        doc = (ROOT / "docs" / "scenarios.md").read_text()
        block = doc.split("<!-- reference:start -->")[1].split("<!-- reference:end -->")[0].strip()
        self.assertEqual(block, out.strip(), "docs/scenarios.md is stale: run `forkpilot lint --reference`")


if __name__ == "__main__":
    unittest.main()
