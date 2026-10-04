"""Correct ahead_by for forks that track an upstream release branch.

compare_forks.py diffs every fork against upstream main/master. A fork whose
default branch is e.g. Copter-4.6 then shows upstream backports as "custom"
commits. Here each fork is re-compared against every upstream branch that
splits from main at the same point (same merge-base date), and the smallest
ahead_by wins: that is the fork's genuinely custom work.

Adds `custom_commits` and `closest_upstream` to data/compare_<repo>.json.
"""
import json
import re
import sys

from compare_forks import UPSTREAM
from fetch_forks import DATA, get

RELEASE = re.compile(r"^(release/|stable|beta|v\d|"
                     r"(Copter|Plane|Rover|Sub|Tracker|Blimp|ArduPilot|APMrover2|ArduCopter|ArduPlane)-)")


def upstream_branches(repo):
    names, page = [], 1
    while True:
        batch = get(f"https://api.github.com/repos/{repo}/branches", per_page=100, page=page).json()
        if not batch:
            return names
        names += [b["name"] for b in batch if RELEASE.match(b["name"])]
        page += 1


def branch_point(repo, base, branch):
    c = get(f"https://api.github.com/repos/{repo}/compare/{base}...{branch}", per_page=1).json()
    return c["merge_base_commit"]["commit"]["committer"]["date"]


def ahead(repo, branch, fork):
    head = f"{fork['owner']}:{fork['default_branch']}"
    try:
        return get(f"https://api.github.com/repos/{repo}/compare/{branch}...{head}", per_page=1).json()["ahead_by"]
    except Exception:
        return None


for name, (repo, base) in UPSTREAM.items():
    path = DATA / f"compare_{name}.json"
    rows = json.loads(path.read_text())
    todo = [r for r in rows if r.get("ahead_by", 0) > 0]
    points = {}
    for b in upstream_branches(repo):
        if b == base:
            continue
        try:
            points.setdefault(branch_point(repo, base, b), []).append(b)
        except Exception:
            pass
    print(f"{name}: {len(todo)} forks to refine, {sum(map(len, points.values()))} upstream branches",
          file=sys.stderr)
    for r in todo:
        best, best_branch = r["ahead_by"], base
        for b in points.get(r["merge_base_date"], []):
            a = ahead(repo, b, r)
            if a is not None and a < best:
                best, best_branch = a, b
        r["custom_commits"], r["closest_upstream"] = best, best_branch
    for r in rows:
        r.setdefault("custom_commits", r.get("ahead_by", 0))
        r.setdefault("closest_upstream", base)
    path.write_text(json.dumps(rows, indent=1))
