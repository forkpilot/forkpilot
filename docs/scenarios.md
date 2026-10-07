# Writing scenarios

ForkPilot's own battery (`scenarios/`) cannot cover every company's flight modes, missions and
parameters. A scenario is a small YAML file that says how to fly, what to set, and what must hold.
Put yours in a directory of your own and ForkPilot's oracle, bisect and fix machinery works on them
exactly as on the shipped ones.

```
python -m forkpilot.cli lint company_scenarios/                    # check before flying anything
python -m forkpilot.cli run --scenarios company_scenarios/ --binary <sitl binary> --out results/mine -n 5
python -m forkpilot.cli investigate --good <sha> --bad <sha> --scenarios company_scenarios/
python -m forkpilot.cli fix investigations/<stamp>                 # re-flies the same scenario directories
```

- `--scenarios DIR [DIR ...]` replaces the default. The default is the shipped `scenarios/` plus
  `$FP_HOME/scenarios/` if that directory exists, so scenarios you keep there are picked up with
  no flag. To fly the shipped battery and yours explicitly, name both:
  `--scenarios scenarios/ company_scenarios/`.
- The same scenario name in two directories is an error (an own scenario never silently replaces a
  shipped one). `--only` names are looked up in all of them.
- `investigate` writes the directories it used into `record.json` (`scenario_dirs`), and `fix` uses
  them again; `fix --scenarios DIR ...` overrides this if the directories moved.
- `run` and `investigate` validate every scenario they are about to fly (the same check as
  `lint`) and refuse to start if one has an error. Warnings are printed and do not stop the run.
- Baselines of the good commit are cached by commit, scenario file names and bytes, and settings.
  Any edit to a scenario file (even a comment) re-flies its baseline; moving the directory does not.

## File format

```yaml
name: company_survey            # required, must equal the file name without .yaml
description: free text          # optional, ignored by the tools
params: {ANGLE_MAX: 3000}       # optional: SITL parameters, NAME: number, set before arming
boot_params: {FLOW_TYPE: 10}    # optional: read at SITL boot, for drivers that start only then
steps:                          # required: the flight, one `- step: argument` per line
  - takeoff: 12
  - hold: 3
  - mode: LAND
  - wait_disarm: 120
expect:                         # optional: absolute rules on metrics
  landing_offset_m: {lt: 3}
```

Only `*.yaml` files in a directory are flown. Any other top-level key is an error, so a typo such
as `step:` or `expects:` is caught instead of ignored. Parameter names are upper case, at most 16
characters; values are numbers (write `1` or `0`, not `true` or `false`).

Names with a double underscore (`pilot_sticks__MIS_OPTIONS_4`, `copter_sticks__ZIGZAG`) are what
`investigate --targeted` generates in `$FP_HOME/targeted/`: parameter variants of your scenarios
and stick templates for modes no scenario flies (see the quickstart). Keep your own names without
`__` so they never meet one of those in the same run. A variant copies a scenario of yours too
when it flies an affected mode, so the scenarios you add widen what targeting can vary.

## Steps

Steps run in order, in simulated time. Each is a single `name: argument` entry. `takeoff` leaves
the vehicle in GUIDED mode; `goto`, `send_goto`, `yaw` and `velocity` are GUIDED commands and are
ignored in other modes, so switch back with `mode: GUIDED` first (lint warns about this).
`sticks` only has an effect in modes that read the pilot's sticks (LOITER, POSHOLD, ALT_HOLD, ...).

A PX4 scenario says `autopilot: px4` and flies a PX4 multicopter on PX4's built-in SIH simulator
(`run --autopilot px4`, `investigate --autopilot px4`). The step names are the same; the commands
under them are PX4's. Mode names are PX4's (`POSCTL`, `ALTCTL`, `AUTO.LOITER`, `AUTO.MISSION`,
`AUTO.RTL`, `AUTO.LAND`, `OFFBOARD`). After `takeoff` and `goto` PX4 holds in `AUTO.LOITER`, and
`goto` works from any mode. `sticks` sends MANUAL_CONTROL: PX4 enters a pilot mode only while
manual control is present, so `mode: POSCTL` starts centred sticks itself. `rc_loss` stops
MANUAL_CONTROL and `link_loss` stops everything the ground station sends; `set_param` with the
`SIM_BAT_*` parameters drains the simulated battery. PX4 holds for `COM_FAIL_ACT_T` (5 s) before
a failsafe action, and `failsafe_mode` reports the action, not that hold.

