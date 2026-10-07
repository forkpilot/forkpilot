"""Copter mission items (spline, loiter, delay, speed, yaw, land) and parameters read at boot."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pymavlink import mavutil

from forkpilot import scenario
from forkpilot.runner import ScenarioError, copter_mission_items
from forkpilot.sitl import Sitl

M = mavutil.mavlink


def lint(text: str):
    d = Path(tempfile.mkdtemp())
    (d / "s.yaml").write_text(text)
    return [i for i in scenario.lint_file(d / "s.yaml") if i.level == "error"]


class ItemsTest(unittest.TestCase):
    def test_plain_waypoints_get_rtl(self):
        items = copter_mission_items([[40, 0, 20], [40, 40, 20]])
        self.assertEqual([i[2] for i in items], [M.MAV_CMD_NAV_WAYPOINT] * 3 + [M.MAV_CMD_NAV_RETURN_TO_LAUNCH])
        self.assertEqual([i[0] for i in items], [0, 1, 2, 3])

    def test_kinds(self):
        items = copter_mission_items([[30, 0, 20], {"speed": 8}, {"spline": [60, 20, 25]}, {"yaw": 270},
                                      {"delay": 3}, {"loiter_turns": {"at": [0, 60, 20], "turns": 2, "radius": 15}},
                                      {"loiter_time": {"at": [0, 30, 20], "seconds": 5}}, {"land": [0, 0]}])
        cmds = [i[2] for i in items]
        self.assertEqual(cmds, [M.MAV_CMD_NAV_WAYPOINT, M.MAV_CMD_NAV_WAYPOINT, M.MAV_CMD_DO_CHANGE_SPEED,
                                M.MAV_CMD_NAV_SPLINE_WAYPOINT, M.MAV_CMD_CONDITION_YAW, M.MAV_CMD_NAV_DELAY,
                                M.MAV_CMD_NAV_LOITER_TURNS, M.MAV_CMD_NAV_LOITER_TIME, M.MAV_CMD_NAV_LAND])
        p = {i[2]: i[5:9] for i in items}
        self.assertEqual(p[M.MAV_CMD_DO_CHANGE_SPEED], (1, 8.0, -1, 0))
        self.assertEqual(p[M.MAV_CMD_NAV_LOITER_TURNS], (2.0, 0, 15.0, 0))
        self.assertEqual(p[M.MAV_CMD_NAV_LOITER_TIME][0], 5.0)
        self.assertEqual(p[M.MAV_CMD_NAV_DELAY], (3.0, -1, -1, -1))
        self.assertEqual(items[3][-1], 25.0)            # spline height
        self.assertEqual(items[5][9:], (0, 0, 0.0))     # no location: a spline must not aim at it
        self.assertEqual(items[2][9:], (0, 0, 0.0))
        with self.assertRaises(ScenarioError):
            copter_mission_items([{"jump": 1}])


class LintTest(unittest.TestCase):
    HEAD = "name: s\nsteps:\n  - mission:\n"
    TAIL = "  - takeoff: 10\n  - mode: AUTO\n  - wait_disarm: 100\n"

    def test_good(self):
        self.assertEqual(lint(self.HEAD + "      - [10, 0, 10]\n      - spline: [20, 0, 10]\n"
                              "      - loiter_time: {at: [0, 0, 10], seconds: 3}\n      - land: [0, 0]\n"
                              + self.TAIL), [])

    def test_bad_items(self):
        msgs = [i.msg for i in lint(self.HEAD + "      - jump: 3\n      - loiter_time: {at: [0, 0, 10]}\n"
                                    "      - yaw: 400\n      - speed: 0\n" + self.TAIL)]
        self.assertTrue(any("unknown mission item 'jump'" in m for m in msgs), msgs)
        self.assertTrue(any("loiter_time" in m and "mapping like" in m for m in msgs), msgs)
        self.assertTrue(any("heading must be <= 360" in m for m in msgs), msgs)
        self.assertTrue(any("ground speed must be > 0" in m for m in msgs), msgs)

    def test_boot_params_checked(self):
        msgs = [i.msg for i in lint("name: s\nboot_params: {flow_type: 10}\nsteps:\n  - takeoff: 5\n")]
        self.assertTrue(any("boot_params" in m and "not a parameter name" in m for m in msgs), msgs)


class BootParamsTest(unittest.TestCase):
    def test_extra_defaults_file(self):
        s = Sitl(binary_path=Path("/nonexistent/arducopter"), defaults="/d/copter.parm",
                 boot_params={"FLOW_TYPE": 10, "RNGFND1_TYPE": 1})
        with mock.patch("subprocess.Popen", side_effect=RuntimeError("stop")) as popen:
            with self.assertRaises(RuntimeError):
                s.start()
        cmd = popen.call_args[0][0]
        defaults = cmd[cmd.index("--defaults") + 1].split(",")
        self.assertEqual(defaults[0], "/d/copter.parm")
        self.assertEqual(Path(defaults[1]).read_text(), "FLOW_TYPE 10\nRNGFND1_TYPE 1\n")


if __name__ == "__main__":
    unittest.main()
