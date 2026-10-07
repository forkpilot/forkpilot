"""Targeted extra flights: scenarios chosen from the impact of a commit range.

  forkpilot investigate --targeted ...   flies the full set plus these
  forkpilot impact --plan ...            prints what --targeted would add, flies nothing

Rule-based, no LLM. From `impact.impact()` of the whole good..bad range:

  parameter variants  every affected parameter with a bitmask gets one variant per bit (that bit
                      flipped from the default), one with @Values one per value other than the
                      default; each is a copy of a scenario that flies the modes using the changed
                      code directly (else the vehicle's basic scenario) with the parameter in params:
  mode templates      an affected mode no scenario flies, if it is on the vehicle's allow-list of
                      pilot-flown modes a generic stick template can fly in SITL, gets that template

Targeting only adds: the normal set is still flown. At most MAX_TARGETED scenarios, in a fixed
order (templates of directly affected modes, vehicle parameters, library parameters, then
templates of modes affected only through core code). Not varied: @Range parameters (v2), and new
parameters: the good commit does not have them, SITL refuses to set them there, and a baseline
that cannot be flown makes the variant useless (setting a parameter only where it exists would
need a harness change, which invalidates every cached baseline). The generated files go to $FP_HOME/targeted/<hash of their contents>/.
"""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path

import yaml

from .config import WORK
from .i18n import t

MAX_TARGETED = 12
MAX_BASES = 2          # scenarios a parameter is varied on
SEP = "__"             # <scenario>__<PARAM>_<value>, <vehicle>_sticks__<MODE>: no shipped name has it
TARGETED = WORK / "targeted"
BASIC = {"copter": "hover", "plane": "plane_modes"}
DISABLED = re.compile(r"^(disabl|off\b|none\b|no\b|never\b)", re.I)

# Pilot-flown modes a stick template flies in SITL with no extra hardware (flow sensor, throw,
# rangefinder, ...). Other modes are reported as not flown.
TEMPLATE_MODES = {
    "copter": ("ACRO", "DRIFT", "SPORT", "STABILIZE", "ZIGZAG"),
    "plane": ("ACRO", "CRUISE", "FBWB", "STABILIZE", "TRAINING"),
}
# rate modes: a held stick keeps rotating, so they get a shorter, smaller input
RATE_MODES = {"ACRO"}
# Copter modes where the throttle stick is the throttle: centred is a little above hover in SITL
# (1500 climbed 1.5 m/s in STABILIZE), so these get a slightly lower stick
MANUAL_THROTTLE = {"ACRO": 1480, "STABILIZE": 1480}


def template_name(vehicle: str, mode: str) -> str:
    return f"{vehicle}_sticks{SEP}{mode}"


def copter_template(mode: str) -> dict:
    """Take off, enter the mode with the throttle centred (hover in the manual-throttle modes),
    then pitch, roll, and pitch with yaw, each held and released, then land."""
    hold, push = (2, 75) if mode in RATE_MODES else (4, 150)
    steps = [{"takeoff": 15}, {"sticks": {"throttle": MANUAL_THROTTLE.get(mode, 1500)}}, {"mode": mode}, {"fly": 3},
             {"sticks": {"pitch": 1500 - push}}, {"fly": hold}, {"release": 10},
             {"sticks": {"roll": 1500 + push}}, {"fly": hold}, {"release": 10},
             {"sticks": {"pitch": 1500 - push, "yaw": 1500 + push}}, {"fly": hold}, {"release": 10},
             {"mode": "LAND"}, {"wait_disarm": 120}]
    return {"params": {}, "steps": steps}


def plane_template(mode: str) -> dict:
    """Airborne as plane_modes gets there (AUTO takeoff), then the mode: roll stick past the roll
    limit, release, pitch stick, release, then RTL."""
    roll, roll_s = (1650, 1) if mode in RATE_MODES else (2000, 6)
    pitch, pitch_s = (1600, 1) if mode in RATE_MODES else (1650, 4)
    steps = [{"mission": [{"takeoff": 50}, {"wp": [800, 0, 60]}]}, {"takeoff": 50},
             {"sticks": {"throttle": 1500}}, {"mode": mode}, {"fly": 10},
             {"sticks": {"roll": roll}}, {"fly": roll_s}, {"release": 10},
             {"sticks": {"pitch": pitch}}, {"fly": pitch_s}, {"release": 10},
             {"mode": "RTL"}, {"fly": 60}]
    return {"vehicle": "plane", "params": {}, "steps": steps}


TEMPLATES = {"copter": copter_template, "plane": plane_template}


