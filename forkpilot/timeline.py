"""First-divergence analysis: where does the candidate's behaviour first leave the baseline's?

Deterministic. Its output is the compact evidence the AI explainer reads instead of raw
telemetry. Times are seconds since arming.
"""
from __future__ import annotations

import json
import math
import re
import statistics as st
from dataclasses import dataclass
from pathlib import Path

from pymavlink import mavutil

from . import i18n

MODE_NAMES = mavutil.mode_mapping_acm   # custom_mode number -> name (Copter; others by HEARTBEAT type)
GCS_TYPE = 6
TIME_TOL = 2.0        # s: minimum tolerance before a shifted event counts as early/late
SIGNAL_TOL = {"altitude": 0.5, "horizontal": 0.15}   # m: minimum tolerance per signal
TIME_SLACK = 1.0      # s: timing jitter tolerated when comparing trajectories
ANCHORS = ("armed", "takeoff_done", "hold_start", "goto", "arrived", "set_param", "mode")


@dataclass
class Ev:
    t: float
    key: str      # normalised identity used for matching
    text: str     # what a human reads


def _norm(text: str) -> str:
    return re.sub(r"-?\d+(\.\d+)?", "#", text)


def events(run: dict) -> list[Ev]:
    t0 = next((t for t, k, _ in run["events"] if k == "armed"), 0.0)
    out = []
    for t, kind, detail in run["events"]:
        if kind == "statustext":
            out.append(Ev(t - t0, "msg:" + _norm(detail), i18n.t("tl.msg", text=detail)))
        elif kind in ("set_param", "goto", "mode", "error", "disarmed"):
            out.append(Ev(t - t0, f"step:{kind}:{detail}", i18n.t("tl.step", kind=kind, detail=detail or "").strip()))
    last = None
    for s in run["samples"]:
        if s["mavpackettype"] != "HEARTBEAT" or s.get("type") == GCS_TYPE:
            continue
        if s.get("autopilot") == mavutil.mavlink.MAV_AUTOPILOT_PX4:
            from .px4_sitl import decode_mode
            mode = decode_mode(s["custom_mode"])
        else:
            names = mavutil.mode_mapping_bynumber(s.get("type")) or MODE_NAMES
            mode = names.get(s["custom_mode"], str(s["custom_mode"]))
        if mode != last:
            if last is not None:
                out.append(Ev(s["t"] - t0, f"mode:{mode}", i18n.t("tl.mode", mode=mode)))
            last = mode
    # pre-arm boot chatter differs run to run and says nothing about flight behaviour
    return sorted((e for e in out if e.t >= 0 or not e.key.startswith("msg:")), key=lambda e: e.t)


def _first(evs: list[Ev], key: str) -> float | None:
    return next((e.t for e in evs if e.key == key), None)


def compare_events(baseline: list[list[Ev]], cand: list[Ev]) -> list[dict]:
    """Classify each event key as same / new / missing / shifted."""
    keys_any = set().union(*({e.key for e in b} for b in baseline))
    keys_all = set.intersection(*({e.key for e in b} for b in baseline))
    cand_keys = {e.key for e in cand}
    rows = []
    for e in cand:
        if e.key not in keys_any:
            rows.append({"t": e.t, "status": "new", "text": e.text, "key": e.key})
            continue
        if e.key in keys_all and _first(cand, e.key) == e.t:
            times = [_first(b, e.key) for b in baseline]
            mean = st.fmean(times)
            tol = max(TIME_TOL, 3 * (st.pstdev(times) if len(times) > 1 else 0))
            if abs(e.t - mean) > tol:
                rows.append({"t": e.t, "status": "shifted", "text": e.text, "key": e.key,
                             "baseline_t": round(mean, 1)})
                continue
        rows.append({"t": e.t, "status": "same", "text": e.text, "key": e.key})
    for key in keys_all - cand_keys:
        ref = next(e for e in baseline[0] if e.key == key)
        rows.append({"t": ref.t, "status": "missing", "text": ref.text, "key": ref.key})
    return sorted(rows, key=lambda r: r["t"])


def _anchors(run: dict) -> list[float]:
    return [t for t, k, _ in run["events"] if k in ANCHORS]


def _track(run: dict, step=0.2):
    """(anchor index, seconds since that anchor, t since arm, n, e, up), sampled every `step` s."""
    anchors = _anchors(run)
    t_arm = anchors[0] if anchors else 0.0
    out, nxt = [], 0.0
    for p in run["samples"]:
        if p["mavpackettype"] != "LOCAL_POSITION_NED" or p["t"] < nxt:
            continue
        k = sum(1 for a in anchors if a <= p["t"]) - 1
        if k >= 0:
            out.append((k, p["t"] - anchors[k], p["t"] - t_arm, p["x"], p["y"], -p["z"]))
        nxt = p["t"] + step
    return out


