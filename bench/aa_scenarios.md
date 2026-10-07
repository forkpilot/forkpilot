# Scenario A/A calibration (2026-10-05)

Unchanged firmware judged against itself (`bench/scenario_aa.py`). Every DRIFT or FAIL is a false
alarm of the first pass (detect), before triage reruns it. Baseline 5 runs, candidate 3 runs, as in
`investigate`. Two sessions per vehicle, 8 runs each: one at `-j 12`, one at `-j 4`. "Cross" judges
a baseline from one session against candidates from the other, which is closer to real use
(baseline and candidate flown at different times).

| Vehicle (binary) | Split | Verdicts | False alarms | Source |
|---|---|---|---|---|
| Copter, 10 scenarios (e21a6aebbf) | same session (j12) | 560 | 0 (0.0%) | |
| | cross: base j12, cand j4 | 4480 | 8 (0.2%) | `wind_hover` `hold_alt_err_max_m` |
| | cross: base j4, cand j12 | 4480 | 21 (0.5%) | `wind_hover` `hold_alt_err_max_m` |
| Plane + QuadPlane, 6 scenarios (cafe674577) | same session (j12) | 336 | 6 (1.8%) | `plane_mission` `landing_offset_m` |
| | cross: base j12, cand j4 | 2688 | 0 (0.0%) | |
| | cross: base j4, cand j12 | 2688 | 118 (4.4%) | `plane_mission` `landing_offset_m` (26% of splits) |

Reading:

- Copter: about 2-5% of investigations would see one first-pass false alarm, all from one metric
  of the turbulent-wind scenario. Triage reruns it before anything is bisected.
- Plane: `landing_offset_m` takes a few discrete values (about 7.7, 10.4 and 11.1 m in these
  runs), so a baseline that happens to hold one cluster gives a narrow band. Its noise floor
  (1.0 m) is smaller than this spread (3.4 m). Not changed yet: more runs from the nightly check
  first, then a decision on the floor.
- Not measured here: the rate after triage, and noise between two builds of the same source.

## Four new Copter scenarios (2026-10-07)

Same protocol for `auto_mission_cmds`, `fence_avoid`, `guided_fast` and `flow_rangefinder` on
master f4669a7226, 8 runs at `-j 12` and 8 at `-j 4`.

| Split | Verdicts | False alarms | Source |
|---|---|---|---|
| same session (j12) | 224 | 0 (0.0%) | |
| cross: base j12, cand j4 | 1792 | 0 (0.0%) | |
| cross: base j4, cand j12 | 1792 | 36 (2.0%) | `guided_fast` `landing_offset_m` (8% of splits) |

`guided_fast` ends with a LAND after velocity commands, and its landing point is 0.9-1.8 m from
home in both sessions (same mean, 1.33 m). A 5-run baseline that holds the narrow part of that
spread gives a band smaller than it. The scenario is too new for the backfill calibration (20
pairs needed), so its tolerance comes from these runs: 0.4 m, the smallest of 0.05/0.2/0.3/0.4/0.5
with no false alarm in any split. It is in `noise.json` under `aa`; `calibrate --write` keeps that
section. With it all three splits give 0 false alarms.

Later the same day a weekly backfill with these scenarios measured noise between builds
(`bench/backfill_2025_2026.md`): 0.95 m for this metric. The oracle takes the larger value.
