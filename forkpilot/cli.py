"""forkpilot command line.

  forkpilot doctor      --repo path/to/fork [--good <sha> --bad <sha>] [--build]
  python -m forkpilot.cli run        --firmware ardupilot --out results/base  -n 3
  python -m forkpilot.cli lint        [paths]         # check scenario files before flying them
  python -m forkpilot.cli fromlog     flight.bin -o scenarios/field_report.yaml [--start 60 --end 120]
  python -m forkpilot.cli replaycheck original.bin replay.bin --start 50.9
  python -m forkpilot.cli verdict     --candidate results/cand --baseline results/base
  python -m forkpilot.cli investigate --repo ardupilot --good <sha> --bad <sha>
  python -m forkpilot.cli nightly     --repo clone --vehicle copter plane   # new upstream commits since last night
  python -m forkpilot.cli calibrate   [--backfill nightly/backfill.jsonl] [--write]
  python -m forkpilot.cli coverage    --good G --bad B [--repo R] [--vehicle copter] [--scenario NAME ...]
  python -m forkpilot.cli impact      --repo ardupilot --good <sha> --bad <sha> [--vehicle plane] [--json] [--plan]
  python -m forkpilot.cli explain     investigations/<stamp> [--backend local] [--lang en]
  python -m forkpilot.cli fix         investigations/<stamp> [--backend anthropic|local]
  python -m forkpilot.cli report      investigations/<stamp>      # writes report.html there
  python -m forkpilot.cli --lang tr ...                           # Turkish output (default: en, or FP_LANG)
"""
from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from pathlib import Path

from . import i18n
from .config import JOBS, ROOT, SPEEDUP
from .i18n import t
from . import vehicles
from .vehicles import VEHICLES

# battery (and everything that flies) imports pymavlink: load it per command, so that
# `forkpilot doctor` still runs, and says what is missing, when the environment is broken


def cmd_doctor(a):
    from .doctor import main as doctor
    sys.exit(doctor(Path(a.repo) if a.repo else None, a.good, a.bad, a.build))


def _dirs(a):
    return [Path(d).resolve() for d in a.scenarios] if getattr(a, "scenarios", None) else None


def cmd_run(a):
    from . import scenario
    from .battery import run_battery
    binary = Path(a.binary).resolve() if a.binary else None
    vehicle = vehicles.for_autopilot(a.autopilot, a.vehicle)
    firmware = a.firmware or ROOT / ("builds/px4" if vehicle == "px4" else "ardupilot")
    dirs = _dirs(a)
    try:
        scenario.check(dirs, a.only)
    except scenario.ScenarioSetError as e:
        sys.exit(f"error: {e}")
    run_battery(binary, Path(firmware), Path(a.out), only=a.only, n=a.n, jobs=a.jobs,
                speedup=a.speedup, dirs=dirs, vehicle=vehicle)


def cmd_lint(a):
    from . import scenario
    if a.reference:
        print(scenario.reference())
        return
    issues = scenario.lint_paths(a.paths or None)
    for i in issues:
        print(i)
    errors = sum(i.level == "error" for i in issues)
    warnings = len(issues) - errors
    print(f"{errors} error(s), {warnings} warning(s)")
    sys.exit(1 if errors or (a.strict and warnings) else 0)


def cmd_rescore(a):
    from . import metrics
    from .battery import metrics_path, read_run, run_paths
    for p in run_paths(a.dir):
        metrics_path(p).write_text(json.dumps(metrics.compute(read_run(p)), indent=1))