The tables below are generated from the code by `python -m forkpilot.cli lint --reference`; a
test fails when they differ from the code.

<!-- reference:start -->
### Steps

| Step | Argument | Example | What it does |
|---|---|---|---|
| `epoch` | none | `epoch:` | Scenario time 0 is now: later `fly`/`release` `{until: s}` count from here. |
| `fly` | number, seconds; or {until: s} | `fly: 8` | Wait while the pilot flies (sticks set). Like hold, but not judged as a hover. `{until: s}` waits until s seconds after the epoch step. |
| `goto` | [north, east, up], metres from home | `goto: [30, 0, 15]` | GUIDED position target; waits until within 1 m of it (90 s limit). |
| `hold` | number, seconds | `hold: 20` | Wait in place (sim time). Its window is judged as a hover (hold_* metrics) until the next goto, mode or set_param step. |
| `mission` | list of mission items | `mission: [[40, 0, 20], [40, 40, 20]]` | Upload home and these items: [north, east, up] or one of `wp` ([north, east, up]), `spline` ([north, east, up] (spline waypoint)), `loiter_turns` ({at: [north, east, up], turns, radius}), `loiter_time` ({at: [north, east, up], seconds}), `delay` (seconds (NAV_DELAY)), `speed` (ground speed in m/s), `yaw` (heading in degrees (CONDITION_YAW)), `land` ([north, east]), `rtl` (true). A list of only waypoints gets a final RTL. Start it with `mode: AUTO`. |
| `mode` | mode name, upper case | `mode: LOITER` | Switch flight mode and wait for the vehicle to report it. |
| `release` | number, seconds; or {until: s} | `release: 15` | Centre roll, pitch and yaw (throttle stays) and watch the stop for this long: the stop_* metrics. `{until: s}` as for fly. |
| `send_goto` | [north, east, up], metres from home | `send_goto: [150, 0, 10]` | Like goto but does not wait: for targets the firmware may refuse (a fence). |
| `set_param` | {NAME: number} | `set_param: {SIM_GPS1_ENABLE: 0}` | Set parameters in flight; the first one is the fault whose reaction failsafe_reaction_s measures. |
| `sticks` | {roll, pitch, throttle, yaw: PWM 1000-2000} | `sticks: {pitch: 1300, throttle: 1500}` | Pilot RC override, held (and re-sent) until changed; channels not named are left alone. |
| `takeoff` | number, metres | `takeoff: 10` | Switch to GUIDED, arm and climb to this height; waits until 95% of it is reached. |
| `velocity` | {vx, vy, vz, frame, seconds} | `velocity: {vx: 3, frame: body, seconds: 8}` | GUIDED velocity command (m/s; vz is down) repeated for `seconds`; frame is local (north/east) or body (default local). |
| `wait_disarm` | number, seconds timeout | `wait_disarm: 120` | Wait for the vehicle to disarm (landing); the flight metrics need it. |
| `yaw` | number, degrees 0-360 | `yaw: 90` | GUIDED: turn to an absolute heading; waits until within 3 degrees. |

### Metrics

