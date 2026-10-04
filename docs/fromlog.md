# fromlog: replay a real flight in SITL

A pilot reports a problem and sends the flight log. `forkpilot fromlog` turns the log into a
ForkPilot scenario, so the same stick inputs, mode changes and mission can be flown against any
two builds of your fork.

```bash
forkpilot fromlog flight.bin -o scenarios/field_report.yaml            # whole flight
forkpilot fromlog flight.bin -o scenarios/field_report.yaml --start 60 --end 120
forkpilot fromlog flight.tlog -o scenarios/field_report.yaml           # telemetry log, lower fidelity
forkpilot run --out results/field --only field_report -n 3             # fly it
```

The command prints the log window it used, for example `log 50.8..164.4 s, takeoff 10 m, 24 steps`,
and the notes for everything it could not replay. The generated file is plain YAML in the runner's
own step vocabulary (`takeoff`, `mode`, `sticks`, `release`, `fly`, `set_param`, `mission`,
`wait_disarm`). Read it before you trust it: every step carries a comment with the log time it came
from, and the header says which log and window it was made from.

## What is read from the log

| From the log | Becomes |
|---|---|
| Arming and the first climb | A GUIDED `takeoff` to the altitude the log shows at the start of the window. Scenario time 0 is the end of that takeoff. |
| `MODE` messages (DataFlash), heartbeats (tlog) | `mode` steps at the log's times. Changes made by the firmware itself (mission sequencing, auto-land after RTL) are left to happen on their own. Failsafe-triggered changes are replayed as plain mode commands; a comment names the trigger, which is not reproduced. |
| `RCIN` (DataFlash), `RC_CHANNELS` (tlog) | `sticks` steps. Channels follow the log's `RCMAP_*`; `RCn_MIN/MAX/TRIM/REVERSED` are undone, so the PWM sent to the stock SITL gives the same stick position. |
| Centred sticks for 2 s or more in a pilot mode | A `release` step. The runner measures stop distance there. |
| `CMD` messages (DataFlash), mission upload (tlog) | A `mission` step: waypoints as north, east, up metres from home. The runner appends an RTL. |
| `PARM` | `params:` and `set_param` steps, see below. |

### Sticks

The RC samples are compressed into piecewise-constant segments. A segment grows while every channel
stays inside a band of `2 * --tol` PWM (default tol 20) and holds the band's midpoint, so no sample is
further than `--tol` from the replayed value. A roll, pitch or yaw within tol of centre becomes exactly
1500, and values are rounded to 5 PWM. A blip shorter than `--min-segment` (0.3 s) is absorbed
by a neighbouring segment, but only if it stays inside the band; a real stick move is never
swallowed. A flight with a lot of stick work gives some tens of segments, not thousands (1142 RC samples became 46 segments).

`--tol` trades size for fidelity. On the validation flights below, 20 was best: 10 gives more steps
and more timing noise, 40 shifts the replayed path systematically.

### Parameters

Only a curated allow-list of flight-behaviour parameters is copied (`ATC_`, `PSC_`, `WPNAV_`, `LOIT_`,
`PHLD_`, `RTL_`, `FS_`, `BATT_FS_`, `ANGLE_MAX`, `PILOT_`, `SIM_WIND_`, and similar; see
`ALLOW` in `forkpilot/fromlog.py`). A parameter is written to the scenario only when its value in the
log differs from the SITL default: the value in the SITL defaults file (`--defaults`, default
`ardupilot/Tools/autotest/default_params/copter.parm`) if there is one, else the firmware default the
log records. Hardware, calibration, serial, logging and similar parameters that differ from their
defaults are not copied; their names are listed in a comment, so that you can see what was left out.
`--keep-param PREFIX` adds more, `--drop-param PREFIX` removes some, `--all-params` copies every
allow-listed parameter.

Two cases need care:

