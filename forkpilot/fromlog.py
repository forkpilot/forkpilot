"""Turn a real flight log into a ForkPilot scenario that replays what the pilot did.

  python -m forkpilot.cli fromlog flight.bin -o scenarios/field_report.yaml [--start 60 --end 120]

Reads an ArduPilot DataFlash log (.bin/.log) or a MAVLink telemetry log (.tlog) and writes a
scenario in the runner's own step vocabulary: takeoff, mode, sticks, release, fly, set_param,
mission, wait_disarm. It replays the pilot's *commands*, not the real vehicle's dynamics.
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from pymavlink import DFReader, mavutil

from .sitl import HOME

MODES = mavutil.mode_mapping_acm
SITL_YAW = float(HOME.split(",")[3])
# stick roles in the runner's channel order (RC1..RC4 of the SITL it flies)
ROLES = ("roll", "pitch", "throttle", "yaw")
DEFAULT_MAP = {"roll": 1, "pitch": 2, "throttle": 3, "yaw": 4}

# Flight-behaviour parameters worth copying to the SITL. Everything else (hardware, sensor
# calibration, serial ports, logging...) is listed in a comment instead.
ALLOW = ("ATC_", "PSC_", "WPNAV_", "WP_", "LOIT_", "PHLD_", "ANGLE_MAX", "PILOT_", "THR_DZ", "MOT_",
         "FS_", "BATT_FS_", "BATT_LOW_", "BATT_CRT_", "RTL_", "LAND_", "FENCE_", "CIRCLE_", "ACRO_",
         "AVOID_", "AUTO_OPTIONS", "GUID_", "DISARM_DELAY", "TKOFF_", "INS_GYRO_FILTER",
         "INS_ACCEL_FILTER", "SIM_WIND_", "BRAKE_", "SPORT_", "SURFTRAK_", "ZIGZ_", "FHLD_", "THROW_", "SRTL_", "AUTOTUNE_", "MIS_", "SUPER_SIMPLE",
         "WVANE_", "FLIP_", "PLDP_")
# these match a prefix above but are hardware or bookkeeping, not flight behaviour
NEVER = ("MOT_PWM", "MOT_BAT_", "MOT_SAFE", "FENCE_TOTAL", "MIS_TOTAL")

# ModeReason (libraries/AP_Vehicle/ModeReason.h): who asked for the mode change
COMMANDED = {0, 1, 2, 18, 32, 42}                 # unknown, RC, GCS, toy, scripting, frsky
SEQUENCE = {8, 9, 12, 13, 15, 16, 26, 31, 41, 43, 45}   # the firmware repeats these by itself
REASONS = {3: "radio failsafe", 4: "battery failsafe", 5: "GCS failsafe", 6: "EKF failsafe",
           7: "GPS glitch", 10: "fence breach", 11: "terrain failsafe", 14: "avoidance",
           17: "terminate", 19: "crash check", 25: "failsafe"}

# modes where the pilot's sticks fly the vehicle: centred sticks there are a `release`
PILOT_MODES = {"STABILIZE", "ACRO", "ALT_HOLD", "LOITER", "POSHOLD", "DRIFT", "SPORT", "FLOWHOLD",
               "ZIGZAG", "POSITION", "OF_LOITER", "RATE_ACRO", "AUTOTUNE"}
TAKEOFF_DONE = 0.95            # the runner's takeoff step returns at this fraction of its altitude

MAV_CMD_WAYPOINT, MAV_CMD_RTL, MAV_CMD_LAND, MAV_CMD_TAKEOFF = 16, 20, 21, 22


class LogError(RuntimeError):
    pass


@dataclass
class Flight:
    """What the converter needs from a log, whatever its format. Times are seconds."""
    path: str = ""
    kind: str = "dataflash"
    modes: list = field(default_factory=list)      # (t, name, reason)
    rc: list = field(default_factory=list)         # (t, [raw PWM of RC1..RC8])
    alt: list = field(default_factory=list)        # (t, metres above home)
    yaw: list = field(default_factory=list)        # (t, degrees)
    arms: list = field(default_factory=list)       # (t, armed)
    params: dict = field(default_factory=dict)     # name -> [(t, value)]
    defaults: dict = field(default_factory=dict)   # name -> firmware default (DataFlash only)
    cmds: dict = field(default_factory=dict)       # seq -> {id, lat, lng, alt, frame}
    home: tuple | None = None                      # lat, lng, alt (MSL)
    info: list = field(default_factory=list)       # firmware / frame lines
    t0: float = 0.0
    t1: float = 0.0


# --- readers --------------------------------------------------------------

def read_flight(path) -> Flight:
    return _read_tlog(path) if str(path).lower().endswith(".tlog") else _read_dataflash(path)


def _clean(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def _read_dataflash(path) -> Flight:
    f = Flight(path=str(path))
    with open(path, "rb") as fh:
        binary = fh.read(2) == b"\xa3\x95"
    reader = DFReader.DFReader_binary if binary else DFReader.DFReader_text
    log = reader(str(path), zero_time_base=False)
    ctun, ev, arm = [], [], []
    times = []
    while (m := log.recv_match(type=["MODE", "RCIN", "POS", "CTUN", "ATT", "ARM", "EV", "PARM",
                                     "CMD", "ORGN", "MSG"])) is not None:
        k, t = m.get_type(), m.TimeUS / 1e6
        times.append(t)
        if k == "MODE":
            n = getattr(m, "ModeNum", m.Mode)
            f.modes.append((t, MODES.get(n, f"MODE_{n}"), getattr(m, "Rsn", 0)))
        elif k == "RCIN":
            f.rc.append((t, [getattr(m, f"C{i}") for i in range(1, 9)]))
        elif k == "POS":
            f.alt.append((t, m.RelHomeAlt))
        elif k == "CTUN":
            ctun.append((t, m.Alt))
        elif k == "ATT":
            f.yaw.append((t, m.Yaw % 360))
        elif k == "ARM":
            arm.append((t, bool(m.ArmState)))
        elif k == "EV" and m.Id in (10, 11):
            ev.append((t, m.Id == 10))
        elif k == "PARM":
            f.params.setdefault(m.Name, []).append((t, m.Value))
            d = _clean(getattr(m, "Default", None))
            if d is not None:
                f.defaults[m.Name] = d
        elif k == "CMD":
            f.cmds[m.CNum] = {"id": m.CId, "lat": m.Lat, "lng": m.Lng, "alt": m.Alt,
                              "frame": getattr(m, "Frame", 3)}
        elif k == "ORGN" and m.Type == 0 and f.home is None:
            f.home = (m.Lat, m.Lng, m.Alt)
        elif k == "MSG" and len(f.info) < 6 and re.search(r"Ardu|Frame|PX4", m.Message):
            f.info.append(m.Message)
    if not times:
        raise LogError("no usable messages in the log")
    f.t0, f.t1 = min(times), max(times)
    f.alt = f.alt or ctun
    f.arms = arm or ev
    if not f.arms or not f.arms[0][1]:   # logging starts on arming: no record of the arming itself
        f.arms.insert(0, (f.t0, True))
    cmd0 = f.cmds.get(0)
    if cmd0 and cmd0["lat"]:
        f.home = (cmd0["lat"], cmd0["lng"], cmd0["alt"])
    return f


COPTER_TYPES = {2, 3, 4, 13, 14, 15}     # MAV_TYPE quad/coax/hex/tri/octo/deca


def _read_tlog(path) -> Flight:
    f = Flight(path=str(path), kind="tlog")
    log = mavutil.mavlink_connection(str(path))
    sysid, first, prev_mode, prev_arm = None, None, None, None
    mission: dict = {}
    while (m := log.recv_match(type=["HEARTBEAT", "RC_CHANNELS", "RC_CHANNELS_RAW",
                                     "GLOBAL_POSITION_INT", "ATTITUDE", "PARAM_VALUE",
                                     "MISSION_ITEM", "MISSION_ITEM_INT", "MISSION_COUNT",
                                     "HOME_POSITION"])) is not None:
        t, k = m._timestamp, m.get_type()
        first = t if first is None else first
        f.t1 = t
        if k == "HEARTBEAT":
            if m.type not in COPTER_TYPES or m.autopilot == 8:
                continue
            sysid = m.get_srcSystem() if sysid is None else sysid
            if m.get_srcSystem() != sysid:
                continue
            name = MODES.get(m.custom_mode, f"MODE_{m.custom_mode}")
            if name != prev_mode:
                f.modes.append((t, name, 0))
                prev_mode = name
            armed = bool(m.base_mode & 128)
            if armed != prev_arm:
                f.arms.append((t, armed))
                prev_arm = armed
            continue
        if k in ("MISSION_ITEM", "MISSION_ITEM_INT", "MISSION_COUNT") and m.get_srcSystem() != sysid:
            if k == "MISSION_COUNT":
                mission = {}
            else:
                s = 1e7 if k == "MISSION_ITEM_INT" else 1.0
                mission[m.seq] = {"id": m.command, "lat": m.x / s, "lng": m.y / s, "alt": m.z,
                                  "frame": m.frame}
            continue
        if sysid is not None and m.get_srcSystem() != sysid:
            continue
        if k in ("RC_CHANNELS", "RC_CHANNELS_RAW"):
            f.rc.append((t, [getattr(m, f"chan{i}_raw") for i in range(1, 9)]))
        elif k == "GLOBAL_POSITION_INT":
            f.alt.append((t, m.relative_alt / 1000))
        elif k == "ATTITUDE":
            f.yaw.append((t, math.degrees(m.yaw) % 360))
        elif k == "PARAM_VALUE":
            f.params.setdefault(m.param_id, []).append((t, m.param_value))
        elif k == "HOME_POSITION":
            f.home = (m.latitude / 1e7, m.longitude / 1e7, m.altitude / 1000)
    if first is None or not f.modes:
        raise LogError("no Copter heartbeat in the telemetry log")
    f.cmds = mission
    for name in ("alt", "yaw", "rc", "modes", "arms"):
        setattr(f, name, [(t - first, *rest) for t, *rest in getattr(f, name)])
    f.params = {k: [(t - first, v) for t, v in vs] for k, vs in f.params.items()}
    f.t1 -= first
    if not f.arms:
        f.arms = [(0.0, True)]
    if f.home is None and 0 in mission:
        f.home = (mission[0]["lat"], mission[0]["lng"], mission[0]["alt"])
    return f


# --- helpers --------------------------------------------------------------

def _at(series, t, default=None):
    """Value of a (t, v...) series at time t: the last sample at or before it, else the first."""
    if not series:
        return default
    i = bisect.bisect_right([s[0] for s in series], t)
    return series[max(i - 1, 0)][1] if len(series[0]) == 2 else series[max(i - 1, 0)][1:]


def _fmt(v):
    v = float(v)
    return str(int(v)) if v.is_integer() else repr(float(f"{v:.7g}"))


def _flow(d):
    return "{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in d.items()) + "}"


def read_defaults(path) -> dict:
    """A SITL --defaults file: 'NAME value' (space, tab, comma or '=')."""
    out = {}
    for line in Path(path).read_text().splitlines():
        parts = re.split(r"[\s,=]+", line.split("#")[0].strip())
        if len(parts) >= 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return out


def mode_at(f: Flight, t):
    return next((n for tt, n, _ in reversed(f.modes) if tt <= t), None)


def param_at(f: Flight, name, t, default=None):
    vs = [(tt, v) for tt, v in f.params.get(name, ()) if tt <= t]
    return vs[-1][1] if vs else default


# --- RC handling ----------------------------------------------------------

def rc_roles(f: Flight, t):
    """role -> (channel, min, max, trim, reversed), from the log's RCMAP_* and RCn_* at time t."""
    out = {}
    for role, ch0 in DEFAULT_MAP.items():
        ch = int(param_at(f, f"RCMAP_{role.upper()}", t, ch0))
        rev = param_at(f, f"RC{ch}_REVERSED", t, 0) or param_at(f, f"RC{ch}_REV", t, 1) == -1
        out[role] = (ch, param_at(f, f"RC{ch}_MIN", t, 1000), param_at(f, f"RC{ch}_MAX", t, 2000),
                     param_at(f, f"RC{ch}_TRIM", t, 1500), bool(rev))
    return out


