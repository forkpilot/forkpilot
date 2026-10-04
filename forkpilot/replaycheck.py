"""Compare an original flight with the replay of its generated scenario, both DataFlash logs.

  python -m forkpilot.cli replaycheck original.bin replay.bin --start 50.9 [--replay-start 31.2]

Time 0 of the comparison is the start of the scenario's stick/mode steps: --start is the log time
`fromlog` printed as the window start, --replay-start is the same moment in the replay (default:
the first mode change after the replay's takeoff). Positions are the EKF's, relative to where each
vehicle was at that moment, so the replay's own takeoff does not count as error.
"""
from __future__ import annotations

import bisect
import math
import statistics as st

from pymavlink import DFReader

from .fromlog import PILOT_MODES, ROLES, compress, mode_at, read_flight, snap, stick_series


def read_track(path):
    """[(t, north, east, down, vn, ve)] of EKF core 0 (XKF1, or NKF1 on older logs)."""
    log = DFReader.DFReader_binary(str(path), zero_time_base=False)
    out = []
    while (m := log.recv_match(type=["XKF1", "NKF1"])) is not None:
        if getattr(m, "C", 0) == 0:
            out.append((m.TimeUS / 1e6, m.PN, m.PE, m.PD, m.VN, m.VE))
    if not out:
        raise ValueError(f"{path}: no EKF position (XKF1/NKF1) in the log")
    return out


class Track:
    def __init__(self, rows, t0):
        self.t = [r[0] for r in rows]
        self.rows, self.t0 = rows, t0
        i = bisect.bisect_left(self.t, t0)
        self.p0 = rows[min(i, len(rows) - 1)][1:4]

    def at(self, t):
        """Linear interpolation of position relative to the start, and velocity."""
        i = min(max(bisect.bisect_left(self.t, t), 1), len(self.t) - 1)
        a, b = self.rows[i - 1], self.rows[i]
        w = 0.0 if b[0] == a[0] else min(max((t - a[0]) / (b[0] - a[0]), 0), 1)
        v = [x + w * (y - x) for x, y in zip(a[1:], b[1:])]
        return [v[0] - self.p0[0], v[1] - self.p0[1], v[2] - self.p0[2], v[3], v[4]]


def landing(rows, t0, high=2.0, low=0.3):
    """Seconds after t0 when the vehicle, having been above `high` m, is back below `low` m."""
    up = False
    for t, _, _, d, *_ in rows:
        if t < t0:
            continue
        up = up or -d > high
        if up and -d < low:
            return t - t0
    return None


def _stats(xs):
    xs = sorted(xs)
    return {"mean": st.fmean(xs), "p95": xs[int(0.95 * (len(xs) - 1))], "max": xs[-1], "final": None}


def release_windows(f, t0, t1, tol=20.0, min_len=2.0):
    """[(a, b)] stretches where roll, pitch and yaw are centred for at least min_len seconds, in
    a mode the pilot flies."""
    segs = [(a, b, snap(v, tol)) for a, b, v in compress(stick_series(f, t0, t1), t1, tol, 0.3)]
    out = []
    for a, b, v in segs:
        if v[0] == v[1] == v[3] == 1500 and b - a >= min_len and mode_at(f, a + 0.25) in PILOT_MODES:
            if out and out[-1][1] == a:
                out[-1] = (out[-1][0], b)
            else:
                out.append((a, b))
    return out


def stop(track, a, b):
    """How the vehicle stops after the sticks centre at a: coast distance, time to under 0.3 m/s,
    height change, speed at release."""
    ts = [a + i * 0.1 for i in range(int((b - a) / 0.1) + 1)]
    rows = [track.at(t) for t in ts]
    d = [math.hypot(r[0] - rows[0][0], r[1] - rows[0][1]) for r in rows]
    slow = next((t for t, r in zip(ts, rows) if math.hypot(r[3], r[4]) < 0.3), None)
    return {"speed": math.hypot(rows[0][3], rows[0][4]), "dist": max(d), "time": (slow if slow else b) - a,
            "alt_dev": max(abs(r[2] - rows[0][2]) for r in rows)}


