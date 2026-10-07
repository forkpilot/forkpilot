"""Inline SVG plots for the investigation report, and the telemetry behind them.

No plotting library: the report is opened on machines with no network and nothing installed
beyond ForkPilot's own environment. Colours are CSS classes, so the page's light/dark theme
reaches the plots; axes carry units; every plot is drawn to scale.
"""
from __future__ import annotations

import gzip
import json
import math
import re
import statistics as st
from bisect import bisect_right
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Callable

from .i18n import t

MAX_RUNS = 5          # lines drawn per side
MAX_POINTS = 400      # per line
STEP_KINDS = ("armed", "takeoff_done", "mode", "sticks", "release", "release_end", "goto",
              "arrived", "set_param", "velocity", "velocity_end", "disarmed")


# ---------------------------------------------------------------- SVG helpers

def fmt(v: float) -> str:
    return "nan" if v != v else "inf" if v == math.inf else "-inf" if v == -math.inf else f"{v:.4g}"


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    span = (hi - lo) or 1.0
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step - 1e-9)
    return [round(i * step, 10) for i in range(first, math.floor(hi / step + 1e-9) + 1)]


def pad_range(lo: float, hi: float, zero: bool = True) -> tuple[float, float]:
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    pad = 0.06 * (hi - lo)
    lo, hi = lo - pad, hi + pad
    if zero and lo > 0 and lo < 0.4 * hi:
        lo = 0.0
    return lo, hi


def svg_open(w: float, h: float, label: str) -> str:
    return (f'<svg viewBox="0 0 {w:g} {h:g}" role="img" aria-label="{escape(label, True)}" '
            f'preserveAspectRatio="xMidYMid meet">')


def _txt(x, y, s, cls="tick", anchor="middle", extra=""):
    return f'<text class="{cls}" x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}"{extra}>{escape(str(s))}</text>'


# ------------------------------------------------------------------ line chart

@dataclass
class Panel:
    title: str
    unit: str
    good: list[list[float | None]] = field(default_factory=list)   # per run, on the shared x grid
    bad: list[list[float | None]] = field(default_factory=list)
    hline: tuple[float, str] | None = None


def _envelope(good: list[list], i: int):
    vals = [g[i] for g in good if g[i] is not None]
    return (min(vals), max(vals), st.median(vals)) if vals else None


def line_chart(xs: list[float], panels: list[Panel], xlabel: str, markers=(), shade=None, ticks=None,
               label: str = "", width: float = 760, panel_h: float = 150) -> str:
    """Stacked panels on a shared x axis. Good runs: min-max envelope and median. Bad runs: one
    line each. markers: [(x, text)] drawn as dashed verticals; shade: (x0, x1, text) band."""
    ticks = ticks or {}
    ml, mr, mt, mb, gap = 56, 14, 42, 38, 26
    h = mt + len(panels) * panel_h + (len(panels) - 1) * gap + mb
    x0, x1 = xs[0], xs[-1] if xs[-1] > xs[0] else xs[0] + 1
    X = lambda v: ml + (v - x0) / (x1 - x0) * (width - ml - mr)
    out = [svg_open(width, h, label)]
    if shade:
        out.append(f'<rect class="shade" x="{X(shade[0]):.1f}" y="{mt}" width="{X(shade[1]) - X(shade[0]):.1f}" '
                   f'height="{h - mt - mb}"/>')
    for pi, p in enumerate(panels):
        top = mt + pi * (panel_h + gap)
        vals = [v for g in p.good + p.bad for v in g if v is not None]
        if p.hline:
            vals.append(p.hline[0])
        lo, hi = pad_range(min(vals), max(vals)) if vals else (0.0, 1.0)
        Y = lambda v, top=top, lo=lo, hi=hi: top + panel_h - (v - lo) / (hi - lo) * panel_h
        out.append(f'<rect class="frame" x="{ml}" y="{top}" width="{width - ml - mr}" height="{panel_h}"/>')
        for tv in nice_ticks(lo, hi, 4):
            y = Y(tv)
            out.append(f'<line class="grid" x1="{ml}" x2="{width - mr}" y1="{y:.1f}" y2="{y:.1f}"/>')
            out.append(_txt(ml - 6, y + 4, fmt(tv), anchor="end"))
        out.append(_txt(0, 0, f"{p.title} ({p.unit})", "ylab", "middle",
                        f' transform="translate(13 {top + panel_h / 2:.1f}) rotate(-90)"'))
        if p.hline:
            y = Y(p.hline[0])
            out.append(f'<line class="ref" x1="{ml}" x2="{width - mr}" y1="{y:.1f}" y2="{y:.1f}"/>')
            out.append(_txt(width - mr - 4, y - 4, p.hline[1], "tick", "end"))
        if len(p.good) > 1:
            segs, cur = [], []
            for i in range(len(xs)):
                e = _envelope(p.good, i)
                if e:
                    cur.append((xs[i], e))
                elif cur:
                    segs.append(cur)
                    cur = []
            segs.append(cur)
            for seg in segs:
                if len(seg) < 2:
                    continue
                pts = [f"{X(x):.1f},{Y(e[1]):.1f}" for x, e in seg] + \
                      [f"{X(x):.1f},{Y(e[0]):.1f}" for x, e in reversed(seg)]
                out.append(f'<polygon class="band-good" points="{" ".join(pts)}"/>')
        for g in p.good:
            out += _polylines(xs, g, X, Y, "good")
        for g in p.bad:
            out += _polylines(xs, g, X, Y, "bad")
    row_end = [-1e9] * 3          # labels go to the first of three rows where they do not collide
    for mx, text in markers:
        if not x0 <= mx <= x1:
            continue
        out.append(f'<line class="mark" x1="{X(mx):.1f}" x2="{X(mx):.1f}" y1="{mt - 4}" y2="{h - mb}"/>')
        right = X(mx) > width - 90
        w = 5.6 * len(text) + 6
        a, b = (X(mx) - w, X(mx)) if right else (X(mx), X(mx) + w)
        row = next((r for r in range(3) if a > row_end[r]), None)
        if row is not None:
            row_end[row] = b
            out.append(_txt(X(mx) + (-2 if right else 2), mt - 8 - row * 11, text, "mark-t",
                            "end" if right else "start"))
    axis_y = h - mb
    for side, xs_ in ticks.items():       # arm / mode / disarm moments of one good and one bad run
        for tx in xs_:
            if x0 <= tx <= x1:
                out.append(f'<path class="dot-{side}" d="M{X(tx) - 4:.1f} {axis_y + 1} h8 l-4 -7 z"/>')
    for tv in nice_ticks(x0, x1, 8):
        out.append(f'<line class="axis" x1="{X(tv):.1f}" x2="{X(tv):.1f}" y1="{axis_y}" y2="{axis_y + 4}"/>')
        out.append(_txt(X(tv), axis_y + 16, fmt(tv)))
    out.append(_txt(ml + (width - ml - mr) / 2, h - 5, xlabel, "ylab"))
    out.append("</svg>")
    return "".join(out)


