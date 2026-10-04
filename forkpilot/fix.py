"""Fix stage: propose patches for a localized regression; the oracle decides whether they work.

Candidates, in order:
  pick   : a known fix (e.g. upstream's fix commits) cherry-picked onto `bad`. No AI.
  patch-N: patch files given by an engineer (git apply). No AI.
  revert : the culprit (and any unbuildable commits it could not be told apart from) reverted
           on top of `bad`. No AI. Often not what a fork wants to ship (the culprit may also carry
           a feature), but it shows whether the symptom belongs to that commit alone.
  llm-N  : a minimal edit proposed by a model from evidence.md and the code at `bad`. A failed
           attempt's oracle result is fed back to the next attempt.

Each candidate is committed on a detached HEAD (kept as refs/forkpilot/fix/...), built and flown:
the symptom scenarios TRIAGE_RUNS times, then (scenarios suite) the rest of the battery. If the
scenarios changed since the investigation, the baseline and `bad` are flown again first. The
model never judges its own patch. 'fixes' means the oracle no longer sees the symptom and sees
nothing that `bad` did not already show. A finding `bad` did not show is a side effect only if it
holds on fresh runs of both the candidate and `good` (the triage test: rank test on a drift,
Fisher on a rule break); one battery pass against the band is only a screen.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from . import oracle, scenario
from .i18n import MSG, lang as ui_lang, t
from .battery import JOBS, expect_for, judge, load_metrics
from .build import build, git, lock_checkout, resolve
from .explain import LANGS, ExplainError, parse as _parse_json
from .investigate import CONFIRM_ALPHA, TRIAGE_RUNS, baseline, baseline_dir, symptom
from .suites import SUITES

MAX_FILE_LINES = 1500      # longer files are shown as windows around the culprit's lines
WINDOW = 40
MAX_CONTEXT_CHARS = 150_000
SOURCE = (".cpp", ".h", ".hpp", ".c", ".cc")

SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"file": {"type": "string"}, "find": {"type": "string"},
                               "replace": {"type": "string"}},
                "required": ["file", "find", "replace"],
                "additionalProperties": False,
            },
        },
        "risk": {"type": "string"},
    },
    "required": ["rationale", "edits", "risk"],
    "additionalProperties": False,
}

SYSTEM = """You propose a minimal source-code fix for a flight-software regression.