- The log's default column also holds the vehicle's own defaults file. If the vehicle ships tuned
  defaults (a vendor build), they equal the log values and are not copied, and the replay uses the
  SITL's stock tune. Use `--all-params` to copy them.
- A tlog has no default column. Allow-listed parameters have no known default then; the header comment
  counts them, and they are not copied unless you pass `--all-params` or a `--defaults` file.

A parameter changed during the flight becomes a `set_param` step at that time. Stick deadzones
(`RCn_DZ`) are moved to the channel the stock SITL uses for that role.

## How well does the replay match? Measured

`forkpilot replaycheck original.bin replay.bin --start <log s>` compares a flight with the replay of
its scenario. Both vehicles' EKF positions (`XKF1`) are taken relative to their own start, and time is
aligned at the replay's first mode step. The window ends at the earlier touchdown. The report gives
horizontal and vertical error over time, path length, the mode sequence with timing, and the stop
distance of every `release`.

Three flights, each flown in SITL at 20x speedup, converted with default options, and replayed three
times (numbers are the range over the three replays):

| Flight | Horizontal error, mean / max | Vertical error, mean | Path length, original / replay | Mode times |
|---|---|---|---|---|
| `pilot_sticks` (LOITER, POSHOLD, ALT_HOLD, LAND; 114 s, 129 m) | 0.27 to 0.32 m / 1.1 to 1.3 m | 0.06 to 0.11 m | 129.2 / 129.0 to 129.3 m | within 0.5 s (replay early by 0.1 to 0.5 s) |
| `auto_mission` (AUTO, 3 waypoints, 74 s, 159 m) | 0.01 to 0.04 m / 0.04 to 0.37 m | 0.01 to 0.03 m | 158.8 / 158.8 m | the same second |
| Field-like: RCMAP swapped, two channels reversed, wind, sensor noise, 9 changed parameters and one in-flight parameter change (LOITER, POSHOLD, ALT_HOLD, RTL; 114 s, 144 m) | 0.39 to 0.49 m / 1.0 to 1.5 m | 0.11 to 0.15 m | 144.0 / 140.7 to 141.5 m | within 0.1 s |

Stop distances after a `release` agree within about 1% to 8%, for example (original / replay) 8.87 / 9.01 m,
7.82 / 7.75 m, 6.35 / 6.31 m and 10.89 / 11.09 m on `pilot_sticks`. The mode sequence was equal in
every replay.

How to read this: the replay reproduces the pilot's commands to within a few tenths of a metre in
a 100 m flight. It does not say the replay will match a real vehicle. The originals above are SITL
flights of the same airframe, so these numbers are an upper bound of what is possible: the only
differences are the compressed sticks, step timing, and the random seeds of noise and wind.

### Speedup has no effect on fidelity

The runner waits in simulated time, so 5x, 10x, 20x and 40x replays of the same scenario give the
same result within noise (field-like flight, horizontal mean error 0.34, 0.37, 0.39 and 0.36 m;
`pilot_sticks` at 5x and 40x: 0.34 and 0.36 m). Use the default speedup.

### Why a replay is a little off: step timing

`fly` and `release` wait in simulated time but end on the next telemetry message, about 7 ms late
per step, and a `mode` step costs about 0.1 s while it waits for a heartbeat. `fromlog` subtracts both
from the following waits (`--step-overhead`, `--mode-latency`), spreading the rounding over
the steps, so the replay does not drift. The remaining error is a few 0.1 s at the end of a flight.
A runner change would remove the cause, see below.

### Wind matters

Without `SIM_WIND_*` the same field-like replay is far worse: mean 3.8 m, max 20.4 m, path 105 m against
144 m. Parameters that stand for the environment are on the allow-list for this reason. Wind is
only copied when the log has it as a parameter, which is true for SITL logs and false for real
flights.

## Limits

Read this before you act on a replay.

- It is SITL physics. No real motors, battery sag, vibration, GPS or compass faults, EKF
  behaviour of real sensors, no radio loss. A bug that needs the real vehicle does not show up.