def _polylines(xs, ys, X, Y, cls) -> list[str]:
    out, cur = [], []
    for x, y in zip(xs, ys):
        if y is None:
            if len(cur) > 1:
                out.append(cur)
            cur = []
        else:
            cur.append(f"{X(x):.1f},{Y(y):.1f}")
    if len(cur) > 1:
        out.append(cur)
    return [f'<polyline class="line-{cls}" points="{" ".join(c)}"/>' for c in out]


def legend(good_n: int, bad_n: int, band: bool = True) -> str:
    good = (f'<span class="key key-good"></span>{escape(t("plot.legend.good" if band else "plot.legend.good.lines", n=good_n))} ' if good_n
            else f'{escape(t("plot.legend.nogood"))} ')
    return f'<p class="legend">{good}<span class="key key-bad"></span>{escape(t("plot.legend.bad", n=bad_n))}</p>'


# ------------------------------------------------------------------ band chart

@dataclass
class BandRow:
    metric: str
    unit: str
    base: list[float]
    bad: list[float]
    lo: float | None = None       # drift: baseline band
    hi: float | None = None
    rule: tuple[str, float] | None = None    # absolute rule the metric must satisfy
    bad_mean: float | None = None
    scenario: str = ""


RULE_SIGNS = {"lt": "<", "le": "≤", "gt": ">", "ge": "≥", "eq": "="}


