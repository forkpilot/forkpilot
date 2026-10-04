"""Check out a PX4 commit and build its SITL (SIH) binary, caching what a flight needs by commit.

builds/cache/<sha>/px4/ holds bin/ (the stripped px4 binary and its px4-* command links) and etc/
(ROMFS: rcS, airframes, mixers), because the startup scripts change with the commit as much as the
code does. The build tree itself is reused across commits (ninja rebuilds what changed, ccache
serves the rest) and never cached.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from . import i18n
from .build import CACHE, build_env, git, resolve
from .config import WORK

TARGET = "px4_sitl_default"


def cache_dir(sha: str) -> Path:
    return CACHE / sha / "px4"


def binary_of(sha: str) -> Path:
    return cache_dir(sha) / "bin" / "px4"


def python_bin() -> str:
    """PX4's build needs its own Python packages (kconfiglib, jinja2, empy, pyros-genmsg ...)."""
    return str(Path(os.environ.get("FP_PX4_VENV") or WORK / "builds" / "px4-venv") / "bin")


def px4_env(repo: Path) -> dict:
    env = build_env(repo)
    env["PATH"] = f"{python_bin()}:{env['PATH']}"
    env["LC_ALL"] = "C"
    return env


def update_submodules(repo: Path):
    """Commits months apart pin different submodule versions. Only the submodules this tree already
    has checked out are moved (an `--init` of all of them would fetch NuttX and other firmware-only
    trees); PX4's own CMake checks out whatever else the SITL build needs."""
    out = subprocess.run(["git", "-C", str(repo), "submodule", "status"], capture_output=True,
                         text=True).stdout
    have = [line[1:].split()[1] for line in out.splitlines() if line and line[0] != "-"]
    if have:
        subprocess.run(["git", "-C", str(repo), "submodule", "sync", "-q", "--", *have], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "submodule", "update", "--init", "--recursive", "-q", "--", *have],
                       capture_output=True)


def _configure(repo: Path, bdir: Path, env: dict, nice: list) -> subprocess.CompletedProcess:
    return subprocess.run([*nice, "cmake", "-S", str(repo), "-B", str(bdir), "-G", "Ninja",
                           f"-DCONFIG={TARGET}"], cwd=repo, env=env, capture_output=True, text=True)


def _compile(repo: Path, bdir: Path, env: dict, nice: list, jobs: str) -> subprocess.CompletedProcess:
    return subprocess.run([*nice, "cmake", "--build", str(bdir), "--", f"-j{jobs}"], cwd=repo, env=env,
                          capture_output=True, text=True)


def build(repo: Path, ref: str, log=print) -> tuple[Path | None, dict]:
    """Return (cached px4 binary or None if the build failed, info). Leaves `repo` at `ref`."""
    sha = resolve(repo, ref)
    if binary_of(sha).exists():
        return binary_of(sha), {"sha": sha, "cached": True, "seconds": 0.0}
    if git(repo, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules"):
        raise RuntimeError(f"{repo} has uncommitted changes; refusing to check out {sha[:10]}")
    git(repo, "checkout", "-q", "--detach", sha)
    update_submodules(repo)
    env = px4_env(repo)
    jobs = os.environ.get("FP_BUILD_JOBS", str(min(12, os.cpu_count() or 12)))
    nice = ["nice", "-n", "19"] if os.environ.get("FP_BUILD_NICE", "1") != "0" else []
    bdir = repo / "build" / TARGET
    t = time.time()
    out = ""
    if not (bdir / "build.ninja").exists():
        shutil.rmtree(bdir, ignore_errors=True)
        p = _configure(repo, bdir, env, nice)
        out += p.stdout + p.stderr
    proc = _compile(repo, bdir, env, nice, jobs)
    if proc.returncode != 0:
        # far-apart commits differ in Kconfig options, generated uORB headers and CMake modules,
        # and a tree configured for another commit can fail without saying why: one clean
        # configure before calling the commit unbuildable
        out += proc.stdout[-20000:] + proc.stderr + "\n--- clean reconfigure ---\n"
        shutil.rmtree(bdir, ignore_errors=True)
        p = _configure(repo, bdir, env, nice)
        out += p.stdout[-5000:] + p.stderr
        proc = _compile(repo, bdir, env, nice, jobs)
    info = {"sha": sha, "cached": False, "seconds": round(time.time() - t, 1)}
    (CACHE / sha).mkdir(parents=True, exist_ok=True)
    out += proc.stdout[-50000:] + proc.stderr
    (CACHE / sha / "build-px4.log").write_text(out)
    if proc.returncode != 0 or not (bdir / "bin" / "px4").exists():
        info["error"] = (proc.stdout + proc.stderr)[-3000:]
        if log:
            log(i18n.t("log.build.failed", sha=sha[:10] + " (px4)", s=info["seconds"]))
        return None, info
    save(bdir, cache_dir(sha))
    if log:
        log(f"build {sha[:10]} (px4) ok ({info['seconds']} s)")
    return binary_of(sha), info


def save(bdir: Path, dest: Path):
    """Copy bin/ (links kept, the binary stripped: debug info is 3/4 of it) and etc/ into the cache,
    via a temporary directory so a half-written entry is never taken for a build."""
    tmp = dest.with_name(f"px4.tmp{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    shutil.copytree(bdir / "bin", tmp / "bin", symlinks=True)
    shutil.copytree(bdir / "etc", tmp / "etc", symlinks=False)
    subprocess.run(["strip", "--strip-debug", str(tmp / "bin" / "px4")], capture_output=True)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
