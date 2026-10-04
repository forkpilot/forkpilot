"""Turn a recorded Run into a flat dict of numeric metrics.

Every metric here is deterministic arithmetic on telemetry — no AI involved.
"""
from __future__ import annotations

import math
import statistics as st


def _series(run, kind, t0=None, t1=None):
    return [s for s in run["samples"] if s["mavpackettype"] == kind
            and (t0 is None or s["t"] >= t0) and (t1 is None or s["t"] <= t1)]


def _event_time(run, kind, nth=0):
    hits = [t for t, k, _ in run["events"] if k == kind]
    return hits[nth] if len(hits) > nth else None


def _windows(run, start_kind, end_kinds):
    """Yield (t0, t1) for each start event up to the next event of any kind in end_kinds."""
    ev = run["events"]
    for i, (t, k, _) in enumerate(ev):
        if k != start_kind:
            continue
        t1 = next((t2 for t2, k2, _ in ev[i + 1:] if k2 in end_kinds or k2 == start_kind), None)
        yield t, t1


def compute(run: dict) -> dict:
    if run.get("source") == "autotest":
        return compute_generic(run)
    if run.get("vehicle") == "px4":
        from .px4_metrics import compute as px4
        return px4(run)
    if run.get("vehicle") == "plane":
        from .plane_metrics import compute as plane
        return plane(run)
    m: dict[str, float] = {}
    m["completed"] = 1.0 if run["ok"] else 0.0

    # timing origin: the moment the firmware armed. The harness only sees arming in the 1 Hz
    # HEARTBEAT and sends takeoff 0-1 s later, but the climb is paced by motor spool-up from the
    # arming moment, so timing from the takeoff command made takeoff time bimodal (7.9 / 8.8 s)
    armed = next((t for t, k, d in run["events"] if k == "statustext" and d == "Arming motors"), None)
    if armed is None:
        armed = _event_time(run, "takeoff_cmd")
    if armed is None:
        armed = _event_time(run, "armed")
    took_off = _event_time(run, "takeoff_done")
    if armed is not None and took_off is not None:
        m["takeoff_time_s"] = took_off - armed

    # Hover quality: horizontal drift and altitude error relative to where hold started
    drifts, alt_errs, tilt = [], [], []
    for t0, t1 in _windows(run, "hold_start", {"goto", "mode", "set_param", "error"}):
        pos = _series(run, "LOCAL_POSITION_NED", t0, t1)
        if len(pos) < 5:
            continue
        x0, y0, z0 = pos[0]["x"], pos[0]["y"], pos[0]["z"]
        drifts += [math.hypot(p["x"] - x0, p["y"] - y0) for p in pos]
        alt_errs += [abs(p["z"] - z0) for p in pos]
        att = _series(run, "ATTITUDE", t0, t1)
        tilt += [math.degrees(math.hypot(a["roll"], a["pitch"])) for a in att]
    if drifts:
        m["hold_drift_max_m"] = max(drifts)
        m["hold_alt_err_max_m"] = max(alt_errs)
        m["hold_tilt_rms_deg"] = math.sqrt(st.fmean(x * x for x in tilt)) if tilt else 0.0

    # Waypoint tracking: time to arrive, and how far past the target the vehicle
    # travels along the leg direction before the next command takes over
    legs, overshoots = [], []
    for t0, t1 in _windows(run, "goto", {"arrived", "error"}):
        if t1 is not None:
            legs.append(t1 - t0)
    for t0, t1 in _windows(run, "goto", {"mode", "set_param", "error"}):
        target = next(d for t, k, d in run["events"] if k == "goto" and t == t0)
        pos = _series(run, "LOCAL_POSITION_NED", t0, t1)
        if len(pos) < 2:
            continue
        sx, sy = pos[0]["x"], pos[0]["y"]
        dx, dy = target[0] - sx, target[1] - sy
        length = math.hypot(dx, dy)
        if length < 1:
            continue
        ux, uy = dx / length, dy / length
        overshoots.append(max(0.0, max((p["x"] - target[0]) * ux + (p["y"] - target[1]) * uy
                                       for p in pos)))
    if legs:
        m["goto_time_total_s"] = sum(legs)
    if overshoots:
        m["goto_overshoot_max_m"] = max(overshoots)

    # Failsafe reaction: after the first injected fault, how long until the mode changes
    # Compare against the mode *before* the fault: HEARTBEAT is 1 Hz, so a sub-second reaction
    # can already show in the first heartbeat after the fault (resolution is ~1 s either way)
    fault = _event_time(run, "set_param")
    if fault is not None:
        hb = [s for s in _series(run, "HEARTBEAT") if s.get("type") != 6]
        before = [s["custom_mode"] for s in hb if s["t"] <= fault]
        if before:
            change = next((s["t"] for s in hb if s["t"] > fault and s["custom_mode"] != before[-1]),
                          None)
            m["failsafe_reaction_s"] = (change - fault) if change is not None else float("inf")

    # AUTO missions: distinct waypoints reported reached, and time spent in AUTO
    if any(k == "mission" for _, k, _ in run["events"]):
        m["mission_wp_reached"] = float(len({d for _, k, d in run["events"] if k == "wp_reached" and d}))
        auto = next((t for t, k, d in run["events"] if k == "mode" and d == "AUTO"), None)
        end = _event_time(run, "disarmed")
        if auto is not None and end is not None:
            m["mission_time_s"] = end - auto
            # cruise speed: on short legs the total time hides a speed limit change
            pos = _series(run, "LOCAL_POSITION_NED", auto, end)
            if pos:
                m["mission_speed_max_mps"] = max(math.hypot(p["vx"], p["vy"]) for p in pos)

    m.update(_stop_metrics(run))
    m.update(_velocity_metrics(run))
    m.update(_mode_metrics(run))

    gpi = _series(run, "GLOBAL_POSITION_INT")
    if gpi:
        m["alt_max_m"] = max(s["relative_alt"] for s in gpi) / 1000
    pos = _series(run, "LOCAL_POSITION_NED")
    if pos:
        # how far from home it ever got: a refused target or a fence must keep this small
        m["range_max_m"] = max(math.hypot(p["x"], p["y"]) for p in pos)

    landed = _event_time(run, "disarmed")
    if landed is not None and armed is not None:
        # the STATUSTEXT is stamped from fast telemetry; the "disarmed" event from 1 Hz HEARTBEAT
        landed = next((t for t, k, d in run["events"] if k == "statustext"
                       and d == "Disarming motors" and t > armed), landed)
        m["flight_time_s"] = landed - armed
        last = _series(run, "LOCAL_POSITION_NED")[-1]
        m["landing_offset_m"] = math.hypot(last["x"], last["y"])
        touchdown = _touchdown(run, landed)
        if touchdown is not None:
            m["disarm_delay_s"] = landed - touchdown
            # how hard it lands: fastest descent in the last 2 m before touchdown
            ground = _series(run, "LOCAL_POSITION_NED", None, touchdown)
            if ground:
                gz = ground[-1]["z"]
                low = [p["vz"] for p in ground if gz - p["z"] < 2.0 and p["t"] > touchdown - 10]
                if low:
                    m["touchdown_speed_mps"] = max(low)
    return m


