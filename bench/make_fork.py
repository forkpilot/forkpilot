"""Build synthetic "company fork" histories on top of ArduPilot for the agent benchmark.

Each case is a branch bench/<case> = base + 30 harmless commits with exactly one
behaviour-changing commit hidden among them. Ground truth goes to bench/truth/<case>.json,
which the agent never sees.

  python bench/make_fork.py                 # all cases
  python bench/make_fork.py guided_ekf_defer
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "ardupilot"
TRUTH = ROOT / "bench" / "truth"
BASE = "cafe67457776027a1bad6e165c409828c1a851e5"   # ArduPilot master, 2 Oct 2026
N_NOISE = 30

# --- edit primitives -------------------------------------------------------


def write(path, text):
    p = REPO / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return [path]


def comment(path, text):
    p = REPO / path
    lines = p.read_text().splitlines(keepends=True)
    lines.insert(1, f"// ACME: {text}\n")
    p.write_text("".join(lines))
    return [path]


def replace(path, old, new, count=1):
    p = REPO / path
    src = p.read_text()
    if src.count(old) < count:
        raise ValueError(f"{path}: expected {count}x {old!r}")
    p.write_text(src.replace(old, new))
    return [path]


# --- harmless fork history -------------------------------------------------

NOISE = [
    ("AP_Acme: add payload library skeleton",
     lambda: write("libraries/AP_Acme/README.md",
                   "# AP_Acme\n\nACME Robotics payload interface (winch + camera trigger).\n"
                   "Not yet wired into the vehicle code.\n")),
    ("AP_Acme: payload interface header",
     lambda: write("libraries/AP_Acme/AP_Acme_Payload.h",
                   "#pragma once\n\n#include <stdint.h>\n\nclass AP_Acme_Payload {\npublic:\n"
                   "    void init() {}\n    bool healthy() const { return _healthy; }\n"
                   "private:\n    bool _healthy;\n    uint32_t _last_msg_ms;\n};\n")),
    ("Tools: add ACME flashing helper",
     lambda: write("Tools/scripts/acme_flash.sh",
                   "#!/bin/sh\n# flash the ACME carrier board over USB\nset -e\n"
                   "./waf configure --board AcmeH7 && ./waf copter --upload\n")),
    ("hwdef: add AcmeH7 carrier board",
     lambda: write("libraries/AP_HAL_ChibiOS/hwdef/AcmeH7/hwdef.dat",
                   "# ACME H7 carrier, electrically a MatekH743 with a second CAN port\n"
                   "include ../MatekH743/hwdef.dat\n")),
    ("Tools: default params for ACME X8 airframe",
     lambda: write("Tools/autotest/default_params/acme_x8.parm",
                   "FRAME_CLASS 4\nFRAME_TYPE 1\nMOT_SPIN_ARM 0.08\nMOT_SPIN_MIN 0.12\n")),
    ("Copter: ACME firmware name",
     lambda: replace("ArduCopter/version.h", '"ArduCopter V4.8.0-dev"', '"ACME Copter V4.8.0-acme1"')),
    ("CI: add ACME code owners",
     lambda: write(".github/CODEOWNERS.acme", "ArduCopter/ @acme/flight-team\nlibraries/AP_Acme/ @acme/payload\n")),
    ("docs: ACME release notes",
     lambda: write("ACME_RELEASE_NOTES.md", "# ACME Copter releases\n\n## acme1\n- based on ArduPilot master (Oct 2026)\n")),
    ("Rover: note ACME ground unit is not supported",
     lambda: comment("Rover/mode.cpp", "ground unit firmware is not maintained in this fork")),
    ("Plane: note ACME does not ship fixed wing",
     lambda: comment("ArduPlane/mode.cpp", "fixed wing is not shipped by ACME")),
    ("Copter: document guided usage by companion computer",
     lambda: comment("ArduCopter/mode_guided.cpp", "GUIDED targets come from the ACME mission computer")),
    ("Copter: document land behaviour for ACME X8",
     lambda: comment("ArduCopter/mode_land.cpp", "X8 lands on skids, see acme_x8.parm")),
    ("Copter: note EKF check owner",
     lambda: comment("ArduCopter/ekf_check.cpp", "reviewed by flight-team for acme1")),
    ("Copter: document RTL expectations",
     lambda: comment("ArduCopter/mode_rtl.cpp", "RTL is the default lost-link action on ACME units")),
    ("Copter: note custom MAVLink handling plan",
     lambda: comment("ArduCopter/GCS_MAVLink_Copter.cpp", "payload messages will be routed to AP_Acme")),
    ("Copter: parameter review note",
     lambda: comment("ArduCopter/Parameters.cpp", "parameter defaults reviewed for acme1")),
    ("AC_PosControl: reviewed for ACME X8",
     lambda: comment("libraries/AC_AttitudeControl/AC_PosControl.cpp", "gains reviewed on X8 airframe")),
    ("AC_WPNav: reviewed for ACME missions",
     lambda: comment("libraries/AC_WPNav/AC_WPNav.cpp", "used for survey missions")),
    ("Copter: land detector note",
     lambda: comment("ArduCopter/land_detector.cpp", "skids make touchdown softer than legs")),
    ("Copter: motors review",
     lambda: comment("ArduCopter/motors.cpp", "arming checks reviewed for acme1")),
    ("Copter: auto mode note",
     lambda: comment("ArduCopter/mode_auto.cpp", "AUTO missions are uploaded by the mission computer")),
    ("Copter: radio note",
     lambda: comment("ArduCopter/radio.cpp", "RC is a backup link on ACME units")),
    ("Copter: failsafe review note",
     lambda: comment("ArduCopter/events.cpp", "failsafe chain reviewed for acme1")),
    ("Copter: takeoff note",
     lambda: comment("ArduCopter/takeoff.cpp", "takeoff is commanded from the mission computer")),
    ("Copter: loiter note",
     lambda: comment("ArduCopter/mode_loiter.cpp", "LOITER is the manual fallback mode")),
    ("Copter: smart RTL note",
     lambda: comment("ArduCopter/mode_smart_rtl.cpp", "SmartRTL disabled in acme_x8.parm")),
    ("AP_Acme: payload timing constants",
     lambda: write("libraries/AP_Acme/AP_Acme_Config.h",
                   "#pragma once\n\n#define ACME_PAYLOAD_TIMEOUT_MS 2000\n#define ACME_TRIGGER_PULSE_MS 50\n")),
    ("Copter: attitude control note",
     lambda: comment("ArduCopter/Attitude.cpp", "attitude limits match X8 frame")),
    ("Copter: logging note",
     lambda: comment("ArduCopter/Log.cpp", "ACME log analysis expects default message set")),
    ("Copter: user hooks note",
     lambda: comment("ArduCopter/UserCode.cpp", "ACME hooks will live here")),
]
assert len(NOISE) == N_NOISE

# --- the commits that change behaviour ---------------------------------------
#
# Each case: `commits` (inserted among the noise; `gap` noise commits between consecutive ones),
# `culprit` = index of the first commit that shows the symptom (None for a clean fork),
# `expect` = scenario -> verdict that detection must show, and notes for grading explanations
# and fixes later: `intent` (what the author wanted), `root_cause`, `good_fix`.
# split "dev" cases may be used to tune the pipeline; "holdout" cases must not be.

EKF_DEFER = ("    AP_Notify::flags.failsafe_ekf = true;\n",
             "    AP_Notify::flags.failsafe_ekf = true;\n\n"
             "    // ACME: the mission computer owns navigation in GUIDED\n"
             "    if (copter.flightmode->mode_number() == Mode::Number::GUIDED) {\n"
             "        gcs().send_text(MAV_SEVERITY_INFO, \"EKF Failsafe: deferred to companion\");\n"
             "        return;\n"
             "    }\n")


def one(message, edit):
    return [(message, edit)]


BUGS = {
    # ---------------------------------------------------------------- dev
    "guided_ekf_defer": dict(
        split="dev", kind="defect", culprit=0,
        commits=one("Copter: let companion computer handle EKF failsafe in GUIDED",
                    lambda: replace("ArduCopter/ekf_check.cpp", *EKF_DEFER)),
        expect={"gps_loss": "FAIL"},
        intent="mission computer should handle navigation faults in GUIDED",
        root_cause="failsafe_ekf_event() returns early in GUIDED, so no LAND is commanded when the "
                   "EKF loses position; the vehicle hovers blind and never lands",
        good_fix="defer only while the mission computer is verifiably alive and acting, with a "
                 "timeout after which the normal EKF failsafe action runs"),
    "ekf_check_slow": dict(
        split="dev", kind="defect", culprit=0,
        commits=one("Copter: reduce nuisance EKF failsafes in gusty conditions",
                    lambda: replace("ArduCopter/ekf_check.cpp",
                                    " # define EKF_CHECK_ITERATIONS_MAX          10      // 1 second (ie. 10 iterations at 10hz) of bad variances signals a failure",
                                    " # define EKF_CHECK_ITERATIONS_MAX          150     // ACME: 15 seconds of bad variances before failsafe")),
        expect={"gps_loss": "FAIL"},
        intent="avoid false EKF failsafes caused by short variance spikes",
        root_cause="EKF_CHECK_ITERATIONS_MAX raised from 10 to 150 iterations at 10 Hz, so a real "
                   "position loss takes ~15 s longer to trigger the failsafe",
        good_fix="keep the 1 s check; tune FS_EKF_THRESH or filter spikes instead of delaying "
                 "detection of sustained bad variance"),
    "pos_p_soft": dict(
        split="dev", kind="intentional", culprit=0,
        commits=one("AC_PosControl: softer horizontal position response for ACME X8",
                    lambda: replace("libraries/AC_AttitudeControl/AC_PosControl.cpp",
                                    " # define POSCONTROL_NE_POS_P                   1.0f",
                                    " # define POSCONTROL_NE_POS_P                   0.6f", count=2)),
        expect={"wind_hover": "DRIFT", "square_mission": "DRIFT"},
        intent="smoother horizontal motion for camera work",
        root_cause="default horizontal position P gain lowered 1.0 -> 0.6: weaker position hold "
                   "in wind and more overshoot at waypoints",
        good_fix="intentional; set PSC_NE_POS_P in the airframe parameter file rather than "
                 "changing the compiled default, and accept the new baseline explicitly"),
    "rtl_loiter_long": dict(
        split="dev", kind="intentional", culprit=0,
        commits=one("Copter: longer RTL loiter so the operator can abort the landing",
                    lambda: replace("ArduCopter/config.h",
                                    " # define RTL_LOITER_TIME           5000 ",
                                    " # define RTL_LOITER_TIME           15000")),
        expect={"wind_hover": "DRIFT"},
        intent="give the operator time to abort an automatic landing",
        root_cause="RTL loiter above home raised from 5 s to 15 s, adding 10 s to every RTL",
        good_fix="intentional; use the RTL_LOIT_TIME parameter and re-baseline"),
    "land_detect_slow": dict(
        split="dev", kind="intentional", culprit=0,
        commits=one("Copter: more conservative land detector for skid landing gear",
                    lambda: replace("ArduCopter/config.h",
                                    " # define LAND_DETECTOR_TRIGGER_SEC         1.0f",
                                    " # define LAND_DETECTOR_TRIGGER_SEC         3.0f")),
        expect={"hover": "DRIFT"},
        intent="avoid false landing detection on soft skids",
        root_cause="land detector needs 3 s instead of 1 s of landed conditions, so motors "
                   "disarm ~2 s later after touchdown",
        good_fix="intentional; re-baseline (or detect landing with the skid geometry instead)"),
    "clean_a": dict(
        split="dev", kind="clean", culprit=None, commits=[], expect={},
        intent="no behaviour change", root_cause="none", good_fix="none"),
    "ne_vel_assign": dict(
        split="dev", kind="defect", culprit=0,
        commits=one("AC_PosControl: assemble NE velocity target in one place",
                    lambda: replace("libraries/AC_AttitudeControl/AC_PosControl.cpp",
                                    "    _vel_target_ned_ms.xy() += _vel_desired_ned_ms.xy() + _vel_offset_ned_ms.xy();",
                                    "    _vel_target_ned_ms.xy() = _vel_desired_ned_ms.xy() + _vel_offset_ned_ms.xy();")),
        # first guess was wind_hover FAIL; observed on the dev run: without position correction
        # the vehicle never closes on AUTO waypoints, while wind only makes the hover drift
        expect={"auto_mission": "FAIL", "wind_hover": "DRIFT"},
        intent="pure refactor, no behaviour change intended",
        root_cause="'+=' became '=', discarding the position-P correction: the horizontal "
                   "position controller no longer corrects position error, only follows feedforward",
        good_fix="restore '+=' so the P-term output and feedforward are summed"),
    "takeoff_cm": dict(
        split="dev", kind="defect", culprit=0,
        commits=one("Copter: GUIDED takeoff altitude in cm to match ACME mission computer API",
                    lambda: replace("ArduCopter/mode_guided.cpp",
                                    "        target_loc.set_alt_m(takeoff_alt_m, Location::AltFrame::ABOVE_HOME);",
                                    "        target_loc.set_alt_m(takeoff_alt_m * 0.01f, Location::AltFrame::ABOVE_HOME);  // ACME API sends cm")),
        expect={"hover": "FAIL", "square_mission": "FAIL"},
        intent="mission computer sends takeoff altitude in centimetres",
        root_cause="MAVLink NAV_TAKEOFF altitude is metres; scaling by 0.01 makes every GUIDED "
                   "takeoff target ~1/100 of the commanded height",
        good_fix="convert cm to m in the mission computer (or at its interface), keep the "
                 "flight code in MAVLink units"),
    "mixed_wp_speed": dict(
        split="dev", kind="intentional", culprit=0,
        commits=one("ACME integration: payload hooks, docs and navigation defaults",
                    lambda: write("libraries/AP_Acme/INTEGRATION.md",
                                  "# Integration notes\n\nPayload hooks live in UserCode.cpp.\n")
                    + comment("ArduCopter/mode_guided.cpp", "guided position targets arrive at 5 Hz")
                    + comment("ArduCopter/mode_auto.cpp", "survey missions use 40 m legs")
                    + replace("libraries/AC_WPNav/AC_WPNav.cpp",
                              "#define WP_SPD_DEFAULT          10.0f",
                              "#define WP_SPD_DEFAULT          5.0f ")
                    + comment("libraries/AC_WPNav/AC_WPNav.cpp", "speeds tuned for survey camera")),
        expect={"square_mission": "DRIFT", "auto_mission": "DRIFT"},
        intent="slower survey legs for the camera payload",
        root_cause="buried in a mixed integration commit, WP_SPD_DEFAULT is halved (10 -> 5 m/s), "
                   "so waypoint legs take longer",
        good_fix="intentional; set WPNAV_SPEED in the airframe params and keep the compiled default"),
    "two_commit_defer": dict(
        split="dev", kind="defect", culprit=1, gap=6,
        commits=[
            ("Copter: optional companion-owned navigation (disabled by default)",
             lambda: replace("ArduCopter/config.h",
                             "#ifndef RTL_LOITER_TIME",
                             "#ifndef ACME_COMPANION_NAV\n # define ACME_COMPANION_NAV 0   // 1: mission computer handles nav faults in GUIDED\n#endif\n\n#ifndef RTL_LOITER_TIME")
             + replace("ArduCopter/ekf_check.cpp", EKF_DEFER[0],
                       EKF_DEFER[1].replace("    // ACME: the mission computer owns navigation in GUIDED\n",
                                            "#if ACME_COMPANION_NAV\n    // ACME: the mission computer owns navigation in GUIDED\n")
                       .replace("        return;\n    }\n", "        return;\n    }\n#endif\n"))),
            ("acme1: enable mission computer navigation",
             lambda: replace("ArduCopter/config.h", " # define ACME_COMPANION_NAV 0 ", " # define ACME_COMPANION_NAV 1 ")),
        ],
        expect={"gps_loss": "FAIL"},
        intent="mission computer should handle navigation faults in GUIDED",
        root_cause="the first commit adds an EKF-failsafe bypass in GUIDED behind "
                   "ACME_COMPANION_NAV; the culprit only flips the flag to 1, enabling it",
        good_fix="fix the bypass added in the earlier commit (defer only while the companion is "
                 "alive, with a timeout) rather than just reverting the flag"),
    "broken_build_rc": dict(
        split="dev", kind="defect", culprit=2,
        commits=[
            ("Copter: add ACME payload user hook (wip)",
             lambda: replace("ArduCopter/UserCode.cpp", "void Copter::userhook_init()",
                             "void acme_payload_hook()\n{\n    acme_payload.update()\n}\n\nvoid Copter::userhook_init()")),
            ("Copter: fix ACME user hook build",
             lambda: replace("ArduCopter/UserCode.cpp",
                             "void acme_payload_hook()\n{\n    acme_payload.update()\n}\n\n",
                             "// ACME payload hook: wired up once AP_Acme lands\n\n")),
            ("Copter: ACME default failsafe options",
             lambda: replace("ArduCopter/Parameters.cpp",
                             "fs_options, (float)Copter::FailsafeOption::GCS_CONTINUE_IF_PILOT_CONTROL)",
                             "fs_options, (float)((uint32_t)Copter::FailsafeOption::GCS_CONTINUE_IF_PILOT_CONTROL | (uint32_t)Copter::FailsafeOption::RC_CONTINUE_IF_GUIDED))")),
        ],
        expect={"rc_loss": "FAIL"},
        intent="mission computer keeps control when the RC link drops",
        root_cause="FS_OPTIONS default gains RC_CONTINUE_IF_GUIDED, so RC loss in GUIDED no "
                   "longer triggers RTL",
        good_fix="leave the default; enable the option only on airframes whose mission computer "
                 "has its own lost-link handling, or bound it with a timeout"),
    "flaky_defer": dict(
        split="dev", kind="defect", culprit=0,
        commits=one("Copter: defer EKF failsafe to companion while its heartbeat is fresh",
                    lambda: replace("ArduCopter/ekf_check.cpp", EKF_DEFER[0],
                                    EKF_DEFER[1].replace(
                                        "    if (copter.flightmode->mode_number() == Mode::Number::GUIDED) {\n",
                                        "    const bool companion_alive = (AP_HAL::millis() & 0x400) != 0;  // TODO: real heartbeat from AP_Acme\n"
                                        "    if (copter.flightmode->mode_number() == Mode::Number::GUIDED && companion_alive) {\n"))),
        expect={"gps_loss": "FAIL"},
        # how this one surfaces: an occasional red gps_loss in CI, which starts the investigation
        reported=["gps_loss"],
        intent="defer the EKF failsafe only while the mission computer is alive",
        root_cause="the 'heartbeat' is a placeholder that is true in alternating ~1 s windows, "
                   "so depending on timing the failsafe is suppressed in some runs (~10% observed)",
        good_fix="use a real companion heartbeat timestamp and fall back to the EKF failsafe "
                 "action after a timeout"),
    # ------------------------------------------------------------ holdout
    "batt_guided_skip": dict(
        split="holdout", kind="defect", culprit=0,
        commits=one("Copter: smart BMS on mission computer handles low battery in GUIDED",
                    lambda: replace("ArduCopter/events.cpp",
                                    "    LOGGER_WRITE_ERROR(LogErrorSubsystem::FAILSAFE_BATT, LogErrorCode::FAILSAFE_OCCURRED);\n",
                                    "    LOGGER_WRITE_ERROR(LogErrorSubsystem::FAILSAFE_BATT, LogErrorCode::FAILSAFE_OCCURRED);\n\n"
                                    "    // ACME: the smart BMS on the mission computer decides in GUIDED\n"
                                    "    if (flightmode->in_guided_mode()) {\n"
                                    "        return;\n"
                                    "    }\n")),
        expect={"battery_low": "FAIL"},
        intent="mission computer's smart BMS decides what to do on low battery in GUIDED",
        root_cause="handle_battery_failsafe() returns early in GUIDED, so a low battery never "
                   "triggers RTL",
        good_fix="only defer while the mission computer is alive and acting; otherwise run the "
                 "configured BATT_FS_LOW_ACT"),
    "wp_radius_big": dict(
        split="holdout", kind="intentional", culprit=0,
        commits=one("AC_WPNav: larger default waypoint radius for smoother survey corners",
                    lambda: replace("libraries/AC_WPNav/AC_WPNav.cpp",
                                    "#define WP_RADIUS_M_DEFAULT     2.0f",
                                    "#define WP_RADIUS_M_DEFAULT     8.0f")),
        expect={"auto_mission": "DRIFT"},
        intent="smoother corners on survey missions",
        root_cause="WP_RADIUS_M_DEFAULT 2 -> 8 m: AUTO waypoints count as reached 8 m early and "
                   "corners are cut",
        good_fix="intentional; set WPNAV_RADIUS_M per airframe and re-baseline"),
    "land_speed_fast": dict(
        split="holdout", kind="intentional", culprit=0,
        commits=one("Copter: faster final descent to save battery",
                    lambda: replace("ArduCopter/config.h",
                                    " # define LAND_SPD_MS_DEFAULT    0.5f",
                                    " # define LAND_SPD_MS_DEFAULT    1.0f")),
        expect={"hover": "DRIFT"},
        intent="shorter landings to save battery",
        root_cause="final landing descent speed doubled 0.5 -> 1.0 m/s, shortening every landing",
        good_fix="intentional; set LAND_SPD_MS per airframe, check touchdown loads, re-baseline"),
    "clean_b": dict(
        split="holdout", kind="clean", culprit=None, commits=[], expect={},
        intent="no behaviour change", root_cause="none", good_fix="none"),
    "rc_fs_land": dict(
        split="holdout", kind="intentional", culprit=0,
        commits=one("Copter: land in place on RC loss (ACME operations policy)",
                    lambda: replace("ArduCopter/events.cpp",
                                    "        case FS_THR_Action::ALWAYS_RTL:\n        case FS_THR_Action::CONTINUE_MISSION:\n            desired_action = FailsafeAction::RTL;",
                                    "        case FS_THR_Action::ALWAYS_RTL:\n        case FS_THR_Action::CONTINUE_MISSION:\n            desired_action = FailsafeAction::LAND;  // ACME ops policy")),
        expect={"rc_loss": "FAIL"},
        intent="operations policy: land where the RC link was lost",
        root_cause="FS_THR_ENABLE=1 (documented as RTL) now lands in place, so the vehicle "
                   "lands ~30 m from home",
        good_fix="set FS_THR_ENABLE=5 (Land) in the airframe params instead of changing what "
                 "the RTL option does"),
}


def git(*args, env=None):
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def make(case: str, seed: int) -> dict:
    import os
    bug = BUGS[case]
    rng = random.Random(seed)
    order = list(range(N_NOISE))
    rng.shuffle(order)
    pos = rng.randint(5, N_NOISE - 5)
    plan = [("noise", i) for i in order]
    at = []
    for k in range(len(bug["commits"])):
        i = pos if k == 0 else at[-1] + 1 + bug.get("gap", 0)
        plan.insert(i, ("bug", k))
        at.append(i)

    start = git("rev-parse", "--abbrev-ref", "HEAD")
    branch = f"bench/{case}"
    git("checkout", "-q", "-B", branch, BASE)
    shas = []
    try:
        for n, (kind, key) in enumerate(plan):
            msg, edit = bug["commits"][key] if kind == "bug" else NOISE[key]
            paths = edit()
            git("add", "--", *paths)
            when = f"2026-10-0{3 + n // 12}T{9 + n % 12:02d}:00:00+03:00"
            env = {**os.environ, "GIT_AUTHOR_NAME": "ACME Dev", "GIT_AUTHOR_EMAIL": "dev@acme.example",
                   "GIT_COMMITTER_NAME": "ACME Dev", "GIT_COMMITTER_EMAIL": "dev@acme.example",
                   "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
            git("commit", "-q", "-m", msg, env=env)
            shas.append(git("rev-parse", "HEAD"))
    finally:
        git("checkout", "-q", start)
    culprit = None if bug["culprit"] is None else shas[at[bug["culprit"]]]
    truth = {"case": case, "split": bug["split"], "branch": branch, "good": BASE, "bad": shas[-1],
             "culprit": culprit, "related": [shas[i] for i in at], "positions": at,
             "commits": len(shas), "expect": bug["expect"], "kind": bug["kind"],
             "intent": bug["intent"], "root_cause": bug["root_cause"], "good_fix": bug["good_fix"],
             "reported": bug.get("reported", [])}
    TRUTH.mkdir(parents=True, exist_ok=True)
    (TRUTH / f"{case}.json").write_text(json.dumps(truth, indent=1))
    return truth


if __name__ == "__main__":
    cases = sys.argv[1:] or list(BUGS)
    for c in cases:
        t = make(c, seed=1000 + list(BUGS).index(c))
        where = ", ".join(f"#{p}" for p in t["positions"]) or "-"
        print(f"{c:18} {t['split']:8} commit {where:10} / {t['commits']}  "
              f"culprit {(t['culprit'] or '-')[:10]}  bad {t['bad'][:10]}")