Rules:
- A deterministic oracle found the symptom and a bisect found the commit. After you answer, your edit \
is built and flown in simulation, and the oracle decides whether it fixes the symptom. You never decide \
that yourself.
- Fix the defect, not the feature: the culprit commit may also contain intended changes (unit \
conversions, new options). Keep them and correct only what causes the symptom. Do not simply revert \
the commit; a plain revert is tested separately.
- Edit only the files shown under "Code at the bad commit". Each edit replaces `find` with `replace`. \
`find` must be copied verbatim from that code (whitespace included) and must occur exactly once in the \
file; include enough surrounding lines to make it unique. Make the smallest change that can work. \
No new parameters, no logging, no unrelated cleanup.
- If earlier attempts are listed, they did not fix the symptom: use the oracle's result to change course.
- rationale: what was wrong and why the edit fixes it. risk: what else the edit could affect. Write both \
in {language}.
- Answer with one JSON object matching this schema and nothing else:
{schema}"""


class Unapplicable(Exception):
    pass


def system_prompt(lang: str | None = None) -> str:
    lang = ui_lang(lang)
    return SYSTEM.format(language=LANGS.get(lang, lang), schema=json.dumps(SCHEMA))


def parse(text: str) -> dict:
    d = _parse_json(text, SCHEMA)
    if not isinstance(d["edits"], list) or not all(isinstance(e, dict) and {"file", "find", "replace"} <= set(e)
                                                   for e in d["edits"]):
        raise ExplainError(t("err.fix.edits"))
    return d


def _added_lines(diff: str, path: str) -> list[str]:
    out, cur = [], None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif cur == path and line.startswith("+") and len(line.strip()) > 9:
            out.append(line[1:].strip())
    return out


def code_context(repo: Path, bad: str, culprit: str, files: list[str]) -> tuple[str, list[str]]:
    """The culprit's source files as they are at `bad`; long ones as windows around the lines the
    culprit added. Returns (markdown, files the model may edit)."""
    diff = git(repo, "show", "--format=", culprit)
    out, allowed, size = ["## Code at the bad commit", ""], [], 0
    for path in files:
        if not path.endswith(SOURCE):
            continue
        try:
            text = git(repo, "show", f"{bad}:{path}")
        except subprocess.CalledProcessError:
            continue                       # deleted since the culprit
        lines = text.splitlines()
        if len(lines) > MAX_FILE_LINES:
            added = set(_added_lines(diff, path))
            hit = [i for i, l in enumerate(lines) if l.strip() in added]
            if not hit:
                continue
            keep = sorted({j for i in hit for j in range(max(0, i - WINDOW), min(len(lines), i + WINDOW + 1))})
            parts, prev = [], -1
            for j in keep:
                if j != prev + 1:
                    parts.append("// ... (lines omitted)")
                parts.append(lines[j])
                prev = j
            if prev != len(lines) - 1:
                parts.append("// ... (lines omitted)")
            body = "\n".join(parts)
        else:
            body = text
        if size + len(body) > MAX_CONTEXT_CHARS:
            out += [f"### {path}", "", "(omitted: context limit)", ""]
            continue
        size += len(body)
        allowed.append(path)
        out += [f"### {path}", "", "```cpp", body, "```", ""]
    return "\n".join(out), allowed


def apply_edits(repo: Path, edits: list[dict], allowed: list[str]):
    """Apply find/replace edits in the working tree; all-or-nothing."""
    new: dict[str, str] = {}
    for e in edits:
        if e["file"] not in allowed:
            raise Unapplicable(t("err.fix.file", file=e["file"]))
        text = new[e["file"]] if e["file"] in new else (repo / e["file"]).read_text()
        n = text.count(e["find"]) if e["find"] else 0
        if n != 1:
            raise Unapplicable(t("err.fix.find", file=e["file"], n=n))
        new[e["file"]] = text.replace(e["find"], e["replace"])
    if not new:
        raise Unapplicable(t("err.fix.noedit"))
    for path, text in new.items():
        (repo / path).write_text(text)


def commit_candidate(repo: Path, bad: str, name: str, stamp: str, change) -> str:
    """Commit `change(repo)` on top of `bad` and keep it under a ref. Leaves the tree clean."""
    if git(repo, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules"):
        raise RuntimeError(f"{repo} has uncommitted changes")
    git(repo, "checkout", "-q", "--detach", bad)
    try:
        change(repo)
        if not git(repo, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules"):
            raise Unapplicable(t("err.fix.nodiff"))
        # only file changes: submodule pointers in a shared checkout may lag behind `bad`
        changed = git(repo, "diff", "--name-only", "--ignore-submodules=all").split()
        if changed:
            git(repo, "add", "--", *changed)
        git(repo, "-c", "user.name=ForkPilot", "-c", "user.email=forkpilot@localhost",
            "commit", "-q", "--no-verify", "-m", f"forkpilot fix candidate: {name}")
    except (Unapplicable, subprocess.CalledProcessError) as e:
        for op in ("revert", "cherry-pick"):
            subprocess.run(["git", "-C", str(repo), op, "--abort"], capture_output=True)
        git(repo, "reset", "-q", "--hard", bad)
        if isinstance(e, subprocess.CalledProcessError):
            raise Unapplicable((e.stderr or e.stdout or str(e)).strip()[-500:])
        raise
    sha = resolve(repo, "HEAD")
    git(repo, "update-ref", f"refs/forkpilot/fix/{stamp}/{name}", sha)
    return sha


def reverter(commits: list[str]):
    def change(repo: Path):
        for c in commits:                  # newest first
            parents = git(repo, "show", "-s", "--format=%P", c).split()
            git(repo, "revert", "--no-commit", *(["-m", "1"] if len(parents) > 1 else []), c)
    return change


def picker(commits: list[str]):
    def change(repo: Path):
        for c in commits:                  # oldest first
            git(repo, "cherry-pick", "--no-commit", c)
    return change


def patcher(path: Path):
    def change(repo: Path):
        git(repo, "apply", "--index", str(Path(path).resolve()))
    return change


def holds(sig: tuple, cand: list[dict], base: list[dict], dirs=None) -> bool:
    """Does a finding (scenario, metric, kind, side) hold on fresh runs, candidate vs good?"""
    name, metric, kind, side = sig
    if kind == "drift":
        return oracle.confirm_shift([m.get(metric) for m in base], [m.get(metric) for m in cand],
                                    side, alpha=CONFIRM_ALPHA)

    def broke(runs):
        return sum(any(f.signature() == (metric, kind, side) for f in
                       oracle.check_rules(m, expect_for(name, dirs))) for m in runs)
    return oracle.fail_rate_p(broke(cand), len(cand), broke(base), len(base)) < CONFIRM_ALPHA


def evaluate(rec_dir: Path, name: str, suite, binary: Path, targets: list[str], wanted: set,
             base_dir: Path, bad_found: set, jobs: int, good_binary=None) -> dict:
    """`good_binary`: callable returning the good build, used to confirm new findings."""
    """Fly the candidate; the oracle's findings decide the outcome."""
    out = rec_dir / "fix" / name
    shutil.rmtree(out, ignore_errors=True)       # a candidate of the same name from an earlier run
    suite.run(binary, out, only=targets, n=TRIAGE_RUNS, jobs=jobs, log=None)
    _, rep = judge(out, base_dir, only=targets, dirs=suite.dirs)
    left = symptom(rep, targets) & wanted
    res = {"remaining": sorted(left),
           "findings": {k: [str(f) for f in fs] for k, (v, fs) in rep.items() if v != "PASS"}}
    if left:
        res["outcome"] = "no_effect" if left == wanted else "partial"
        return res
    if suite.name == "scenarios":
        rest = [t for t in load_metrics(base_dir) if t not in targets]
        if rest:
            suite.run(binary, out, only=rest, n=suite.detect_runs, jobs=jobs, log=None)
        _, rep = judge(out, base_dir, only=targets + rest, dirs=suite.dirs)
        res["findings"] = {k: [str(f) for f in fs] for k, (v, fs) in rep.items() if v != "PASS"}
    found = symptom(rep, list(rep))
    new = sorted(found - bad_found)
    if new and good_binary is not None:
        names = sorted({s[0] for s in new})
        fresh = rec_dir / "fix" / f"good-{base_dir.name}"   # fresh good runs, per scenario version
        suite.run(binary, out, only=names, n=TRIAGE_RUNS, jobs=jobs, rep_offset=100, log=None)
        have = load_metrics(fresh)
        todo = [n for n in names if len(have.get(n, [])) < TRIAGE_RUNS]
        if todo:
            suite.run(good_binary(), fresh, only=todo, n=TRIAGE_RUNS, jobs=jobs, rep_offset=100, log=None)
        cand, base = load_metrics(out, min_rep=100), load_metrics(fresh)
        res["unconfirmed"] = [s for s in new if not holds(s, cand.get(s[0], []), base.get(s[0], []), suite.dirs)]
        new = [s for s in new if s not in res["unconfirmed"]]
    res["new"] = new
    res["outcome"] = "side_effects" if new else "fixes"
    res["full_battery"] = suite.name == "scenarios"
    return res