| Metric | Unit | Meaning | Produced when |
|---|---|---|---|
| `completed` | 0/1 | 1 when every step ran to the end; below 1 is always a FAIL. | always |
| `takeoff_time_s` | s | From arming until the climb passes 95% of the takeoff height. | a takeoff step |
| `hold_drift_max_m` | m | Largest horizontal distance from the position where a hold started. | a hold of 1 s or more |
| `hold_alt_err_max_m` | m | Largest altitude change from the start of a hold. | a hold of 1 s or more |
| `hold_tilt_rms_deg` | deg | RMS of the tilt angle during holds. | a hold of 1 s or more |
| `goto_time_total_s` | s | Sum over goto steps of the time from command to arrival. | a goto step |
| `goto_overshoot_max_m` | m | How far past the target, along the leg, the vehicle went (legs shorter than 1 m are skipped). | a goto step |
| `failsafe_reaction_s` | s | From the first set_param step until the flight mode changes; inf when it never does. Resolution is about 1 s (heartbeat). | a set_param step |
| `mission_wp_reached` | count | Distinct mission waypoints reported reached. | a mission step |
| `mission_time_s` | s | From `mode: AUTO` until disarm. | mission, `mode: AUTO` and wait_disarm |
| `mission_speed_max_mps` | m/s | Highest ground speed from `mode: AUTO` until disarm. | mission, `mode: AUTO` and wait_disarm |
| `stop_dist_m_<mode>` | m | After a release: how far the vehicle coasts from the release point. | a release step |
| `stop_backtrack_m_<mode>` | m | After a release: how far it comes back towards the release point after its furthest point. | a release step |
| `stop_time_s_<mode>` | s | After a release: time until ground speed is under 0.3 m/s (the whole window if it never is). | a release step |
| `stop_alt_dev_m_<mode>` | m | After a release: largest height change while stopping. | a release step |
| `stick_speed_mps_<mode>` | m/s | Ground speed at the moment of a release. | a release step |
| `mode_speed_mps_<mode>` | m/s | Mean ground speed of a stretch spent in a mode you switched to (see Mode segments). | a segment of 5 s or more |
| `mode_yaw_rate_dps_<mode>` | deg/s | Mean absolute yaw rate of the same stretch. | a segment of 5 s or more |
| `vel_err_mps_<n>` | m/s | n-th velocity step: distance between the commanded and the flown mean velocity, after the first 3 s. | a velocity step longer than 3 s |
| `alt_max_m` | m | Highest altitude above home. | always |
| `range_max_m` | m | Furthest horizontal distance from home: a refused target or a fence keeps it small. | always |
| `flight_time_s` | s | From arming to disarming. | takeoff and wait_disarm |
| `landing_offset_m` | m | Horizontal distance from home at the end. | takeoff and wait_disarm |
| `disarm_delay_s` | s | From touchdown until the vehicle disarms. | takeoff and wait_disarm |
| `touchdown_speed_mps` | m/s | Fastest descent in the last 2 m before touchdown. | takeoff and wait_disarm |

### Operators

| Operator | Meaning |
|---|---|
| `lt` | metric < bound |
| `le` | metric <= bound |
| `gt` | metric > bound |
| `ge` | metric >= bound |

### Flight modes

`ACRO`, `ALT_HOLD`, `AUTO`, `AUTOROTATE`, `AUTOTUNE`, `AUTO_RTL`, `AVOID_ADSB`, `BRAKE`, `CIRCLE`, `DRIFT`, `FLIP`, `FLOWHOLD`, `FOLLOW`, `GUIDED`, `GUIDED_NOGPS`, `LAND`, `LOITER`, `OF_LOITER`, `POSHOLD`, `POSITION`, `RATE_ACRO`, `RTL`, `SMART_RTL`, `SPORT`, `STABILIZE`, `SYSTEMID`, `THROW`, `TURTLE`, `ZIGZAG`

### Plane and QuadPlane (`vehicle: plane`)

Steps as above except the Copter GUIDED commands (goto, send_goto, velocity, yaw); these differ or are added:

