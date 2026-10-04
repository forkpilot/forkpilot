"""Score the fix stage on real regressions that ForkPilot localized (bench/real/cases.json).

For each case: the upstream fix commit(s) cherry-picked onto `bad`, and the plain revert. The
upstream fix is ground truth, so the oracle must call it 'fixes' — a check on the fix judge
itself, not only on the candidates. LLM candidates are added with --backend.

  python bench/real/fix_real.py poshold_brake_units=investigations/20261004-011501
  python bench/real/fix_real.py land_noGPS=investigations/20261004-011809@7d69dc29

`@sha,...` names the fix to pick when the investigation found a different bug than its case's.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from forkpilot.explain import make_backend  # noqa: E402
from forkpilot.fix import fix  # noqa: E402
from forkpilot.i18n import t as msg  # noqa: E402

CASES = ROOT / "bench" / "real" / "cases.json"
RESULTS = ROOT / "bench" / "real" / "results"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="case=investigation_dir[@fix_sha,...]")
    ap.add_argument("--repo", default=str(ROOT / "ardupilot"), help="checkout to build in")
    ap.add_argument("--backend", choices=["anthropic", "local"])
    ap.add_argument("--model")
    ap.add_argument("-j", "--jobs", type=int, default=6)
    a = ap.parse_args()
    cases = {c["id"]: c for c in json.loads(CASES.read_text())["cases"]}
    backend = make_backend(a.backend, a.model) if a.backend else None
    rows = []
    for run in a.runs:
        name, inv = run.split("=", 1)
        inv, _, given = inv.partition("@")
        case = cases.get(name, {"split": "dev"})
        if case.get("split") != "dev":
            sys.exit(f"{name}: only dev cases here (holdout stays blind)")
        picks = [c for c in (given or str(case.get("fix") or "")).split(",") if c]
        t = time.time()
        fix(ROOT / inv, backend, picks=picks, repo=Path(a.repo), jobs=a.jobs)
        data = json.loads((ROOT / inv / "fix.json").read_text())
        out = {c["name"]: c["outcome"] for c in data["candidates"]}
        rows.append({"case": name, "investigation": inv, "outcomes": out,
                     "upstream_fix_judged_fixes": out.get("pick") == "fixes" if picks else None,
                     "seconds": round(time.time() - t)})
        print(f"{name:24} {out}", flush=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"fix-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(rows, indent=1))
    print(msg("bench.fixresult", path=path))


if __name__ == "__main__":
    main()
