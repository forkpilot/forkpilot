"""`forkpilot doctor`: is this machine, and this ArduPilot checkout, ready for an investigation?

Every check returns a Check(status, name, detail, hint). The logic that decides a status is in
small pure functions (versions, memory, disk, jobs, submodule status) so it can be tested offline;
the probes around them only run a command or read a file.
"""
from __future__ import annotations

import importlib.metadata as md
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import CCACHE, JOBS, WORK

MIN_PYTHON = (3, 10)
MIN_FREE_GB = 5.0          # investigate refuses below this (investigate.MIN_FREE_BYTES)
EMPY_PINNED = "3.3.4"      # ArduPilot's waf imports `em` and breaks on empy 4
GB_PER_BUILD_JOB = 0.75    # cc1plus on a Copter translation unit
OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class Check:
    status: str
    name: str
    detail: str = ""
    hint: str = ""


def version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split("."):
        m = re.match(r"\d+", part)
        if not m:
            break
        out.append(int(m.group()))
    return tuple(out)


def check_python(version: tuple = tuple(sys.version_info[:3])) -> Check:
    v = ".".join(map(str, version))
    if tuple(version[:2]) >= MIN_PYTHON:
        return Check(OK, "Python", v)
    return Check(FAIL, "Python", f"{v}, need {'.'.join(map(str, MIN_PYTHON))} or newer",
                 "install a newer Python and make a fresh venv with it")


def check_module(dist: str, why: str, required: bool = True, pin: str | None = None,
                 installed: str | None = ..., hint: str = "") -> Check:
    """`installed`: the distribution's version, None if absent (default: look it up)."""
    if installed is ...:
        try:
            installed = md.version(dist)
        except md.PackageNotFoundError:
            installed = None
    spec = f"{dist}=={pin}" if pin else dist
    label = f"{dist} ({why})"
    if installed is None:
        return Check(FAIL if required else WARN, label, "not installed", hint or f"pip install '{spec}'")
    if pin and version_tuple(installed) != version_tuple(pin):
        return Check(FAIL if required else WARN, label, f"{installed}, need exactly {pin}",
                     f"pip install '{spec}'")
    return Check(OK, label, installed)


def check_pkg_resources(found: bool | None = None) -> Check:
    """Older ArduPilot (DroneCAN's DSDL generator) imports pkg_resources: setuptools before 81."""
    if found is None:
        import importlib.util
        found = importlib.util.find_spec("pkg_resources") is not None
    label = "pkg_resources (build of older ArduPilot)"
    if found:
        return Check(OK, label, "importable")
    return Check(FAIL, label, "not importable: older commits fail in dronecangen",
                 "pip install 'setuptools<81'")


def python_deps() -> list[Check]:
    out = [check_module("pymavlink", "MAVLink to SITL"),
           check_module("PyYAML", "scenario files"),
           check_module("empy", "ArduPilot build", pin=EMPY_PINNED),
           check_module("pexpect", "ArduPilot build"),
           check_pkg_resources()]
    # only the autotest suite imports these (autotest_bridge runs ArduPilot's own test code)
    extra = "pip install -e '.[autotest]' in the ForkPilot clone"
    out += [check_module("MAVProxy", "--suite autotest only", required=False, hint=extra),
            check_module("numpy", "--suite autotest only", required=False, hint=extra)]
    return out


def check_tool(cmd: str, why: str, required: bool = True, hint: str = "") -> Check:
    path = shutil.which(cmd)
    if path:
        return Check(OK, f"{cmd} ({why})", path)
    return Check(FAIL if required else WARN, f"{cmd} ({why})", "not found",
                 hint or "sudo apt install build-essential")


def check_git() -> Check:
    path = shutil.which("git")
    if not path:
        return Check(FAIL, "git", "not found", "sudo apt install git")
    ver = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
    return Check(OK, "git", ver)