def band_chart(rows: list[BandRow], label: str = "", width: float = 760) -> str:
    """One strip per metric, to its own scale: the allowed band (or rule bound), the baseline
    runs and the bad runs. Each strip is drawn from its own data range, so strips cannot be
    compared with each other, only each with its own band."""
    lw, rw, rh, top = 215, 150, 44, 6
    px0, px1 = lw + 8, width - rw
    h = top + rh * len(rows) + 30
    out = [svg_open(width, h, label)]
    for i, r in enumerate(rows):
        y0 = top + i * rh
        cy = y0 + rh / 2
        pts = r.base + r.bad + ([r.bad_mean] if r.bad_mean is not None else [])
        bounds = [b for b in (r.lo, r.hi, r.rule[1] if r.rule else None) if b is not None and math.isfinite(b)]
        vals = [v for v in pts + bounds if v is not None and math.isfinite(v)]
        lo, hi = pad_range(min(vals), max(vals), zero=False) if vals else (0.0, 1.0)
        X = lambda v, lo=lo, hi=hi: px0 + (v - lo) / (hi - lo) * (px1 - px0)
        out.append(f'<line class="grid" x1="{lw}" x2="{px1}" y1="{y0 + rh - 1}" y2="{y0 + rh - 1}"/>')
        out.append(_txt(lw, y0 + 17, r.metric, "mlab", "end"))
        out.append(_txt(lw, y0 + 32, ", ".join(x for x in (r.scenario[:30], r.unit) if x), "tick", "end"))
        if r.lo is not None and r.hi is not None:
            a, b = X(max(r.lo, lo)), X(min(r.hi, hi))
            out.append(f'<rect class="band" x="{a:.1f}" y="{cy - 12}" width="{max(b - a, 1):.1f}" height="24"/>')
        elif r.rule:
            op, bound = r.rule
            bx = X(bound)
            ok_left = op in ("lt", "le")
            a, b = (px0, bx) if ok_left else (bx, px1)
            out.append(f'<rect class="band" x="{a:.1f}" y="{cy - 12}" width="{max(b - a, 1):.1f}" height="24"/>')
            out.append(f'<line class="ref" x1="{bx:.1f}" x2="{bx:.1f}" y1="{cy - 14}" y2="{cy + 14}"/>')
        for j, v in enumerate(r.base):
            if math.isfinite(v):
                out.append(f'<circle class="dot-good" cx="{X(v):.1f}" cy="{cy - 4 + (j % 3) * 4:.1f}" r="3"/>')
        for j, v in enumerate(r.bad):
            if math.isfinite(v):
                x, y = X(v), cy - 4 + (j % 3) * 4
                out.append(f'<path class="dot-bad" d="M{x:.1f} {y - 4:.1f} l4 4 l-4 4 l-4 -4 z"/>')
        if r.bad_mean is not None and math.isfinite(r.bad_mean) and not r.bad:
            x = X(r.bad_mean)
            out.append(f'<path class="dot-bad" d="M{x:.1f} {cy - 5:.1f} l5 5 l-5 5 l-5 -5 z"/>')
        for tv in (min(vals), max(vals)) if vals else ():
            out.append(_txt(X(tv), y0 + rh - 3, fmt(round(tv, 9)), "tick"))
        if r.lo is not None and r.hi is not None:
            note = f"[{fmt(r.lo)}, {fmt(r.hi)}]"
        elif r.rule:
            note = f"{RULE_SIGNS.get(r.rule[0], r.rule[0])} {fmt(r.rule[1])}"
        else:
            note = ""
        out.append(_txt(px1 + 10, cy + 4, note, "tick", "start"))
    ay = top + rh * len(rows) + 8
    out.append(f'<rect class="band" x="{px0}" y="{ay}" width="16" height="10"/>')
    out.append(_txt(px0 + 22, ay + 9, t("plot.band.allowed"), "tick", "start"))
    out.append(f'<circle class="dot-good" cx="{px0 + 150}" cy="{ay + 5}" r="3"/>')
    out.append(_txt(px0 + 158, ay + 9, t("plot.band.baseline"), "tick", "start"))
    out.append(f'<path class="dot-bad" d="M{px0 + 270} {ay + 1} l4 4 l-4 4 l-4 -4 z"/>')
    out.append(_txt(px0 + 280, ay + 9, t("plot.band.bad"), "tick", "start"))
    out.append("</svg>")
    return "".join(out)


UNIT_TOKENS = {"m": "m", "s": "s", "mps": "m/s", "dps": "°/s", "deg": "°", "cm": "cm"}


def unit_of(metric: str) -> str:
    return next((UNIT_TOKENS[tok] for tok in metric.split("_")[1:] if tok in UNIT_TOKENS), "")


# ------------------------------------------------------------------- telemetry

def load_run(path: Path) -> dict | None:
    try:
        raw = Path(path).read_bytes()
        return json.loads(gzip.decompress(raw) if str(path).endswith(".gz") else raw)
    except (OSError, ValueError, EOFError):
        return None


def run_files(d: Path | None, name: str, limit: int = MAX_RUNS) -> list[Path]:
    if d is None or not Path(d).is_dir():
        return []
    found: dict[int, Path] = {}
    for p in sorted(Path(d).glob(f"{_glob_escape(name)}.*.run.json*")):
        m = re.fullmatch(re.escape(name) + r"\.(\d+)\.run\.json(\.gz)?", p.name)
        if m and (int(m[1]) not in found or not m[2]):
            found[int(m[1])] = p
    return [found[k] for k in sorted(found)][:limit]


def _glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)


def metric_files(d: Path | None, name: str) -> list[dict]:
    out = []
    if d is not None and Path(d).is_dir():
        for p in sorted(Path(d).glob(f"{_glob_escape(name)}.*.metrics.json")):
            if re.fullmatch(re.escape(name) + r"\.\d+\.metrics\.json", p.name):
                try:
                    out.append(json.loads(p.read_text()))
                except (OSError, ValueError):
                    pass
    return out


