"""Deterministic investigation of a regression between two firmware commits.

  detect  : run the full battery on `good` (baseline) and `bad`, judge `bad` against it
  triage  : rerun each non-PASS scenario more times — consistent, flaky, or infrastructure?
  bisect  : binary-search the commit range for the first commit that reproduces the symptom

No AI is involved in any of these steps.
"""
from __future__ import annotations

import math
from dataclasses import asdict
from pathlib import Path

from . import metrics, oracle, scenario
import hashlib
import json
import shutil

from .battery import JOBS, ROOT, SCENARIOS, WORK, expect_for, judge, load_metrics, metrics_path, read_run, run_paths
from .suites import SUITES
from .build import build, commits_between, git, lock_checkout, resolve
from .i18n import t
from .record import Record
from .timeline import analyse_dirs

TRIAGE_RUNS = 5
PREEXISTING_MARGIN = 0.5   # rule-break rate increase over the baseline needed to call it new
CONSISTENT = 0.8   # share of non-PASS runs above which a symptom counts as reproducible
MIN_FREE_BYTES = 5e9    # an autotest investigation writes ~1 GB, a new autotest baseline ~0.3 GB
CONFIRM_ALPHA = 0.01   # significance for the confirmation stage (rank test on drifts, Fisher on rule breaks)
MAX_DIFF_LINES = 400
STEP_MISS = 0.02     # intermittent bisect: accepted chance of calling a bad commit good, per step
CONFIRM_MISS = 0.005 # ... and for the commit just below the culprit, which decides the answer
MAX_RUNS = 48
SOAK_RUNS = 24       # extra runs of a scenario CI reported as failing, if the first runs look clean


def runs_needed(hits: int, total: int, miss: float) -> int:
    """Runs so that a symptom seen in hits/total runs on bad commits shows at least once with
    probability 1 - miss. The rate is estimated with one phantom clean run so a lucky early
    estimate (e.g. 2/2) cannot shrink the run count to almost nothing."""
    rate = hits / (total + 1)
    if rate <= 0:
        return MAX_RUNS
    return max(4, min(MAX_RUNS, math.ceil(math.log(miss) / math.log(1 - rate))))


def _summ(report):
    # text for people, data for the report (which also reads records that have only the text)
    return {name: {"verdict": v, "findings": [str(f) for f in fs], "data": [asdict(f) for f in fs]}
            for name, (v, fs) in report.items()}


