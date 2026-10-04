"""PX4 support without PX4: mode encoding, ports, parameters, build cache layout, mission items,
sticks, lint and metrics on synthetic telemetry."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pymavlink import mavutil

from forkpilot import battery, px4_build, px4_metrics as pm, scenario, suites, vehicles
from forkpilot.px4_runner import mission_items, stick_axes
from forkpilot.px4_sitl import MODES, decode_mode, encode_mode, float_bits, from_bits, ports

M = mavutil.mavlink
ROOT = Path(__file__).resolve().parents[1]
HOME = (-35.363261, 149.165230, 584.0)


def sample(kind, t, **kw):
    return {"mavpackettype": kind, "t": t, **kw}


def pos(t, x, y, up=10.0, vx=0.0, vy=0.0, vz=0.0):
    return sample("LOCAL_POSITION_NED", t, x=x, y=y, z=-up, vx=vx, vy=vy, vz=vz)


def hb(t, mode):
    return sample("HEARTBEAT", t, type=M.MAV_TYPE_QUADROTOR, autopilot=M.MAV_AUTOPILOT_PX4,
                  custom_mode=encode_mode(mode))


class ModeTest(unittest.TestCase):
    def test_encoding(self):
        self.assertEqual(encode_mode("POSCTL"), 3 << 16)
        self.assertEqual(encode_mode("AUTO.RTL"), (4 << 16) | (5 << 24))
        for name in MODES:
            self.assertEqual(decode_mode(encode_mode(name)), name)
        self.assertEqual(decode_mode((3 << 16) | (7 << 24)), "POSCTL")      # unknown sub: the main mode
        self.assertEqual(decode_mode(9 << 16), "9.0")

    def test_ports_never_shared(self):
        self.assertEqual(ports(3)["gcs"], 18573)
        seen = [p for i in range(10) for p in ports(i).values()]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertNotIn(14550, seen)       # where every PX4 talks before it hears a GCS

    def test_int_params_go_bytewise(self):
        f = float_bits(2, M.MAV_PARAM_TYPE_INT32)
        self.assertNotEqual(f, 2.0)
        self.assertEqual(from_bits(f, M.MAV_PARAM_TYPE_INT32), 2)
        self.assertEqual(float_bits(0.5, M.MAV_PARAM_TYPE_REAL32), 0.5)

    def test_vehicle_and_autopilot(self):
        self.assertEqual(vehicles.for_autopilot("px4", "copter"), "px4")
        self.assertEqual(vehicles.for_autopilot("ardupilot", "plane"), "plane")
        self.assertEqual(vehicles.scenario_vehicle(ROOT / "scenarios" / "px4_hover.yaml"), "px4")
        self.assertEqual(vehicles.of_binary(Path("x/bin/px4")).name, "px4")
        self.assertEqual([p.stem for p in battery.scenario_paths(vehicle="px4")],
                         sorted(p.stem for p in (ROOT / "scenarios").glob("px4_*.yaml")))

    def test_fingerprint_follows_the_px4_harness(self):
        s = suites.Scenarios(ROOT, "HEAD", vehicle="px4")
        with mock.patch.object(Path, "read_bytes", lambda p: b"x" if p.name == "px4_runner.py" else b""):
            a = s.fingerprint(None)
        with mock.patch.object(Path, "read_bytes", lambda p: b"y" if p.name == "px4_runner.py" else b""):
            self.assertNotEqual(a, s.fingerprint(None))


class BuildCacheTest(unittest.TestCase):
    def test_save_keeps_bin_and_etc_only(self):
        d = Path(tempfile.mkdtemp())
        b = d / "build"
        (b / "bin").mkdir(parents=True)
        (b / "bin" / "px4").write_bytes(b"\x7fELF not really")
        os.symlink("px4", b / "bin" / "px4-commander")
        (b / "etc" / "init.d-posix").mkdir(parents=True)
        (b / "etc" / "init.d-posix" / "rcS").write_text("#!/bin/sh\n")
        (b / "src").mkdir()
        with mock.patch.object(px4_build, "CACHE", d / "cache"):
            dest = px4_build.cache_dir("abc")
            px4_build.save(b, dest)
            self.assertEqual(px4_build.binary_of("abc"), d / "cache" / "abc" / "px4" / "bin" / "px4")
        self.assertEqual(sorted(x.name for x in dest.iterdir()), ["bin", "etc"])
        self.assertTrue((dest / "bin" / "px4-commander").is_symlink())
        self.assertTrue((dest / "etc" / "init.d-posix" / "rcS").exists())
        self.assertEqual([x.name for x in dest.parent.iterdir()], ["px4"])     # no temporary left


class StepsTest(unittest.TestCase):
    def test_sticks(self):
        self.assertEqual(stick_axes({}), (0, 0, 500, 0))
        self.assertEqual(stick_axes({2: 1250}), (500, 0, 500, 0))          # pitch low is forward
        self.assertEqual(stick_axes({1: 2000, 3: 1000, 4: 1100}), (0, 1000, 0, -800))

    def test_mission_items(self):
        items, info = mission_items([[40, 0, 20], [40, 40, 20]], HOME)
        self.assertEqual([i[2] for i in items], [M.MAV_CMD_NAV_WAYPOINT] * 2 + [M.MAV_CMD_NAV_RETURN_TO_LAUNCH])
        self.assertEqual([i[0] for i in items], [0, 1, 2])
        self.assertEqual(items[2][1], M.MAV_FRAME_MISSION)
        self.assertEqual([(x["seq"], x["kind"]) for x in info], [(0, "home"), (1, "wp"), (2, "wp"), (3, "rtl")])
        self.assertGreater(items[0][9], int(HOME[0] * 1e7))         # 40 m north
        items, info = mission_items([{"takeoff": 10}, {"speed": 5}, {"land": [0, 0]}], HOME)
        self.assertEqual(len(items), 3)                             # not only waypoints: no RTL added
        self.assertEqual(items[1][1], M.MAV_FRAME_MISSION)
        self.assertEqual(items[1][6], 5.0)


class Px4LintTest(unittest.TestCase):
    def lint(self, text):
        d = Path(tempfile.mkdtemp())
        (d / "s.yaml").write_text("name: s\nautopilot: px4\n" + text)
        return [i.msg for i in scenario.lint_file(d / "s.yaml")]

    def test_shipped_px4_scenarios_are_clean(self):
        for p in sorted((ROOT / "scenarios").glob("px4_*.yaml")):
            self.assertEqual(scenario.lint_file(p), [], p.name)

    def test_vocabulary(self):
        msgs = " | ".join(self.lint("steps:\n  - takeoff: 10\n  - yaw: 90\n  - mode: GUIDED\n  - rc_loss: 1\n"
                                    "  - mission: [{wp: [1, 2, 3]}, {orbit: 3}]\n  - wait_disarm: 60\n"))
        for want in ("no PX4 command", "unknown mode 'GUIDED'", "rc_loss takes true or false",
                     "unknown mission item 'orbit'"):
            self.assertIn(want, msgs)
        bad = scenario.lint_file(self._w("autopilot: inav\nsteps:\n  - fly: 1\n"))
        self.assertIn("unknown autopilot", " ".join(i.msg for i in bad))

    def _w(self, text):
        d = Path(tempfile.mkdtemp())
        (d / "s.yaml").write_text("name: s\n" + text)
        return d / "s.yaml"

    def test_rules_against_the_px4_table(self):
        steps = ("steps:\n  - takeoff: 10\n  - mode: POSCTL\n  - sticks: {pitch: 1300}\n  - fly: 5\n  - release: 10\n"
                 "  - rc_loss: true\n  - wait_disarm: 120\n")
        self.assertEqual(self.lint(steps + "expect:\n  stop_dist_m_posctl: {lt: 15}\n  failsafe_reaction_s: {lt: 9}\n"
                                   "  landing_offset_m: {lt: 3}\n  mode_speed_mps_auto_rtl: {lt: 9}\n"), [])
        msgs = self.lint(steps + "expect:\n  stop_dist_m_guided: {lt: 1}\n  mission_wp_reached: {gt: 1}\n"
                         "  hold_drift: {lt: 1}\n")
        self.assertEqual(len(msgs), 3, msgs)
        self.assertIn("not a PX4 mode", msgs[0])
        self.assertIn("a mission step", msgs[1])


class MetricsTest(unittest.TestCase):
    def run_of(self, samples, events, ok=True):
        return {"ok": ok, "vehicle": "px4", "events": events, "samples": sorted(samples, key=lambda s: s["t"])}

    def test_hover_and_landing_relative_to_home(self):
        hx, hy = 5.0, -3.0          # PX4's home need not be the local origin
        s = [sample("HOME_POSITION", 0.5, x=hx, y=hy, z=0.0), hb(0, "AUTO.LOITER"), hb(30, "AUTO.LAND")]
        s += [pos(t, hx + 0.01 * (t - 15), hy, 10.0) for t in range(15, 30)]
        s += [pos(t, hx + 0.3, hy + 0.4, max(0.0, 10.0 - (t - 30))) for t in range(31, 42)]
        s += [sample("ATTITUDE_TARGET", t, thrust=0.5, q=[1, 0, 0, 0]) for t in range(15, 30)]
        s += [sample("ATTITUDE", t, roll=0.0, pitch=0.0) for t in range(15, 30)]
        ev = [[1.0, "statustext", "Armed by external command"], [13.0, "takeoff_done", 10],
              [15.0, "hold_start", 15], [30.0, "mode", "AUTO.LAND"], [42.0, "disarmed", None]]
        self.assertEqual(pm.compute(self.run_of(s, ev))["land_drift_m"], pm.FLOORS["land_drift_m"])
        with mock.patch.dict(pm.FLOORS, clear=True):
            m = pm.compute(self.run_of(s, ev))
        self.assertAlmostEqual(m["takeoff_time_s"], 12.0)
        self.assertAlmostEqual(m["hold_drift_max_m"], 0.14, places=6)
        self.assertAlmostEqual(m["hold_thrust"], 0.5)
        self.assertAlmostEqual(m["landing_offset_m"], 0.5, places=6)
        self.assertAlmostEqual(m["flight_time_s"], 41.0)
        self.assertAlmostEqual(m["land_drift_m"], 0.0, places=6)
        self.assertEqual(m["completed"], 1.0)

    def test_failsafe_skips_the_hold(self):
        s = [hb(0, "POSCTL"), hb(10, "POSCTL"), hb(11, "AUTO.LOITER"), hb(16, "AUTO.RTL"), pos(0, 0, 0)]
        ev = [[10.2, "rc_loss", True]]
        m = pm.compute(self.run_of(s, ev))
        self.assertAlmostEqual(m["failsafe_reaction_s"], 0.8)
        self.assertAlmostEqual(m["failsafe_mode"], 4.5)
        m = pm.compute(self.run_of([hb(0, "POSCTL"), hb(20, "POSCTL"), pos(0, 0, 0)], ev))
        self.assertEqual(m["failsafe_reaction_s"], float("inf"))

    def test_failsafe_timed_by_px4s_text(self):
        s = [hb(0, "POSCTL"), hb(12, "AUTO.LOITER"), hb(16, "AUTO.RTL"), pos(0, 0, 0)]
        ev = [[10.2, "rc_loss", True], [10.8, "statustext", "Failsafe activated: entering Hold for 5 seconds"]]
        self.assertAlmostEqual(pm.compute(self.run_of(s, ev))["failsafe_reaction_s"], 0.6)

    def test_waypoints_passed_count_as_reached(self):
        _, info = mission_items([[40, 0, 20], [40, 40, 20], [0, 40, 20]], HOME)
        s = [hb(0, "AUTO.MISSION"), pos(0, 0, 0)] + [sample("MISSION_CURRENT", t, seq=q)
                                                     for t, q in ((1, 0), (2, 1), (3, 2), (4, 3), (5, 65535))]
        ev = [[0.5, "mission", info], [2, "wp_reached", 1], [3, "wp_reached", 2], [4, "wp_reached", 4]]
        self.assertEqual(pm.compute(self.run_of(s, ev))["mission_wp_reached"], 3.0)    # 3 implied, RTL not counted
