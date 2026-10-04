"""Fetch every direct fork of PX4 and ArduPilot and cache the metadata.

Output: data/forks_<repo>.json
"""
import json
import pathlib
import subprocess
import sys
import time

import requests

REPOS = ["PX4/PX4-Autopilot", "ArduPilot/ardupilot"]
DATA = pathlib.Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)

TOKEN = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"})

KEEP = ["full_name", "created_at", "pushed_at", "updated_at", "default_branch",
        "stargazers_count", "forks_count", "size", "archived", "description"]


def get(url, **params):
    while True:
        r = S.get(url, params=params, timeout=60)
        if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
            wait = int(r.headers["X-RateLimit-Reset"]) - int(time.time()) + 5
            print(f"rate limited, sleeping {wait}s", file=sys.stderr)
            time.sleep(max(wait, 5))
            continue
        r.raise_for_status()
        return r


def fetch(repo):
    out, page = [], 1
    while True:
        r = get(f"https://api.github.com/repos/{repo}/forks", per_page=100, page=page, sort="oldest")
        batch = r.json()
        if not batch:
            break
        for f in batch:
            row = {k: f.get(k) for k in KEEP}
            row["owner"] = f["owner"]["login"]
            row["owner_type"] = f["owner"]["type"]
            out.append(row)
        if page % 20 == 0:
            print(f"{repo}: {len(out)} forks", file=sys.stderr)
        page += 1
    return out


if __name__ == "__main__":
    for repo in REPOS:
        forks = fetch(repo)
        path = DATA / f"forks_{repo.split('/')[1]}.json"
        path.write_text(json.dumps(forks, indent=1))
        print(f"{repo}: {len(forks)} forks -> {path.name}")
