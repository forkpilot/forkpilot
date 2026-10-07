"""Which changed lines did the flights run? Code coverage (gcov) of a commit range.

"No regression" can mean that the changed code ran and behaved, or that no scenario ran it. This
tells the two apart. The bad commit is built once more with ArduPilot's `--coverage` option in a
separate worktree ($FP_HOME/builds/cov), each scenario flies once, and gcov counts the lines each
flight executed. Against `git diff good..bad` that gives, per changed file:

  run          changed executable lines at least one flight executed (and which scenarios)
  not run      changed executable lines no flight executed: no scenario tested them
  not built    changed files the vehicle's SITL binary does not compile (another vehicle, a
               hardware driver): flying cannot test them

Lines without code (comments, declarations) are not counted. A deleted line has no line in the bad
commit and is not counted either (`deleted_only` hunks are reported as a number).

A coverage flight is as fast as a normal one (SITL runs in lock step; measured 2026-10-06), and
gcov over one flight takes about 1.5 s. Each flight writes its counters to its own directory
(GCOV_PREFIX), so flights run in parallel. Results per (commit, vehicle, scenario file) are cached
in $FP_HOME/coverage/.

  forkpilot coverage --good G --bad B [--repo R] [--vehicle copter] [--scenario NAME ...]
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from . import vehicles
from .build import build_env, git, lock_checkout, python_bin, resolve
from .config import JOBS, SPEEDUP, WORK

TREE = WORK / "builds" / "cov"
CACHE = WORK / "coverage"
SOURCE = re.compile(r"\.(c|cc|cpp|cxx|h|hpp)$")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class CoverageError(RuntimeError):
    pass


# --- what changed --------------------------------------------------------------------------

def changed_lines(repo: Path, good: str, bad: str) -> tuple[dict[str, set[int]], int]:
    """({file: line numbers in `bad` that the range added or changed}, hunks that only delete).
    C/C++ sources only; renames count as changes of the new path."""
    # not git(): a diff may carry bytes that are not UTF-8 (a latin-1 £ in a source file)
    out = subprocess.run(["git", "-C", str(repo), "diff", "-U0", "--no-color", "--no-ext-diff", "-M", good, bad],
                         capture_output=True, text=True, errors="replace", check=True).stdout
    files: dict[str, set[int]] = {}
    deleted_only = 0
    path = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            p = line[4:]
            path = p[2:] if p.startswith("b/") and SOURCE.search(p) else None
        elif path and (m := HUNK.match(line)):
            start, count = int(m[1]), int(m[2]) if m[2] is not None else 1
            if count == 0:
                deleted_only += 1
            else:
                files.setdefault(path, set()).update(range(start, start + count))
    return files, deleted_only


# --- build ---------------------------------------------------------------------------------

def _tree(repo: Path) -> Path:
    if not (TREE / "waf").exists():
        TREE.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "--detach", "-f", str(TREE), "HEAD")
    return TREE


FLAGS = {"CFLAGS": ["-fprofile-arcs", "-ftest-coverage"],
         "CXXFLAGS": ["-fprofile-arcs", "-ftest-coverage"],
         "LINKFLAGS": ["-lgcov", "-coverage"]}


def instrument(cache: Path):
    """Add the coverage flags to a configured waf environment (build/c4che/sitl_cache.py, one
    `NAME = <python literal>` per line)."""
    out = []
    for line in cache.read_text().splitlines():
        name, _, value = line.partition(" = ")
        if name in FLAGS:
            v = ast.literal_eval(value)
            line = f"{name} = {FLAGS[name] + [x for x in v if x not in FLAGS[name]]!r}"
        out.append(line)
    cache.write_text("\n".join(out) + "\n")


def build(repo: Path, ref: str, vehicle: str = "copter", log=print) -> tuple[Path, str]:
    """Build `ref` with coverage in the coverage worktree; (binary, sha). The binary writes its
    counters next to that tree's objects, so it is used in place, never copied to a cache."""
    v = vehicles.get(vehicle)
    if v.autopilot != "ardupilot":
        raise CoverageError("coverage is for ArduPilot SITL only")
    tree = _tree(repo)
    sha = resolve(repo, ref)
    git(tree, "checkout", "-q", "--detach", "-f", sha)
    git(tree, "submodule", "update", "--init", "--recursive", "-q")
    # no ccache: coverage objects carry their notes files (.gcno), a cache hit would not
    env = {**build_env(tree), "PATH": f"{python_bin()}:{os.environ['PATH']}"}
    jobs = os.environ.get("FP_BUILD_JOBS", str(os.cpu_count()))
    cache = tree / "build" / "c4che" / "sitl_cache.py"

    def configure():
        # not `--coverage`: before ArduPilot b3c52b2fcb (2026-09-23) it instruments the configure
        # probes too, they fail to link and the build breaks (byteswap.h redefined). Configure
        # plainly, then add the flags after the checks, as that commit does.
        subprocess.run(["./waf", "configure", "--board", "sitl"], cwd=tree, env=env,
                       capture_output=True, text=True)
        instrument(cache)

    def make():
        return subprocess.run(["./waf", v.target, f"-j{jobs}"], cwd=tree, env=env,
                              capture_output=True, text=True)

    if not cache.exists() or "-ftest-coverage" not in cache.read_text():
        configure()
    proc = make()
    if proc.returncode != 0:    # far-apart commits differ in configure options (as in build.build)
        configure()
        proc = make()
    if proc.returncode != 0:
        raise CoverageError(f"coverage build of {sha[:10]} failed: {(proc.stdout + proc.stderr)[-1500:]}")
    if log:
        log(f"coverage build {sha[:10]} ({v.name}) ok")
    return tree / "build" / "sitl" / "bin" / v.binary, sha


