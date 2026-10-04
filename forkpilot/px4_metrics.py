"""Metrics for PX4 multicopter runs. Deterministic arithmetic on telemetry, as in metrics.py.

The names and meanings follow the Copter metrics wherever the quantity is the same, so rules and
reports read alike. Differences: positions are taken relative to PX4's home (HOME_POSITION x, y, z
in the local frame, which need not be the EKF origin), arming and disarming are timed from PX4's
own messages, and mode names in metric keys are PX4's with the dot as underscore (auto_loiter).
"""
from __future__ import annotations

import bisect
import math
import statistics as st

from .metrics import _event_time, _mode_metrics, _series, _stop_metrics, _touchdown, _velocity_metrics, _windows
from .px4_sitl import decode_mode, encode_mode

ARMED = ("Armed by external command", "Armed by RC", "Armed by")
DISARMED = ("Disarmed by landing", "Disarmed by", "Disarmed")
FAULTS = ("set_param", "rc_loss", "link_loss")
IN_AIR = 2          # MAV_LANDED_STATE_IN_AIR


def _home(run: dict) -> tuple[float, float, float]:
    """Home in the local frame as set at arming (PX4 sets it again at every arming)."""
    hp = _series(run, "HOME_POSITION")
    took_off = _event_time(run, "takeoff_done")
    before = [s for s in hp if took_off is None or s["t"] <= took_off]
    h = (before or hp or [{"x": 0.0, "y": 0.0, "z": 0.0}])[-1]
    return h["x"], h["y"], h["z"]


def relative(run: dict) -> dict:
    """The run with LOCAL_POSITION_NED shifted so that home is the origin."""
    hx, hy, hz = _home(run)
    samples = [{**s, "x": s["x"] - hx, "y": s["y"] - hy, "z": s["z"] - hz}
               if s["mavpackettype"] == "LOCAL_POSITION_NED" else s for s in run["samples"]]
    return {**run, "samples": samples}


def _text_time(run, starts, after=None):
    return next((t for t, k, d in run["events"] if k == "statustext" and str(d).startswith(starts)
                 and (after is None or t > after)), None)


def _segments(run: dict) -> list[tuple[str, float, float]]:
    """(PX4 mode, start, end) from the vehicle's HEARTBEAT, however the mode changed."""
    out, end = [], (run["samples"][-1]["t"] if run["samples"] else 0.0)
    for s in _series(run, "HEARTBEAT"):
        if s.get("type") == 6:
            continue
        name = decode_mode(s["custom_mode"])
        if not out or out[-1][0] != name:
            if out:
                out[-1][2] = s["t"]
            out.append([name, s["t"], end])
    return [tuple(x) for x in out]


def _key(k: str) -> str:
    return k.replace(".", "_")


def compute(run: dict) -> dict:
    run = relative(run)
    ev = run["events"]
    m: dict[str, float] = {"completed": 1.0 if run["ok"] else 0.0}
    armed = _text_time(run, ARMED) or _event_time(run, "takeoff_cmd") or _event_time(run, "armed")
    took_off = _event_time(run, "takeoff_done")
    if armed is not None and took_off is not None:
        m["takeoff_time_s"] = took_off - armed

    drifts, alt_errs, tilt, thrust = [], [], [], []
    for t0, t1 in _windows(run, "hold_start", {"goto", "mode", "set_param", "error", *FAULTS}):
        pos = _series(run, "LOCAL_POSITION_NED", t0, t1)
        if len(pos) < 5:
            continue
        x0, y0, z0 = pos[0]["x"], pos[0]["y"], pos[0]["z"]
        drifts += [math.hypot(p["x"] - x0, p["y"] - y0) for p in pos]
        alt_errs += [abs(p["z"] - z0) for p in pos]
        tilt += [math.degrees(math.hypot(a["roll"], a["pitch"])) for a in _series(run, "ATTITUDE", t0, t1)]
        thrust += [a["thrust"] for a in _series(run, "ATTITUDE_TARGET", t0, t1)]
    if drifts:
        m["hold_drift_max_m"] = max(drifts)
        m["hold_alt_err_max_m"] = max(alt_errs)
        m["hold_tilt_rms_deg"] = math.sqrt(st.fmean(x * x for x in tilt)) if tilt else 0.0
    if thrust:
        m["hold_thrust"] = st.fmean(thrust)

    legs, overshoots = [], []
    for t0, t1 in _windows(run, "goto", {"arrived", "error"}):
        if t1 is not None:
            legs.append(t1 - t0)
    for t0, t1 in _windows(run, "goto", {"mode", "set_param", "error", *FAULTS}):
        target = next(d for t, k, d in ev if k == "goto" and t == t0)
        pos = _series(run, "LOCAL_POSITION_NED", t0, t1)
        if len(pos) < 2:
            continue
        dx, dy = target[0] - pos[0]["x"], target[1] - pos[0]["y"]
        length = math.hypot(dx, dy)
        if length < 1:
            continue
        ux, uy = dx / length, dy / length
        overshoots.append(max(0.0, max((p["x"] - target[0]) * ux + (p["y"] - target[1]) * uy for p in pos)))
    if legs:
        m["goto_time_total_s"] = sum(legs)
    if overshoots:
        m["goto_overshoot_max_m"] = max(overshoots)

    segs = _segments(run)
    m.update(_failsafe_metrics(run, segs))
    m.update(_mission_metrics(run, segs))
    m.update(_rtl_metrics(run, segs))
    m.update({_key(k): v for k, v in {**_stop_metrics(run), **_mode_metrics(run)}.items()})
    m.update(_velocity_metrics(run))
    m.update(_attitude_metrics(run))

    gpi = _series(run, "GLOBAL_POSITION_INT")
    if gpi:
        m["alt_max_m"] = max(s["relative_alt"] for s in gpi) / 1000
    pos = _series(run, "LOCAL_POSITION_NED")
    if pos:
        m["range_max_m"] = max(math.hypot(p["x"], p["y"]) for p in pos)
    m.update(_landing_metrics(run, armed, segs))
    return {k: max(v, _floor(k)) for k, v in m.items()}