def _envelope(track, k, dt, slack=TIME_SLACK):
    """Baseline samples in the same phase (anchor k) within ±slack s of the same time into it."""
    return [p for p in track if p[0] == k and abs(p[1] - dt) <= slack]


def _outside(v, vals):
    return max(0.0, min(vals) - v, v - max(vals))


def signal_divergence(baseline: list[dict], cand: dict) -> list[dict]:
    """First time the candidate's altitude / horizontal position leaves the baseline envelope.

    Samples are compared phase by phase (time since the latest scenario step), against the
    range every baseline run covered within ±TIME_SLACK — so a few hundred milliseconds of
    timing jitter during a fast climb is not mistaken for a behaviour change."""
    bt = [_track(b) for b in baseline]
    out = []
    for name in ("altitude", "horizontal"):
        for k, dt, t, n, e, up in _track(cand):
            env = [_envelope(b, k, dt) for b in bt]
            if any(not x for x in env):
                if name == "altitude" and all(b and b[-1][2] < t for b in bt):
                    # baseline already landed and disarmed while the candidate flies on
                    ref = st.fmean(b[-1][5] for b in bt)
                    if abs(up - ref) > SIGNAL_TOL[name]:
                        out.append({"signal": name, "t": round(t, 1), "baseline": round(ref, 1),
                                    "candidate": round(up, 1), "note": i18n.t("tl.baseline_done")})
                        break
                continue
            pts = [p for x in env for p in x]
            if name == "altitude":
                dev = _outside(up, [p[5] for p in pts])
                bv = f"{min(p[5] for p in pts):.2f}…{max(p[5] for p in pts):.2f}"
                cv = round(up, 2)
            else:
                dev = math.hypot(_outside(n, [p[3] for p in pts]), _outside(e, [p[4] for p in pts]))
                mn, me = st.fmean(p[3] for p in pts), st.fmean(p[4] for p in pts)
                bv, cv = i18n.t("tl.ne", n=f"{mn:.2f}", e=f"{me:.2f}"), i18n.t("tl.ne", n=f"{n:.2f}", e=f"{e:.2f}")
            if dev > SIGNAL_TOL[name]:
                out.append({"signal": name, "t": round(t, 1), "baseline": bv, "candidate": cv,
                            "outside_m": round(dev, 2)})
                break
    return out


def analyse(baseline_runs: list[dict], cand_run: dict) -> dict:
    rows = compare_events([events(b) for b in baseline_runs], events(cand_run))
    diverged = [r for r in rows if r["status"] != "same"]
    signals = signal_divergence(baseline_runs, cand_run)
    first = min([r["t"] for r in diverged] + [s["t"] for s in signals], default=None)
    return {"first_divergence_t": None if first is None else round(first, 1),
            "events": rows, "signals": signals,
            "candidate_error": cand_run.get("error")}


def render(scenario: str, a: dict, window: float = 15.0) -> str:
    mark = {"same": "  ", "new": "+ ", "missing": "- ", "shifted": "~ "}
    first = f"t={a['first_divergence_t']} s" if a["first_divergence_t"] is not None else i18n.t("tl.none")
    lines = [i18n.t("tl.head", scenario=scenario, first=first), "  " + i18n.t("tl.legend")]
    f = a["first_divergence_t"]
    for r in a["events"]:
        keep = r["status"] != "same" or r["key"].startswith(("step:", "mode:")) or (
            f is not None and abs(r["t"] - f) <= window)
        if keep:
            extra = "  " + i18n.t("tl.baseline_t", t=r["baseline_t"]) if "baseline_t" in r else ""
            lines.append(f"  {mark[r['status']]}{r['t']:7.1f}  {r['text']}{extra}")
    for s in a["signals"]:
        lines.append("  " + i18n.t("tl.signal", name=i18n.t("tl.sig." + s["signal"]), t=s["t"], base=s["baseline"],
                                cand=s["candidate"]) + (f" — {s['note']}" if s.get("note") else ""))
    if a.get("candidate_error"):
        lines.append("  " + i18n.t("tl.error", error=a["candidate_error"]))
    return "\n".join(lines)


def analyse_dirs(scenario: str, base_dir: Path, cand_dir: Path) -> tuple[dict, str]:
    from .battery import read_run, run_paths
    base = [read_run(p) for p in run_paths(base_dir, scenario)]
    a = analyse(base, read_run(run_paths(cand_dir, scenario)[0]))
    return a, render(scenario, a)


if __name__ == "__main__":
    import sys
    print(analyse_dirs(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))[1])
