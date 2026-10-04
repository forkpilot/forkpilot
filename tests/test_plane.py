"""Vehicle abstraction and Plane metrics on synthetic telemetry (no flights)."""
import hashlib
import re
import time
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pymavlink import mavutil

from forkpilot import battery, build, metrics, plane_metrics as pm, scenario, suites, timeline, vehicles
from forkpilot.plane import mission_items
from forkpilot.runner import ScenarioError

ROOT = Path(__file__).resolve().parents[1]

M = mavutil.mavlink


def sample(kind, t, **kw):
    return {"mavpackettype": kind, "t": t, **kw}


def pos(t, x, y, up=50.0, vx=0.0, vy=0.0, vz=0.0):
    return sample("LOCAL_POSITION_NED", t, x=x, y=y, z=-up, vx=vx, vy=vy, vz=vz)


def heartbeat(t, mode, mav_type=M.MAV_TYPE_FIXED_WING):
    return sample("HEARTBEAT", t, type=mav_type, custom_mode=mode)


AUTO, RTL, LOITER = 10, 11, 12


class VehicleTest(unittest.TestCase):
    def test_lookup(self):
        self.assertEqual(vehicles.get(None).binary, "arducopter")
        self.assertEqual(vehicles.get("plane").target, "plane")
        self.assertEqual(vehicles.get("plane").frame("quadplane").model, "quadplane")
        self.assertEqual(vehicles.get("plane").frame(None).model, "plane")
        self.assertEqual(vehicles.of_binary(Path("x/arduplane")).name, "plane")
        self.assertIsNone(vehicles.of_binary(Path("x/ardurover")))
        with self.assertRaises(ValueError):
            vehicles.get("blimp")
        with self.assertRaises(ValueError):
            vehicles.get("plane").frame("hexa")

    def test_defaults_saved_with_build_win(self):
        d = Path(tempfile.mkdtemp())
        (d / "plane.parm").write_text("")
        frame = vehicles.get("plane").frame("plane")
        self.assertEqual(vehicles.defaults_for(frame, d / "arduplane", Path("/ap")), str(d / "plane.parm"))
        quad = vehicles.get("plane").frame("quadplane")
        self.assertEqual(vehicles.defaults_for(quad, d / "arduplane", Path("/ap")),
                         "/ap/Tools/autotest/default_params/quadplane.parm")

    def test_scenarios_split_by_vehicle(self):
        copter = battery.scenario_paths(vehicle="copter")
        plane = battery.scenario_paths(vehicle="plane")
        self.assertIn("hover", [p.stem for p in copter])
        self.assertTrue(plane and all(vehicles.scenario_vehicle(p) == "plane" for p in plane))
        self.assertFalse(set(copter) & set(plane))
        self.assertEqual(len(battery.scenario_paths(["hover", plane[0].stem])), 2)   # as given

    def test_wrong_binary_refused(self):
        with self.assertRaises(ValueError):
            battery.run_battery(Path("/x/arducopter"), Path("/ap"), Path(tempfile.mkdtemp()),
                                vehicle="plane", log=None)

    def test_copter_fingerprint_as_before_plane(self):
        # the fingerprint Copter baselines were cached under, computed the way it was before
        h = hashlib.sha256(f"{battery.SPEEDUP}".encode())
        for p in sorted(battery.SCENARIOS.glob("*.yaml")):
            if not p.stem.startswith(("plane_", "quadplane_", "px4_")):
                h.update(p.name.encode() + p.read_bytes())
        for p in ("runner.py", "sitl.py"):
            h.update((battery.ROOT / "forkpilot" / p).read_bytes())
        self.assertEqual(suites.Scenarios(Path("."), "x").fingerprint(None), h.digest())
        self.assertNotEqual(suites.Scenarios(Path("."), "x", vehicle="plane").fingerprint(None),
                            h.digest())

    def test_build_cache_per_vehicle(self):
        cache = Path(tempfile.mkdtemp())
        sha = "a" * 40
        (cache / sha).mkdir()
        (cache / sha / "arducopter").write_text("")
        (cache / sha / "arduplane").write_text("")
        with mock.patch.object(build, "CACHE", cache), mock.patch.object(build, "resolve", return_value=sha):
            self.assertEqual(build.build(Path("."), "x")[0], cache / sha / "arducopter")
            self.assertEqual(build.build(Path("."), "x", vehicle="plane")[0], cache / sha / "arduplane")

    def test_timeline_names_plane_modes(self):
        run = {"events": [(0.0, "armed", None)],
               "samples": [heartbeat(1, AUTO), heartbeat(2, RTL),
                           heartbeat(3, 5, M.MAV_TYPE_QUADROTOR)]}
        texts = [e.text for e in timeline.events(run)]
        self.assertIn("mode → RTL", texts)


