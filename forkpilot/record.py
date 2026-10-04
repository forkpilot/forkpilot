"""Investigation record: an append-only log of every step, written to disk as it happens."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .battery import WORK

INVESTIGATIONS = WORK / "investigations"


class Record:
    def __init__(self, **meta):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = INVESTIGATIONS / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "record.json"
        self.data = {"started": stamp, **meta, "steps": []}
        self.t0 = time.time()
        self._flush()

    def step(self, kind: str, **data):
        self.data["steps"].append({"kind": kind, "at_s": round(time.time() - self.t0, 1), **data})
        self._flush()

    def set(self, **data):
        self.data.update(data)
        self._flush()

    def _flush(self):
        self.path.write_text(json.dumps(self.data, indent=1, default=str))

    def log(self, msg: str):
        print(f"[{time.time() - self.t0:7.1f}s] {msg}", flush=True)
