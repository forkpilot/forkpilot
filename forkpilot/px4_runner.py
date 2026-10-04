"""PX4 scenarios: SIH SITL flights in ForkPilot's step vocabulary, recorded in the same Run format.

    autopilot: px4                   # flies with this runner; frame: quadx (default)
    steps:
      - takeoff: 10                  # arm, MAV_CMD_NAV_TAKEOFF (AUTO.TAKEOFF), wait for 95% of 10 m
      - hold: 20                     # seconds of sim time
      - mode: POSCTL                 # PX4 names: POSCTL, ALTCTL, AUTO.MISSION, AUTO.LOITER, AUTO.RTL ...
      - sticks: {pitch: 1300}        # MANUAL_CONTROL, written as RC PWM like the ArduPilot steps
      - release: 15                  # roll, pitch, yaw centred; watch the stop
      - goto: [20, 0, 10]            # MAV_CMD_DO_REPOSITION (north, east, up from home); wait for it
      - send_goto: [100, 0, 15]      # the same, without waiting
      - velocity: {vx: 3, frame: body, seconds: 8}   # OFFBOARD velocity setpoints, then AUTO.LOITER
      - mission: [[30, 0, 15], [30, 30, 15]]         # waypoints (+ RTL), or items as for Plane
      - set_param: {SIM_BAT_DRAIN: 20}
      - rc_loss: true                # stop sending MANUAL_CONTROL (false: resume)
      - link_loss: true              # stop sending anything, GCS heartbeat included
      - wait_disarm: 120

Steps shared with ArduPilot keep their meaning and event names, so metrics, timelines and reports
read PX4 runs unchanged where the quantity is the same.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import yaml

from .px4_sitl import SPEED_STRETCHED, Px4Sitl, decode_mode, float_bits, from_bits
from .runner import M, Run, Runner, ScenarioError

RECORD = {"ATTITUDE", "LOCAL_POSITION_NED", "GLOBAL_POSITION_INT", "HEARTBEAT", "STATUSTEXT", "VFR_HUD",
          "POSITION_TARGET_LOCAL_NED", "ATTITUDE_TARGET", "SYS_STATUS", "EXTENDED_SYS_STATE",
          "MISSION_CURRENT", "BATTERY_STATUS", "HOME_POSITION"}
# GCS link of px4-rc.mavlink streams a lot at 50 Hz; what we do not record is switched off
STREAMS = {M.MAVLINK_MSG_ID_ATTITUDE: 25, M.MAVLINK_MSG_ID_LOCAL_POSITION_NED: 25,
           M.MAVLINK_MSG_ID_GLOBAL_POSITION_INT: 10, M.MAVLINK_MSG_ID_VFR_HUD: 5,
           M.MAVLINK_MSG_ID_POSITION_TARGET_LOCAL_NED: 10, M.MAVLINK_MSG_ID_ATTITUDE_TARGET: 10,
           M.MAVLINK_MSG_ID_EXTENDED_SYS_STATE: 5, M.MAVLINK_MSG_ID_HEARTBEAT: 5,
           M.MAVLINK_MSG_ID_MISSION_CURRENT: 2, M.MAVLINK_MSG_ID_SYS_STATUS: 2,
           M.MAVLINK_MSG_ID_BATTERY_STATUS: 1, M.MAVLINK_MSG_ID_HOME_POSITION: 1,
           M.MAVLINK_MSG_ID_ATTITUDE_QUATERNION: 0, M.MAVLINK_MSG_ID_SERVO_OUTPUT_RAW: 0,
           M.MAVLINK_MSG_ID_RC_CHANNELS: 0, M.MAVLINK_MSG_ID_OPTICAL_FLOW_RAD: 0}
MANUAL_MODES = {"MANUAL", "ALTCTL", "POSCTL", "STABILIZED", "ACRO", "POSCTL.ORBIT", "POSCTL.SLOW"}
# mission items beyond plain waypoints, as in Plane scenarios
ITEMS = {"takeoff": M.MAV_CMD_NAV_TAKEOFF, "wp": M.MAV_CMD_NAV_WAYPOINT, "land": M.MAV_CMD_NAV_LAND,
         "rtl": M.MAV_CMD_NAV_RETURN_TO_LAUNCH, "loiter_time": M.MAV_CMD_NAV_LOITER_TIME,
         "speed": M.MAV_CMD_DO_CHANGE_SPEED}
GCS_PERIOD, MANUAL_PERIOD, OFFBOARD_PERIOD = 1.0, 0.05, 0.1     # sim seconds


def stick_axes(rc: dict) -> tuple[int, int, int, int]:
    """RC PWM (as a transmitter: pitch low = forward, throttle 1000-2000) to MANUAL_CONTROL x, y, z, r.
    Unset sticks are centred; throttle centred is PX4's hold-height position."""
    def c(ch):
        return max(-1000, min(1000, int(round((rc.get(ch, 1500) - 1500) * 2))))
    return -c(2), c(1), max(0, min(1000, rc.get(3, 1500) - 1000)), c(4)


