"""Scenario files: finding them, validating them before a flight, and the reference tables.

A company's scenarios live in their own directories (`--scenarios DIR [DIR ...]`). Everything a
flight would trip over is checked here first, with file and line: the YAML, the keys, every step
and its argument, the `expect:` rules and whether the metrics they name can be produced at all.
The step vocabulary comes from `Runner.step_*`; the argument shapes (STEPS) and the metric names
(METRICS) are tables here, and the tests fail when runner.py or metrics.py gain something they lack.
A scenario with `vehicle: plane` is checked against PlaneRunner's steps, Plane mode names and
PLANE_METRICS instead; one with `autopilot: px4` against Px4Runner's steps, PX4 mode names and
PX4_METRICS.
"""
from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pymavlink import mavutil

from . import oracle, vehicles
from .config import SCENARIOS, WORK
from .runner import Runner

TOP_KEYS = {"name": "required: same as the file name without .yaml",
            "description": "optional free text, ignored by the tools",
            "params": "optional: SITL parameters set before arming",
            "steps": "required: the flight, in order",
            "expect": "optional: absolute rules on metrics",
            "vehicle": "optional: copter (default) or plane",
            "autopilot": "optional: ardupilot (default) or px4",
            "frame": "optional, plane only: plane (default) or quadplane"}


class ScenarioSetError(RuntimeError):
    pass


# --- YAML with line numbers ------------------------------------------------------------------

class LDict(dict):
    line = 0

    def __init__(self):
        super().__init__()
        self.key_line, self.val_line = {}, {}


class LList(list):
    line = 0

    def __init__(self):
        super().__init__()
        self.item_line = []


class _Loader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self.dups = []      # (key, line): safe_load silently keeps the last of a duplicated key


def _map(loader, node):
    loader.flatten_mapping(node)
    d = LDict()
    d.line = node.start_mark.line + 1
    for k, v in node.value:
        key = loader.construct_object(k, deep=True)
        try:
            hash(key)
        except TypeError:
            raise yaml.constructor.ConstructorError(None, None, "a mapping key must be a plain "
                                                    "scalar", k.start_mark) from None
        if key in d:
            loader.dups.append((key, k.start_mark.line + 1))
        d[key] = loader.construct_object(v, deep=True)
        d.key_line[key], d.val_line[key] = k.start_mark.line + 1, v.start_mark.line + 1
    return d


def _seq(loader, node):
    out = LList()
    out.line = node.start_mark.line + 1
    for item in node.value:
        out.append(loader.construct_object(item, deep=True))
        out.item_line.append(item.start_mark.line + 1)
    return out


_Loader.add_constructor("tag:yaml.org,2002:map", _map)
_Loader.add_constructor("tag:yaml.org,2002:seq", _seq)


@dataclass(frozen=True)
class Issue:
    path: str
    line: int
    level: str          # "error" | "warning"
    msg: str

    def __str__(self):
        return f"{self.path}:{self.line}: {self.level}: {self.msg}"


class _Ctx:
    def __init__(self, path):
        self.path, self.issues = str(path), []
        self.vehicle, self.frame = "copter", None
        self.steps, self.modes = STEPS, modes()

    def err(self, line, msg):
        self.issues.append(Issue(self.path, line, "error", msg))

    def warn(self, line, msg):
        self.issues.append(Issue(self.path, line, "warning", msg))


# --- vocabulary ----------------------------------------------------------------------------------

def step_names() -> list[str]:
    """Every step the runner implements (introspection, so a new Runner.step_* is seen at once)."""
    return sorted(n[5:] for n in dir(Runner) if n.startswith("step_"))


def modes() -> list[str]:
    return sorted(mavutil.mode_mapping_acm.values())


def _near(word, options) -> str:
    low = {str(o).lower(): o for o in options}
    hit = difflib.get_close_matches(str(word).lower(), list(low), n=1, cutoff=0.6)
    return f" (did you mean '{low[hit[0]]}'?)" if hit else ""


def _unknown(kind, word, options, list_all=True) -> str:
    options = sorted(options)
    tail = f"; valid: {', '.join(map(str, options))}" if list_all and len(options) <= 16 else ""
    return f"unknown {kind} '{word}'{_near(word, options)}{tail}"