def feedback(history: list[dict]) -> str:
    out = ["## Earlier attempts (none fixed the symptom)", ""]
    for h in history:
        out += [f"### {h['name']}: {h['outcome']}", "", h.get("rationale", ""), ""]
        for e in h.get("edits", []):
            out += [f"`{e['file']}`", "```", "- " + e["find"].replace("\n", "\n- "),
                    "+ " + e["replace"].replace("\n", "\n+ "), "```"]
        if h.get("error"):
            out.append(f"Not applied/built: {h['error']}")
        for k, fs in h.get("findings", {}).items():
            out += [f"- {k}: " + "; ".join(fs)]
        out.append("")
    return "\n".join(out)


def fix(inv_dir: Path, backend=None, attempts: int = 3, lang: str | None = None, jobs: int = JOBS,
        revert: bool = True, picks=(), patches=(), repo: Path | None = None, log=None,
        scenario_dirs=None) -> Path:
    """`repo`: build somewhere other than the investigation's checkout (any clone or worktree
    that has the commits), e.g. while that checkout is busy with another investigation.
    `scenario_dirs`: where the scenarios are now; default: where the investigation flew them
    (its record.json), or the shipped ones."""
    log = log or (lambda msg: print(msg, flush=True))
    inv_dir = Path(inv_dir)
    rec = json.loads((inv_dir / "record.json").read_text())
    if rec.get("outcome") != "localized":
        raise ExplainError(t("err.fix.notlocalized", outcome=rec.get("outcome")))
    repo, bad, cul = Path(repo or rec["repo"]).resolve(), rec["bad"], rec["culprit"]
    base_dir = Path(rec["baseline_dir"])
    vehicle = rec.get("vehicle", "copter")
    dirs = scenario_dirs or rec.get("scenario_dirs")
    if rec.get("suite", "scenarios") == "scenarios":
        scenario.check(dirs, log=log)
    suite = SUITES[rec.get("suite", "scenarios")](repo, rec["good"], dirs, vehicle=vehicle)
    targets = rec.get("targets") or sorted(rec["symptom"])
    bad_dir = inv_dir / "bad"
    _, bad_rep = judge(bad_dir, base_dir, dirs=suite.dirs)
    bad_found = symptom(bad_rep, list(bad_rep))
    wanted = {tuple(w) for w in rec["wanted"]} if rec.get("wanted") else symptom(bad_rep, targets)
    stamp = inv_dir.name
    lock = lock_checkout(repo)
    start_ref = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if start_ref == "HEAD":
        start_ref = resolve(repo, "HEAD")
    results = []
    good_cache = []

    def good_binary() -> Path:
        if not good_cache:
            b, _ = build(repo, rec["good"], log=log, vehicle=vehicle)
            if b is None:
                raise ExplainError(t("err.fix.goodbuild"))
            good_cache.append(b)
        return good_cache[0]

    def attempt(name: str, change, extra: dict) -> dict:
        r = {"name": name, **extra}
        try:
            sha = commit_candidate(repo, bad, name, stamp, change)
        except Unapplicable as e:
            r.update(outcome="not_applicable", error=str(e))
            log(t("log.fix.inapplicable", name=name, e=e))
            return r
        diff = git(repo, "diff", bad, sha)
        (inv_dir / "fix").mkdir(exist_ok=True)
        (inv_dir / "fix" / f"{name}.diff").write_text(diff + "\n")
        binary, info = build(repo, sha, log=log, vehicle=vehicle)
        r.update(sha=sha, diff_lines=diff.count("\n") + 1)
        if binary is None:
            r.update(outcome="build_failed", error=info.get("error", "")[-1500:])
            log(t("log.fix.buildfail", name=name))
            return r
        r.update(evaluate(inv_dir, name, suite, binary, targets, wanted, base_dir, bad_found, jobs,
                          good_binary=good_binary))
        log(t("log.fix.outcome", name=name, outcome=r["outcome"]))
        return r

    try:
        # scenarios edited since the investigation: its flights are no longer comparable
        names = sorted(load_metrics(base_dir))
        only = None if set(names) == set(suite.tests()) else names
        if baseline_dir(suite, rec["good"], only) != base_dir:
            log(t("log.fix.rebase"))
            good_bin, _ = build(repo, rec["good"], log=log, vehicle=vehicle)
            bad_bin, _ = build(repo, bad, log=log, vehicle=vehicle)
            if good_bin is None or bad_bin is None:
                raise ExplainError(t("err.fix.bothbuild"))
            base_dir = baseline(SimpleNamespace(log=log), suite, good_bin, rec["good"], only, jobs)
            bad_dir = inv_dir / "fix" / "bad"
            if not (bad_dir / "DONE").exists():
                suite.run(bad_bin, bad_dir, only=targets, n=TRIAGE_RUNS, jobs=jobs, log=None)
                rest = [t for t in load_metrics(base_dir) if t not in targets]
                if rest and suite.name == "scenarios":
                    suite.run(bad_bin, bad_dir, only=rest, n=suite.detect_runs, jobs=jobs, log=None)
                (bad_dir / "DONE").write_text("")
            _, bad_rep = judge(bad_dir, base_dir, dirs=suite.dirs)
            bad_found = symptom(bad_rep, list(bad_rep))
            wanted &= symptom(bad_rep, targets)
            if not wanted:
                raise ExplainError(t("err.fix.norepro"))
        if picks:
            shas = sorted((resolve(repo, c) for c in picks), key=lambda c: int(git(repo, "rev-list", "--count", c)))
            results.append(attempt("pick", picker(shas), {"source": "git cherry-pick", "picks": shas}))
        for i, path in enumerate(patches, 1):
            results.append(attempt(f"patch-{i}", patcher(path), {"source": f"patch {Path(path).name}"}))
        if revert:
            commits = [cul["culprit"], *cul.get("ambiguous_with", [])]
            commits.sort(key=lambda c: int(git(repo, "rev-list", "--count", c)), reverse=True)
            results.append(attempt("revert", reverter(commits), {"source": "git revert", "reverts": commits}))
        if backend is not None:
            context, allowed = code_context(repo, bad, cul["culprit"], cul.get("files", []))
            evidence = (inv_dir / "evidence.md").read_text()
            history: list[dict] = []
            for i in range(1, attempts + 1):
                user = "\n\n".join([evidence, context] + ([feedback(history)] if history else []))
                try:
                    prop = parse(backend.complete(system_prompt(lang), user, SCHEMA))
                except ExplainError as e:
                    results.append({"name": f"llm-{i}", "outcome": "model_error", "error": str(e)})
                    log(t("log.fix.modelerr", i=i, e=e))
                    break
                r = attempt(f"llm-{i}", lambda repo, p=prop: apply_edits(repo, p["edits"], allowed),
                            {"source": f"{backend.name}:{backend.model}", **prop})
                results.append(r)
                if r["outcome"] == "fixes":
                    break
                history.append(r)
    finally:
        git(repo, "checkout", "-q", start_ref)
        lock.close()
    data = {"investigation": stamp, "culprit": cul["culprit"], "targets": targets,
            "wanted": sorted(wanted), "candidates": results}
    (inv_dir / "fix.json").write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str))
    path = inv_dir / "fix.md"
    path.write_text(render(data, lang))
    return path