def stick_pwm(role, raw, cal):
    """One raw RC value as the PWM the runner must send to the stock SITL (1000..2000, trim
    1500) to give the vehicle the same stick position: undoes the log's min/max/trim and
    reversal. Deadzone is left to the vehicle (RCn_DZ is copied as a parameter)."""
    _, lo, hi, trim, rev = cal
    if role == "throttle":
        n = min(max((raw - lo) / max(hi - lo, 1), 0), 1)
        return 1000 + 1000 * ((1 - n) if rev else n)
    n = (raw - trim) / max(hi - trim, 1) if raw >= trim else (raw - trim) / max(trim - lo, 1)
    n = min(max(n, -1), 1) * (-1 if rev else 1)
    return 1500 + 500 * n


def stick_series(f: Flight, t0, t1):
    """[(t, [roll, pitch, throttle, yaw] PWM)] in [t0, t1]; samples with no RC signal are skipped."""
    roles, out = rc_roles(f, t0), []
    for t, raw in f.rc:
        if t < t0 or t > t1:
            continue
        vals = [raw[roles[r][0] - 1] for r in ROLES]
        if min(vals) < 900:
            continue
        out.append((t, [stick_pwm(r, v, roles[r]) for r, v in zip(ROLES, vals)]))
    return out


def compress(series, t_end, tol=20.0, min_seg=0.3):
    """Piecewise-constant segments [(t_start, t_stop, [4 values])] of a stick series. A segment
    grows while every channel stays inside a band of 2*tol, and holds the band's midpoint, so
    no sample is further than tol from it. A segment shorter than min_seg is absorbed by a neighbour
    that is within the band of it."""
    segs, i, n = [], 0, len(series)
    while i < n:
        lo, hi, j = list(series[i][1]), list(series[i][1]), i + 1
        while j < n:
            nl = [min(a, b) for a, b in zip(lo, series[j][1])]
            nh = [max(a, b) for a, b in zip(hi, series[j][1])]
            if any(h - l > 2 * tol for l, h in zip(nl, nh)):
                break
            lo, hi, j = nl, nh, j + 1
        segs.append([series[i][0], series[j][0] if j < n else t_end,
                     [(a + b) / 2 for a, b in zip(lo, hi)]])
        i = j
    # a blip shorter than min_seg is absorbed by a neighbour it differs from by at most the band;
    # a short segment that is really a steep ramp step stays
    close = lambda x, y: all(abs(p - q) <= 2 * tol for p, q in zip(x[2], y[2]))
    skip = set()
    while len(segs) > 1:
        k = next((k for k, s in enumerate(segs) if s[1] - s[0] < min_seg and id(s) not in skip), None)
        if k is None:
            break
        nb = next((j for j in (k - 1, k + 1) if 0 <= j < len(segs) and close(segs[k], segs[j])), None)
        if nb is None:
            skip.add(id(segs[k]))
            continue
        if nb < k:
            segs[nb][1] = segs[k][1]
        else:
            segs[nb][0] = segs[k][0]
        del segs[k]
    return segs


