# holdout2: single blind run (2026-10-05)

Method frozen at tag `holdout2-freeze` (dee5a28) before the run. The only change before the
freeze: `run_real.py` reads `--cases-file` and the case's vehicle (method unchanged). Run once,
reported as is. Cases: `candidates_new.json` (chosen 2026-10-04, before any run).

Results: `results/20261005-003349.json` (scenarios), `results/20261005-005605.json` (autotest);
the results directory is not tracked. Machine time: scenarios 22 min, autotest 147 min.

| Case | Vehicle | good..bad | ForkPilot scenarios | ArduPilot autotest | Notes |
|---|---|---|---|---|---|
| loiter_coord_turn_brake | copter | 1 commit | nothing found | DRIFT/FAIL, culprit named | Symptom: `ModeZigZag` fails in every run ("reached 4.0 of 8.0 m"), `BeaconPosition` final offset drift. No scenario flies Loiter with yaw while moving. `Parameters` also fails, but only because the new `LOIT_OPTIONS` is missing from the parent's parameter XML (autotest is pinned to the parent): a harness artifact, not a symptom. |
| posctrl_down_sign_flip | copter | 1 commit | DRIFT, culprit named | 99 tests changed (11 fail), culprit named | Symptom matches the bug (slower descent: longer flight and mission time; autotest `Landing`, `LoiterToAlt`, `WPNAV_SPEED_DN` fail). Culprit and fix are in the same PR, so master never carried it. |
| mis_options_bitmask | copter | 1 commit | nothing found | nothing found | Needs a non-default `MIS_OPTIONS` bit. Missed by both. |
| qplane_spoolup_block_stuck | quadplane | 1 commit | FAIL, culprit named | (Copter only) | All three QuadPlane scenarios fail to complete. Culprit and fix in the same PR. |
| plane_training_shaping_stale | plane | 12 commits (culprit does not build) | DRIFT, range named | (Copter only) | 11 of 12 commits do not build (an API change across the PR). The named set contains the culprit, but the drifts are in mission legs and RC-loss climb; no scenario flies TRAINING mode. The drift is most likely from another commit of the same PR. Not counted as the bug. |

## Reading

- Clear detection of the bug: 3 of 5 (loiter: autotest only; posctrl: both; qplane: scenarios only).
  Uncertain: 1 (plane). Missed: 1 (param-gated).
- Localization was not tested: in 4 cases the range was a single commit, and in the fifth the
  commits around the culprit do not build. The real benchmark scores `culprit_hit`, but with
  good = parent of the culprit, this is detection.
- 2 of the 3 clear detections are mid-PR commits that no fork merging upstream master would get.
- The scenarios and autotest are complementary: each found one case the other missed.

## Note for the next holdout

Give each case a realistic range (for example, the last release tag or a fork's merge window
before the culprit to some commits after it), so that the bisect is tested too.
