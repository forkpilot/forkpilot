"""Coverage of the weekly backfill ranges: how much of a week's changed code the scenarios run.

  python bench/coverage_weeks.py [--vehicle copter] [--limit N]

Reads $FP_HOME/nightly/backfill.jsonl (from `nightly --backfill`), measures `forkpilot.coverage`
for each weekly range of the vehicle (newest first), and appends one JSON line per range to
bench/coverage_weeks.jsonl (resumable). About 3 min per range.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forkpilot import coverage  # noqa: E402
from forkpilot.nightly import nightly_dir  # noqa: E402

REPO = ROOT / "ardupilot"
OUT = ROOT / "bench" / "coverage_weeks.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicle", default="copter", choices=("copter", "plane"))
    ap.add_argument("--limit", type=int, default=0, help="at most N ranges this run")
    a = ap.parse_args()
    rows = [json.loads(x) for x in (nightly_dir() / "backfill.jsonl").read_text().splitlines() if x.strip()]
    weeks = {(r["good"], r["bad"]): r for r in rows
             if r["vehicle"] == a.vehicle and r.get("outcome") and r.get("commits", 0) > 30}
    done = set()
    if OUT.exists():
        done = {(r["good"], r["bad"], r["vehicle"]) for r in map(json.loads, OUT.read_text().splitlines())}
    todo = sorted((r for k, r in weeks.items() if (*k, a.vehicle) not in done), key=lambda r: r["day"], reverse=True)
    for r in todo[:a.limit or None]:
        t0 = time.time()
        print(f"== {r['day']} {r['good'][:10]}..{r['bad'][:10]} ({r['commits']} commits)", flush=True)
        row = {"day": r["day"], "vehicle": a.vehicle, "good": r["good"], "bad": r["bad"],
               "commits": r["commits"], "outcome": r["outcome"]}
        try:
            res = coverage.measure(REPO, r["good"], r["bad"], a.vehicle, log=None)
            per_file = {f: [x["code"], x["run"]] for f, x in res["files"].items() if x["built"]}
            row.update(code=res["code_lines"], run=res["run_lines"], not_built=len(res["not_built"]),
                       failed=res["failed"], files=per_file)
        except Exception as e:      # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {str(e)[-300:]}"
        row["seconds"] = round(time.time() - t0, 1)
        print(json.dumps({k: v for k, v in row.items() if k != "files"}), flush=True)
        with open(OUT, "a") as f:
            f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