def compare(orig, replay, start, replay_start=None, end=None, tol=20.0, release_min=2.0):
    fo, fr = read_flight(orig), read_flight(replay)
    if replay_start is None:
        # the replay takes off in GUIDED; its next mode change is the scenario's first `mode` step
        first = next((t for t, n, _ in fr.modes if n != "GUIDED"), None)
        if first is None:
            raise ValueError("no mode change after takeoff in the replay; pass --replay-start")
        replay_start = first
    to, tr = read_track(orig), read_track(replay)
    t_end_o = min(x for x in (end, fo.arms[-1][0] if not fo.arms[-1][1] else fo.t1, fo.t1) if x is not None)
    dur = min(t_end_o - start, fr.t1 - replay_start, tr[-1][0] - replay_start)
    # a vehicle sitting on the ground after landing agrees trivially: stop at the earlier touchdown
    down = [landing(to, start), landing(tr, replay_start)]
    if all(d is not None for d in down):
        dur = min(dur, min(down) + 1.0)
    ao, ar = Track(to, start), Track(tr, replay_start)
    ts = [i * 0.1 for i in range(int(dur / 0.1) + 1)]
    eh, ev = [], []
    for t in ts:
        o, r = ao.at(start + t), ar.at(replay_start + t)
        eh.append(math.hypot(o[0] - r[0], o[1] - r[1]))
        ev.append(abs(o[2] - r[2]))
    res = {"window_s": dur, "horizontal_m": _stats(eh), "vertical_m": _stats(ev)}
    res["horizontal_m"]["final"], res["vertical_m"]["final"] = eh[-1], ev[-1]
    path = lambda trk, t0: sum(math.dist(trk.at(t0 + a)[:2], trk.at(t0 + a + 0.1)[:2]) for a in ts[:-1])
    res["path_m"] = {"orig": path(ao, start), "replay": path(ar, replay_start)}
    res["max_from_start_m"] = {
        "orig": max(math.hypot(*ao.at(start + t)[:2]) for t in ts),
        "replay": max(math.hypot(*ar.at(replay_start + t)[:2]) for t in ts)}
    # modes entered in the window, relative to its start; the replay's takeoff GUIDED is before it
    mo = [(t - start, n) for t, n, _ in fo.modes if -0.2 <= t - start <= dur]
    mr = [(t - replay_start, n) for t, n, _ in fr.modes if t - replay_start >= -0.2]
    res["modes"] = [{"mode": n, "orig_s": t, "replay_s": mr[i][0] if i < len(mr) and mr[i][1] == n else None}
                    for i, (t, n) in enumerate(mo)]
    res["mode_sequence_equal"] = [n for _, n in mo] == [n for _, n in mr][:len(mo)]
    # stops
    wo = release_windows(fo, start, start + dur, tol, release_min)
    wr = release_windows(fr, replay_start, replay_start + dur + 30, tol, release_min)
    stops = []
    for (a, b), (c, d) in zip(wo, wr):
        stops.append({"at_s": a - start, "orig": stop(ao, a, b), "replay": stop(ar, c, d)})
    res["stops"] = stops
    # sticks the vehicle was given: mean and max PWM difference per role, on the stock-SITL scale
    so, sr = stick_series(fo, start, start + dur), stick_series(fr, replay_start, replay_start + dur + 1)
    to_, tr_ = [t - start for t, _ in so], [t - replay_start for t, _ in sr]
    diffs = [[], [], [], []]
    for t in ts:
        a = so[max(bisect.bisect_right(to_, t) - 1, 0)][1]
        b = sr[max(bisect.bisect_right(tr_, t) - 1, 0)][1]
        for i in range(4):
            diffs[i].append(abs(a[i] - b[i]))
    res["sticks_pwm"] = {r: {"mean": st.fmean(d), "max": max(d)} for r, d in zip(ROLES, diffs)} if so and sr else {}
    res["releases"] = {"orig": len(wo), "replay": len(wr)}
    return res


def report(r) -> str:
    h, v = r["horizontal_m"], r["vertical_m"]
    out = [f"compared {r['window_s']:.0f} s",
           f"horizontal error  mean {h['mean']:.2f} m  p95 {h['p95']:.2f}  max {h['max']:.2f}  final {h['final']:.2f}",
           f"vertical error    mean {v['mean']:.2f} m  p95 {v['p95']:.2f}  max {v['max']:.2f}  final {v['final']:.2f}",
           f"path length       original {r['path_m']['orig']:.1f} m, replay {r['path_m']['replay']:.1f} m",
           f"farthest from start  original {r['max_from_start_m']['orig']:.1f} m, replay {r['max_from_start_m']['replay']:.1f} m",
           f"mode sequence equal: {r['mode_sequence_equal']}"]
    if r.get("sticks_pwm"):
        out.append("stick difference (PWM, mean/max): " + "  ".join(
            f"{k} {v['mean']:.1f}/{v['max']:.0f}" for k, v in r["sticks_pwm"].items()))
    for m in r["modes"]:
        rs = m["replay_s"]
        out.append(f"  {m['mode']:9} original +{m['orig_s']:6.1f} s  replay "
                   + (f"+{rs:6.1f} s  ({rs - m['orig_s']:+.1f} s)" if rs is not None else "-"))
    out.append(f"releases (sticks centred >= 2 s): original {r['releases']['orig']}, replay {r['releases']['replay']}")
    for s in r["stops"]:
        o, p = s["orig"], s["replay"]
        out.append(f"  at +{s['at_s']:5.1f} s  stop dist {o['dist']:.2f} / {p['dist']:.2f} m   "
                   f"stop time {o['time']:.1f} / {p['time']:.1f} s   alt dev {o['alt_dev']:.2f} / {p['alt_dev']:.2f} m   "
                   f"(original / replay)")
    return "\n".join(out)