def cmd_fromlog(a):
    from .fromlog import LogError, Options, convert, read_defaults, read_flight
    defaults = Path(a.defaults) if a.defaults else ROOT / "ardupilot/Tools/autotest/default_params/copter.parm"
    o = Options(start=a.start, end=a.end, tol=a.tol, min_seg=a.min_segment, release_min=a.release_min,
                mode_latency=a.mode_latency, step_overhead=a.step_overhead, takeoff_alt=a.takeoff_alt, keep=tuple(a.keep_param or ()),
                drop=tuple(a.drop_param or ()), all_params=a.all_params,
                defaults=read_defaults(defaults) if defaults.exists() else None,
                name=a.name or (Path(a.out).stem if a.out else Path(a.log).stem))
    try:
        res = convert(read_flight(a.log), o)
    except LogError as e:
        sys.exit(f"error: {e}")
    if a.out:
        Path(a.out).write_text(res.yaml)
    else:
        print(res.yaml, end="")
    s = res.stats
    print(f"log {res.start:.2f}..{res.end:.2f} s, takeoff {res.takeoff_alt:g} m, {s['steps']} steps: "
          f"{s['stick_segments']} stick segments from {s['samples']} RC samples, {s['modes']} mode "
          f"changes, {s['params']} params kept, {s['dropped']} dropped"
          + (f" -> {a.out}" if a.out else ""), file=sys.stderr)
    for n in res.notes:
        print(f"note: {n}", file=sys.stderr)


def cmd_replaycheck(a):
    from .replaycheck import compare, report
    print(report(compare(a.original, a.replay, a.start, a.replay_start, a.end)))


def cmd_verdict(a):
    from .battery import RANK, judge
    worst, report = judge(Path(a.candidate), Path(a.baseline) if a.baseline else None, dirs=_dirs(a))
    for name, (v, findings) in report.items():
        print(f"{v:5}  {name}")
        for f in findings:
            print(f"         {f}")
    print("\n" + t("cli.result", worst=worst))
    sys.exit(RANK[worst])


def cmd_investigate(a):
    from .investigate import investigate
    from .scenario import ScenarioSetError
    vehicle = vehicles.for_autopilot(a.autopilot, a.vehicle)
    repo = a.repo or ROOT / ("builds/px4" if vehicle == "px4" else "ardupilot")
    if vehicle == "px4" and a.suite == "autotest":
        sys.exit(t("cli.error", e=t("cli.px4_autotest")))
    if not (Path(repo) / ".git").exists():
        sys.exit(f"error: {repo} is not a checkout of your fork; pass --repo <path> "
                 "(`forkpilot doctor --repo <path>` checks it)")
    try:
        rec = investigate(Path(repo), a.good, a.bad, only=a.only, jobs=a.jobs,
                          reported=a.reported or (), suite=a.suite, vehicle=vehicle,
                          explain=(a.explain, a.model, i18n.lang()) if a.explain else None,
                          scenario_dirs=_dirs(a), targeted=a.targeted, coverage=a.coverage)
    except ScenarioSetError as e:
        sys.exit(t("cli.error", e=e))
    print("\n" + t("cli.record", path=rec.path))
    if a.fix and rec.data.get("outcome") == "localized":
        from .explain import ExplainError, make_backend
        from .scenario import ScenarioSetError
        from .fix import fix
        try:
            backend = make_backend(a.fix, a.model) if a.fix != "revert" else None
            print(t("cli.fixreport", path=fix(rec.dir, backend, jobs=a.jobs, log=rec.log)))
        except (ExplainError, ScenarioSetError) as e:
            print(t("cli.fixskipped", e=e))
    if a.report:       # last, so it includes the explanation and the fix results
        _write_report(rec.dir, rec.log)


def cmd_nightly(a):
    from . import nightly
    from .scenario import ScenarioSetError
    if not (Path(a.repo) / ".git").exists():
        sys.exit(f"error: {a.repo} is not a git checkout; pass --repo <dedicated clone of your fork>")
    try:
        if a.backfill:
            nightly.backfill(Path(a.repo), a.remote, a.branch, a.vehicle, a.suite, a.backfill,
                             a.until, a.step_days, a.jobs, a.dry_run, a.keep_telemetry,
                             coverage=a.coverage)
        else:
            nightly.run(Path(a.repo), a.remote, a.branch, a.vehicle, a.suite, a.max_commits,
                        a.first_range, a.jobs, a.dry_run, coverage=a.coverage)
    except (RuntimeError, ScenarioSetError, subprocess.CalledProcessError) as e:
        sys.exit(t("cli.error", e=e))
    if a.dry_run:
        print(t("nightly.dry"))


def _write_report(inv_dir, log=print):
    """A report failure never costs the investigation."""
    from .report import ReportError, write
    try:
        log(t("cli.report", path=write(inv_dir)))
    except (ReportError, OSError) as e:
        log(t("cli.report_skipped", e=e))


