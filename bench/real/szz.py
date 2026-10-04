"""SZZ-style mining of real regressions: for each fix commit, the lines it deletes are blamed
at its parent; the commits that wrote them are culprit candidates. Every candidate is then
read by hand before it becomes a benchmark case (SZZ is noisy: refactors, comment edits).

  python bench/real/szz.py --since 2025-05-01 > bench/real/szz_candidates.tsv
  python bench/real/szz.py --vehicle plane --since 2024-06-01 > bench/real/szz_candidates_plane.tsv
"""
from __future__ import annotations

import argparse
import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT / "ardupilot"
COPTER_PATHS = ["ArduCopter", "libraries/AC_AttitudeControl", "libraries/AC_WPNav", "libraries/AP_Motors",
         "libraries/AP_NavEKF3", "libraries/AP_AHRS", "libraries/AP_Math", "libraries/AP_Mission",
         "libraries/AC_Fence", "libraries/AC_Avoidance", "libraries/AP_Arming",
         "libraries/AC_PrecLand", "libraries/AP_Follow", "libraries/AP_InertialNav",
         "libraries/AC_Autorotation", "libraries/AP_SurfaceDistance"]
# ArduPlane and QuadPlane (QuadPlane lives in ArduPlane/ and reuses the AC_* controllers)
PLANE_PATHS = ["ArduPlane", "libraries/AP_TECS", "libraries/APM_Control", "libraries/AP_L1_Control",
               "libraries/AP_Landing", "libraries/AP_Navigation", "libraries/AP_Soaring",
               "libraries/AP_Mission", "libraries/AP_NavEKF3", "libraries/AP_AHRS",
               "libraries/AP_Math", "libraries/AC_Fence", "libraries/AP_Arming",
               "libraries/AC_AttitudeControl", "libraries/AC_WPNav", "libraries/AP_Motors",
               "libraries/AP_SpdHgtControl", "libraries/AP_Airspeed", "libraries/AP_Terrain",
               "libraries/AP_Avoidance", "libraries/AP_Follow"]
VEHICLE_PATHS = {"copter": COPTER_PATHS, "plane": PLANE_PATHS}
PATHS = COPTER_PATHS
FIX = re.compile(r"\b(fix|fixed|fixes|correct|corrected|bug|wrong|regression|revert)\b", re.I)
SKIP = re.compile(r"\b(comment|typo|spelling|compil|warning|build|doc|param desc|whitespace|"
                  r"log(ging)?|autotest|test)\b", re.I)


def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True).stdout


def deleted_lines(fix):
    """{path: [line numbers in fix^]} for code lines the fix removes or changes."""
    out, path, res = git("diff", "-U0", f"{fix}^", fix, "--", *PATHS), None, {}
    for line in out.splitlines():
        if line.startswith("--- a/"):
            path = line[6:]
        elif line.startswith("@@") and path and path.endswith((".cpp", ".h")):
            m = re.match(r"@@ -(\d+)(?:,(\d+))?", line)
            start, n = int(m.group(1)), int(m.group(2) or 1)
            res.setdefault(path, []).extend(range(start, start + n))
    return res


def blame(fix, path, lines):
    if not lines:
        return []
    args = ["blame", "--porcelain", "-w"]
    for ln in lines:
        args += ["-L", f"{ln},{ln}"]
    out = git(*args, f"{fix}^", "--", path)
    shas, texts = [], {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^([0-9a-f]{40}) \d+ \d+", line)
        if m:
            cur = m.group(1)
        elif line.startswith("\t") and cur:
            t = line[1:].strip()
            if t and not t.startswith(("//", "/*", "*", "#include")):
                shas.append(cur)
            cur = None
    return shas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-05-01")
    ap.add_argument("--rev", default="origin/master")
    ap.add_argument("--vehicle", choices=sorted(VEHICLE_PATHS), default="copter",
                    help="path set to mine (plane covers ArduPlane and QuadPlane)")
    ap.add_argument("--paths", nargs="+", help="explicit path list; overrides --vehicle")
    a = ap.parse_args()
    global PATHS
    PATHS = a.paths or VEHICLE_PATHS[a.vehicle]
    window = set(git("rev-list", "--first-parent", f"--since={a.since}", a.rev).split())
    fixes = git("log", a.rev, "--first-parent", "--no-merges", f"--since={a.since}",
                "--format=%H\t%cd\t%s", "--date=short", "--", *PATHS).splitlines()
    print("fix\tfix_date\tculprit\tculprit_date\tlines\tfix_subject\tculprit_subject")
    for row in fixes:
        sha, date, subj = row.split("\t", 2)
        if not FIX.search(subj) or SKIP.search(subj):
            continue
        votes = Counter()
        for path, lines in deleted_lines(sha).items():
            votes.update(blame(sha, path, lines))
        for culprit, k in votes.most_common(2):
            if culprit not in window:      # outside the history we can build; skip
                continue
            cdate, csubj = git("log", "-1", "--format=%cd\t%s", "--date=short", culprit).strip().split("\t", 1)
            print(f"{sha[:10]}\t{date}\t{culprit[:10]}\t{cdate}\t{k}\t{subj}\t{csubj}")


if __name__ == "__main__":
    main()