def _pos(run):
    return [s for s in run["samples"] if s["mavpackettype"] == "LOCAL_POSITION_NED"]


def _att(run):
    return [s for s in run["samples"] if s["mavpackettype"] == "ATTITUDE"]


def sig_speed(run, t0):
    p = _pos(run)
    return [s["t"] for s in p], [math.hypot(s["vx"], s["vy"]) for s in p]


def sig_alt(run, t0):
    p = _pos(run)
    if p:
        return [s["t"] for s in p], [-s["z"] for s in p]
    g = [s for s in run["samples"] if s["mavpackettype"] == "GLOBAL_POSITION_INT"]
    return [s["t"] for s in g], [s["relative_alt"] / 1000 for s in g]


def sig_dist(run, t0):
    p = _pos(run)
    ref = next((s for s in p if s["t"] >= t0), None)
    if ref is None:
        return [], []
    return [s["t"] for s in p], [math.hypot(s["x"] - ref["x"], s["y"] - ref["y"]) for s in p]


def sig_range(run, t0):
    p = _pos(run)
    return [s["t"] for s in p], [math.hypot(s["x"], s["y"]) for s in p]


def sig_yawrate(run, t0):
    a = _att(run)
    return [s["t"] for s in a], [math.degrees(abs(s["yawspeed"])) for s in a]


def sig_tilt(run, t0):
    a = _att(run)
    return [s["t"] for s in a], [math.degrees(math.hypot(s["roll"], s["pitch"])) for s in a]


def sig_sink(run, t0):
    p = _pos(run)
    return [s["t"] for s in p], [s["vz"] for s in p]


def sig_goto_dist(run, t0):
    tgt = next((d for t_, k, d in run["events"] if k == "goto" and abs(t_ - t0) < 1e-6), None)
    p = _pos(run)
    if not tgt:
        return [], []
    return [s["t"] for s in p], [math.hypot(s["x"] - tgt[0], s["y"] - tgt[1]) for s in p]


@dataclass
class PanelSpec:
    key: str                # catalogue key of the signal's name
    unit: str
    fn: Callable
    hline: tuple[float, str] | None = None


SPEED = PanelSpec("plot.sig.speed", "m/s", sig_speed)
ALT = PanelSpec("plot.sig.alt", "m", sig_alt)
DIST = PanelSpec("plot.sig.dist_release", "m", sig_dist)
YAW = PanelSpec("plot.sig.yawrate", "°/s", sig_yawrate)
TILT = PanelSpec("plot.sig.tilt", "°", sig_tilt)
SINK = PanelSpec("plot.sig.sink", "m/s", sig_sink)
RANGE = PanelSpec("plot.sig.range", "m", sig_range)


def _arm_time(run) -> float:
    ev = run["events"]
    return next((t_ for t_, k, _ in ev if k == "armed"), run["samples"][0]["t"] if run["samples"] else 0.0)


def _end_time(run) -> float:
    return run["samples"][-1]["t"] if run["samples"] else 0.0


def _split_name(rest: str) -> tuple[str, int]:
    m = re.fullmatch(r"(.*?)(\d+)?", rest)
    return m[1], int(m[2] or 1)


def w_release(rest):
    mode, n = _split_name(rest)

    def win(run):
        seen = 0
        ev = run["events"]
        for i, (t0, k, d) in enumerate(ev):
            if k == "release" and str(d).lower() == mode:
                seen += 1
                if seen == n:
                    t1 = next((t_ for t_, k2, _ in ev[i + 1:] if k2 == "release_end"), None)
                    return (t0, t1) if t1 is not None else None
        return None
    return win


def w_segment(rest):
    want, n = _split_name(rest)
    cuts = {"mode", "sticks", "release", "release_end", "goto", "send_goto", "velocity", "yaw",
            "set_param", "error", "disarmed"}

    def win(run):
        seen, mode = 0, None
        ev = run["events"]
        for i, (t0, k, d) in enumerate(ev):
            if k == "mode":
                mode = d
            if mode is None or k not in ("mode", "sticks", "release"):
                continue
            t1 = next((t_ for t_, k2, _ in ev[i + 1:] if k2 in cuts), None)
            if t1 is None or t1 - t0 < 5:
                continue
            if str(mode).lower() == want:
                seen += 1
                if seen == n:
                    return t0, t1
        return None
    return win


def w_velocity(rest):
    n = int(rest)

    def win(run):
        seen = 0
        ev = run["events"]
        for i, (t0, k, d) in enumerate(ev):
            if k == "velocity":
                t1 = next((t_ for t_, k2, _ in ev[i + 1:] if k2 == "velocity_end"), None)
                if t1 is None:
                    continue
                seen += 1
                if seen == n:
                    return t0, t1
        return None
    return win