def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _show(v) -> str:
    if v is None:
        return "nothing"
    text = repr(dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
    return text if len(text) <= 40 else ("a mapping" if isinstance(v, dict) else "a list")


def _num(ctx, v, line, what, lo=None, hi=None, strict=False) -> bool:
    """A finite number within [lo, hi] (lo exclusive when strict)."""
    if not _isnum(v):
        ctx.err(line, f"{what} must be a number, got {_show(v)}")
        return False
    if lo is not None and (v <= lo if strict else v < lo):
        ctx.err(line, f"{what} must be {'>' if strict else '>='} {lo}, got {v}")
        return False
    if hi is not None and v > hi:
        ctx.err(line, f"{what} must be <= {hi}, got {v}")
        return False
    return True


# --- step argument shapes ----------------------------------------------------------------------

def _positive(what, lo=0, strict=True, hi=None):
    return lambda ctx, v, line: _num(ctx, v, line, what, lo, hi, strict)


def _point(ctx, v, line, what="target"):
    if not isinstance(v, list) or len(v) != 3:
        ctx.err(line, f"{what} must be a list [north, east, up] in metres, got {_show(v)}")
        return False
    lines = getattr(v, "item_line", [line] * 3)
    ok = all(_num(ctx, x, ln, f"{what} {axis}") for x, ln, axis in zip(v, lines, ("north", "east", "up")))
    if ok and v[2] < 0:
        ctx.warn(line, f"{what} is {-v[2]} m below home (up = {v[2]})")
    return ok


def _mission(ctx, v, line):
    if not isinstance(v, list) or not v:
        ctx.err(line, f"mission must be a non-empty list of [north, east, up] waypoints, got {_show(v)}")
        return
    for i, (wp, ln) in enumerate(zip(v, v.item_line if hasattr(v, "item_line") else [line] * len(v)), 1):
        _point(ctx, wp, ln, f"waypoint {i}")


def _velocity(ctx, v, line):
    keys = {"vx", "vy", "vz", "frame", "seconds"}
    if not isinstance(v, dict):
        ctx.err(line, f"velocity takes a mapping like {{vx: 3, frame: body, seconds: 8}}, got {_show(v)}")
        return
    for k in v:
        if k not in keys:
            ctx.err(v.key_line[k], _unknown("velocity key", k, keys))
    if "seconds" not in v:
        ctx.err(line, "velocity needs 'seconds' (how long the command is repeated)")
    for k in ("seconds", "vx", "vy", "vz"):
        if k in v:
            _num(ctx, v[k], v.val_line[k], f"velocity {k}", *((0, None, True) if k == "seconds" else ()))
    if "frame" in v and v["frame"] not in Runner.VEL_FRAMES:
        ctx.err(v.val_line["frame"], _unknown("frame", v["frame"], Runner.VEL_FRAMES))
    if "seconds" in v and not any(_isnum(v.get(k)) and v[k] for k in ("vx", "vy", "vz")):
        ctx.warn(line, "velocity command with no non-zero vx, vy or vz")


def _set_param(ctx, v, line):
    if not isinstance(v, dict) or not v:
        ctx.err(line, f"set_param takes a mapping like {{SIM_GPS1_ENABLE: 0}}, got {_show(v)}")
        return
    _params(ctx, v, line, "set_param")


def _params(ctx, v, line, what):
    for k, x in v.items():
        ln = getattr(v, "key_line", {}).get(k, line)
        if not isinstance(k, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", k):
            ctx.err(ln, f"{what}: '{k}' is not a parameter name (upper case letters, digits, _)"
                    + (f" (did you mean '{k.upper()}'?)" if isinstance(k, str) and k.upper() != k else ""))
        elif len(k) > 16:
            ctx.err(ln, f"{what}: '{k}' is longer than 16 characters, the longest ArduPilot parameter name")
        if not _isnum(x):
            ctx.err(ln, f"{what}: {k} must be a number, got {_show(x)}"
                    + (" (write 1 or 0, not true or false)" if isinstance(x, bool) else ""))


def _mode(ctx, v, line):
    if not isinstance(v, str):
        ctx.err(line, f"mode must be a flight mode name such as LOITER, got {_show(v)}")
    elif v not in ctx.modes:
        ctx.err(line, _unknown("mode", v, ctx.modes, list_all=False) + "; mode names are upper case")


def _sticks(ctx, v, line):
    if not isinstance(v, dict) or not v:
        ctx.err(line, f"sticks takes a mapping like {{pitch: 1300, throttle: 1500}}, got {_show(v)}")
        return
    for k, x in v.items():
        ln = getattr(v, "val_line", {}).get(k, line)
        if k not in Runner.STICKS:
            ctx.err(getattr(v, "key_line", {}).get(k, line), _unknown("stick channel", k, Runner.STICKS))
        else:
            _num(ctx, x, ln, f"sticks {k} (PWM)", 1000, 2000)


def _yaw(ctx, v, line):
    _num(ctx, v, line, "yaw heading in degrees", 0, 360)


def _wait(what):
    def check(ctx, v, line):
        if isinstance(v, dict):
            for k in v:
                if k != "until":
                    ctx.err(v.key_line[k], _unknown(f"{what} key", k, ("until",)))
            if "until" not in v:
                ctx.err(line, f"{what} takes seconds or {{until: <s>}}, seconds since the epoch step")
            else:
                _num(ctx, v["until"], v.val_line["until"], f"{what} until (s)", 0)
        else:
            _num(ctx, v, line, f"{what} seconds", 0, None, True)
    return check


def _no_arg(ctx, v, line):
    if v is not None:
        ctx.err(line, f"epoch takes no argument (write `- epoch:`), got {_show(v)}")


@dataclass(frozen=True)
class Step:
    arg: str
    example: str
    desc: str
    check: object


STEPS = {
    "takeoff": Step("number, metres", "takeoff: 10",
                    "Switch to GUIDED, arm and climb to this height; waits until 95% of it is reached.",
                    _positive("takeoff height in metres")),
    "hold": Step("number, seconds", "hold: 20",
                 "Wait in place (sim time). Its window is judged as a hover (hold_* metrics) until the "
                 "next goto, mode or set_param step.", _positive("hold seconds")),
    "fly": Step("number, seconds; or {until: s}", "fly: 8",
                "Wait while the pilot flies (sticks set). Like hold, but not judged as a hover. "
                "`{until: s}` waits until s seconds after the epoch step.", _wait("fly")),
    "epoch": Step("none", "epoch:",
                  "Scenario time 0 is now: later `fly`/`release` `{until: s}` count from here.", _no_arg),
    "goto": Step("[north, east, up], metres from home", "goto: [30, 0, 15]",
                 "GUIDED position target; waits until within 1 m of it (90 s limit).", _point),
    "send_goto": Step("[north, east, up], metres from home", "send_goto: [150, 0, 10]",
                      "Like goto but does not wait: for targets the firmware may refuse (a fence).",
                      _point),
    "yaw": Step("number, degrees 0-360", "yaw: 90",
                "GUIDED: turn to an absolute heading; waits until within 3 degrees.", _yaw),
    "velocity": Step("{vx, vy, vz, frame, seconds}", "velocity: {vx: 3, frame: body, seconds: 8}",
                     "GUIDED velocity command (m/s; vz is down) repeated for `seconds`; frame is "
                     "local (north/east) or body (default local).", _velocity),
    "set_param": Step("{NAME: number}", "set_param: {SIM_GPS1_ENABLE: 0}",
                      "Set parameters in flight; the first one is the fault whose reaction "
                      "failsafe_reaction_s measures.", _set_param),
    "mode": Step("mode name, upper case", "mode: LOITER",
                 "Switch flight mode and wait for the vehicle to report it.", _mode),
    "mission": Step("list of [north, east, up]", "mission: [[40, 0, 20], [40, 40, 20]]",
                    "Upload home, these waypoints and a final RTL. Start it with `mode: AUTO`.", _mission),
    "sticks": Step("{roll, pitch, throttle, yaw: PWM 1000-2000}", "sticks: {pitch: 1300, throttle: 1500}",
                   "Pilot RC override, held (and re-sent) until changed; channels not named are left alone.",
                   _sticks),
    "release": Step("number, seconds; or {until: s}", "release: 15",
                    "Centre roll, pitch and yaw (throttle stays) and watch the stop for this long: "
                    "the stop_* metrics. `{until: s}` as for fly.", _wait("release")),
    "wait_disarm": Step("number, seconds timeout", "wait_disarm: 120",
                        "Wait for the vehicle to disarm (landing); the flight metrics need it.",
                        _positive("wait_disarm timeout in seconds")),
}
NEEDS_FLIGHT = {"hold", "fly", "goto", "send_goto", "yaw", "velocity", "release"}
GUIDED_ONLY = {"goto", "send_goto", "yaw", "velocity"}      # other modes ignore these commands


# --- metrics -----------------------------------------------------------------------------------------

METRICS = [
    ("completed", "0/1", "1 when every step ran to the end; below 1 is always a FAIL.", "always"),
    ("takeoff_time_s", "s", "From arming until the climb passes 95% of the takeoff height.",
     "a takeoff step"),
    ("hold_drift_max_m", "m", "Largest horizontal distance from the position where a hold started.",
     "a hold of 1 s or more"),
    ("hold_alt_err_max_m", "m", "Largest altitude change from the start of a hold.",
     "a hold of 1 s or more"),
    ("hold_tilt_rms_deg", "deg", "RMS of the tilt angle during holds.", "a hold of 1 s or more"),
    ("goto_time_total_s", "s", "Sum over goto steps of the time from command to arrival.", "a goto step"),
    ("goto_overshoot_max_m", "m",
     "How far past the target, along the leg, the vehicle went (legs shorter than 1 m are skipped).",
     "a goto step"),
    ("failsafe_reaction_s", "s",
     "From the first set_param step until the flight mode changes; inf when it never does. "
     "Resolution is about 1 s (heartbeat).", "a set_param step"),
    ("mission_wp_reached", "count", "Distinct mission waypoints reported reached.", "a mission step"),
    ("mission_time_s", "s", "From `mode: AUTO` until disarm.", "mission, `mode: AUTO` and wait_disarm"),
    ("mission_speed_max_mps", "m/s", "Highest ground speed from `mode: AUTO` until disarm.",
     "mission, `mode: AUTO` and wait_disarm"),
    ("stop_dist_m_<mode>", "m", "After a release: how far the vehicle coasts from the release point.",
     "a release step"),
    ("stop_backtrack_m_<mode>", "m",
     "After a release: how far it comes back towards the release point after its furthest point.",
     "a release step"),
    ("stop_time_s_<mode>", "s",
     "After a release: time until ground speed is under 0.3 m/s (the whole window if it never is).",
     "a release step"),
    ("stop_alt_dev_m_<mode>", "m", "After a release: largest height change while stopping.",
     "a release step"),
    ("stick_speed_mps_<mode>", "m/s", "Ground speed at the moment of a release.", "a release step"),
    ("mode_speed_mps_<mode>", "m/s",
     "Mean ground speed of a stretch spent in a mode you switched to (see Mode segments).",
     "a segment of 5 s or more"),
    ("mode_yaw_rate_dps_<mode>", "deg/s", "Mean absolute yaw rate of the same stretch.",
     "a segment of 5 s or more"),
    ("vel_err_mps_<n>", "m/s",
     "n-th velocity step: distance between the commanded and the flown mean velocity, after the "
     "first 3 s.", "a velocity step longer than 3 s"),
    ("alt_max_m", "m", "Highest altitude above home.", "always"),
    ("range_max_m", "m", "Furthest horizontal distance from home: a refused target or a fence keeps "
                         "it small.", "always"),
    ("flight_time_s", "s", "From arming to disarming.", "takeoff and wait_disarm"),
    ("landing_offset_m", "m", "Horizontal distance from home at the end.", "takeoff and wait_disarm"),
    ("disarm_delay_s", "s", "From touchdown until the vehicle disarms.", "takeoff and wait_disarm"),
    ("touchdown_speed_mps", "m/s", "Fastest descent in the last 2 m before touchdown.",
     "takeoff and wait_disarm"),
]
FAMILIES = {"stop": ("stop_dist_m", "stop_backtrack_m", "stop_time_s", "stop_alt_dev_m", "stick_speed_mps"),
            "mode": ("mode_speed_mps", "mode_yaw_rate_dps")}
STATIC = [m[0] for m in METRICS if "<" not in m[0]]
_CUTS = {"mode", "sticks", "release", "release_end", "goto", "send_goto", "velocity", "yaw",
         "set_param", "error", "disarmed"}     # metrics._mode_metrics: what ends a segment


@dataclass
class Prediction:
    certain: set
    possible: set
    loose: bool          # a release may happen in another mode than the steps set (failsafe, mission end)
    why: dict            # metric -> why it is not produced


def predict(steps) -> Prediction:
    """Which metrics the flight of these (shape-valid) steps will produce, by walking the events
    the runner would record. `certain`: yes. `possible`: depends on what the flight does."""
    # kind, detail, seconds until the next event: at least, at most this much more, unbounded
    ev = [["ready", None, 0.0, 0.0, False]]

    def add(kind, detail=None):
        ev.append([kind, detail, 0.0, 0.0, False])

    def wait(sec=0.0, unbounded=False, slack=0.0):
        ev[-1][2] += sec
        ev[-1][3] += slack
        ev[-1][4] |= unbounded

    mode, loose = None, False
    cert, poss, why = {"completed", "alt_max_m", "range_max_m"}, set(), {}
    seen = {n: False for n in ("takeoff", "hold", "goto", "set_param", "mission", "auto", "disarm")}
    vels, rels = [], []
    for name, arg in steps:
        if name == "takeoff":
            add("armed"); wait(unbounded=True); add("takeoff_cmd"); wait(unbounded=True); add("takeoff_done")
            mode, seen["takeoff"] = "GUIDED", True
        elif name == "hold":
            add("hold_start"); wait(arg)
            seen["hold"] = True
        elif name == "fly":
            wait(*((0.0, True) if isinstance(arg, dict) else (arg,)))
        elif name == "goto":
            add("goto"); wait(unbounded=True); add("arrived")
            seen["goto"] = True
        elif name == "send_goto":
            add("send_goto")
        elif name == "yaw":
            add("yaw"); wait(unbounded=True)
        elif name == "velocity":
            add("velocity"); wait(arg["seconds"]); add("velocity_end")
            vels.append(arg["seconds"])
        elif name == "set_param":
            for _ in arg:
                add("set_param")
            seen["set_param"], loose = True, True
        elif name == "mode":
            add("mode", arg); wait(slack=1.5)      # the 1 Hz heartbeat is what confirms the switch
            mode = arg
            seen["auto"] = seen["auto"] or arg == "AUTO"
        elif name == "mission":
            add("mission")
            seen["mission"], loose = True, True
        elif name == "sticks":
            add("sticks")
        elif name == "release":
            add("release", mode); wait(*((0.0, True) if isinstance(arg, dict) else (arg,))); add("release_end")
            rels.append((mode, loose))
        elif name == "wait_disarm":
            wait(unbounded=True); add("disarmed")
            seen["disarm"] = True
    if seen["takeoff"]:
        cert.add("takeoff_time_s")
    else:
        why["takeoff_time_s"] = "it needs a takeoff step"
    # a hold window lasts until the next goto, mode, set_param or hold step, and needs 5 samples (0.2 s)
    best = None
    for i, e in enumerate(ev):
        if e[0] == "hold_start":
            j = next((k for k in range(i + 1, len(ev)) if ev[k][0] in ("goto", "mode", "set_param", "hold_start")),
                     len(ev))
            low = sum(x[2] for x in ev[i:j])
            unb = any(x[4] for x in ev[i:j])
            best = max(best or 0, 2 if low >= 1 else 1 if (unb or low >= 0.2) else 0)
    hold = {"hold_drift_max_m", "hold_alt_err_max_m", "hold_tilt_rms_deg"}
    if best == 2:
        cert |= hold
    elif best == 1:
        poss |= hold
    else:
        for n in hold:
            why[n] = "it needs a hold step that lasts at least 0.2 s" if seen["hold"] else "it needs a hold step"
    if seen["goto"]:
        cert.add("goto_time_total_s")
        poss.add("goto_overshoot_max_m")
    else:
        why["goto_time_total_s"] = why["goto_overshoot_max_m"] = "it needs a goto step"
    if seen["set_param"]:
        cert.add("failsafe_reaction_s")
    else:
        why["failsafe_reaction_s"] = "it needs a set_param step (the injected fault)"
    if seen["mission"]:
        cert.add("mission_wp_reached")
    else:
        why["mission_wp_reached"] = "it needs a mission step"
    if seen["mission"] and seen["auto"] and seen["disarm"]:
        poss |= {"mission_time_s", "mission_speed_max_mps"}
    else:
        for n in ("mission_time_s", "mission_speed_max_mps"):
            why[n] = "it needs a mission step, `mode: AUTO` and wait_disarm"
    if seen["takeoff"] and seen["disarm"]:
        cert |= {"flight_time_s", "landing_offset_m"}
        poss |= {"disarm_delay_s", "touchdown_speed_mps"}
    else:
        for n in ("flight_time_s", "landing_offset_m", "disarm_delay_s", "touchdown_speed_mps"):
            why[n] = "it needs a takeoff step and a wait_disarm step"

    def names(stems, mode_name, count):
        return {f"{s}_{mode_name.lower()}{count if count > 1 else ''}" for s in stems}

    counts: dict = {}
    for m, risky in rels:       # a release always yields its metrics; only the mode name can be uncertain
        if m is None:
            continue
        c = counts[m] = counts.get(m, 0) + 1
        (poss if risky else cert).update(names(FAMILIES["stop"], m, c))
    loose = any(r for _, r in rels)
    seg: dict = {}              # mode -> possible counts so far
    cur = None
    for i, (kind, detail, *_) in enumerate(ev):
        if kind == "mode":
            cur = detail
        if cur is None or kind not in ("mode", "sticks", "release"):
            continue
        j = next((k for k in range(i + 1, len(ev)) if ev[k][0] in _CUTS), None)
        if j is None:
            continue
        low = sum(e[2] for e in ev[i:j])
        high = low + sum(e[3] for e in ev[i:j])
        if high < 5 and not any(e[4] for e in ev[i:j]):
            continue
        counts_now = seg.get(cur, {0})
        new = {c + 1 for c in counts_now}
        sure = low >= 5 and len(counts_now) == 1
        for c in new:
            (cert if sure else poss).update(names(FAMILIES["mode"], cur, c))
        seg[cur] = new if low >= 5 else counts_now | new
    n = 0
    for s in vels:
        if s <= 3.2:
            continue
        n += 1
        (cert if s >= 3.5 else poss).add(f"vel_err_mps_{n}")
    return Prediction(cert, poss - cert, loose, why)


def _metric_problem(name, p: Prediction):
    """None when the metric can be produced; ("error"|"warning", message) otherwise."""
    if name in p.certain or name in p.possible or name == "completed":
        return None
    if name in p.why:
        return "error", f"metric '{name}' is never produced here: {p.why[name]}"
    m = re.fullmatch(r"(vel_err_mps)_(\d+)", name)
    if m:
        return "error", (f"metric '{name}' is never produced here: there are not {m[2]} velocity steps "
                         "longer than 3 s (the first 3 s of each are the settling time)")
    for fam, stems in FAMILIES.items():
        for stem in stems:
            if name.startswith(stem + "_"):
                suffix = name[len(stem) + 1:]
                base = re.sub(r"\d+$", "", suffix).upper()
                if base not in modes():
                    pool = sorted(p.certain | p.possible)
                    return "error", _unknown("metric", name, pool or STATIC, list_all=False) + \
                        f"; '{suffix}' is not a flight mode name"
                if fam == "stop" and p.loose:
                    return "warning", (f"metric '{name}' may not be produced: it depends on the vehicle "
                                       f"being in {base} at that moment, and a failsafe or mission "
                                       "can change the mode (cannot be decided before flying)")
                what = ("a release step" if fam == "stop" else
                        "a stretch of 5 s or more after `mode: " + base + "`, ended by another step")
                return "error", f"metric '{name}' is never produced here: it needs {what} in {base}"
    return "error", _unknown("metric", name, p.certain | p.possible | set(STATIC), list_all=False) + \
        "; list them with `forkpilot lint --reference`"


# --- one file --------------------------------------------------------------------------------------

def load(path: Path):
    """(data, dups) with line numbers on every mapping and list; raises yaml.MarkedYAMLError."""
    loader = _Loader(Path(path).read_text())
    try:
        return loader.get_single_data(), loader.dups
    finally:
        loader.dispose()


def lint_file(path) -> list[Issue]:
    ctx = _Ctx(path)
    _lint(ctx, Path(path))
    return sorted(ctx.issues, key=lambda i: i.line)


def _lint(ctx, path):
    try:
        spec, dups = load(path)
    except yaml.MarkedYAMLError as e:
        ctx.err((e.problem_mark.line + 1) if e.problem_mark else 1,
                f"YAML: {e.problem or e.context}")
        return
    except (OSError, UnicodeDecodeError) as e:
        ctx.err(1, f"cannot read: {e}")
        return
    for key, line in dups:
        ctx.err(line, f"key '{key}' appears twice (only the last would count)")
    if not isinstance(spec, dict):
        ctx.err(1, "a scenario is a mapping with name, steps and (optionally) params and expect")
        return
    for k in spec:
        if k not in TOP_KEYS:
            ctx.err(spec.key_line[k], _unknown("key", k, TOP_KEYS))
    if "name" not in spec:
        ctx.err(1, "missing 'name'")
    elif spec["name"] != path.stem:
        ctx.err(spec.val_line["name"], f"name '{spec['name']}' must equal the file name '{path.stem}' "
                "(results and rules are keyed by it)")
    _vehicle(ctx, spec)
    if "params" in spec:
        p = spec["params"]
        if p is None:
            pass
        elif not isinstance(p, dict):
            ctx.err(spec.val_line["params"], f"params must be a mapping NAME: number, got {_show(p)}")
        else:
            _params(ctx, p, spec.val_line["params"], "params")
    steps = _steps(ctx, spec)
    rules = _rules(ctx, spec)
    if steps is not None and rules:
        pred = predict(steps) if ctx.vehicle == "copter" else _px4_have(steps) if ctx.vehicle == "px4" \
            else _plane_have(steps, ctx.frame)
        check = {"copter": _metric_problem, "px4": _px4_metric_problem}.get(ctx.vehicle, _plane_metric_problem)
        for name, (line, _) in rules.items():
            problem = check(name, pred)
            if problem:
                (ctx.err if problem[0] == "error" else ctx.warn)(line, problem[1])


def _steps(ctx, spec):
    """Check every step; returns [(name, arg)] when they are all well-formed, else None."""
    steps = spec.get("steps")
    if "steps" not in spec:
        ctx.err(1, "missing 'steps'")
        return None
    if not isinstance(steps, list) or not steps:
        ctx.err(spec.val_line["steps"], "steps must be a non-empty list, one `- step: argument` per line")
        return None
    vocab = {"copter": step_names, "px4": px4_step_names}.get(ctx.vehicle, plane_step_names)()
    out, bad = [], False
    mission, took_off, disarm, cur, epoch = False, False, False, None, False
    for step, line in zip(steps, steps.item_line):
        before = len(ctx.issues)
        if isinstance(step, str):
            ctx.err(line, f"step '{step}' needs an argument: `- {step}: <argument>`"
                    + (f" (for example `{ctx.steps[step].example}`)" if step in ctx.steps
                       else _near(step, vocab)))
            bad = True
            continue
        if not isinstance(step, dict) or len(step) != 1:
            ctx.err(line, "each step is a single `name: argument` entry"
                    + (f"; this one has {len(step)} keys, put each in its own `- ` item"
                       if isinstance(step, dict) else ""))
            bad = True
            continue
        (name, arg), = step.items()
        line = step.key_line[name]
        if ctx.vehicle == "px4" and name in PX4_UNSUPPORTED:
            ctx.err(line, f"step '{name}' has no PX4 command; turn with a goto or in a mission instead")
            bad = True
            continue
        if ctx.vehicle == "plane" and name in COPTER_ONLY:
            ctx.err(line, f"step '{name}' is a Copter GUIDED command; a {ctx.vehicle} scenario cannot use it")
            bad = True
            continue
        if name not in vocab:
            ctx.err(line, _unknown("step", name, vocab))
            bad = True
            continue
        if name not in ctx.steps:   # a step added to the runner that this validator does not know yet
            ctx.warn(line, f"step '{name}' exists but its argument is not checked")
            out.append((name, arg))
            continue
        ctx.steps[name].check(ctx, arg, step.val_line[name])
        if len(ctx.issues) > before and any(i.level == "error" for i in ctx.issues[before:]):
            bad = True
            continue
        out.append((name, arg))
        if name in NEEDS_FLIGHT and not took_off:
            ctx.warn(line, f"'{name}' before any takeoff step: the vehicle is on the ground")
        if name in GUIDED_ONLY and cur not in (None, "GUIDED") and ctx.vehicle != "px4":
            ctx.warn(line, f"'{name}' is a GUIDED command and the vehicle is in {cur} here: it would be "
                     "ignored (add `mode: GUIDED` first)")
        if name in ("fly", "release") and isinstance(arg, dict) and not epoch:
            ctx.warn(line, f"'{name}: {{until: ...}}' with no epoch step before it: it counts from "
                     "the start of the run")
        if name == "takeoff":
            took_off, cur = True, {"copter": "GUIDED", "px4": "AUTO.LOITER"}.get(ctx.vehicle) or \
                _plane_takeoff_mode(arg, ctx.frame)
        elif name == "mode":
            cur = arg
        elif name == "epoch":
            epoch = True
        if name == "mission":
            mission = True
        elif name == "mode" and arg in ("AUTO", "AUTO.MISSION"):
            if not mission:
                ctx.warn(line, "mode AUTO with no mission step before it: AUTO has nothing to fly")
        elif name == "wait_disarm":
            disarm = True
    # a plane cannot land in RTL or LOITER: ending in the air is normal there, and a rule on a
    # landing metric is still caught by the metric check
    if took_off and not disarm and ctx.vehicle in ("copter", "px4"):
        ctx.warn(spec.val_line["steps"], "no wait_disarm step: the run ends in the air or on landing; "
                 "flight_time_s and landing_offset_m are not produced")
    return None if bad else out


def _rules(ctx, spec) -> dict:
    """Check `expect:`; returns {metric: (line, rule)} for the well-formed rules."""
    if "expect" not in spec or spec["expect"] is None:
        return {}
    exp = spec["expect"]
    if not isinstance(exp, dict):
        ctx.err(spec.val_line["expect"], "expect must be a mapping: metric: {op: bound}")
        return {}
    out, hints = {}, {"<": "lt", "<=": "le", ">": "gt", ">=": "ge", "lte": "le", "gte": "ge"}
    for name, rule in exp.items():
        line = exp.key_line[name]
        ok = True
        if not isinstance(rule, dict) or not rule:
            ctx.err(line, f"rule for '{name}' must be a mapping like {{lt: 1.5}} "
                    f"(operators: {', '.join(oracle.OPS)}), got {_show(rule)}")
            continue
        for op, bound in rule.items():
            if op not in oracle.OPS:
                hint = f" (did you mean '{hints[op]}'?)" if op in hints else _near(op, oracle.OPS)
                ctx.err(rule.key_line[op], f"unknown operator '{op}'{hint}; valid: {', '.join(oracle.OPS)}")
                ok = False
            elif not _num(ctx, bound, rule.val_line[op], f"bound of {name} {op}"):
                ok = False
        if ok:
            lo = max((b for o, b in rule.items() if o in ("gt", "ge")), default=-math.inf)
            hi = min((b for o, b in rule.items() if o in ("lt", "le")), default=math.inf)
            if lo >= hi and not (lo == hi and "gt" not in rule and "lt" not in rule):
                ctx.warn(line, f"rule for '{name}' can never be satisfied: {rule}")
            out[name] = (line, rule)
    return out


# --- vehicles ----------------------------------------------------------------------------------

def _vehicle(ctx, spec):
    """Set ctx's vehicle, frame and vocabulary from `autopilot:`, `vehicle:` and `frame:`."""
    autopilot = spec.get("autopilot", "ardupilot")
    if autopilot not in vehicles.AUTOPILOTS:
        ctx.err(spec.val_line["autopilot"], _unknown("autopilot", autopilot, vehicles.AUTOPILOTS))
        return
    if autopilot == "px4":
        if spec.get("vehicle", "copter") != "copter":
            ctx.err(spec.val_line["vehicle"], "PX4 scenarios fly a multicopter for now (leave vehicle out)")
        v = vehicles.VEHICLES["px4"]
        frame = spec.get("frame", v.default_frame)
        if frame not in v.frames:
            ctx.err(spec.val_line["frame"], _unknown("frame", frame, v.frames))
        ctx.vehicle, ctx.frame, ctx.steps, ctx.modes = "px4", frame, PX4_STEPS, px4_modes()
        return
    name = spec.get("vehicle", "copter")
    if name not in vehicles.VEHICLES:
        ctx.err(spec.val_line["vehicle"], _unknown("vehicle", name, vehicles.VEHICLES))
        return
    v = vehicles.VEHICLES[name]
    frame = spec.get("frame", v.default_frame)
    if name == "copter" and "frame" in spec:
        ctx.err(spec.key_line["frame"], "frame applies to a plane scenario only (vehicle: plane)")
    elif frame not in v.frames:
        ctx.err(spec.val_line["frame"], _unknown("frame", frame, v.frames))
        frame = v.default_frame
    ctx.vehicle, ctx.frame = name, frame
    if name == "plane":
        ctx.steps, ctx.modes = PLANE_STEPS, plane_modes()


def plane_step_names() -> list[str]:
    from .plane import PlaneRunner
    return sorted(n[5:] for n in dir(PlaneRunner) if n.startswith("step_") and n[5:] not in COPTER_ONLY)


def plane_modes() -> list[str]:
    return sorted(set(mavutil.mode_mapping_apm.values()))


def _plane_takeoff_mode(arg, frame):
    if isinstance(arg, dict) and arg.get("mode"):
        return arg["mode"]
    return "QLOITER" if frame == "quadplane" else "AUTO"


def _plane_takeoff(ctx, v, line):
    if not isinstance(v, dict):
        _num(ctx, v, line, "takeoff height in metres", 0, None, True)
        return
    for k in v:
        if k not in ("alt", "mode"):
            ctx.err(v.key_line[k], _unknown("takeoff key", k, ("alt", "mode")))
    if "alt" not in v:
        ctx.err(line, "takeoff needs 'alt' (metres)")
    else:
        _num(ctx, v["alt"], v.val_line["alt"], "takeoff height in metres", 0, None, True)
    if "mode" in v:
        _mode(ctx, v["mode"], v.val_line["mode"])


MISSION_ITEMS = {"takeoff": "height in m (NAV_TAKEOFF)", "vtol_takeoff": "height in m (QuadPlane)",
                 "wp": "[north, east, up]", "loiter_turns": "{at: [north, east, up], turns, radius}",
                 "land": "[north, east] (fixed-wing landing)", "vtol_land": "[north, east] (QuadPlane)",
                 "transition": "mc or fw", "speed": "airspeed in m/s", "land_start": "true", "rtl": "true"}


def _plane_item(ctx, item, line, i):
    if isinstance(item, list):
        _point(ctx, item, line, f"waypoint {i}")
        return
    if not isinstance(item, dict) or len(item) != 1:
        ctx.err(line, f"mission item {i} must be [north, east, up] or one `kind: argument`, got {_show(item)}")
        return
    (kind, arg), = item.items()
    ln, what = item.val_line[kind], f"mission item {i} ({kind})"
    if kind not in MISSION_ITEMS:
        ctx.err(item.key_line[kind], _unknown("mission item", kind, MISSION_ITEMS))
    elif kind in ("takeoff", "vtol_takeoff"):
        _num(ctx, arg, ln, f"{what} height", 0, None, True)
    elif kind == "wp":
        _point(ctx, arg, ln, what)
    elif kind in ("land", "vtol_land"):
        if not isinstance(arg, list) or len(arg) not in (2, 3):
            ctx.err(ln, f"{what} must be [north, east] in metres, got {_show(arg)}")
        else:
            for x, axis in zip(arg, ("north", "east")):
                _num(ctx, x, ln, f"{what} {axis}")
    elif kind == "loiter_turns":
        keys = ("at", "turns", "radius")
        if not isinstance(arg, dict) or "at" not in arg:
            ctx.err(ln, f"{what} must be a mapping like {{at: [0, 0, 60], turns: 2, radius: 80}}, got {_show(arg)}")
            return
        for k in arg:
            if k not in keys:
                ctx.err(arg.key_line[k], _unknown("loiter_turns key", k, keys))
        _point(ctx, arg["at"], arg.val_line["at"], f"{what} at")
        if "turns" in arg:
            _num(ctx, arg["turns"], arg.val_line["turns"], f"{what} turns", 0, None, True)
        if "radius" in arg:
            _num(ctx, arg["radius"], arg.val_line["radius"], f"{what} radius", 0)
    elif kind == "transition":
        if arg not in ("mc", "fw"):
            ctx.err(ln, f"{what} must be mc (hover) or fw (wing-borne), got {_show(arg)}")
    elif kind == "speed":
        _num(ctx, arg, ln, f"{what} airspeed", 0, None, True)
    elif arg is not True:
        ctx.err(ln, f"{what} takes `true`, got {_show(arg)}")


def _plane_mission(ctx, v, line):
    if not isinstance(v, list) or not v:
        ctx.err(line, f"mission must be a non-empty list of mission items, got {_show(v)}")
        return
    for i, (item, ln) in enumerate(zip(v, getattr(v, "item_line", [line] * len(v))), 1):
        _plane_item(ctx, item, ln, i)


def _wait_wp(ctx, v, line):
    if _num(ctx, v, line, "wait_wp mission item number", 1) and v != int(v):
        ctx.err(line, f"wait_wp takes a whole mission item number, got {v}")


COPTER_ONLY = {"goto", "send_goto", "yaw", "velocity"}     # GUIDED position, velocity, heading
PLANE_STEPS = {**{n: st for n, st in STEPS.items() if n not in COPTER_ONLY},
               "takeoff": Step("number, metres; or {alt, mode}", "takeoff: 40",
                               "Plane: arm in AUTO (a mission takeoff item flies the climb). QuadPlane: "
                               "QLOITER with the throttle stick. Waits until 95% of the height; "
                               "`{alt: 30, mode: AUTO}` picks the mode.", _plane_takeoff),
               "mission": Step("list of mission items", "mission: [{takeoff: 40}, {wp: [500, 0, 60]}]",
                               "Upload home and these items: [north, east, up] or one of "
                               + ", ".join(f"`{k}` ({v})" for k, v in MISSION_ITEMS.items())
                               + ". A list of only waypoints gets a final RTL.", _plane_mission),
               "wait_wp": Step("number, mission item", "wait_wp: 3",
                               "Wait until the mission's current item is this one or later (600 s limit).",
                               _wait_wp)}

# name, unit, meaning, produced when, what the steps must have ("land": a land or vtol_land item)
PLANE_METRICS = [
    ("completed", "0/1", "1 when every step ran to the end; below 1 is always a FAIL.", "always", ()),
    ("takeoff_time_s", "s", "From arming until the climb passes 95% of the takeoff height.", "a takeoff step",
     ("takeoff",)),
    ("mission_wp_reached", "count", "Distinct mission items reported reached.", "a mission step", ("mission",)),
    ("leg_xtrack_max_m", "m", "Largest distance off the line between two waypoints (AUTO, legs of 50 m or "
     "more ending at a wp).", "a mission with such legs", ("mission",)),
    ("leg_xtrack_rms_m", "m", "RMS of the same over the second half of each leg.", "the same", ("mission",)),
    ("leg_alt_err_rms_m", "m", "RMS height error against the leg's target on level legs (TECS).", "the same",
     ("mission",)),
    ("leg_airspeed_mps", "m/s", "Mean airspeed on the legs.", "the same", ("mission",)),
    ("leg_aspd_err_rms_mps", "m/s", "RMS airspeed error the controller reports on the legs.", "the same",
     ("mission",)),
    ("loiter_radius_m_<mode>", "m", "Fitted radius of a fixed-wing loiter (LOITER, RTL, CIRCLE, GUIDED; "
     "segments of 45 s or more, last half).", "such a segment", ()),
    ("loiter_track_err_m_<mode>", "m", "RMS distance of the track from that circle.", "the same", ()),
    ("loiter_alt_sd_m_<mode>", "m", "Height steadiness on the circle.", "the same", ()),
    ("loiter_centre_err_m_<mode>", "m", "Circle centre against where it should be (home for RTL, the entry "
     "point for LOITER); reported from 5 m up.", "the same, RTL or LOITER", ()),
    ("hold_drift_max_m", "m", "QuadPlane hover: largest horizontal drift during a hold.", "a hold step",
     ("hold",)),
    ("hold_alt_err_max_m", "m", "QuadPlane hover: largest height change during a hold.", "a hold step",
     ("hold",)),
    ("assist_time_s", "s", "Time the VTOL motors assisted wing-borne flight.", "a quadplane", ("quadplane",)),
    ("transition_fw_time_s_<n>", "s", "n-th forward transition: time to wing-borne flight.", "a quadplane",
     ("quadplane",)),
    ("transition_fw_alt_loss_m_<n>", "m", "Height lost during it; reported from 5 m up.", "a quadplane",
     ("quadplane",)),
    ("back_transition_time_s_<n>", "s", "n-th back transition: until under 3 m/s ground speed.", "a quadplane",
     ("quadplane",)),
    ("back_transition_dist_m_<n>", "m", "Distance flown during it.", "a quadplane", ("quadplane",)),
    ("back_transition_alt_loss_m_<n>", "m", "Height lost during it; reported from 5 m up.", "a quadplane",
     ("quadplane",)),
    ("airspeed_min_mps", "m/s", "Lowest airspeed in wing-borne flight above 20 m (stall margin).",
     "a takeoff step", ("takeoff",)),
    ("airspeed_max_mps", "m/s", "Highest airspeed in wing-borne flight above 20 m (overspeed).",
     "a takeoff step", ("takeoff",)),
    ("failsafe_reaction_s", "s", "From the first set_param step until the mode changes; inf when it never does.",
     "a set_param step", ("set_param",)),
    ("failsafe_mode", "number", "The mode number it changed to (RTL is 11, QRTL 21).", "a set_param step",
     ("set_param",)),
    ("mode_speed_mps_<mode>", "m/s", "Mean ground speed of a stretch in a mode you switched to; from 0.5 up.",
     "a segment of 5 s or more", ()),
    ("mode_yaw_rate_dps_<mode>", "deg/s", "Mean absolute yaw (turn) rate of the same stretch; from 2 up.",
     "a segment of 5 s or more", ()),
    ("alt_max_m", "m", "Highest altitude above home.", "always", ()),
    ("range_max_m", "m", "Furthest horizontal distance from home.", "always", ()),
    ("flight_time_s", "s", "From arming to disarming.", "takeoff and wait_disarm", ("takeoff", "disarm")),
    ("landing_offset_m", "m", "Horizontal distance from home at the end; from 1 m up.", "takeoff and wait_disarm",
     ("takeoff", "disarm")),
    ("disarm_delay_s", "s", "From touchdown until disarm.", "takeoff and wait_disarm", ("takeoff", "disarm")),
    ("touchdown_gs_mps", "m/s", "Ground speed at touchdown.", "takeoff and wait_disarm", ("takeoff", "disarm")),
    ("rollout_m", "m", "Ground roll after touchdown.", "takeoff and wait_disarm", ("takeoff", "disarm")),
    ("touchdown_speed_mps", "m/s", "Fastest descent in the last 2 m before touchdown.", "takeoff and wait_disarm",
     ("takeoff", "disarm")),
    ("touchdown_err_m", "m", "Touchdown point against the mission's land point; from 1 m up.",
     "a land or vtol_land item, takeoff and wait_disarm", ("takeoff", "disarm", "land")),
    ("touchdown_along_m", "m", "The same along the approach leg (negative: short).",
     "a fixed-wing land item after a waypoint", ("takeoff", "disarm", "land")),
    ("touchdown_cross_m", "m", "The same across the approach leg.", "a fixed-wing land item after a waypoint",
     ("takeoff", "disarm", "land")),
]
_NEED = {"takeoff": "a takeoff step", "disarm": "a wait_disarm step", "mission": "a mission step",
         "set_param": "a set_param step", "hold": "a hold step", "quadplane": "frame: quadplane",
         "land": "a land or vtol_land mission item"}


def _plane_have(steps, frame) -> dict:
    names = {n for n, _ in steps}
    items = [i for n, a in steps if n == "mission" for i in a]
    return {"takeoff": "takeoff" in names, "disarm": "wait_disarm" in names, "mission": "mission" in names,
            "set_param": "set_param" in names, "hold": "hold" in names, "quadplane": frame == "quadplane",
            "land": any(isinstance(i, dict) and set(i) & {"land", "vtol_land"} for i in items)}


def _plane_metric_problem(name, have):
    for pattern, *_, needs in PLANE_METRICS:
        stem = re.sub(r"_<\w+>$", "", pattern)
        if pattern.endswith("_<mode>"):
            if not name.startswith(stem + "_"):
                continue
            base = re.sub(r"\d+$", "", name[len(stem) + 1:]).upper()
            if base not in plane_modes():
                return "error", f"metric '{name}': '{base.lower()}' is not a Plane flight mode name"
        elif pattern.endswith("_<n>"):
            if not re.fullmatch(re.escape(stem) + r"_\d+", name):
                continue
        elif name != pattern:
            continue
        missing = [_NEED[n] for n in needs if not have[n]]
        return ("error", f"metric '{name}' is never produced here: it needs {' and '.join(missing)}") \
            if missing else None
    return "error", _unknown("metric", name, [m[0] for m in PLANE_METRICS], list_all=False) + \
        "; list them with `forkpilot lint --reference`"


# --- PX4 (`autopilot: px4`) ---------------------------------------------------------------------

def px4_step_names() -> list[str]:
    from .px4_runner import Px4Runner
    return sorted(n[5:] for n in dir(Px4Runner) if n.startswith("step_") and n[5:] not in PX4_UNSUPPORTED)


def px4_modes() -> list[str]:
    from .px4_sitl import MODES
    return sorted(MODES)


def _px4_key(mode: str) -> str:
    return mode.lower().replace(".", "_")


PX4_ITEMS = {"takeoff": "height in m", "wp": "[north, east, up]", "land": "[north, east]",
             "loiter_time": "{at: [north, east, up], seconds}", "speed": "ground speed in m/s", "rtl": "true"}


def _px4_item(ctx, item, line, i):
    if isinstance(item, list):
        _point(ctx, item, line, f"waypoint {i}")
        return
    if not isinstance(item, dict) or len(item) != 1:
        ctx.err(line, f"mission item {i} must be [north, east, up] or one `kind: argument`, got {_show(item)}")
        return
    (kind, arg), = item.items()
    ln, what = item.val_line[kind], f"mission item {i} ({kind})"
    if kind not in PX4_ITEMS:
        ctx.err(item.key_line[kind], _unknown("mission item", kind, PX4_ITEMS))
    elif kind in ("takeoff", "speed"):
        _num(ctx, arg, ln, f"{what}", 0, None, True)
    elif kind == "wp":
        _point(ctx, arg, ln, what)
    elif kind == "land":
        if not isinstance(arg, list) or len(arg) not in (2, 3):
            ctx.err(ln, f"{what} must be [north, east] in metres, got {_show(arg)}")
        else:
            for x, axis in zip(arg, ("north", "east")):
                _num(ctx, x, ln, f"{what} {axis}")
    elif kind == "loiter_time":
        if not isinstance(arg, dict) or set(arg) != {"at", "seconds"}:
            ctx.err(ln, f"{what} must be a mapping like {{at: [0, 0, 20], seconds: 10}}, got {_show(arg)}")
            return
        _point(ctx, arg["at"], arg.val_line["at"], f"{what} at")
        _num(ctx, arg["seconds"], arg.val_line["seconds"], f"{what} seconds", 0)
    elif arg is not True:
        ctx.err(ln, f"{what} takes `true`, got {_show(arg)}")


def _px4_mission(ctx, v, line):
    if not isinstance(v, list) or not v:
        ctx.err(line, f"mission must be a non-empty list of mission items, got {_show(v)}")
        return
    for i, (item, ln) in enumerate(zip(v, getattr(v, "item_line", [line] * len(v))), 1):
        _px4_item(ctx, item, ln, i)


def _flag(what):
    def check(ctx, v, line):
        if not isinstance(v, bool):
            ctx.err(line, f"{what} takes true or false, got {_show(v)}")
    return check


PX4_UNSUPPORTED = {"yaw"}       # Runner steps PX4 has no command for
PX4_STEPS = {**{n: st for n, st in STEPS.items() if n not in PX4_UNSUPPORTED},
             "takeoff": Step("number, metres", "takeoff: 10",
                             "Arm and take off (MAV_CMD_NAV_TAKEOFF, AUTO.TAKEOFF); waits until past 90% of the "
                             "height with the climb ended. PX4 then holds in AUTO.LOITER.", _positive("takeoff height in metres")),
             "goto": Step("[north, east, up], metres from home", "goto: [30, 0, 15]",
                          "MAV_CMD_DO_REPOSITION (switches to AUTO.LOITER); waits until within 1 m (90 s limit).",
                          _point),
             "send_goto": Step("[north, east, up], metres from home", "send_goto: [150, 0, 10]",
                               "Like goto but does not wait.", _point),
             "velocity": Step("{vx, vy, vz, frame, seconds}", "velocity: {vx: 3, frame: body, seconds: 8}",
                              "OFFBOARD velocity setpoints (m/s; vz is down), streamed at 10 Hz, then back to "
                              "AUTO.LOITER; frame local (north/east) or body.", _velocity),
             "mode": Step("PX4 mode name", "mode: POSCTL",
                          "Switch flight mode (main.sub as PX4 names them) and wait for it. A pilot mode "
                          "(POSCTL, ALTCTL, ...) starts centred MANUAL_CONTROL first.", _mode),
             "mission": Step("list of mission items", "mission: [[40, 0, 20], [40, 40, 20]]",
                             "Upload these items: [north, east, up] or one of "
                             + ", ".join(f"`{k}` ({v})" for k, v in PX4_ITEMS.items())
                             + ". A list of only waypoints gets a final RTL. Start it with "
                             "`mode: AUTO.MISSION`.", _px4_mission),
             "sticks": Step("{roll, pitch, throttle, yaw: PWM 1000-2000}", "sticks: {pitch: 1300}",
                            "Pilot input as MANUAL_CONTROL (PWM as an RC transmitter: pitch low is forward, "
                            "throttle 1500 holds height), re-sent at 20 Hz until changed.", _sticks),
             "set_param": Step("{NAME: number}", "set_param: {SIM_BAT_MIN_PCT: 6}",
                               "Set parameters in flight; a fault injected this way starts failsafe_reaction_s.",
                               _set_param),
             "rc_loss": Step("true or false", "rc_loss: true",
                             "Stop (true) or resume (false) the MANUAL_CONTROL stream: a manual control loss.",
                             _flag("rc_loss")),
             "link_loss": Step("true or false", "link_loss: true",
                               "Stop (true) or resume (false) everything the ground station sends, heartbeat "
                               "included: a data link loss.", _flag("link_loss"))}

# name, unit, meaning, produced when, what the steps must have
PX4_METRICS = [
    ("completed", "0/1", "1 when every step ran to the end; below 1 is always a FAIL.", "always", ()),
    ("takeoff_time_s", "s", "From arming until the climb ends past 90% of the takeoff height.", "a takeoff step",
     ("takeoff",)),
    ("hold_drift_max_m", "m", "Largest horizontal drift from where a hold started.", "a hold step", ("hold",)),
    ("hold_alt_err_max_m", "m", "Largest height change during a hold.", "a hold step", ("hold",)),
    ("hold_tilt_rms_deg", "deg", "RMS tilt during holds.", "a hold step", ("hold",)),
    ("hold_thrust", "0-1", "Mean collective thrust setpoint during holds (hover thrust).", "a hold step",
     ("hold",)),
    ("goto_time_total_s", "s", "Sum over goto steps of the time from command to arrival.", "a goto step",
     ("goto",)),
    ("goto_overshoot_max_m", "m", "How far past the target, along the leg, the vehicle went.", "a goto step",
     ("goto",)),
    ("failsafe_reaction_s", "s", "From the first fault (set_param, rc_loss, link_loss) until the mode changes; "
     "inf when it never does.", "a fault step", ("fault",)),
    ("failsafe_mode", "main.sub", "The failsafe action as PX4 mode number (AUTO.RTL 4.5, AUTO.LAND 4.6), "
     "after the Hold PX4 waits in first.", "a fault step", ("fault",)),
    ("mission_wp_reached", "count", "Distinct mission items reported reached.", "a mission step", ("mission",)),
    ("mission_time_s", "s", "From AUTO.MISSION until disarm.", "a mission flown to the landing",
     ("mission", "disarm")),
    ("mission_speed_max_mps", "m/s", "Highest ground speed from AUTO.MISSION until disarm.",
     "a mission flown to the landing", ("mission", "disarm")),
    ("mission_xtrack_max_m", "m", "Largest distance off the line between consecutive waypoints while that "
     "leg was current.", "a mission with legs of 5 m or more", ("mission",)),
    ("rtl_alt_max_m", "m", "Highest altitude in the first AUTO.RTL.", "a Return, commanded or failsafe", ()),
    ("rtl_time_s", "s", "From entering AUTO.RTL until disarm.", "a Return and wait_disarm", ("disarm",)),
    ("stop_dist_m_<mode>", "m", "After a release: how far the vehicle coasts.", "a release step", ("release",)),
    ("stop_backtrack_m_<mode>", "m", "After a release: how far it comes back after its furthest point.",
     "a release step", ("release",)),
    ("stop_time_s_<mode>", "s", "After a release: until under 0.3 m/s.", "a release step", ("release",)),
    ("stop_alt_dev_m_<mode>", "m", "After a release: largest height change.", "a release step", ("release",)),
    ("stick_speed_mps_<mode>", "m/s", "Ground speed at the release.", "a release step", ("release",)),
    ("mode_speed_mps_<mode>", "m/s", "Mean ground speed of a stretch in a mode you switched to.",
     "a segment of 5 s or more", ()),
    ("mode_yaw_rate_dps_<mode>", "deg/s", "Mean absolute yaw rate of the same stretch.",
     "a segment of 5 s or more", ()),
    ("vel_err_mps_<n>", "m/s", "n-th velocity step: commanded against flown mean velocity after 3 s.",
     "a velocity step longer than 3 s", ("velocity",)),
    ("att_err_rms_deg", "deg", "RMS roll/pitch error against the attitude setpoint while in the air.",
     "a takeoff step", ("takeoff",)),
    ("alt_max_m", "m", "Highest altitude above home.", "always", ()),
    ("range_max_m", "m", "Furthest horizontal distance from home.", "always", ()),
    ("flight_time_s", "s", "From arming to disarming.", "takeoff and wait_disarm", ("takeoff", "disarm")),
    ("landing_offset_m", "m", "Horizontal distance from home at the end.", "takeoff and wait_disarm",
     ("takeoff", "disarm")),
    ("disarm_delay_s", "s", "From touchdown until disarm.", "takeoff and wait_disarm", ("takeoff", "disarm")),
    ("touchdown_speed_mps", "m/s", "Fastest descent in the last 2 m before touchdown.", "takeoff and wait_disarm",
     ("takeoff", "disarm")),
    ("land_drift_m", "m", "Horizontal distance moved between entering AUTO.LAND and the touchdown.",
     "`mode: AUTO.LAND` and wait_disarm", ("disarm",)),
]
_PX4_NEED = {"takeoff": "a takeoff step", "disarm": "a wait_disarm step", "mission": "a mission step",
             "fault": "a set_param, rc_loss or link_loss step", "hold": "a hold step", "goto": "a goto step",
             "release": "a release step", "velocity": "a velocity step"}


def _px4_have(steps) -> dict:
    names = {n for n, _ in steps}
    return {"takeoff": "takeoff" in names, "disarm": "wait_disarm" in names, "mission": "mission" in names,
            "fault": bool(names & {"set_param", "rc_loss", "link_loss"}), "hold": "hold" in names,
            "goto": "goto" in names, "release": "release" in names, "velocity": "velocity" in names}


def _px4_metric_problem(name, have):
    keys = {_px4_key(m) for m in px4_modes()}
    for pattern, *_, needs in PX4_METRICS:
        stem = re.sub(r"_<\w+>$", "", pattern)
        if pattern.endswith("_<mode>"):
            if not name.startswith(stem + "_"):
                continue
            base = re.sub(r"\d+$", "", name[len(stem) + 1:])
            if base not in keys:
                return "error", (f"metric '{name}': '{base}' is not a PX4 mode in metric form "
                                 "(lower case, dot as underscore: posctl, auto_loiter)")
        elif pattern.endswith("_<n>"):
            if not re.fullmatch(re.escape(stem) + r"_\d+", name):
                continue
        elif name != pattern:
            continue
        missing = [_PX4_NEED[n] for n in needs if not have[n]]
        return ("error", f"metric '{name}' is never produced here: it needs {' and '.join(missing)}") \
            if missing else None
    return "error", _unknown("metric", name, [m[0] for m in PX4_METRICS], list_all=False) + \
        "; list them with `forkpilot lint --reference`"


# --- sets of scenarios -------------------------------------------------------------------------


def as_dirs(dirs=None) -> list[Path]:
    """The directories named, or by default the shipped scenarios plus $FP_HOME/scenarios (the
    company's own, when that directory exists and is not the shipped one)."""
    if dirs:
        return [Path(d) for d in dirs]
    own = WORK / "scenarios"
    return [SCENARIOS, own] if own != SCENARIOS and own.is_dir() else [SCENARIOS]


def collect(dirs=None) -> dict[str, Path]:
    """{name: file} over the directories; the same name in two of them is an error."""
    out: dict[str, Path] = {}
    for d in as_dirs(dirs):
        if not d.is_dir():
            raise ScenarioSetError(f"scenario directory not found: {d}")
        for p in sorted(d.glob("*.yaml")):
            if p.stem in out:
                raise ScenarioSetError(f"scenario name '{p.stem}' is in two directories: "
                                       f"{out[p.stem]} and {p}")
            out[p.stem] = p
    return dict(sorted(out.items()))


def paths(only=None, dirs=None) -> list[Path]:
    """Scenario files in name order, or in the order of `only`."""
    col = collect(dirs)
    if not only:
        return [col[n] for n in sorted(col)]
    for n in only:
        if n not in col:
            raise ScenarioSetError(_unknown("scenario", n, col))
    return [col[n] for n in only]


def lint_paths(targets=None) -> list[Issue]:
    """Lint files and directories (every *.yaml in them); default: the shipped scenarios."""
    files, issues, names = [], [], {}
    for t in map(Path, targets or as_dirs()):
        if t.is_dir():
            found = sorted(t.glob("*.yaml"))
            if not found:
                issues.append(Issue(str(t), 1, "error", "no *.yaml scenarios in this directory"))
            for other in sorted(set(t.glob("*.yml")) | set(t.glob("*.YAML"))):
                issues.append(Issue(str(other), 1, "warning", "ignored: only *.yaml files are flown"))
            files += found
        elif t.exists():
            files.append(t)
        else:
            issues.append(Issue(str(t), 1, "error", "no such file or directory"))
    for f in files:
        if f.stem in names and names[f.stem] != f:
            issues.append(Issue(str(f), 1, "error", f"scenario name '{f.stem}' is also used by {names[f.stem]}"))
        names.setdefault(f.stem, f)
        issues += lint_file(f)
    return issues


def check(dirs=None, only=None, log=print) -> None:
    """Validate what a run is about to fly; print warnings, raise ScenarioSetError on any error."""
    col = collect(dirs)
    for n in only or ():
        if n not in col:
            raise ScenarioSetError(_unknown("scenario", n, col))
    issues = [i for n in (only or sorted(col)) for i in lint_file(col[n])]
    for i in issues:
        if i.level == "warning" and log:
            log(str(i))
    errors = [str(i) for i in issues if i.level == "error"]
    if errors:
        raise ScenarioSetError("invalid scenarios, nothing was flown:\n" + "\n".join(errors))


# --- reference ---------------------------------------------------------------------------------------

def reference() -> str:
    """Markdown tables of the steps and metrics, generated from the tables above."""
    out = ["### Steps", "", "| Step | Argument | Example | What it does |", "|---|---|---|---|"]
    out += [f"| `{n}` | {STEPS[n].arg} | `{STEPS[n].example}` | {STEPS[n].desc} |"
            for n in step_names() if n in STEPS]
    out += ["", "### Metrics", "", "| Metric | Unit | Meaning | Produced when |", "|---|---|---|---|"]
    out += [f"| `{n}` | {u} | {d} | {w} |" for n, u, d, w in METRICS]
    out += ["", "### Operators", "",
            "| Operator | Meaning |", "|---|---|"]
    out += [f"| `{op}` | metric {sym} bound |" for op, sym in zip(oracle.OPS, ("<", "<=", ">", ">="))]
    out += ["", "### Flight modes", "", ", ".join(f"`{m}`" for m in modes())]
    out += ["", "### Plane and QuadPlane (`vehicle: plane`)", "",
            "Steps as above except the Copter GUIDED commands (" + ", ".join(sorted(COPTER_ONLY)) + "); these differ "
            "or are added:", "", "| Step | Argument | Example | What it does |", "|---|---|---|---|"]
    out += [f"| `{n}` | {st.arg} | `{st.example}` | {st.desc} |" for n, st in PLANE_STEPS.items()
            if STEPS.get(n) is not st]
    out += ["", "| Metric | Unit | Meaning | Produced when |", "|---|---|---|---|"]
    out += [f"| `{n}` | {u} | {d} | {w} |" for n, u, d, w, _ in PLANE_METRICS]
    out += ["", "Plane flight modes: " + ", ".join(f"`{m}`" for m in plane_modes())]
    out += ["", "### PX4 multicopter (`autopilot: px4`)", "",
            "Steps as above except " + ", ".join(sorted(PX4_UNSUPPORTED)) + "; these differ or are added:", "",
            "| Step | Argument | Example | What it does |", "|---|---|---|---|"]
    out += [f"| `{n}` | {st.arg} | `{st.example}` | {st.desc} |" for n, st in PX4_STEPS.items()
            if STEPS.get(n) is not st]
    out += ["", "| Metric | Unit | Meaning | Produced when |", "|---|---|---|---|"]
    out += [f"| `{n}` | {u} | {d} | {w} |" for n, u, d, w, _ in PX4_METRICS]
    out += ["", "PX4 flight modes: " + ", ".join(f"`{m}`" for m in px4_modes()),
            "", "In metric names a PX4 mode is lower case with the dot as underscore: `stop_dist_m_posctl`, "
            "`mode_speed_mps_auto_loiter`."]
    return "\n".join(out)
