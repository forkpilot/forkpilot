import tempfile
import unittest
from pathlib import Path

import yaml
from pymavlink import mavutil

from forkpilot.fromlog import (Flight, LogError, Options, compress, convert, mission_items, mode_at,
                               rc_roles, read_defaults, read_flight, select_params, snap,
                               stick_pwm, stick_series)

DATA = Path(__file__).parent / "data"
STEPS = {"takeoff", "hold", "fly", "goto", "send_goto", "yaw", "velocity", "set_param", "mode",
         "mission", "sticks", "release", "wait_disarm"}


def flight(**kw):
    """A synthetic 60 s flight: airborne at 10 m from t=0, RC samples at 10 Hz."""
    f = Flight(path="synthetic.bin", t0=0.0, t1=60.0)
    f.arms = [(0.0, True), (60.0, False)]
    f.alt = [(float(t), 10.0) for t in range(61)]
    f.yaw = [(0.0, 0.0)]
    f.modes = [(0.0, "LOITER", 1)]
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def rc(fn, t0=0.0, t1=60.0, hz=10):
    return [(t0 + i / hz, fn(t0 + i / hz)) for i in range(int((t1 - t0) * hz) + 1)]


def steps_of(res):
    doc = yaml.safe_load(res.yaml)
    return [(next(iter(s)), s[next(iter(s))]) for s in doc["steps"]], doc


class Compress(unittest.TestCase):
    def test_constant_is_one_segment(self):
        series = [(i / 10, [1500, 1500, 1500, 1500]) for i in range(100)]
        segs = compress(series, 10.0)
        self.assertEqual(len(segs), 1)
        self.assertEqual((segs[0][0], segs[0][1]), (0.0, 10.0))

    def test_step_change_splits(self):
        series = [(i / 10, [1500, 1500 if i < 50 else 1700, 1500, 1500]) for i in range(100)]
        segs = compress(series, 10.0)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0][1], 5.0)
        self.assertEqual(segs[1][2][1], 1700)

    def test_noise_inside_tolerance_is_one_segment(self):
        series = [(i / 10, [1500 + (i % 3) * 10, 1500, 1500, 1500]) for i in range(100)]
        segs = compress(series, 10.0)       # band 20 PWM, noise 20 PWM wide
        self.assertEqual(len(segs), 1)
        self.assertLessEqual(max(abs(s[1][0] - segs[0][2][0]) for s in series), 10.0)

    def test_every_sample_within_tolerance(self):
        series = [(i / 10, [1500 + 4 * i if i < 60 else 1740, 1500, 1500, 1500]) for i in range(100)]
        tol = 20
        segs = compress(series, 10.0, tol, 0.0)
        for t, v in series:
            seg = next(s for s in segs if s[0] <= t < s[1])
            self.assertLessEqual(abs(v[0] - seg[2][0]), tol + 1e-9)

    def test_blip_is_absorbed_only_inside_the_band(self):
        base = lambda i, x: (i / 10, [x, 1500, 1500, 1500])
        series = [base(i, 1500) for i in range(40)] + [base(40, 1530)] + [base(i, 1500) for i in range(41, 80)]
        self.assertEqual(len(compress(series, 8.0, 20, 0.3)), 1)       # 30 PWM blip, band 40
        series[40] = base(40, 1700)
        self.assertEqual(len(compress(series, 8.0, 20, 0.3)), 3)       # a real stick move stays