# --- what to add ----------------------------------------------------------------------------------

def _int_default(p: dict):
    d = p.get("default")
    return int(d) if isinstance(d, (int, float)) and float(d).is_integer() else None


def param_values(p: dict) -> tuple[list[dict], str | None]:
    """([{"value", "bit"?, "on"?, "label"}], None) or ([], why it is not varied)."""
    if not p.get("name"):
        return [], "prefix"
    if p["status"] in ("removed", "new"):
        return [], p["status"]
    doc, d = p.get("doc") or {}, p.get("default")
    out = []
    if doc.get("bitmask"):
        base = _int_default(p)
        for bit, label in doc["bitmask"]:
            if not 0 <= bit < 31:
                continue
            on = not (base is not None and base & (1 << bit))
            value = (1 << bit) if base is None else base ^ (1 << bit)
            out.append({"value": value, "bit": bit, "on": on, "label": label})
    elif doc.get("values"):
        for value, label in doc["values"]:
            if (d is not None and value == d) or (d is None and value == 0 and DISABLED.search(label)):
                continue
            out.append({"value": value, "label": label})
    elif doc.get("range"):
        return [], "range"
    else:
        return [], "nodoc"
    return (out, None) if out else ([], "default_only")


def _value_tag(v) -> str:
    return str(v).replace("-", "m").replace(".", "p")


def bases(direct: set[str], facts: list[dict], vehicle: str, ran: dict | None = None) -> list[str]:
    """Scenarios a parameter is varied on: with `ran` ({scenario: changed lines it executed that
    not every scenario executed}, from a coverage build) the ones that ran the most of them, filled
    up with the ones flying the most directly affected modes."""
    names = {f["name"] for f in facts}
    picked = []
    if ran:
        measured = [(n, k) for k, n in ran.items() if n and k in names]
        picked = [k for _, k in sorted(measured, key=lambda h: (-h[0], h[1]))][:MAX_BASES]
        if len(picked) == MAX_BASES:
            return picked
    # fill up from the modes
    return picked + [n for n in _mode_bases(direct, facts, vehicle) if n not in picked][:MAX_BASES - len(picked)]


def _mode_bases(direct: set[str], facts: list[dict], vehicle: str) -> list[str]:
    hits = [(len(f["modes"] & direct), f["name"]) for f in facts if f["modes"] & direct]
    picked = [n for _, n in sorted(hits, key=lambda h: (-h[0], h[1]))][:MAX_BASES]
    if picked:
        return picked
    names = sorted(f["name"] for f in facts)
    return [BASIC[vehicle]] if BASIC.get(vehicle) in names else names[:1]


def plan(report: dict, vehicle: str, dirs=None, facts=None, paths=None, cap: int = MAX_TARGETED,
         ran: dict | None = None) -> dict:
    """What --targeted adds for an `impact.impact()` report. `facts`/`paths` (scenario facts and
    {name: file}) default to the scenarios of `dirs`. `ran`: changed lines each scenario executed
    (coverage), to pick the scenarios parameters are varied on. Items under "add" carry their YAML
    in "spec"."""
    from . import battery, impact
    if paths is None:
        paths = {p.stem: p for p in battery.scenario_paths(dirs=dirs, vehicle=vehicle)}
    if facts is None:
        facts = [impact.scenario_facts(p, vehicle) for p in paths.values()]
    r, c = report["impact"], report["coverage"]
    direct = set(r["modes"])
    allowed = TEMPLATE_MODES.get(vehicle, ())
    cands, not_varied = [], []

    for m in c["modes_not_flown"]:
        if m in allowed:
            spec = {"name": template_name(vehicle, m), "description": f"targeted: {m} template", **TEMPLATES[vehicle](m)}
            cands.append(((0 if m in direct else 4, 0, m), {
                "name": spec["name"], "kind": "mode", "mode": m, "direct": m in direct, "spec": spec}))
    no_template = [m for m in c["modes_not_flown"] if m not in allowed]

    on = bases(direct, facts, vehicle, ran)
    by_coverage = [b for b in on if ran and ran.get(b)]
    for p in r["params"]:
        values, why = param_values(p)
        if why:
            not_varied.append({"param": p["name"] or "?" + p["key"], "why": why})
            continue
        group = 1 if p["scope"] == "vehicle" else 2
        combos = [(v, b) for v in values for b in on if b in paths]
        for i, (v, b) in enumerate(combos):
            base = yaml.safe_load(Path(paths[b]).read_text())
            name = f"{b}{SEP}{p['name']}_{_value_tag(v['value'])}"
            rest = {k: x for k, x in copy.deepcopy(base).items() if k not in ("name", "description", "params")}
            spec = {"name": name, "description": f"targeted: {b} with {p['name']}={v['value']}",
                    **{k: rest.pop(k) for k in ("vehicle", "autopilot", "frame") if k in rest},
                    "params": {**(base.get("params") or {}), p["name"]: v["value"]}, **rest}
            cands.append(((group, i, p["name"]), {
                "name": name, "kind": "param", "param": p["name"], "status": p["status"], "base": b,
                "default": p.get("default"), **v, "spec": spec}))

    cands.sort(key=lambda x: (x[0], x[1]["name"]))
    seen, ordered = set(), []
    for _, item in cands:
        if item["name"] not in seen:
            seen.add(item["name"])
            ordered.append(item)
    return {"vehicle": vehicle, "cap": cap, "add": ordered[:cap],
            "dropped": [i["name"] for i in ordered[cap:]],
            "not_varied": not_varied, "no_template": no_template,
            **({"bases_by_coverage": by_coverage} if by_coverage else {})}