class MissionTest(unittest.TestCase):
    def test_items(self):
        items, info = mission_items([{"vtol_takeoff": 30}, {"wp": [100, 0, 50]},
                                     {"transition": "mc"}, {"vtol_land": [0, 0]}])
        self.assertEqual([i[2] for i in items], [M.MAV_CMD_NAV_WAYPOINT, M.MAV_CMD_NAV_VTOL_TAKEOFF,
                                                 M.MAV_CMD_NAV_WAYPOINT, M.MAV_CMD_DO_VTOL_TRANSITION,
                                                 M.MAV_CMD_NAV_VTOL_LAND])
        self.assertEqual(items[3][5], M.MAV_VTOL_STATE_MC)
        self.assertEqual(info[2], {"seq": 2, "kind": "wp", "n": 100.0, "e": 0.0, "up": 50.0})
        self.assertEqual(items[1][-1], 30.0)

    def test_plain_waypoints_end_with_rtl(self):
        items, _ = mission_items([[10, 0, 20], [10, 10, 20]])
        self.assertEqual(items[-1][2], M.MAV_CMD_NAV_RETURN_TO_LAUNCH)

    def test_unknown_item(self):
        with self.assertRaises(ScenarioError):
            mission_items([{"teleport": 1}])


def base_run(samples, events=(), frame="plane"):
    return {"vehicle": "plane", "frame": frame, "ok": True, "error": None,
            "events": list(events), "samples": sorted(samples, key=lambda s: s["t"])}


