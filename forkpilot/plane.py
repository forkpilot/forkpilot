"""ArduPlane and QuadPlane scenarios: SITL launch and steps on top of the Copter runner.

Copter scenarios never pass through this module. Steps the two vehicles share (mode, sticks, fly,
set_param, wait_disarm) are the Copter runner's own; this module adds:

    vehicle: plane
    frame: quadplane                 # SITL frame: plane (default) or quadplane
    steps:
      - mission:                     # plane mission items, in order (home is added first)
          - takeoff: 40              # NAV_TAKEOFF (runway/hand launch) to 40 m
          - vtol_takeoff: 25         # QuadPlane NAV_VTOL_TAKEOFF
          - wp: [300, 0, 60]         # north, east, up (m) relative to home
          - loiter_turns: {at: [0, 0, 60], turns: 2, radius: 80}
          - transition: mc           # DO_VTOL_TRANSITION: mc (hover) or fw
          - speed: 18                # DO_CHANGE_SPEED (airspeed, m/s)
          - land_start: true         # DO_LAND_START
          - land: [0, 0]             # NAV_LAND (fixed wing, along the approach leg)
          - vtol_land: [0, 0]        # NAV_VTOL_LAND
          - rtl: true
      - takeoff: 40                  # plane: arm in AUTO (the mission takes off), wait for 40 m;
                                     # quadplane: QLOITER with throttle stick; or {alt, mode}
      - wait_wp: 4                   # until the mission's current item is 4 or later
"""
from __future__ import annotations

import math
from pathlib import Path

import yaml

from . import vehicles
from .runner import M, Run, Runner, ScenarioError
from .sitl import HOME, Sitl

EXTRA_RECORD = {"NAV_CONTROLLER_OUTPUT", "MISSION_CURRENT", "EXTENDED_SYS_STATE"}
EXTRA_STREAMS = {M.MAVLINK_MSG_ID_VFR_HUD: 10, M.MAVLINK_MSG_ID_NAV_CONTROLLER_OUTPUT: 10,
                 M.MAVLINK_MSG_ID_MISSION_CURRENT: 2, M.MAVLINK_MSG_ID_EXTENDED_SYS_STATE: 5}
VTOL_MANUAL = {"QLOITER", "QHOVER", "QSTABILIZE"}
ITEMS = {"takeoff": M.MAV_CMD_NAV_TAKEOFF, "vtol_takeoff": M.MAV_CMD_NAV_VTOL_TAKEOFF,
         "wp": M.MAV_CMD_NAV_WAYPOINT, "loiter_turns": M.MAV_CMD_NAV_LOITER_TURNS,
         "land": M.MAV_CMD_NAV_LAND, "vtol_land": M.MAV_CMD_NAV_VTOL_LAND,
         "rtl": M.MAV_CMD_NAV_RETURN_TO_LAUNCH, "transition": M.MAV_CMD_DO_VTOL_TRANSITION,
         "speed": M.MAV_CMD_DO_CHANGE_SPEED, "land_start": M.MAV_CMD_DO_LAND_START}


def _latlon(n: float, e: float) -> tuple[int, int]:
    lat0, lon0 = (float(v) for v in HOME.split(",")[:2])
    lat = lat0 + n / 111319.5
    lon = lon0 + e / (111319.5 * math.cos(math.radians(lat0)))
    return int(lat * 1e7), int(lon * 1e7)


def mission_items(spec: list) -> tuple[list[tuple], list[dict]]:
    """MAVLink items (home first) and, for metrics, what each one asks for. A bare [n, e, up]
    is a waypoint; a list of only those gets an RTL at the end, as Copter missions do."""
    plain = all(isinstance(x, (list, tuple)) for x in spec)
    spec = [{"wp": list(x)} if isinstance(x, (list, tuple)) else x for x in spec]
    if plain:
        spec.append({"rtl": True})
    items = [(0, M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, M.MAV_CMD_NAV_WAYPOINT, 0, 1,
              0, 0, 0, 0, *_latlon(0, 0), 0)]
    info = [{"seq": 0, "kind": "home", "n": 0.0, "e": 0.0, "up": 0.0}]
    for seq, entry in enumerate(spec, 1):
        (kind, arg), = entry.items()
        p, n, e, up = [0.0] * 4, None, None, 0.0
        if kind in ("takeoff", "vtol_takeoff"):
            up = float(arg)
            if kind == "takeoff":
                p[0] = 15.0                       # minimum pitch while climbing out (deg)
        elif kind == "wp":
            n, e, up = map(float, arg)
        elif kind == "loiter_turns":
            n, e, up = map(float, arg["at"])
            p[0], p[2] = float(arg.get("turns", 1)), float(arg.get("radius", 0))
        elif kind in ("land", "vtol_land"):
            n, e = map(float, arg[:2])
        elif kind == "transition":
            p[0] = {"mc": M.MAV_VTOL_STATE_MC, "fw": M.MAV_VTOL_STATE_FW}[arg]
        elif kind == "speed":
            p[0], p[1], p[2] = 0.0, float(arg), -1.0      # airspeed, value, throttle unchanged
        elif kind not in ("rtl", "land_start"):
            raise ScenarioError(f"unknown mission item {kind!r}")
        lat, lon = _latlon(n or 0.0, e or 0.0) if n is not None else (0, 0)
        items.append((seq, M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, ITEMS[kind], 0, 1, *p,
                      lat, lon, up))
        info.append({"seq": seq, "kind": kind, "n": n, "e": e, "up": up})
    return items, info