# --- fly and count -------------------------------------------------------------------------

def gcov_lines(tree: Path, gcda_root: Path) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """({file: executed lines}, {file: executable lines}) from the counters under `gcda_root`
    (GCOV_PREFIX of one flight). Files outside the tree (system headers) are left out."""
    tree = tree.resolve()
    gcdas = list(gcda_root.rglob("*.gcda"))
    for g in gcdas:      # gcov looks for the notes file next to the counters
        note = g.with_suffix(".gcno")
        if not note.exists():
            note.symlink_to(Path("/" + str(g.relative_to(gcda_root))).with_suffix(".gcno"))
    hit: dict[str, set[int]] = {}
    exe: dict[str, set[int]] = {}
    if not gcdas:
        return {}, {}
    proc = subprocess.run(["gcov", "--json-format", "--stdout", *map(str, gcdas)],
                          cwd=tree / "build" / "sitl", capture_output=True, text=True)
    for doc in proc.stdout.splitlines():
        if not doc.startswith("{"):
            continue
        d = json.loads(doc)
        cwd = d.get("current_working_directory", "")
        for f in d.get("files", []):
            p = Path(os.path.normpath(os.path.join(cwd, f["file"])))
            if not p.is_relative_to(tree):
                continue
            rel = str(p.relative_to(tree))
            for ln in f["lines"]:
                exe.setdefault(rel, set()).add(ln["line_number"])
                if ln["count"]:
                    hit.setdefault(rel, set()).add(ln["line_number"])
    return ({k: sorted(v) for k, v in hit.items()}, {k: sorted(v) for k, v in exe.items()})


def _key(scenario: Path) -> str:
    return f"{scenario.stem}.{hashlib.sha256(scenario.read_bytes()).hexdigest()[:10]}"


def _job(args):
    scenario, binary, tree, out, instance, speedup = args
    from .battery import claim_instance
    instance, slot = claim_instance(instance)
    gcda = Path(tempfile.mkdtemp(prefix="forkpilot-gcda-"))
    os.environ["GCOV_PREFIX"] = str(gcda)       # this worker runs one flight at a time
    try:
        run = vehicles.run_scenario(scenario, ardupilot=tree, speedup=speedup, instance=instance,
                                    binary=binary)
        hit, exe = gcov_lines(tree, gcda)
        with gzip.open(out, "wt") as f:
            json.dump({"scenario": scenario.stem, "ok": run.ok, "error": run.error,
                       "hit": hit, "executable": exe}, f)
        return scenario.stem, run.ok, run.error
    except Exception as e:      # SITL never came up: infrastructure
        return scenario.stem, False, f"infra: {e}"
    finally:
        slot.close()
        os.environ.pop("GCOV_PREFIX", None)
        shutil.rmtree(gcda, ignore_errors=True)


def fly(binary: Path, sha: str, vehicle: str, scenarios: list[Path], jobs: int = JOBS,
        speedup: int = SPEEDUP, log=print) -> dict[str, dict]:
    """Fly each scenario once with the coverage binary; {scenario: its cached record}."""
    d = CACHE / sha / vehicle
    d.mkdir(parents=True, exist_ok=True)
    todo = [(s, binary, TREE, d / f"{_key(s)}.json.gz", i, speedup)
            for i, s in enumerate(scenarios) if not (d / f"{_key(s)}.json.gz").exists()]
    if todo:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for f in as_completed([pool.submit(_job, w) for w in todo]):
                name, ok, err = f.result()
                if log:
                    log(f"{'ok  ' if ok else 'err '} coverage {name}  {err or ''}")
    out = {}
    for s in scenarios:
        p = d / f"{_key(s)}.json.gz"
        if p.exists():
            with gzip.open(p, "rt") as f:
                out[s.stem] = json.load(f)
    return out


# --- compare -------------------------------------------------------------------------------