class Sticks(unittest.TestCase):
    STOCK = (1, 1000, 2000, 1500, False)

    def test_stock_mapping_is_identity(self):
        for raw in (1000, 1250, 1500, 1800, 2000):
            self.assertAlmostEqual(stick_pwm("roll", raw, self.STOCK), raw)
        self.assertAlmostEqual(stick_pwm("throttle", 1300, (3, 1000, 2000, 1500, False)), 1300)

    def test_reversed_channel_flips(self):
        self.assertAlmostEqual(stick_pwm("pitch", 1700, (2, 1000, 2000, 1500, True)), 1300)
        self.assertAlmostEqual(stick_pwm("throttle", 1100, (3, 1000, 2000, 1500, True)), 1900)

    def test_trim_and_range_are_normalised(self):
        cal = (1, 1100, 1900, 1480, False)     # radio with its own calibration
        self.assertAlmostEqual(stick_pwm("roll", 1480, cal), 1500)
        self.assertAlmostEqual(stick_pwm("roll", 1900, cal), 2000)
        self.assertAlmostEqual(stick_pwm("roll", 1100, cal), 1000)
        self.assertAlmostEqual(stick_pwm("roll", 1690, cal), 1750)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(stick_pwm("roll", 2200, self.STOCK), 2000)

    def test_rcmap_swaps_channels(self):
        params = {"RCMAP_ROLL": [(0, 2)], "RCMAP_PITCH": [(0, 1)], "RCMAP_THROTTLE": [(0, 3)],
                  "RCMAP_YAW": [(0, 4)], "RC2_REVERSED": [(0, 1)]}
        f = flight(params=params, rc=[(1.0, [1600, 1300, 1100, 1500, 0, 0, 0, 0])])
        roles = rc_roles(f, 1.0)
        self.assertEqual([roles[r][0] for r in ("roll", "pitch", "throttle", "yaw")], [2, 1, 3, 4])
        (t, v), = stick_series(f, 0, 5)
        # roll comes from RC2 (1300, reversed -> 1700), pitch from RC1 (1600)
        self.assertEqual([round(x) for x in v], [1700, 1600, 1100, 1500])

    def test_samples_without_signal_are_skipped(self):
        f = flight(rc=[(1.0, [0, 0, 0, 0, 0, 0, 0, 0]), (2.0, [1500, 1500, 1000, 1500, 0, 0, 0, 0])])
        self.assertEqual([t for t, _ in stick_series(f, 0, 5)], [2.0])

    def test_snap(self):
        self.assertEqual(snap([1512, 1488, 1003, 1493], 20), [1500, 1500, 1005, 1500])
        self.assertEqual(snap([1523, 1500, 1500, 1500], 20), [1525, 1500, 1500, 1500])


class Modes(unittest.TestCase):
    def test_commanded_changes_are_extracted_with_timing(self):
        f = flight(modes=[(0.0, "LOITER", 1), (12.0, "POSHOLD", 1), (30.0, "LAND", 2)],
                   rc=rc(lambda t: [1500, 1500, 1500, 1500, 0, 0, 0, 0]))
        steps, _ = steps_of(convert(f))
        modes = [v for k, v in steps if k == "mode"]
        self.assertEqual(modes, ["LOITER", "POSHOLD", "LAND"])

    def test_firmware_sequenced_and_failsafe_modes(self):
        f = flight(modes=[(0.0, "LOITER", 1), (20.0, "RTL", 3), (40.0, "LAND", 9)],
                   rc=rc(lambda t: [1500, 1500, 1500, 1500, 0, 0, 0, 0]))
        res = convert(f)
        steps, _ = steps_of(res)
        self.assertEqual([v for k, v in steps if k == "mode"], ["LOITER", "RTL"])   # LAND by the firmware (9) dropped
        self.assertIn("radio failsafe", res.yaml)
        self.assertTrue(any("entered by the firmware" in n for n in res.notes))

    def test_mode_at(self):
        f = flight(modes=[(0, "STABILIZE", 1), (5, "LOITER", 1)])
        self.assertEqual(mode_at(f, 4.9), "STABILIZE")
        self.assertEqual(mode_at(f, 5.0), "LOITER")

    def test_unknown_mode_is_noted(self):
        f = flight(modes=[(0.0, "LOITER", 1), (10.0, "MODE_99", 1)],
                   rc=rc(lambda t: [1500, 1500, 1500, 1500, 0, 0, 0, 0]))
        res = convert(f)
        self.assertTrue(any("unknown mode" in n for n in res.notes))