def w_first(kind, end_kinds, pre=0.0, post=0.0, stop_at_end=True):
    def win(run):
        ev = run["events"]
        for i, (t0, k, _) in enumerate(ev):
            if k == kind:
                t1 = next((t_ for t_, k2, _ in ev[i + 1:] if k2 in end_kinds), None)
                if t1 is None and stop_at_end:
                    t1 = _end_time(run)
                return t0 - pre, (t1 if t1 is not None else t0) + post
        return None
    return win


def w_takeoff(run):
    a = _arm_time(run)
    up = next((t_ for t_, k, _ in run["events"] if k == "takeoff_done"), None)
    return (a, up + 3) if up is not None else None


def w_landing(run):
    d = next((t_ for t_, k, _ in run["events"] if k == "disarmed"), _end_time(run))
    return d - 40, d + 2


def w_whole(run):
    """Arming to the end of the position telemetry, or shortly after disarming if that is later."""
    pos = _pos(run)
    off = next((t_ for t_, k, _ in run["events"] if k == "disarmed"), None)
    ends = ([pos[-1]["t"]] if pos else []) + ([off + 3] if off is not None else [])
    return _arm_time(run), min(max(ends, default=_end_time(run)), _end_time(run))


@dataclass
class Spec:
    window: Callable
    panels: list[PanelSpec]
    zero_key: str           # catalogue key: what x = 0 means
    whole: bool = False


def spec_for(metric: str) -> Spec:
    m = re.fullmatch(r"(stop_dist_m|stop_backtrack_m|stop_time_s|stop_alt_dev_m|stick_speed_mps)_(.+)", metric)
    if m:
        return Spec(w_release(m[2]), [DIST, PanelSpec("plot.sig.speed", "m/s", sig_speed, (0.3, "0.3 m/s"))],
                    "plot.zero.release")
    m = re.fullmatch(r"mode_speed_mps_(.+)", metric)
    if m:
        return Spec(w_segment(m[1]), [SPEED, ALT], "plot.zero.segment")
    m = re.fullmatch(r"mode_yaw_rate_dps_(.+)", metric)
    if m:
        return Spec(w_segment(m[1]), [YAW, SPEED], "plot.zero.segment")
    m = re.fullmatch(r"vel_err_mps_(\d+)", metric)
    if m:
        return Spec(w_velocity(m[1]), [SPEED, ALT], "plot.zero.velocity")
    if metric.startswith("hold_"):
        return Spec(w_first("hold_start", {"goto", "mode", "set_param", "error"}), [DIST, ALT], "plot.zero.hold")
    if metric == "takeoff_time_s":
        return Spec(w_takeoff, [ALT], "plot.zero.arm")
    if metric.startswith("goto_"):
        return Spec(w_first("goto", {"mode", "set_param", "error"}),
                    [PanelSpec("plot.sig.goto_dist", "m", sig_goto_dist), SPEED], "plot.zero.goto")
    if metric == "failsafe_reaction_s":
        return Spec(w_first("set_param", set(), pre=5, post=25, stop_at_end=False), [ALT, SPEED], "plot.zero.fault")
    if metric.startswith("mission_"):
        return Spec(w_first("mode", {"disarmed"}), [SPEED, ALT], "plot.zero.mode")
    if metric in ("touchdown_speed_mps", "disarm_delay_s"):
        return Spec(w_landing, [ALT, SINK], "plot.zero.landing")
    if metric == "tilt_max_deg":
        return Spec(w_whole, [TILT, SPEED], "plot.zero.arm", whole=True)
    if metric == "range_max_m":
        return Spec(w_whole, [RANGE, ALT], "plot.zero.arm", whole=True)
    return Spec(w_whole, [ALT, SPEED], "plot.zero.arm", whole=True)


def resample(ts: list[float], vs: list[float], grid: list[float]) -> list[float | None]:
    out: list[float | None] = []
    if not ts:
        return [None] * len(grid)
    for g in grid:
        if g < ts[0] - 1e-9 or g > ts[-1] + 1e-9:
            out.append(None)
            continue
        j = bisect_right(ts, g) - 1
        if j >= len(ts) - 1:
            out.append(vs[-1])
        else:
            f = (g - ts[j]) / ((ts[j + 1] - ts[j]) or 1.0)
            out.append(round(vs[j] + f * (vs[j + 1] - vs[j]), 3))
    return out


def moments(run: dict, t0: float) -> list[tuple[float, str]]:
    """Arm, mode change and disarm moments of a run, seconds from t0."""
    return [(round(t_ - t0, 2), k) for t_, k, _ in run["events"] if k in ("armed", "mode", "disarmed")]