def uninitialised_submodules(status_output: str) -> list[str]:
    """Paths from `git submodule status` that are not checked out (line starts with '-')."""
    return [ln[1:].split()[1] for ln in status_output.splitlines() if ln.startswith("-") and len(ln.split()) > 1]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def check_repo(repo: Path, good: str | None = None, bad: str | None = None) -> list[Check]:
    if not repo.is_dir():
        return [Check(FAIL, f"ArduPilot checkout {repo}", "no such directory",
                      "pass your fork's clone with --repo <path>")]
    if _git(repo, "rev-parse", "--git-dir").returncode != 0:
        return [Check(FAIL, f"ArduPilot checkout {repo}", "not a git repository",
                      "clone your fork (git clone <url> <path>) and pass it with --repo")]
    head = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    out = [Check(OK, "ArduPilot checkout", f"{repo} at {head or 'no commits'}")]
    waf = repo / "waf"
    out.append(Check(OK, "waf", str(waf)) if waf.exists() and os.access(waf, os.X_OK) else
               Check(FAIL, "waf", "missing or not executable in the checkout root",
                     "is this the ArduPilot root? (it contains ./waf, ArduCopter/, libraries/)"))
    out.append(Check(OK, "ArduCopter", "ArduCopter/ present") if (repo / "ArduCopter").is_dir() else
               Check(FAIL, "ArduCopter", "no ArduCopter/ directory",
                     "ForkPilot flies arducopter SITL; other vehicles are not supported yet"))
    if not (repo / "Tools" / "autotest" / "default_params" / "copter.parm").exists():
        out.append(Check(FAIL, "copter.parm", "Tools/autotest/default_params/copter.parm missing at HEAD",
                         "SITL defaults are read from there; check out a commit that has it"))
    sub = _git(repo, "submodule", "status")
    missing = uninitialised_submodules(sub.stdout)
    if (repo / "waf").exists() and not (repo / "modules" / "waf" / "waflib").exists():
        missing = sorted(set(missing) | {"modules/waf"})
    out.append(Check(OK, "submodules", "initialised") if not missing else
               Check(FAIL, "submodules", f"not checked out: {', '.join(missing[:4])}"
                     + (" ..." if len(missing) > 4 else ""),
                     f"cd {repo} && git submodule update --init --recursive "
                     "(needs your internal mirror or network once; builds of older commits fetch more)"))
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules").stdout.strip()
    out.append(Check(OK, "working tree", "clean") if not dirty else
               Check(FAIL, "working tree", "uncommitted changes to tracked files",
                     "commit or stash them: investigate refuses to check out commits over them"))
    for label, ref in (("good", good), ("bad", bad)):
        if ref:
            ok = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").returncode == 0
            out.append(Check(OK, f"{label} commit", ref) if ok else
                       Check(FAIL, f"{label} commit", f"{ref} not found in the checkout",
                             "git fetch the branch that holds it, or use a full SHA"))
    if good and bad and all(c.status == OK for c in out[-2:]):
        anc = _git(repo, "merge-base", "--is-ancestor", good, bad).returncode == 0
        out.append(Check(OK, "good is an ancestor of bad", "") if anc else
                    Check(WARN, "good is an ancestor of bad", "it is not",
                          "bisect walks the first-parent line of bad back to good; fine for two branches off one "
                          "base, a mistake if --good and --bad are swapped"))
    return out


def check_ccache(env_no_ccache: str | None = None) -> Check:
    path = shutil.which("ccache")
    if env_no_ccache:
        return Check(WARN, "ccache", "disabled by FP_NO_CCACHE", "unset FP_NO_CCACHE to rebuild faster")
    if path and CCACHE.is_dir():
        return Check(OK, "ccache (optional, faster rebuilds)", path)
    if path:
        return Check(WARN, "ccache (optional, faster rebuilds)",
                     f"installed but {CCACHE} is missing, so ForkPilot will not use it",
                     "install the Debian/Ubuntu package: sudo apt install ccache")
    return Check(WARN, "ccache (optional, faster rebuilds)", "not found", "sudo apt install ccache")


