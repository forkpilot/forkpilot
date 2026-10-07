"""Launch a headless ArduPilot SITL instance and talk to it over MAVLink."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from pymavlink import mavutil

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARDUPILOT = ROOT / "ardupilot"
HOME = "-35.363261,149.165230,584,353"  # ArduPilot's standard CMAC test field


def _closed():
    raise ConnectionError("SITL closed the MAVLink connection")


@dataclass
class Sitl:
    ardupilot: Path = DEFAULT_ARDUPILOT
    vehicle: str = "arducopter"
    model: str = "quad"
    speedup: int = 10
    instance: int = 0
    params: dict = field(default_factory=dict)
    binary_path: Path | None = None
    defaults: str = ""      # --defaults; empty: Copter's, saved with a cached build
    boot_params: dict = field(default_factory=dict)     # read at boot: drivers a set after boot misses

    proc: subprocess.Popen | None = None
    mav: mavutil.mavfile | None = None
    workdir: Path | None = None

    @property
    def binary(self) -> Path:
        if self.binary_path:
            return Path(self.binary_path)
        return self.ardupilot / "build" / "sitl" / "bin" / self.vehicle

    @property
    def port(self) -> int:
        return 5760 + 10 * self.instance

    def start(self, timeout: float = 60) -> "Sitl":
        self.workdir = Path(tempfile.mkdtemp(prefix="forkpilot-sitl-"))
        defaults = self.defaults or self.binary.parent / "copter.parm"      # saved with a cached build
        if not self.defaults and not defaults.exists():
            defaults = self.ardupilot / "Tools" / "autotest" / "default_params" / "copter.parm"
        if self.boot_params:
            boot = self.workdir / "boot.parm"
            boot.write_text("".join(f"{k} {v}\n" for k, v in self.boot_params.items()))
            defaults = f"{defaults},{boot}"
        cmd = [str(self.binary), "--model", self.model, "--speedup", str(self.speedup),
               "-I", str(self.instance), "--home", HOME, "--defaults", str(defaults), "-w"]
        self.proc = subprocess.Popen(cmd, cwd=self.workdir, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # retries=1: one connect attempt that raises; this loop retries, without
                # pymavlink's own "Connection refused sleeping" lines on the console
                self.mav = mavutil.mavlink_connection(f"tcp:127.0.0.1:{self.port}",
                                                      source_system=255, autoreconnect=False,
                                                      retries=1)
                # without autoreconnect pymavlink keeps reading a closed socket and prints
                # "EOF on TCP socket" in a busy loop; fail the attempt instead
                self.mav.handle_eof = _closed
                if self.mav.wait_heartbeat(timeout=5):
                    break
            except OSError:
                if self.mav:
                    self.mav.close()
                    self.mav = None
                if self.proc.poll() is not None:
                    break
                time.sleep(0.5)
        else:
            self.stop()
            raise RuntimeError("SITL did not come up")
        if self.proc.poll() is not None:
            self.stop()
            raise RuntimeError(f"SITL exited at start (code {self.proc.returncode})")
        for name, value in self.params.items():
            self.set_param(name, value)
        return self

    def stop(self):
        if self.mav:
            self.mav.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.workdir and not os.environ.get("FORKPILOT_KEEP_SITL"):
            shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # --- MAVLink helpers -------------------------------------------------

    def set_param(self, name: str, value: float, timeout: float = 5):
        for _ in range(3):
            self.mav.mav.param_set_send(self.mav.target_system, self.mav.target_component,
                                        name.encode(), float(value),
                                        mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
            # any message, deadline in wall time: recv_match with a type filter checks its timeout
            # only when nothing arrives, so with telemetry flowing an unanswered set waited for ever
            deadline, msg = time.time() + timeout, None
            while msg is None and time.time() < deadline:
                m = self.mav.recv_match(blocking=True, timeout=timeout)
                if m is not None and m.get_type() == "PARAM_VALUE" and m.param_id == name:
                    msg = m
            if msg and abs(msg.param_value - float(value)) < 1e-4:
                return
        raise RuntimeError(f"could not set {name}={value}: not acknowledged (does this firmware "
                           "have the parameter?)")

    def command(self, cmd: int, *p: float):
        p = list(p) + [0] * (7 - len(p))
        self.mav.mav.command_long_send(self.mav.target_system, self.mav.target_component,
                                       cmd, 0, *p)

    def set_message_rate(self, msg_id: int, hz: float):
        self.command(mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, msg_id, 1e6 / hz)

    def set_mode(self, mode: str):
        self.mav.set_mode(self.mav.mode_mapping()[mode])
