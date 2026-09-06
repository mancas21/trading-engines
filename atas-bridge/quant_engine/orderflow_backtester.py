"""
orderflow_backtester.py — Replay _tick_history.jsonl / orderflow_history/*.jsonl
through signal_engine.compute_signal and resolve forward returns.

Outputs per-symbol × direction expectancy table (gross & net with costs).

Usage:
    python -m quant_engine.orderflow_backtester --help
    python quant_engine/orderflow_backtester.py
    python quant_engine/orderflow_backtester.py --ticks C:/atas-bridge/_tick_history.jsonl --bars 3 --out report.json

Design: stateless replay, no network, no LLM.
"""
from __future__ import annotations

import argparse
import json as _json
import os
import sys
import glob
import math
import time
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_BRIDGE = os.path.abspath(os.path.join(_HERE, ".."))
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Local imports (after path fix)
try:
    from fills import simulate_fill, estimate_slippage_bps
except ImportError:
    from quant_engine.fills import simulate_fill, estimate_slippage_bps  # type: ignore

# signal_engine lives at bridge root
_BRIDGE_PARENT = _BRIDGE  # C:/atas-bridge
if _BRIDGE_PARENT not in sys.path:
    sys.path.insert(0, _BRIDGE_PARENT)

import importlib.util as _ilu

def _load_signal_engine():
    spec = _ilu.spec_from_file_location("signal_engine", os.path.join(_BRIDGE, "signal_engine.py"))
    mod = _ilu.module_from_spec(spec)  # type: ignore
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore
    return mod

_se = _load_signal_engine()
Snapshot = _se.Snapshot  # type: ignore
compute_signal = _se.compute_signal  # type: ignore
build_snapshot_from_live = _se.build_snapshot_from_live  # type: ignore


DEFAULT_TICK_FILES = [
    os.path.join(_BRIDGE, "_tick_history.jsonl"),
    os.path.join(_BRIDGE, "_tick_history.jsonl.1"),
]

DEFAULT_BARS = 3
DEFAULT_OUT = os.path.join(_BRIDGE, "quant_engine", "backtest_report.json")


