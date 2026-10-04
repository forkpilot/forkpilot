"""A/A calibration for the autotest suite: judge unchanged firmware against itself.

Every DRIFT or FAIL here is a false alarm: the binary is identical. The rate per metric says
which generic metrics the oracle can trust on upstream tests, and how noisy each test is.

  python bench/autotest_aa.py results/at_master_aa [--base N] [extra candidate dirs...]

--base N: the first N repetitions form the baseline (default: all but the last), every other
repetition is judged against it as a separate candidate.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forkpilot import metrics  # noqa: E402
from forkpilot.battery import judge, metrics_path, read_run, run_paths  # noqa: E402
from forkpilot.i18n import t as msg  # noqa: E402


def split(src: Path, base_reps: set[int], tmp: Path) -> tuple[Path, list[Path]]:
    """Baseline dir from some reps, one candidate dir per remaining rep."""
    base = tmp / "base"
    base.mkdir()
    cands: dict[int, Path] = {}
    for p in run_paths(src):
        name, rep = p.name.split(".")[0], int(p.name.split(".")[1])
        m = metrics.compute(read_run(p))
        if rep in base_reps:
            target = base
        else:
            target = cands.setdefault(rep, tmp / f"cand{rep}")
            target.mkdir(exist_ok=True)
        (target / f"{name}.{rep}.metrics.json").write_text(json.dumps(m))
    return base, list(cands.values())


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("extra", nargs="*")
    ap.add_argument("--base", type=int)
    a = ap.parse_args()
    src = Path(a.src)
    reps = sorted({int(p.name.split(".")[1]) for p in run_paths(src)})
    base_reps = set(reps[:a.base] if a.base else reps[:-1])
    tmp = Path(tempfile.mkdtemp())
    base, cands = split(src, base_reps, tmp)
    for extra in a.extra:
        d = tmp / f"extra-{Path(extra).name}"
        d.mkdir()
        for p in run_paths(Path(extra)):
            m = metrics.compute(read_run(p))
            (d / metrics_path(p).name).write_text(json.dumps(m))
        cands.append(d)

    per_metric, tests_flagged, verdicts = Counter(), Counter(), Counter()
    n_tests = 0
    for c in cands:
        worst, report = judge(c, base)
        n_tests += len(report)
        for name, (v, findings) in report.items():
            verdicts[v] += 1
            if v != "PASS":
                tests_flagged[name] += 1
            for f in findings:
                per_metric[(f.kind, f.metric)] += 1
    print(msg("bench.aa.base", reps=sorted(base_reps), c=len(cands), n=n_tests))
    print(msg("bench.aa.verdicts"), dict(verdicts))
    print("\n" + msg("bench.aa.metric"))
    for (kind, metric), k in per_metric.most_common():
        print(f"  {kind:5} {metric:22} {k:4}  ({k / n_tests:.1%} {msg('bench.aa.ofverdicts')})")
    print("\n" + msg("bench.aa.noisy"))
    for name, k in tests_flagged.most_common(25):
        print(f"  {name:45} {k}/{len(cands)}")
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