def snap(vec, tol):
    """Round to 5 PWM; a roll/pitch/yaw within tol of centre is centre."""
    out = []
    for role, v in zip(ROLES, vec):
        v = 1500 if role != "throttle" and abs(v - 1500) <= tol else round(v / 5) * 5
        out.append(int(min(max(v, 1000), 2000)))
    return out


# --- conversion -----------------------------------------------------------

@dataclass
class Options:
    start: float | None = None          # log seconds; default: when the vehicle is airborne
    end: float | None = None
    name: str = "from_log"
    tol: float = 20.0                   # stick compression tolerance, PWM
    min_seg: float = 0.3                # a shorter blip within the tolerance band is absorbed, s
    release_min: float = 2.0            # centred sticks at least this long become `release`
    mode_latency: float = 0.1           # sim s the runner's `mode` step costs (measured, see docs)
    step_overhead: float = 0.007        # sim s each wait step overshoots by (next telemetry message)
    takeoff_alt: float | None = None
    defaults: dict | None = None        # SITL defaults file, name -> value
    keep: tuple = ()
    drop: tuple = ()
    all_params: bool = False            # copy every allow-listed param, not only the changed ones


@dataclass
class Result:
    yaml: str
    start: float                        # log time of scenario t=0 (end of the takeoff)
    end: float
    takeoff_alt: float
    notes: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def airborne_start(f: Flight, t_arm, t_stop):
    """When the vehicle is airborne and settled: the first in-air mode change, or the end of the
    initial climb (above 2 m, climbing slower than 0.5 m/s over the next second)."""
    cands = []
    for t, a in f.alt:
        if t < t_arm or t > t_stop:
            continue
        if a >= 2.0 and abs(_at(f.alt, t + 1.0) - a) < 0.5:
            cands.append(t)
            break
    cands += [t for t, _, _ in f.modes if t_arm + 0.5 < t < t_stop and (_at(f.alt, t) or 0) > 1.0]
    return min(cands) if cands else None


