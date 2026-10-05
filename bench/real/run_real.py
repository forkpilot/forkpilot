"""Score ForkPilot on real regressions from ArduPilot history (bench/real/cases.json).

  python bench/real/run_real.py poshold_brake_units      # selected cases
  python bench/real/run_real.py --kind escaped            # all cases of one kind
  python bench/real/run_real.py --cases-file bench/real/candidates_new.json --suite scenarios

A case may name its vehicle (copter, plane, quadplane). Plane and QuadPlane cases build and fly
`--vehicle plane`; ArduPilot's autotest suite is Copter only, so it skips them.

For each case: baseline = culprit's parent, candidate = culprit, ArduPilot's own Copter autotest
suite pinned to the parent. Builds happen in a separate worktree (builds/src) so the main
checkout is never moved. Two questions per case:

  upstream  would autotest's own pass/fail have caught it?  (a test that passes on the parent
            fails consistently on the culprit; preexisting/flaky failures do not count)
  forkpilot did ForkPilot find a reproducible behaviour change (DRIFT or FAIL) on the culprit?

For a negative case the right answer is to find nothing. Results: bench/real/results/*.json.
--suite scenarios flies ForkPilot's own scenarios instead (no upstream column then).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from forkpilot.build import build, git  # noqa: E402
from forkpilot.i18n import t as msg  # noqa: E402
from forkpilot.investigate import investigate  # noqa: E402

CASES = ROOT / "bench" / "real" / "cases.json"
RESULTS = ROOT / "bench" / "real" / "results"
SRC = ROOT / "builds" / "src"


def series(sha: str, fixes=()) -> tuple[str, str]:
    """A unit-conversion PR lands as dozens of commits with one commit time, and the ones in the
    middle often do not build. A fork merging upstream takes the series whole, so the case
    becomes: the commit before the series against the last commit of the series. When the
    series also carries the fix (review feedback inside the same PR), it ends before the fix:
    otherwise the "bad" end is already fixed."""
    fixes = [git(SRC, "rev-parse", f) for f in fixes]
    when = git(SRC, "log", "-1", "--format=%ct", sha)
    start = sha
    while git(SRC, "log", "-1", "--format=%ct", f"{start}^1") == when:
        start = git(SRC, "rev-parse", f"{start}^1")
    after = git(SRC, "rev-list", "--reverse", "--first-parent", "--ancestry-path",
                f"{sha}..origin/master").split()
    end = sha
    for c in after:
        if git(SRC, "log", "-1", "--format=%ct", c) != when or c in fixes:
            break
        end = c
    return git(SRC, "rev-parse", f"{start}^1"), end


def vehicle_of(case: dict) -> str:
    """ForkPilot's vehicle key: QuadPlane is a frame of the plane vehicle."""
    v = case.get("vehicle", "copter")
    return "plane" if v in ("plane", "quadplane") else v


def score(case: dict, rec: dict, suite: str) -> dict:
    steps = {s["kind"]: s for s in rec.get("steps", [])}
    report = steps.get("detect", {}).get("report", {})
    tri = steps.get("triage", {}).get("scenarios", {})
    real = {k: t for k, t in tri.items() if t["class"] in ("consistent", "intermittent")}
    # upstream's verdict: autotest's own pass/fail, i.e. our `completed` rule
    upstream = sorted(k for k, t in real.items() if t["rule_fail"]) if suite == "autotest" else None
    found = rec.get("outcome") == "localized"
    hit = steps.get("bisect", {})
    named = [hit.get("culprit")] + hit.get("ambiguous_with", [])
    # did bisect name the commit that introduced the bug (or a set of unbuildable commits
    # that includes it)?
    culprit_hit = any(c and c.startswith(case["culprit"][:10]) for c in named)
    row = {"case": case["id"], "kind": case["kind"], "split": case["split"], "suite": suite,
           "vehicle": case.get("vehicle", "copter"),
           "outcome": rec.get("outcome"),
           "upstream_caught": None if upstream is None else bool(upstream), "upstream_tests": upstream,
           "forkpilot_found": found, "culprit_hit": culprit_hit,
           "symptoms": {k: report.get(k, {}).get("findings", []) for k in real},
           "noise": sorted(k for k, t in tri.items() if t["class"] not in ("consistent", "intermittent")),
           "seconds": rec["steps"][-1]["at_s"] if rec.get("steps") else 0,
           "record": rec.get("_path")}
    row["correct"] = (not found) if case["kind"] == "negative" else found
    return row


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--kind", choices=["escaped", "series", "negative"])
    ap.add_argument("--split", help="dev, holdout, holdout2, ...")
    ap.add_argument("--cases-file", type=Path, default=CASES)
    ap.add_argument("--suite", choices=["autotest", "scenarios"], default="autotest")
    a = ap.parse_args()
    cases = json.loads(a.cases_file.read_text())["cases"]
    cases = [c for c in cases if (not a.cases or c["id"] in a.cases)
             and (not a.kind or c["kind"] == a.kind) and (not a.split or c["split"] == a.split)]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    rows = []
    for case in cases:
        vehicle = vehicle_of(case)
        if a.suite == "autotest" and vehicle != "copter":
            print(f"\n=== {case['id']}: skipped, autotest suite is Copter only ===", flush=True)
            continue
        culprit = case["culprit"]
        parent = git(SRC, "rev-parse", f"{culprit}^1")
        good, bad = parent, culprit
        print(f"\n=== {case['id']} ({case['kind']}) {culprit[:10]} ===", flush=True)
        try:
            if (build(SRC, parent, log=None, vehicle=vehicle)[0] is None
                    or build(SRC, culprit, log=None, vehicle=vehicle)[0] is None):
                good, bad = series(culprit, [f for f in str(case.get("fix") or "").split(",") if f])
                n = len(git(SRC, "rev-list", f"{good}..{bad}").split())
                print(msg("bench.series", good=good[:10], bad=bad[:10], n=n), flush=True)
            r = investigate(SRC, good, bad, suite=a.suite, vehicle=vehicle)
            data = json.loads(r.path.read_text())
            data["_path"] = str(r.path.relative_to(ROOT))
        except Exception:
            import traceback
            traceback.print_exc()
            data = {"outcome": "crash", "steps": [], "_path": None}
        rows.append({**score(case, data, a.suite), "good": good, "bad": bad})
        out.write_text(json.dumps(rows, indent=1))   # partial results survive a long run
        r = rows[-1]
        up = {None: "-", True: msg("bench.caught"), False: msg("bench.missed")}[r["upstream_caught"]]
        print(msg("bench.case", up=up, fp=msg("bench.found" if r["forkpilot_found"] else "bench.notfound"),
                  outcome=r["outcome"], m=f"{r['seconds'] / 60:.0f}"), flush=True)

    print(f"\n{msg('bench.col.case'):24} {msg('bench.col.kind'):9} {'upstream':9} {'forkpilot':10} "
          f"{msg('bench.col.symptoms')}")
    for r in rows:
        print(f"{r['case']:24} {r['kind']:9} {({None: '-', True: '✓', False: '✗'})[r['upstream_caught']]:9} "
              f"{'✓' if r['forkpilot_found'] else '✗':10} {r['outcome']} "
              f"{list(r['symptoms'])[:4]}")
    print("\n" + msg("bench.results", out=out.relative_to(ROOT)))


if __name__ == "__main__":
    main()
