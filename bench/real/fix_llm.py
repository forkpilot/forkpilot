"""Score LLM fix proposals on real regressions ForkPilot localized: does a model's patch, built and
flown, bring the symptom back to the good commit (the oracle's 'fixes'), with no new finding?

  python bench/real/fix_llm.py --model qwen/qwen3.8-27b [--attempts 3] [CASE ...]

LLM candidates only (the revert and the upstream fix were scored by fix_real.py). Any
OpenAI-compatible endpoint: FP_LLM_BASE_URL (default here: OpenRouter) and FP_LLM_API_KEY; when
the key is not set it is read from ~/.config/forkpilot/openrouter.env (one `FP_LLM_API_KEY=...`
line), never printed. The model sees evidence.md and the code at `bad`: the bisect's diff, not the
upstream fix. Upstream fixes are public, so a model may have seen them in training.

Per case and model: fix.json/fix.md are kept as fix-llm-<model>.json/.md in the investigation,
and one line goes to bench/real/results/fix-llm.jsonl (resumable per case and model).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from forkpilot.explain import make_backend  # noqa: E402
from forkpilot.fix import fix  # noqa: E402

OUT = ROOT / "bench" / "real" / "results" / "fix-llm.jsonl"
KEY_FILE = Path.home() / ".config" / "forkpilot" / "openrouter.env"

# localized with the right culprit (bench/real/results), scenarios suite
CASES = {
    "poshold_brake_units": "investigations/20261004-011501",
    "body_frame_rotation": "investigations/20261004-012327",
    "land_noGPS_alt_cm": "investigations/20261004-011809",
    "guided_fence_units": "investigations/20261004-013827",
    "posctrl_down_sign_flip": "investigations/20261005-003953",
    "plane_training_shaping_stale": "investigations/20261005-005044",
    "qplane_spoolup_block_stuck": "investigations/20261005-004715",
}


def load_key():
    if os.environ.get("FP_LLM_API_KEY") or not KEY_FILE.exists():
        return
    for line in KEY_FILE.read_text().splitlines():
        k, _, v = line.strip().partition("=")
        if k == "FP_LLM_API_KEY" and v:
            os.environ["FP_LLM_API_KEY"] = v.strip().strip('"')


def slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "-", model)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*", help=f"default: all of {', '.join(CASES)}")
    ap.add_argument("--model", required=True)
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--repo", default=str(ROOT / "ardupilot"))
    ap.add_argument("-j", "--jobs", type=int, default=12)
    a = ap.parse_args()
    os.environ.setdefault("FP_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    load_key()
    if not os.environ.get("FP_LLM_API_KEY"):
        sys.exit(f"no FP_LLM_API_KEY and no key in {KEY_FILE}")
    backend = make_backend("local", a.model)
    done = set()
    if OUT.exists():
        done = {(r["case"], r["model"]) for r in map(json.loads, OUT.read_text().splitlines()) if "outcome" in r}
    for name in a.cases or CASES:
        if (name, a.model) in done:
            continue
        inv = ROOT / CASES[name]
        t0 = time.time()
        print(f"== {name} ({a.model})", flush=True)
        row = {"case": name, "model": a.model, "investigation": CASES[name]}
        try:
            fix(inv, backend, attempts=a.attempts, revert=False, repo=Path(a.repo), jobs=a.jobs)
            data = json.loads((inv / "fix.json").read_text())
            for ext in ("json", "md"):
                shutil.copy2(inv / f"fix.{ext}", inv / f"fix-llm-{slug(a.model)}.{ext}")
            cands = data["candidates"]
            outs = [c["outcome"] for c in cands]
            row.update(outcome="fixes" if "fixes" in outs else (outs[-1] if outs else "none"),
                       attempts=outs, before_after=data.get("before_after"),
                       winner_diff_lines=next((c.get("diff_lines") for c in cands if c["outcome"] == "fixes"), None))
        except Exception as e:      # noqa: BLE001  one case must not stop the run
            row["error"] = f"{type(e).__name__}: {str(e)[-400:]}"
        row["seconds"] = round(time.time() - t0)
        print(json.dumps({k: v for k, v in row.items() if k != "before_after"}), flush=True)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT, "a") as f:
            f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
