"""Summarize fork divergence: how many serious forks exist and how stale they are."""
import json
import statistics as st
from datetime import datetime, timezone

from compare_forks import DATA, is_candidate, ts

NOW = datetime.now(timezone.utc)
BUCKETS = [(0.5, "< 6 mo"), (1, "6-12 mo"), (2, "1-2 yr"), (3, "2-3 yr"), (99, "3+ yr")]


def bucket(years):
    return next(label for limit, label in BUCKETS if years < limit)


def summarize(name):
    forks = json.loads((DATA / f"forks_{name}.json").read_text())
    rows = json.loads((DATA / f"compare_{name}.json").read_text())
    ok = [r for r in rows if "ahead_by" in r]
    custom = [r for r in ok if r.get("custom_commits", r["ahead_by"]) > 0]
    recent = [r for r in custom if (NOW - ts(r["pushed_at"])).days < 365]

    print(f"\n=== {name} ===")
    print(f"forks in total:                    {len(forks)}")
    print(f"candidates (active + org/interest): {sum(map(is_candidate, forks))}")
    print(f"compared with upstream:            {len(ok)}")
    print(f"with own commits:                  {len(custom)}")
    print(f"  pushed in the last year:         {len(recent)}")

    if not recent:
        return
    age = [(ts(r["pushed_at"]) - ts(r["merge_base_date"])).days / 365 for r in recent]
    print(f"age of the upstream base at the last push, median: {st.median(age):.1f} years")
    counts = {}
    for a in age:
        counts[bucket(a)] = counts.get(bucket(a), 0) + 1
    for _, label in BUCKETS:
        n = counts.get(label, 0)
        print(f"  {label:8} {n:4}  {'#' * round(60 * n / len(age))}")
    print(f"own commits, median: {st.median(r.get('custom_commits', r['ahead_by']) for r in recent):.0f}")
    on_release = sum(1 for r in recent if r.get("closest_upstream") not in (None, "main", "master"))
    print(f"following a release branch: {on_release}/{len(recent)}")
    print(f"upstream commits missing, median: {st.median(r['behind_by'] for r in recent):.0f}")

    print("\n15 active organization forks with the most own commits:")
    orgs = sorted((r for r in recent if r["owner_type"] == "Organization"),
                  key=lambda r: -r.get("custom_commits", r["ahead_by"]))[:15]
    for r in orgs:
        print(f"  {r['full_name']:45} +{r.get('custom_commits', r['ahead_by']):<5} -{r['behind_by']:<6} "
              f"base {r['merge_base_date'][:10]}  ({r.get('closest_upstream', '?')})")


if __name__ == "__main__":
    for name in ["PX4-Autopilot", "ardupilot"]:
        if (DATA / f"compare_{name}.json").exists():
            summarize(name)
