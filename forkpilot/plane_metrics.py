"""Metrics for ArduPlane and QuadPlane runs. Deterministic arithmetic on telemetry, as in metrics.py.

Geometry comes from what the scenario asked for (mission items, home), not from what the firmware
reports about its own errors, so a bug that moves the controller's target is still visible. The
firmware's own TECS airspeed error (NAV_CONTROLLER_OUTPUT) is used only for airspeed tracking.
"""
from __future__ import annotations

import bisect
import math
import statistics as st

from pymavlink import mavutil

from .metrics import _event_time, _mode_metrics, _series, _touchdown, _windows

GCS_TYPE = 6
LOITER_MODES = {"LOITER", "RTL", "CIRCLE", "GUIDED"}
LOCATED = {"wp", "loiter_turns", "land", "vtol_land"}
MC, FW, TO_FW, TO_MC = (mavutil.mavlink.MAV_VTOL_STATE_MC, mavutil.mavlink.MAV_VTOL_STATE_FW,
                        mavutil.mavlink.MAV_VTOL_STATE_TRANSITION_TO_FW,
                        mavutil.mavlink.MAV_VTOL_STATE_TRANSITION_TO_MC)


def compute(run: dict) -> dict:
    m: dict[str, float] = {"completed": 1.0 if run["ok"] else 0.0}
    ev = run["events"]
    armed = next((t for t, k, d in ev if k == "statustext" and d == "Throttle armed"), None)
    if armed is None:
        armed = _event_time(run, "takeoff_cmd")
    if armed is None:
        armed = _event_time(run, "armed")
    took_off = _event_time(run, "takeoff_done")
    if armed is not None and took_off is not None:
        m["takeoff_time_s"] = took_off - armed

    mission = next((d for t, k, d in reversed(ev) if k == "mission"), None) or []
    if mission:
        m["mission_wp_reached"] = float(len({d for _, k, d in ev if k == "wp_reached" and d}))
    m.update(_leg_metrics(run, mission))
    m.update(_loiter_metrics(run))
    m.update(_hover_metrics(run))
    if run.get("frame") == "quadplane":
        m.update(_transition_metrics(run))
    m.update(_airspeed_metrics(run, took_off))
    m.update(_failsafe_metrics(run))
    m.update(_mode_metrics(run))

    gpi = _series(run, "GLOBAL_POSITION_INT")
    if gpi:
        m["alt_max_m"] = max(s["relative_alt"] for s in gpi) / 1000
    pos = _series(run, "LOCAL_POSITION_NED")
    if pos:
        m["range_max_m"] = max(math.hypot(p["x"], p["y"]) for p in pos)
    m.update(_landing_metrics(run, armed, mission))
    return {k: max(v, _floor(k)) for k, v in m.items()}


# Noise floors, from A/A runs of one binary (30 per scenario): below these the values split into
# run-to-run clusters (e.g. QRTL back-transition height loss 3.3 m or 4.2 m, QLOITER yaw rate
# 0.3-0.6 deg/s, 1.6-1.9 deg/s rolling out of a turn) and a 5-run baseline band, sized for larger
# values, flags them. A 2024 build added rarer clusters a 5-run baseline can miss: VTOL assist 1.0 s
# or 1.2 s, level-leg height error RMS 0.45-0.53 m. A value under its floor carries no information,
# so it is reported as the floor; a regression above it still shows.
FLOORS = {"loiter_centre_err_m": 5.0, "transition_fw_alt_loss_m": 5.0, "back_transition_alt_loss_m": 5.0,
          "mode_yaw_rate_dps": 2.0, "mode_speed_mps": 0.5, "landing_offset_m": 1.0, "touchdown_err_m": 1.0,
          "assist_time_s": 2.0, "leg_alt_err_rms_m": 0.6}


def _floor(key: str) -> float:
    return next((f for prefix, f in FLOORS.items() if key.startswith(prefix)), -math.inf)


def _at(series: list[dict], t: float) -> dict | None:
    """The last sample at or before t."""
    key = id(series)
    if _TIMES.get(key, (None,))[0] is not series:     # times of a series, kept while it lives
        if len(_TIMES) > 16:
            _TIMES.clear()
        _TIMES[key] = (series, [s["t"] for s in series])
    i = bisect.bisect_right(_TIMES[key][1], t) - 1
    return series[i] if i >= 0 else None


_TIMES: dict[int, tuple] = {}


def _rms(xs) -> float:
    xs = list(xs)
    return math.sqrt(st.fmean(x * x for x in xs)) if xs else 0.0