| Step | Argument | Example | What it does |
|---|---|---|---|
| `takeoff` | number, metres; or {alt, mode} | `takeoff: 40` | Plane: arm in AUTO (a mission takeoff item flies the climb). QuadPlane: QLOITER with the throttle stick. Waits until 95% of the height; `{alt: 30, mode: AUTO}` picks the mode. |
| `mission` | list of mission items | `mission: [{takeoff: 40}, {wp: [500, 0, 60]}]` | Upload home and these items: [north, east, up] or one of `takeoff` (height in m (NAV_TAKEOFF)), `vtol_takeoff` (height in m (QuadPlane)), `wp` ([north, east, up]), `loiter_turns` ({at: [north, east, up], turns, radius}), `land` ([north, east] (fixed-wing landing)), `vtol_land` ([north, east] (QuadPlane)), `transition` (mc or fw), `speed` (airspeed in m/s), `land_start` (true), `rtl` (true). A list of only waypoints gets a final RTL. |
| `wait_wp` | number, mission item | `wait_wp: 3` | Wait until the mission's current item is this one or later (600 s limit). |

| Metric | Unit | Meaning | Produced when |
|---|---|---|---|
| `completed` | 0/1 | 1 when every step ran to the end; below 1 is always a FAIL. | always |
| `takeoff_time_s` | s | From arming until the climb passes 95% of the takeoff height. | a takeoff step |
| `mission_wp_reached` | count | Distinct mission items reported reached. | a mission step |
| `leg_xtrack_max_m` | m | Largest distance off the line between two waypoints (AUTO, legs of 50 m or more ending at a wp). | a mission with such legs |
| `leg_xtrack_rms_m` | m | RMS of the same over the second half of each leg. | the same |
| `leg_alt_err_rms_m` | m | RMS height error against the leg's target on level legs (TECS). | the same |
| `leg_airspeed_mps` | m/s | Mean airspeed on the legs. | the same |
| `leg_aspd_err_rms_mps` | m/s | RMS airspeed error the controller reports on the legs. | the same |
| `loiter_radius_m_<mode>` | m | Fitted radius of a fixed-wing loiter (LOITER, RTL, CIRCLE, GUIDED; segments of 45 s or more, last half). | such a segment |
| `loiter_track_err_m_<mode>` | m | RMS distance of the track from that circle. | the same |
| `loiter_alt_sd_m_<mode>` | m | Height steadiness on the circle. | the same |
| `loiter_centre_err_m_<mode>` | m | Circle centre against where it should be (home for RTL, the entry point for LOITER); reported from 5 m up. | the same, RTL or LOITER |
| `hold_drift_max_m` | m | QuadPlane hover: largest horizontal drift during a hold. | a hold step |
| `hold_alt_err_max_m` | m | QuadPlane hover: largest height change during a hold. | a hold step |
| `assist_time_s` | s | Time the VTOL motors assisted wing-borne flight. | a quadplane |
| `transition_fw_time_s_<n>` | s | n-th forward transition: time to wing-borne flight. | a quadplane |
| `transition_fw_alt_loss_m_<n>` | m | Height lost during it; reported from 5 m up. | a quadplane |
| `back_transition_time_s_<n>` | s | n-th back transition: until under 3 m/s ground speed. | a quadplane |
| `back_transition_dist_m_<n>` | m | Distance flown during it. | a quadplane |
| `back_transition_alt_loss_m_<n>` | m | Height lost during it; reported from 5 m up. | a quadplane |
| `airspeed_min_mps` | m/s | Lowest airspeed in wing-borne flight above 20 m (stall margin). | a takeoff step |
| `airspeed_max_mps` | m/s | Highest airspeed in wing-borne flight above 20 m (overspeed). | a takeoff step |
| `failsafe_reaction_s` | s | From the first set_param step until the mode changes; inf when it never does. | a set_param step |
| `failsafe_mode` | number | The mode number it changed to (RTL is 11, QRTL 21). | a set_param step |
| `mode_speed_mps_<mode>` | m/s | Mean ground speed of a stretch in a mode you switched to; from 0.5 up. | a segment of 5 s or more |
| `mode_yaw_rate_dps_<mode>` | deg/s | Mean absolute yaw (turn) rate of the same stretch; from 2 up. | a segment of 5 s or more |
| `alt_max_m` | m | Highest altitude above home. | always |
| `range_max_m` | m | Furthest horizontal distance from home. | always |
| `flight_time_s` | s | From arming to disarming. | takeoff and wait_disarm |
| `landing_offset_m` | m | Horizontal distance from home at the end; from 1 m up. | takeoff and wait_disarm |
| `disarm_delay_s` | s | From touchdown until disarm. | takeoff and wait_disarm |
| `touchdown_gs_mps` | m/s | Ground speed at touchdown. | takeoff and wait_disarm |
| `rollout_m` | m | Ground roll after touchdown. | takeoff and wait_disarm |
| `touchdown_speed_mps` | m/s | Fastest descent in the last 2 m before touchdown. | takeoff and wait_disarm |
| `touchdown_err_m` | m | Touchdown point against the mission's land point; from 1 m up. | a land or vtol_land item, takeoff and wait_disarm |
| `touchdown_along_m` | m | The same along the approach leg (negative: short). | a fixed-wing land item after a waypoint |
| `touchdown_cross_m` | m | The same across the approach leg. | a fixed-wing land item after a waypoint |

