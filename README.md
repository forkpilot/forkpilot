# ForkPilot

Regression triage for company forks of ArduPilot and PX4 (PX4: multicopter on SIH, see below).

A fork merges upstream, or adds its own commits, and some flight behaviour changes. ForkPilot
flies a battery of SITL scenarios on the last good commit and on the bad one, decides with a
deterministic oracle whether behaviour changed, finds the commit that changed it, and tries
fix candidates. A language model may explain the evidence and propose a patch. It never decides
a verdict: every claim it makes is either quoted from the evidence or checked by flying.

Simulation results do not replace flight tests. They narrow down what to test.

Example: [docs/example/report.html](docs/example/report.html), the report of a hidden regression in a
synthetic fork (download it and open it in a browser).

## What it has found

- A unit slip in the brake-entry threshold of Copter PosHold, left over from a units conversion.
  Found while studying a benchmark regression; fixed upstream in
  [ArduPilot/ardupilot#34624](https://github.com/ArduPilot/ardupilot/pull/34624).
- In AUTO, a spline waypoint followed by NAV_DELAY flies the copter into the ground. Found on the
  first flight of a new scenario: [ArduPilot/ardupilot#34651](https://github.com/ArduPilot/ardupilot/issues/34651) (open).
- On real ArduPilot regressions, with the culprit commit known only to the benchmark, the shipped
  scenarios and ArduPilot's autotest together detect 4 of 12 dev cases (3 of them with scenarios
  written after the case was studied) and 3 of 5 blind holdout cases ([Benchmarks](#benchmarks)). The misses are mostly behaviours no scenario flies; `coverage`
  shows which changed lines a run never reached.

## Quick look

```bash
pip install -e .                                   # in a clone of this repository
forkpilot doctor --repo ~/fp/fork --build          # checks the machine, builds, flies one hover
forkpilot investigate --repo ~/fp/fork --good <sha> --bad <sha> --report
```

The last command builds both commits, flies the scenarios, bisects to the commit that changed the
behaviour and writes `report.html`. On a fresh install, on the PosHold regression above (two
adjacent commits, `--only hover pilot_sticks`), it took 100 seconds with both builds, after
`doctor --build` had filled the compiler cache (that first build: about 3 minutes on 8 jobs).
Step by step: [docs/quickstart.md](docs/quickstart.md).

## Pipeline

| Stage | What it does | AI |
|---|---|---|
| detect | Full battery on `good` (baseline, cached) and `bad`; oracle verdict per scenario | no |
| triage | Reruns each non-PASS scenario: consistent, intermittent, flaky, preexisting or infra | no |
| bisect | Binary search for the first commit that shows the same symptom (same scenario, metric, direction); skips unbuildable commits and reports them | no |
| evidence | `evidence.md`: oracle findings, triage, first divergence in telemetry, culprit diff | no |
| explain | `explanation.md` from `evidence.md` only; quotes not found in the evidence are marked unverified | optional |
| fix | Candidates (known fix cherry-picked, plain revert, LLM edits) built and flown; the oracle judges each | optional |

Oracle (`forkpilot/oracle.py`):

- **FAIL**: a rule in the scenario's `expect:` block is broken, or the scenario did not complete.
- **DRIFT**: no rule broken, but a metric's mean left the baseline band
  `mean ± max(3·sd, 10%·|mean|, tol)`. `tol` is 0.05, or more for a metric that moves between
  builds without a behaviour change (`forkpilot/noise.json`, from `forkpilot calibrate`).
- **PASS**: otherwise.

Suites whose first pass is only a screen (ArduPilot's own autotest, ~400 tests) confirm each drift
on fresh reruns of both sides (Mann-Whitney, p < 0.01) and replicate it once more.

## Why not `git bisect run` with autotest?

If one autotest test fails on `bad` and passes on `good`, `git bisect run` with that test works,
and you do not need ForkPilot for that. ForkPilot is for the other cases:

- **No test fails.** Most regressions change behaviour without breaking a pass/fail limit: the
  vehicle stops later, lands slower, overshoots more. ForkPilot compares metrics against a
  baseline band (DRIFT), so a change can be found before it becomes a failure.
- **You do not know which test to bisect on.** Detect runs the whole battery (or all ~400
  autotest tests) and gives bisect a symptom: scenario, metric and direction.
- **SITL is noisy.** A single run can differ from the next. Triage reruns each finding, and the
  autotest suite confirms drifts on fresh runs with a rank test. Without this, a bisect can follow
  noise to a wrong commit (the PX4 table below has one such case, from before confirm was added).
- **Some commits do not build.** Bisect skips them and reports the set that contains the culprit.
- **Someone has to read it.** The result is an evidence file and a report with the culprit diff,
  the first divergence in telemetry and the reruns, not only a commit hash.

ForkPilot can use autotest as its suite (`--suite autotest`). The two find different things:
in the blind holdout, each found one regression the other missed.

## Usage

New here? [docs/quickstart.md](docs/quickstart.md) goes from `pip install -e .` and `forkpilot doctor`
to a first investigation on your own fork. `forkpilot` is the same as `python -m forkpilot.cli`.
`FP_HOME` sets where builds, results and investigations are written.

```bash
python -m forkpilot.cli investigate --repo path/to/fork --good <sha> --bad <sha>
python -m forkpilot.cli investigate ... --suite autotest          # ArduPilot's own Copter tests
python -m forkpilot.cli explain investigations/<stamp> --backend local
python -m forkpilot.cli fix investigations/<stamp> --pick <upstream-fix-sha>
python -m forkpilot.cli fix investigations/<stamp> --no-revert --patch my.diff   # fly a hand-written patch
python -m forkpilot.cli fix investigations/<stamp> --backend anthropic --attempts 3
python -m forkpilot.cli report investigations/<stamp>            # one self-contained report.html
python -m forkpilot.cli investigate ... --report                 # write it at the end
python -m forkpilot.cli impact --repo path/to/fork --good <sha> --bad <sha> [--vehicle plane] [--json] [--plan]
python -m forkpilot.cli investigate ... --targeted                # also fly variants/templates the impact picks
python -m forkpilot.cli coverage --repo path/to/fork --good <sha> --bad <sha>   # changed lines the flights run
python -m forkpilot.cli investigate ... --coverage                # the same, of the culprit, in the report
python -m forkpilot.cli fromlog flight.bin -o scenarios/field_report.yaml   # replay a real flight log
```

`impact` is a static, rule-based look at the diff (no build, no flight, no LLM; it only reads the
repository's objects, so it never touches your working tree). It lists the classes the diff changed,
the vehicle objects of those classes, the flight modes that use them (or "all modes" when core code
such as the attitude controller changed), and the parameters touched, with their bitmask and value
docs. Then it compares that with what the shipped scenarios fly: affected modes no scenario flies,
and affected parameters every scenario leaves at its default. `investigate` adds the same section
for the culprit to `evidence.md`. It narrows what to flight-test; it does not prove that a mode or
parameter it leaves out is safe. `investigate --targeted` turns it into extra flights: parameter
variants and stick templates for affected modes no scenario flies (`impact --plan` lists them; see
[quickstart](docs/quickstart.md)).

`coverage` measures instead of predicting. It builds the bad commit once more with gcov (in
`$FP_HOME/builds/cov`), flies each scenario once and splits the changed lines into run (and by which
scenarios), not run, and not built for this vehicle. A coverage flight is as fast as a normal one;
the build takes about 2 min. "No regression" only speaks for the lines that ran: on 40 weeks of
ArduPilot master (2025-10 to 2026-10) the Copter scenarios ran 26% of a week's changed code in the
median week (3-55%; 24% of all changed lines). The rest includes code no scenario reaches yet,
such as gimbal mounts and CAN and serial peripherals (CRSF, MSP). `investigate --coverage` adds
this for the culprit (or, with no culprit, the range) to `evidence.md`, the report and the nightly
summary; with `--targeted` it also picks the scenarios parameter variants fly on.

Output is English. `--lang tr` (before the command, or on `investigate`, `explain`, `fix`) or
`FP_LANG=tr` switches logs, `evidence.md`, `explanation.md`, `fix.md`, `report.html` and the LLM
prompts to Turkish. Texts live in `forkpilot/i18n.py`, one dict per language.

`report.html` has no script and makes no request (inline CSS and SVG, light and dark): it opens on an
air-gapped network. Sections: verdict summary, verdict per scenario, good-against-bad telemetry
and baseline-band plots, bisect trail, coverage of the changed lines, culprit diff, explanation, fix candidates. A missing file
drops its section.

LLM backends:

- `anthropic`: needs `ANTHROPIC_API_KEY`.
- `local`: any OpenAI-compatible endpoint (vLLM, llama.cpp server, Ollama) via `FP_LLM_BASE_URL`
  and `FP_LLM_MODEL`. Nothing leaves the site.

Company scenarios: `lint` checks them, `--scenarios DIR ...` flies them; see [docs/scenarios.md](docs/scenarios.md).

Lower-level commands: `run` (fly the battery), `verdict` (judge a run against a baseline),
`rescore` (recompute metrics from saved telemetry).

## ArduPlane and QuadPlane

A scenario names its vehicle and frame: `vehicle: plane` with `frame: plane` (default) or
`frame: quadplane`. No `vehicle:` means copter, and Copter scenarios fly as before.
`--vehicle plane` on `run` and `investigate` builds `./waf plane` and flies the plane scenarios.
A cached build keeps `arduplane` beside `arducopter` in `builds/cache/<sha>/`, together with the
frame's default parameter files from that commit (`plane.parm`, `quadplane.parm`).
Plane steps (mission items, `wait_wp`, the QuadPlane takeoff) and metrics (airspeed, TECS height,
cross-track, loiter radius and centre, transition time and height loss, VTOL assist, touchdown
point and speed, stall and overspeed margins) are listed in [docs/scenarios.md](docs/scenarios.md).

Starter scenarios: `plane_mission`, `plane_modes`, `plane_battery_rtl`, `quadplane_mission`,
`quadplane_modes`, `quadplane_rc_loss`. Plane SITL is close to deterministic. Each metric falls
into a few discrete values, so a 5-run baseline band can be narrower than the real spread.
Below a noise floor (`FLOORS` in `forkpilot/plane_metrics.py`) a value is reported as the floor.
A mission leg that a failsafe cuts short is left out of the leg metrics.

A/A, same binary (a 5-run baseline, then 3 candidate runs, 1000-3000 random splits):

| Data | Before | Now |
|---|---|---|
| 2026 master, 20 runs per scenario | plane_modes 5.9%, quadplane_modes 8.2%, quadplane_rc_loss 4.4%, others 0% (all ≤ 0.53% after triage) | 0% |
| the same build, a second set of 10 and a set flown at `-j 2` | | 0% in and across sets |
| 2024 build: its 5-run baseline against later runs of the same build (found by a wrong bisect, below) | quadplane_mission 52%, plane_battery_rtl 51% | 0% |
| flown after the floors were fixed: 10 runs each of the 2026 and the 2024 build | | 0%, except plane_mission on the 2024 build: 2.6% (1.6% after triage) |

On the 2024 build the fixed-wing landing has two touchdown points, about 5 m apart. A baseline that saw only one of them flags the other. This was found after the floors
were fixed, so no floor was added for it.

Real regressions: a build with the bug and one with the fix, each flown 5 times. The scenarios were
written before the bugs were looked up:

| Upstream bug | Separated? |
|---|---|
| QuadPlane forced full throttle in transition (ae6f41f414, fixed in 85bb4ad88f) | yes: quadplane_mission DRIFT both ways (max height 51.3 → 63.3 m, leg height RMS 0.46 → 1.01 m), quadplane_modes transition time 10.4 → 9.4 s |
| TECS pitch rate limit (880ebbcdad, fixed in 68003a5eb4) | no: the largest shift is minimum airspeed +0.6 m/s (3.8%), inside the band |
| VTOL approach stop speed (fixed in 972c03ed08) | no: no scenario flies a VTOL approach landing |

`investigate --vehicle plane` from 26c51a8002 (good) to 548b871168 (bad), 25 commits: DRIFT in
quadplane_mission only, consistent in triage (8 of 8), and the bisect names ae6f41f414 after 5 tests.
A first run blamed 6929ae60f9, which changes only a board-ID list. Its binary and the good one fly
the same clusters (VTOL assist 1.0 s or 1.2 s, leg height error RMS 0.45-0.53 m), and the
good baseline had seen only one of them. The floors on those two metrics come from that run.

## PX4 multicopter (SIH)

A scenario with `autopilot: px4` flies a PX4 quadcopter on PX4's built-in SIH simulator (airframe
10040, headless, no Gazebo). `--autopilot px4` on `run` and `investigate` builds
`px4_sitl_default` in the checkout (default `builds/px4`) and flies the PX4 scenarios; `fix` takes
the autopilot from the investigation. ArduPilot flights do not change.

- Build: CMake and Ninja with ccache, nice 19, at most 12 jobs (`FP_BUILD_JOBS`). Per commit, the
  submodules that are already checked out are updated. A failed build gets one clean reconfigure;
  after that the commit is reported as unbuildable. The cache keeps only `bin/` (binary stripped,
  6 MB) and `etc/` in `builds/cache/<sha>/px4/`, about 7 MB per commit. The build tree stays in the
  checkout. Times on this machine (16 cores): a far jump between commits with a warm ccache
  64-80 s, the next commit 2-3 s.
- SIH SITL exists from about v1.14. Older commits cannot fly.
- Instances: each flight claims a machine-wide instance i. PX4 binds udp 18570+i for its GCS link
  (and 14580+i, 14280+i, 13030+i, 19450+i). ForkPilot sends to 18570+i and never listens on 14550,
  so concurrent instances never mix.
- Time: everything ForkPilot sends (GCS heartbeat, MANUAL_CONTROL at 20 Hz, offboard setpoints at
  10 Hz) is paced in sim time (`time_boot_ms`). rcS multiplies `COM_DL_LOSS_T`, `COM_RC_LOSS_T`,
  `COM_OF_LOSS_T` and `COM_DISARM_PRFLT` by the speed factor; ForkPilot sets them back to the
  defaults, except `COM_RC_LOSS_T` = 2 s (at 20x, 0.5 s of sim time is 25 ms of wall time, and 2 of
  150 A/A runs lost manual control mid-flight).
- Speed: SIH runs in lockstep at `PX4_SIM_SPEED_FACTOR`. Measured: one instance, asked for
  5/10/20/40, ran at x4.6/8.5/14.9/22.9; eight at once, asked for 10/20, ran at x7.5/x12. Four
  scenarios flown 4 times each at 4x, 10x and 20x gave the same value clusters. Two small offsets:
  `rc_loss` flight time 57.1-57.8 s at 4x against 58.3-59.4 s at 10x and 20x, and its stop
  backtrack 0.05 m against 0.11-0.20 m. PX4 flies at the default speedup (20).

Starter scenarios (`scenarios/px4_*.yaml`): `px4_hover`, `px4_posctl_sticks` (POSCTL and ALTCTL
stick stops), `px4_mission` (three waypoints, then RTL), `px4_goto_land` (reposition, then
AUTO.LAND), `px4_offboard` (velocity setpoints), `px4_rc_loss`, `px4_link_loss` (`NAV_DLL_ACT` 2),
`px4_battery` (`SIM_BAT_MIN_PCT`). PX4 steps, modes and metrics are in
[docs/scenarios.md](docs/scenarios.md).

A/A on HEAD (9fda8a51, 2026-10), 5-run baseline against 3 candidate runs, 1000 random splits,
triage on 5 more runs. Rates are after triage (detect rate in brackets):

| Data | Rate |
|---|---|
| sets A (10 runs) and B (6 runs), before the fixes below | goto_land 2.0% (14%), hover 1.6% (4.5%), link_loss 1.6%, rc_loss 2.9% (26%), posctl_sticks 6.0% (32%), mission 100% (waypoint count) |
| A and B after the fixes and floors | 0% (posctl_sticks and rc_loss detect 20%: the two runs with a lost manual control) |
| set C, 13 fresh runs (out of sample) | 0%, mission 0.9% (`mission_xtrack_max_m`, 5.8-9.8 m) |
| set C against a baseline from A+B | ≤ 0.1% |
| set D, 10 runs after the last harness change | 0% (posctl_sticks detect 20%: `stop_time_s_posctl`, 2.0 s or 2.9 s) |
| 2025-06 tree (5a430f0b), 13 runs | 5-39% per scenario: see below |

Fixes from the A/A: a waypoint reached just before the RTL item was often never reported (PX4
streams only the latest MISSION_ITEM_REACHED), so a waypoint the mission moved past now counts;
`failsafe_reaction_s` is timed from PX4's "Failsafe activated" text when there is one (HEARTBEAT
comes at 1 Hz). Floors (`FLOORS` in `forkpilot/px4_metrics.py`): `disarm_delay_s` 5 s (two
clusters in every scenario, 1-1.8 s or about 4 s), and centimetre or sub-degree metrics
(`hold_alt_err_max_m`, `land_drift_m`, `landing_offset_m`, `stop_backtrack_m`, `stop_alt_dev_m`
0.5 m, `hold_tilt_rms_deg` 0.5°, `mode_yaw_rate_dps` 1°/s).

Older trees are much noisier. On a 2025-06 build, hover tilt RMS is 1.3-2.9° (HEAD: 0.2-0.4°) and
landing drift, touchdown speed and stop times spread several times wider. The spread is the same at
5x, so the speedup does not cause it; the cause was not looked into. Against itself that build fails
5-39% of splits after triage. PX4 investigations therefore confirm drifts as autotest does (rank test on fresh runs of
both sides, then once more).

Real regressions. `fork-analysis/known_bugs.py` lists 14 PX4 bugs. They were known before this work,
and their category names were in the task. The scenarios, metrics and HEAD floors were written and
calibrated before the list was read. Then three bugs were picked that the scenarios might reach,
and the commit before each fix was flown against the fix, 5 runs each:

| Upstream bug | Separated? |
|---|---|
| spool-up battery failsafe shares state with the in-flight check (fix a3c387fa85) | no: two drifts (touchdown speed, landing offset), neither confirmed by the rank test |
| mission altitude shift after a home-altitude change (fix 3e396f65e5) | no: drifts in landing and stop metrics, none confirmed |
| home altitude baro filter fed microseconds (fix 0bdf8c2fb0) | no: one unconfirmed drift (mission cross-track) |

None of the three is in what the scenarios fly: no unhealthy battery at spool-up, no home-altitude
reset during a mission, no in-air home correction that the baro filter would smooth.

End-to-end `investigate --autopilot px4`:

| Range | Result |
|---|---|
| 2025-06, ef252481a8 → 6604c52c98 (1 commit), before confirm was added | DRIFT, "culprit" 6604c52c98 by elimination. Later shown to be noise (the A/A above) |
| 2025-06, 5a430f0ba6 → fa9f8734d0 (10 commits), before confirm | wrong culprit (3e8f054a1c, a rover rename): bisect followed noise |
| same 10 commits, with confirm | not reproducible: the detect drifts did not survive the rank test |
| synthetic: 5 local commits on HEAD, the 3rd lowers the `MPC_XY_P` default 0.95 → 0.5 | right culprit, 860450b569, in 295 s (2 bisect tests). Confirmed shift in `px4_goto_land` and `px4_link_loss`: `goto_overshoot_max_m`, `hold_drift_max_m`, `hold_tilt_rms_deg` |

The synthetic chain was never pushed; it was deleted after the run. No real PX4 regression has
been localized yet.

## Layout

| Path | Contents |
|---|---|
| `forkpilot/` | runner (SITL + MAVLink steps), vehicles, plane (Plane steps), px4_build, px4_sitl, px4_runner (PX4 SIH), metrics, plane_metrics, px4_metrics, oracle, battery, suites, build, investigate, impact, coverage, calibrate, timeline, explain, fix, fromlog, replaycheck |
| `scenarios/` | YAML scenarios: steps, parameters, `expect:` rules (format: `docs/scenarios.md`) |
| `bench/` | synthetic fork benchmark (`make_fork.py`, `run_bench.py`, `truth/`), autotest A/A calibration |
| `bench/real/` | real ArduPilot regressions (`cases.json`, dev and blind holdout), `run_real.py`, `fix_real.py` |
| `fork-analysis/` | public fork census and known-bug scan (`known_bugs.py --upstream ardupilot\|px4`): does a fork still carry the code of a fixed upstream bug? Carrying the code is not the same as being affected: some bugs need a non-default parameter. The data files are not published ([fork-analysis/README.md](fork-analysis/README.md)). |
| `docs/` | quickstart for company engineers, [fromlog](docs/fromlog.md) (flight log to scenario), [example report](docs/example/) |
| `site/` | project page (`build.py` writes `forkpilot.html`) |
| `Dockerfile` | on-prem image (not yet built or tested) |
| `tests/` | offline unit tests (`python -m unittest`) |

Builds, baselines, investigations and results are written under `builds/`, `results/` and
`investigations/` (not tracked) of `$FP_HOME`, which defaults to this directory.

## Benchmarks

- Synthetic: a fork branch with 30 harmless commits and one behaviour change hidden among them.
- Real: regressions that entered ArduPilot master and were fixed later. Dev cases tune the
  method. Holdout cases stay unseen until the method is frozen.
  Scenarios written after a dev case was studied are marked post-hoc in their comments.

Fix stage on dev cases (`bench/real/fix_real.py`, 2026-10-04). The upstream fix is ground truth,
so it checks the judge as much as the candidates:

| Case | Upstream fix cherry-picked | Revert of culprit |
|---|---|---|
| poshold_brake_units | partial (see below) | fixes |
| body_frame_rotation | does not build on `bad` (needs a later API) | fixes (a first run said side effects on one borderline drift; new findings are now confirmed on fresh runs) |
| land_noGPS_alt_cm | fixes | conflicts |
| guided_fence_units | fixes | conflicts |

The PosHold culprit had a second unit slip (brake entry threshold
`radians(2 * rate)` where the old code meant 0.02·rate degrees). Upstream fix alone: the backtrack
is gone, stop distance and time still drift. That threshold alone: the reverse. Both: the full
battery is clean. Simulation only; the scenario that shows it was written after this case was
studied. Reported upstream ([ArduPilot/ardupilot#34617](https://github.com/ArduPilot/ardupilot/issues/34617))
and fixed in [ArduPilot/ardupilot#34624](https://github.com/ArduPilot/ardupilot/pull/34624).

Detection on the 13 real dev cases (12 regressions, 1 negative), both suites, 2026-10-04:

| Result | Own scenarios | ArduPilot autotest |
|---|---|---|
| Right culprit | 3 (poshold, body_frame, guided_fence) | 1 (follow_kinematic, which upstream's own test also catches) |
| A different real bug | 2 (both point at 34795c4f4e, the no-GPS landing altitude units bug) | the same 2 |
| Nothing found | 7 | 9 |
| Negative case | correctly nothing | correctly nothing |

Together the two suites detect 4 of 12. Most misses are behaviours no scenario flies.
This table measures detection, not bisect: for poshold, body_frame and follow_kinematic the
range was a single commit (good = parent of the culprit). Bisect over real ranges: guided_fence
(19 commits, right culprit) and 34795c4f4e (24 commits, found twice from other cases).
All three scenario hits come from scenarios written after those cases were studied
(`pilot_sticks` for poshold, `guided_cmds` for body_frame and guided_fence), so they show that
the method works once a behaviour is flown, not how often a fixed battery catches an unseen bug.

Blind holdout2 (5 new cases, method frozen first, one run, 2026-10-05): 3 detected, 1 uncertain,
1 missed. Each suite found one case the other missed. All ranges were again a single commit, so
this also tests detection only. Details: [bench/real/holdout2.md](bench/real/holdout2.md).

## License

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE). ForkPilot does not contain
ArduPilot (GPL-3.0) or PX4 (BSD-3-Clause) code, except as listed in NOTICE; it builds and runs your own checkout of them.
The patches in `bench/real/patches/` change ArduPilot files, and the test fixtures in
`tests/data/impact/` are excerpts of ArduPilot files and commits: both are GPL-3.0.

Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).