def render(data: dict, lang: str | None = None) -> str:
    def L(key, **kw):
        return t("fix." + key, lang, **kw)
    out = [L("title"), "", f"_{L('note', n=TRIAGE_RUNS)}_", "",
           f"| {L('cand')} | {L('src')} | {L('out')} | {L('size')} |", "|---|---|---|---|"]
    for c in data["candidates"]:
        outcome = L("outcome." + c["outcome"]) if "fix.outcome." + c["outcome"] in MSG["en"] else c["outcome"]
        out.append(f"| {c['name']} | {c.get('source', '')} | {outcome} | {c.get('diff_lines', '')} |")
    for c in data["candidates"]:
        out += ["", f"## {c['name']}", ""]
        if c.get("rationale"):
            out += [f"**{L('why')}:** {c['rationale']}", ""]
        if c.get("risk"):
            out += [f"**{L('risk')}:** {c['risk']}", ""]
        if c.get("remaining"):
            out += [f"**{L('rem')}:** " + ", ".join(" ".join(map(str, s)) for s in c["remaining"]), ""]
        if c.get("new"):
            out += [f"**{L('new')}:** " + ", ".join(" ".join(map(str, s)) for s in c["new"]), ""]
        if c.get("unconfirmed"):
            out += [f"**{L('unconf')}:** " + ", ".join(" ".join(map(str, s)) for s in c["unconfirmed"]), ""]
        if c.get("error"):
            out += [f"**{L('err')}:**", "```", c["error"][-800:], "```", ""]
        if c.get("sha"):
            out += [f"`fix/{c['name']}.diff` ({c['sha'][:10]})", ""]
    if any(c.get("full_battery") is False for c in data["candidates"]):
        out += ["", f"_{L('partial_note')}_"]
    return "\n".join(out) + "\n"