def _velocity_metrics(run: dict) -> dict:
    """GUIDED velocity commands: how far the flown velocity is from the command once settled
    (after 3 s), in the earth frame; a body-frame command is rotated by the measured yaw."""
    m: dict[str, float] = {}
    ev = run["events"]
    n = 0
    for i, (t0, k, d) in enumerate(ev):
        if k != "velocity":
            continue
        t1 = next((t for t, k2, _ in ev[i + 1:] if k2 == "velocity_end"), None)
        pos = _series(run, "LOCAL_POSITION_NED", t0 + 3, t1)
        att = _series(run, "ATTITUDE", t0 + 3, t1)
        if t1 is None or len(pos) < 5 or not att:
            continue
        n += 1
        vx, vy, _ = d["v"]
        if d["frame"] == "body":
            yaw = math.atan2(st.fmean(math.sin(a["yaw"]) for a in att),
                             st.fmean(math.cos(a["yaw"]) for a in att))
            vx, vy = vx * math.cos(yaw) - vy * math.sin(yaw), vx * math.sin(yaw) + vy * math.cos(yaw)
        ax, ay = st.fmean(p["vx"] for p in pos), st.fmean(p["vy"] for p in pos)
        m[f"vel_err_mps_{n}"] = math.hypot(ax - vx, ay - vy)
    return m


