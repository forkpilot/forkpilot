"""Deterministic verdicts. The oracle — not the AI — decides PASS / FAIL / DRIFT.

FAIL  : an absolute rule from the scenario's `expect:` block is violated,
        or the scenario did not complete.
DRIFT : no rule violated, but a metric moved outside the band observed across
        baseline runs (mean ± max(k·std, rel_tol·|mean|, abs_tol)).
PASS  : otherwise.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass

from .i18n import t

OPS = {"lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
       "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b}


@dataclass
class Finding:
    metric: str
    kind: str          # "rule" | "drift"
    value: float
    expected: str
    side: str = ""     # for drift: "+" above the band, "-" below it

    def signature(self) -> tuple:
        """What makes two findings 'the same symptom' (used by bisect)."""
        return (self.metric, self.kind, self.side)

    def __str__(self):
        return f"{self.kind.upper():5} {self.metric}: {self.value:.3f} ({t('oracle.expected')} {self.expected})"


def check_rules(metrics: dict, expect: dict) -> list[Finding]:
    out = []
    if metrics.get("completed", 0) < 1:
        out.append(Finding("completed", "rule", metrics.get("completed", 0), "== 1"))
    for name, rule in (expect or {}).items():
        if name not in metrics:
            out.append(Finding(name, "rule", float("nan"), t("oracle.missing")))
            continue
        for op, bound in rule.items():
            if not OPS[op](metrics[name], bound):
                out.append(Finding(name, "rule", metrics[name], f"{op} {bound}"))
    return out


def band(values: list[float], k=3.0, rel_tol=0.10, abs_tol=0.05):
    mean = st.fmean(values)
    sd = st.stdev(values) if len(values) > 1 else 0.0
    half = max(k * sd, rel_tol * abs(mean), abs_tol)
    return mean - half, mean + half


def check_drift(candidate: list[dict], baseline: list[dict], **tol) -> list[Finding]:
    out = []
    names = set().union(*baseline) & set().union(*candidate)
    for name in sorted(names - {"completed"}):
        # non-finite values (e.g. "no failsafe reaction" = inf) are for the absolute rules to
        # judge; they have no place in a statistical band
        base = [m[name] for m in baseline if name in m and m[name] is not None and math.isfinite(m[name])]
        cand = [m[name] for m in candidate if name in m and m[name] is not None and math.isfinite(m[name])]
        if not base or not cand:
            continue
        lo, hi = band(base, **tol)
        value = st.fmean(cand)
        if not lo <= value <= hi:
            out.append(Finding(name, "drift", value, f"[{lo:.3f}, {hi:.3f}]",
                               "+" if value > hi else "-"))
    return out


def shift_p(base: list[float], cand: list[float], side: str) -> float:
    """One-sided Mann-Whitney p-value that `cand` sits above (side "+") or below ("-") `base`.
    Exact over all rank assignments (small samples are the normal case here); ties count half."""
    from itertools import combinations
    pooled = base + cand
    n, k = len(pooled), len(cand)

    def u(idx):
        # pairs (c, b) where the candidate value beats the baseline value in the asked direction
        c = [pooled[i] for i in idx]
        b = [pooled[i] for i in range(n) if i not in idx]
        sign = 1 if side == "+" else -1
        return sum((sign * (x - y) > 0) + 0.5 * (x == y) for x in c for y in b)

    observed = u(set(range(len(base), n)))
    combos = list(combinations(range(n), k))
    if len(combos) > 50000:      # never needed at our run counts; keep it bounded anyway
        combos = combos[:: len(combos) // 50000 + 1]
    return sum(u(set(c)) >= observed for c in combos) / len(combos)


def fail_rate_p(cand_fail: int, cand_n: int, base_fail: int, base_n: int) -> float:
    """One-sided Fisher exact p-value that the candidate breaks a rule more often than the
    baseline. 1 of 6 against 0 of 8 is p = 0.43: no evidence at all."""
    from math import comb
    t, n = cand_fail + base_fail, cand_n + base_n
    if not cand_n or not t:
        return 1.0
    return sum(comb(cand_n, k) * comb(base_n, t - k)
               for k in range(cand_fail, min(cand_n, t) + 1)) / comb(n, t)


def confirm_shift(base: list[float], cand: list[float], side: str, alpha=0.01,
                  rel_tol=0.10, abs_tol=0.05) -> bool:
    """A drift is confirmed when the candidate runs as a group sit on one side of the baseline
    runs (rank test) AND the mean moved by more than the tolerance: a shift that is both real
    and big enough to matter. Both sides need several runs; one run against a few is a screen."""
    base = [x for x in base if x is not None and math.isfinite(x)]
    cand = [x for x in cand if x is not None and math.isfinite(x)]
    if len(base) < 3 or len(cand) < 3:
        return False
    moved = st.fmean(cand) - st.fmean(base)
    if (moved > 0) != (side == "+") or abs(moved) <= max(rel_tol * abs(st.fmean(base)), abs_tol):
        return False
    return shift_p(base, cand, side) < alpha


def verdict(candidate: list[dict], expect: dict, baseline: list[dict] | None = None):
    fails = [f for m in candidate for f in check_rules(m, expect)]
    # report each failing metric once
    seen = set()
    fails = [f for f in fails if not (f.metric in seen or seen.add(f.metric))]
    if fails:
        return "FAIL", fails
    drift = check_drift(candidate, baseline) if baseline else []
    return ("DRIFT" if drift else "PASS"), drift