def cmd_impact(a):
    from . import impact
    repo = a.repo or ROOT / "ardupilot"
    if not (Path(repo) / ".git").exists():
        sys.exit(f"error: {repo} is not a checkout of your fork; pass --repo <path>")
    try:
        report = impact.impact(Path(repo), a.good, a.bad, a.vehicle)
    except impact.ImpactError as e:
        sys.exit(t("cli.error", e=e))
    text = impact.lines(report)
    if a.plan:
        from . import targeted
        p = targeted.plan(report, a.vehicle)
        report["plan"] = targeted.summary(p)
        text += [""] + targeted.lines(p)
    print(json.dumps(report, indent=2) if a.json else "\n".join(text))


def cmd_calibrate(a):
    from . import calibrate
    from .nightly import nightly_dir
    src = Path(a.backfill) if a.backfill else nightly_dir() / "backfill.jsonl"
    if not src.exists():
        sys.exit(f"no backfill results at {src}: run `nightly --backfill` first")
    calibrate.main(src, write=a.write)


def cmd_coverage(a):
    from . import coverage
    repo = Path(a.repo or ROOT / "ardupilot")
    if not (repo / ".git").exists():
        sys.exit(f"error: {repo} is not a checkout of your fork; pass --repo <path>")
    try:
        res = coverage.measure(repo, a.good, a.bad, a.vehicle, a.scenario or None, jobs=a.jobs)
    except (coverage.CoverageError, subprocess.CalledProcessError) as e:
        sys.exit(t("cli.error", e=e))
    print(json.dumps(res, indent=1) if a.json else "\n".join(coverage.summary_lines(res)))


def cmd_explain(a):
    from .explain import ExplainError, explain, make_backend
    try:
        path = explain(Path(a.dir), make_backend(a.explain_backend, a.model))
    except ExplainError as e:
        sys.exit(t("cli.error", e=e))
    print(t("cli.explanation", path=path))


def cmd_fix(a):
    from .explain import ExplainError, make_backend
    from .scenario import ScenarioSetError
    from .fix import fix
    try:
        backend = make_backend(a.fix_backend, a.model) if a.fix_backend else None
        path = fix(Path(a.dir), backend, attempts=a.attempts, jobs=a.jobs,
                   revert=not a.no_revert, picks=a.pick or (),
                   patches=a.patch or (), repo=a.repo, scenario_dirs=_dirs(a))
    except (ExplainError, ScenarioSetError) as e:
        sys.exit(t("cli.error", e=e))
    print(t("cli.fixreport", path=path))


def cmd_report(a):
    from .report import ReportError, write
    try:
        path = write(Path(a.dir), Path(a.out) if a.out else None)
    except ReportError as e:
        sys.exit(t("cli.error", e=e))
    print(t("cli.report", path=path))


SCEN_HELP = ("directories of scenario YAML files to fly (default: the shipped scenarios/ and "
             "$FP_HOME/scenarios); the same name in two directories is an error")


