"""Run ArduPilot's own autotest suite (Tools/autotest) and record each test in ForkPilot's
run format, so the oracle, triage, timeline and bisect work on upstream's tests too.

  python -m forkpilot.autotest_bridge list
  python -m forkpilot.autotest_bridge run --out results/at_base --tests ModeLoiter ModeAltHold

autotest decides pass/fail with its own checks; what it does not do is compare a fork against
its own baseline, tell a flaky test from a real regression, or find the commit. Each test runs
in its own process and working directory, talking to SITL over unix domain sockets, so many
tests can fly at once without port clashes. autotest's automatic retries are switched off:
a retry that passes is exactly how an intermittent regression slips through.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import metrics
from .battery import JOBS, ROOT

RECORD = {"ATTITUDE", "LOCAL_POSITION_NED", "GLOBAL_POSITION_INT", "HEARTBEAT", "VFR_HUD",
          "EKF_STATUS_REPORT", "MISSION_ITEM_REACHED", "SYS_STATUS"}
TEST_TIMEOUT = 600   # wall seconds per test (longest real one ~480 s); a hung SITL must not stall the battery
# tests that cannot say anything about the binary under test in this harness
EXCLUDED = {
    "DefaultIntervalsFromFiles": "writes its config file at the repo root, SITL reads it from "
                                 "its own working directory",
    "Replay": "needs the separately built Replay tool",
    "PeriphMultiUARTTunnel": "builds and flies its own AP_Periph from the working tree, "
                             "not the binary under test",
}


def _autotest_path(repo: Path) -> Path:
    return Path(repo).resolve() / "Tools" / "autotest"


def _worker(binary: str, repo: str, test_name: str, out: str, speedup: int | None):
    """Runs inside the child process: fly one autotest test and write <out> (a run.json)."""
    sys.path.insert(0, str(_autotest_path(Path(repo))))
    import arducopter                      # noqa: E402  (autotest's own module)
    from vehicle_test_suite import Test    # noqa: E402

    opts = {"binary": binary, "logs_dir": os.getcwd()}
    if _has_uds(Path(repo)):
        opts["unix_domain_socket"] = True    # else the parent gave us a network namespace
    if speedup:
        opts["speedup"] = speedup
    suite = arducopter.AutoTestCopter(**opts)
    test = next((t if isinstance(t, Test) else Test(t) for t in suite.tests()
                 if (t.name if isinstance(t, Test) else t.__name__) == test_name), None)
    if test is None:
        raise SystemExit(f"unknown test {test_name}")
    test.attempts = 1

    run = {"scenario": test_name, "events": [], "samples": [], "ok": False, "error": None,
           "source": "autotest"}
    clock = {"last": 0.0, "offset": 0.0, "mode": None, "armed": None}

    def now(msg):
        # sim time from the autopilot's boot clock, kept monotonic across the reboots many
        # tests do (time_boot_ms restarts at zero). Other components (gimbals, peripherals)
        # have boot clocks of their own and must not move this one.
        tb = getattr(msg, "time_boot_ms", None)
        if tb is not None and msg.get_srcComponent() == 1:
            t = tb / 1000 + clock["offset"]
            if t < clock["last"] - 1.0:
                clock["offset"] += clock["last"] - tb / 1000
                t = tb / 1000 + clock["offset"]
            clock["last"] = t
        return clock["last"]

    def hook(mav, msg):
        if msg.get_srcSystem() != 1:
            return
        kind = msg.get_type()
        t = now(msg)
        if kind == "STATUSTEXT":
            run["events"].append((t, "statustext", msg.text))
        elif kind == "HEARTBEAT" and msg.get_srcComponent() == 1:
            armed = bool(msg.base_mode & 128)
            if armed != clock["armed"]:
                if clock["armed"] is not None:
                    run["events"].append((t, "armed" if armed else "disarmed", None))
                clock["armed"] = armed
            mode = mav.flightmode
            if mode != clock["mode"]:
                run["events"].append((t, "mode", mode))
                clock["mode"] = mode
        elif kind == "MISSION_ITEM_REACHED":
            run["events"].append((t, "wp_reached", msg.seq))
        if kind in RECORD:
            d = msg.to_dict()
            d["t"] = t
            run["samples"].append(d)

    suite.install_message_hook(hook)
    started = time.time()
    try:
        results = suite.run_tests([test])
        # run_tests can append framework failures (timeouts, valgrind, ...) after the test
        failed = [r for r in results if not r.passed]
        run["ok"] = not failed
        if failed:
            r = failed[0]
            run["error"] = str(r.reason or r.exception or "failed")[:500]
    except Exception as e:  # autotest itself blew up: an infrastructure problem, not a verdict
        run["error"] = f"infra: {type(e).__name__}: {e}"[:500]
    run["wall_s"] = time.time() - started
    # write then rename: a worker killed at the timeout must not leave a half-written file
    tmp = Path(f"{out}.tmp")
    tmp.write_text(json.dumps(run))
    os.replace(tmp, out)
    # autotest leaves threads (and, after an exception, SITL) behind; the parent kills the
    # whole process group, this process just must not wait for them
    sys.stdout.flush()
    os._exit(0)


def _has_uds(repo: Path) -> bool:
    """Newer autotest can talk to SITL over unix sockets; older versions (before ~2026) only
    over fixed TCP/UDP ports, so parallel tests need a network namespace each instead."""
    return "unix_domain_socket" in (_autotest_path(repo) / "vehicle_test_suite.py").read_text()


def list_tests(repo: Path = ROOT / "ardupilot") -> list[tuple[str, str]]:
    code = (f"import sys; sys.path.insert(0, {str(_autotest_path(repo))!r});"
            "import arducopter, json; from vehicle_test_suite import Test;"
            "s = arducopter.AutoTestCopter('/bin/true');"
            "ts = [t if isinstance(t, Test) else Test(t) for t in s.tests()];"
            "d = s.disabled_tests();"
            "print(json.dumps([(t.name, t.description) for t in ts if t.name not in d]))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return [tuple(x) for x in json.loads(out.stdout.strip().splitlines()[-1])
            if x[0] not in EXCLUDED]


def _kill_strays(work: Path):
    """SITL is started by autotest in a session of its own, out of reach of killpg; anything
    still running in this test's working directory belongs to the test and goes too."""
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if Path(os.readlink(proc / "cwd")).resolve() == work.resolve():
                os.kill(int(proc.name), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def _job(binary: Path, repo: Path, name: str, rep: int, out: Path, speedup: int | None):
    target = out / f"{name}.{rep}.run.json"
    work = Path(tempfile.mkdtemp(prefix=f"fp-at-{name}-"))
    (work / "buildlogs").mkdir()
    env = {**os.environ, "BUILDLOGS": str(work / "buildlogs"), "PYTHONUNBUFFERED": "1",
           "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}"}
    started = time.time()
    with open(out / f"{name}.{rep}.log", "w") as log:
        # own session, so SITL and anything else autotest starts can be killed as a group
        cmd = [sys.executable, "-m", "forkpilot.autotest_bridge", "_worker",
               str(binary), str(repo), name, str(target), str(speedup or 0)]
        if not _has_uds(repo):
            # private loopback per test: every SITL gets its default ports to itself
            cmd = ["unshare", "-rn", "sh", "-c",
                   'ip link set lo up && exec "$@"', "sh", *cmd]
        proc = subprocess.Popen(cmd,
                                cwd=work, env={**env, "PYTHONPATH": str(ROOT)}, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            proc.wait(timeout=TEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    _kill_strays(work)
    subprocess.run(["rm", "-rf", str(work)])
    try:
        run = json.loads(target.read_text())
    except (OSError, ValueError):     # no file, or one cut short by an older worker
        run = {"scenario": name, "events": [], "samples": [], "ok": False,
               "error": "infra: worker died or timed out", "source": "autotest"}
        target.write_text(json.dumps(run))
    (out / f"{name}.{rep}.metrics.json").write_text(json.dumps(metrics.compute(run), indent=1))
    return name, rep, run["ok"], run["error"], time.time() - started


def run_autotests(binary: Path, repo: Path, out: Path, tests: list[str], n: int = 1,
                  jobs: int = JOBS, speedup: int | None = None, rep_offset: int = 0, log=print):
    """Fly each test n times, jobs at a time. Longest tests are not known up front, so the
    order is simply as given."""
    out = Path(out).resolve()     # workers run in their own directories
    out.mkdir(parents=True, exist_ok=True)
    work = [(t, r) for r in range(rep_offset, rep_offset + n) for t in tests]
    results = []
    # each job is a separate process already; threads only wait on them
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = [pool.submit(_job, Path(binary).resolve(), Path(repo).resolve(), t, r, out, speedup)
                for t, r in work]
        for f in as_completed(futs):
            name, rep, ok, err, dt = f.result()
            results.append((name, rep, ok, err, dt))
            if log:
                log(f"{'ok  ' if ok else 'FAIL'} {name}#{rep}  {dt:5.1f}s  {(err or '')[:120]}")
    return results


def main():
    import argparse
    if len(sys.argv) > 1 and sys.argv[1] == "_worker":
        _, _, binary, repo, name, out, speedup = sys.argv
        _worker(binary, repo, name, out, int(speedup) or None)
        return
    ap = argparse.ArgumentParser(prog="forkpilot.autotest_bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--repo", default=str(ROOT / "ardupilot"))
    r = sub.add_parser("run")
    r.add_argument("--repo", default=str(ROOT / "ardupilot"))
    r.add_argument("--binary", help="default: <repo>/build/sitl/bin/arducopter")
    r.add_argument("--out", required=True)
    r.add_argument("--tests", nargs="*", help="default: every enabled Copter test")
    r.add_argument("-n", type=int, default=1)
    r.add_argument("-j", "--jobs", type=int, default=JOBS)
    r.add_argument("--speedup", type=int)
    a = ap.parse_args()
    if a.cmd == "list":
        for name, desc in list_tests(Path(a.repo)):
            print(f"{name:45} {desc}")
        return
    binary = Path(a.binary or Path(a.repo) / "build" / "sitl" / "bin" / "arducopter")
    tests = a.tests or [t for t, _ in list_tests(Path(a.repo))]
    t0 = time.time()
    res = run_autotests(binary, Path(a.repo), Path(a.out), tests, n=a.n, jobs=a.jobs,
                        speedup=a.speedup)
    bad = [x for x in res if not x[2]]
    print(f"\n{len(res) - len(bad)}/{len(res)} passed, {len(bad)} failed · "
          f"{(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
