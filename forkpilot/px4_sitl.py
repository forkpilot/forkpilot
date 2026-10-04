"""Launch a headless PX4 SITL instance with the built-in SIH simulator and talk to it over MAVLink.

Ports: PX4 instance i (`-i i`) listens for its GCS link on udp 18570+i and, until it hears from a
GCS, sends to udp 14550, the same port for every instance. We therefore never listen on 14550: we
send to 18570+i from a socket of our own, and PX4 answers to the address it last heard from. Two
instances cannot mix, and the instance number is claimed machine-wide (px4 also keys its lock file
/tmp/px4_lock-i and its command socket by it).
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from pymavlink import mavutil

from .sitl import HOME

M = mavutil.mavlink
# airframe (SYS_AUTOSTART) and model name per frame; the model name must match the airframe file
FRAMES = {"quadx": (10040, "sihsim_quadx"), "airplane": (10041, "sihsim_airplane"),
          "standard_vtol": (10043, "sihsim_standard_vtol")}
# rcS stretches these by PX4_SIM_SPEED_FACTOR because a GCS sends in wall time; ours sends in sim
# time, so the flight keeps the firmware's own defaults at any speed. Except manual control loss:
# 0.5 s of sim time is 25 ms of wall time at 20x, and a busy machine can delay one MANUAL_CONTROL
# that long (2 of 150 A/A runs failed safe mid-flight); 2 s keeps that out of the results
SPEED_STRETCHED = {"COM_DL_LOSS_T": 10, "COM_RC_LOSS_T": 2.0, "COM_OF_LOSS_T": 1.0, "COM_DISARM_PRFLT": 10}

# PX4 encodes the flight mode in custom_mode: main mode in bits 16-23, sub mode in bits 24-31
MAIN = {"MANUAL": 1, "ALTCTL": 2, "POSCTL": 3, "AUTO": 4, "ACRO": 5, "OFFBOARD": 6, "STABILIZED": 7}
AUTO_SUB = {"READY": 1, "TAKEOFF": 2, "LOITER": 3, "MISSION": 4, "RTL": 5, "LAND": 6, "FOLLOW_TARGET": 8,
            "PRECLAND": 9, "VTOL_TAKEOFF": 10}
POSCTL_SUB = {"ORBIT": 1, "SLOW": 2}
MODES = {**{m: (n, 0) for m, n in MAIN.items() if m != "AUTO"},
         **{f"AUTO.{s}": (MAIN["AUTO"], n) for s, n in AUTO_SUB.items()},
         **{f"POSCTL.{s}": (MAIN["POSCTL"], n) for s, n in POSCTL_SUB.items()}}


def encode_mode(name: str) -> int:
    main, sub = MODES[name]
    return (main << 16) | (sub << 24)


def decode_mode(custom_mode: int) -> str:
    main, sub = (custom_mode >> 16) & 0xFF, (custom_mode >> 24) & 0xFF
    return next((n for n, ms in MODES.items() if ms == (main, sub)),
                next((n for n, ms in MODES.items() if ms == (main, 0)), f"{main}.{sub}"))


def ports(instance: int) -> dict:
    """UDP ports PX4 instance `instance` binds (px4-rc.mavlink), and the GCS port we send to."""
    return {"gcs": 18570 + instance, "offboard": 14580 + instance, "payload": 14280 + instance,
            "gimbal": 13030 + instance, "sih_display": 19450 + instance}


def udp_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def float_bits(value: float, ptype: int) -> float:
    """PX4 sends and takes integer parameters bytewise in PARAM_VALUE/PARAM_SET's float field."""
    if ptype in (M.MAV_PARAM_TYPE_INT32, M.MAV_PARAM_TYPE_UINT32, M.MAV_PARAM_TYPE_INT16,
                 M.MAV_PARAM_TYPE_INT8, M.MAV_PARAM_TYPE_UINT8, M.MAV_PARAM_TYPE_UINT16):
        return struct.unpack("<f", struct.pack("<i", int(round(value))))[0]
    return float(value)


def from_bits(value: float, ptype: int) -> float:
    if ptype == M.MAV_PARAM_TYPE_REAL32:
        return value
    return float(struct.unpack("<i", struct.pack("<f", value))[0])