def check_unshare(repo: Path | None, run=subprocess.run) -> Check:
    """autotest suite on older ArduPilot (no unix-socket support) runs each test in its own network namespace."""
    name = "unshare -rn (autotest isolation)"
    needed = True
    if repo and (repo / "Tools" / "autotest" / "vehicle_test_suite.py").exists():
        try:
            from .autotest_bridge import _has_uds     # imports pymavlink: absent, assume it is needed
            needed = not _has_uds(repo)
        except ImportError:
            pass
    try:
        r = run(["unshare", "-rn", "sh", "-c", "ip link set lo up"], capture_output=True, text=True, timeout=20)
        ok = r.returncode == 0
        err = (r.stderr or "").strip().splitlines()[-1:] or [""]
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, err = False, [str(e)]
    if ok:
        return Check(OK, name, "usable" + ("" if needed else " (not needed: this autotest uses unix sockets)"))
    if not needed:
        return Check(OK, name, "not usable, but not needed: this autotest uses unix sockets")
    return Check(WARN, name, f"not usable ({err[0]})",
                 "only matters for --suite autotest on this ArduPilot. Needs util-linux and `ip` "
                 "(iproute2); on Ubuntu 24.04 also: sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0")


def meminfo_gb(text: str) -> float | None:
    for ln in text.splitlines():
        if ln.startswith("MemTotal:"):
            return int(ln.split()[1]) / 1024 / 1024
    return None


def suggest_build_jobs(cpus: int, mem_gb: float | None) -> int:
    if mem_gb is None:
        return cpus
    return max(1, min(cpus, int(mem_gb / GB_PER_BUILD_JOB)))


def check_disk(path: Path, free_bytes: float, min_gb: float = MIN_FREE_GB) -> Check:
    gb = free_bytes / 1e9
    if gb >= min_gb:
        return Check(OK, "free disk", f"{gb:.0f} GB free under {path} (investigate needs {min_gb:.0f} GB)")
    return Check(FAIL, "free disk", f"{gb:.1f} GB free under {path}, investigate needs {min_gb:.0f} GB",
                 "free space, or point FP_HOME at a bigger disk; a SITL build tree takes ~1 GB, "
                 "an autotest baseline ~2 GB")


def check_resources(cpus: int, mem_gb: float | None, flight_jobs: int, build_jobs: int) -> list[Check]:
    ram = f"{mem_gb:.0f} GB RAM" if mem_gb is not None else "RAM unknown"
    out = [Check(OK, "CPU / RAM", f"{cpus} cores, {ram}")]
    sug = suggest_build_jobs(cpus, mem_gb)
    if build_jobs > sug:
        out.append(Check(WARN, "build jobs", f"{build_jobs} (FP_BUILD_JOBS, default: all cores) is a lot for {ram}",
                         f"export FP_BUILD_JOBS={sug}"))
    else:
        out.append(Check(OK, "build jobs", f"{build_jobs} (FP_BUILD_JOBS)"))
    if flight_jobs > cpus:
        out.append(Check(WARN, "flight jobs", f"{flight_jobs} parallel SITLs on {cpus} cores",
                         f"pass -j {cpus} to investigate/run; starved SITLs time out"))
    else:
        out.append(Check(OK, "flight jobs", f"{flight_jobs} parallel SITLs (-j)"))
    return out


def read_meminfo() -> float | None:
    try:
        return meminfo_gb(Path("/proc/meminfo").read_text())
    except OSError:
        return None


