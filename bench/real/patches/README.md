# Patches to ArduPilot

These `.diff` files change ArduPilot source files (`ArduCopter/mode_poshold.cpp`). They are
derived from ArduPilot and are licensed under GPL-3.0, the ArduPilot license, not under
ForkPilot's Apache-2.0 license.

| File | Contents |
|---|---|
| `poshold_threshold_only.diff` | PosHold brake-entry threshold back to 0.02·rate degrees (`cd_to_rad`) |
| `poshold_upstream_plus_threshold.diff` | The same, plus the upstream fix f4a1826f3a (POSHOLD_SPEED_0 units) |

Use them with `forkpilot fix <investigation> --patch <file>`. They are simulation results only.
