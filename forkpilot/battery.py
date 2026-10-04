"""Run the scenario battery against one SITL binary and judge the results."""
from __future__ import annotations

import fcntl
import gzip
import json
import os
import random
import socket
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import yaml

from . import i18n, metrics, oracle, scenario, vehicles
from .config import JOBS, ROOT, SCENARIOS, SPEEDUP, WORK  # noqa: F401  (re-exported)
from .vehicles import run_scenario

RANK = {"PASS": 0, "DRIFT": 1, "FAIL": 2}


def scenario_dirs() -> list[Path]:
    """The scenario directories of a run that names none: the shipped ones, plus the user's own in
    $FP_HOME/scenarios when that exists. The same name in two of them is an error."""
    return scenario.as_dirs()


def scenario_file(name: str, dirs=None) -> Path:
    found = [d / f"{name}.yaml" for d in scenario.as_dirs(dirs) if (d / f"{name}.yaml").exists()]
    return found[0] if found else SCENARIOS / f"{name}.yaml"


def scenario_paths(only=None, dirs=None, vehicle: str = "copter") -> list[Path]:
    """The scenario files of `dirs` (default: scenario_dirs()): `only` as given, whatever their
    vehicle, in its order; otherwise every scenario of `vehicle`, by name."""
    paths = scenario.paths(only, dirs)
    return paths if only else [p for p in paths if vehicles.scenario_vehicle(p) == vehicle]


# SITL instance I listens on 5760 + 10*I. Two ForkPilot processes on one machine both count from 0,
# and a run that connects to the other's SITL flies the wrong firmware. Instances are claimed
# machine-wide with a lock file held for the whole flight.
SLOTS = Path(tempfile.gettempdir()) / "forkpilot-sitl"


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def claim_instance(start: int = 0, limit: int = 500, free=None):
    """(instance, open lock file) for the first free instance from `start`; keep the file open.
    `free(i)`: whether nothing outside ForkPilot uses instance i's ports (default: ArduPilot's)."""
    SLOTS.mkdir(exist_ok=True)
    for i in range(start, start + limit):
        f = open(SLOTS / f"{i % limit}.lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            continue
        if (free or (lambda j: _port_free(5760 + 10 * j)))(i % limit):     # SITL started outside ForkPilot
            return i % limit, f
        f.close()
    raise RuntimeError("no free SITL instance")


def _px4_free(i: int) -> bool:
    from .px4_sitl import ports, udp_free
    return _port_free(5760 + 10 * i) and all(udp_free(p) for p in ports(i).values())


def _job(args):
    scenario, firmware, binary, out_dir, rep, instance, speedup = args
    instance, slot = claim_instance(instance, free=_px4_free if vehicles.scenario_vehicle(scenario) == "px4" else None)
    # stagger starts: runs launched in the same instant share timing and look identical,
    # which makes the baseline band look tighter than run-to-run variation really is
    time.sleep(random.Random(rep * 7919 + instance).uniform(0, 2.0))
    t = time.time()
    try:
        run = run_scenario(scenario, ardupilot=firmware, speedup=speedup, instance=instance,
                           binary=binary)
    except Exception as e:  # SITL never came up: infrastructure, not firmware behaviour
        return scenario.stem, rep, False, f"infra: {e}", time.time() - t
    finally:
        slot.close()
    run.save(out_dir / f"{scenario.stem}.{rep}.run.json")
    m = metrics.compute(run.__dict__)
    (out_dir / f"{scenario.stem}.{rep}.metrics.json").write_text(json.dumps(m, indent=1))
    return scenario.stem, rep, run.ok, run.error, time.time() - t


def run_battery(binary: Path | None, firmware: Path, out: Path, only=None, n=3, jobs=JOBS,
                speedup=SPEEDUP, rep_offset=0, log=print, dirs=None,
                vehicle: str = "copter") -> list[tuple]:
    paths = scenario_paths(only, dirs, vehicle)
    flown = vehicles.of_binary(binary) if binary else None
    wrong = [p.stem for p in paths if flown and vehicles.scenario_vehicle(p) != flown.name]
    if wrong:
        raise ValueError(f"{Path(binary).name} cannot fly {', '.join(wrong)} (other vehicle)")
    out.mkdir(parents=True, exist_ok=True)
    pairs = [(s, r) for s in paths for r in range(rep_offset, rep_offset + n)]
    work = [(s, Path(firmware).resolve(), binary, out, r, i, speedup)
            for i, (s, r) in enumerate(pairs)]
    results = []
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for f in as_completed([pool.submit(_job, w) for w in work]):
            name, rep, ok, err, dt = f.result()
            results.append((name, rep, ok, err, dt))
            if log:
                log(f"{'ok  ' if ok else i18n.t('log.run.err')} {name}#{rep}  {dt:5.1f}s  {err or ''}")
    return results


def run_paths(d: Path, scenario: str = "*") -> list[Path]:
    """Telemetry files, one per run: <name>.<rep>.run.json, or .run.json.gz once compacted.
    A plain file is newer than a compacted one of the same run (a rerun into the same dir)."""
    out = {p.name[:-3]: p for p in Path(d).glob(f"{scenario}.*.run.json.gz")}
    out.update({p.name: p for p in Path(d).glob(f"{scenario}.*.run.json")})
    return [out[k] for k in sorted(out)]


def read_run(p: Path) -> dict:
    p = Path(p)
    return json.loads(gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_text())


def metrics_path(p: Path) -> Path:
    return p.with_name(p.name.split(".run.json")[0] + ".metrics.json")


def compact(d: Path):
    """Gzip the telemetry of finished runs (about 8x smaller; an autotest baseline is ~2 GB)."""
    for p in Path(d).glob("*.run.json"):
        gz = p.with_name(p.name + ".gz")
        tmp = p.with_name(p.name + ".gz.tmp")
        tmp.write_bytes(gzip.compress(p.read_bytes(), 6))
        os.replace(tmp, gz)
        p.unlink()


def load_metrics(d: Path, min_rep: int = 0, max_rep: int = 1 << 30) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in sorted(Path(d).glob("*.metrics.json")):
        name, rep = p.name.split(".")[:2]
        if min_rep <= int(rep) < max_rep:
            out.setdefault(name, []).append(json.loads(p.read_text()))
    return out


def expect_for(name: str, dirs=None) -> dict:
    """Absolute rules of a scenario. Upstream autotest tests have no YAML: their rule is
    autotest's own verdict, which `completed` carries."""
    p = scenario_file(name, dirs)
    return (yaml.safe_load(p.read_text()) or {}).get("expect") or {} if p.exists() else {}


def judge(candidate: Path, baseline: Path | None = None, only=None, dirs=None):
    """Return (worst verdict, {scenario: (verdict, findings)})."""
    cand = load_metrics(candidate)
    base = load_metrics(baseline) if baseline else {}
    names = only or sorted(cand)
    report = {}
    for name in names:
        # a scenario with no metrics at all never produced telemetry: that is a failure
        runs = cand.get(name) or [{"completed": 0.0}]
        report[name] = oracle.verdict(runs, expect_for(name, dirs), base.get(name))
    worst = max((v for v, _ in report.values()), key=RANK.get, default="PASS")
    return worst, report
