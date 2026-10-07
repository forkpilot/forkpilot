"""A/A calibration for the scenarios suite: judge unchanged firmware against itself.

Fly one binary n times (`forkpilot run --binary B -n 8 --out DIR`), then judge every split of the
repetitions into a 5-run baseline and a 3-run candidate, as investigate's detect step does. Every
DRIFT or FAIL is a false alarm of the first pass, before triage reruns it.

  python bench/scenario_aa.py DIR [--base 5] [--cand 3] [--other DIR2]

--other: candidates come from a second session (another time, another -j); every baseline split
is judged against 8 candidate splits of DIR2. This is closer to investigate, where baseline and
candidate are flown at different times.
"""
from __future__ import annotations

import argparse
import itertools
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forkpilot.battery import judge  # noqa: E402


def reps_of(src: Path) -> dict[int, list[Path]]:
    out: dict[int, list[Path]] = {}
    for p in src.glob("*.metrics.json"):
        out.setdefault(int(p.name.split(".")[1]), []).append(p)
    return out


def fill(d: Path, files) -> Path:
    d.mkdir(parents=True)
    for p in files:
        (d / p.name).symlink_to(p.resolve())
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--base", type=int, default=5)
    ap.add_argument("--cand", type=int, default=3)
    ap.add_argument("--other")
    a = ap.parse_args()
    by_rep = reps_of(Path(a.src))
    reps = sorted(by_rep)
    other = reps_of(Path(a.other)) if a.other else None
    if len(reps) < a.base + (0 if other else a.cand):
        sys.exit(f"not enough repetitions: {len(reps)}")
    flagged, metric_hits, verdicts = Counter(), Counter(), Counter()
    splits = 0
    tmp = Path(tempfile.mkdtemp())
    try:
        for base in itertools.combinations(reps, a.base):
            if other:
                pool, src = sorted(other), other
                cands = list(itertools.islice(itertools.combinations(pool, a.cand), 0, None, 7))[:8]
            else:
                pool, src = [r for r in reps if r not in base], by_rep
                cands = itertools.combinations(pool, a.cand)
            for cand in cands:
                splits += 1
                d = tmp / str(splits)
                b = fill(d / "base", [p for r in base for p in by_rep[r]])
                c = fill(d / "cand", [p for r in cand for p in src[r]])
                _, report = judge(c, b)
                for name, (v, findings) in report.items():
                    verdicts[v] += 1
                    if v != "PASS":
                        flagged[name] += 1
                    for f in findings:
                        metric_hits[(name, f.kind, f.metric)] += 1
                shutil.rmtree(d)
    finally:
        shutil.rmtree(tmp)
    n = sum(verdicts.values())
    print(f"{splits} splits ({a.base} baseline runs vs {a.cand} candidate runs), {n} scenario verdicts")
    print("verdicts:", dict(verdicts), f"false alarm rate per scenario verdict: {(n - verdicts['PASS']) / n:.1%}")
    print("\nscenarios flagged (splits with DRIFT or FAIL):")
    for name, k in flagged.most_common():
        print(f"  {name:24} {k:4}/{splits}  {k / splits:.1%}")
    print("\nmetrics behind the flags:")
    for (name, kind, metric), k in metric_hits.most_common(20):
        print(f"  {name:24} {kind:5} {metric:32} {k}")


if __name__ == "__main__":
    main()
