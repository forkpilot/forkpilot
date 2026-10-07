"""Check out a firmware commit and build its SITL binary, caching binaries by commit."""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import i18n, vehicles
from .config import CCACHE, ROOT, WORK

CACHE = WORK / "builds" / "cache"


# build errors that mean a Python module is missing where ForkPilot runs, not a broken commit
PYTHON_MODULE_ERRORS = ("No module named", "you need to install empy", "please install dronecan")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def resolve(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def lock_checkout(repo: Path):
    """Exclusive use of a checkout for this process: two investigations checking out commits in
    the same tree would build and fly each other's code. Hold the returned file open."""
    f = open(Path(git(repo, "rev-parse", "--absolute-git-dir")) / "forkpilot.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError(f"{repo} is in use by another ForkPilot process")
    return f


def commits_between(repo: Path, good: str, bad: str) -> list[str]:
    """Commits after `good` up to and including `bad`, oldest first."""
    out = git(repo, "rev-list", "--reverse", "--first-parent", f"{good}..{bad}")
    return out.split() if out else []


def python_bin() -> str:
    """Directory whose python3 waf must find first: ForkPilot's own environment, which carries
    the build dependencies (empy, pexpect). Falls back to the repo's .venv for a bare `python3 -m`."""
    if sys.prefix != sys.base_prefix:
        return str(Path(sys.prefix) / "bin")
    return str(ROOT / ".venv" / "bin")


def build_env(repo: Path) -> dict:
    env = {**os.environ, "PATH": f"{python_bin()}:{os.environ['PATH']}"}
    if CCACHE.is_dir() and not os.environ.get("FP_NO_CCACHE"):
        # paths relative to the tree, cwd not hashed: worktrees and checkouts share hits
        env["PATH"] = f"{CCACHE}:{env['PATH']}"
        env.setdefault("CCACHE_BASEDIR", str(repo.resolve()))
        env.setdefault("CCACHE_NOHASHDIR", "1")
    return env


def configured(repo: Path, env: dict) -> bool:
    """True if the tree was configured, with ccache when ccache is in use."""
    cache = repo / "build" / "c4che" / "sitl_cache.py"
    if not cache.exists():
        return False
    return str(CCACHE) not in env["PATH"] or "ccache" in cache.read_text()


def build(repo: Path, ref: str, log=print, vehicle: str = "copter") -> tuple[Path | None, dict]:
    """Return (binary or None if the build failed, info). Leaves `repo` checked out at `ref`.
    One cache dir per commit holds every vehicle's binary and default parameters."""
    v = vehicles.get(vehicle)
    if v.autopilot == "px4":
        from .px4_build import build as px4
        return px4(repo, ref, log=log)
    sha = resolve(repo, ref)
    cached = CACHE / sha / v.binary
    if cached.exists():
        return cached, {"sha": sha, "cached": True, "seconds": 0.0}
    if git(repo, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules"):
        raise RuntimeError(f"{repo} has uncommitted changes; refusing to check out {sha[:10]}")
    git(repo, "checkout", "-q", "--detach", sha)
    # commits months apart pin different submodule versions (mavlink above all)
    git(repo, "submodule", "update", "--init", "--recursive", "-q")
    env = build_env(repo)
    # FP_BUILD_JOBS / nice: builds can share the machine with flights still running
    jobs = os.environ.get("FP_BUILD_JOBS", str(os.cpu_count()))
    nice = ["nice", "-n", "19"] if os.environ.get("FP_BUILD_NICE") else []
    t = time.time()
    configure = [*nice, "./waf", "configure", "--board", "sitl"]
    if not configured(repo, env):     # a fresh worktree, or configured before ccache was used
        subprocess.run(configure, cwd=repo, env=env, capture_output=True, text=True)
    proc = subprocess.run([*nice, "./waf", v.target, f"-j{jobs}"], cwd=repo, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        # far-apart commits differ in configure options, and a stale configuration can also
        # fail without saying so (e.g. after another board was configured in the same tree):
        # one fresh configure before calling the commit unbuildable
        subprocess.run(configure, cwd=repo, env=env, capture_output=True, text=True)
        proc = subprocess.run([*nice, "./waf", v.target, f"-j{jobs}"], cwd=repo, env=env,
                              capture_output=True, text=True)
    info = {"sha": sha, "cached": False, "seconds": round(time.time() - t, 1)}
    (CACHE / sha).mkdir(parents=True, exist_ok=True)
    # copter keeps the log name it always had
    log_name = "build.log" if v.name == "copter" else f"build-{v.name}.log"
    (CACHE / sha / log_name).write_text(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        info["error"] = (proc.stdout + proc.stderr)[-3000:]
        if log:
            log(i18n.t("log.build.failed", sha=sha[:10] + _tag(v), s=info["seconds"]))
            log(i18n.t("log.build.see", path=CACHE / sha / log_name))
            if any(m in info["error"] for m in PYTHON_MODULE_ERRORS):
                log(i18n.t("log.build.pymod"))
        return None, info
    shutil.copy2(repo / "build" / "sitl" / "bin" / v.binary, cached)
    # SITL defaults belong to the binary's commit, not to whatever the checkout holds when it flies
    for d in sorted(v.defaults()):
        if (repo / "Tools" / "autotest" / d).exists():
            shutil.copy2(repo / "Tools" / "autotest" / d, CACHE / sha / Path(d).name)
    if log:
        log(f"build {sha[:10]}{_tag(v)} ok ({info['seconds']} s)")
    return cached, info


def _tag(v) -> str:
    return "" if v.name == "copter" else f" ({v.name})"
