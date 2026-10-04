"""Where things live and how parallel to run. No third-party imports: `forkpilot doctor` must work
on a machine whose environment is broken."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]     # where the code lives
# builds/, results/ and investigations/ go here: the source tree unless FP_HOME says otherwise
WORK = Path(os.environ.get("FP_HOME") or ROOT).expanduser().resolve()
# shipped scenarios: next to the code in a checkout or editable install, inside the package in a wheel
SCENARIOS = next((d for d in (ROOT / "scenarios", Path(__file__).parent / "scenarios") if d.is_dir()),
                 ROOT / "scenarios")
# 20x with 12 parallel SITLs: same verdicts and metric spread as 10x/6 (measured 2026-10-03), 3x faster
SPEEDUP = 20
JOBS = min(12, os.cpu_count() or 12)     # 12 was measured on 16 cores; fewer cores, fewer parallel SITLs
CCACHE = Path("/usr/lib/ccache")         # compiler symlinks to ccache (Debian/Ubuntu package)
