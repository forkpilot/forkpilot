"""Run a YAML scenario against SITL and record telemetry.

Scenario format:
    name: hover
    params: {SIM_WIND_SPD: 5}        # applied before arming
    steps:
      - takeoff: 10                  # metres, GUIDED mode
      - hold: 20                     # seconds of *sim* time
      - goto: [20, 0, 10]            # north, east, up (m) relative to home
      - set_param: {SIM_GPS1_ENABLE: 0}
      - mode: LAND
      - mission: [[30, 0, 15], [30, 30, 15]]   # upload waypoints (north, east, up) + RTL
      - sticks: {pitch: 1300, throttle: 1500}  # pilot RC override (PWM), held until changed
      - release: 15                  # sticks back to centre, then watch the stop for 15 s
      - yaw: 90                      # GUIDED: turn to a heading (deg)
      - velocity: {vx: 3, frame: body, seconds: 8}   # GUIDED velocity command
      - send_goto: [100, 0, 15]      # like goto, but do not wait (the target may be refused)
      - wait_disarm: 120
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pymavlink import mavutil

from .sitl import HOME, Sitl

M = mavutil.mavlink
RECORD = {"ATTITUDE", "LOCAL_POSITION_NED", "GLOBAL_POSITION_INT", "HEARTBEAT",
          "STATUSTEXT", "VFR_HUD", "EKF_STATUS_REPORT", "POSITION_TARGET_LOCAL_NED",
          "MISSION_ITEM_REACHED", "SYS_STATUS"}
STREAMS = {M.MAVLINK_MSG_ID_ATTITUDE: 25, M.MAVLINK_MSG_ID_LOCAL_POSITION_NED: 25,
           M.MAVLINK_MSG_ID_GLOBAL_POSITION_INT: 10, M.MAVLINK_MSG_ID_VFR_HUD: 5,
           M.MAVLINK_MSG_ID_EKF_STATUS_REPORT: 2,
           M.MAVLINK_MSG_ID_POSITION_TARGET_LOCAL_NED: 10}


class ScenarioError(RuntimeError):
    pass


@dataclass
class Run:
    scenario: str
    events: list = field(default_factory=list)   # (t, kind, detail)
    samples: list = field(default_factory=list)  # telemetry dicts with "t"
    ok: bool = True
    error: str | None = None

    def save(self, path: Path):
        path.write_text(json.dumps(self.__dict__))


class Runner:
    def __init__(self, sitl: Sitl, run: Run):
        self.s, self.run = sitl, run
        self.t = 0.0          # sim time, seconds
        self.t_epoch = 0.0    # sim time of scenario t=0, see step_epoch
        self.mode = None
        self.armed = False
        self.rc: dict[int, int] = {}   # pilot stick override, channel -> PWM, re-sent while set
        self._rc_sent = -1.0
        sitl.mav.handle_eof = self._closed

    def _closed(self):
        # firmware crash or SITL exit mid-flight: the scenario did not complete
        raise ScenarioError("SITL closed the MAVLink connection")

    # --- telemetry pump --------------------------------------------------

    def pump(self, timeout=1.0):
        msg = self.s.mav.recv_match(blocking=True, timeout=timeout)
        if msg is None:
            return None
        kind = msg.get_type()
        if hasattr(msg, "time_boot_ms"):
            self.t = msg.time_boot_ms / 1000
        if kind == "HEARTBEAT" and msg.get_srcComponent() == 1:
            self.armed = bool(msg.base_mode & M.MAV_MODE_FLAG_SAFETY_ARMED)
            self.mode = self.s.mav.flightmode
        if kind == "STATUSTEXT":
            self.event("statustext", msg.text)
        if kind == "MISSION_ITEM_REACHED":
            self.event("wp_reached", msg.seq)
        if kind in RECORD:
            d = msg.to_dict()
            d["t"] = self.t
            self.run.samples.append(d)
        # an override lapses after RC_OVERRIDE_TIME (3 s), so it is re-sent at 5 Hz sim time
        if self.rc and (self.t - self._rc_sent >= 0.2 or self.t < self._rc_sent):
            self._send_rc()
        return msg

    def _send_rc(self):
        ch = [self.rc.get(i, 65535) for i in range(1, 9)]   # 65535: leave channel alone
        self.s.mav.mav.rc_channels_override_send(self.s.mav.target_system,
                                                 self.s.mav.target_component, *ch)
        self._rc_sent = self.t

    def wait(self, cond, timeout_s: float, what: str):
        start = self.t
        wall = 0
        while not cond():
            if self.pump() is None:
                wall += 1
                if wall > 30:
                    raise ScenarioError(f"no telemetry while waiting for {what}")
            if self.t - start > timeout_s:
                raise ScenarioError(f"timeout waiting for {what}")

    def event(self, kind, detail=None):
        self.run.events.append((self.t, kind, detail))

    def latest(self, kind):
        for d in reversed(self.run.samples):
            if d["mavpackettype"] == kind:
                return d
        return None

    # --- steps -----------------------------------------------------------

    def ready(self):
        for msg_id, hz in STREAMS.items():
            self.s.set_message_rate(msg_id, hz)
        # EKF must be using GPS before GUIDED arming is allowed
        def ekf_ok():
            e = self.latest("EKF_STATUS_REPORT")
            return e and (e["flags"] & M.EKF_PRED_POS_HORIZ_ABS) and (e["flags"] & M.EKF_POS_HORIZ_ABS)
        self.wait(ekf_ok, 120, "EKF/GPS ready")
        self.event("ready")

    def step_takeoff(self, alt):
        self.s.set_mode("GUIDED")
        self.wait(lambda: self.mode == "GUIDED", 10, "GUIDED")
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
        # HEARTBEAT is 1 Hz, so "armed" is only accurate to a second; the takeoff command
        # time is stamped from 25 Hz telemetry and is what timing metrics measure from
        self.event("takeoff_cmd", alt)
        self.s.command(M.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, alt)
        self.wait(lambda: (self.latest("GLOBAL_POSITION_INT") or {}).get("relative_alt", 0) > alt * 950,
                  60, f"takeoff to {alt} m")
        self.event("takeoff_done", alt)

    def step_hold(self, seconds):
        end = self.t + seconds
        self.event("hold_start", seconds)
        self.wait(lambda: self.t >= end, seconds + 5, "hold")

    def step_epoch(self, _=None):
        """Scenario time 0 is now: `fly: {until: s}` and `release: {until: s}` count from here."""
        self.t_epoch = self.t

    def step_fly(self, seconds):
        """Wait while the pilot flies (sticks set); unlike hold, not judged as a hover."""
        end = self.t_epoch + seconds["until"] if isinstance(seconds, dict) else self.t + seconds
        self.wait(lambda: self.t >= end, max(end - self.t, 0) + 5, "fly")

    def step_goto(self, target):
        n, e, up = target
        mask = 0b0000111111111000  # position only
        self.s.mav.mav.set_position_target_local_ned_send(
            0, self.s.mav.target_system, self.s.mav.target_component,
            M.MAV_FRAME_LOCAL_NED, mask, n, e, -up, 0, 0, 0, 0, 0, 0, 0, 0)
        self.event("goto", [n, e, up])

        def arrived():
            p = self.latest("LOCAL_POSITION_NED")
            return p and math.dist((p["x"], p["y"], -p["z"]), (n, e, up)) < 1.0
        self.wait(arrived, 90, f"reach {target}")
        self.event("arrived", [n, e, up])

    def step_send_goto(self, target):
        """Send a position target and do not wait for arrival: the firmware may reject it
        (a fence), and what the vehicle then does is the measurement."""
        n, e, up = target
        self.s.mav.mav.set_position_target_local_ned_send(
            0, self.s.mav.target_system, self.s.mav.target_component,
            M.MAV_FRAME_LOCAL_NED, 0b0000111111111000, n, e, -up, 0, 0, 0, 0, 0, 0, 0, 0)
        self.event("send_goto", [n, e, up])

    def step_yaw(self, heading):
        """GUIDED: turn to an absolute heading (deg) and wait until within 3 degrees."""
        self.s.command(M.MAV_CMD_CONDITION_YAW, heading, 0, 1, 0)
        self.event("yaw", heading)

        def turned():
            a = self.latest("ATTITUDE")
            return a and abs((math.degrees(a["yaw"]) - heading + 180) % 360 - 180) < 3
        self.wait(turned, 30, f"yaw {heading}")

    VEL_FRAMES = {"local": M.MAV_FRAME_LOCAL_NED, "body": M.MAV_FRAME_BODY_OFFSET_NED}

    def step_velocity(self, spec):
        """GUIDED velocity command {vx, vy, vz (down), frame: local|body, seconds}, re-sent at
        2 Hz (GUIDED stops after 3 s without a new command)."""
        frame = self.VEL_FRAMES[spec.get("frame", "local")]
        v = [float(spec.get(k, 0)) for k in ("vx", "vy", "vz")]
        mask = 0b0000111111000111  # velocity only
        end, sent = self.t + spec["seconds"], -1.0
        self.event("velocity", {"frame": spec.get("frame", "local"), "v": v})
        while self.t < end:
            if self.t - sent >= 0.5 or self.t < sent:
                self.s.mav.mav.set_position_target_local_ned_send(
                    0, self.s.mav.target_system, self.s.mav.target_component, frame, mask,
                    0, 0, 0, *v, 0, 0, 0, 0, 0)
                sent = self.t
            if self.pump() is None:
                raise ScenarioError("no telemetry during velocity command")
        self.event("velocity_end")

    def step_set_param(self, params):
        for k, v in params.items():
            self.s.set_param(k, v)
            self.event("set_param", [k, v])

    def step_mode(self, mode):
        self.s.set_mode(mode)
        self.wait(lambda: self.mode == mode, 10, mode)
        self.event("mode", mode)

    def step_mission(self, waypoints):
        """Upload home + NAV_WAYPOINTs + RTL using the MAVLink mission protocol."""
        lat0, lon0 = (float(v) for v in HOME.split(",")[:2])
        def item(seq, cmd, n=0.0, e=0.0, up=0.0):
            lat = lat0 + n / 111319.5
            lon = lon0 + e / (111319.5 * math.cos(math.radians(lat0)))
            return (seq, M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, cmd, 0, 1, 0, 0, 0, 0,
                    int(lat * 1e7), int(lon * 1e7), up)
        items = [item(0, M.MAV_CMD_NAV_WAYPOINT)]
        items += [item(i + 1, M.MAV_CMD_NAV_WAYPOINT, *wp) for i, wp in enumerate(waypoints)]
        items.append(item(len(items), M.MAV_CMD_NAV_RETURN_TO_LAUNCH))
        mav = self.s.mav
        mav.mav.mission_count_send(mav.target_system, mav.target_component, len(items),
                                   M.MAV_MISSION_TYPE_MISSION)
        # wall-clock deadline: self.t does not advance in this loop, and recv_match with a type
        # filter only times out when nothing at all arrives
        deadline = time.time() + 30
        while True:
            msg = None
            while msg is None and time.time() < deadline:
                m = mav.recv_match(blocking=True, timeout=5)
                if m is not None and m.get_type() in ("MISSION_REQUEST_INT", "MISSION_REQUEST",
                                                       "MISSION_ACK"):
                    msg = m
            if msg is None:
                raise ScenarioError("mission upload timed out")
            if msg.get_type() == "MISSION_ACK":
                if msg.type != M.MAV_MISSION_ACCEPTED:
                    raise ScenarioError(f"mission rejected ({msg.type})")
                break
            if not 0 <= msg.seq < len(items):
                raise ScenarioError(f"mission upload: vehicle asked for item {msg.seq} "
                                    f"of {len(items)}")
            seq, frame, cmd, cur, auto, p1, p2, p3, p4, x, y, z = items[msg.seq]
            mav.mav.mission_item_int_send(mav.target_system, mav.target_component, seq, frame,
                                          cmd, cur, auto, p1, p2, p3, p4, x, y, z,
                                          M.MAV_MISSION_TYPE_MISSION)
        self.event("mission", len(waypoints))

    STICKS = {"roll": 1, "pitch": 2, "throttle": 3, "yaw": 4}

    def step_sticks(self, sticks):
        self.rc.update({self.STICKS[k]: int(v) for k, v in sticks.items()})
        self._send_rc()
        self.event("sticks", {k: self.rc.get(c) for k, c in self.STICKS.items()})

    def step_release(self, seconds):
        """Centre roll, pitch and yaw (throttle stays where it is) and watch the stop."""
        self.rc.update({1: 1500, 2: 1500, 4: 1500})
        self._send_rc()
        self.event("release", self.mode)
        self.step_fly(seconds)
        self.event("release_end", self.mode)

    def step_wait_disarm(self, timeout):
        self.wait(lambda: not self.armed, timeout, "disarm")
        self.event("disarmed")


def run_scenario(path: Path, ardupilot: Path | None = None, speedup: int = 10,
                 instance: int = 0, extra_params: dict | None = None,
                 binary: Path | None = None) -> Run:
    spec = yaml.safe_load(Path(path).read_text())
    run = Run(scenario=spec["name"])
    params = {**(extra_params or {}), **spec.get("params", {})}
    kw = {"ardupilot": ardupilot} if ardupilot else {}
    with Sitl(speedup=speedup, instance=instance, params=params, binary_path=binary, **kw) as sitl:
        r = Runner(sitl, run)
        try:
            r.ready()
            for step in spec["steps"]:
                (name, arg), = step.items()
                getattr(r, f"step_{name}")(arg)
        except ScenarioError as e:
            run.ok, run.error = False, str(e)
            r.event("error", str(e))
    return run