def _mode_segments(run: dict) -> list[tuple[str, float, float]]:
    """(mode name, start, end) from the vehicle's HEARTBEAT, whichever way the mode changed
    (scenario step, failsafe, mission end)."""
    hb = [s for s in _series(run, "HEARTBEAT") if s.get("type") != GCS_TYPE]
    out, end = [], (run["samples"][-1]["t"] if run["samples"] else 0.0)
    for s in hb:
        names = mavutil.mode_mapping_bynumber(s["type"]) or {}
        name = names.get(s["custom_mode"], str(s["custom_mode"]))
        if not out or out[-1][0] != name:
            if out:
                out[-1][2] = s["t"]
            out.append([name, s["t"], end])
    return [tuple(x) for x in out]


def _leg_metrics(run: dict, mission: list[dict]) -> dict:
    """Mission legs between two located items, flown to their end in AUTO: cross-track error (whole
    leg, and settled over its second half), height error on level legs, airspeed and TECS airspeed
    error. A leg cut short by a failsafe or the pilot is left out: where it ends depends on timing."""
    cur = _series(run, "MISSION_CURRENT")
    pos = _series(run, "LOCAL_POSITION_NED")
    if not mission or not cur or not pos:
        return {}
    hud = _series(run, "VFR_HUD")
    nav = _series(run, "NAV_CONTROLLER_OUTPUT")
    # the mission's current item stays put when a failsafe or the pilot takes over
    auto = [(t0, t1) for mode, t0, t1 in _mode_segments(run) if mode == "AUTO"]
    xt_all, xt_late, alt_late, aspd, aspd_err = [], [], [], [], []
    prev = None
    for item in mission[1:]:
        if item["kind"] not in LOCATED:
            continue
        a, prev = prev, item
        if a is None or item["kind"] != "wp":
            continue
        span = [s["t"] for s in cur if s["seq"] == item["seq"]
                and any(a0 <= s["t"] <= a1 for a0, a1 in auto)]
        if not span or not any(s["seq"] > item["seq"] and any(a0 <= s["t"] <= a1 for a0, a1 in auto)
                               for s in cur):
            continue
        t0, t1 = span[0], span[-1]
        ax, ay, bx, by = a["n"], a["e"], item["n"], item["e"]
        length = math.hypot(bx - ax, by - ay)
        if length < 50:
            continue
        ux, uy = (bx - ax) / length, (by - ay) / length
        late = []
        for p in pos:
            if not t0 <= p["t"] <= t1:
                continue
            along = (p["x"] - ax) * ux + (p["y"] - ay) * uy
            cross = abs((p["x"] - ax) * uy - (p["y"] - ay) * ux)
            xt_all.append(cross)
            if along >= 0.5 * length:
                xt_late.append(cross)
                late.append(p["t"])
                if a["up"] == item["up"]:
                    alt_late.append(-p["z"] - item["up"])
        if late:
            lo, hi = late[0], late[-1]
            aspd += [h["airspeed"] for h in hud if lo <= h["t"] <= hi]
            aspd_err += [n["aspd_error"] / 100 for n in nav if lo <= n["t"] <= hi]
    m = {}
    if xt_all:
        m["leg_xtrack_max_m"] = max(xt_all)
    if xt_late:
        m["leg_xtrack_rms_m"] = _rms(xt_late)
    if alt_late:
        m["leg_alt_err_rms_m"] = _rms(alt_late)
    if aspd:
        m["leg_airspeed_mps"] = st.fmean(aspd)
    if aspd_err:
        m["leg_aspd_err_rms_mps"] = _rms(aspd_err)
    return m