def smoke_build(repo: Path, log=print) -> list[Check]:
    """Build HEAD and fly `hover` once. Slow on the first run (a full Copter build)."""
    from .battery import judge, run_battery
    from .build import build, git, lock_checkout, resolve
    try:
        lock = lock_checkout(repo)
    except RuntimeError as e:
        return [Check(FAIL, "build", str(e), "wait for the other ForkPilot process")]
    start = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if start == "HEAD":
        start = resolve(repo, "HEAD")
    try:
        t = time.time()
        log("building SITL for HEAD (a first build takes 10 to 30 minutes, later ones use the cache) ...")
        try:
            binary, info = build(repo, "HEAD", log=None)
        except (RuntimeError, subprocess.CalledProcessError) as e:
            return [Check(FAIL, "build", str(getattr(e, "stderr", None) or e).strip()[-400:],
                          "fix the earlier FAIL items; the full log is in builds/cache/<sha>/build.log")]
        if binary is None:
            tail = info.get("error", "").strip().splitlines()[-6:]
            return [Check(FAIL, "build", "waf failed: " + " | ".join(tail)[-500:],
                          f"full log: {WORK}/builds/cache/{info['sha']}/build.log. 'you need to install empy' / "
                          "'pexpect' / 'pkg_resources' means a missing Python module in the environment ForkPilot runs in")]
        out = [Check(OK, "build", f"{binary} ({'cached' if info['cached'] else str(info['seconds']) + ' s'})")]
        log("flying hover once ...")
        d = Path(tempfile.mkdtemp(prefix="doctor-", dir=WORK))
        try:
            res = run_battery(binary, repo, d, only=["hover"], n=1, jobs=1, log=None)
            name, rep, ok, err, dt = res[0]
            if not ok:
                return out + [Check(FAIL, "fly hover", err or "scenario did not complete",
                                    f"telemetry kept in {d}; is port 5760 free? does the binary start by hand?")]
            worst, report = judge(d, None, only=["hover"])
            shutil.rmtree(d, ignore_errors=True)
        except Exception as e:     # SITL could not start, MAVLink never connected, ...
            return out + [Check(FAIL, "fly hover", f"{type(e).__name__}: {e}"[:400],
                                f"kept in {d}")]
        detail = f"{worst} in {dt:.0f} s"
        return out + [Check(OK if worst == "PASS" else WARN, "fly hover", detail,
                            "" if worst == "PASS" else "; ".join(report["hover"][1])[:300])]
    finally:
        git(repo, "checkout", "-q", start)
        lock.close()


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"[{c.status:^4}] {c.name}" + (f": {c.detail}" if c.detail else ""))
        if c.hint and c.status != OK:
            lines.append(f"       fix: {c.hint}")
    n = {s: sum(c.status == s for c in checks) for s in (OK, WARN, FAIL)}
    lines.append(f"\n{n[OK]} ok, {n[WARN]} warning(s), {n[FAIL]} failed")
    return "\n".join(lines)


def run_checks(repo: Path | None, good=None, bad=None) -> list[Check]:
    checks = [check_python(), *python_deps(), check_git(),
              check_tool("gcc", "compiler"), check_tool("g++", "compiler"), check_ccache(os.environ.get("FP_NO_CCACHE"))]
    if repo is not None:
        checks += check_repo(repo, good, bad)
    checks.append(check_unshare(repo))
    WORK.mkdir(parents=True, exist_ok=True)
    checks.append(check_disk(WORK, shutil.disk_usage(WORK).free))
    cpus = os.cpu_count() or 1
    build_jobs = int(os.environ.get("FP_BUILD_JOBS", cpus))
    checks += check_resources(cpus, read_meminfo(), JOBS, build_jobs)
    return checks


def main(repo: Path | None, good=None, bad=None, build: bool = False) -> int:
    print(f"ForkPilot doctor (work dir {WORK}, set FP_HOME to change it)\n")
    checks = run_checks(repo, good, bad)
    if build:
        if repo is None:
            checks.append(Check(FAIL, "build", "--build needs --repo", "pass --repo <your fork>"))
        elif any(c.status == FAIL for c in checks):
            checks.append(Check(WARN, "build", "skipped: fix the failed items first"))
        else:
            checks += smoke_build(repo)
    print(render(checks))
    if repo is not None:
        print("\nNote: investigate checks out commits in --repo and restores your branch afterwards, and "
              "locks the tree while it runs.\nUse a dedicated clone, not the one you work in "
              "(cheap: clone with --reference <your clone>).")
    return 1 if any(c.status == FAIL for c in checks) else 0