class PlaneMetricsTest(unittest.TestCase):
    def test_dispatch(self):
        run = base_run([pos(0, 0, 0)])
        self.assertEqual(metrics.compute(run)["completed"], 1.0)
        self.assertNotIn("hold_drift_max_m", metrics.compute(run))

    def test_leg_cross_track_and_height(self):
        mission = [{"seq": 0, "kind": "home", "n": 0.0, "e": 0.0, "up": 0.0},
                   {"seq": 1, "kind": "wp", "n": 0.0, "e": 0.0, "up": 50.0},
                   {"seq": 2, "kind": "wp", "n": 400.0, "e": 0.0, "up": 50.0}]
        s = [heartbeat(0, AUTO)]
        for i in range(101):
            t = 10 + i * 0.2
            s.append(pos(t, 4.0 * i, 3.0, up=52.0))          # 3 m east of the leg, 2 m high
            s.append(sample("MISSION_CURRENT", t, seq=2))
            s.append(sample("VFR_HUD", t, airspeed=22.0))
            s.append(sample("NAV_CONTROLLER_OUTPUT", t, aspd_error=50.0))
        s.append(sample("MISSION_CURRENT", 30.1, seq=3))    # reached: the leg counts
        s.append(heartbeat(40, RTL))
        for i in range(20):        # after a failsafe the current item stays 2: not a leg any more
            s.append(pos(41 + i, -500.0, 300.0))
            s.append(sample("MISSION_CURRENT", 41 + i, seq=2))
        m = metrics.compute(base_run(s, [(0, "mission", mission)]))
        self.assertAlmostEqual(m["leg_xtrack_max_m"], 3.0)
        self.assertAlmostEqual(m["leg_xtrack_rms_m"], 3.0)
        self.assertAlmostEqual(m["leg_alt_err_rms_m"], 2.0)
        self.assertAlmostEqual(m["leg_airspeed_mps"], 22.0)
        self.assertAlmostEqual(m["leg_aspd_err_rms_mps"], 0.5)
        cut = [x for x in s if not (x["mavpackettype"] == "MISSION_CURRENT" and x["seq"] == 3)]
        self.assertNotIn("leg_xtrack_max_m", metrics.compute(base_run(cut, [(0, "mission", mission)])))

    def test_circle_fit(self):
        pts = [(10 + 80 * math.cos(a / 10), 20 + 80 * math.sin(a / 10)) for a in range(63)]
        cx, cy, r = pm.fit_circle(pts)
        self.assertAlmostEqual(cx, 10, places=6)
        self.assertAlmostEqual(cy, 20, places=6)
        self.assertAlmostEqual(r, 80, places=6)

    def test_rtl_loiter(self):
        s = [heartbeat(0, AUTO), heartbeat(10, RTL)]
        for i in range(900):        # 90 s on a 90 m circle 5 m north of home, height 100 m
            t = 10 + i * 0.1
            s.append(pos(t, 5 + 90 * math.cos(t / 4), 90 * math.sin(t / 4), up=100.0))
        m = metrics.compute(base_run(s))
        self.assertAlmostEqual(m["loiter_radius_m_rtl"], 90, places=3)
        self.assertAlmostEqual(m["loiter_centre_err_m_rtl"], 5, places=3)
        self.assertAlmostEqual(m["loiter_alt_sd_m_rtl"], 0, places=6)

    def test_transitions(self):
        MC, FW, TO_FW = (M.MAV_VTOL_STATE_MC, M.MAV_VTOL_STATE_FW, M.MAV_VTOL_STATE_TRANSITION_TO_FW)
        flying = M.MAV_LANDED_STATE_IN_AIR
        states = [(0, FW, M.MAV_LANDED_STATE_ON_GROUND), (1, MC, M.MAV_LANDED_STATE_ON_GROUND),
                  (5, MC, flying), (20, TO_FW, flying), (28, FW, flying), (50, TO_FW, flying),
                  (52, FW, flying), (70, MC, flying)]
        s = [sample("EXTENDED_SYS_STATE", t, vtol_state=v, landed_state=ls) for t, v, ls in states]
        s += [sample("EXTENDED_SYS_STATE", 80, vtol_state=MC, landed_state=flying)]
        for i in range(800):
            t = i * 0.1
            up = 30.0 - (8.0 if 22 <= t <= 26 else 0.0)
            speed = 20.0 if 28 <= t < 70 else (20.0 - 4 * (t - 70) if 70 <= t < 75 else 0.0)
            s.append(pos(t, 0.0, 0.0, up=up, vx=speed))
        m = metrics.compute(base_run(s, frame="quadplane"))
        self.assertAlmostEqual(m["transition_fw_time_s_1"], 8.0)
        self.assertAlmostEqual(m["transition_fw_alt_loss_m_1"], 8.0)
        self.assertAlmostEqual(pm._transition_metrics(base_run(s, frame="quadplane"))["assist_time_s"], 2.0)
        self.assertAlmostEqual(m["back_transition_time_s_1"], 4.3, places=1)
        self.assertNotIn("back_transition_time_s_2", m)    # the boot FW→MC is on the ground

    def test_noise_floor(self):
        self.assertEqual(pm._floor("back_transition_alt_loss_m_2"), 5.0)
        self.assertEqual(pm._floor("mode_yaw_rate_dps_qloiter"), 2.0)
        self.assertEqual(pm._floor("leg_xtrack_max_m"), -math.inf)

    def test_failsafe_and_landing(self):
        mission = [{"seq": 0, "kind": "home", "n": 0.0, "e": 0.0, "up": 0.0},
                   {"seq": 1, "kind": "wp", "n": -500.0, "e": 0.0, "up": 40.0},
                   {"seq": 2, "kind": "land", "n": 0.0, "e": 0.0, "up": 0.0}]
        s = [heartbeat(0, AUTO), heartbeat(20, RTL)]
        for i in range(301):
            t = i * 0.1
            x = min(-40 + 2 * t, 0.0)        # touches down 20 m short at t=10, rolls 20 m
            up = max(0.0, 10 - t) if t < 10 else 0.0
            s.append(pos(t, x, 1.5, up=up, vz=1.0 if t < 10 else 0.0, vx=2.0 if t < 20 else 0.0))
            s.append(sample("MISSION_CURRENT", t, seq=2))
        ev = [(0, "mission", mission), (0.0, "statustext", "Throttle armed"), (15, "set_param", ["X", 1]),
              (30, "statustext", "Throttle disarmed"), (30, "disarmed", None)]
        m = metrics.compute(base_run(s, ev))
        self.assertAlmostEqual(m["failsafe_reaction_s"], 5.0)
        self.assertEqual(m["failsafe_mode"], RTL)
        self.assertAlmostEqual(m["touchdown_along_m"], -20.0, places=1)
        self.assertAlmostEqual(m["rollout_m"], 20.0, places=1)
        self.assertAlmostEqual(m["touchdown_cross_m"], 1.5, places=3)
        self.assertAlmostEqual(m["touchdown_speed_mps"], 1.0)
        self.assertAlmostEqual(m["flight_time_s"], 30.0)

    def test_airspeed_only_wing_borne_above_20m(self):
        s = [pos(0, 0, 0, up=5.0), pos(10, 0, 0, up=50.0)]
        s += [sample("VFR_HUD", 1, airspeed=8.0), sample("VFR_HUD", 11, airspeed=21.0),
              sample("VFR_HUD", 12, airspeed=27.0)]
        m = metrics.compute(base_run(s, [(0.5, "takeoff_done", 40)]))
        self.assertEqual((m["airspeed_min_mps"], m["airspeed_max_mps"]), (21.0, 27.0))


