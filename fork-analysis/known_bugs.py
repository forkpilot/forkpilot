"""Does a public fork still carry a known, since-fixed upstream regression?

Each signature is a line (or several lines that must all be present) the culprit commit
introduced and the fix commit removed. It is
checked against upstream first (present at the culprit, absent at its parent and at the fix),
then looked for in each fork's default branch, one raw file per check. Only public repositories,
only files; nothing is cloned or run. A hit means "the buggy line is in your default branch",
not "your vehicle misbehaves": the fork may not fly that mode, or may patch it elsewhere.

  python fork-analysis/known_bugs.py verify [--upstream ardupilot|px4]
  python fork-analysis/known_bugs.py scan [--upstream ardupilot|px4] [--min-custom 5] [--limit 40]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "fork-analysis" / "data"

BUGS = [
    {"case": "poshold_brake_units", "file": "ArduCopter/mode_poshold.cpp",
     "line": "vel_fw_ms) <= POSHOLD_SPEED_0)", "culprit": "d56e91bbe2", "fix": "f4a1826f3a",
     "what": "PosHold braking cut short (speed threshold 10 m/s instead of 0.1 m/s): overshoot and fly-back when sticks are released"},
    {"case": "rtl_descent_trunc", "file": "ArduCopter/mode_rtl.cpp",
     "line": "labs(rtl_path.descent_target.alt * 0.01", "culprit": "2db6c0ec8c", "fix": "042ae97d01",
     "what": "RTL descent stage ends 1 m from RTL_ALT_FINAL instead of 0.2 m (integer truncation)"},
    {"case": "hover_learn_roll_trunc", "file": "ArduCopter/Attitude.cpp",
     "line": "labs(ahrs.get_", "culprit": "f8f720a0c0", "fix": "0612b9d15d",
     "what": "hover-throttle learning allowed far beyond the intended 5 deg lean (integer truncation of radians)"},
    {"case": "circle_rate_entry", "file": "ArduCopter/mode_circle.cpp",
     "line": "rate_current_degs + rate_pilot_change_degs", "culprit": "5e26bf6b7f", "fix": "62d8bbb80f",
     "what": "Circle-mode pilot rate change applied to the ramping current rate instead of the target rate"},
    {"case": "body_frame_rotation", "file": "ArduCopter/GCS_MAVLink_Copter.cpp",
     "line": "            copter.ahrs.body_to_earth2D(vel_neu_ms.xy());", "culprit": "7e45e20885", "fix": "7811979ca6",
     "what": "body-frame SET_POSITION_TARGET velocity/accel not rotated to earth frame (result discarded)"},
    {"case": "bendyruler_sign", "file": "libraries/AC_WPNav/AC_WPNav_OA.cpp",
     # the line itself predates the bug; it became wrong when the controller call switched to NED
     "line": ["destination_ne_m.y, target_alt_loc_alt_m}", "input_pos_NED_m(destination_ned_m, terrain_d_m"],
     "culprit": "93b5456b6b", "fix": "a9ad30dbf9",
     "what": "BendyRuler horizontal avoidance passes the destination altitude with the wrong sign"},
    {"case": "terrain_d_sign", "file": "ArduCopter/mode_guided.cpp",
     "line": "init_pos_terrain_D_m(-terrain_d_m)", "culprit": "986c4a4e19", "fix": "d6f64dac94",
     "what": "Guided terrain offset initialised with the wrong sign"},
    {"case": 'follow_face_lead', "file": 'ArduCopter/mode_follow.cpp',
     "line": "if (pos_ned_m.xy().length_squared() > 1.0) {", "culprit": '61f35ed0c8', "fix": 'f6c0aa9cf9',
     "what": "FOLL_YAW_BEHAVE=face-lead checks the lead's absolute position instead of the vector to it"},
    {"case": 'follow_kinematic', "file": 'libraries/AP_Follow/AP_Follow.cpp',
     "line": "return 0.5f * jerk_max * sq(t_half);", "culprit": 'c74a29722c', "fix": '9aeac8bfa2',
     "what": "AP_Follow kinematic smoothing: half-area velocity profile, wrong offset velocity under lead rotation, unshaped vertical"},
    {"case": 'yaw_slew_limit', "file": 'libraries/AC_AttitudeControl/AC_AttitudeControl.cpp',
     "line": "attitude_command_model(0.0, heading_rate_rads, _ang_vel_target_rads.z, _ang_accel_target_rads.z, radians(_ang_vel_yaw_max_degs), get_accel_yaw_max_radss(), _rate_y_tc, _dt_s);", "culprit": 'eaeae81b91', "fix": '4959ff6824',
     "what": "S-curve command model: yaw slew limit applied as a hard clamp on the input rate rather than in the command model"},
    {"case": 'payload_land_alt', "file": 'ArduCopter/mode_auto.cpp',
     "line": "curr_rngfnd_alt_m -= pos_control->get_pos_offset_U_m();", "culprit": '11292d4139', "fix": 'feb84ea854',
     "what": "rangefinder-height conversion to metres broke altitude shifting for Land and Payload Place"},
    {"case": 'ahrs_trim_scale', "file": 'ArduCopter/RC_Channel_Copter.cpp',
     "line": "roll_trim = cd_to_rad((float)get_roll_channel().get_control_in());", "culprit": 'f4069f57d9', "fix": 'b10ea90714',
     "what": "save_trim converts stick input with the wrong scale"},
    {"case": 'circle_edge_dist', "file": 'libraries/AC_WPNav/AC_Circle.cpp',
     "line": "result_neu_m.x = _center_neu_m.x + vec_to_center_neu_m.x / dist_m * _radius_m;", "culprit": '6df1d293b9', "fix": '17a820d435',
     "what": "AC_Circle distance-to-edge computed from the wrong point after cm->m conversion"},
    {"case": 'ned_rename_signs', "file": 'ArduCopter/mode_auto.cpp',
     "line": "pos_control->input_pos_vel_accel_D_m(descent_start_altitude_m, vel, 0.0);", "culprit": '7a8cc3815f', "fix": '00f9265c68',
     "what": "commit titled 'No compiler change' flips payload-place ascent target and Throw-mode height check signs"},
    {"case": 'scurve_climb_arc', "file": 'libraries/AP_Math/SCurve.cpp',
     "line": "Vector3f path_unit(arc_tangent_ne.x, arc_tangent_ne.y, dz_ds);", "culprit": 'd650d534f9', "fix": '67052e7f79',
     "what": "climbing arcs: velocity/centripetal accel use the 3D path speed as horizontal speed"},
    {"case": 'srtm_flow_sign', "file": 'libraries/AP_NavEKF3/AP_NavEKF3_OptFlowFusion.cpp',
     "line": "heightAboveGndEst = MAX((terrain_srtm_alt - pd), rngOnGnd);", "culprit": 'd84ae4c51d', "fix": '09c48d855f',
     "what": "EKF3 optical-flow height from SRTM terrain has the wrong sign"},
    {"case": 'guided_fence_units', "file": 'ArduCopter/mode_guided.cpp',
     "line": "const Location dest_loc(pos_neu_m, is_terrain_alt ? Location::AltFrame::ABOVE_TERRAIN", "culprit": '1a4f8b738e', "fix": '7f25d3efbf',
     "what": "Guided destination fence check gets metres where cm are expected: outside-fence targets accepted"},
    {"case": 'follow_dir_flight', "file": 'ArduCopter/mode_follow.cpp',
     "line": "if (vel_ofs_ne_ms.length_squared() > (100.0 * 100.0)) {", "culprit": '654cc97520', "fix": 'a1e5d47248',
     "what": "face-direction-of-flight threshold 100 m/s after conversion: never yaws to the flight direction"},
    # found by ForkPilot itself (rtl_descent_trunc series run); upstream fix message names no symptom
    {"case": 'land_noGPS_alt_cm', "file": 'ArduCopter/mode.cpp',
     "line": ["float Mode::get_alt_above_ground_m(void) const", "if (!pos_control->is_active_NE()) {"],
     "culprit": '34795c4f4e', "fix": '7d69dc29',
     "what": "LAND without position control returns altitude in cm as metres: no slow-down near ground, "
             "touchdown at 1.5 m/s instead of 0.5 m/s after GPS loss"},
]


PX4_BUGS = [
    # regressions on PX4 main since 2024-01 that were fixed later on main
    {"case": "offtrack_land_3d", "file": "src/modules/flight_mode_manager/tasks/Auto/FlightTaskAuto.cpp",
     "line": "} else if ((_position - _closest_pt).longerThan(_target_acceptance_radius)) {",
     "culprit": "33be5d8356", "fix": "2c62caeb7d",
     "what": "mission LAND leg: off-track check in 3D flags a vehicle above the previous waypoint as off track"},
    {"case": "vel_lp_cutoff_ignored", "file": "src/modules/mc_pos_control/MulticopterPositionControl.cpp",
     "line": "_vel_xy_lp_filter.setCutoffFreq(sample_freq_hz, _param_mpc_vel_lp.get());",
     "culprit": "8b9900cce3", "fix": "71f8b37183",
     "what": "MPC_VEL_LP at or above half the loop rate is rejected but ignored: velocity feedback frozen at zero, hover oscillation"},
    {"case": "att_ref_windup", "file": "src/modules/mc_att_control/AttitudeControl/AttitudeControl.cpp",
     "line": "const Vector3f delta_phi = (1.f - a) * e + b * _omega_correction + omega_command * dt;",
     "culprit": "2a0e048171", "fix": "d9fa6201f4",
     "what": "attitude reference model winds up under a yaw-rate command the vehicle cannot follow (yaw rate setpoint far above the command)"},
    {"case": "manual_vel_limit", "file": "src/modules/flight_mode_manager/tasks/ManualAcceleration/FlightTaskManualAcceleration.cpp",
     "line": "_stick_acceleration_xy.setVelocityConstraint(interpolate(vxy_max, factor_threshold, min_vel, vxy_max, min_vel));",
     "culprit": "4a5aa1e947", "fix": "b4395d5960",
     "what": "Position mode: max-height slowdown built from velocities and overwritten by other limits, stricter limit lost"},
    {"case": "hover_thrust_sub", "file": "src/modules/mc_att_control/mc_att_control_main.cpp",
     "line": "// Update hover thrust for stick scaling\n\tif (_vehicle_status_sub.updated()) {",
     "culprit": "f08d01b4d5", "fix": "9eaec534ab",
     "what": "Stabilized throttle stick scaling reads the hover-thrust estimate only when vehicle_status updates (stale hover thrust)"},
    {"case": "home_baro_dt_units", "file": "src/modules/commander/HomePosition.cpp",
     "line": "const float dt = baro_data.timestamp - _last_baro_timestamp;",
     "culprit": "6604c52c98", "fix": "0bdf8c2fb0",
     "what": "home altitude baro low-pass fed microseconds as seconds: no smoothing, noisy in-air home altitude correction"},
    {"case": "home_gps_alt_ctrl", "file": "src/modules/commander/HomePosition.cpp",
     "line": "return (ekf2_gps_ctrl & 1);",
     "culprit": "4e59a060a8", "fix": "c51aabcfec",
     "what": "raw GNSS home position and in-air home correction used with GNSS altitude fusion disabled (only horizontal bit checked)"},
    {"case": "quadchute_alt_reset", "file": "src/modules/vtol_att_control/vtol_type.cpp",
     "line": "_quadchute_ref_alt += _local_pos->delta_z;",
     "culprit": "b60e73c76f", "fix": "ca9cb2214f",
     "what": "VTOL quad-chute reference altitude shifted the wrong way on an EKF altitude reset: false or missed altitude-loss quad-chute"},
    {"case": "rtl_reverse_index", "file": "src/modules/navigator/rtl_mission_fast_reverse.cpp",
     "line": "getPreviousPositionItems(math::max(_mission_index_prior_rtl - INT32_C(1), INT32_C(0)), &previous_mission_item_index,",
     "culprit": "17e96554ec", "fix": "305306ad1c",
     "what": "RTL_TYPE=2 fast reverse starts one waypoint too far back (skips the waypoint just left)"},
    {"case": "mission_land_rtl", "file": "src/modules/navigator/mission_base.cpp",
     "line": "if (hasMissionLandStart() && (_mission.current_seq > _mission.land_start_index)) {",
     "culprit": "c94c1ce4d2", "fix": "6daec07bbe",
     "what": "RTL triggered after a mission LAND item counts as landing and continues the mission instead of returning"},
    {"case": "mission_home_alt_shift", "file": "src/modules/navigator/mission_base.cpp",
     "line": "\t\tfloat new_alt = get_absolute_altitude_for_item(_mission_item);\n\t\tfloat altitude_diff",
     "culprit": "37caddedbb", "fix": "3e396f65e5",
     "what": "after a home-altitude change the mission altitude shift is applied for non-position items too: wrong altitude setpoint"},
    {"case": "ekf_ext_pos_var", "file": "src/modules/ekf2/EKF/ekf.cpp",
     "line": "const Vector2f innov_var = Vector2f(getStateVariance<State::vel>()) + obs_var;",
     "culprit": "e04c53241a", "fix": "4bc0286eb8",
     "what": "EKF2 external position reset gate uses velocity variance (and accuracy not squared): wrong innovation test"},
    {"case": "ekf_joseph_ph", "file": "src/modules/ekf2/EKF/ekf_helper.cpp",
     "line": "\tPH = P.row(state_index);",
     "culprit": "d501d8e1d4", "fix": "6d819343aa",
     "what": "EKF2 direct state fusion: Joseph covariance update uses a row for a column, over-optimistic variance when the gain is suboptimal"},
    {"case": "failsafe_spoolup_dup", "file": "src/modules/commander/failsafe/failsafe.cpp",
     "line": "CHECK_FAILSAFE(status_flags, fd_esc_arming_failure, ActionOptions(Action::Disarm).cannotBeDeferred());\n\t\tCHECK_FAILSAFE(status_flags, battery_unhealthy,",
     "culprit": "e06629bfe5", "fix": "a3c387fa85",
     "what": "spool-up battery_unhealthy failsafe shares its state with the in-flight check: 'duplicate check' error, failsafe state corrupted"},
]

UPSTREAMS = {
    "ardupilot": {"bugs": BUGS, "repo": ROOT / "ardupilot", "compare": "compare_ardupilot.json",
                  "out": "known_bugs_scan.json"},
    "px4": {"bugs": PX4_BUGS, "repo": ROOT / "px4", "compare": "compare_PX4-Autopilot.json",
            "out": "known_bugs_scan_px4.json"},
}


def show(repo: Path, ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def carries(text: str | None, bug: dict) -> bool:
    lines = bug["line"] if isinstance(bug["line"], list) else [bug["line"]]
    return text is not None and all(line in text for line in lines)


def verify(upstream="ardupilot"):
    up = UPSTREAMS[upstream]
    ok = True
    for b in up["bugs"]:
        at = lambda ref: carries(show(up["repo"], ref, b["file"]), b)
        row = (at(b["culprit"]), at(b["culprit"] + "^"), at(b["fix"]))
        good = row == (True, False, False)
        ok &= good
        print(f"{'ok ' if good else 'BAD'} {b['case']:24} culprit={row[0]} parent={row[1]} fix={row[2]}")
    return ok


def raw(full_name: str, branch: str, path: str) -> str | None:
    url = f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError:
            return None
        except (urllib.error.URLError, OSError):   # network blip: DNS, reset, timeout
            if attempt == 3:
                raise
            time.sleep(5 * 2 ** attempt)


def scan(upstream="ardupilot", min_custom=5, limit=40, pushed_after="2025-06"):
    up = UPSTREAMS[upstream]
    bugs = up["bugs"]
    forks = json.loads((DATA / up["compare"]).read_text())
    forks = [f for f in forks if f.get("custom_commits", 0) >= min_custom
             and (f.get("pushed_at") or "") >= pushed_after]
    forks.sort(key=lambda f: -f["custom_commits"])
    # resume: a fork already scanned at the same push with the same signatures is not fetched again
    path = DATA / up["out"]
    names = [b["case"] for b in bugs]
    prev = {(r["fork"], r.get("pushed_at")): r for r in json.loads(path.read_text())
            if r.get("checked") == names} if path.exists() else {}
    out = []
    for f in forks[:limit]:
        if (f["full_name"], f.get("pushed_at")) in prev:
            out.append(prev[(f["full_name"], f.get("pushed_at"))])
            continue
        hits, missing, files = [], 0, {}
        for b in bugs:
            if b["file"] not in files:
                files[b["file"]] = raw(f["full_name"], f["default_branch"], b["file"])
                time.sleep(0.2)
            text = files[b["file"]]
            if text is None:
                missing += 1
            elif carries(text, b):
                hits.append(b["case"])
        row = {"fork": f["full_name"], "owner_type": f["owner_type"], "branch": f["default_branch"],
               "custom_commits": f["custom_commits"], "behind_by": f["behind_by"],
               "merge_base_date": f.get("merge_base_date"), "pushed_at": f.get("pushed_at"),
               "checked": names, "carries": hits, "files_missing": missing}
        out.append(row)
        if len(out) % 10 == 0:
            path.write_text(json.dumps(out, indent=1))
        print(f"{len(hits):2} {f['full_name']:42} {f['default_branch']:14} {', '.join(hits)}", flush=True)
    path.write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["verify", "scan"])
    ap.add_argument("--upstream", choices=list(UPSTREAMS), default="ardupilot")
    ap.add_argument("--min-custom", type=int, default=5)
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    if a.cmd == "verify":
        sys.exit(0 if verify(a.upstream) else 1)
    scan(a.upstream, a.min_custom, a.limit)
