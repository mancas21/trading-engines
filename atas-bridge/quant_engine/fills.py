"""
fills.py — Realistic fills simulator: DOM-depth slippage + queue position + partial fills.

Used by orderflow_backtester and live trade planning. Pure functions, no I/O.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class FillResult:
    requested_qty: float
    filled_qty: float
    avg_price: float
    slippage_bps: float
    partial: bool
    queue_ahead: float
    consumed_levels: int
    reason: str  # "full" | "partial_depth" | "partial_queue" | "no_fill"


@dataclass
class DOMLevel:
    price: float
    size: float  # contracts / lots


def estimate_slippage_bps(
    side: str,
    qty: float,
    dom_levels: list[DOMLevel | list | dict],
    mid_price: float,
    queue_position: float = 0.0,
) -> tuple[float, float, int]:
    """
    Walk DOM levels to estimate slippage in bps.

    side: "buy" (lift ask) or "sell" (hit bid)
    qty: requested quantity
    dom_levels: ordered best->worst (asks for buy, bids for sell).
               Each element may be [price, size], dict{price,size}, or DOMLevel.
    mid_price: reference for bps conversion.
    queue_position: fraction 0-1 of queue ahead at best level (0 = front, 1 = back).
                   Only affects the first level's available size.

    Returns (avg_fill_price, slippage_bps, levels_consumed).
    If qty exceeds total depth, avg is computed over available depth and caller
    should treat remaining as unfilled.
    """
    if qty <= 0 or not dom_levels or mid_price <= 0:
        return mid_price, 0.0, 0

    # Normalize levels
    norm: list[tuple[float, float]] = []
    for lv in dom_levels:
        if isinstance(lv, DOMLevel):
            norm.append((float(lv.price), float(lv.size)))
        elif isinstance(lv, dict):
            norm.append((float(lv.get("price", lv.get("p", 0))), float(lv.get("size", lv.get("v", lv.get("q", 0))))))
        elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
            norm.append((float(lv[0]), float(lv[1])))
        else:
            continue
    # Filter zero sizes
    norm = [(p, s) for p, s in norm if s > 0]
    if not norm:
        return mid_price, 0.0, 0

    remaining = qty
    filled = 0.0
    cost = 0.0
    levels_consumed = 0

    for i, (price, size) in enumerate(norm):
        avail = size
        if i == 0 and 0 < queue_position < 1:
            # queue ahead consumes part of best level before we can fill
            avail = size * (1.0 - queue_position)
            avail = max(0.0, avail)
        take = min(remaining, avail)
        if take <= 0:
            continue
        cost += take * price
        filled += take
        remaining -= take
        levels_consumed += 1
        if remaining <= 1e-9:
            break

    if filled <= 0:
        return norm[0][0], 0.0, 0

    avg_price = cost / filled
    # bps = (avg - mid) / mid * 10000 signed by side
    if side.lower() == "buy":
        slip_bps = (avg_price - mid_price) / mid_price * 10000.0
    else:
        slip_bps = (mid_price - avg_price) / mid_price * 10000.0
    # slippage is adverse magnitude; for buy avg > mid, for sell avg < mid -> positive = adverse
    # If price improvement (rare), negative.
    return avg_price, round(slip_bps, 2), levels_consumed


def simulate_fill(
    side: str,
    qty: float,
    dom_levels: list,
    mid_price: float,
    queue_position: float | None = None,
    volatility_bps: float = 0.0,
    seed: int | None = None,
) -> FillResult:
    """
    Simulate a single fill attempt with queue + depth + partial fills.

    queue_position: 0-1 fraction ahead in queue at best level. If None, random uniform(0,0.7).
    volatility_bps: extra random slippage jitter (e.g. 2 bps -> +/-2 bps noise).
    """
    rng = random.Random(seed)
    if queue_position is None:
        queue_position = rng.uniform(0.0, 0.7)
    queue_position = max(0.0, min(1.0, queue_position))

    # Total available depth
    total_depth = 0.0
    for lv in dom_levels:
        if isinstance(lv, DOMLevel):
            total_depth += float(lv.size)
        elif isinstance(lv, dict):
            total_depth += float(lv.get("size", lv.get("v", lv.get("q", 0))) or 0)
        elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
            total_depth += float(lv[1])

    avg_price, slip_bps, levels = estimate_slippage_bps(side, qty, dom_levels, mid_price, queue_position)

    # Partial fill if qty exceeds total depth (after queue adjustment)
    # Effective depth after queue: total_depth - queue_eaten at best level
    if dom_levels:
        first_size = 0.0
        lv0 = dom_levels[0]
        if isinstance(lv0, DOMLevel):
            first_size = float(lv0.size)
        elif isinstance(lv0, dict):
            first_size = float(lv0.get("size", lv0.get("v", lv0.get("q", 0))) or 0)
        elif isinstance(lv0, (list, tuple)):
            first_size = float(lv0[1])
        effective_depth = total_depth - first_size * queue_position
    else:
        effective_depth = 0.0

    filled_qty = min(qty, max(0.0, effective_depth))
    if filled_qty <= 0:
        return FillResult(qty, 0.0, mid_price, 0.0, True, queue_position, 0, "no_fill")

    # Add volatility jitter
    if volatility_bps > 0:
        jitter = rng.uniform(-volatility_bps, volatility_bps)
        slip_bps = round(slip_bps + jitter, 2)
        # adjust avg_price accordingly
        if side.lower() == "buy":
            avg_price = mid_price * (1 + slip_bps / 10000.0)
        else:
            avg_price = mid_price * (1 - slip_bps / 10000.0)

    partial = filled_qty < qty - 1e-9
    reason = "partial_depth" if partial and effective_depth < qty else ("partial_queue" if partial else "full")
    if filled_qty == 0:
        reason = "no_fill"

    return FillResult(
        requested_qty=qty,
        filled_qty=round(filled_qty, 4),
        avg_price=round(avg_price, 6),
        slippage_bps=slip_bps,
        partial=partial,
        queue_ahead=round(queue_position, 3),
        consumed_levels=levels,
        reason=reason,
    )


def slippage_for_qty_curve(
    side: str,
    quantities: list[float],
    dom_levels: list,
    mid_price: float,
    queue_position: float = 0.0,
) -> list[dict]:
    """Return slippage bps for a sweep of quantities (for backtester cost curves)."""
    out = []
    for q in quantities:
        avg, bps, lvls = estimate_slippage_bps(side, q, dom_levels, mid_price, queue_position)
        out.append({"qty": q, "avg_price": round(avg, 6), "slippage_bps": bps, "levels": lvls})
    return out
