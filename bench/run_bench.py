"""Score the deterministic investigation pipeline against the benchmark ground truth.

  python bench/run_bench.py --split dev        # tuning set
  python bench/run_bench.py --split holdout    # only after tuning is frozen
  python bench/run_bench.py guided_ekf_defer   # selected cases

Writes bench/results/<timestamp>.json and prints a scorecard.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forkpilot.i18n import t as msg  # noqa: E402
from forkpilot.investigate import investigate  # noqa: E402

TRUTH = ROOT / "bench" / "truth"
RESULTS = ROOT / "bench" / "results"


def score(truth: dict, rec: dict) -> dict:
    detect = next((s for s in rec["steps"] if s["kind"] == "detect"), None)
    seen = {k: v["verdict"] for k, v in (detect or {}).get("report", {}).items()}
    expected = truth["expect"]
    # the expected scenarios must show the expected verdict; extra symptoms are reported, not penalised
    culprit = (rec.get("culprit") or {}).get("culprit")
    if truth["kind"] == "clean":
        # nothing changed behaviour: the right answer is to find nothing
        detected = rec.get("outcome") == "no_regression"
        extra = {k: v for k, v in seen.items() if v != "PASS"}
    else:
        detected = all(seen.get(k) == v for k, v in expected.items())
        extra = {k: v for k, v in seen.items() if v != "PASS" and k not in expected}
    tests = sum(1 for s in rec["steps"] if s["kind"] == "bisect_test")
    return {"case": truth["case"], "split": truth["split"], "kind": truth["kind"],
            "outcome": rec.get("outcome"), "intermittent": rec.get("intermittent", False),
            "detected": detected, "seen": seen, "extra_symptoms": extra,
            "localized": culprit == truth["culprit"], "bisect_tests": tests,
            "range": truth["commits"], "seconds": rec["steps"][-1]["at_s"] if rec["steps"] else 0,
            "record": rec.get("_path")}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--split", choices=["dev", "holdout"])
    a = ap.parse_args()
    truths = {p.stem: json.loads(p.read_text()) for p in sorted(TRUTH.glob("*.json"))}
    cases = a.cases or [c for c, t in truths.items() if not a.split or t["split"] == a.split]
    rows = []
    for case in cases:
        truth = truths[case]
        print(f"\n=== {case} ===", flush=True)
        try:
            r = investigate(ROOT / "ardupilot", truth["good"], truth["bad"],
                            reported=truth.get("reported", []))
        except Exception:
            # one broken case must not take the whole benchmark down; it scores as a miss
            import traceback
            traceback.print_exc()
            rows.append(score(truth, {"outcome": "crash", "steps": [], "_path": None}))
            continue
        data = json.loads(r.path.read_text())
        data["_path"] = str(r.path.relative_to(ROOT))
        rows.append(score(truth, data))
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(rows, indent=1))

    print(f"\n{msg('bench.col.case'):18} {msg('bench.col.set'):8} {msg('bench.col.kind'):11} "
          f"{msg('bench.col.detected'):9} {msg('bench.col.located'):7} {msg('bench.col.tests'):>5} "
          f"{msg('bench.col.time'):>6}  {msg('bench.col.extra')}")
    for r in rows:
        loc = "—" if r["kind"] == "clean" else ("✓" if r["localized"] else "✗")
        print(f"{r['case']:18} {r['split']:8} {r['kind']:11} {'✓' if r['detected'] else '✗':9} "
              f"{loc:7} {r['bisect_tests']:>5} {r['seconds']:>5.0f}s  {r['outcome']}"
              f"{msg('bench.intermittent') if r['intermittent'] else ''} {r['extra_symptoms'] or ''}")
    n = len(rows)
    bugs = [r for r in rows if r["kind"] != "clean"]
    print(msg("bench.summary", d=sum(r["detected"] for r in rows), n=n, l=sum(r["localized"] for r in bugs),
              b=len(bugs), m=f"{sum(r['seconds'] for r in rows) / 60:.0f}", out=out.relative_to(ROOT)))


if __name__ == "__main__":
    main()