class Params(unittest.TestCase):
    def setUp(self):
        self.f = flight(params={
            "ATC_ANG_RLL_P": [(0, 6.0)], "ATC_ACCEL_R_MAX": [(0, 110000.0)],   # default 110000
            "SERIAL1_BAUD": [(0, 921.0)], "MOT_PWM_MIN": [(0, 1100.0)],
            "WPNAV_SPEED": [(0, 800.0), (30.0, 1000.0)], "PSC_VELXY_P": [(0, 2.0)],
            "RC1_DZ": [(0, 40.0)]},
            defaults={"ATC_ANG_RLL_P": 4.5, "ATC_ACCEL_R_MAX": 110000.0, "SERIAL1_BAUD": 57.0,
                      "MOT_PWM_MIN": 0.0, "WPNAV_SPEED": 500.0, "PSC_VELXY_P": 2.0, "RC1_DZ": 20.0})

    def test_only_changed_flight_behaviour_is_kept(self):
        kept, dropped = select_params(self.f, 5.0, Options())
        self.assertEqual(kept, {"ATC_ANG_RLL_P": 6.0, "WPNAV_SPEED": 800.0, "RC1_DZ": 40.0})
        self.assertEqual(dropped["not flight behaviour"], ["MOT_PWM_MIN", "SERIAL1_BAUD"])   # listed, not copied
        self.assertIn("ATC_ACCEL_R_MAX", dropped["same as the SITL default"])
        self.assertNotIn("MOT_PWM_MIN", kept)                           # hardware, matched by NEVER

    def test_value_is_taken_at_the_requested_time(self):
        kept, _ = select_params(self.f, 35.0, Options())
        self.assertEqual(kept["WPNAV_SPEED"], 1000.0)

    def test_defaults_file_wins_over_the_log_default(self):
        kept, dropped = select_params(self.f, 5.0, Options(defaults={"ATC_ANG_RLL_P": 6.0}))
        self.assertNotIn("ATC_ANG_RLL_P", kept)
        self.assertIn("ATC_ANG_RLL_P", dropped["same as the SITL default"])

    def test_keep_drop_and_all(self):
        kept, _ = select_params(self.f, 5.0, Options(keep=("SERIAL1_",), drop=("WPNAV_",)))
        self.assertIn("SERIAL1_BAUD", kept)
        self.assertNotIn("WPNAV_SPEED", kept)
        kept, _ = select_params(self.f, 5.0, Options(all_params=True))
        self.assertIn("PSC_VELXY_P", kept)

    def test_deadzone_follows_the_role_not_the_channel(self):
        f = flight(params={"RCMAP_ROLL": [(0, 2)], "RCMAP_PITCH": [(0, 1)], "RC1_DZ": [(0, 40.0)],
                           "RC2_DZ": [(0, 20.0)]}, defaults={"RC1_DZ": 20.0, "RC2_DZ": 20.0})
        kept, _ = select_params(f, 5.0, Options())
        # RC1 carries pitch in the log; the stock SITL reads pitch from RC2
        self.assertEqual(kept, {"RC2_DZ": 40.0})

    def test_unknown_default_is_listed_not_guessed(self):
        f = flight(params={"ATC_ANG_RLL_P": [(0, 6.0)]})
        kept, dropped = select_params(f, 5.0, Options())
        self.assertEqual(kept, {})
        self.assertEqual(dropped["default unknown (tlog)"], ["ATC_ANG_RLL_P"])

    def test_param_changed_in_flight_becomes_a_step(self):
        f = flight(params=self.f.params, defaults=self.f.defaults,
                   rc=rc(lambda t: [1500, 1500, 1500, 1500, 0, 0, 0, 0]))
        steps, doc = steps_of(convert(f))
        self.assertIn(("set_param", {"WPNAV_SPEED": 1000}), steps)
        self.assertEqual(doc["params"]["WPNAV_SPEED"], 800)

    def test_read_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "d.parm")
            p.write_text("# c\nATC_ANG_RLL_P 6\nFOO,2.5\nBAR=3 # x\nbad word\n")
            self.assertEqual(read_defaults(p), {"ATC_ANG_RLL_P": 6.0, "FOO": 2.5, "BAR": 3.0})