@dataclass
class Px4Sitl:
    binary_path: Path
    frame: str = "quadx"
    speedup: int = 1
    instance: int = 0
    streams: dict = field(default_factory=dict)     # message id -> Hz (0: off)
    home: str = HOME

    proc: subprocess.Popen | None = None
    mav: mavutil.mavfile | None = None
    workdir: Path | None = None
    log_path: Path | None = None

    @property
    def binary(self) -> Path:
        return Path(self.binary_path)

    @property
    def etc(self) -> Path:
        return self.binary.resolve().parent.parent / "etc"

    def env(self) -> dict:
        autostart, model = FRAMES[self.frame]
        lat, lon, alt, yaw = self.home.split(",")
        return {**os.environ, "PX4_SYS_AUTOSTART": str(autostart), "PX4_SIM_MODEL": model, "HEADLESS": "1",
                "PX4_SIM_SPEED_FACTOR": str(self.speedup), "PX4_HOME_LAT": lat, "PX4_HOME_LON": lon,
                "PX4_HOME_ALT": alt, "PX4_HOME_YAW": yaw, "LC_ALL": "C"}

    def start(self, timeout: float = 60) -> "Px4Sitl":
        self.workdir = Path(tempfile.mkdtemp(prefix="forkpilot-px4-"))
        self.log_path = self.workdir / "px4.log"
        cmd = [str(self.binary.resolve()), "-d", "-i", str(self.instance), "-w", str(self.workdir),
               "-s", "etc/init.d-posix/rcS", str(self.etc)]
        self.proc = subprocess.Popen(cmd, cwd=self.workdir, env=self.env(), stdout=open(self.log_path, "w"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
        try:
            self._connect(timeout)
            # the GCS link streams a lot at 50 Hz: thin it out before anything else, or with
            # several instances running the parser falls behind the simulation
            for msg_id, hz in self.streams.items():
                self.set_message_rate(msg_id, hz)
        except BaseException:
            self.stop()
            raise
        return self

    def _connect(self, timeout: float):
        self.mav = mavutil.mavlink_connection(f"udpout:127.0.0.1:{ports(self.instance)['gcs']}",
                                              source_system=255, source_component=190)
        want = self.instance + 1           # rcS: MAV_SYS_ID = instance + 1
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"PX4 exited at start (code {self.proc.returncode}): {self.log_tail()}")
            self.heartbeat()
            m = self.mav.recv_match(type="HEARTBEAT", blocking=True, timeout=0.5)
            if m and m.get_srcSystem() == want and m.get_srcComponent() == 1:
                self.mav.target_system, self.mav.target_component = want, 1
                return
        raise RuntimeError(f"PX4 SITL did not come up: {self.log_tail()}")

    def log_tail(self, n: int = 400) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-n:].replace("\n", " | ")
        except (OSError, AttributeError):
            return ""

    def stop(self):
        if self.mav:
            self.mav.close()
            self.mav = None
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)     # px4 forks nothing we want to keep
            except ProcessLookupError:
                pass
            self.proc.wait(timeout=10)
        if self.workdir and not os.environ.get("FORKPILOT_KEEP_SITL"):
            shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # --- MAVLink helpers -------------------------------------------------

    def heartbeat(self):
        self.mav.mav.heartbeat_send(M.MAV_TYPE_GCS, M.MAV_AUTOPILOT_INVALID, 0, 0, M.MAV_STATE_ACTIVE)

    def _param_value(self, name: str, timeout: float, send):
        """Send (re-sent up to 3 times) and wait for the PARAM_VALUE of `name`, wall-time bounded:
        recv_match with a type filter only checks its timeout when nothing at all arrives."""
        for _ in range(3):
            send()
            deadline = time.time() + timeout
            while time.time() < deadline:
                m = self.mav.recv_match(blocking=True, timeout=timeout)
                if m is not None and m.get_type() == "PARAM_VALUE" and m.param_id == name:
                    return m
        return None

    def get_param(self, name: str, timeout: float = 5) -> tuple[float, int]:
        m = self._param_value(name, timeout, lambda: self.mav.mav.param_request_read_send(
            self.mav.target_system, self.mav.target_component, name.encode(), -1))
        if m is None:
            raise RuntimeError(f"parameter {name} not found (does this firmware have it?)")
        return from_bits(m.param_value, m.param_type), m.param_type

    def set_param(self, name: str, value: float, timeout: float = 5):
        _, ptype = self.get_param(name, timeout)
        m = self._param_value(name, timeout, lambda: self.mav.mav.param_set_send(
            self.mav.target_system, self.mav.target_component, name.encode(), float_bits(value, ptype), ptype))
        if m is None or abs(from_bits(m.param_value, m.param_type) - float(value)) > 1e-4 * max(1, abs(value)):
            raise RuntimeError(f"could not set {name}={value}: not acknowledged")

    def command(self, cmd: int, *p: float):
        p = list(p) + [0] * (7 - len(p))
        self.mav.mav.command_long_send(self.mav.target_system, self.mav.target_component, cmd, 0, *p)

    def set_message_rate(self, msg_id: int, hz: float):
        self.command(M.MAV_CMD_SET_MESSAGE_INTERVAL, msg_id, 1e6 / hz if hz > 0 else -1)

    def set_mode(self, mode: str):
        main, sub = MODES[mode]
        self.command(M.MAV_CMD_DO_SET_MODE, M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, main, sub)