def fit_circle(pts: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Least-squares circle (Kasa): x² + y² + D·x + E·y + F = 0. Returns (cx, cy, r)."""
    n = len(pts)
    if n < 10:
        return None
    sx = sum(x for x, _ in pts); sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts); syy = sum(y * y for _, y in pts); sxy = sum(x * y for x, y in pts)
    z = [x * x + y * y for x, y in pts]
    sz = sum(z); sxz = sum(x * w for (x, _), w in zip(pts, z)); syz = sum(y * w for (_, y), w in zip(pts, z))
    a = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]]
    b = [-sxz, -syz, -sz]
    det = _det(a)
    if abs(det) < 1e-9:
        return None
    sol = []
    for i in range(3):
        ai = [row[:] for row in a]
        for r in range(3):
            ai[r][i] = b[r]
        sol.append(_det(ai) / det)
    d, e, f = sol
    cx, cy = -d / 2, -e / 2
    r2 = cx * cx + cy * cy - f
    return (cx, cy, math.sqrt(r2)) if r2 > 0 else None


def _det(a):
    return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
            - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))


def _loiter_metrics(run: dict) -> dict:
    """Fixed-wing loiter (LOITER, RTL, CIRCLE, GUIDED), each segment of 45 s or more: fitted
    radius, how far the track strays from that circle, the circle centre's offset from where it
    should be (home for RTL, the entry point for LOITER) and height steadiness. The first part of
    each segment (flying to the circle) is skipped: only the last half, at least 30 s of it."""
    m: dict[str, float] = {}
    seen: dict[str, int] = {}
    pos = _series(run, "LOCAL_POSITION_NED")
    vtol = _series(run, "EXTENDED_SYS_STATE")
    for mode, t0, t1 in _mode_segments(run):
        if mode not in LOITER_MODES or t1 - t0 < 45:
            continue
        start = max(t0 + (t1 - t0) / 2, t0 + 15)
        if t1 - start < 30:
            start = t1 - 30
        win = [p for p in pos if start <= p["t"] <= t1]
        if run.get("frame") == "quadplane":
            win = [p for p in win if (_at(vtol, p["t"]) or {}).get("vtol_state") == FW]
        fit = fit_circle([(p["x"], p["y"]) for p in win])
        if fit is None:
            continue
        cx, cy, r = fit
        name = mode.lower()
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name += str(seen[name])
        m[f"loiter_radius_m_{name}"] = r
        m[f"loiter_track_err_m_{name}"] = _rms(math.hypot(p["x"] - cx, p["y"] - cy) - r for p in win)
        m[f"loiter_alt_sd_m_{name}"] = st.pstdev(-p["z"] for p in win)
        entry = _at(pos, t0)
        centre = {"RTL": (0.0, 0.0), "LOITER": (entry["x"], entry["y"]) if entry else None}.get(mode)
        if centre:
            m[f"loiter_centre_err_m_{name}"] = math.hypot(cx - centre[0], cy - centre[1])
    return m


def _hover_metrics(run: dict) -> dict:
    """QuadPlane `hold` in a VTOL mode: drift and height change, as Copter's hover metrics."""
    drifts, alt_errs = [], []
    for t0, t1 in _windows(run, "hold_start", {"goto", "mode", "set_param", "sticks", "error"}):
        pos = _series(run, "LOCAL_POSITION_NED", t0, t1)
        if len(pos) < 5:
            continue
        x0, y0, z0 = pos[0]["x"], pos[0]["y"], pos[0]["z"]
        drifts += [math.hypot(p["x"] - x0, p["y"] - y0) for p in pos]
        alt_errs += [abs(p["z"] - z0) for p in pos]
    return {"hold_drift_max_m": max(drifts), "hold_alt_err_max_m": max(alt_errs)} if drifts else {}


def _transition_metrics(run: dict) -> dict:
    """QuadPlane transitions from the reported VTOL state. Forward (hover to wing-borne): time
    and height lost. Back (wing-borne to hover): time and distance until slowed under 3 m/s,
    height lost. Assisted flight (motors helping a wing-borne plane, e.g. near stall) is the
    time spent in the forward-transition state after the plane was already wing-borne."""
    states = [(s["t"], s["vtol_state"]) for s in _series(run, "EXTENDED_SYS_STATE")
              if s["landed_state"] != mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND]
    pos = _series(run, "LOCAL_POSITION_NED")
    if not states or not pos:
        return {}
    segs = []
    for t, v in states:
        if not segs or segs[-1][0] != v:
            if segs:
                segs[-1][2] = t
            segs.append([v, t, t])
    segs[-1][2] = states[-1][0]
    m: dict[str, float] = {"assist_time_s": 0.0}
    fwd = back = 0
    for i, (v, t0, t1) in enumerate(segs):
        before = segs[i - 1][0] if i else None
        after = segs[i + 1][0] if i + 1 < len(segs) else None
        if v == TO_FW and before == MC and after == FW:
            fwd += 1
            p0 = _at(pos, t0)
            low = min(-p["z"] for p in pos if t0 <= p["t"] <= t1 + 3)
            m[f"transition_fw_time_s_{fwd}"] = t1 - t0
            m[f"transition_fw_alt_loss_m_{fwd}"] = -p0["z"] - low
        elif v == TO_FW and before == FW:
            m["assist_time_s"] += t1 - t0
        elif v in (TO_MC, MC) and before == FW:
            back += 1
            p0 = _at(pos, t0)
            end = next((p for p in pos if p["t"] > t0 and math.hypot(p["vx"], p["vy"]) < 3), None)
            if p0 is None or end is None:
                continue
            m[f"back_transition_time_s_{back}"] = end["t"] - t0
            m[f"back_transition_dist_m_{back}"] = math.hypot(end["x"] - p0["x"], end["y"] - p0["y"])
            m[f"back_transition_alt_loss_m_{back}"] = -p0["z"] - min(
                -p["z"] for p in pos if t0 <= p["t"] <= end["t"] + 2)
    return m


def _airspeed_metrics(run: dict, took_off: float | None) -> dict:
    """Lowest and highest airspeed in wing-borne flight above 20 m (stall and overspeed margin):
    after takeoff, and for a QuadPlane only while the VTOL state says wing-borne."""
    if took_off is None:
        return {}
    pos = _series(run, "LOCAL_POSITION_NED", took_off)
    vtol = _series(run, "EXTENDED_SYS_STATE")
    quad = run.get("frame") == "quadplane"
    speeds = []
    for h in _series(run, "VFR_HUD", took_off):
        p = _at(pos, h["t"])
        if p is None or -p["z"] < 20:
            continue
        if quad and (_at(vtol, h["t"]) or {}).get("vtol_state") != FW:
            continue
        speeds.append(h["airspeed"])
    return {"airspeed_min_mps": min(speeds), "airspeed_max_mps": max(speeds)} if speeds else {}


def _failsafe_metrics(run: dict) -> dict:
    """After the first injected fault: seconds until the mode changes, and the mode it changes
    to (its number: a different failsafe action shows as a step in this value)."""
    fault = _event_time(run, "set_param")
    if fault is None:
        return {}
    hb = [s for s in _series(run, "HEARTBEAT") if s.get("type") != GCS_TYPE]
    before = [s["custom_mode"] for s in hb if s["t"] <= fault]
    if not before:
        return {}
    change = next((s for s in hb if s["t"] > fault and s["custom_mode"] != before[-1]), None)
    if change is None:
        return {"failsafe_reaction_s": float("inf")}
    return {"failsafe_reaction_s": change["t"] - fault, "failsafe_mode": float(change["custom_mode"])}


def _landing_metrics(run: dict, armed: float | None, mission: list[dict]) -> dict:
    landed = _event_time(run, "disarmed")
    if landed is None or armed is None:
        return {}
    landed = next((t for t, k, d in run["events"] if k == "statustext"
                   and d == "Throttle disarmed" and t > armed), landed)
    m = {"flight_time_s": landed - armed}
    pos = _series(run, "LOCAL_POSITION_NED", None, landed)
    if not pos:
        return m
    last = pos[-1]
    m["landing_offset_m"] = math.hypot(last["x"], last["y"])
    touchdown = _touchdown(run, landed)
    if touchdown is None:
        return m
    p = _at(pos, touchdown)
    m["disarm_delay_s"] = landed - touchdown
    m["touchdown_gs_mps"] = math.hypot(p["vx"], p["vy"])
    m["rollout_m"] = math.hypot(last["x"] - p["x"], last["y"] - p["y"])
    gz = p["z"]
    low = [q["vz"] for q in pos if q["t"] <= touchdown and gz - q["z"] < 2.0 and q["t"] > touchdown - 10]
    if low:
        m["touchdown_speed_mps"] = max(low)
    # where it touched down against the mission's landing point, along and across the approach
    located = [i for i in mission[1:] if i["kind"] in LOCATED]
    cur = _at(_series(run, "MISSION_CURRENT"), touchdown)
    if located and located[-1]["kind"] in ("land", "vtol_land") and cur and cur["seq"] == located[-1]["seq"]:
        land = located[-1]
        dx, dy = p["x"] - land["n"], p["y"] - land["e"]
        m["touchdown_err_m"] = math.hypot(dx, dy)
        if len(located) > 1 and land["kind"] == "land":
            a = located[-2]
            length = math.hypot(land["n"] - a["n"], land["e"] - a["e"])
            if length > 1:
                ux, uy = (land["n"] - a["n"]) / length, (land["e"] - a["e"]) / length
                m["touchdown_along_m"] = dx * ux + dy * uy
                m["touchdown_cross_m"] = abs(dx * uy - dy * ux)
    return m