def offset(home: tuple[float, float, float], n: float, e: float) -> tuple[float, float]:
    lat0, lon0, _ = home
    return lat0 + n / 111319.5, lon0 + e / (111319.5 * math.cos(math.radians(lat0)))


def mission_items(spec: list, home: tuple[float, float, float]) -> tuple[list[tuple], list[dict]]:
    """MAVLink items for PX4 (no home item: seq 0 is the first thing flown) and, for metrics, what
    each asks for, numbered from 1 as in ArduPilot missions. Bare [n, e, up] are waypoints; a list
    of only those ends with an RTL."""
    plain = all(isinstance(x, (list, tuple)) for x in spec)
    spec = [{"wp": list(x)} if isinstance(x, (list, tuple)) else x for x in spec]
    if plain:
        spec.append({"rtl": True})
    items, info = [], [{"seq": 0, "kind": "home", "n": 0.0, "e": 0.0, "up": 0.0}]
    for seq, entry in enumerate(spec):
        (kind, arg), = entry.items()
        p, n, e, up = [0.0] * 4, None, None, 0.0
        if kind == "takeoff":
            up = float(arg)
        elif kind == "wp":
            n, e, up = map(float, arg)
        elif kind == "loiter_time":
            n, e, up = map(float, arg["at"])
            p[0] = float(arg["seconds"])
        elif kind == "land":
            n, e = map(float, arg[:2])
        elif kind == "speed":
            p[0], p[1], p[2] = 1.0, float(arg), -1.0          # ground speed, value, throttle unchanged
        elif kind != "rtl":
            raise ScenarioError(f"unknown mission item {kind!r}")
        if n is not None:
            lat, lon = offset(home, n, e)
        else:
            lat, lon = (math.nan, math.nan) if kind == "takeoff" else (0.0, 0.0)
        x, y = (int(lat * 1e7), int(lon * 1e7)) if not math.isnan(lat) else (0, 0)
        p4 = math.nan if kind in ("wp", "takeoff", "land") else 0.0      # yaw: PX4 chooses
        frame = M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT if kind in ("takeoff", "wp", "land", "loiter_time") else M.MAV_FRAME_MISSION
        items.append((seq, frame, ITEMS[kind], 0, 1, *p[:3], p4, x, y, up))
        info.append({"seq": seq + 1, "kind": kind, "n": n, "e": e, "up": up})
    return items, info