def main(argv=None):
    p = argparse.ArgumentParser(prog="forkpilot", description="Regression triage for ArduPilot forks. "
                                "Builds, results and investigations go to $FP_HOME (default: the source tree).")
    p.add_argument("--lang", choices=i18n.LANGS, help="output language (default: FP_LANG or en)")
    sub = p.add_subparsers(required=True)
    dr = sub.add_parser("doctor", help="check this machine and your ArduPilot checkout; --build also smoke-tests")
    dr.add_argument("--repo", help="your fork's checkout")
    dr.add_argument("--good", help="last known-good commit (checked to exist in --repo)")
    dr.add_argument("--bad", help="commit that shows the problem (checked to exist in --repo)")
    dr.add_argument("--build", action="store_true",
                    help="build the SITL binary for HEAD and fly the hover scenario once (slow the first time)")
    dr.set_defaults(fn=cmd_doctor)
    r = sub.add_parser("run")
    r.add_argument("--firmware", help="ArduPilot checkout (default: ardupilot; with --autopilot px4: builds/px4)")
    r.add_argument("--binary", help="SITL binary to test (default: <firmware>/build/sitl/bin/<vehicle's binary>)")
    r.add_argument("--vehicle", choices=sorted(VEHICLES), default="copter",
                   help="fly this vehicle's scenarios (each scenario names its vehicle; default copter)")
    r.add_argument("--autopilot", choices=vehicles.AUTOPILOTS, default="ardupilot",
                   help="px4: fly the PX4 multicopter scenarios (`autopilot: px4`) on PX4's SIH simulator")
    r.add_argument("--out", required=True)
    r.add_argument("-n", type=int, default=3, help="repeats per scenario")
    r.add_argument("-j", "--jobs", type=int, default=JOBS)
    r.add_argument("--speedup", type=int, default=SPEEDUP)
    r.add_argument("--only", nargs="*")
    r.add_argument("--scenarios", nargs="+", metavar="DIR", help=SCEN_HELP)
    r.set_defaults(fn=cmd_run)
    ln = sub.add_parser("lint", help="check scenario files (YAML, steps, expect rules) without flying")
    ln.add_argument("paths", nargs="*", help="files or directories (default: the shipped scenarios)")
    ln.add_argument("--strict", action="store_true", help="warnings also fail")
    ln.add_argument("--reference", action="store_true",
                    help="print the step and metric reference (Markdown) generated from the code")
    ln.set_defaults(fn=cmd_lint)
    fl = sub.add_parser("fromlog", help="turn a flight log (.bin/.log/.tlog) into a replayable scenario")
    fl.add_argument("log")
    fl.add_argument("-o", "--out", help="scenario file to write (default: stdout)")
    fl.add_argument("--name", help="scenario name (default: the output file's stem)")
    fl.add_argument("--start", type=float, help="window start, log seconds (default: airborne)")
    fl.add_argument("--end", type=float, help="window end, log seconds (default: disarm)")
    fl.add_argument("--tol", type=float, default=20.0, help="stick compression tolerance, PWM")
    fl.add_argument("--min-segment", type=float, default=0.3, help="a stick blip shorter than this, inside the tolerance band, is absorbed, s")
    fl.add_argument("--release-min", type=float, default=2.0,
                    help="centred sticks at least this long become a `release` step, s")
    fl.add_argument("--mode-latency", type=float, default=0.1,
                    help="s a `mode` step costs in the runner, subtracted from the next wait")
    fl.add_argument("--step-overhead", type=float, default=0.007,
                    help="s each fly/release step overshoots in the runner, subtracted from later waits")
    fl.add_argument("--takeoff-alt", type=float, help="override the takeoff altitude, m")
    fl.add_argument("--defaults", help="SITL defaults file (default: ardupilot's copter.parm)")
    fl.add_argument("--keep-param", nargs="*", help="extra parameter name prefixes to copy")
    fl.add_argument("--drop-param", nargs="*", help="parameter name prefixes not to copy")
    fl.add_argument("--all-params", action="store_true",
                    help="copy every allow-listed parameter, not only those that differ from the defaults")
    fl.set_defaults(fn=cmd_fromlog)
    rc = sub.add_parser("replaycheck", help="compare an original flight with the replay of its scenario")
    rc.add_argument("original", help="the original DataFlash log")
    rc.add_argument("replay", help="DataFlash log of the replayed scenario")
    rc.add_argument("--start", type=float, required=True, help="window start in the original, log seconds (fromlog prints it)")
    rc.add_argument("--replay-start", type=float, help="the same moment in the replay (default: its first mode change)")
    rc.add_argument("--end", type=float, help="window end in the original, log seconds")
    rc.set_defaults(fn=cmd_replaycheck)
    v = sub.add_parser("verdict")
    v.add_argument("--candidate", required=True)
    v.add_argument("--baseline")
    v.add_argument("--scenarios", nargs="+", metavar="DIR", help=SCEN_HELP)
    v.set_defaults(fn=cmd_verdict)
    rs = sub.add_parser("rescore", help="recompute metrics from saved telemetry")
    rs.add_argument("dir")
    rs.set_defaults(fn=cmd_rescore)
    iv = sub.add_parser("investigate", help="triage + bisect a regression between two commits")
    iv.add_argument("--repo", help="checkout of your fork (default: ardupilot; with --autopilot px4: builds/px4)")
    iv.add_argument("--good", required=True, help="last known-good commit")
    iv.add_argument("--bad", required=True, help="commit that shows the problem")
    iv.add_argument("--only", nargs="*", help="limit the battery to these scenarios")
    iv.add_argument("--scenarios", nargs="+", metavar="DIR", help=SCEN_HELP)
    iv.add_argument("--reported", nargs="*", help="scenarios CI saw fail on the bad commit")
    iv.add_argument("--vehicle", choices=sorted(VEHICLES), default="copter",
                    help="vehicle to build and fly (the scenarios suite flies that vehicle's scenarios)")
    iv.add_argument("--autopilot", choices=vehicles.AUTOPILOTS, default="ardupilot",
                    help="px4: fly the PX4 multicopter scenarios (`autopilot: px4`) on PX4's SIH simulator")
    iv.add_argument("--suite", choices=["scenarios", "autotest"], default="scenarios",
                    help="ForkPilot's scenarios or ArduPilot's own autotest suite")
    iv.add_argument("--targeted", action="store_true",
                    help="also fly scenarios picked from the range's impact: parameter variants and stick "
                         "templates for affected modes no scenario flies (`impact --plan` lists them)")
    iv.add_argument("--coverage", action="store_true",
                    help="also measure which changed lines (of the culprit, else of the range) the "
                         "scenarios run: one gcov build and one flight per scenario, about 3 min")
    iv.add_argument("-j", "--jobs", type=int, default=JOBS)
    iv.add_argument("--explain", choices=["anthropic", "local"],
                    help="after localizing, write explanation.md with this LLM backend (off by default)")
    iv.add_argument("--model", help="model name for --explain (default: the backend's)")
    iv.add_argument("--lang", choices=i18n.LANGS, default=argparse.SUPPRESS, help="same as the global --lang")
    iv.add_argument("--report", action="store_true", help="after the investigation, write report.html")
    iv.add_argument("--fix", choices=["revert", "anthropic", "local"],
                    help="after localizing, try fix candidates: revert only, or revert + this LLM")
    iv.set_defaults(fn=cmd_investigate)
    nt = sub.add_parser("nightly", help="investigate the upstream commits that landed since the last check")
    nt.add_argument("--repo", required=True, help="a dedicated clone (its tree is checked out and built in)")
    nt.add_argument("--remote", default="origin")
    nt.add_argument("--branch", default="master")
    nt.add_argument("--vehicle", nargs="+", choices=["copter", "plane"], default=["copter"],
                    help="vehicles to check, one investigation each (default: copter)")
    nt.add_argument("--suite", choices=["scenarios", "autotest"], default="scenarios")
    nt.add_argument("--max-commits", type=int, default=60,
                    help="a longer range is still investigated, only noted in the record")
    nt.add_argument("--first-range", type=int, default=20,
                    help="first run: check this many commits behind the branch tip")
    nt.add_argument("-j", "--jobs", type=int, default=JOBS)
    nt.add_argument("--coverage", action="store_true",
                    help="also measure which changed lines the scenarios run (about 3 min per range)")
    nt.add_argument("--dry-run", action="store_true", help="fetch, print the ranges, build and fly nothing")
    nt.add_argument("--lang", choices=i18n.LANGS, default=argparse.SUPPRESS, help="same as the global --lang")
    nt.add_argument("--backfill", metavar="YYYY-MM-DD",
                    help="check history since this day as if the nightly had run every day; "
                         "resumable, results in $FP_HOME/nightly/backfill.jsonl")
    nt.add_argument("--until", metavar="YYYY-MM-DD", help="with --backfill: last day (default: today)")
    nt.add_argument("--keep-telemetry", action="store_true",
                    help="with --backfill: keep raw telemetry of ranges without a localized regression")
    nt.add_argument("--step-days", type=int, default=1, help="with --backfill: one range per N days with commits")
    nt.set_defaults(fn=cmd_nightly)
    im = sub.add_parser("impact", help="which modes and parameters a commit range touches, and which "
                        "of them the scenarios fly (static: no build, no flight)")
    im.add_argument("--repo", help="your fork's checkout (default: ardupilot); only read, never changed")
    im.add_argument("--good", required=True)
    im.add_argument("--bad", required=True)
    im.add_argument("--vehicle", choices=("copter", "plane"), default="copter",
                    help="plane covers QuadPlane too (default copter)")
    im.add_argument("--json", action="store_true", help="machine-readable output")
    im.add_argument("--plan", action="store_true",
                    help="also print the scenarios `investigate --targeted` would add, and why (flies nothing)")
    im.set_defaults(fn=cmd_impact)
    cb = sub.add_parser("calibrate", help="per-metric noise tolerances from a weekly backfill "
                        "(how much each metric moves between builds without a behaviour change)")
    cb.add_argument("--backfill", help="backfill.jsonl (default: $FP_HOME/nightly/backfill.jsonl)")
    cb.add_argument("--write", action="store_true", help="write forkpilot/noise.json (the oracle reads it)")
    cb.set_defaults(fn=cmd_calibrate)
    cv = sub.add_parser("coverage", help="which changed lines of a commit range the scenarios run "
                        "(gcov build of the bad commit, one flight per scenario)")
    cv.add_argument("--repo", help="your fork's checkout (default: ardupilot); builds in a separate worktree")
    cv.add_argument("--good", required=True)
    cv.add_argument("--bad", required=True)
    cv.add_argument("--vehicle", choices=("copter", "plane"), default="copter")
    cv.add_argument("--scenario", action="append", help="fly only this scenario (repeatable)")
    cv.add_argument("-j", "--jobs", type=int, default=JOBS)
    cv.add_argument("--json", action="store_true", help="machine-readable output")
    cv.set_defaults(fn=cmd_coverage)
    ex = sub.add_parser("explain", help="LLM explanation of an investigation's evidence.md")
    ex.add_argument("dir", help="investigation directory containing evidence.md")
    ex.add_argument("--backend", dest="explain_backend", choices=["anthropic", "local"], default="anthropic",
                    help="anthropic (ANTHROPIC_API_KEY) or local OpenAI-compatible (FP_LLM_BASE_URL, FP_LLM_MODEL)")
    ex.add_argument("--model", help="model name (default: claude-opus-5-5 / FP_LLM_MODEL)")
    ex.add_argument("--lang", choices=i18n.LANGS, default=argparse.SUPPRESS, help="same as the global --lang")
    ex.set_defaults(fn=cmd_explain)
    fx = sub.add_parser("fix", help="try fix candidates for a localized regression; the oracle judges them")
    fx.add_argument("dir", help="investigation directory with outcome 'localized'")
    fx.add_argument("--backend", dest="fix_backend", choices=["anthropic", "local"],
                    help="also ask this LLM for patches (default: revert candidate only)")
    fx.add_argument("--model", help="model name (default: the backend's)")
    fx.add_argument("--attempts", type=int, default=3, help="LLM attempts, each told why the last one failed")
    fx.add_argument("--no-revert", action="store_true", help="skip the plain revert candidate")
    fx.add_argument("--pick", nargs="*", help="known fix commits to cherry-pick as candidates (e.g. upstream's fix)")
    fx.add_argument("--patch", nargs="*", help="patch files to try as candidates (git apply)")
    fx.add_argument("--repo", help="checkout to build in (default: the investigation's)")
    fx.add_argument("--scenarios", nargs="+", metavar="DIR",
                    help="scenario directories (default: the ones the investigation flew)")
    fx.add_argument("--lang", choices=i18n.LANGS, default=argparse.SUPPRESS, help="same as the global --lang")
    fx.add_argument("-j", "--jobs", type=int, default=JOBS)
    fx.set_defaults(fn=cmd_fix)
    rp = sub.add_parser("report", help="write one self-contained report.html for an investigation")
    rp.add_argument("dir", help="investigation directory")
    rp.add_argument("--out", help="output file (default: <dir>/report.html)")
    rp.set_defaults(fn=cmd_report)
    a = p.parse_args(argv)
    if a.lang:
        i18n.set_lang(a.lang)
    # a CI timeout or `kill` must still run the cleanup that puts the checkout back on its branch
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    a.fn(a)


if __name__ == "__main__":
    main()