Plane flight modes: `ACRO`, `AUTO`, `AUTOLAND`, `AUTOTUNE`, `AVOID_ADSB`, `CIRCLE`, `CRUISE`, `FBWA`, `FBWB`, `GUIDED`, `INITIALISING`, `LOITER`, `LOITERALTQLAND`, `MANUAL`, `QACRO`, `QAUTOTUNE`, `QHOVER`, `QLAND`, `QLOITER`, `QRTL`, `QSTABILIZE`, `RTL`, `STABILIZE`, `TAKEOFF`, `THERMAL`, `TRAINING`

### PX4 multicopter (`autopilot: px4`)

Steps as above except yaw; these differ or are added:

| Step | Argument | Example | What it does |
|---|---|---|---|
| `takeoff` | number, metres | `takeoff: 10` | Arm and take off (MAV_CMD_NAV_TAKEOFF, AUTO.TAKEOFF); waits until past 90% of the height with the climb ended. PX4 then holds in AUTO.LOITER. |
| `goto` | [north, east, up], metres from home | `goto: [30, 0, 15]` | MAV_CMD_DO_REPOSITION (switches to AUTO.LOITER); waits until within 1 m (90 s limit). |
| `send_goto` | [north, east, up], metres from home | `send_goto: [150, 0, 10]` | Like goto but does not wait. |
| `velocity` | {vx, vy, vz, frame, seconds} | `velocity: {vx: 3, frame: body, seconds: 8}` | OFFBOARD velocity setpoints (m/s; vz is down), streamed at 10 Hz, then back to AUTO.LOITER; frame local (north/east) or body. |
| `set_param` | {NAME: number} | `set_param: {SIM_BAT_MIN_PCT: 6}` | Set parameters in flight; a fault injected this way starts failsafe_reaction_s. |
| `mode` | PX4 mode name | `mode: POSCTL` | Switch flight mode (main.sub as PX4 names them) and wait for it. A pilot mode (POSCTL, ALTCTL, ...) starts centred MANUAL_CONTROL first. |
| `mission` | list of mission items | `mission: [[40, 0, 20], [40, 40, 20]]` | Upload these items: [north, east, up] or one of `takeoff` (height in m), `wp` ([north, east, up]), `land` ([north, east]), `loiter_time` ({at: [north, east, up], seconds}), `speed` (ground speed in m/s), `rtl` (true). A list of only waypoints gets a final RTL. Start it with `mode: AUTO.MISSION`. |
| `sticks` | {roll, pitch, throttle, yaw: PWM 1000-2000} | `sticks: {pitch: 1300}` | Pilot input as MANUAL_CONTROL (PWM as an RC transmitter: pitch low is forward, throttle 1500 holds height), re-sent at 20 Hz until changed. |
| `rc_loss` | true or false | `rc_loss: true` | Stop (true) or resume (false) the MANUAL_CONTROL stream: a manual control loss. |
| `link_loss` | true or false | `link_loss: true` | Stop (true) or resume (false) everything the ground station sends, heartbeat included: a data link loss. |