class Px4Runner(Runner):
    def __init__(self, sitl: Px4Sitl, run: Run):
        super().__init__(sitl, run)
        self.home = None              # (lat, lon, amsl) from HOME_POSITION
        self.home_local = (0.0, 0.0, 0.0)     # its x, y, z in the local frame
        self.manual = False           # MANUAL_CONTROL is being sent
        self.rc_lost = self.link_lost = False
        self._gcs_sent = self._manual_sent = -1.0
        self._offboard = None         # velocity setpoint re-sent while OFFBOARD is wanted
        self._reached = None

    # --- telemetry pump --------------------------------------------------

    def pump(self, timeout=1.0):
        msg = self.s.mav.recv_match(blocking=True, timeout=timeout)
        if msg is None:
            return None
        kind = msg.get_type()
        if msg.get_srcSystem() != self.s.mav.target_system or kind == "BAD_DATA":
            return msg
        if hasattr(msg, "time_boot_ms"):
            self.t = msg.time_boot_ms / 1000
        if kind == "HEARTBEAT" and msg.get_srcComponent() == 1:
            self.armed = bool(msg.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED)
            self.mode = decode_mode(msg.custom_mode)
        elif kind == "HOME_POSITION":
            self.home = (msg.latitude / 1e7, msg.longitude / 1e7, msg.altitude / 1000)
            self.home_local = (msg.x, msg.y, msg.z)
        elif kind == "STATUSTEXT":
            self.event("statustext", msg.text.strip())
        elif kind == "MISSION_ITEM_REACHED" and msg.seq + 1 != self._reached:
            # numbered from 1, as ArduPilot's (0 is home); PX4 repeats the last one at 1 Hz
            self._reached = msg.seq + 1
            self.event("wp_reached", self._reached)
        if kind in RECORD:
            d = msg.to_dict()
            d["t"] = self.t
            self.run.samples.append(d)
        self._keepalive()
        return msg

    def _keepalive(self):
        """Everything a GCS sends periodically, paced in sim time."""
        if self.link_lost:
            return
        if self.t - self._gcs_sent >= GCS_PERIOD or self.t < self._gcs_sent:
            self.s.heartbeat()
            self._gcs_sent = self.t
        if self.manual and not self.rc_lost and (self.t - self._manual_sent >= MANUAL_PERIOD
                                                  or self.t < self._manual_sent):
            self._send_rc()
        if self._offboard and (self.t - self._offboard[0] >= OFFBOARD_PERIOD or self.t < self._offboard[0]):
            self._offboard[0] = self.t
            self._offboard[1]()

    def _send_rc(self):
        x, y, z, r = stick_axes(self.rc)
        self.s.mav.mav.manual_control_send(self.s.mav.target_system, x, y, z, r, 0)
        self._manual_sent = self.t

    def ready(self, params: dict | None = None):
        # rcS stretched these timeouts for the speedup; set before anything else can time out
        for k, v in {**SPEED_STRETCHED, **(params or {})}.items():
            self.set_param(k, v)

        def ok():
            st, g = self.latest("SYS_STATUS"), self.latest("GLOBAL_POSITION_INT")
            return (self.home is not None and g and g["lat"] != 0 and st
                    and st["onboard_control_sensors_health"] & M.MAV_SYS_STATUS_PREARM_CHECK)
        self.wait(ok, 120, "position estimate, home and pre-arm checks")
        self.event("ready")

    def pause(self, seconds: float):
        end = self.t + seconds
        self.wait(lambda: self.t >= end, seconds + 5, "pause")

    def _param(self, name: str, send, want=lambda msg: True) -> dict | None:
        """Send (up to 3 times) and pump until a PARAM_VALUE of `name` that `want` accepts arrives: in
        flight the keepalive must go on, a parameter exchange outside the pump would starve
        MANUAL_CONTROL. A try lasts 5 s of sim time and at least 1 s of wall time (at a high speedup
        PX4 answers its first requests late), and a late answer to an earlier request is skipped."""
        for _ in range(3):
            send()
            start, t0, empty = None, time.monotonic(), 0
            while (start is None or self.t - start < 5 or time.monotonic() - t0 < 1) and empty < 5:
                msg = self.pump()
                if msg is None:
                    empty += 1
                    continue
                start = self.t if start is None else start     # sim time of the first answer to come
                if msg.get_type() == "PARAM_VALUE" and msg.param_id == name and want(msg):
                    return msg
        return None

    def step_set_param(self, params):
        for k, v in params.items():
            self.set_param(k, v)
            self.event("set_param", [k, v])

    def set_param(self, k: str, v: float):
        """Within a deadline: an unknown name is an error, not a hang."""
        mav = self.s.mav
        got = self._param(k, lambda: mav.mav.param_request_read_send(
            mav.target_system, mav.target_component, k.encode(), -1))
        if got is None:
            raise ScenarioError(f"parameter {k} not found (does this firmware have it?)")
        ptype = got.param_type
        got = self._param(k, lambda: mav.mav.param_set_send(
            mav.target_system, mav.target_component, k.encode(), float_bits(v, ptype), ptype),
            lambda m: abs(from_bits(m.param_value, m.param_type) - float(v)) <= 1e-4 * max(1, abs(v)))
        if got is None:
            raise ScenarioError(f"could not set {k}={v}: not acknowledged")

    # --- steps -----------------------------------------------------------

    def arm(self):
        for _ in range(10):
            self.s.command(M.MAV_CMD_COMPONENT_ARM_DISARM, 1)
            try:
                self.wait(lambda: self.armed, 3, "arm")
                break
            except ScenarioError:
                continue
        else:
            raise ScenarioError("could not arm")
        self.event("armed")

    def step_takeoff(self, alt):
        alt = float(alt)
        self.arm()
        self.event("takeoff_cmd", alt)
        self.s.command(M.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, math.nan, math.nan, math.nan, self.home[2] + alt)
        # done when the climb has ended near the height: PX4 sets home again at arming, after the
        # target was sent (older trees: 0.1 m higher), so the vehicle may stop a little short of it
        def done():
            g, p = self.latest("GLOBAL_POSITION_INT") or {}, self.latest("LOCAL_POSITION_NED") or {}
            return g.get("relative_alt", 0) > alt * 900 and abs(p.get("vz", 1)) < 0.2
        self.wait(done, 60, f"takeoff to {alt} m")
        self.event("takeoff_done", alt)

    def step_mode(self, mode):
        if mode in MANUAL_MODES and not self.manual:
            # PX4 refuses a pilot mode without manual control input: centre the sticks first
            self.manual = True
            self._send_rc()
            self.pause(1.0)
        # PX4 refuses a mode whose requirements it does not see met yet ("currently not
        # available"): ask again every 2 s
        for _ in range(4):
            self.s.set_mode(mode)
            try:
                self.wait(lambda: self.mode == mode, 2.5, mode)
                break
            except ScenarioError:
                continue
        self.wait(lambda: self.mode == mode, 0.1, mode)
        if self._offboard and mode != "OFFBOARD":
            self._offboard = None
        self.event("mode", mode)

    def step_sticks(self, sticks):
        self.manual = True
        super().step_sticks(sticks)

    def _reposition(self, target):
        n, e, up = map(float, target)
        lat, lon = offset(self.home, n, e)
        self.s.mav.mav.command_int_send(
            self.s.mav.target_system, self.s.mav.target_component, M.MAV_FRAME_GLOBAL, M.MAV_CMD_DO_REPOSITION,
            0, 0, -1, M.MAV_DO_REPOSITION_FLAGS_CHANGE_MODE, 0, math.nan,
            int(lat * 1e7), int(lon * 1e7), self.home[2] + up)
        return n, e, up

    def step_goto(self, target):
        n, e, up = self._reposition(target)
        self.event("goto", [n, e, up])

        def arrived():         # home need not be the local origin (older trees: 1.4 m below it)
            p, (hx, hy, hz) = self.latest("LOCAL_POSITION_NED"), self.home_local
            return p and math.dist((p["x"] - hx, p["y"] - hy, hz - p["z"]), (n, e, up)) < 1.0
        self.wait(arrived, 90, f"reach {target}")
        self.event("arrived", [n, e, up])

    def step_send_goto(self, target):
        self.event("send_goto", list(self._reposition(target)))

    VEL_FRAMES = {"local": M.MAV_FRAME_LOCAL_NED, "body": M.MAV_FRAME_BODY_NED}

    def step_velocity(self, spec):
        """OFFBOARD velocity {vx, vy, vz (down), frame: local|body, seconds}: setpoints stream at
        10 Hz sim time (PX4 needs them before it enters OFFBOARD); then back to AUTO.LOITER."""
        frame = self.VEL_FRAMES[spec.get("frame", "local")]
        v = [float(spec.get(k, 0)) for k in ("vx", "vy", "vz")]
        mask = 0b0000111111000111  # velocity only (yaw and yaw rate ignored: heading kept)
        mav = self.s.mav

        def send():
            mav.mav.set_position_target_local_ned_send(0, mav.target_system, mav.target_component, frame,
                                                       mask, 0, 0, 0, *v, 0, 0, 0, 0, 0)
        self._offboard = [self.t, send]
        send()
        self.pause(0.5)
        self.event("velocity", {"frame": spec.get("frame", "local"), "v": v})
        self.s.set_mode("OFFBOARD")
        self.wait(lambda: self.mode == "OFFBOARD", 10, "OFFBOARD")
        end = self.t + spec["seconds"]
        self.wait(lambda: self.t >= end, spec["seconds"] + 5, "velocity")
        self.event("velocity_end")
        self.s.set_mode("AUTO.LOITER")
        self.wait(lambda: self.mode == "AUTO.LOITER", 10, "AUTO.LOITER")
        self._offboard = None

    def step_yaw(self, heading):
        raise ScenarioError("yaw is not supported on PX4 (no CONDITION_YAW); use a goto")

    def step_mission(self, spec):
        items, info = mission_items(spec, self.home)
        mav = self.s.mav
        mav.mav.mission_count_send(mav.target_system, mav.target_component, len(items),
                                   M.MAV_MISSION_TYPE_MISSION)
        start = self.t
        while True:
            msg = self.pump(timeout=5)
            if msg is None or self.t - start > 30:
                raise ScenarioError("mission upload timed out")
            kind = msg.get_type()
            if kind == "MISSION_ACK" and msg.mission_type == M.MAV_MISSION_TYPE_MISSION:
                if msg.type != M.MAV_MISSION_ACCEPTED:
                    raise ScenarioError(f"mission rejected ({msg.type})")
                break
            if kind in ("MISSION_REQUEST_INT", "MISSION_REQUEST") and msg.mission_type == M.MAV_MISSION_TYPE_MISSION:
                if not 0 <= msg.seq < len(items):
                    raise ScenarioError(f"mission upload: vehicle asked for item {msg.seq} "
                                        f"of {len(items)}")
                seq, frame, cmd, cur, auto, p1, p2, p3, p4, x, y, z = items[msg.seq]
                mav.mav.mission_item_int_send(mav.target_system, mav.target_component, seq, frame,
                                              cmd, cur, auto, p1, p2, p3, p4, x, y, z,
                                              M.MAV_MISSION_TYPE_MISSION)
        self.event("mission", info)

    def step_rc_loss(self, lost):
        """Stop (true) or resume (false) the pilot's MANUAL_CONTROL stream."""
        self.rc_lost = bool(lost)
        self.event("rc_loss", self.rc_lost)

    def step_link_loss(self, lost):
        """Stop (true) or resume (false) everything the ground station sends, heartbeat included."""
        self.link_lost = bool(lost)
        self.event("link_loss", self.link_lost)


