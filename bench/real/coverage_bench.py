"""Check coverage on the real benchmark: did the scenarios that detected a regression run the
culprit's changed lines, and did any scenario run them for the regressions ForkPilot missed?

  python bench/real/coverage_bench.py [CASE ...]        # default: every case of both case files

Per case: `forkpilot.coverage` of culprit^..culprit (a series case whose culprit does not build:
of the series range), on the case's vehicle with the shipped scenarios. Joined with the latest
`--suite scenarios` result of the case (bench/real/results/*.json): which scenarios found it.
Resumable: one JSON line per case in bench/real/results/coverage.jsonl; a case already there is
skipped. About 3-4 min per case (one coverage build, one flight per scenario).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from forkpilot import coverage  # noqa: E402
from forkpilot.build import git  # noqa: E402
from run_real import series, vehicle_of  # noqa: E402

REPO = ROOT / "ardupilot"
FILES = [ROOT / "bench" / "real" / "cases.json", ROOT / "bench" / "real" / "candidates_new.json"]
RESULTS = ROOT / "bench" / "real" / "results"
OUT = RESULTS / "coverage.jsonl"


def cases() -> list[dict]:
    out = []
    for f in FILES:
        d = json.loads(f.read_text())
        out += d["cases"] if isinstance(d, dict) else d
    return out


def detections() -> dict[str, dict]:
    """{case: latest scenarios-suite result}"""
    out = {}
    for f in sorted(RESULTS.glob("2*.json")):
        for r in json.loads(f.read_text()):
            if r.get("suite") == "scenarios":
                out[r["case"]] = r
    return out


def one(case: dict, found: dict | None, log=print) -> dict:
    vehicle = vehicle_of(case)
    c = git(REPO, "rev-parse", case["culprit"])
    row = {"case": case["id"], "kind": case["kind"], "split": case["split"], "vehicle": vehicle,
           "culprit": c, "found": None, "detected_by": []}
    if found:
        row["found"] = found.get("forkpilot_found")
        row["detected_by"] = sorted(found.get("symptoms") or {})
    t0 = time.time()
    try:
        res = coverage.measure(REPO, f"{c}^", c, vehicle, log=log)
        row["scope"] = "culprit"
    except coverage.CoverageError as e:
        good, bad = series(c, [f for f in str(case.get("fix") or "").split(",") if f])
        log(f"{case['id']}: culprit does not build ({str(e)[:80]}...), series {good[:10]}..{bad[:10]}")
        res = coverage.measure(REPO, good, bad, vehicle, log=log)
        row["scope"] = "series"
    ran_by = set()
    for x in res["files"].values():
        ran_by |= set(x.get("by_scenario") or {})
    row.update(code=res["code_lines"], run=res["run_lines"], ran_by=sorted(ran_by),
               not_built=len(res["not_built"]), failed=res["failed"],
               files={f: {k: x[k] for k in ("code", "run", "by_scenario") if k in x}
                      for f, x in res["files"].items() if x["built"]},
               seconds=round(time.time() - t0, 1))
    # the check: every scenario that detected the regression ran at least one changed line
    row["detectors_ran_lines"] = all(s in ran_by for s in row["detected_by"]) if row["detected_by"] else None
    return row


def main():
    want = set(sys.argv[1:])
    done = set()
    if OUT.exists():
        done = {json.loads(line)["case"] for line in OUT.read_text().splitlines() if line.strip()}
    found = detections()
    for case in cases():
        if (want and case["id"] not in want) or case["id"] in done:
            continue
        print(f"== {case['id']}", flush=True)
        try:
            row = one(case, found.get(case["id"]))
        except Exception as e:      # noqa: BLE001  one case must not stop the run
            row = {"case": case["id"], "error": f"{type(e).__name__}: {str(e)[-400:]}"}
        print(json.dumps({k: v for k, v in row.items() if k != "files"}), flush=True)
        with open(OUT, "a") as f:
            f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