class Convert(unittest.TestCase):
    def test_stick_move_becomes_sticks_and_fly(self):
        fn = lambda t: [1500, 1250 if 10 <= t < 20 else 1500, 1500, 1500, 0, 0, 0, 0]
        res = convert(flight(rc=rc(fn)), Options(end=50.0))
        steps, doc = steps_of(res)
        self.assertEqual({k for k, _ in steps} - STEPS, set())
        self.assertEqual(steps[0][0], "takeoff")
        self.assertAlmostEqual(steps[0][1], 10.5, delta=0.1)        # 10 m seen, step returns at 95%
        self.assertIn(("sticks", {"pitch": 1250}), steps)
        waits = [v for k, v in steps if k in ("fly", "release")]
        self.assertAlmostEqual(sum(waits), 50.0, delta=1.5)

    def test_disarm_in_the_window_ends_with_wait_disarm(self):
        steps, _ = steps_of(convert(flight(rc=rc(lambda t: [1500, 1500, 1500, 1500, 0, 0, 0, 0]))))
        self.assertEqual(steps[-1][0], "wait_disarm")

    def test_long_centred_sticks_in_a_pilot_mode_are_a_release(self):
        fn = lambda t: [1500, 1250 if t < 5 else 1500, 1500, 1500, 0, 0, 0, 0]
        steps, _ = steps_of(convert(flight(rc=rc(fn)), Options(end=50.0)))
        self.assertEqual([v for k, v in steps if k == "release"], [steps[-1][1]] if steps[-1][0] == "release" else [])
        self.assertTrue(any(k == "release" for k, _ in steps))

    def test_centred_sticks_in_auto_are_no_release(self):
        f = flight(modes=[(0.0, "AUTO", 1)], rc=rc(lambda t: [1500, 1500, 1500, 1500, 0, 0, 0, 0]),
                   cmds={0: {"id": 16, "lat": 1.0, "lng": 2.0, "alt": 0, "frame": 0},
                         1: {"id": 16, "lat": 1.0004, "lng": 2.0, "alt": 20, "frame": 3}},
                   home=(1.0, 2.0, 0.0))
        steps, _ = steps_of(convert(f))
        self.assertFalse(any(k == "release" for k, _ in steps))
        self.assertEqual(steps[0][0], "mission")

    def test_window_options(self):
        fn = lambda t: [1500, 1250 if 10 <= t < 20 else 1500, 1500, 1500, 0, 0, 0, 0]
        f = flight(rc=rc(fn), modes=[(0.0, "LOITER", 1), (40.0, "LAND", 2)])
        res = convert(f, Options(start=15.0, end=30.0))
        steps, _ = steps_of(res)
        self.assertEqual((res.start, res.end), (15.0, 30.0))
        self.assertFalse(any(k == "wait_disarm" for k, _ in steps))
        self.assertNotIn("LAND", res.yaml.split("steps:")[1])
        self.assertAlmostEqual(sum(v for k, v in steps if k in ("fly", "release")), 15.0, delta=0.5)

    def test_start_before_airborne_is_clamped_with_a_note(self):
        f = flight(alt=[(float(t), min(t * 2.0, 10.0)) for t in range(61)],
                   rc=rc(lambda t: [1500, 1500, 1500, 1500, 0, 0, 0, 0]))
        res = convert(f, Options(start=0.0))
        self.assertGreaterEqual(res.start, 1.0)
        self.assertTrue(any("before the vehicle is airborne" in n for n in res.notes))

    def test_never_airborne_is_an_error(self):
        f = flight(alt=[(float(t), 0.1) for t in range(61)])
        with self.assertRaises(LogError):
            convert(f)

    def test_empty_window_is_an_error(self):
        with self.assertRaises(LogError):
            convert(flight(), Options(start=50.0, end=50.0))

    def test_output_is_valid_yaml_with_header_and_name(self):
        res = convert(flight(rc=rc(lambda t: [1500, 1500, 1500, 1500, 0, 0, 0, 0])), Options(name="Field Report #3"))
        self.assertTrue(res.yaml.startswith("# Generated by `forkpilot fromlog` from synthetic.bin"))
        doc = yaml.safe_load(res.yaml)
        self.assertEqual(doc["name"], "field_report_3")
        self.assertEqual(doc["expect"], {})

    def test_longer_tolerance_gives_fewer_segments(self):
        fn = lambda t: [1500, 1500 + 8 * ((int(t * 2)) % 11), 1500, 1500, 0, 0, 0, 0]
        f = flight(rc=rc(fn))
        fine, coarse = convert(f, Options(tol=5)), convert(f, Options(tol=60))
        self.assertGreater(fine.stats["stick_segments"], coarse.stats["stick_segments"])


class Mission(unittest.TestCase):
    def test_waypoints_relative_to_home(self):
        f = flight(home=(47.0, 8.0, 500.0), cmds={
            0: {"id": 16, "lat": 47.0, "lng": 8.0, "alt": 500.0, "frame": 0},
            1: {"id": 22, "lat": 0, "lng": 0, "alt": 20, "frame": 3},
            2: {"id": 16, "lat": 47.0004, "lng": 8.0, "alt": 20, "frame": 3},
            3: {"id": 16, "lat": 47.0004, "lng": 8.0006, "alt": 520.0, "frame": 0},
            4: {"id": 20, "lat": 0, "lng": 0, "alt": 0, "frame": 3},
            5: {"id": 183, "lat": 0, "lng": 0, "alt": 0, "frame": 3}})
        wps, notes = mission_items(f)
        self.assertEqual(wps[0][0], 44.5)
        self.assertEqual(wps[0][1:], [0.0, 20.0])
        self.assertAlmostEqual(wps[1][1], 0.0006 * 111319.5 * 0.68200, delta=0.2)
        self.assertEqual(wps[1][2], 20.0)                  # absolute frame: 520 m MSL - 500 m home
        self.assertEqual(len(notes), 1)                    # the servo command is not replayable

    def test_no_home_no_mission(self):
        self.assertEqual(mission_items(flight(cmds={1: {"id": 16, "lat": 1, "lng": 1, "alt": 5, "frame": 3}})), ([], []))