def summary(p: dict) -> dict:
    """The plan without the YAML, for the record and --json."""
    return {**p, "add": [{k: v for k, v in i.items() if k != "spec"} for i in p["add"]]}


def _flow(x) -> str:
    text = yaml.safe_dump(x, default_flow_style=True, width=1 << 16).strip()
    return text[:-3].strip() if text.endswith("\n...") else text


def yaml_text(spec: dict) -> str:
    """The scenario as YAML in the shipped files' style: one `- step: argument` line per step."""
    out = []
    for k, v in spec.items():
        if k == "steps":
            out.append("steps:")
            out += [f"  - {name}: {_flow(arg)}" for step in v for name, arg in step.items()]
        else:
            out.append(f"{k}: {_flow(v)}")
    return "\n".join(out) + "\n"


def write(p: dict, root: Path | None = None) -> Path | None:
    """Write the plan's scenarios to <root>/<hash of names and contents>/; None if it adds nothing.
    The same set always lands in the same directory, so a baseline cached for it is found again."""
    files = {i["name"] + ".yaml": yaml_text(i["spec"]) for i in p["add"]}
    if not files:
        return None
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + files[name].encode() + b"\0")
    d = Path(root or TARGETED) / h.hexdigest()[:12]
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        if not (d / name).exists() or (d / name).read_text() != text:
            (d / name).write_text(text)
    return d


# --- text ------------------------------------------------------------------------------------------

def reason(item: dict) -> str:
    if item["kind"] == "mode":
        return t("tg.reason.mode" if item["direct"] else "tg.reason.mode_core", mode=item["mode"])
    if "bit" in item:
        what = t("tg.bit_on" if item["on"] else "tg.bit_off", bit=item["bit"], label=item["label"])
    else:
        what = item["label"]
    return t("tg.reason.param", param=item["param"], value=item["value"], what=what, base=item["base"])


def lines(p: dict) -> list[str]:
    out = [t("tg.title", n=len(p["add"]), cap=p["cap"])]
    out += [f"  {i['name']}: {reason(i)}" for i in p["add"]] or ["  " + t("impact.none")]
    if p["dropped"]:
        out.append(t("tg.dropped", cap=p["cap"], names=", ".join(p["dropped"])))
    if p["not_varied"]:
        out.append(t("tg.not_varied", names=", ".join(f"{x['param']} ({t('tg.why.' + x['why'])})"
                                                      for x in p["not_varied"])))
    if p["no_template"]:
        out.append(t("tg.no_template", names=", ".join(p["no_template"])))
    if p.get("bases_by_coverage"):
        out.append(t("tg.bases_by_coverage", names=", ".join(p["bases_by_coverage"])))
    return out


def markdown(p: dict) -> list[str]:
    """Evidence section: what --targeted added and why."""
    return [t("tg.section"), "", "```", *lines(p), "```", ""]


# --- investigate ---------------------------------------------------------------------------------

def prepare(repo, good: str, bad: str, vehicle: str, dirs, root: Path | None = None,
            ran: dict | None = None):
    """(plan, directory or None) for `investigate --targeted`: impact of the whole range, the plan
    against the scenarios of `dirs`, and the files written."""
    from . import impact
    report = impact.impact(repo, good, bad, vehicle, facts=impact.scenario_facts_for(vehicle, dirs))
    p = plan(report, vehicle, dirs, ran=ran)
    return p, write(p, root)
