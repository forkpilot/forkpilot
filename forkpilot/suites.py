"""What an investigation flies: ForkPilot's own YAML scenarios, or ArduPilot's autotest suite.

Both produce the same run/metrics files, so judging, triage, timelines and bisect are shared.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .battery import JOBS, ROOT, SPEEDUP, WORK, compact, run_battery, scenario_paths
from .build import git, resolve
from .scenario import ScenarioSetError

AUTOTEST_TREES = WORK / "builds" / "autotest"
# seconds per autotest from earlier passes: longest first, so a 6-min test does not start last
DURATIONS = WORK / "results" / "autotest_durations.json"


def longest_first(tests: list[str]) -> list[str]:
    try:
        known = json.loads(DURATIONS.read_text())
    except (OSError, ValueError):
        known = {}
    return sorted(tests, key=lambda t: -known.get(t, float("inf")))    # unknown may be long


def record_durations(results) -> None:
    try:
        known = json.loads(DURATIONS.read_text())
    except (OSError, ValueError):
        known = {}
    for name, rep, ok, err, dt in results:
        if ok:
            known[name] = round(dt, 1)
    DURATIONS.parent.mkdir(parents=True, exist_ok=True)
    tmp = DURATIONS.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(known, indent=1, sort_keys=True))
    os.replace(tmp, DURATIONS)


class Scenarios:
    name = "scenarios"
    detect_runs = 3
    baseline_runs = 5

    def __init__(self, repo: Path, good: str, dirs=None, vehicle: str = "copter"):
        self.repo, self.vehicle = repo, vehicle
        self.dirs = [Path(d) for d in dirs] if dirs else None     # None: the shipped scenarios
        # older PX4 trees fly much noisier in SIH: a 2025-06 build flown against itself fails 5-39%
        # of 5-vs-3 splits even after triage, so PX4 drifts must confirm like autotest's
        self.confirm = vehicle == "px4"

    def tests(self, only=None) -> list[str]:
        return list(only) if only else [p.stem for p in scenario_paths(dirs=self.dirs, vehicle=self.vehicle)]

    def run(self, binary, out, only=None, n=1, jobs=JOBS, rep_offset=0, log=print):
        res = run_battery(binary, self.repo, out, only=only, n=n, jobs=jobs,
                          rep_offset=rep_offset, log=log, dirs=self.dirs, vehicle=self.vehicle)
        compact(out)
        return res

    def fingerprint(self, only) -> bytes:
        # file name + bytes only, in name order: the directory a scenario lives in must not matter,
        # and for the shipped set this is exactly what baselines cached earlier were keyed by
        h = hashlib.sha256(f"{SPEEDUP}".encode())
        for p in scenario_paths(only, self.dirs, self.vehicle):
            h.update(p.name.encode() + p.read_bytes())
        # the harness decides what is flown and when: a change there needs fresh flights
        for p in ("runner.py", "sitl.py"):
            h.update((ROOT / "forkpilot" / p).read_bytes())
        if self.vehicle != "copter":     # Copter fingerprints stay what they were before Plane
            h.update(self.vehicle.encode())
            harness = ("px4_sitl.py", "px4_runner.py") if self.vehicle == "px4" else ("plane.py",)
            for p in ("vehicles.py", *harness):
                h.update((ROOT / "forkpilot" / p).read_bytes())
        return h.digest()


class Autotest:
    """ArduPilot's own Copter tests. The test code is pinned to the good commit: during a
    bisect the repo checks out older commits, and tests that change along with the firmware
    would make the steps incomparable. Only the binary under test changes."""
    name = "autotest"
    detect_runs = 1    # 400 tests: one pass to find candidates, triage repeats only those
    baseline_runs = 3  # a full pass is ~10 min; noise per test is calibrated A/A (bench/autotest_aa.py)
    confirm = True     # the detect pass is a screen: drifts are confirmed against a rerun baseline

    dirs = None
    vehicle = "copter"

    def __init__(self, repo: Path, good: str, dirs=None, vehicle: str = "copter"):
        if dirs:
            raise ScenarioSetError("--scenarios applies to the scenarios suite, not to autotest")
        if vehicle != "copter":
            raise ValueError("the autotest suite flies ArduPilot's Copter tests only")
        self.repo, self.good = repo, resolve(repo, good)
        self.tree = AUTOTEST_TREES / self.good[:12]
        if not (self.tree / "Tools" / "autotest").exists():
            AUTOTEST_TREES.mkdir(parents=True, exist_ok=True)
            git(repo, "worktree", "add", "--detach", "-f", str(self.tree), self.good)

    def tests(self, only=None) -> list[str]:
        from .autotest_bridge import list_tests
        return list(only) if only else [t for t, _ in list_tests(self.tree)]

    def run(self, binary, out, only=None, n=1, jobs=JOBS, rep_offset=0, log=print):
        from .autotest_bridge import run_autotests
        res = run_autotests(binary, self.tree, out, longest_first(self.tests(only)), n=n,
                            jobs=jobs, rep_offset=rep_offset, log=log)
        record_durations(res)
        compact(out)
        return res

    def fingerprint(self, only) -> bytes:
        h = hashlib.sha256(f"autotest/{self.good}".encode())
        h.update((ROOT / "forkpilot" / "autotest_bridge.py").read_bytes())
        for t in sorted(self.tests(only)):
            h.update(t.encode())
        return h.digest()


SUITES = {"scenarios": Scenarios, "autotest": Autotest}