def _arm_period(f: Flight, start):
    periods, t_arm = [], None
    for t, armed in sorted(f.arms):
        if armed and t_arm is None:
            t_arm = t
        elif not armed and t_arm is not None:
            periods.append((t_arm, t))
            t_arm = None
    if t_arm is not None:
        periods.append((t_arm, None))
    if not periods:
        raise LogError("the log shows no arming")
    if start is None:
        return periods[0]
    return next((p for p in periods if p[0] <= start and (p[1] is None or start <= p[1])), periods[0])


def select_params(f: Flight, t, o: Options):
    """-> (kept {name: value}, dropped {reason: [names]}). A parameter is kept when it is on the
    allow-list and differs from what the SITL would have: its defaults-file value, else the
    firmware default recorded in the log."""
    sitl = o.defaults or {}
    kept, dropped = {}, {"not flight behaviour": [], "same as the SITL default": [],
                         "default unknown (tlog)": []}
    for name in sorted(f.params):
        v = param_at(f, name, t)
        if v is None:
            continue
        allowed = (name.startswith(tuple(ALLOW)) and not name.startswith(NEVER)
                   or name.startswith(tuple(o.keep))) and not name.startswith(tuple(o.drop))
        if not allowed:
            if re.fullmatch(r"RC[1-4]_DZ", name):
                continue                                  # handled below, by role
            if abs(v - sitl.get(name, f.defaults.get(name, v))) > 1e-6 * max(1, abs(v)):
                dropped["not flight behaviour"].append(name)
            continue
        ref = sitl.get(name, f.defaults.get(name))
        if ref is None and not o.all_params:
            dropped["default unknown (tlog)"].append(name)
        elif ref is not None and abs(v - ref) <= 1e-6 * max(1, abs(v)) and not o.all_params:
            dropped["same as the SITL default"].append(name)
        else:
            kept[name] = v
    # stick deadzones follow the role, not the channel number: the replay's SITL has the stock RCMAP
    for role, (ch, *_) in rc_roles(f, t).items():
        v = param_at(f, f"RC{ch}_DZ", t)
        target = f"RC{DEFAULT_MAP[role]}_DZ"
        ref = sitl.get(target, f.defaults.get(f"RC{ch}_DZ"))
        if v is not None and (o.all_params or ref is not None and abs(v - ref) > 1e-6):
            kept[target] = v
    return kept, dropped