def step_markers(run: dict, t0: float, lo: float, hi: float) -> list[tuple[float, str]]:
    """Scenario steps and mode changes of one run, as (seconds from t0, label)."""
    out, last = [], None
    for t_, k, d in run["events"]:
        if k not in STEP_KINDS or not lo <= t_ - t0 <= hi:
            continue
        label = str(d) if k == "mode" else "param" if k == "set_param" else k
        if k in ("sticks", "release_end", "velocity_end", "armed"):
            continue
        if last is not None and t_ - t0 - last < 0.01 * (hi - lo):
            continue
        out.append((round(t_ - t0, 2), label))
        last = t_ - t0
    return out


def step_signature(run: dict) -> list:
    """The scenario steps a run flew, without times: equal for two flights of one scenario."""
    return [(k, json.dumps(d, sort_keys=True)) for _, k, d in run["events"] if k in STEP_KINDS]


def telemetry_figure(metric: str, good_paths: list[Path], bad_paths: list[Path],
                     divergence_t: float | None = None) -> dict | None:
    """Good vs bad traces around the metric's event, plus the whole flight with step markers.
    Returns {"zoom": svg|None, "flight": svg, "good": n, "bad": n, "zero": text} or None."""
    good = [r for r in map(load_run, good_paths) if r and r.get("samples")]
    bad = [r for r in map(load_run, bad_paths) if r and r.get("samples")]
    if not bad:
        return None
    spec = spec_for(metric)

    def build(spec: Spec, window_of):
        wins = {}
        for side, runs in (("good", good), ("bad", bad)):
            for i, r in enumerate(runs):
                w = window_of(r)
                if w and w[1] - w[0] > 0.5:
                    wins[side, i] = w
        if not any(k[0] == "bad" for k in wins):
            return None
        dur = min(max(w[1] - w[0] for w in wins.values()), 400.0)
        n = max(2, min(MAX_POINTS, int(dur / 0.1)))
        grid = [round(i * dur / n, 3) for i in range(n + 1)]
        panels = []
        for ps in spec.panels:
            p = Panel(t(ps.key), ps.unit, hline=ps.hline)
            for side, runs in (("good", good), ("bad", bad)):
                for i, r in enumerate(runs):
                    if (side, i) in wins:
                        t0 = wins[side, i][0]
                        ts, vs = ps.fn(r, t0)
                        getattr(p, side).append(resample([x - t0 for x in ts], vs, grid))
            panels.append(p)
        ref = min(i for side, i in wins if side == "bad")      # markers come from this run
        gref = min((i for side, i in wins if side == "good"), default=None)
        return grid, panels, wins, bad[ref], wins["bad", ref][0], (good[gref], wins["good", gref][0]) if gref is not None else None

    zoom = None
    if not spec.whole:
        b = build(spec, spec.window)
        if b:
            grid, panels, wins, ref, t0, _ = b
            zoom = line_chart(grid, panels, t(spec.zero_key), step_markers(ref, t0, 0, grid[-1]),
                              label=t("plot.zoom.label", metric=metric))
    b = build(Spec(w_whole, spec.panels if spec.whole else [ALT, SPEED], "plot.zero.arm"), w_whole)
    if not b:
        return None
    grid, panels, wins, ref, a0, gref = b
    ticks = {"bad": [x for x, _ in moments(ref, a0)]}
    if gref:
        ticks["good"] = [x for x, _ in moments(*gref)]
    marks = step_markers(ref, a0, 0, grid[-1])
    if divergence_t is not None and 0 <= divergence_t <= grid[-1]:
        marks.append((divergence_t, t("plot.divergence")))
    w = None if spec.whole else spec.window(ref)
    shade = (w[0] - a0, w[1] - a0, "") if zoom and w else None
    flight = line_chart(grid, panels, t("plot.zero.arm"), sorted(marks), shade=shade, ticks=ticks,
                        label=t("plot.flight.label", metric=metric), panel_h=130)
    return {"zoom": zoom, "flight": flight, "good": sum(k[0] == "good" for k in wins),
            "bad": sum(k[0] == "bad" for k in wins)}


# ---------------------------------------------------------------- ground track

