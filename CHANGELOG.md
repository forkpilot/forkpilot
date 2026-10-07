# Changelog

## Unreleased

- Report: it opens with the answer in one sentence: "After <commit>, <metric> in <scenario> fell
  46% (9.2 → 5 m)", then how many runs show it and how many test builds the search took.
- Report: a ground-track map, top-down and to scale, good runs against bad, with a ring where the
  first bad run parts from the good ones. When the metric measures a short part of the flight
  (a stop, a hold), the map shows only that part of each run, drawn from a common start.
- Report: a "What to check next" box: what to flight-test first (the moment and mode where good
  and bad part), the commit to read (its GitHub page when an origin branch has it) and the
  command that reproduces the finding alone.
- Report: the bisect trail as a strip: the suspect range before each test, narrowing to the culprit.
- The example report (`docs/example/report.html`) is rendered again with these.
- README: an image of the top of the example report.
- Report map: the "first divergence" label goes where it covers the fewest track points.

## 0.2.0 (2026-10-07)

- Install: `setuptools<81` is now a dependency. ArduPilot's DroneCAN generator in older trees
  (e.g. 2025-08 master) imports `pkg_resources`, which a fresh Python 3.12 venv does not have and
  setuptools 81 removed: a new install could build master but no older commit. `doctor` checks it,
  and a failed build prints its log path (and a hint when a Python module is missing).
- SITL connect no longer prints pymavlink's "Connection refused sleeping" lines.
- Four Copter scenarios aimed at code the others never ran (picked from a year of coverage data,
  not from any benchmark case): `auto_mission_cmds` (spline, speed, yaw, delay, loiter-turns and
  loiter-time items), `fence_avoid` (circular and altitude fence in LOITER and ALT_HOLD),
  `guided_fast` (long GUIDED legs and velocity commands), `flow_rangefinder` (optical flow and
  rangefinder fused in LOITER). On 40 weeks of master they raise the share of changed lines that
  the Copter scenarios run from 22% to 24%. The first flight of `auto_mission_cmds` found an
  upstream bug: a spline waypoint followed by NAV_DELAY flies into the ground
  ([ArduPilot/ardupilot#34651](https://github.com/ArduPilot/ardupilot/issues/34651)).
- Scenario `mission:` takes Copter mission items (`wp`, `spline`, `loiter_turns`, `loiter_time`,
  `delay`, `speed`, `yaw`, `land`, `rtl`); a plain list of waypoints flies as before. New top-level
  `boot_params:`, read at SITL boot, for drivers that start only then (`FLOW_TYPE`, `RNGFND1_TYPE`).
- The default scenario set changed: cached Copter baselines fly once more.
- A/A check of the four scenarios (`bench/aa_scenarios.md`): `guided_fast` `landing_offset_m` gave
  2% first-pass false alarms between sessions. `noise.json` takes tolerances from A/A runs (`aa`)
  for scenarios too new for the backfill calibration; this one is 0.4 m, and the rate is now 0.
- `noise.json` recalibrated on a second weekly backfill (49 Copter ranges, 14 scenarios): the new
  scenarios get tolerances between builds; every metric keeps the larger of the old and new value.
  The backfill found the 6 earlier Copter behaviour changes again and 2 more, both with the new
  scenarios (`bench/backfill_2025_2026.md`).

- `fix`: a before-and-after table in `fix.md` and the report: the mean of each symptom metric on
  good, on bad and on every flown candidate (a fix brings bad back to good).
- `coverage`: which changed lines of a commit range the scenarios actually run. The bad commit is
  built once more with gcov (in `$FP_HOME/builds/cov`; works for commits before ArduPilot's own
  `--coverage` fix too), each scenario flies once, and the changed lines are split into run (with
  the scenarios that ran them), not run, and not built for this vehicle. A coverage flight is as
  fast as a normal one. On a typical week of master (147 commits) the Copter scenarios ran 13% of
  the changed code: "no regression" says nothing about the rest, and now the report can say which.
- `calibrate`: per-metric noise tolerances from a weekly backfill. SITL is almost deterministic
  for one binary, but a rebuild of unrelated code can move a sensitive metric (a plane landing
  falls into another of a few clusters, metres apart). For consecutive weeks without a localized
  regression, the change of each metric's 3-run mean is noise between builds; 1.25 times the
  largest one widens that metric's band (`forkpilot/noise.json`, shipped). On the one-year
  backfill this removes 3 of the 5 Plane false alarms; every Copter finding and every detection
  of the real benchmark stays. Plane A/A across sessions: 4.4% to 0%.
- A culprit that changes only simulator or tools code, or only another vehicle's code, is marked
  "low confidence" in `evidence.md`, the report and the nightly summary.
- `nightly`: investigate the commits that landed on an upstream branch since the last checked
  one, per vehicle (copter, plane), and keep the results in `$FP_HOME/nightly`. Local only; the
  state advances only for investigations that finished. `--dry-run` shows the ranges.
- `impact`: static analysis of a commit range (no build, no flight, no LLM, repository objects
  only): changed classes, vehicle objects, affected modes (or "all modes" for core code), affected
  parameters with their bitmask/value docs, and which of them the scenarios fly or leave at the
  default. `investigate` also writes the culprit's impact to `evidence.md`.
- `investigate --targeted` (off by default): also fly scenarios picked from the impact of the
  whole range: parameter variants (each documented bit flipped from the default, each non-default
  `@Values` value) and generic stick templates for affected pilot modes no scenario flies (Copter
  ACRO, DRIFT, SPORT, STABILIZE, ZIGZAG; Plane ACRO, CRUISE, FBWB, STABILIZE, TRAINING). Parameters
  the range adds are not varied (the good commit cannot set them). At most 12;
  they get their own cached baseline, and the record, `evidence.md` and `fix` use the same set.
  `impact --plan` prints what would be added. Scenarios suite only; not for PX4.
- `impact`: a vehicle object used only inside a boot-time setup function (`init*`, `setup*`,
  `*_init`, `load_parameters`) no longer counts as core code (MIS_OPTIONS now reaches AUTO, not all
  modes). Parameters carry their default value (a literal or a `#define`).

## 0.1.0 (2026-10-05)

First public release.

- `investigate`: detect, triage, bisect and evidence for ArduCopter, ArduPlane, QuadPlane
  and PX4 multicopter (SIH).
- Deterministic oracle: PASS, DRIFT or FAIL against a baseline band, with per-metric noise
  floors. Drifts in noisy suites are confirmed on fresh runs (Mann-Whitney).
- Suites: ForkPilot scenarios (`scenarios/`) and ArduPilot's own autotest.
- `explain` and `fix`: optional language model (Anthropic or a local OpenAI-compatible
  server). The oracle judges every fix candidate by flying it.
- `report`: one self-contained HTML file, no scripts and no network requests.
- `fromlog` and `replaycheck`: a scenario from a flight log.
- `doctor`, `lint`, `--scenarios DIR`, English and Turkish output (`--lang`).
- Benchmarks: synthetic fork regressions, real ArduPilot regressions (dev and holdout cases).