| Metric | Unit | Meaning | Produced when |
|---|---|---|---|
| `completed` | 0/1 | 1 when every step ran to the end; below 1 is always a FAIL. | always |
| `takeoff_time_s` | s | From arming until the climb ends past 90% of the takeoff height. | a takeoff step |
| `hold_drift_max_m` | m | Largest horizontal drift from where a hold started. | a hold step |
| `hold_alt_err_max_m` | m | Largest height change during a hold. | a hold step |
| `hold_tilt_rms_deg` | deg | RMS tilt during holds. | a hold step |
| `hold_thrust` | 0-1 | Mean collective thrust setpoint during holds (hover thrust). | a hold step |
| `goto_time_total_s` | s | Sum over goto steps of the time from command to arrival. | a goto step |
| `goto_overshoot_max_m` | m | How far past the target, along the leg, the vehicle went. | a goto step |
| `failsafe_reaction_s` | s | From the first fault (set_param, rc_loss, link_loss) until the mode changes; inf when it never does. | a fault step |
| `failsafe_mode` | main.sub | The failsafe action as PX4 mode number (AUTO.RTL 4.5, AUTO.LAND 4.6), after the Hold PX4 waits in first. | a fault step |
| `mission_wp_reached` | count | Distinct mission items reported reached. | a mission step |
| `mission_time_s` | s | From AUTO.MISSION until disarm. | a mission flown to the landing |
| `mission_speed_max_mps` | m/s | Highest ground speed from AUTO.MISSION until disarm. | a mission flown to the landing |
| `mission_xtrack_max_m` | m | Largest distance off the line between consecutive waypoints while that leg was current. | a mission with legs of 5 m or more |
| `rtl_alt_max_m` | m | Highest altitude in the first AUTO.RTL. | a Return, commanded or failsafe |
| `rtl_time_s` | s | From entering AUTO.RTL until disarm. | a Return and wait_disarm |
| `stop_dist_m_<mode>` | m | After a release: how far the vehicle coasts. | a release step |
| `stop_backtrack_m_<mode>` | m | After a release: how far it comes back after its furthest point. | a release step |
| `stop_time_s_<mode>` | s | After a release: until under 0.3 m/s. | a release step |
| `stop_alt_dev_m_<mode>` | m | After a release: largest height change. | a release step |
| `stick_speed_mps_<mode>` | m/s | Ground speed at the release. | a release step |
| `mode_speed_mps_<mode>` | m/s | Mean ground speed of a stretch in a mode you switched to. | a segment of 5 s or more |
| `mode_yaw_rate_dps_<mode>` | deg/s | Mean absolute yaw rate of the same stretch. | a segment of 5 s or more |
| `vel_err_mps_<n>` | m/s | n-th velocity step: commanded against flown mean velocity after 3 s. | a velocity step longer than 3 s |
| `att_err_rms_deg` | deg | RMS roll/pitch error against the attitude setpoint while in the air. | a takeoff step |
| `alt_max_m` | m | Highest altitude above home. | always |
| `range_max_m` | m | Furthest horizontal distance from home. | always |
| `flight_time_s` | s | From arming to disarming. | takeoff and wait_disarm |
| `landing_offset_m` | m | Horizontal distance from home at the end. | takeoff and wait_disarm |
| `disarm_delay_s` | s | From touchdown until disarm. | takeoff and wait_disarm |
| `touchdown_speed_mps` | m/s | Fastest descent in the last 2 m before touchdown. | takeoff and wait_disarm |
| `land_drift_m` | m | Horizontal distance moved between entering AUTO.LAND and the touchdown. | `mode: AUTO.LAND` and wait_disarm |