- The airframe is the SITL's stock quad. The replay reproduces what the pilot *commanded*, not what
  the real vehicle *did*. If the real vehicle was heavier, tuned differently or underpowered, the same
  sticks fly a different path. Stop distances and trajectories compared with the original log are only
  meaningful when the log is itself from SITL (as in the table above).
- No wind and no sensor noise unless the log has `SIM_WIND_*` parameters. Real wind is not in a
  DataFlash log in a form that can be replayed.
- The ground phase is replaced by a GUIDED takeoff. The scenario starts at the first moment the
  vehicle is airborne and settled, not at arming.
- Guided targets, `goto` commands from a ground station, and other MAVLink commands are not
  recovered; only modes, sticks, parameters and the mission are. The steps `goto`, `send_goto`,
  `velocity` and `hold` are never generated.
- Mission items other than waypoints, RTL and takeoff (jumps, conditions, servo, DO commands, LAND)
  are dropped with a note.
- Failsafes are not re-triggered. A failsafe mode change is replayed as a plain command and a
  comment says what caused it. If the real flight failed because of a radio or GPS loss, the
  replay does not lose them.
- Firmware-sequenced mode changes (for example AUTO to RTL at the end of a mission, auto-land) are not
  replayed as commands; they depend on the replayed vehicle reaching the same state.
- A tlog has 1 Hz heartbeats, so mode changes are accurate to about a second, and RC and position
  samples are sparser. Use a `.bin` when you have one.
- Logs that start after arming have no arming record. `fromlog` treats the log start as the
  arming.
- Replay errors grow with time and with aggressive flying: tens of seconds of hard manoeuvres give a
  larger drift than a gentle mission. Compare the two builds on the replay with the same
  scenario, and treat an absolute match to the original as optional.

## Proposed runner change

`runner.py` is part of the baseline-cache fingerprint, so `fromlog` does not touch it. The timing
workarounds above would be unnecessary with an absolute-time wait. The proposal is two small additions to
the step vocabulary: an `epoch` step that sets scenario time 0 to the current simulated time, and
`fly`/`release` accepting `{until: <s>}`, which waits until that scenario time instead of for a
duration. Then each wait lands on the log's time and errors do not accumulate, and a finer
`--tol` would become practical. `release` already calls `fly`, so it gets `until` too. The diff against the current runner:

```diff
@@ -59,6 +59,7 @@
     def __init__(self, sitl: Sitl, run: Run):
         self.s, self.run = sitl, run
         self.t = 0.0          # sim time, seconds
+        self.t_epoch = 0.0    # sim time of scenario t=0, see step_epoch
         self.mode = None
         self.armed = False
         self.rc: dict[int, int] = {}   # pilot stick override, channel -> PWM, re-sent while set
@@ -158,10 +159,14 @@
         self.event("hold_start", seconds)
         self.wait(lambda: self.t >= end, seconds + 5, "hold")
 
+    def step_epoch(self, _=None):
+        """Scenario time 0 is now: `fly: {until: s}` and `release: {until: s}` count from here."""
+        self.t_epoch = self.t
+
     def step_fly(self, seconds):
         """Wait while the pilot flies (sticks set); unlike hold, not judged as a hover."""
-        end = self.t + seconds
-        self.wait(lambda: self.t >= end, seconds + 5, "fly")
+        end = self.t_epoch + seconds["until"] if isinstance(seconds, dict) else self.t + seconds
+        self.wait(lambda: self.t >= end, max(end - self.t, 0) + 5, "fly")
 
     def step_goto(self, target):
         n, e, up = target
```

`fromlog` would then emit an `epoch` step after the takeoff and `until` waits. It is not applied; it changes
the runner's bytes, so it needs a baseline re-fly.

## Tests

`python -m unittest tests.test_fromlog` runs offline. It uses synthetic flights, a 68 KB excerpt of a
real log (`tests/data/auto_mission_small.bin`) and a generated tlog.