def compare(changed: dict[str, set[int]], records: dict[str, dict]) -> dict:
    """Per changed file: executable changed lines, which of them ran (and in which scenarios)."""
    exe: dict[str, set[int]] = {}
    for r in records.values():
        for f, lines in r["executable"].items():
            if f in changed:
                exe.setdefault(f, set()).update(lines)
    files = {}
    for f, lines in sorted(changed.items()):
        if f not in exe:
            files[f] = {"built": False, "changed": len(lines)}
            continue
        code = sorted(lines & exe[f])
        by = {}
        for name, r in records.items():
            n = len(set(code) & set(r["hit"].get(f, ())))
            if n:
                by[name] = n
        ran = sorted(set().union(*(set(r["hit"].get(f, ())) for r in records.values())) & set(code))
        # lines every flight runs (start-up, common code) do not tell scenarios apart
        common = set(code).intersection(*(set(r["hit"].get(f, ())) for r in records.values())) \
            if records else set()
        distinct = {}
        for name, r in records.items():
            n = len((set(code) & set(r["hit"].get(f, ()))) - common)
            if n:
                distinct[name] = n
        files[f] = {"built": True, "changed": len(lines), "code": len(code), "run": len(ran),
                    "not_run": sorted(set(code) - set(ran)), "by_scenario": by,
                    "by_scenario_distinct": distinct}
    built = [x for x in files.values() if x["built"]]
    return {"files": files,
            "code_lines": sum(x["code"] for x in built),
            "run_lines": sum(x["run"] for x in built),
            "not_built": sorted(f for f, x in files.items() if not x["built"]),
            "failed": sorted(n for n, r in records.items() if not r["ok"])}


def ranges(lines: list[int]) -> str:
    """[3, 4, 5, 9] -> '3-5, 9'"""
    out, start = [], None
    for i, n in enumerate(lines):
        if start is None:
            start = n
        if i + 1 == len(lines) or lines[i + 1] != n + 1:
            out.append(str(start) if start == n else f"{start}-{n}")
            start = None
    return ", ".join(out)


def summary_lines(res: dict, top: int = 15) -> list[str]:
    """Files with the most lines not run first; at most `top` files per group (all of them in
    --json)."""
    code, run = res["code_lines"], res["run_lines"]
    pct = f" ({100 * run / code:.0f}%)" if code else ""
    out = [f"changed lines with code: {code}, run in flight: {run}{pct}"]
    files = res["files"]
    missed = sorted((f for f, x in files.items() if x["built"] and x["not_run"]),
                    key=lambda f: -len(files[f]["not_run"]))
    for f in missed[:top]:
        out.append(f"  not run   {f}: {ranges(files[f]['not_run'])}")
    if len(missed) > top:
        rest = sum(len(files[f]["not_run"]) for f in missed[top:])
        out.append(f"  not run   ... {len(missed) - top} more files, {rest} lines")
    ran = sorted((f for f, x in files.items() if x["built"] and x["run"]),
                 key=lambda f: -files[f]["run"])
    for f in ran[:top]:
        x = files[f]
        who = ", ".join(sorted(x["by_scenario"], key=lambda n: -x["by_scenario"][n]))
        out.append(f"  run       {f}: {x['run']}/{x['code']} ({who})")
    if len(ran) > top:
        out.append(f"  run       ... {len(ran) - top} more files")
    if res["not_built"]:
        nb = res["not_built"]
        more = f" ... {len(nb) - top} more" if len(nb) > top else ""
        out.append(f"  not built for this vehicle ({len(nb)} files): {', '.join(nb[:top])}{more}")
    if res.get("deleted_only"):
        out.append(f"  deletions only (not counted): {res['deleted_only']} hunks")
    if res["failed"]:
        out.append(f"  flights that failed (coverage up to the failure): {', '.join(res['failed'])}")
    return out


def brief(res: dict) -> dict:
    """What a record keeps (the full result goes to coverage.json)."""
    return {"good": res["good"], "bad": res["bad"], "code": res["code_lines"], "run": res["run_lines"],
            "not_run_files": sum(1 for x in res["files"].values() if x["built"] and x["not_run"]),
            "not_built": len(res["not_built"]), "failed": res["failed"]}


def top_not_run(res: dict, n: int = 20) -> list[tuple[str, list[int]]]:
    files = res["files"]
    missed = [f for f, x in files.items() if x["built"] and x["not_run"]]
    return [(f, files[f]["not_run"]) for f in sorted(missed, key=lambda f: -len(files[f]["not_run"]))[:n]]


def measure(repo: Path, good: str, bad: str, vehicle: str = "copter", scenarios=None,
            jobs: int = JOBS, log=print, dirs=None) -> dict:
    """`scenarios`, `dirs`: as for battery.scenario_paths (default: the vehicle's shipped set)."""
    from .battery import scenario_paths
    repo = Path(repo).resolve()
    changed, deleted_only = changed_lines(repo, resolve(repo, good), resolve(repo, bad))
    paths = scenario_paths(scenarios, dirs, vehicle)
    res = {"good": resolve(repo, good), "bad": resolve(repo, bad), "vehicle": vehicle,
           "scenarios": [p.stem for p in paths], "deleted_only": deleted_only}
    if not changed:
        return {**res, **compare({}, {})}
    TREE.parent.mkdir(parents=True, exist_ok=True)
    tree = _tree(repo)
    with lock_checkout(tree):
        binary, sha = build(repo, bad, vehicle, log=log)
        records = fly(binary, sha, vehicle, paths, jobs=jobs, log=log)
    return {**res, **compare(changed, records)}