def mission_items(f: Flight):
    """-> (waypoints [[n, e, up]], notes) from the log's mission (CMD / uploaded items)."""
    if not f.cmds or not f.home:
        return [], []
    lat0, lon0, alt0 = f.home
    wps, notes = [], []
    for seq in sorted(f.cmds):
        if seq == 0:
            continue
        c = f.cmds[seq]
        if c["id"] == MAV_CMD_WAYPOINT:
            up = c["alt"] - alt0 if c["frame"] in (0, 2) else c["alt"]
            wps.append([round((c["lat"] - lat0) * 111319.5, 1),
                        round((c["lng"] - lon0) * 111319.5 * math.cos(math.radians(lat0)), 1),
                        round(up, 1)])
            if c["frame"] == 10:
                notes.append(f"mission item {seq} uses terrain-relative altitude; replayed as height above home")
        elif c["id"] == MAV_CMD_RTL:
            pass                                  # the runner appends an RTL
        elif c["id"] == MAV_CMD_TAKEOFF:
            pass                                  # replayed as the takeoff step
        elif c["id"] == MAV_CMD_LAND:
            notes.append(f"mission item {seq} is LAND; the runner ends missions with RTL instead")
        else:
            notes.append(f"mission item {seq} (command {c['id']}) is not replayable and was dropped")
    return wps, notes