def triage(rec: Record, suite, binary: Path, targets: list[str], bad_dir: Path,
           base_dir: Path, jobs: int, good_binary: Path | None = None, report=None) -> dict:
    """Rerun each target scenario and judge every run on its own.

    Suites whose detect pass is only a screen (suite.confirm: one candidate run against a
    short baseline, hundreds of tests) also get the baseline rerun on the targets, and a drift
    counts only if the candidate runs as a group sit on the same side of the baseline runs
    (oracle.confirm_shift). Without this, one in ten upstream tests 'drifts' on unchanged
    firmware. With dozens of tests confirmed at once, a few clean 5-vs-5 separations still
    happen by chance, so every confirmed drift is flown once more on both sides and must
    confirm again on those runs alone."""
    results = suite.run(binary, bad_dir, only=targets, n=TRIAGE_RUNS, jobs=jobs,
                        rep_offset=100, log=rec.log)
    confirm = getattr(suite, "confirm", False) and good_binary is not None
    if confirm:
        rec.log(t("log.confirm.baseline", n=len(targets), k=TRIAGE_RUNS))
        suite.run(good_binary, base_dir, only=targets, n=TRIAGE_RUNS, jobs=jobs,
                  rep_offset=100, log=None)
    infra = {name for name, _, ok, err, _ in results if err and err.startswith("infra:")}
    cand, base = load_metrics(bad_dir), load_metrics(base_dir)
    if confirm:
        # judge only the fresh reruns: the screen run is why the test was picked, and the first
        # baseline runs set the band it fell outside of, so both are biased toward a difference
        cand = load_metrics(bad_dir, min_rep=100, max_rep=200)
        base = load_metrics(base_dir, min_rep=100, max_rep=200)

    def shifted(name, cand, base):
        if not (confirm and report is not None):
            return []
        return [list(f.signature()) for f in report[name][1] if f.kind == "drift" and
                oracle.confirm_shift([m.get(f.metric) for m in base.get(name, [])],
                                     [m.get(f.metric) for m in cand.get(name, [])], f.side,
                                     alpha=CONFIRM_ALPHA)]

    first = {name: shifted(name, cand, base) for name in targets}
    again = [name for name in targets if first[name] and name not in infra]
    replicated = {}
    if again:
        rec.log(t("log.confirm.again", names=", ".join(again), k=TRIAGE_RUNS))
        suite.run(binary, bad_dir, only=again, n=TRIAGE_RUNS, jobs=jobs, rep_offset=200, log=None)
        suite.run(good_binary, base_dir, only=again, n=TRIAGE_RUNS, jobs=jobs, rep_offset=200,
                  log=None)
        cand2 = load_metrics(bad_dir, min_rep=200, max_rep=300)
        base2 = load_metrics(base_dir, min_rep=200, max_rep=300)
        replicated = {name: shifted(name, cand2, base2) for name in again}
    out = {}
    for name in targets:
        runs, expect = cand.get(name, []), expect_for(name, suite.dirs)
        bad_runs = sum(oracle.verdict([m], expect, base.get(name))[0] != "PASS" for m in runs)
        rule_runs = sum(bool(oracle.check_rules(m, expect)) for m in runs)
        base_rule_runs = sum(bool(oracle.check_rules(m, expect)) for m in base.get(name, []))
        rate = bad_runs / len(runs) if runs else 0.0
        base_rate = base_rule_runs / len(base.get(name, [])) if base.get(name) else 0.0
        confirmed = [sig for sig in first[name] if sig in replicated.get(name, [])]
        if name in infra:
            cls = "infra"
        elif base_rule_runs and rule_runs / max(len(runs), 1) - base_rate < PREEXISTING_MARGIN:
            # the good commit breaks the same rule about as often: not something this range
            # introduced (common for load-sensitive upstream tests)
            cls = "preexisting"
        elif confirm and (not rule_runs or oracle.fail_rate_p(
                rule_runs, len(runs), base_rule_runs, len(base.get(name, []))) >= CONFIRM_ALPHA):
            # rule failures no more frequent than chance allows: only a confirmed drift counts
            cls = "consistent" if confirmed else "not_reproduced"
        elif rate >= CONSISTENT:
            cls = "consistent"
        elif rule_runs and not base_rule_runs:
            # an absolute rule the baseline never broke now breaks in some runs: a real,
            # timing-dependent regression rather than noise around a statistical band
            cls = "intermittent"
        elif rate > 0:
            cls = "flaky"
        else:
            cls = "not_reproduced"
        out[name] = {"class": cls, "non_pass": bad_runs, "rule_fail": rule_runs,
                     "runs": len(runs), "baseline_rule_fail": base_rule_runs,
                     "baseline_runs": len(base.get(name, []))}
        if confirm:
            out[name]["confirmed"] = confirmed
        rec.log(t("log.triage", name=name, cls=cls, bad=bad_runs, runs=len(runs), base=base_rule_runs,
                  shift=t("log.triage.shift", confirmed=confirmed) if confirm else ""))
    return out


def symptom(report, targets) -> set[tuple]:
    return {(name, *f.signature()) for name in targets for f in report[name][1]}


def per_run_symptom_of(m: dict, name: str, dirs=None) -> set[tuple]:
    return {(name, *f.signature()) for f in oracle.check_rules(m, expect_for(name, dirs))}


def per_run_symptom(run_dir: Path, targets, dirs=None) -> set[tuple]:
    """Rule violations seen in any single run (used for intermittent regressions)."""
    cand = load_metrics(run_dir)
    return {s for name in targets for m in cand.get(name, []) for s in per_run_symptom_of(m, name, dirs)}