def run_scenario(path: Path, speedup: int = 10, instance: int = 0, extra_params: dict | None = None,
                 binary: Path | None = None, ardupilot: Path | None = None, **_) -> Run:
    spec = yaml.safe_load(Path(path).read_text())
    run = Run(scenario=spec["name"])
    run.vehicle, run.frame = "px4", spec.get("frame") or "quadx"
    params = {**(extra_params or {}), **spec.get("params", {})}
    if binary is None and ardupilot:        # the firmware checkout: a PX4 tree built in place
        binary = Path(ardupilot) / "build" / "px4_sitl_default" / "bin" / "px4"
    if binary is None or not Path(binary).exists():
        raise ValueError(f"no px4 binary at {binary}: build it or pass --binary")
    with Px4Sitl(binary, frame=run.frame, speedup=speedup, instance=instance, streams=STREAMS) as sitl:
        r = Px4Runner(sitl, run)
        try:
            r.ready(params)
            for step in spec["steps"]:
                (name, arg), = step.items()
                getattr(r, f"step_{name}")(arg)
        except ScenarioError as e:
            run.ok, run.error = False, str(e)
            r.event("error", str(e))
        except RuntimeError as e:          # a parameter the firmware does not have, mid-flight
            run.ok, run.error = False, str(e)
            r.event("error", str(e))
    return run
