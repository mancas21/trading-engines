"""
plan_gate.py — creation-time plan gate (audit action 9, 2026-09-17).

Rejects plans whose geometry cannot pay for itself, BEFORE they reach /calls:
  - breakeven win rate > 0.55  (needs >55% wins just to break even)
  - cost_r > 0.25R             (a quarter of the risk paid to the exchange)

Both are pure functions of entry/SL/T1 — no klines needed. (The audit's
"replay on 1m klines" is the validation context; the rejection criteria are
these two numbers, computed here at birth.) Thresholds env-overridable.
Runs inline pre-POST in orderflow_calls.monitor — stronger than a CI flag:
it blocks, it doesn't just warn. The bridge's own geometry guard stays
warn-only by design (comparability); this gate is the enforcement point.

There is no CI runner in this repo (no git, no pipelines), so "the gate"
lives in the logging path itself.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

MAX_BREAKEVEN = float(os.getenv("PLAN_GATE_MAX_BREAKEVEN", "0.55"))
MAX_COST_R = float(os.getenv("PLAN_GATE_MAX_COST_R", "0.25"))
COST_BPS = float(os.getenv("CALLS_COST_BPS", "8.0"))


def gate_plan(plan: dict) -> Tuple[bool, List[str]]:
    """Return (ok, reasons). ok=False means DO NOT LOG this plan."""
    reasons: List[str] = []
    try:
        lo = float(plan["entry_low"])
        hi = float(plan["entry_high"])
        sl = float(plan["sl"])
        t1 = float(plan["t1"])
    except (KeyError, TypeError, ValueError):
        return False, ["malformed plan (entry/sl/t1)"]
    mid = (lo + hi) / 2.0
    risk = abs(mid - sl)
    t1d = abs(t1 - mid)
    if mid <= 0 or risk <= 0 or t1d <= 0:
        return False, ["non-positive geometry"]
    cost_r = (COST_BPS / 1e4) * mid / risk
    breakeven = (1.0 + cost_r) / (1.0 + t1d / risk)
    if cost_r > MAX_COST_R:
        reasons.append(f"cost_r {cost_r:.3f}R > {MAX_COST_R:.2f}R")
    if breakeven > MAX_BREAKEVEN:
        reasons.append(f"breakeven {breakeven:.3f} > {MAX_BREAKEVEN:.2f}")
    return (len(reasons) == 0), reasons


def describe(plan: Dict[str, Any]) -> str:
    ok, reasons = gate_plan(plan)
    return "gate OK" if ok else "gate REJECT: " + "; ".join(reasons)