def _mode_metrics(run: dict) -> dict:
    """Per segment of a mode the scenario switched to (a segment ends at the next mode change,
    stick input or command; at least 5 s long): mean ground speed and mean yaw rate. Catches a
    mode that flies at the wrong rate or turns when it should not."""
    m: dict[str, float] = {}
    seen: dict[str, int] = {}
    ev = run["events"]
    cuts = {"mode", "sticks", "release", "release_end", "goto", "send_goto", "velocity", "yaw",
            "set_param", "error", "disarmed"}
    mode = None
    for i, (t0, k, d) in enumerate(ev):
        if k == "mode":
            mode = d
        if mode is None or k not in ("mode", "sticks", "release"):
            continue
        t1 = next((t for t, k2, _ in ev[i + 1:] if k2 in cuts), None)
        if t1 is None or t1 - t0 < 5:
            continue
        pos = _series(run, "LOCAL_POSITION_NED", t0, t1)
        att = _series(run, "ATTITUDE", t0, t1)
        if len(pos) < 5 or len(att) < 5:
            continue
        name = str(mode).lower()
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name += str(seen[name])
        m[f"mode_speed_mps_{name}"] = st.fmean(math.hypot(p["vx"], p["vy"]) for p in pos)
        m[f"mode_yaw_rate_dps_{name}"] = math.degrees(st.fmean(abs(a["yawspeed"]) for a in att))
    return m


def _stop_metrics(run: dict) -> dict:
    """How the vehicle stops when the pilot lets go of the sticks, per release, named by mode:
    stop_dist (how far it coasts), stop_backtrack (how far it then comes back towards the
    release point — a vehicle that overshoots its hold point and returns), stop_time (until
    under 0.3 m/s) and stop_alt_dev (height change while stopping)."""
    m: dict[str, float] = {}
    seen: dict[str, int] = {}
    ev = run["events"]
    for i, (t0, k, mode) in enumerate(ev):
        if k != "release":
            continue
        t1 = next((t for t, k2, _ in ev[i + 1:] if k2 == "release_end"), None)
        pos = _series(run, "LOCAL_POSITION_NED", t0, t1)
        if t1 is None or len(pos) < 2:
            continue
        name = str(mode).lower()
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name += str(seen[name])
        x0, y0, z0 = pos[0]["x"], pos[0]["y"], pos[0]["z"]
        dist = [math.hypot(p["x"] - x0, p["y"] - y0) for p in pos]
        m[f"stop_dist_m_{name}"] = max(dist)
        m[f"stop_backtrack_m_{name}"] = max(dist) - dist[-1]
        slow = next((p["t"] for p in pos if math.hypot(p["vx"], p["vy"]) < 0.3), None)
        m[f"stop_time_s_{name}"] = (slow - t0) if slow is not None else (t1 - t0)
        m[f"stop_alt_dev_m_{name}"] = max(abs(p["z"] - z0) for p in pos)
        m[f"stick_speed_mps_{name}"] = math.hypot(pos[0]["vx"], pos[0]["vy"])
    return m


def compute_generic(run: dict) -> dict:
    """Metrics for an upstream autotest test. Its steps are unknown to us, so only quantities
    every flight has: autotest's own pass/fail, how long and how high and fast it flew, how
    hard it tilted, where it ended up."""
    m: dict[str, float] = {"completed": 1.0 if run["ok"] else 0.0}
    samples = run["samples"]
    if not samples:
        return m
    m["sim_time_s"] = samples[-1]["t"] - samples[0]["t"]
    ev = run["events"]
    m["mode_changes"] = float(sum(1 for _, k, _ in ev if k == "mode"))
    arms = [t for t, k, _ in ev if k == "armed"]
    disarms = [t for t, k, _ in ev if k == "disarmed"]
    m["arm_count"] = float(len(arms))
    if arms:
        flown = 0.0
        for a in arms:
            d = next((t for t in disarms if t > a), samples[-1]["t"])
            flown += d - a
        m["armed_time_s"] = flown
    gpi = _series(run, "GLOBAL_POSITION_INT")
    if gpi:
        m["alt_max_m"] = max(s["relative_alt"] for s in gpi) / 1000
        m["speed_max_mps"] = max(math.hypot(s["vx"], s["vy"]) for s in gpi) / 100
        m["climb_max_mps"] = max(-s["vz"] for s in gpi) / 100
        m["sink_max_mps"] = max(s["vz"] for s in gpi) / 100
    att = _series(run, "ATTITUDE")
    if att:
        m["tilt_max_deg"] = max(math.degrees(math.hypot(a["roll"], a["pitch"])) for a in att)
    pos = _series(run, "LOCAL_POSITION_NED")
    if pos:
        m["final_offset_m"] = math.hypot(pos[-1]["x"], pos[-1]["y"])
    return m


def _touchdown(run, disarm_t, still=0.15):
    """Earliest time after which the vehicle stays on the ground (altitude at its final value,
    no vertical motion) until it disarms."""
    pos = [p for p in _series(run, "LOCAL_POSITION_NED", None, disarm_t)]
    if len(pos) < 5:
        return None
    ground = pos[-1]["z"]
    t = None
    for p in reversed(pos):
        if abs(p["z"] - ground) < still and abs(p["vz"]) < still:
            t = p["t"]
        else:
            break
    return t