def bisect(rec: Record, suite, repo: Path, good: str, bad: str, targets: list[str],
           base_dir: Path, jobs: int, wanted: set[tuple], n: int = 2, per_run: bool = False,
           seen_rate: tuple[int, int] = (0, 0)) -> dict:
    """A commit is 'bad' only if it reproduces the original symptom — same scenario, metric and
    direction — not merely any non-PASS verdict, which noise in another metric can produce.

    Intermittent symptoms (per_run): the baseline never shows them, so one symptomatic run proves
    a commit bad, but a clean set of runs only makes it probably good. The symptom rate is
    re-estimated from every run on a commit known to be bad, and the commit just below the
    culprit must pass a stricter confirmation before the answer is accepted."""
    commits = commits_between(repo, good, bad)
    rec.log(t("log.bisect.start", n=len(commits), sym=sorted(wanted)))
    tested: dict[str, str] = {commits[-1]: "bad"}
    clean_runs: dict[str, int] = {}    # intermittent: symptom-free runs so far, per commit
    hits, total = seen_rate

    def test(sha: str, runs: int, tag: str = "", rep_offset: int = 0) -> str:
        nonlocal hits, total
        binary, info = build(repo, sha, log=rec.log, vehicle=suite.vehicle)
        if binary is None:
            rec.step("bisect_test", sha=sha, result="skip", build=info)
            return "skip"
        out = rec.dir / "bisect" / (sha[:10] + tag)
        suite.run(binary, out, only=targets, n=runs, jobs=jobs, rep_offset=rep_offset, log=None)
        worst, report = judge(out, base_dir, only=targets, dirs=suite.dirs)
        if per_run:
            per = [per_run_symptom_of(m, name, suite.dirs) for name, ms in load_metrics(out).items()
                   if name in targets for m in ms]
            seen = set().union(*per) if per else set()
            k = sum(bool(x & wanted) for x in per)
        else:
            seen = symptom(report, targets)
        result = "bad" if seen & wanted else "good"
        if per_run:
            if result == "bad":
                hits, total = hits + k, total + len(per)
            else:
                clean_runs[sha] = clean_runs.get(sha, 0) + len(per)
        subject = git(repo, "log", "-1", "--format=%s", sha)
        rec.step("bisect_test", sha=sha, subject=subject, result=result, verdict=_summ(report), runs=runs,
                 other_symptoms=sorted(seen - wanted),
                 build={k: v for k, v in info.items() if k != "error"})
        rec.log(t("log.bisect.step", sha=sha[:10], subject=repr(subject[:60]), worst=worst,
                  what=t("log.symptom.yes" if result == "bad" else "log.symptom.no"),
                  runs=t("log.runs", n=runs) if per_run else "", conf=t("log.confirmation") if tag else ""))
        return result

    def step_runs() -> int:
        return runs_needed(hits, total, STEP_MISS) if per_run else n

    lo, hi = -1, len(commits) - 1      # commits[lo] is good (-1 = `good` itself), commits[hi] is bad
    skipped: set[int] = set()
    confirmations = 0
    while True:
        cands = [i for i in range(lo + 1, hi) if i not in skipped]
        if cands:
            mid = min(cands, key=lambda i: abs(i - (lo + hi) / 2))
            res = tested.get(commits[mid]) or test(commits[mid], step_runs())
            tested[commits[mid]] = res
            if res == "skip":
                skipped.add(mid)
            elif res == "bad":
                hi = mid
            else:
                lo = mid
            continue
        if not per_run or lo < 0:
            break
        # the answer hinges on commits[lo] being good: top its clean runs up to the stricter bar
        need = runs_needed(hits, total, CONFIRM_MISS) - clean_runs.get(commits[lo], 0)
        if need <= 0:
            break
        confirmations += 1
        res = test(commits[lo], max(need, 4), tag=f"-c{confirmations}", rep_offset=1000 * confirmations)
        if res == "bad":
            tested[commits[lo]] = "bad"
            hi = lo
            lo = max((i for i in range(-1, hi) if i < 0 or tested.get(commits[i]) == "good"))
    culprit = commits[hi]
    ambiguous = [commits[i] for i in range(lo + 1, hi) if i in skipped]
    files = git(repo, "show", "--name-only", "--format=", culprit).split()
    result = {"culprit": culprit, "subject": git(repo, "log", "-1", "--format=%s", culprit),
              "files": files, "tests": len(tested) - 1 + confirmations, "range": len(commits),
              "ambiguous_with": ambiguous}
    if per_run:
        result["symptom_rate"] = [hits, total]
    rec.log(t("log.bisect.result", sha=culprit[:10], subject=repr(result["subject"]), tests=result["tests"]))
    return result


