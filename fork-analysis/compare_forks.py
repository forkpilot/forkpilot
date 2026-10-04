"""For forks that look like real work, measure divergence from upstream.

A fork "looks like real work" when it was pushed to well after it was created
(i.e. someone committed to it) and it is owned by an Organization
(companies, universities, student teams), or it has stars/forks of its own.

For each, GitHub's compare API (upstream_default...owner:fork_default) gives:
  ahead_by   -> custom commits on top of upstream
  behind_by  -> upstream commits the fork is missing
  merge_base -> date of the upstream code the fork is built on

Output: data/compare_<repo>.json
"""
import json
import pathlib
import sys
from datetime import datetime, timedelta

from fetch_forks import DATA, get  # reuses the authenticated session

UPSTREAM = {"PX4-Autopilot": ("PX4/PX4-Autopilot", "main"),
            "ardupilot": ("ArduPilot/ardupilot", "master")}
MIN_ACTIVE = timedelta(days=7)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_candidate(f, recent=None):
    """recent: also take any owner's fork pushed since that date (companies often work from
    personal accounts)."""
    if not f["pushed_at"] or not f["created_at"]:
        return False
    active = ts(f["pushed_at"]) - ts(f["created_at"]) > MIN_ACTIVE
    notable = f["owner_type"] == "Organization" or f["stargazers_count"] >= 5 or f["forks_count"] >= 3
    return active and (notable or (recent is not None and f["pushed_at"] >= recent))


def compare(upstream, base, fork):
    owner = fork["owner"]
    url = f"https://api.github.com/repos/{upstream}/compare/{base}...{owner}:{fork['default_branch']}"
    try:
        r = get(url, per_page=1)
    except Exception as e:  # deleted branches, empty forks, huge diffs
        return {"error": str(e)[:120]}
    c = r.json()
    return {
        "ahead_by": c["ahead_by"],
        "behind_by": c["behind_by"],
        "status": c["status"],
        "merge_base_date": c["merge_base_commit"]["commit"]["committer"]["date"],
    }


if __name__ == "__main__":
    recent = sys.argv[sys.argv.index("--recent") + 1] if "--recent" in sys.argv else None
    for name, (upstream, base) in UPSTREAM.items():
        src = DATA / f"forks_{name}.json"
        if not src.exists():
            continue
        forks = json.loads(src.read_text())
        cands = [f for f in forks if is_candidate(f, recent)]
        print(f"{name}: {len(forks)} forks, {len(cands)} candidates", file=sys.stderr)
        out_path = DATA / f"compare_{name}.json"
        done = {r["full_name"]: r for r in json.loads(out_path.read_text())} if out_path.exists() else {}
        for i, f in enumerate(cands):
            if f["full_name"] in done:
                continue
            done[f["full_name"]] = {**f, **compare(upstream, base, f)}
            if i % 50 == 0:
                out_path.write_text(json.dumps(list(done.values()), indent=1))
                print(f"  {i}/{len(cands)}", file=sys.stderr)
        out_path.write_text(json.dumps(list(done.values()), indent=1))