PX4 flight modes: `ACRO`, `ALTCTL`, `AUTO.FOLLOW_TARGET`, `AUTO.LAND`, `AUTO.LOITER`, `AUTO.MISSION`, `AUTO.PRECLAND`, `AUTO.READY`, `AUTO.RTL`, `AUTO.TAKEOFF`, `AUTO.VTOL_TAKEOFF`, `MANUAL`, `OFFBOARD`, `POSCTL`, `POSCTL.ORBIT`, `POSCTL.SLOW`, `STABILIZED`

In metric names a PX4 mode is lower case with the dot as underscore: `stop_dist_m_posctl`, `mode_speed_mps_auto_loiter`.
<!-- reference:end -->

## How a scenario is judged

Every run produces a flat dictionary of metrics (above), computed from the telemetry by
deterministic arithmetic (`forkpilot/metrics.py`). Then, per scenario (`forkpilot/oracle.py`):

- **FAIL**: any run did not complete (a step timed out or the vehicle died: `completed` is 0), or
  any run breaks a rule in `expect:`, or a metric named in `expect:` was not produced at all. A
  rule is `metric: {operator: bound}`; several operators on one metric must all hold, e.g.
  `{gt: 0.5, lt: 10}`. `inf` (for example no failsafe reaction) never satisfies `lt` or `le`.
- **DRIFT**: no rule is broken, but the mean of a metric over the candidate's runs is outside the
  band measured on the good commit: `mean ± max(3·sd, 10%·|mean|, 0.05)` over the baseline runs
  (`sd` is the sample standard deviation). This applies to every metric both sides produced, with
  or without a rule. The band never narrows below 10% of the mean or 0.05 in the metric's unit,
  whatever the baseline spread was.
- **PASS**: otherwise.

`expect:` therefore holds the hard requirements ("must land within 3 m of home"), and the baseline
band catches everything else that moved. A scenario with `expect: {}` is still judged by drift.

Run counts: `run -n` flies each scenario that many times (default 3). `investigate` flies the
good commit 5 times (the cached baseline), the bad commit 3 times, reruns each non-PASS scenario
5 more times to separate a consistent change from noise, and then flies at least 2 runs at every
bisect step (more for intermittent symptoms). A rule that fails in only some runs is reported as
intermittent. Simulation runs at 20x, so a 3-minute flight takes well under a minute of wall time
plus start-up.

## Metrics a scenario can produce, and what lint checks

`lint` knows which metrics the steps of a scenario will produce, by walking through the events the
runner records, and rejects an `expect:` rule on a metric that the flight cannot produce (it would
FAIL on every run with "metric must be produced"). It also suggests the closest valid name.

- Metrics with a mode in their name (`stop_dist_m_<mode>` and the other `stop_*`, `stick_speed_mps_<mode>`,
  `mode_speed_mps_<mode>`, `mode_yaw_rate_dps_<mode>`) use the lower-case mode name
  (`stop_dist_m_poshold`, `mode_speed_mps_alt_hold`). The second time the same mode appears, the
  name gets a number: `stop_dist_m_poshold2`. Lint checks the mode exists and that the scenario
  has a matching release or segment.
- **Mode segments** (`mode_speed_mps_*`, `mode_yaw_rate_dps_*`): a segment starts at a `mode`,
  `sticks` or `release` step (after at least one `mode` step) and ends at the next `mode`,
  `sticks`, `release`, `goto`, `send_goto`, `velocity`, `yaw`, `set_param` step or at disarm. It
  needs to last 5 s or more, so `mode: LOITER` followed by `fly: 3` and then a `sticks` step gives no
  `mode_speed_mps_loiter` for that segment.
- **Hold windows** (`hold_*`): a hold lasts from its `hold` step until the next `goto`, `mode`
  or `set_param` step (or the end of the flight). Steps between them (`fly`, `velocity`,
  `wait_disarm`) are inside the window and judged as a hover. End the hold with `mode:` before
  landing or flying a pattern you do not want judged as a hover.
- `failsafe_reaction_s` is measured from the first `set_param` step, whatever it sets.
- When lint cannot decide before flying (for example a release after a `set_param`, where a
  failsafe may have changed the mode) it warns instead of failing.