def write_evidence(rec: Record, repo: Path, report, tri, timelines, result) -> Path:
    """The evidence pack: everything the explainer is allowed to see, as one document."""
    out = [t("ev.title"), ""]
    out += [t("ev.oracle"), ""]
    for name, (v, fs) in report.items():
        out.append(f"- {name}: {v}")
        out += [f"    - {f}" for f in fs]
    out += ["", t("ev.triage"), ""]
    out += [t("ev.triage.line", name=k, cls=v["class"], k=v["non_pass"], n=v["runs"]) for k, v in tri.items()]
    out += ["", t("ev.diverge"), ""]
    for name, text in timelines.items():
        out += ["```", text, "```", ""]
    c = result["culprit"]
    out += [t("ev.bisect"), "",
            t("ev.found", range=result["range"], tests=result["tests"], sha=c[:10], subject=result["subject"])]
    if result["ambiguous_with"]:
        out.append(t("ev.ambiguous", shas=", ".join(x[:10] for x in result["ambiguous_with"])))
    diff = git(repo, "show", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n    %s%n%n%b", c).splitlines()
    if len(diff) > MAX_DIFF_LINES:
        diff = diff[:MAX_DIFF_LINES] + [t("ev.cut", n=len(diff) - MAX_DIFF_LINES)]
    out += ["", "```diff", *diff, "```", ""]
    path = rec.dir / "evidence.md"
    path.write_text("\n".join(out))
    return path


def _explain(rec: Record, kind: str, model: str | None, lang: str):
    """Optional LLM explanation; a failure here never costs the investigation result."""
    from .explain import ExplainError, explain, make_backend
    try:
        rec.log(t("log.explain.written", path=explain(rec.dir, make_backend(kind, model), lang)))
    except ExplainError as e:
        rec.log(t("log.explain.skipped", err=e))


BASELINES = WORK / "results" / "baselines"


def infra_mostly(base_dir: Path) -> set[str]:
    runs: dict[str, list[bool]] = {}
    for p in run_paths(base_dir):
        err = read_run(p).get("error") or ""
        runs.setdefault(p.name.split(".")[0], []).append(err.startswith("infra:"))
    return {name for name, flags in runs.items() if flags and sum(flags) / len(flags) > 0.5}


def baseline_dir(suite, sha: str, only) -> Path:
    h = hashlib.sha256(f"{suite.name}/{suite.baseline_runs}".encode())
    h.update(suite.fingerprint(only))
    return BASELINES / f"{sha[:12]}-{h.hexdigest()[:10]}"


def metrics_hash() -> str:
    h = hashlib.sha256((ROOT / "forkpilot" / "metrics.py").read_bytes())
    h.update((ROOT / "forkpilot" / "plane_metrics.py").read_bytes())
    h.update((ROOT / "forkpilot" / "px4_metrics.py").read_bytes())
    return h.hexdigest()


def baseline(rec: Record, suite, binary: Path, sha: str, only, jobs: int) -> Path:
    """Baseline runs of the good commit, cached by commit + what is flown + settings:
    benchmark cases share a base commit, so this battery would otherwise be re-flown every time."""
    d = baseline_dir(suite, sha, only)
    metrics_version = metrics_hash()
    if (d / "DONE").exists():
        if (d / "DONE").read_text() != metrics_version:
            # telemetry is still valid; only the metric definitions changed
            for p in run_paths(d):
                metrics_path(p).write_text(json.dumps(metrics.compute(read_run(p)), indent=1))
            (d / "DONE").write_text(metrics_version)
            rec.log(t("log.baseline.recomputed", d=d.relative_to(WORK)))
        else:
            rec.log(t("log.baseline.cached", d=d.relative_to(WORK)))
        return d
    shutil.rmtree(d, ignore_errors=True)
    rec.log(t("log.baseline.fly", suite=suite.name))
    suite.run(binary, d, only=only, n=suite.baseline_runs, jobs=jobs, log=rec.log)
    (d / "DONE").write_text(metrics_version)
    return d


def investigate(repo: Path, good: str, bad: str, only=None, jobs: int = JOBS,
                reported=(), suite: str = "scenarios", explain=None, scenario_dirs=None,
                vehicle: str = "copter") -> Record:
    """`reported`: scenarios CI saw fail on `bad`. An intermittent failure may not show in the
    first three runs, so those scenarios are soaked before the regression is called absent.
    `scenario_dirs`: directories with the scenarios to fly (default: the shipped ones); they are
    validated first and recorded, so that `fix` flies the same set."""
    repo = repo.resolve()
    if suite == "scenarios":
        # the directories this run flies, resolved now (the default can include $FP_HOME/scenarios)
        # so the record can name them; a bad scenario fails here, not after a build
        scenario_dirs = [d.resolve() for d in scenario.as_dirs(scenario_dirs)]
        scenario.check(scenario_dirs, only)
    elif scenario_dirs:
        raise scenario.ScenarioSetError("--scenarios applies to the scenarios suite, not to autotest")
    WORK.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(WORK).free
    if free < MIN_FREE_BYTES:
        # a full disk truncates telemetry to empty files and every later step fails on them
        raise RuntimeError(t("err.disk", free=f"{free / 1e9:.1f}", need=f"{MIN_FREE_BYTES / 1e9:.0f}"))
    lock = lock_checkout(repo)
    start_ref = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if start_ref == "HEAD":
        start_ref = resolve(repo, "HEAD")
    good, bad = resolve(repo, good), resolve(repo, bad)
    # recorded unless it is just the shipped set: `fix` flies the same directories
    meta = {"scenario_dirs": [str(d) for d in scenario_dirs]} if scenario_dirs and \
        scenario_dirs != [SCENARIOS.resolve()] else {}
    rec = Record(repo=str(repo), good=good, bad=bad, suite=suite, vehicle=vehicle, **meta)
    suite = SUITES[suite](repo, good, scenario_dirs, vehicle=vehicle)
    try:
        good_bin, info = build(repo, good, log=rec.log, vehicle=vehicle)
        rec.step("build", ref="good", **{k: v for k, v in info.items() if k != "error"})
        bad_bin, info = build(repo, bad, log=rec.log, vehicle=vehicle)
        rec.step("build", ref="bad", **{k: v for k, v in info.items() if k != "error"})
        if good_bin is None or bad_bin is None:
            rec.set(outcome="build_failed")
            return rec

        base_dir, bad_dir = baseline(rec, suite, good_bin, good, only, jobs), rec.dir / "bad"
        rec.set(baseline_dir=str(base_dir))
        dead = infra_mostly(base_dir)
        if dead:
            # a test the harness never managed to run on the good commit says nothing about
            # the bad one, and a hung one costs a full timeout per run
            rec.log(t("log.dead", tests=sorted(dead)))
            rec.step("skipped", tests=sorted(dead), reason="infra on most baseline runs")
            only = [t for t in (only or suite.tests()) if t not in dead]
        rec.log(t("log.candidate", suite=suite.name))
        suite.run(bad_bin, bad_dir, only=only, n=suite.detect_runs, jobs=jobs, log=rec.log)
        worst, report = judge(bad_dir, base_dir, only=only, dirs=suite.dirs)
        soak = [k for k in reported if k in report and report[k][0] == "PASS"]
        if soak:
            rec.log(t("log.soak", soak=soak, n=SOAK_RUNS))
            suite.run(bad_bin, bad_dir, only=soak, n=SOAK_RUNS, jobs=jobs, rep_offset=200, log=None)
            worst, report = judge(bad_dir, base_dir, only=only, dirs=suite.dirs)
        rec.step("detect", worst=worst, report=_summ(report), soaked=soak)
        rec.log(t("log.detect", worst=worst) + " " + ", ".join(f"{k}={v}" for k, (v, _) in report.items()))
        if worst == "PASS":
            rec.set(outcome="no_regression")
            return rec

        targets = [k for k, (v, _) in report.items() if v != "PASS"]
        tri = triage(rec, suite, bad_bin, targets, bad_dir, base_dir, jobs,
                     good_binary=good_bin, report=report)
        rec.step("triage", scenarios=tri)
        real = [k for k, t in tri.items() if t["class"] == "consistent"]
        per_run, n, seen_rate = False, 2, (0, 0)
        wanted = symptom(report, real)
        if getattr(suite, "confirm", False):
            # only the shifts that survived confirmation define the symptom, plus rule breaks
            wanted = {(k, *sig) for k in real for sig in map(tuple, tri[k].get("confirmed", []))}
            wanted |= {(k, *f.signature()) for k in real for f in report[k][1] if f.kind == "rule"}
        if not real:
            real = [k for k, t in tri.items() if t["class"] == "intermittent"]
            if not real:
                rec.set(outcome="not_reproducible", triage=tri)
                return rec
            # enough runs per bisect step that missing the symptom on a bad commit is unlikely;
            # the rate estimate is refined during the bisect
            per_run = True
            wanted = per_run_symptom(bad_dir, real, suite.dirs)
            cand = load_metrics(bad_dir)
            per = [per_run_symptom_of(m, k, suite.dirs) for k in real for m in cand.get(k, [])]
            seen_rate = (sum(bool(x & wanted) for x in per), len(per))
            rec.log(t("log.intermittent", k=seen_rate[0], n=seen_rate[1],
                      r=runs_needed(*seen_rate, STEP_MISS)))

        timelines = {}
        for name in real:
            a, text = analyse_dirs(name, base_dir, bad_dir)
            timelines[name] = text
            rec.step("timeline", scenario=name, first_divergence_t=a["first_divergence_t"],
                     signals=a["signals"])

        result = bisect(rec, suite, repo, good, bad, real, base_dir, jobs, wanted=wanted, n=n,
                        per_run=per_run, seen_rate=seen_rate)
        rec.step("bisect", **result)
        rec.set(outcome="localized", intermittent=per_run,
                symptom={k: report[k][0] for k in real}, culprit=result,
                wanted=sorted(wanted), targets=real)
        write_evidence(rec, repo, report, tri, timelines, result)
        if explain:
            _explain(rec, *explain)
        return rec
    finally:
        git(repo, "checkout", "-q", start_ref)
        lock.close()