def _iter_ticks(paths: list[str]):
    """Yield parsed tick entries in file order, tolerating corrupt lines."""
    for p in paths:
        # also expand globs
        expanded = glob.glob(p) if ("*" in p or "?" in p) else [p]
        for ep in expanded:
            if not os.path.exists(ep):
                continue
            try:
                with open(ep, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            yield _json.loads(line)
                        except Exception:
                            continue
            except Exception:
                continue


def _tick_to_snapshot(tick: dict) -> Snapshot | None:
    """Convert a _tick_history entry into a signal_engine Snapshot."""
    # _tick_history entries have: ticker, price, delta, volume{total,ask,bid}, dom_imbalance, etc.
    # Need to synthesize footprint/cvd/tape minimally.
    try:
        ticker = tick.get("ticker") or "UNKNOWN"
        price = float(tick.get("price") or 0)
        delta = float(tick.get("delta") or 0)
        vol = tick.get("volume") or {}
        total = float(vol.get("total") or 0)
        bid_v = float(vol.get("bid") or 0)
        ask_v = float(vol.get("ask") or 0)
        dom_imb = float(tick.get("dom_imbalance") or 0)
        cvd = tick.get("cvd")
        cvd_bars = int(tick.get("cvd_bars") or 0)
        # Synthesize minimal footprint: single level with delta
        levels = []
        if total > 0:
            levels = [{"price": price, "bid": bid_v, "ask": ask_v, "delta": delta, "volume": total}]
        tape_buy_pct = tick.get("tape_buy_pct")
        if tape_buy_pct is not None:
            try:
                tbp = float(tape_buy_pct)
                # approximate tape vols from total
                buy_vol = total * tbp / 100.0 if total else 0
                sell_vol = total - buy_vol
                buy_cnt = int(tbp)
                sell_cnt = max(0, 100 - int(tbp))
            except Exception:
                buy_vol = sell_vol = 0
                buy_cnt = sell_cnt = 0
        else:
            buy_vol = sell_vol = 0
            buy_cnt = sell_cnt = 0

        return Snapshot(
            ticker=ticker,
            price=price,
            delta=delta,
            volume_total=total,
            volume_bid=bid_v,
            volume_ask=ask_v,
            footprint_levels=levels,
            poc=price,
            value_area_high=price,
            value_area_low=price,
            dom_imbalance_pct=dom_imb,
            cumulative_bid_vol=bid_v,
            cumulative_ask_vol=ask_v,
            tape_buy_vol=buy_vol,
            tape_sell_vol=sell_vol,
            tape_buy_count=buy_cnt,
            tape_sell_count=sell_cnt,
            tape_buy_count_pct=float(tape_buy_pct) if tape_buy_pct is not None else 50.0,
            cvd=float(cvd) if cvd is not None else 0.0,
            cvd_bars=cvd_bars,
            cvd_bar_deltas=[],
            cvd_continuity="clean",
        )
    except Exception:
        return None


def run_backtest(
    tick_paths: list[str] | None = None,
    bars: int = DEFAULT_BARS,
    min_score: float = 60.0,
    max_ticks: int | None = None,
    cost_bps: float = 8.0,
    use_fills: bool = True,
) -> dict:
    """
    Replay ticks through signal_engine, resolve forward returns over next N ticks.

    Forward return: (price[t+N] - price[t]) / price[t] for each tick where a signal fires.
    For bearish signals the return is inverted (expecting price to fall).

    Returns a dict with per-symbol×direction expectancy table and raw trades.
    """
    paths = tick_paths or DEFAULT_TICK_FILES
    # Also include orderflow_history if present
    of_hist = os.path.join(_BRIDGE, "data", "orderflow_history", "*.jsonl")
    if glob.glob(of_hist):
        paths = list(paths) + [of_hist]
    # Legacy: orderflow_history at bridge root
    alt = os.path.join(_BRIDGE, "orderflow_history", "*.jsonl")
    if glob.glob(alt):
        paths = list(paths) + [alt]

    ticks = list(_iter_ticks(paths))
    if max_ticks and len(ticks) > max_ticks:
        ticks = ticks[-max_ticks:]

    if not ticks:
        return {"error": "no_ticks", "paths": paths, "n_ticks": 0}

    # Group by symbol to keep temporal order per symbol
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for t in ticks:
        sym = (t.get("ticker") or "UNKNOWN").upper()
        by_sym[sym].append(t)

    # Sort each symbol's ticks by ts
    for sym in list(by_sym.keys()):
        by_sym[sym].sort(key=lambda x: x.get("ts", 0))

    trades: list[dict] = []
    skipped_low_coverage = 0

    for sym, seq in by_sym.items():
        prices = [float(x.get("price") or 0) for x in seq]
        for i, tick in enumerate(seq):
            snap = _tick_to_snapshot(tick)
            if snap is None:
                continue
            sig = compute_signal(snap)
            if sig.data_coverage < 0.5:
                skipped_low_coverage += 1
                continue
            direction = sig.bias_label  # bullish/bearish/neutral
            if direction == "neutral":
                continue
            # Only take high-conviction: bias_score >= min_score or <= 100-min_score
            if direction == "bullish" and sig.bias_score < min_score:
                continue
            if direction == "bearish" and sig.bias_score > (100 - min_score):
                continue
            # Resolve forward return if enough future ticks exist
            if i + bars >= len(seq):
                continue
            entry = prices[i]
            future = prices[i + bars]
            if entry <= 0 or future <= 0:
                continue
            gross_ret = (future - entry) / entry
            if direction == "bearish":
                gross_ret = -gross_ret
            # Convert to R: assume 0.5% risk per trade for expectancy calc, or use 1% price risk
            # Use 1% risk baseline so R = ret / 0.01
            r_gross = gross_ret / 0.01
            # Net with cost: cost_bps of notional -> R
            # cost in price terms = cost_bps/1e4 * entry, / (0.01*entry) = cost_bps/100
            cost_r = (cost_bps / 10000.0) / 0.01  # = cost_bps/100
            r_net = r_gross - cost_r

            # Optional fills adjustment: slippage from DOM depth if available
            slip_bps = 0.0
            if use_fills:
                # Synthesize DOM: 5 levels each side, 0.05% spacing
                mid = entry
                spread = mid * 0.0005
                if direction == "bullish":
                    # buying: walk asks
                    dom = [[mid + spread * (j + 1) * 0.3, 1000.0 + j * 500] for j in range(5)]
                else:
                    dom = [[mid - spread * (j + 1) * 0.3, 1000.0 + j * 500] for j in range(5)]
                _, slip_bps, _ = estimate_slippage_bps("buy" if direction == "bullish" else "sell", 100, dom, mid, 0.2)
                # Convert slippage to R haircut
                slip_r = (slip_bps / 10000.0) / 0.01
                r_net -= slip_r

            trades.append({
                "symbol": sym,
                "direction": direction,
                "bias_score": sig.bias_score,
                "price_entry": entry,
                "price_future": future,
                "bars": bars,
                "r_gross": round(r_gross, 4),
                "r_net": round(r_net, 4),
                "slippage_bps": round(slip_bps, 2),
                "ts": tick.get("ts"),
                "coverage": sig.data_coverage,
            })

    # Aggregate per symbol × direction
    from collections import defaultdict as _dd
    table: dict[str, dict] = {}
    grouped: dict[tuple, list[dict]] = _dd(list)
    for tr in trades:
        grouped[(tr["symbol"], tr["direction"])].append(tr)

    for (sym, direction), lst in grouped.items():
        n = len(lst)
        r_nets = [x["r_net"] for x in lst]
        r_grosses = [x["r_gross"] for x in lst]
        wins = sum(1 for r in r_nets if r > 0)
        losses = sum(1 for r in r_nets if r < 0)
        zeros = n - wins - losses
        winrate = wins / n if n else 0
        exp_net = sum(r_nets) / n if n else 0
        exp_gross = sum(r_grosses) / n if n else 0
        # Wilson CI for winrate
        try:
            from quant_engine.engine.core.types import wilson_interval
            lo, hi = wilson_interval(wins, n)
        except Exception:
            # inline fallback
            import math as _math
            if n == 0:
                lo, hi = 0.0, 1.0
            else:
                z = 1.96
                phat = wins / n
                denom = 1 + z**2 / n
                centre = phat + z**2 / (2*n)
                adj = z * _math.sqrt((phat*(1-phat) + z**2/(4*n))/n)
                lo = max(0.0, (centre - adj)/denom)
                hi = min(1.0, (centre + adj)/denom)
        # Simple bootstrap CI for expectancy (percentile, 500 resamples for speed)
        import random as _rand
        if n >= 10:
            _rand.seed(42)
            means = []
            for _ in range(500):
                samp = [_rand.choice(r_nets) for _ in range(n)]
                means.append(sum(samp)/n)
            means.sort()
            lo_e = means[int(0.025*len(means))]
            hi_e = means[int(0.975*len(means))]
        else:
            lo_e = hi_e = None

        key = f"{sym}×{direction}"
        table[key] = {
            "symbol": sym,
            "direction": direction,
            "n": n,
            "wins": wins,
            "losses": losses,
            "zeros": zeros,
            "winrate": round(winrate, 4),
            "winrate_ci95": [round(lo, 4), round(hi, 4)],
            "expectancy_r_net": round(exp_net, 4),
            "expectancy_r_gross": round(exp_gross, 4),
            "expectancy_ci95": [round(lo_e, 4), round(hi_e, 4)] if lo_e is not None else None,
            "avg_r_net": round(exp_net, 4),
        }

    # Overall
    all_r_net = [t["r_net"] for t in trades]
    overall = {}
    if all_r_net:
        n = len(all_r_net)
        wins = sum(1 for r in all_r_net if r > 0)
        overall = {
            "n": n,
            "wins": wins,
            "winrate": round(wins/n, 4),
            "expectancy_r_net": round(sum(all_r_net)/n, 4),
        }

    return {
        "meta": {
            "bars": bars,
            "cost_bps": cost_bps,
            "min_score": min_score,
            "n_ticks": len(ticks),
            "n_signals": len(trades),
            "skipped_low_coverage": skipped_low_coverage,
            "paths": paths,
            "generated_at": time.time(),
        },
        "overall": overall,
        "per_symbol_direction": table,
        "trades": trades[:2000],  # cap
    }


def main():
    ap = argparse.ArgumentParser(description="Orderflow backtester replay")
    ap.add_argument("--ticks", nargs="*", default=None, help="tick file paths/globs")
    ap.add_argument("--bars", type=int, default=DEFAULT_BARS, help="forward bars to resolve")
    ap.add_argument("--out", type=str, default=DEFAULT_OUT, help="output JSON path")
    ap.add_argument("--max-ticks", type=int, default=None, help="cap ticks (most recent)")
    ap.add_argument("--min-score", type=float, default=60.0, help="bias score threshold")
    ap.add_argument("--cost-bps", type=float, default=8.0, help="round-trip cost in bps")
    args = ap.parse_args()

    res = run_backtest(
        tick_paths=args.ticks,
        bars=args.bars,
        min_score=args.min_score,
        max_ticks=args.max_ticks,
        cost_bps=args.cost_bps,
    )
    out = args.out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        _json.dump(res, f, indent=2, default=str)
    print(f"Backtest: {res['meta']['n_ticks']} ticks -> {res['meta']['n_signals']} signals")
    print(f"Overall: {res.get('overall')}")
    for k, v in res.get("per_symbol_direction", {}).items():
        print(f"  {k}: n={v['n']} winrate={v['winrate']} exp_net={v['expectancy_r_net']} ci={v['winrate_ci95']}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