## Writing scenarios that are deterministic enough

The oracle compares runs of the good and the bad commit, so the scenario itself must not be the
main source of variation. Measured spread comes from timing: SITL, telemetry and the 1 Hz
heartbeat are not aligned the same way twice.

- Measure over a fixed time, not at an event. `hold: 20` after `takeoff` is repeatable;
  "wait until it feels settled" is not. Put a `hold: 3` or `fly: 3` between a mode switch or a
  takeoff and the manoeuvre you measure, so the vehicle has settled.
- Do not race the firmware: after `mode:` the runner waits for the mode to be confirmed, but a
  `sticks` step right after it acts on a vehicle that may not have finished switching. Add a `fly`.
- `goto` waits until the vehicle is within 1 m of the target (90 s limit); `send_goto` does not
  wait. Use `send_goto` plus `hold` for targets the firmware may refuse, and `goto` otherwise.
- Always end with `wait_disarm` with a generous timeout when you want landing metrics. A timeout is
  an error: the run does not complete, which is a FAIL.
- Set thresholds from data. Fly the good build `-n 5` or more, look at the `*.metrics.json` files,
  and put the bound with margin outside the spread (the band for drift is already there for
  the metrics you do not bound). A rule tighter than the run-to-run spread FAILs on the good
  commit too.
- `failsafe_reaction_s` has about 1 s resolution (the heartbeat); a bound below 2 s is noise.
- Keep wind and turbulence parameters explicit in `params:` if the scenario depends on them.
- One idea per scenario. When a scenario fails, the metric names tell the story; a long scenario
  that exercises five things makes the bisect symptom ambiguous.
- Keep flights short; every baseline run, triage rerun and bisect step flies them again.

## Example: a company scenario

A survey leg flown as an AUTO mission in a crosswind (`SIM_WIND_SPD` is m/s, `SIM_WIND_DIR` is degrees). The vehicle must reach both waypoints, stay under the speed limit and land at home.

```yaml
name: company_survey
description: >
  Survey leg flown as an AUTO mission in a light crosswind;
  the vehicle must fly both waypoints, stay under the speed limit and land at home.
params: {SIM_WIND_SPD: 3, SIM_WIND_DIR: 90}
steps:
  - mission: [[25, 0, 12], [25, 25, 12]]     # waypoints are north, east, up in metres from home
  - takeoff: 12
  - hold: 3                                   # settle; this hold ends at the mode step
  - mode: AUTO
  - wait_disarm: 200                          # the mission ends with RTL and landing
expect:
  takeoff_time_s: {lt: 15}
  mission_wp_reached: {ge: 2}
  mission_speed_max_mps: {lt: 7}
  landing_offset_m: {lt: 3}
```

```
$ python -m forkpilot.cli lint company_scenarios/
0 error(s), 0 warning(s)
```

What lint says about mistakes (file, line, and the closest valid name):

```
company_survey.yaml:9: error: unknown step 'wait_disarmed' (did you mean 'wait_disarm'?); valid: ...
company_survey.yaml:12: error: unknown operator 'lte' (did you mean 'le'?); valid: lt, le, gt, ge
company_survey.yaml:13: error: unknown metric 'landing_ofset_m' (did you mean 'landing_offset_m'?); ...
company_survey.yaml:14: error: metric 'failsafe_reaction_s' is never produced here: it needs a set_param step (the injected fault)
```

## What lint does not know

- Parameter names are checked for form, not against the firmware. SITL setting a name the firmware
  does not acknowledge can stall the flight (the runner waits for the acknowledgement), so try a
  new `params:` or `set_param` name in a single short `run -n 1` first.
- Whether a mission is flyable, a fence is respected, or the thresholds are sensible.
- Metrics marked "possible" by the walk-through (a landing touchdown, `goto_overshoot_max_m` on a leg
  shorter than 1 m) depend on what the vehicle does; the oracle FAILs a rule on a metric that
  turns out missing.