def convert(f: Flight, o: Options | None = None) -> Result:
    o = o or Options()
    notes: list[str] = []
    t_arm, t_dis = _arm_period(f, o.start)
    t_stop = min(x for x in (o.end, t_dis, f.t1) if x is not None)
    t_air = airborne_start(f, t_arm, t_stop)
    if t_air is None:
        raise LogError("the vehicle never got airborne in this window (no altitude above 2 m)")
    s0 = t_air if o.start is None or o.start < t_air else o.start
    if o.start is not None and o.start < t_air:
        notes.append(f"--start {o.start:g} s is before the vehicle is airborne; the replay "
                     f"takes off with a GUIDED takeoff and starts at {t_air:.1f} s")
    if s0 >= t_stop:
        raise LogError("the window is empty")
    disarm = t_dis is not None and t_dis <= (o.end if o.end is not None else t_dis)
    t_end = t_stop
    height = _at(f.alt, s0)
    alt = o.takeoff_alt or round(height / TAKEOFF_DONE, 1)

    # modes: the one in force at s0, then the commanded changes
    mode0 = next((m for m in reversed(f.modes) if m[0] <= s0 + 0.05), (0, "GUIDED", 0))[1]
    changes = []
    for t, name, rsn in f.modes:
        if not (s0 + 0.05 < t <= t_end) or name.startswith("MODE_"):
            if name.startswith("MODE_") and s0 < t <= t_end:
                notes.append(f"{t:.1f} s: unknown mode number in the log, skipped")
            continue
        if rsn in SEQUENCE:
            notes.append(f"{t:.1f} s: {name} was entered by the firmware itself "
                         f"(reason {rsn}); the replay lets it happen on its own")
            continue
        changes.append((t, name, REASONS.get(rsn) if rsn not in COMMANDED else None))

    kept, dropped = select_params(f, s0, o)
    pchanges = []                        # parameters changed in flight
    for name in kept:
        last = kept[name]
        for t, v in f.params.get(name, ()):
            if s0 + 1.0 < t <= t_end and abs(v - last) > 1e-9:
                pchanges.append((t, {name: v}))
                last = v

    # sticks
    series = stick_series(f, s0, t_end)
    segs = [(a, b, snap(v, o.tol)) for a, b, v in compress(series, t_end, o.tol, o.min_seg)]
    merged = []
    for a, b, v in segs:
        if merged and merged[-1][2] == v:
            merged[-1][1] = b
        else:
            merged.append([a, b, v])
    segs = merged
    if segs:
        segs[0][0] = s0
    else:
        notes.append("the log has no RC input in this window; no stick steps")
    centred = lambda v: v[0] == v[1] == v[3] == 1500

    # mission
    wps, mnotes = mission_items(f)
    notes += mnotes
    uses_auto = mode0 == "AUTO" or any(n == "AUTO" for _, n, _ in changes)
    if wps and not uses_auto:
        notes.append("the log has a mission but the window never flies AUTO; not replayed")
        wps = []
    if uses_auto and not wps:
        notes.append("AUTO is flown but no mission could be read from the log")

    # steps ---------------------------------------------------------------
    steps: list[tuple[str, str, str]] = []          # (key, value text, comment)
    if wps:
        steps.append(("mission", "[" + ", ".join(f"[{', '.join(_fmt(x) for x in w)}]" for w in wps) + "]",
                      f"{len(wps)} waypoints from the log's mission, then RTL"))
    steps.append(("takeoff", _fmt(alt), f"log: airborne at {s0:.1f} s at {height:.1f} m (the step returns at 95% of its height)"))
    yaw0 = _at(f.yaw, s0)
    if yaw0 is not None and abs((yaw0 - SITL_YAW + 180) % 360 - 180) > 5:
        steps.append(("yaw", _fmt(round(yaw0)), "heading at the start of the window"))
    state: dict[str, int] = {}                       # what the runner holds on each stick
    cursor = 0.0                                     # scenario time the steps so far account for
    pending = None                                   # centred sticks waiting for a `release` step

    def rel(t):
        return round(t - s0, 1)

    def tag(t):
        return f"+{rel(t):g} s (log {t:.1f})"

    def set_sticks(vec, t, only=None):
        # a channel that moves less than half the tolerance is noise: keep the value already held
        new = {r: v for r, v in zip(ROLES, vec) if state.get(r) != v and (only is None or r in only)
               and (v == 1500 or r not in state or abs(v - state[r]) >= o.tol / 2)}
        if new:
            steps.append(("sticks", _flow(new), tag(t)))
            state.update(new)

    def advance(t, final=False):
        """Wait until log time t: `release` if centred sticks are pending, else `fly`."""
        nonlocal cursor, pending
        d = round(rel(t) - cursor, 1)
        if d >= 0.1 and not (final and disarm):
            steps.append(("release" if pending else "fly", _fmt(d), ""))
            if pending:
                state.update(roll=1500, pitch=1500, yaw=1500)
            pending, cursor = None, cursor + d + o.step_overhead
        elif pending and (d < 0.1 or final):         # no time left to watch the stop
            set_sticks(pending, t, ("roll", "pitch", "yaw"))
            pending = None

    points = [(a, 0, "seg", (a, b, v)) for a, b, v in segs]
    starts = [a for a, _, _ in segs]
    for t, name, why in changes:
        near = min(starts, key=lambda a: abs(a - t), default=None)
        points.append((near if near is not None and abs(near - t) <= 0.25 else t, 1, "mode", (t, name, why)))
    points += [(t, 1, "param", d) for t, d in pchanges]
    points.sort(key=lambda p: (p[0], p[1]))

    def mode_step(name, comment):
        nonlocal cursor
        steps.append(("mode", name, comment))
        cursor += o.mode_latency

    if not segs and mode0 != "GUIDED":
        mode_step(mode0, f"log: {mode0} at the start of the window")
    for i, (t, _, kind, data) in enumerate(points):
        advance(t)
        if kind == "seg":
            a, b, v = data
            if centred(v) and b - a >= o.release_min and mode_at(f, a + 0.25) in PILOT_MODES:
                set_sticks(v, a, ("roll", "pitch", "yaw", "throttle") if not state else ("throttle",))
                pending = [1500, 1500, v[2], 1500]
            else:
                set_sticks(v, a)
            if i == 0 and mode0 != "GUIDED":
                mode_step(mode0, f"log: {mode0} at the start of the window")
        elif kind == "mode":
            tt, name, why = data
            mode_step(name, tag(tt) + (f"; log: {why}, the trigger is not reproduced" if why else ""))
        else:
            steps.append(("set_param", _flow(data), tag(t)))
    advance(t_end, final=True)
    if disarm:
        steps.append(("wait_disarm", _fmt(round(1.5 * (t_dis - s0 - cursor) + 60)),
                      f"log: disarmed at {t_dis:.1f} s"))

    # render -------------------------------------------------------------
    name = re.sub(r"[^a-z0-9_]+", "_", o.name.lower()).strip("_") or "from_log"
    lines = [f"# Generated by `forkpilot fromlog` from {Path(f.path).name} ({f.kind})"]
    if f.info:
        lines.append("# Log says: " + "; ".join(f.info[:3]))
    lines += [f"# Log window {s0:.1f} .. {t_end:.1f} s (log time). Scenario t=0 is the end of the "
              f"GUIDED takeoff, log time {s0:.1f} s.",
              "# This replays the pilot's commands (modes, sticks, mission), not the real vehicle's "
              "dynamics, wind or sensor noise; see docs/fromlog.md.",
              f"# Sticks: {len(segs)} segment{'s' * (len(segs) != 1)} (tolerance {o.tol:g} PWM, min {o.min_seg:g} s), mapped "
              f"through the log's RCMAP_*/RCn_* to the SITL's RC1..RC4."]
    for n in notes:
        lines.append(f"# Note: {n}")
    names = dropped["not flight behaviour"]
    if names:
        lines += _wrap(f"# Dropped parameters, differ from the defaults but are not flight behaviour ({len(names)}): ", names)
    if dropped["same as the SITL default"] or dropped["default unknown (tlog)"]:
        lines.append(f"# Not copied: {len(dropped['same as the SITL default'])} allow-listed parameters equal the "
                     f"SITL default, {len(dropped['default unknown (tlog)'])} have no known default")
    lines.append(f"name: {name}")
    if not kept:
        lines.append("params: {}")
    elif len(kept) <= 3:
        lines.append(f"params: {_flow(kept)}")
    else:
        lines.append("params:")
        lines += [f"  {k}: {_fmt(v)}" for k, v in kept.items()]
    lines.append("steps:")
    for key, val, com in steps:
        lines.append(f"  - {key}: {val}" + (f"  # {com}" if com else ""))
    lines.append("expect: {}")
    stats = {"stick_segments": len(segs), "modes": len(changes), "params": len(kept),
             "dropped": sum(len(v) for v in dropped.values()), "steps": len(steps),
             "samples": len(series)}
    return Result("\n".join(lines) + "\n", s0, t_end, alt, notes, stats)


def _wrap(prefix, names, width=100):
    lines, cur = [], prefix
    for n in names:
        if len(cur) + len(n) > width and cur != prefix and cur != "#   ":
            lines.append(cur.rstrip())
            cur = "#   "
        cur += n + ", "
    lines.append(cur.rstrip(", "))
    return lines