# Noise floors from A/A runs of one binary (see the README): below these, values split into
# run-to-run clusters a 5-run baseline band, sized for larger values, would flag. disarm_delay_s
# has two clusters in every scenario (land detector: 1-1.8 s or about 4 s); the others are
# centimetres or tenths of a degree that a 5-run band, at 10% of a tiny mean, cannot hold.
FLOORS = {"disarm_delay_s": 5.0, "hold_alt_err_max_m": 0.5, "hold_tilt_rms_deg": 0.5, "land_drift_m": 0.5,
          "landing_offset_m": 0.5, "stop_backtrack_m": 0.5, "stop_alt_dev_m": 0.5, "mode_yaw_rate_dps": 1.0}


def _floor(key: str) -> float:
    return next((f for prefix, f in FLOORS.items() if key.startswith(prefix)), -math.inf)


def _failsafe_metrics(run: dict, segs) -> dict:
    """After the first injected fault (set_param, rc_loss, link_loss): seconds until the mode
    changes, and the action taken as main.sub (AUTO.RTL 4.5, AUTO.LAND 4.6, AUTO.LOITER 4.3). PX4
    holds for COM_FAIL_ACT_T before acting, so the action is the first mode after the fault that is
    neither the one before it nor that Hold."""
    fault = min((t for k in FAULTS if (t := _event_time(run, k)) is not None), default=None)
    if fault is None:
        return {}
    hb = [s for s in _series(run, "HEARTBEAT") if s.get("type") != 6]
    before = [s["custom_mode"] for s in hb if s["t"] <= fault]
    if not before:
        return {}
    change = next((s for s in hb if s["t"] > fault and s["custom_mode"] != before[-1]), None)
    if change is None:
        return {"failsafe_reaction_s": float("inf")}
    # PX4's HEARTBEAT comes at 1 Hz: its own "Failsafe activated" text, when it sends one, times
    # the reaction to the message instead of to the next whole second
    said = _text_time(run, "Failsafe activated", fault)
    t = min(change["t"], said) if said is not None else change["t"]
    hold = encode_mode("AUTO.LOITER")
    act = next((s for s in hb if s["t"] > fault and s["custom_mode"] not in (before[-1], hold)), change)
    c = act["custom_mode"]
    return {"failsafe_reaction_s": t - fault,
            "failsafe_mode": ((c >> 16) & 0xFF) + ((c >> 24) & 0xFF) / 10}


