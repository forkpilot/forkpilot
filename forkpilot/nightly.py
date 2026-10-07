"""Nightly check of an upstream branch: investigate the commits that landed since the last
checked one, per vehicle, and keep an honest record ($FP_HOME/nightly).

Nothing leaves the machine except the `git fetch`. State advances for a vehicle only when its
investigation finished with an outcome; an error keeps the old `good`, so the range is retried.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from .build import commits_between, git, resolve
from .config import JOBS, WORK
from .i18n import t

OUTCOMES = ("no_regression", "localized", "not_reproducible", "build_failed")


def nightly_dir() -> Path:
    return WORK / "nightly"


def _investigate(*args, **kw):
    from .investigate import investigate     # imports pymavlink: only when something flies
    return investigate(*args, **kw)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(path)


def _first_good(repo: Path, bad: str, first_range: int) -> str:
    """bad~first_range along the first parent; the root commit if the history is shorter."""
    try:
        return resolve(repo, f"{bad}~{first_range}")
    except subprocess.CalledProcessError:
        return git(repo, "rev-list", "--max-parents=0", bad).split()[-1]


def plan_range(repo: Path, state: dict, branch: str, vehicle: str, suite: str, bad: str,
               first_range: int) -> tuple[str, bool]:
    """(good, first run). A stored good that is no longer behind `bad` (force-push) counts as a first run."""
    last = state.get(branch, {}).get(vehicle, {}).get(suite, {}).get("sha")
    if last:
        try:
            git(repo, "merge-base", "--is-ancestor", last, bad)
            return last, False
        except subprocess.CalledProcessError:
            pass
    return _first_good(repo, bad, first_range), True


def _summary(day: str, branch: str, results: list[dict]) -> str:
    lines = [f"# Nightly {day} ({branch})", ""]
    for r in results:
        lines.append(f"## {r['vehicle']}")
        lines.append(f"- range: {r['good'][:10]}..{r['bad'][:10]} ({r['commits']} commits"
                     f"{', first run' if r['first_run'] else ''})")
        lines.append(f"- outcome: {r['outcome']}")
        if r.get("error"):
            lines.append(f"- error: {r['error']}")
        if r.get("culprit"):
            lines.append(f"- culprit: {r['culprit'][:10]} {r.get('subject', '')}"
                         f"{' (low confidence: ' + r['caution'] + ')' if r.get('caution') else ''}")
        cov = r.get("coverage")
        if cov and cov["code"]:
            lines.append(f"- changed lines run in flight: {cov['run']} of {cov['code']} "
                         f"({100 * cov['run'] / cov['code']:.0f}%)")
        if r.get("investigation"):
            lines.append(f"- investigation: {r['investigation']}")
        lines.append(f"- wall time: {r['wall_s']} s")
        lines.append("")
    return "\n".join(lines)


def run(repo: Path, remote: str = "origin", branch: str = "master", vehicles=("copter",),
        suite: str = "scenarios", max_commits: int = 60, first_range: int = 20,
        jobs: int = JOBS, dry_run: bool = False, log=print, coverage: bool = False) -> list[dict]:
    """Returns one result dict per vehicle."""
    repo = repo.resolve()
    if git(repo, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules"):
        raise RuntimeError(t("nightly.dirty", repo=repo))
    git(repo, "fetch", remote, branch)
    bad = resolve(repo, f"{remote}/{branch}")
    root = nightly_dir()
    state_path = root / "state.json"
    state = load_state(state_path)
    day = time.strftime("%Y-%m-%d")
    results = []
    for vehicle in vehicles:
        good, first = plan_range(repo, state, branch, vehicle, suite, bad, first_range)
        n = len(commits_between(repo, good, bad))
        r = {"day": day, "branch": branch, "remote": remote, "vehicle": vehicle, "suite": suite,
             "good": good, "bad": bad, "commits": n, "first_run": first}
        if good == bad:
            log(t("nightly.none", vehicle=vehicle, sha=bad[:10]))
            r["outcome"] = "no_new_commits"
            results.append(r)
            continue
        log(t("nightly.range", vehicle=vehicle, good=good[:10], bad=bad[:10], n=n))
        if n > max_commits:
            log(t("nightly.long", n=n, max=max_commits))
        if dry_run:
            r["outcome"] = "dry_run"
            results.append(r)
            continue
        t0 = time.time()
        try:
            rec = _investigate(repo, good, bad, suite=suite, vehicle=vehicle, jobs=jobs,
                               **({"coverage": True} if coverage else {}))
            outcome = rec.data.get("outcome")
            if outcome not in OUTCOMES:
                raise RuntimeError(f"investigation ended without an outcome ({rec.path})")
            r["outcome"] = outcome
            r["investigation"] = str(rec.dir)
            culprit = rec.data.get("culprit") or {}
            if culprit:
                r["culprit"], r["subject"] = culprit.get("culprit"), culprit.get("subject")
                if culprit.get("caution"):
                    r["caution"] = culprit["caution"]
            cov = rec.data.get("coverage") or {}
            if cov.get("code") is not None:
                r["coverage"] = {"run": cov["run"], "code": cov["code"]}
            state.setdefault(branch, {}).setdefault(vehicle, {})[suite] = {"sha": bad, "date": day}
            save_state(state_path, state)
        except Exception as e:      # one vehicle's failure must not cost the others
            r["outcome"], r["error"] = "error", f"{type(e).__name__}: {e}"
            log(t("nightly.error", vehicle=vehicle, e=r["error"]))
        r["wall_s"] = round(time.time() - t0, 1)
        results.append(r)
    if dry_run:
        return results
    out = root / day
    out.mkdir(parents=True, exist_ok=True)
    for r in results:
        r.setdefault("wall_s", 0.0)
        (out / f"{r['vehicle']}.json").write_text(json.dumps(r, indent=1))
        with open(root / "log.jsonl", "a") as f:
            f.write(json.dumps(r) + "\n")
        log(t("nightly.result", vehicle=r["vehicle"], outcome=r["outcome"]))
    (out / "summary.md").write_text(_summary(day, branch, results))
    log(t("nightly.summary", path=out / "summary.md"))
    return results


# --- backfill --------------------------------------------------------------------------------------

def day_points(repo: Path, ref: str, since: str, until: str | None = None, step_days: int = 1) -> list[tuple[str, str]]:
    """(day, sha): the last first-parent commit of every `step_days`-th day with commits, preceded by
    the last commit before `since` as the first good."""
    args = ["log", "--first-parent", "--reverse", "--format=%H %cs", f"--since={since} 00:00"]
    if until:
        args.append(f"--until={until} 23:59")
    last: dict[str, str] = {}
    for line in git(repo, *args, ref).splitlines():
        sha, day = line.split()
        last[day] = sha                      # oldest first: the last one per day wins
    days = sorted(last)[step_days - 1::step_days] if step_days > 1 else sorted(last)
    start = git(repo, "rev-list", "-1", "--first-parent", f"--before={since} 00:00", ref).strip()
    return ([("start", start)] if start else []) + [(d, last[d]) for d in days]


MIN_FREE_BACKFILL = 8e9


def prune(rec_dir: Path, baseline: str | None) -> None:
    """After a range without a localized regression: drop raw telemetry, keep record and metrics.
    The baseline of `good` is deleted whole (a later use flies it again), so no cached baseline
    is ever left without the telemetry a timeline needs."""
    for p in Path(rec_dir).rglob("*.run.json.gz"):
        p.unlink()
    if baseline:
        shutil.rmtree(baseline, ignore_errors=True)


def backfill(repo: Path, remote: str = "origin", branch: str = "master", vehicles=("copter",),
             suite: str = "scenarios", since: str = "", until: str | None = None, step_days: int = 1,
             jobs: int = JOBS, dry_run: bool = False, keep: bool = False, log=print,
             coverage: bool = False) -> list[dict]:
    """Check history as if the nightly had run every `step_days` days since `since`. Resumable: a
    (good, bad, vehicle, suite) already in backfill.jsonl with an outcome is skipped. The nightly
    state is not touched."""
    repo = repo.resolve()
    if git(repo, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules"):
        raise RuntimeError(t("nightly.dirty", repo=repo))
    git(repo, "fetch", remote, branch)
    points = day_points(repo, f"{remote}/{branch}", since, until, step_days)
    out = nightly_dir() / "backfill.jsonl"
    done = set()
    if out.exists():
        for line in out.read_text().splitlines():
            r = json.loads(line)
            if r.get("outcome") in OUTCOMES:
                done.add((r["good"], r["bad"], r["vehicle"], r["suite"]))
    results = []
    for (_, good), (day, bad) in zip(points, points[1:]):
        n = len(commits_between(repo, good, bad))
        for vehicle in vehicles:
            if (good, bad, vehicle, suite) in done:
                continue
            if not dry_run and shutil.disk_usage(nightly_dir().parent).free < MIN_FREE_BACKFILL:
                log(t("nightly.disk", free=f"{shutil.disk_usage(nightly_dir().parent).free / 1e9:.1f}"))
                return results
            log(t("nightly.range", vehicle=f"{day} {vehicle}", good=good[:10], bad=bad[:10], n=n))
            r = {"day": day, "branch": branch, "vehicle": vehicle, "suite": suite, "good": good,
                 "bad": bad, "commits": n}
            if dry_run:
                results.append({**r, "outcome": "dry_run"})
                continue
            t0 = time.time()
            try:
                rec = _investigate(repo, good, bad, suite=suite, vehicle=vehicle, jobs=jobs,
                                   **({"coverage": True} if coverage else {}))
                r["outcome"] = rec.data.get("outcome") or "error"
                r["investigation"] = str(rec.dir)
                culprit = rec.data.get("culprit") or {}
                if culprit:
                    r["culprit"], r["subject"] = culprit.get("culprit"), culprit.get("subject")
                if culprit.get("caution"):
                    r["caution"] = culprit["caution"]
                cov = rec.data.get("coverage") or {}
                if cov.get("code") is not None:
                    r["coverage"] = {"run": cov["run"], "code": cov["code"]}
                if r["outcome"] != "localized" and not keep:
                    prune(rec.dir, rec.data.get("baseline_dir"))
            except Exception as e:      # one range's failure must not stop the walk
                r["outcome"], r["error"] = "error", f"{type(e).__name__}: {e}"
                log(t("nightly.error", vehicle=vehicle, e=r["error"]))
            r["wall_s"] = round(time.time() - t0, 1)
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "a") as f:
                f.write(json.dumps(r) + "\n")
            log(t("nightly.result", vehicle=f"{day} {vehicle}", outcome=r["outcome"]))
            results.append(r)
    return results
