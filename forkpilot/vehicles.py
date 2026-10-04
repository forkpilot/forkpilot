"""Vehicles ForkPilot flies: build target, SITL binary, frames and their default parameters.

A scenario names its vehicle (`vehicle: plane`, default copter) and optionally a frame
(`frame: quadplane`). Copter scenarios fly exactly as before, through runner.run_scenario.
A PX4 scenario says `autopilot: px4`: its vehicle is a PX4 multicopter flown with the SIH simulator
through px4_runner; everything else here is ArduPilot.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Frame:
    model: str                   # SITL --model
    defaults: tuple[str, ...]    # parameter files under Tools/autotest, in load order


@dataclass(frozen=True)
class Vehicle:
    name: str
    target: str                  # ./waf <target>
    binary: str                  # build/sitl/bin/<binary>
    frames: dict
    default_frame: str
    autopilot: str = "ardupilot"

    def frame(self, name: str | None) -> Frame:
        name = name or self.default_frame
        if name not in self.frames:
            raise ValueError(f"{self.name}: unknown frame {name!r} (known: {', '.join(self.frames)})")
        return self.frames[name]

    def defaults(self) -> set[str]:
        return {d for f in self.frames.values() for d in f.defaults}


# frames and parameter files as in Tools/autotest/pysim/vehicleinfo
VEHICLES = {
    "copter": Vehicle("copter", "copter", "arducopter",
                      {"quad": Frame("quad", ("default_params/copter.parm",))}, "quad"),
    "plane": Vehicle("plane", "plane", "arduplane",
                     {"plane": Frame("plane", ("models/plane.parm",)),
                      "quadplane": Frame("quadplane", ("default_params/quadplane.parm",))},
                     "plane"),
    # target is the CMake config, binary px4's own name, frames SIH airframes (px4_sitl.FRAMES)
    "px4": Vehicle("px4", "px4_sitl_default", "px4", {"quadx": Frame("sihsim_quadx", ())}, "quadx", "px4"),
}
AUTOPILOTS = ("ardupilot", "px4")


def get(name: str | None) -> Vehicle:
    name = name or "copter"
    if name not in VEHICLES:
        raise ValueError(f"unknown vehicle {name!r} (known: {', '.join(VEHICLES)})")
    return VEHICLES[name]


def of_binary(binary: Path) -> Vehicle | None:
    return next((v for v in VEHICLES.values() if Path(binary).name == v.binary), None)


def scenario_vehicle(path: Path) -> str:
    spec = yaml.safe_load(Path(path).read_text()) or {}
    return "px4" if spec.get("autopilot") == "px4" else spec.get("vehicle") or "copter"


def for_autopilot(autopilot: str | None, vehicle: str | None) -> str:
    """The vehicle key of `--autopilot` and `--vehicle` together: PX4 flies its multicopter."""
    return "px4" if autopilot == "px4" else vehicle or "copter"


def defaults_for(frame: Frame, binary: Path, ardupilot: Path) -> str:
    """The frame's parameter files: the copies saved with a cached build (they belong to the
    binary's commit), else the checkout's. Comma-separated, as SITL's --defaults takes them."""
    out = []
    for d in frame.defaults:
        saved = Path(binary).parent / Path(d).name
        out.append(str(saved if saved.exists() else Path(ardupilot) / "Tools" / "autotest" / d))
    return ",".join(out)


def run_scenario(path: Path, **kw):
    """Fly one scenario with the runner of its vehicle."""
    v = scenario_vehicle(path)
    if v == "copter":
        from .runner import run_scenario as copter
        return copter(path, **kw)
    if v == "px4":
        from .px4_runner import run_scenario as px4
        return px4(path, **kw)
    from .plane import run_scenario as plane
    return plane(path, **kw)