def _mission_metrics(run: dict, segs) -> dict:
    """Waypoints reached, time from AUTO.MISSION to disarm, top cruise speed, and the largest
    distance off the straight line between consecutive waypoints while that leg was current."""
    ev = run["events"]
    mission = next((d for t, k, d in reversed(ev) if k == "mission"), None)
    if not mission:
        return {}
    # PX4 streams only the latest MISSION_ITEM_REACHED, so an item reached just before the next
    # (the last waypoint before an RTL item) is often never reported: an item the mission moved
    # past (MISSION_CURRENT, numbered from 0) counts as reached too. RTL items do not count.
    rtl = {i["seq"] for i in mission if i["kind"] == "rtl"}
    past = max((s["seq"] for s in _series(run, "MISSION_CURRENT") if s["seq"] < len(mission) - 1), default=0)
    reached = {d for _, k, d in ev if k == "wp_reached" and d} | set(range(1, past + 1))
    m = {"mission_wp_reached": float(len(reached - rtl))}
    start = next((t0 for mode, t0, _ in segs if mode == "AUTO.MISSION"), None)
    end = _event_time(run, "disarmed")
    if start is not None and end is not None:
        m["mission_time_s"] = end - start
        pos = _series(run, "LOCAL_POSITION_NED", start, end)
        if pos:
            m["mission_speed_max_mps"] = max(math.hypot(p["vx"], p["vy"]) for p in pos)
    if start is None:
        return m
    auto = [(t0, t1) for mode, t0, t1 in segs if mode == "AUTO.MISSION"]
    cur = [s for s in _series(run, "MISSION_CURRENT") if any(a <= s["t"] <= b for a, b in auto)]
    pts = [i for i in mission if i["kind"] == "wp"]
    xt = []
    for a, b in zip(pts, pts[1:]):
        seq = b["seq"] - 1                       # PX4's own numbering starts at 0
        ts = [s["t"] for s in cur if s["seq"] == seq]
        length = math.hypot(b["n"] - a["n"], b["e"] - a["e"])
        if not ts or length < 5:
            continue
        ux, uy = (b["n"] - a["n"]) / length, (b["e"] - a["e"]) / length
        for p in _series(run, "LOCAL_POSITION_NED", ts[0], ts[-1]):
            along = (p["x"] - a["n"]) * ux + (p["y"] - a["e"]) * uy
            if 0 <= along <= length:
                xt.append(abs((p["x"] - a["n"]) * uy - (p["y"] - a["e"]) * ux))
    if xt:
        m["mission_xtrack_max_m"] = max(xt)
    return m


def _rtl_metrics(run: dict, segs) -> dict:
    """The first AUTO.RTL: how high it flew home, how long it took to land, and how far from home."""
    rtl = next(((t0, t1) for mode, t0, t1 in segs if mode == "AUTO.RTL"), None)
    if rtl is None:
        return {}
    gpi = _series(run, "GLOBAL_POSITION_INT", *rtl)
    if not gpi:
        return {}
    m = {"rtl_alt_max_m": max(s["relative_alt"] for s in gpi) / 1000}
    end = _event_time(run, "disarmed")
    if end is not None and end >= rtl[0]:
        m["rtl_time_s"] = end - rtl[0]
    return m


def _quat_rp(q) -> tuple[float, float]:
    w, x, y, z = q
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    return roll, pitch


def _attitude_metrics(run: dict) -> dict:
    """RMS of the roll/pitch error against the attitude setpoint while in the air (the attitude
    controller's tracking; a gain or filter change shows here before it shows in the path)."""
    air = [s for s in _series(run, "EXTENDED_SYS_STATE")]
    if not air:
        return {}
    spans, start = [], None
    for s in air:
        if s["landed_state"] == IN_AIR and start is None:
            start = s["t"]
        elif s["landed_state"] != IN_AIR and start is not None:
            spans.append((start, s["t"]))
            start = None
    if start is not None:
        spans.append((start, air[-1]["t"]))
    att = _series(run, "ATTITUDE")
    times = [a["t"] for a in att]
    errs = []
    for t0, t1 in spans:
        for s in _series(run, "ATTITUDE_TARGET", t0, t1):
            i = bisect.bisect_right(times, s["t"]) - 1
            if i < 0:
                continue
            r, p = _quat_rp(s["q"])
            errs.append(math.degrees(math.hypot(att[i]["roll"] - r, att[i]["pitch"] - p)))
    return {"att_err_rms_deg": math.sqrt(st.fmean(e * e for e in errs))} if errs else {}


def _landing_metrics(run: dict, armed, segs) -> dict:
    landed = _event_time(run, "disarmed")
    if landed is None or armed is None:
        return {}
    landed = _text_time(run, DISARMED, armed) or landed
    m = {"flight_time_s": landed - armed}
    pos = _series(run, "LOCAL_POSITION_NED", None, landed)
    if not pos:
        return m
    last = pos[-1]
    m["landing_offset_m"] = math.hypot(last["x"], last["y"])
    touchdown = _touchdown(run, landed)
    if touchdown is not None:
        m["disarm_delay_s"] = landed - touchdown
        ground = _series(run, "LOCAL_POSITION_NED", None, touchdown)
        gz = ground[-1]["z"]
        low = [p["vz"] for p in ground if gz - p["z"] < 2.0 and p["t"] > touchdown - 10]
        if low:
            m["touchdown_speed_mps"] = max(low)
    # a landing commanded where the vehicle is (AUTO.LAND): how far it moved on the way down
    land = next((t0 for mode, t0, _ in reversed(segs) if mode == "AUTO.LAND" and t0 < landed), None)
    if land is not None:
        p0 = next((p for p in pos if p["t"] >= land), None)
        if p0 is not None:
            m["land_drift_m"] = math.hypot(last["x"] - p0["x"], last["y"] - p0["y"])
    return m
