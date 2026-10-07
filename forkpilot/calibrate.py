"""Measure how much each metric moves between builds whose flight behaviour did not change.

SITL is almost deterministic for one binary, so the spread of a 5-run baseline is tiny. But any
rebuild, even of an unrelated commit, can move a sensitive metric (a plane landing falls into
another of a few clusters, metres apart). The band of a 5-run baseline does not see that, and a
range without a behaviour change gets a DRIFT.

The weekly backfill (`nightly --backfill`) flies one commit per week, and each range's candidate
is the next range's baseline. For every pair of consecutive weeks whose range was not localized,
the difference of the two 3-run means is noise between builds. The tolerance of a metric is
FACTOR times the largest such difference, kept when it is above the default (0.05) and seen in at
least MIN_PAIRS pairs. The oracle widens the band of that metric to it (oracle.noise_tol).

  forkpilot calibrate [--backfill nightly/backfill.jsonl] [--write]
"""
from __future__ import annotations

import json
import math
import statistics as st
from collections import defaultdict
from pathlib import Path

from . import oracle

FACTOR = 1.25
MIN_PAIRS = 20
FIRST_REPS = 100        # reps below this are the detect runs; triage reruns start at 100


def week_means(inv_dir: Path) -> dict[tuple[str, str], float]:
    """Mean of each (scenario, metric) over the detect runs of a range's candidate."""
    runs: dict[str, list[dict]] = defaultdict(list)
    for p in (Path(inv_dir) / "bad").glob("*.metrics.json"):
        name, rep = p.name.split(".")[:2]
        if int(rep) < FIRST_REPS:
            runs[name].append(json.loads(p.read_text()))
    out = {}
    for name, ms in runs.items():
        for k in set().union(*ms) - {"completed"}:
            xs = [m[k] for m in ms if m.get(k) is not None and math.isfinite(m[k])]
            if xs:
                out[(name, k)] = st.fmean(xs)
    return out


def between_builds(rows: list[dict]) -> dict[tuple[str, str], list[float]]:
    """|difference of 3-run means| for consecutive weeks of one vehicle, when the later range was
    not localized (a localized range may be a real change)."""
    diffs: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_vehicle: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        if r.get("investigation") and r.get("outcome"):
            by_vehicle[r["vehicle"]][r["bad"]] = r
    for ranges in by_vehicle.values():
        for r in ranges.values():
            prev = ranges.get(r["good"])
            if not prev or r["outcome"] == "localized":
                continue
            a, b = week_means(prev["investigation"]), week_means(r["investigation"])
            for key in a.keys() & b.keys():
                diffs[key].append(abs(b[key] - a[key]))
    return diffs


def table(diffs, factor: float = FACTOR, min_pairs: int = MIN_PAIRS) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for (name, metric), ds in sorted(diffs.items()):
        tol = factor * max(ds)
        if len(ds) >= min_pairs and tol > 0.05:
            out[name][metric] = round(tol, 3)
    return dict(out)


def main(backfill: Path, write: bool = False, log=print) -> dict:
    rows = [json.loads(line) for line in Path(backfill).read_text().splitlines() if line.strip()]
    diffs = between_builds(rows)
    tol = table(diffs)
    pairs = max((len(d) for d in diffs.values()), default=0)
    for name, metrics in tol.items():
        for metric, v in metrics.items():
            log(f"{name:22} {metric:34} {v:8.3f}")
    days = sorted(r["day"] for r in rows if r.get("day", "").startswith("2"))
    # no local paths: the file ships with the package
    source = f"backfill {days[0]}..{days[-1]}, {len(rows)} ranges" if days else "backfill"
    data = {"source": source, "pairs": pairs, "factor": FACTOR, "min_pairs": MIN_PAIRS,
            "tolerance": tol}
    if write:
        # A/A tolerances are measured by hand (bench/aa_scenarios.md): keep them
        old = json.loads(oracle.NOISE_FILE.read_text()) if oracle.NOISE_FILE.exists() else {}
        data |= {k: old[k] for k in ("aa", "aa_source") if k in old}
        oracle.NOISE_FILE.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        oracle._noise.cache_clear()
        log(f"-> {oracle.NOISE_FILE}")
    return data