class PlaneRunner(Runner):
    def __init__(self, sitl: Sitl, run: Run, frame: str):
        super().__init__(sitl, run)
        self.frame = frame

    def pump(self, timeout=1.0):
        msg = super().pump(timeout)
        if msg is not None and msg.get_type() in EXTRA_RECORD:
            d = msg.to_dict()
            d["t"] = self.t
            self.run.samples.append(d)
        return msg

    def ready(self):
        super().ready()
        for msg_id, hz in EXTRA_STREAMS.items():
            self.s.set_message_rate(msg_id, hz)

    def arm(self):
        # plane pre-arm checks (airspeed, AHRS) can take longer after the EKF is ready
        for _ in range(20):
            self.s.command(M.MAV_CMD_COMPONENT_ARM_DISARM, 1)
            try:
                self.wait(lambda: self.armed, 3, "arm")
                break
            except ScenarioError:
                continue
        else:
            raise ScenarioError("could not arm")
        self.event("armed")

    def step_takeoff(self, spec):
        spec = spec if isinstance(spec, dict) else {"alt": spec}
        alt = float(spec["alt"])
        mode = spec.get("mode") or ("QLOITER" if self.frame == "quadplane" else "AUTO")
        manual = mode in VTOL_MANUAL
        if manual:
            self.step_sticks({"throttle": 1000})     # VTOL arming needs the throttle stick low
        self.s.set_mode(mode)
        self.wait(lambda: self.mode == mode, 10, mode)
        self.arm()
        self.event("takeoff_cmd", alt)
        if manual:
            self.step_sticks({"throttle": 1800})
        self.wait(lambda: (self.latest("GLOBAL_POSITION_INT") or {}).get("relative_alt", 0) > alt * 950,
                  120, f"takeoff to {alt} m")
        if manual:
            self.step_sticks({"throttle": 1500})     # centred throttle holds height in Q modes
        self.event("takeoff_done", alt)

    def step_mission(self, spec):
        items, info = mission_items(spec)
        mav = self.s.mav
        mav.mav.mission_count_send(mav.target_system, mav.target_component, len(items),
                                   M.MAV_MISSION_TYPE_MISSION)
        start = self.t
        while True:
            msg = mav.recv_match(type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"],
                                 blocking=True, timeout=5)
            if msg is None or self.t - start > 30:
                raise ScenarioError("mission upload timed out")
            if msg.get_type() == "MISSION_ACK":
                if msg.type != M.MAV_MISSION_ACCEPTED:
                    raise ScenarioError(f"mission rejected ({msg.type})")
                break
            seq, frame, cmd, cur, auto, p1, p2, p3, p4, x, y, z = items[msg.seq]
            mav.mav.mission_item_int_send(mav.target_system, mav.target_component, seq, frame,
                                          cmd, cur, auto, p1, p2, p3, p4, x, y, z,
                                          M.MAV_MISSION_TYPE_MISSION)
        self.event("mission", info)

    def step_wait_wp(self, seq):
        self.wait(lambda: (self.latest("MISSION_CURRENT") or {}).get("seq", 0) >= seq, 600,
                  f"mission item {seq}")
        self.event("wp_current", seq)


def run_scenario(path: Path, ardupilot: Path | None = None, speedup: int = 10,
                 instance: int = 0, extra_params: dict | None = None,
                 binary: Path | None = None) -> Run:
    spec = yaml.safe_load(Path(path).read_text())
    v = vehicles.get(spec.get("vehicle"))
    frame_name = spec.get("frame") or v.default_frame
    frame = v.frame(frame_name)
    run = Run(scenario=spec["name"])
    run.vehicle, run.frame = v.name, frame_name
    params = {**(extra_params or {}), **spec.get("params", {})}
    kw = {"ardupilot": ardupilot} if ardupilot else {}
    sitl = Sitl(speedup=speedup, instance=instance, params=params, binary_path=binary,
                vehicle=v.binary, model=frame.model, boot_params=spec.get("boot_params") or {}, **kw)
    sitl.defaults = vehicles.defaults_for(frame, sitl.binary, sitl.ardupilot)
    with sitl:
        r = PlaneRunner(sitl, run, frame_name)
        try:
            r.ready()
            for step in spec["steps"]:
                (name, arg), = step.items()
                getattr(r, f"step_{name}")(arg)
        except ScenarioError as e:
            run.ok, run.error = False, str(e)
            r.event("error", str(e))
    return run