class SetParamTest(unittest.TestCase):
    class Mav:
        """Telemetry that never stops; PARAM_VALUE only for names in `known`."""
        def __init__(self, known):
            self.known, self.sent, self.target_system, self.target_component = known, [], 1, 1
            self.mav = self

        def param_set_send(self, sys_, comp, name, value, kind):
            self.sent.append(name.decode())

        def recv_match(self, blocking=True, timeout=None, **kw):
            name = self.sent[-1]
            if name in self.known and len(self.sent) % 2:      # answer every other request
                return mavutil.mavlink.MAVLink_param_value_message(name.encode(), self.known[name], 9, 1, 0)
            return mavutil.mavlink.MAVLink_heartbeat_message(1, 3, 0, 0, 0, 3)

    def test_unknown_parameter_fails_instead_of_waiting(self):
        from forkpilot.sitl import Sitl
        s = Sitl()
        s.mav = self.Mav({"KNOWN": 2.0})
        s.set_param("KNOWN", 2.0, timeout=0.2)
        t = time.time()
        with self.assertRaisesRegex(RuntimeError, "not acknowledged"):
            s.set_param("NOPE", 1.0, timeout=0.2)
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(s.mav.sent.count("NOPE"), 3)


class PlaneLintTest(unittest.TestCase):
    def lint(self, text):
        d = Path(tempfile.mkdtemp())
        (d / "s.yaml").write_text("name: s\nvehicle: plane\n" + text)
        return scenario.lint_file(d / "s.yaml")

    def test_shipped_plane_scenarios_are_clean(self):
        for p in sorted((ROOT / "scenarios").glob("*plane*.yaml")):
            self.assertEqual(scenario.lint_file(p), [], p.name)

    def test_steps_and_items(self):
        bad = self.lint("frame: quadplane\nsteps:\n  - mission:\n      - vtol_takeoff: 20\n      - hover: [1, 2]\n"
                        "      - land: [1]\n      - transition: up\n  - takeoff: {alt: 20, mode: QFOO}\n"
                        "  - goto: [1, 2, 3]\n  - wait_wp: 1.5\n")
        msgs = " | ".join(i.msg for i in bad)
        for want in ("unknown mission item 'hover'", "land) must be [north, east]", "mc (hover) or fw",
                     "unknown mode 'QFOO'", "Copter GUIDED command", "whole mission item number"):
            self.assertIn(want, msgs)

    def test_vehicle_and_frame(self):
        self.assertIn("unknown vehicle 'rover'",
                      " ".join(i.msg for i in scenario.lint_file(self._w("vehicle: rover\nsteps:\n  - fly: 1\n"))))
        self.assertIn("unknown frame", " ".join(i.msg for i in self.lint("frame: tiltrotor\nsteps:\n  - fly: 1\n")))
        self.assertIn("plane scenario only",
                      " ".join(i.msg for i in scenario.lint_file(self._w("frame: quadplane\nsteps:\n  - fly: 1\n"))))

    def _w(self, text):
        d = Path(tempfile.mkdtemp())
        (d / "s.yaml").write_text("name: s\n" + text)
        return d / "s.yaml"

    def test_rules_against_the_plane_table(self):
        steps = "steps:\n  - mission: [{takeoff: 40}, {wp: [500, 0, 60]}]\n  - takeoff: 40\n  - mode: LOITER\n  - fly: 60\n"
        ok = self.lint(steps + "expect:\n  leg_xtrack_max_m: {lt: 30}\n  loiter_radius_m_loiter: {lt: 120}\n"
                       "  mode_speed_mps_loiter2: {gt: 10}\n  airspeed_min_mps: {gt: 10}\n")
        self.assertEqual(ok, [])
        bad = self.lint(steps + "expect:\n  transition_fw_time_s_1: {lt: 9}\n  flight_time_s: {lt: 9}\n"
                        "  mode_speed_mps_poshold: {lt: 9}\n  xtrack: {lt: 1}\n")
        msgs = [i.msg for i in bad]
        self.assertEqual(len(msgs), 4)
        self.assertIn("frame: quadplane", msgs[0])
        self.assertIn("wait_disarm", msgs[1])
        self.assertIn("not a Plane flight mode", msgs[2])
        self.assertIn("unknown metric", msgs[3])

    def test_plane_step_table_follows_the_runner(self):
        self.assertEqual(set(scenario.plane_step_names()), set(scenario.PLANE_STEPS))
        for name, st in scenario.PLANE_STEPS.items():
            with self.subTest(name):
                self.assertEqual(self.lint(f"steps:\n  - takeoff: 30\n  - {st.example}\n"), [])

    def test_metric_table_covers_plane_metrics(self):
        src = (ROOT / "forkpilot" / "plane_metrics.py").read_text()
        used = set(re.findall(r'm\[f?"([a-z_]+?)_?(?:\{[a-z]+\})?"\]', src))
        used |= set(re.findall(r'"([a-z_]+_(?:s|m|mps))": ', src)) - {"x"}
        used |= {"mode_speed_mps", "mode_yaw_rate_dps", "completed"}     # from metrics._mode_metrics, compute
        known = {re.sub(r"_?<\w+>$", "", n) for n, *_ in scenario.PLANE_METRICS}
        self.assertEqual(used - known - {"seq", "kind", "n", "e", "up"}, set())


if __name__ == "__main__":
    unittest.main()