def _thin(pts: list, n: int = 600) -> list:
    step = max(1, len(pts) // n)
    return pts[::step] + ([pts[-1]] if pts and (len(pts) - 1) % step else [])


def _pos_at(run: dict, t_abs: float):
    p = _pos(run)
    return min(p, key=lambda s: abs(s["t"] - t_abs)) if p else None


def ground_track(metric: str, good_paths: list[Path], bad_paths: list[Path],
                 divergence_t: float | None = None, width: float = 760) -> dict | None:
    """The flights seen from above (north up, to scale): good runs, bad runs and where they first
    part. When the metric measures a short part of the flight (a stop, a hold), the map shows
    that part of every run, which is where the difference is. {"svg", "good", "bad", "zoom"} or None."""
    good = [r for r in map(load_run, good_paths) if r and _pos(r)]
    bad = [r for r in map(load_run, bad_paths) if r and _pos(r)]
    if not bad:
        return None
    spec = spec_for(metric)

    def track(run, win):
        return [(s["y"], s["x"]) for s in _pos(run) if win[0] <= s["t"] <= win[1]]     # (east, north)

    def part(run):
        w = None if spec.whole else spec.window(run)
        whole = track(run, w_whole(run))
        sub = track(run, w) if w else []
        return whole, sub
    runs = [("good", r, *part(r)) for r in good] + [("bad", r, *part(r)) for r in bad]
    runs = [x for x in runs if len(x[2]) > 1]
    if not any(x[0] == "bad" for x in runs):
        return None
    # a window that is most of the flight marks nothing: show the whole flight
    zoom = all(len(sub) > 1 and len(sub) < 0.8 * len(whole) for side, _, whole, sub in runs if side == "bad")
    tracks = [(side, sub if zoom and len(sub) > 1 else whole) for side, _, whole, sub in runs]
    if zoom:                           # each part from its own start: the runs overlay and compare
        tracks = [(side, [(e - tr[0][0], n - tr[0][1]) for e, n in tr]) for side, tr in tracks if len(tr) > 1]
    marks = []                         # (east, north, side) where good and bad first part
    if divergence_t is not None and not zoom:
        for side, runs_ in (("good", good), ("bad", bad)):
            if runs_:
                q = _pos_at(runs_[0], _arm_time(runs_[0]) + divergence_t)
                if q:
                    marks.append((q["y"], q["x"], side))
    es = [e for _, tr in tracks for e, _ in tr] + ([] if zoom else [0.0])
    ns = [n for _, tr in tracks for _, n in tr] + ([] if zoom else [0.0])
    e0, e1 = pad_range(min(es), max(es), zero=False)
    n0, n1 = pad_range(min(ns), max(ns), zero=False)
    ml, mr, mt, mb, hmax, hmin = 56, 14, 14, 38, 400.0, 220.0
    pw = width - ml - mr
    if (n1 - n0) / (e1 - e0) * pw > hmax:      # tall: fit the height, keep the scale equal on both axes
        scale = hmax / (n1 - n0)
        mid, half = (e0 + e1) / 2, pw / scale / 2
        e0, e1 = mid - half, mid + half
    else:
        scale = pw / (e1 - e0)
    ph = (n1 - n0) * scale
    if ph < hmin:                              # flat: more north range around the same centre
        mid, half = (n0 + n1) / 2, hmin / scale / 2
        n0, n1, ph = mid - half, mid + half, hmin
    X = lambda e: ml + (e - e0) * scale
    Y = lambda n: mt + ph - (n - n0) * scale
    h = mt + ph + mb
    out = [svg_open(width, h, t("plot.map.label", metric=metric))]
    out.append(f'<rect class="frame" x="{ml}" y="{mt}" width="{pw:.1f}" height="{ph:.1f}"/>')
    for tv in nice_ticks(e0, e1, 10):
        out.append(f'<line class="grid" x1="{X(tv):.1f}" x2="{X(tv):.1f}" y1="{mt}" y2="{mt + ph:.1f}"/>')
        out.append(_txt(X(tv), mt + ph + 15, fmt(tv)))
    for tv in nice_ticks(n0, n1, 6):
        out.append(f'<line class="grid" x1="{ml}" x2="{ml + pw:.1f}" y1="{Y(tv):.1f}" y2="{Y(tv):.1f}"/>')
        out.append(_txt(ml - 6, Y(tv) + 4, fmt(tv), anchor="end"))
    out.append(_txt(ml + pw / 2, h - 5, t("plot.map.east.rel" if zoom else "plot.map.east"), "ylab"))
    out.append(_txt(0, 0, t("plot.map.north.rel" if zoom else "plot.map.north"), "ylab", "middle",
                    f' transform="translate(13 {mt + ph / 2:.1f}) rotate(-90)"'))
    pl = lambda tr, cls: ('<polyline class="' + cls + '" points="'
                          + " ".join(f"{X(e):.1f},{Y(n):.1f}" for e, n in _thin(tr)) + '"/>')
    for side in ("good", "bad"):
        out += [pl(tr, "track-" + side) for sd, tr in tracks if sd == side]
    if zoom:                            # where each run's part ends, and the common start
        for side, tr in tracks:
            out.append(f'<circle class="dot-{side}" cx="{X(tr[-1][0]):.1f}" cy="{Y(tr[-1][1]):.1f}" r="2.5"/>')
        out.append(f'<circle class="home" cx="{X(0):.1f}" cy="{Y(0):.1f}" r="3.5"/>')
        out.append(_txt(X(0) + 7, Y(0) - 6, t("plot.map.start"), "mark-t", "start"))
    else:
        out.append(f'<circle class="home" cx="{X(0):.1f}" cy="{Y(0):.1f}" r="3.5"/>')
        out.append(_txt(X(0) + 7, Y(0) - 6, t("plot.map.home"), "mark-t", "start"))
    pts = {side: (X(e), Y(n)) for e, n, side in marks}
    if "bad" in pts:
        bx, by = pts["bad"]
        if "good" in pts:
            gx, gy = pts["good"]
            out.append(f'<line class="mark" x1="{gx:.1f}" y1="{gy:.1f}" x2="{bx:.1f}" y2="{by:.1f}"/>')
            out.append(f'<circle class="dot-good" cx="{gx:.1f}" cy="{gy:.1f}" r="3.5"/>')
        out.append(f'<circle class="dot-bad" cx="{bx:.1f}" cy="{by:.1f}" r="3.5"/>')
        out.append(f'<circle class="div-ring" cx="{bx:.1f}" cy="{by:.1f}" r="9"/>')
        # the label goes where it crosses the fewest track points, inside the frame
        label, lw = t("plot.divergence"), 6.5 * len(t("plot.divergence"))
        xy = [(X(e), Y(n)) for _, tr in tracks for e, n in _thin(tr)]
        boxes = []                     # (text x, text y, anchor, box x0, y0, x1, y1)
        for dx, dy in ((13, 4), (13, 22), (13, -12), (-13, 4), (-13, 22), (-13, -12)):
            x0 = bx + dx if dx > 0 else bx + dx - lw
            y1 = by + dy + 3
            if x0 >= ml and x0 + lw <= ml + pw and y1 - 14 >= mt and y1 <= mt + ph:
                boxes.append((bx + dx, by + dy, "start" if dx > 0 else "end", x0, y1 - 14, x0 + lw, y1))
        if boxes:
            tx, ty, anchor = min(boxes, key=lambda b: sum(b[3] <= x <= b[5] and b[4] <= y <= b[6]
                                                          for x, y in xy))[:3]
        else:
            tx, ty, anchor = bx + 13, by + 4, "start"
        out.append(_txt(tx, ty, label, "mark-t", anchor))
    out.append("</svg>")
    return {"svg": "".join(out), "good": len(good), "bad": len(bad), "zoom": zoom}


# ---------------------------------------------------------------- bisect strip

def bisect_strip(n: int, tests: list[tuple[int, str, str]], culprit: int | None, width: float = 760) -> str:
    """How bisect narrowed the range: one row per test, oldest commit left. The bar is what was
    still suspect before the test, the mark the commit it flew. tests: (index, result, label)."""
    ml, mr, mt, rh = 14, 190, 30, 24
    rows = len(tests) + (1 if culprit is not None else 0)
    h = mt + rows * rh + 8
    pw = width - ml - mr
    X = lambda i: ml + (i + 0.5) / n * pw
    out = [svg_open(width, h, t("plot.bisect.label", n=n))]
    out.append(_txt(ml, 14, t("plot.bisect.old"), "tick", "start"))
    out.append(_txt(ml + pw, 14, t("plot.bisect.new", n=n), "tick", "end"))
    lo, hi = 0, n - 1
    bar = lambda a, b, y, cls: (f'<rect class="{cls}" x="{ml + a / n * pw:.1f}" y="{y - 5:.1f}" '
                                f'width="{max((b - a + 1) / n * pw, 2):.1f}" height="10" rx="2"/>')
    for row, (i, res, label) in enumerate(tests):
        y = mt + row * rh + rh / 2
        out.append(f'<line class="grid" x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(bar(lo, hi, y, "susp"))
        x = X(i)
        if res == "bad":
            out.append(f'<path class="dot-bad" d="M{x:.1f} {y - 6:.1f} l6 6 l-6 6 l-6 -6 z"/>')
            hi = min(hi, i)
        elif res == "good":
            out.append(f'<circle class="dot-good" cx="{x:.1f}" cy="{y:.1f}" r="5"/>')
            lo = max(lo, i + 1)
        else:
            out.append(f'<path class="mark" d="M{x - 4:.1f} {y - 4:.1f} l8 8 m0 -8 l-8 8"/>')
        out.append(_txt(ml + pw + 12, y + 4, label, "tick", "start"))
    if culprit is not None:
        y = mt + len(tests) * rh + rh / 2
        out.append(bar(culprit, culprit, y, "susp-final"))
        out.append(f'<path class="dot-bad" d="M{X(culprit):.1f} {y - 6:.1f} l6 6 l-6 6 l-6 -6 z"/>')
        out.append(_txt(ml + pw + 12, y + 4, t("plot.bisect.culprit"), "mark-t", "start"))
    out.append("</svg>")
    return "".join(out)