class RealLog(unittest.TestCase):
    """A 68 KB excerpt of a real ArduCopter 4.8 log: an AUTO mission over 3 waypoints."""
    path = DATA / "auto_mission_small.bin"

    def test_reads_the_dataflash_log(self):
        f = read_flight(self.path)
        self.assertEqual(f.kind, "dataflash")
        self.assertIn("ArduCopter", " ".join(f.info))
        self.assertEqual(f.modes[-1][1], "AUTO")
        self.assertTrue(f.rc and f.alt and f.params and f.home)

    def test_converts_to_a_mission_scenario(self):
        res = convert(read_flight(self.path), Options(name="auto"))
        steps, doc = steps_of(res)
        self.assertEqual({k for k, _ in steps} - STEPS, set())
        self.assertEqual(steps[0], ("mission", [[40, 0, 20], [40, 40, 20], [0, 40, 20]]))
        self.assertIn(("mode", "AUTO"), steps)
        self.assertEqual(steps[-1][0], "wait_disarm")
        self.assertAlmostEqual(res.takeoff_alt, 20.0, delta=0.5)

    def test_window_from_a_real_log(self):
        res = convert(read_flight(self.path), Options(start=70.0, end=90.0))
        self.assertEqual((res.start, res.end), (70.0, 90.0))
        steps, _ = steps_of(res)
        self.assertNotIn("wait_disarm", [k for k, _ in steps])


class Tlog(unittest.TestCase):
    def test_tlog_modes_sticks_and_params(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "x.tlog")
            mav = mavutil.mavlink.MAVLink(open(p, "wb"), srcSystem=1, srcComponent=1)
            mav.robust_parsing = True
            t0 = 1.7e9
            out = mav.file

            def put(msg, t):
                buf = msg.pack(mav)
                out.write(int(t * 1e6).to_bytes(8, "big") + buf)

            M = mavutil.mavlink
            for i in range(0, 400):
                t = t0 + i * 0.1
                if i in (0, 300):
                    put(M.MAVLink_param_value_message(b"WPNAV_SPEED", 800.0 if i == 0 else 1200.0, 9, 1, 0), t)
                if i % 10 == 0:
                    mode = 5 if i < 200 else 3          # LOITER then AUTO
                    put(M.MAVLink_heartbeat_message(M.MAV_TYPE_QUADROTOR, M.MAV_AUTOPILOT_ARDUPILOTMEGA,
                                                    128 | 1, mode, 4, 3), t)
                    put(M.MAVLink_global_position_int_message(0, 473000000, 80000000, 500000, 12000, 0, 0, 0, 0), t)
                put(M.MAVLink_rc_channels_message(*[0, 8, 1500, 1300 if i > 100 else 1500, 1500, 1500] + [0] * 14 + [255]), t)
            out.close()
            f = read_flight(p)
            self.assertEqual(f.kind, "tlog")
            self.assertEqual([m[1] for m in f.modes], ["LOITER", "AUTO"])
            self.assertAlmostEqual(f.modes[1][0], 20.0, delta=0.2)
            self.assertAlmostEqual(f.alt[0][1], 12.0)
            res = convert(f)
            steps, doc = steps_of(res)
            self.assertIn(("sticks", {"pitch": 1300}), steps)
            self.assertIn("1 have no known default", res.yaml)
            full = convert(f, Options(all_params=True, end=39.0))
            self.assertIn(("set_param", {"WPNAV_SPEED": 1200}), steps_of(full)[0])

    def test_tlog_without_copter_heartbeat_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "empty.tlog")
            p.write_bytes(b"")
            with self.assertRaises(LogError):
                read_flight(p)


if __name__ == "__main__":
    unittest.main()
