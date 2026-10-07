"""`forkpilot report <investigation>`: one self-contained report.html for an engineering manager.

Everything is inline (CSS, SVG): no script, no font, no request of any kind, so the file opens on
an air-gapped network and can be mailed as is. Every section is built from files the
investigation may or may not have; a missing file drops its section and never fails the report.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from html import escape
from pathlib import Path

from . import plots
from .i18n import MSG, lang as current_lang, t
from .plots import BandRow, fmt, metric_files, run_files, unit_of

MAX_PLOTTED = 2          # scenarios with telemetry plots
MAX_BAND_ROWS = 16
MAX_FINDINGS = 20        # per scenario in the verdict table
MAX_DIFF_LINES = 600
COVERAGE_ROWS = 30
MAX_ROWS_PASS = 12       # PASS scenarios listed as rows; more go into one line
VERDICT_CLASS = {"PASS": "pass", "DRIFT": "drift", "FAIL": "fail"}
OUTCOMES = ("localized", "no_regression", "not_reproducible", "build_failed")
MISSING = tuple(m["oracle.missing"] for m in MSG.values())    # oracle text for a metric the run lacks
FINDING = re.compile(r"^(?P<kind>[A-Za-z]+)\s+(?P<metric>.+?): (?P<value>\S+) "
                     r"\((?:beklenen|expected) (?P<exp>.*)\)$")


class ReportError(Exception):
    pass


CSS = """
:root{color-scheme:light dark;--bg:#fafaf8;--fg:#1d1f23;--muted:#596069;--card:#fff;--line:#d9dce1;
--good:#1f6fd1;--bad:#d4501a;--band:#dcebd8;--shade:#e9ecf1;--pass:#17712f;--drift:#8a5a00;--fail:#c4222f;
--warn-bg:#fff3d6;--code:#f2f3f5;--add:#e3f4e6;--del:#fde8e8}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#14161a;--fg:#e6e8eb;--muted:#a0a7b0;
--card:#1c1f24;--line:#363c45;--good:#6aaeff;--bad:#ff9468;--band:#25402c;--shade:#252a32;--pass:#5fd07c;
--drift:#e8b34a;--fail:#ff7b84;--warn-bg:#3b3118;--code:#242830;--add:#1b3524;--del:#432125}}
:root[data-theme=dark]{--bg:#14161a;--fg:#e6e8eb;--muted:#a0a7b0;--card:#1c1f24;--line:#363c45;
--good:#6aaeff;--bad:#ff9468;--band:#25402c;--shade:#252a32;--pass:#5fd07c;--drift:#e8b34a;--fail:#ff7b84;
--warn-bg:#3b3118;--code:#242830;--add:#1b3524;--del:#432125}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,
"Helvetica Neue",Arial,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 40px}
h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.15rem;margin:32px 0 10px;padding-top:8px;border-top:1px solid var(--line)}
h3{font-size:1rem;margin:20px 0 6px}
.sub,.muted{color:var(--muted)}.sub{margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:10px 0}
.verdict{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.verdict .big{font-size:1.25rem;font-weight:650}
.badge{display:inline-block;border:1px solid currentColor;border-radius:4px;padding:0 6px;font-size:.8rem;
font-weight:600;white-space:nowrap}
.pass{color:var(--pass)}.drift{color:var(--drift)}.fail{color:var(--fail)}.warn{color:var(--drift)}
dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}
dl.kv dt{color:var(--muted)}dl.kv dd{margin:0;overflow-wrap:anywhere}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;white-space:nowrap}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
code,pre{font:.85rem/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
code{background:var(--code);padding:0 4px;border-radius:3px}
pre{background:var(--code);padding:10px 12px;border-radius:6px;overflow-x:auto;margin:8px 0}
pre.diff{padding:8px 0}pre.diff span{display:block;padding:0 12px;white-space:pre}
.ad{background:var(--add)}.rm{background:var(--del)}.hh{color:var(--muted)}
details{margin:8px 0}summary{cursor:pointer;color:var(--muted)}
svg{width:100%;height:auto;display:block}
svg text{font:11px system-ui,sans-serif;fill:var(--muted)}
svg .ylab{font-size:12px}svg .mlab{font-size:12px;fill:var(--fg)}svg .mark-t{font-size:10.5px;fill:var(--fg)}
svg .frame{fill:none;stroke:var(--line)}svg .grid{stroke:var(--line);stroke-width:.6}
svg .axis{stroke:var(--muted)}svg .shade{fill:var(--shade)}
svg .mark{stroke:var(--muted);stroke-dasharray:3 3;stroke-width:1}
svg .ref{stroke:var(--fg);stroke-width:1.2;stroke-dasharray:5 3}
svg .band-good{fill:var(--good);opacity:.2}svg .line-good{fill:none;stroke:var(--good);stroke-width:1;opacity:.55}
svg .line-bad{fill:none;stroke:var(--bad);stroke-width:1.5;opacity:.85}
svg .band{fill:var(--band)}svg .dot-good{fill:var(--good)}svg .dot-bad{fill:var(--bad)}
.legend{margin:2px 0 14px;color:var(--muted);font-size:.85rem}
.key{display:inline-block;width:22px;height:0;border-top:3px solid;vertical-align:middle;margin:0 4px 0 12px}
.key-good{border-color:var(--good)}.key-bad{border-color:var(--bad)}
.note{background:var(--warn-bg);border-radius:6px;padding:8px 12px;margin:10px 0}
.hero{padding:18px 20px}.lede{font-size:1.2rem;line-height:1.45;margin:10px 0 4px}.facts{color:var(--muted);margin:0 0 12px}.hero dl.kv{margin-top:14px;font-size:.9rem}
.next ol{margin:6px 0 0;padding-left:20px}.next li{margin:6px 0}.next pre{margin:4px 0}
svg .track-good{fill:none;stroke:var(--good);stroke-width:1.6;opacity:.55;stroke-linejoin:round}
svg .track-bad{fill:none;stroke:var(--bad);stroke-width:1.8;opacity:.9;stroke-linejoin:round}
svg .track-focus{fill:none;stroke:var(--bad);stroke-width:9;opacity:.16;stroke-linecap:round;stroke-linejoin:round}
svg .div-ring{fill:none;stroke:var(--fg);stroke-width:1.5}svg .home{fill:var(--fg)}
svg .susp{fill:var(--shade)}svg .susp-final{fill:var(--bad);opacity:.35}
footer{margin-top:40px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:.9rem}
@media print{details{display:block}body{background:#fff}}
"""


def _e(x) -> str:
    return escape(str(x), quote=True)


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _git(repo: str | None, *args: str) -> str:
    """Best effort: the checkout may not exist where the report is written."""
    if not repo or not Path(repo).is_dir():
        return ""
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _dur(seconds: float) -> str:
    s = int(round(seconds))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return t("report.dur.h", h=h, m=m) if h else t("report.dur.m", m=m, s=s) if m else t("report.dur.s", s=s)


def _badge(verdict: str) -> str:
    return f'<span class="badge {VERDICT_CLASS.get(verdict, "")}">{_e(verdict)}</span>'


# ------------------------------------------------------------------ data

def parse_finding(f) -> dict:
    """Findings are stored as text, and as data from newer investigations; both give
    {metric, kind, value, expected, side, lo, hi, rule}. The text parses in either language."""
    if isinstance(f, dict):
        d = dict(f)
        text = d.get("expected", "")
    else:
        m = FINDING.match(str(f))
        if not m:
            return {"metric": str(f), "kind": "", "value": math.nan, "expected": "", "side": "",
                    "lo": None, "hi": None, "rule": None}
        d = {"metric": m["metric"], "kind": m["kind"].lower(), "expected": m["exp"], "side": ""}
        try:
            d["value"] = float(m["value"])
        except ValueError:
            d["value"] = math.nan
        text = m["exp"]
    band = re.fullmatch(r"\[(\S+), (\S+)\]", text)
    rule = re.fullmatch(r"(lt|le|gt|ge|==) (\S+)", text)
    d["lo"] = d["hi"] = d["rule"] = None
    try:
        if band:
            d["lo"], d["hi"] = float(band[1]), float(band[2])
            if not d.get("side") and d["value"] == d["value"]:
                d["side"] = "+" if d["value"] > d["hi"] else "-" if d["value"] < d["lo"] else ""
        elif rule:
            d["rule"] = ("eq" if rule[1] == "==" else rule[1], float(rule[2]))
    except ValueError:
        pass
    d["value"] = d.get("value", math.nan)
    return d


def deviation(f: dict) -> float:
    """How far outside its band a finding is, in band widths: orders the metrics for the plots."""
    v = f["value"]
    if f["lo"] is not None and v == v:
        w = (f["hi"] - f["lo"]) or 1.0
        return max(f["lo"] - v, v - f["hi"], 0) / w
    return 1e9 if f["kind"] == "rule" else 0.0


def findings_of(entry: dict) -> list[dict]:
    return [parse_finding(f) for f in (entry.get("data") or entry.get("findings") or [])]


def parse_evidence(text: str | None) -> dict:
    out = {"diff": None, "author": "", "date": ""}
    if not text:
        return out
    m = re.search(r"^```diff\n(.*?)^```", text, re.S | re.M)
    if m:
        out["diff"] = m[1].rstrip("\n").split("\n")
        for line in out["diff"]:
            if line.startswith("Author:"):
                out["author"] = line[7:].strip()
            elif line.startswith("Date:"):
                out["date"] = line[5:].strip()
                break
    return out


def count_flights(d: Path) -> int:
    return (len(list(d.glob("bad/*.metrics.json"))) + len(list(d.glob("bisect/*/*.metrics.json")))
            + len(list(d.glob("fix/*/*.metrics.json"))))


class Inv:
    """One investigation directory, read once."""

    def __init__(self, d: Path):
        self.dir = Path(d)
        self.rec = _read_json(self.dir / "record.json")
        if not isinstance(self.rec, dict):
            raise ReportError(t("report.err.norecord", path=self.dir / "record.json"))
        self.steps = self.rec.get("steps") or []
        self.evidence = _read_text(self.dir / "evidence.md")
        self.ev = parse_evidence(self.evidence)
        self.fix = _read_json(self.dir / "fix.json")
        self.expl = _read_json(self.dir / "explanation.json")
        self.expl_md = _read_text(self.dir / "explanation.md")
        base = self.rec.get("baseline_dir")
        self.base_dir = Path(base) if base and Path(base).is_dir() else None
        if self.base_dir is None and base:      # moved tree: same cache name under this results/
            alt = self.dir.parent.parent / "results" / "baselines" / Path(base).name
            self.base_dir = alt if alt.is_dir() else None
        self.bad_dir = self.dir / "bad" if (self.dir / "bad").is_dir() else None

    def good_source(self, name: str) -> tuple[Path | None, bool]:
        """Where the good commit's telemetry for a scenario is. The baseline of the investigation
        may be gone (the scenarios changed and it was flown again): then another run set of the
        same good commit stands in, but only if it flew the same steps as the bad runs, and the
        report says so (second value: approximate)."""
        if self.base_dir and run_files(self.base_dir, name, 1):
            return self.base_dir, False
        sha = (self.rec.get("good") or "")[:12]
        if not sha:
            return None, False
        base = self.rec.get("baseline_dir")
        pools = [self.dir / "fix"] if (self.dir / "fix").is_dir() else []
        pools.append(Path(base).parent if base else self.dir.parent.parent / "results" / "baselines")
        bad = plots.load_run(next(iter(run_files(self.bad_dir, name, 1)), Path("/nonexistent")))
        for pool in pools:
            for d in sorted(pool.glob(f"*{sha}-*"), key=lambda p: -p.stat().st_mtime) if pool.is_dir() else []:
                good = plots.load_run(next(iter(run_files(d, name, 1)), Path("/nonexistent")))
                if good and bad and plots.step_signature(good) == plots.step_signature(bad):
                    return d, True
        return None, False

    def step(self, kind: str) -> dict | None:
        return next((s for s in self.steps if s.get("kind") == kind), None)

    def all_steps(self, kind: str) -> list[dict]:
        return [s for s in self.steps if s.get("kind") == kind]

    @property
    def outcome(self) -> str:
        o = self.rec.get("outcome")
        return o if o in OUTCOMES else "incomplete"

    @property
    def triage(self) -> dict:
        s = self.step("triage")
        return (s or {}).get("scenarios") or self.rec.get("triage") or {}

    @property
    def report(self) -> dict:
        return (self.step("detect") or {}).get("report") or {}


# -------------------------------------------------------------- sections

def _th(key: str, **parts: str) -> str:
    """A catalogue sentence with HTML parts: the sentence is escaped, the parts are not."""
    text = _e(t(key, **{k: f"\x00{k}\x00" for k in parts}))
    for k, html in parts.items():
        text = text.replace(f"\x00{k}\x00", html)
    return text


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float)) and math.isfinite(x)]
    return sum(xs) / len(xs) if xs else None


def lead(v: Inv) -> tuple[str, dict] | None:
    """The scenario and finding the report leads with."""
    for name in plotted_scenarios(v):
        h = headline(v, name)
        if h:
            return name, h
    return None


def lede(v: Inv) -> str:
    """One sentence: what changed, by how much, after which commit."""
    c = v.rec.get("culprit") or {}
    ld = lead(v)
    if v.outcome != "localized" or not c.get("culprit") or not ld:
        return _e(t("report.outcome." + v.outcome + ".text"))
    name, f = ld
    commit = f'<code>{_e(c["culprit"][:10])}</code> <strong>{_e(c.get("subject", ""))}</strong>'
    parts = {"commit": commit, "scenario": f"<code>{_e(name)}</code>", "metric": f'<code>{_e(f["metric"])}</code>'}
    unit = unit_of(f["metric"])
    u = f" {unit}" if unit else ""
    if f["kind"] == "rule":
        if f["metric"] == "completed":
            return _th("report.lede.incomplete", **parts)
        return _th("report.lede.rule", bad=_e(num_text(f["value"]) + u), expected=_e(expected_text(f)), **parts)
    good = _mean([m.get(f["metric"]) for m in metric_files(v.good_source(name)[0], name)])
    if good is None and f["lo"] is not None:
        good = (f["lo"] + f["hi"]) / 2
    bad = f["value"]
    if good is None or bad != bad:
        return _th("report.lede.change.nogood", bad=_e(num_text(bad) + u), **parts)
    vals = _e(f"{good:.3g} → {bad:.3g}{u}")
    if abs(good) < 1e-6:
        return _th("report.lede.change", values=vals, **parts)
    pct = _e(t("report.pct", p=f"{abs(bad - good) / abs(good) * 100:.0f}"))
    return _th("report.lede.up" if bad > good else "report.lede.down", pct=pct, values=vals, **parts)


def facts(v: Inv) -> str:
    c, ld = v.rec.get("culprit") or {}, lead(v)
    out = []
    if ld and v.triage.get(ld[0]):
        tr = v.triage[ld[0]]
        out.append(t("report.facts.runs", k=tr.get("non_pass", 0), n=tr.get("runs", 0)))
    if c.get("range") == 1:
        out.append(t("report.facts.single"))
    elif c.get("tests") is not None and c.get("range"):
        out.append(t("report.facts.search", k=c["tests"], n=c["range"]))
    if v.steps:
        out.append(_dur(max(s.get("at_s", 0) for s in v.steps)))
    return " · ".join(out)


def map_figure(v: Inv, name: str, metric: str) -> str:
    tl = divergence_of(v, name)
    good_dir, _ = v.good_source(name)
    fig = plots.ground_track(metric, run_files(good_dir, name), run_files(v.bad_dir, name),
                             tl.get("first_divergence_t") if tl else None)
    if not fig:
        return ""
    note = t("report.map.zoom", metric=metric) if fig["zoom"] else t("report.map.note")
    return f'{fig["svg"]}{plots.legend(fig["good"], fig["bad"], band=False)}<p class="muted">{_e(note)}</p>'


def sec_summary(v: Inv) -> str:
    r, c = v.rec, v.rec.get("culprit") or {}
    repo = r.get("repo")
    cls = {"localized": "fail", "no_regression": "pass"}.get(v.outcome, "drift")
    out = [f'<div class="card hero"><div class="verdict"><span class="badge {cls}">{_e(t("report.outcome." + v.outcome))}'
           f'</span></div><p class="lede">{lede(v)}</p>']
    if v.outcome == "localized" and facts(v):
        out.append(f'<p class="facts">{_e(facts(v))}</p>')
    if c.get("caution"):
        out.append(f'<p class="note">{_e(t("ev.caution." + c["caution"]))}</p>')
    ld = lead(v)
    if ld:
        out.append(map_figure(v, ld[0], ld[1]["metric"]))
    out.append('<dl class="kv">')

    def kv(k, val):
        if val not in ("", None):
            out.append(f"<dt>{_e(t(k))}</dt><dd>{val}</dd>")

    if c.get("culprit"):
        kv("report.k.culprit", f'<code>{_e(c["culprit"][:10])}</code> {_e(c.get("subject", ""))}')
        kv("report.k.author", _e(v.ev["author"]))
        kv("report.k.date", _e(v.ev["date"]))
        kv("report.k.files", ", ".join(f"<code>{_e(f)}</code>" for f in c.get("files") or []))
        kv("report.k.search", _e(t("report.search", n=c.get("range", "?"), k=c.get("tests", "?"))))
        if c.get("ambiguous_with"):
            kv("report.k.ambiguous", ", ".join(f"<code>{_e(x[:10])}</code>" for x in c["ambiguous_with"]))
        if c.get("caution"):
            kv("report.k.caution", _e(t("ev.caution." + c["caution"])))
    cov = r.get("coverage") or {}
    if cov.get("code"):
        kv("report.k.coverage", _e(t("report.coverage.value", run=cov["run"], code=cov["code"],
                                     pct=f'{100 * cov["run"] / cov["code"]:.0f}%')))
    for key, ref in (("report.k.good", "good"), ("report.k.bad", "bad")):
        if r.get(ref):
            kv(key, f'<code>{_e(r[ref][:10])}</code> {_e(_git(repo, "log", "-1", "--format=%s", r[ref]))}')
    kv("report.k.suite", _e(r.get("suite") or "scenarios"))
    n = count_flights(v.dir)
    base_n = len(list(v.base_dir.glob("*.metrics.json"))) if v.base_dir else 0
    kv("report.k.flights", _e(t("report.flights" if base_n else "report.flights.nobase", n=n, b=base_n)) if n or base_n else "")
    if v.steps:
        kv("report.k.wall", _e(_dur(max(s.get("at_s", 0) for s in v.steps))))
    kv("report.k.id", f'<code>{_e(v.dir.name)}</code>')
    out.append("</dl></div>")
    return "".join(out)


def _home_rel(path: str | None) -> str:
    """~/x for a path under the home directory: shorter, and no user name in a shared report."""
    if not path:
        return ""
    try:
        return "~/" + Path(path).relative_to(Path.home()).as_posix()
    except ValueError:
        return path


def _web_commit(repo: str | None, sha: str) -> str:
    """The commit's page on GitHub, as text, when the checkout's origin is on GitHub and an origin
    branch has the commit (a local-only commit has no page)."""
    url = _git(repo, "remote", "get-url", "origin")
    m = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?", url)
    if not m or not _git(repo, "branch", "-r", "--contains", sha, "--list", "origin/*"):
        return ""
    return f"https://github.com/{m[1]}/commit/{sha}"


def context_at(v: Inv, name: str, div: float) -> str:
    """Mode and last mission item of the first bad run when it first parted from good."""
    run = plots.load_run(next(iter(run_files(v.bad_dir, name, 1)), Path("/nonexistent")))
    if not run:
        return ""
    at = plots._arm_time(run) + div
    mode = item = None
    for t_, k, d in run["events"]:
        if t_ > at:
            break
        if k == "mode":
            mode = d
        elif k == "statustext" and str(d).startswith("Mission: "):
            item = str(d)[9:]
    return " · ".join(str(x) for x in (mode, item) if x)


def sec_next(v: Inv) -> str:
    c, ld = v.rec.get("culprit") or {}, lead(v)
    if v.outcome != "localized" or not c.get("culprit") or not ld:
        return ""
    name, sha = ld[0], c["culprit"]
    items = []
    tl = divergence_of(v, name)
    div = tl.get("first_divergence_t") if tl else None
    ctx = context_at(v, name, div) if div is not None else ""
    if div is not None and ctx:
        items.append(_th("report.next.fly.at", scenario=f"<code>{_e(name)}</code>", t=_e(f"{div:g}"), context=f"<strong>{_e(ctx)}</strong>"))
    else:
        items.append(_th("report.next.fly", scenario=f"<code>{_e(name)}</code>"))
    web = _web_commit(v.rec.get("repo"), sha)
    items.append(_th("report.next.intent", commit=f"<code>{_e(sha[:10])}</code>")
                 + (f"<br><code>{_e(web)}</code>" if web else ""))
    cmd = (f"forkpilot investigate --repo {_home_rel(v.rec.get('repo')) or '<your clone>'} --good {sha[:10]}^ "
           f"--bad {sha[:10]} --only {name} --report")
    items.append(_e(t("report.next.repro")) + f"<pre>{_e(cmd)}</pre>")
    return (f'<div class="card next"><strong>{_e(t("report.h.next"))}</strong><ol>'
            + "".join(f"<li>{i}</li>" for i in items) + "</ol></div>")


def direction(f: dict) -> str:
    if f["kind"] == "rule":
        return t("report.dir.rule")
    return {"+": t("report.dir.above"), "-": t("report.dir.below")}.get(f["side"], "")


def expected_text(f: dict) -> str:
    if f["lo"] is not None:
        return f"[{fmt(f['lo'])}, {fmt(f['hi'])}]"
    if f["rule"]:
        op, b = f["rule"]
        return f"{plots.RULE_SIGNS.get(op, '=')} {fmt(b)}"
    return t("report.exp.missing") if f["expected"] in MISSING else f["expected"]


def num_text(v: float) -> str:
    return "–" if v != v else fmt(v)


def sec_verdicts(v: Inv) -> str:
    if not v.report:
        return ""
    tri = v.triage
    nonpass = [(n, e) for n, e in v.report.items() if e.get("verdict") != "PASS"]
    passed = [n for n, e in v.report.items() if e.get("verdict") == "PASS"]
    rows = []
    shown = sorted(nonpass, key=lambda x: (x[1].get("verdict") != "FAIL", x[0]))
    if len(passed) <= MAX_ROWS_PASS:
        shown += [(n, v.report[n]) for n in passed]
        passed = []
    for name, e in shown:
        fs = findings_of(e)[:MAX_FINDINGS] or [None]
        extra = len(findings_of(e)) - len(fs) if fs != [None] else 0
        tr = tri.get(name)
        trtext = ""
        if tr:
            trtext = (f'{_e(t("report.triage." + tr["class"]))}<br><span class="muted">'
                      f'{_e(t("report.triage.runs", k=tr.get("non_pass", 0), n=tr.get("runs", 0)))}</span>')
        span = len(fs) + (1 if extra > 0 else 0)
        for i, f in enumerate(fs):
            cells = ""
            if i == 0:
                cells = (f'<td rowspan="{span}"><code>{_e(name)}</code></td>'
                         f'<td rowspan="{span}">{_badge(e.get("verdict", ""))}</td><td rowspan="{span}">{trtext}</td>')
            if f is None:
                cells += '<td colspan="4"></td>'
            else:
                cells += (f'<td><code>{_e(f["metric"])}</code></td><td class="num">{_e(num_text(f["value"]))}</td>'
                          f'<td class="num">{_e(expected_text(f))}</td><td>{_e(direction(f))}</td>')
            rows.append(f"<tr>{cells}</tr>")
        if extra > 0:
            rows.append(f'<tr><td colspan="4" class="muted">{_e(t("report.more", n=extra))}</td></tr>')
    head = "".join(f"<th>{_e(t(k))}</th>" for k in ("report.th.scenario", "report.th.verdict", "report.th.triage",
                   "report.th.metric", "report.th.value", "report.th.expected", "report.th.direction"))
    out = [f"<h2>{_e(t('report.h.verdicts'))}</h2>"]
    if rows:
        out.append(f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    if passed:
        out.append(f'<details><summary>{_e(t("report.pass.n", n=len(passed)))}</summary><p>'
                   + ", ".join(f"<code>{_e(n)}</code>" for n in passed) + "</p></details>")
    out.append(f'<p class="muted">{_e(t("report.verdicts.note"))}</p>')
    return "".join(out)


def plotted_scenarios(v: Inv) -> list[str]:
    names = [n for n in (v.rec.get("targets") or sorted(v.rec.get("symptom") or {})) if n in v.report]
    if not names:
        names = [n for n, e in sorted(v.report.items(), key=lambda x: x[1].get("verdict") != "FAIL")
                 if e.get("verdict") != "PASS"]
    return names[:MAX_PLOTTED]


def headline(v: Inv, name: str) -> dict | None:
    fs = findings_of(v.report.get(name, {}))
    wanted = {w[1] for w in v.rec.get("wanted") or [] if w and w[0] == name}
    pool = [f for f in fs if f["metric"] in wanted] or fs
    return max(pool, key=deviation, default=None)


def divergence_of(v: Inv, name: str):
    for s in v.all_steps("timeline"):
        if s.get("scenario") == name:
            return s
    return None


def sec_plots(v: Inv) -> str:
    names = plotted_scenarios(v)
    if not names:
        return ""
    out = [f"<h2>{_e(t('report.h.plots'))}</h2>"]
    drawn = False
    for name in names:
        h = headline(v, name)
        if not h:
            continue
        tl = divergence_of(v, name)
        div = tl.get("first_divergence_t") if tl else None
        good_dir, approx = v.good_source(name)
        fig = plots.telemetry_figure(h["metric"], run_files(good_dir, name), run_files(v.bad_dir, name), div)
        out.append(f'<h3>{_e(name)} <span class="muted">· <code>{_e(h["metric"])}</code></span></h3>')
        unit = unit_of(h["metric"])
        out.append(f'<p>{_e(t("report.plot.headline", metric=h["metric"], value=fmt(h["value"]) + (" " + unit if unit else ""), expected=expected_text(h)))}</p>')
        if not fig:
            out.append(f'<p class="note">{_e(t("report.plot.notelemetry"))}</p>')
            continue
        drawn = True
        if name != (lead(v) or ('',))[0]:
            m = map_figure(v, name, h["metric"])
            if m:
                out.append(f'<p class="muted">{_e(t("report.map.title"))}</p>{m}')
        if fig["zoom"]:
            out.append(f'<p class="muted">{_e(t("report.plot.zoom"))}</p>{fig["zoom"]}')
        out.append(f'<p class="muted">{_e(t("report.plot.flight"))}</p>{fig["flight"]}')
        out.append(plots.legend(fig["good"], fig["bad"]))
        if approx:
            out.append(f'<p class="note">{_e(t("report.plot.approx"))}</p>')
        if tl and div is not None:
            sig = (tl.get("signals") or [{}])[0]
            out.append(f'<p>{_e(t("report.plot.diverge", t=div))}'
                       + (f' {_e(t("report.plot.signal", name=_signal_name(sig.get("signal")), m=sig.get("outside_m", "")))}'
                          if sig.get("signal") and sig.get("outside_m") is not None else "") + "</p>")
    rows = band_rows(v, names)
    if rows:
        out.append(f"<h3>{_e(t('report.h.bands'))}</h3><p class=\"muted\">{_e(t('report.bands.note'))}</p>")
        out.append(plots.band_chart(rows, t("report.h.bands")))
    return "".join(out) if (drawn or rows) else ""


def _signal_name(s: str | None) -> str:
    return t({"irtifa": "plot.sig.alt", "altitude": "plot.sig.alt", "yatay konum": "plot.sig.horizontal",
              "horizontal": "plot.sig.horizontal"}.get(s or "", "plot.sig.horizontal"))


def band_rows(v: Inv, names: list[str]) -> list[BandRow]:
    cand: list[tuple[float, BandRow]] = []
    order = names + [n for n, e in v.report.items() if e.get("verdict") != "PASS" and n not in names]
    for name in order:
        base_m, bad_m = metric_files(v.good_source(name)[0], name), metric_files(v.bad_dir, name)
        for f in findings_of(v.report.get(name, {})):
            if f["kind"] not in ("drift", "rule") or not (f["lo"] is not None or f["rule"]):
                continue
            pick = lambda ms: [m[f["metric"]] for m in ms if isinstance(m.get(f["metric"]), (int, float))
                               and math.isfinite(m[f["metric"]])]
            row = BandRow(f["metric"], unit_of(f["metric"]), pick(base_m), pick(bad_m), f["lo"], f["hi"],
                          f["rule"], f["value"] if f["value"] == f["value"] else None, name)
            cand.append((deviation(f) + (1e6 if name in names else 0), row))
    cand.sort(key=lambda x: -x[0])
    return [r for _, r in cand[:MAX_BAND_ROWS]]


def bisect_figure(v: Inv, tests: list[dict]) -> str:
    """The narrowing drawn over the range; needs the checkout for the order of the commits."""
    r = v.rec
    commits = _git(r.get("repo"), "rev-list", "--reverse", "--first-parent", f"{r.get('good')}..{r.get('bad')}").split()
    index = {sha: i for i, sha in enumerate(commits)}
    if not commits or any(s.get("sha") not in index for s in tests):
        return ""
    rows = [(index[s["sha"]], s.get("result", ""), f'{i}  {s["sha"][:10]}  {t("report.bisect." + s["result"]) if s.get("result") else ""}')
            for i, s in enumerate(tests, 1)]
    culprit = index.get((r.get("culprit") or {}).get("culprit"))
    return (plots.bisect_strip(len(commits), rows, culprit)
            + f'<p class="muted">{_e(t("report.bisect.strip", n=len(commits), k=len(tests)))}</p>')


def sec_bisect(v: Inv) -> str:
    tests = v.all_steps("bisect_test")
    if not tests:
        return ""
    repo = v.rec.get("repo")
    rows = []
    for i, s in enumerate(tests, 1):
        res = s.get("result", "")
        subject = s.get("subject") or _git(repo, "log", "-1", "--format=%s", s["sha"])
        b = s.get("build") or {}
        err = b.get("error")
        build = t("report.build.cached") if b.get("cached") else t("report.build.built", s=b.get("seconds", "?"))
        if res == "skip":
            build = t("report.build.failed")
        symptom = ""
        if res == "bad":
            fs = [f["metric"] for e in (s.get("verdict") or {}).values() for f in findings_of(e)]
            symptom = ", ".join(sorted(set(fs))[:4])
        note = f'<br><span class="muted">{_e(symptom)}</span>' if symptom else ""
        if err:
            note += f'<details><summary>{_e(t("report.build.log"))}</summary><pre>{_e(err[-600:])}</pre></details>'
        cls = {"bad": "fail", "good": "pass", "skip": "warn"}.get(res, "")
        rows.append(f'<tr><td class="num">{i}</td><td><code>{_e(s["sha"][:10])}</code></td><td>{_e(subject)}</td>'
                    f'<td><span class="badge {cls}">{_e(t("report.bisect." + res)) if res else ""}</span>{note}</td>'
                    f'<td class="num">{_e(s.get("runs", ""))}</td><td>{_e(build)}</td></tr>')
    head = "".join(f"<th>{_e(t(k))}</th>" for k in ("report.th.n", "report.th.commit", "report.th.subject",
                   "report.th.result", "report.th.runs", "report.th.build"))
    c = v.rec.get("culprit") or {}
    amb = f'<p class="note">{_e(t("report.bisect.ambiguous", shas=", ".join(x[:10] for x in c["ambiguous_with"])))}</p>' \
        if c.get("ambiguous_with") else ""
    return (f"<h2>{_e(t('report.h.bisect'))}</h2>{bisect_figure(v, tests)}<p class=\"muted\">{_e(t('report.bisect.note'))}</p>"
            f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>{amb}')


def sec_coverage(v: Inv) -> str:
    """Changed lines run / not run per file (coverage.json from `investigate --coverage`)."""
    from .coverage import ranges
    try:
        res = json.loads((v.dir / "coverage.json").read_text())
    except (OSError, ValueError):
        return ""
    files = res.get("files") or {}
    built = sorted((f for f, x in files.items() if x.get("built") and x.get("code")),
                   key=lambda f: (-len(files[f]["not_run"]), f))
    rows = []
    for f in built[:COVERAGE_ROWS]:
        x = files[f]
        cls = "pass" if not x["not_run"] else "fail" if not x["run"] else "warn"
        rows.append(f'<tr><td><code>{_e(f)}</code></td>'
                    f'<td class="num"><span class="badge {cls}">{x["run"]}/{x["code"]}</span></td>'
                    f'<td>{_e(", ".join(sorted(x.get("by_scenario") or {})))}</td>'
                    f'<td><code>{_e(ranges(x["not_run"]))}</code></td></tr>')
    head = "".join(f"<th>{_e(t(k))}</th>" for k in ("report.coverage.col.file", "report.coverage.col.run",
                   "report.coverage.col.by", "report.coverage.col.missed"))
    scope = "culprit" if v.rec.get("outcome") == "localized" else "range"
    tail = ""
    if len(built) > COVERAGE_ROWS:
        tail += f'<p class="muted">{_e(t("report.coverage.more", n=len(built) - COVERAGE_ROWS))}</p>'
    if res.get("not_built"):
        nb = res["not_built"]
        tail += f'<p class="muted">{_e(t("report.coverage.not_built", files=", ".join(nb[:20]) + (" ..." if len(nb) > 20 else "")))}</p>'
    code, run = res.get("code_lines", 0), res.get("run_lines", 0)
    total = f'{t("report.k.coverage")}: ' + t("report.coverage.value", run=run, code=code,
                                              pct=f"{100 * run / code:.0f}%" if code else "-")
    return (f"<h2>{_e(t('report.h.coverage'))}</h2><p class=\"muted\">{_e(t('report.coverage.note'))} "
            f"{_e(t('report.coverage.scope.' + scope))}</p><p>{_e(total)}</p>"
            + (f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
               if rows else "") + tail)


def diff_html(lines: list[str]) -> str:
    out = []
    for line in lines[:MAX_DIFF_LINES]:
        cls = "ad" if line.startswith("+") and not line.startswith("+++") else \
            "rm" if line.startswith("-") and not line.startswith("---") else \
            "hh" if line.startswith(("@@", "diff ", "index ")) else ""
        out.append(f'<span class="{cls}">{_e(line) or " "}</span>')
    if len(lines) > MAX_DIFF_LINES:
        out.append(f'<span class="hh">{_e(t("report.diff.cut", n=len(lines) - MAX_DIFF_LINES))}</span>')
    return f'<pre class="diff">{"".join(out)}</pre>'


def sec_diff(v: Inv) -> str:
    if not v.ev["diff"]:
        return ""
    return (f"<h2>{_e(t('report.h.diff'))}</h2><details><summary>{_e(t('report.diff.open'))}</summary>"
            f"{diff_html(v.ev['diff'])}</details>")


def md_html(md: str) -> str:
    """The few Markdown constructs explanation.md uses."""
    def inline(s):
        s = _e(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*(\[[^\]]+\])\*\*", lambda m: f'<strong class="warn">{m[1]}</strong>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        return re.sub(r"(?<![\w*])_([^_]+)_(?![\w*])", r"<em>\1</em>", s)
    out, fence, items = [], None, []
    flush = lambda: (out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>"), items.clear()) if items else None
    for line in md.splitlines():
        if line.strip().startswith("```"):
            flush()
            if fence is None:
                fence = []
            else:
                out.append("<pre>" + _e("\n".join(fence)) + "</pre>")
                fence = None
        elif fence is not None:
            fence.append(line.removeprefix("  "))
        elif line.startswith("- "):
            items.append(line[2:])
        elif line.startswith("#"):
            flush()
            level = min(len(line) - len(line.lstrip("#")) + 2, 5)
            out.append(f"<h{level}>{inline(line.lstrip('# '))}</h{level}>")
        elif line.strip():
            flush()
            out.append(f"<p>{inline(line.strip())}</p>")
    flush()
    return "".join(out)


def sec_explanation(v: Inv) -> str:
    d = v.expl
    if not d and not v.expl_md:
        return ""
    out = [f"<h2>{_e(t('report.h.explanation'))}</h2><p class=\"muted\">{_e(t('report.expl.note'))}</p>"]
    if isinstance(d, dict) and "summary" in d:
        out.append(f"<h3>{_e(t('report.expl.summary'))}</h3><p>{_e(d['summary'])}</p>"
                   f"<h3>{_e(t('report.expl.mechanism'))}</h3><p>{_e(d.get('mechanism', ''))}</p>"
                   f"<h3>{_e(t('report.expl.lines'))}</h3>")
        for c in d.get("culprit_lines") or []:
            mark = "" if c.get("verified") else f' <span class="badge warn">{_e(t("report.expl.unverified"))}</span>'
            out.append(f'<p><code>{_e(c.get("file", ""))}</code>{mark}</p><pre>{_e(c.get("quote", ""))}</pre>'
                       + (f'<p>{_e(c["why"])}</p>' if c.get("why") else ""))
        for key, name in (("affected_situations", "report.expl.affected"), ("open_questions", "report.expl.open")):
            out.append(f"<h3>{_e(t(name))}</h3><ul>" + "".join(f"<li>{_e(x)}</li>" for x in d.get(key) or []) + "</ul>")
        out.append(f"<h3>{_e(t('report.expl.confidence'))}</h3><p>{_e(d.get('confidence', ''))}</p>")
        if d.get("model"):
            out.append(f'<p class="muted">{_e(d.get("backend", ""))} · {_e(d["model"])}</p>')
    else:
        out.append(md_html(v.expl_md or ""))
    return "".join(out)


def sec_fix(v: Inv) -> str:
    cands = (v.fix or {}).get("candidates") if isinstance(v.fix, dict) else None
    if not cands:
        return ""
    rows, details = [], []
    for c in cands:
        out = c.get("outcome", "")
        key = "fix.outcome." + out
        label = t(key) if out else ""
        cls = "pass" if out == "fixes" else "fail" if out in ("no_effect", "build_failed", "not_applicable", "model_error") else "drift"
        notes = []
        for k, name in (("remaining", "fix.rem"), ("new", "fix.new"), ("unconfirmed", "fix.unconf")):
            if c.get(k):
                notes.append(f'<div><strong>{_e(t(name))}:</strong> '
                             + ", ".join(f"<code>{_e(' '.join(map(str, s)))}</code>" for s in c[k]) + "</div>")
        if c.get("error"):
            notes.append(f'<div class="muted">{_e(str(c["error"])[-200:])}</div>')
        rows.append(f'<tr><td><code>{_e(c.get("name", ""))}</code></td><td>{_e(c.get("source", ""))}</td>'
                    f'<td><span class="badge {cls}">{_e(label)}</span></td><td class="num">{_e(c.get("diff_lines", ""))}</td>'
                    f'<td>{"".join(notes)}</td></tr>')
        extra = ""
        for k, name in (("rationale", "fix.why"), ("risk", "fix.risk")):
            if c.get(k):
                extra += f"<p><strong>{_e(t(name))}:</strong> {_e(c[k])}</p>"
        diff = _read_text(v.dir / "fix" / f"{c.get('name', '')}.diff")
        if diff:
            extra += diff_html(diff.splitlines())
        if extra:
            details.append(f'<details><summary><code>{_e(c.get("name", ""))}</code></summary>{extra}</details>')
    head = "".join(f"<th>{_e(t(k))}</th>" for k in ("fix.cand", "fix.src", "fix.out", "fix.size", "report.th.notes"))
    return (f"<h2>{_e(t('report.h.fix'))}</h2><p class=\"muted\">{_e(t('report.fix.note'))}</p>"
            f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            + _before_after(v.fix.get("before_after") or []) + "".join(details))


def _before_after(rows: list[dict]) -> str:
    """Symptom metric means: good, bad, each candidate (fix.json "before_after")."""
    from .fix import _num
    if not rows:
        return ""
    names = list(rows[0]["candidates"])
    head = "".join(f"<th>{_e(x)}</th>" for x in [t("fix.ba.metric"), "good", "bad", *names])
    body = "".join(
        f'<tr><td><code>{_e(r["scenario"])}</code> {_e(r["metric"])}</td>'
        + "".join(f'<td class="num">{_e(_num(x))}</td>' for x in [r["good"], r["bad"], *(r["candidates"][n] for n in names)])
        + "</tr>" for r in rows)
    return (f'<p class="muted">{_e(t("fix.ba.note"))}</p>'
            f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def build(inv_dir: Path) -> str:
    v = Inv(inv_dir)
    body = [sec_summary(v), sec_next(v), sec_verdicts(v), sec_plots(v), sec_bisect(v), sec_coverage(v), sec_diff(v), sec_explanation(v), sec_fix(v)]
    lang = current_lang()
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_e(t('report.title', id=v.dir.name))}</title><style>{CSS}</style></head><body><main>"
            f"<h1>{_e(t('report.title', id=v.dir.name))}</h1><p class=\"sub\">{_e(t('report.subtitle'))}</p>"
            + "".join(body) + f"<footer>{_e(t('report.footer'))}</footer></main></body></html>\n")


def write(inv_dir: Path, out: Path | None = None) -> Path:
    html = build(Path(inv_dir))
    out = Path(out) if out else Path(inv_dir) / "report.html"
    out.write_text(html, encoding="utf-8")
    return out
