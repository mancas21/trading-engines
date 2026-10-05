"""
expectancy_report.py — Step 1: is there any edge, net of costs?

The only question that matters before any infrastructure: does this system make
money after commission, slippage and funding? Three read-only views:

  outcomes  Realized expectancy in R, NET of costs, from the recorded outcomes
            joined with the stored ATR feature. No network, no assumptions about
            fills beyond "exit at the horizon".
  gates     Ablation of every gate that can be reconstructed from stored data,
            and an explicit list of the ones that CANNOT be — instead of
            inventing numbers for them.
  simulate  Full-path fill simulation (stop/target touched on real high/low) with
            the repo's own CostModel and compute_metrics, from Binance klines.
            This is what corrects the optimistic horizon-exit assumption above.

R units. Everything is expressed in multiples of the money risked (R):
    stop_frac  = stop_mult * atr_pct / 100      stop distance as a fraction of price
    realized_R = forward_return / stop_frac     forward_return is already directional
    cost_R     = roundtrip_cost / stop_frac     roundtrip cost in risk units
    net_R      = realized_R - cost_R
A signal only pays if its net_R is reliably above zero. Cost is charged on the
NOTIONAL (risk / stop distance), which is why wide stops are cheap in R terms and
tight stops are expensive — a fact the raw hit-rate view hides completely.

Usage:
    python expectancy_report.py outcomes
    python expectancy_report.py gates
    python expectancy_report.py simulate --symbol "SOL/USDT:USDT"
    python expectancy_report.py all --json data/expectancy_report.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from quant_alert_engine import CostModel, TF_SECONDS, compute_metrics, get_signal_direction

DB_DEFAULT = "data/alerts.db"
ROUNDTRIP = CostModel().roundtrip_cost_pct()          # 0.0010 = 0.10% of notional
COMMISSION = CostModel().commission_pct
SLIPPAGE = CostModel().slippage_pct
FUNDING_PER_8H = CostModel().funding_rate_pct         # 0.0001 per 8h interval

BOOTSTRAP_N = 2000
SEED = 7


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def load_rows(db: str) -> List[dict]:
    """Directional signals with their realized outcome AND stored features."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        """SELECT o.signal_id, o.symbol, o.timeframe, o.signal_name, o.signal_type,
                  COALESCE(o.direction, 0) AS direction, o.fired_ts,
                  o.forward_return, o.forward_bars, f.features_json
           FROM signal_outcomes o
           LEFT JOIN signal_features f ON f.signal_id = o.signal_id
           WHERE o.resolved = 1 AND o.forward_return IS NOT NULL
           ORDER BY o.fired_ts""").fetchall()
    conn.close()
    out = []
    for (sid, sym, tf, name, stype, d, ts, fret, fwd, fjson) in rows:
        feats = {}
        if fjson:
            try:
                feats = json.loads(fjson) or {}
            except Exception:
                feats = {}
        out.append({"signal_id": sid, "symbol": sym, "timeframe": tf,
                    "signal_name": name, "signal_type": stype,
                    "direction": int(d or 0), "fired_ts": float(ts or 0.0),
                    "forward_return": float(fret), "forward_bars": int(fwd or 12),
                    "features": feats})
    return out


def stop_frac(row: dict, stop_mult: float) -> Optional[float]:
    """Stop distance as a fraction of price, from the STORED atr_pct feature."""
    atr_pct = row["features"].get("atr_pct")
    if not atr_pct or atr_pct <= 0:
        return None
    return stop_mult * float(atr_pct) / 100.0


def funding_cost_frac(row: dict) -> float:
    """Funding paid over the holding period, as a fraction of price.

    CostModel.funding_cost() counts bars assuming 5-minute bars regardless of the
    timeframe, so a 1h position held 12 bars is charged zero funding when the true
    answer is one 8h interval. Time is what funds accrue on, so compute it from
    the timeframe directly (see the note in main(): the engine's own bug is fixed
    in quant_alert_engine.CostModel).
    """
    tf = row["timeframe"]
    bar_s = TF_SECONDS.get(tf, 300)
    held_s = row["forward_bars"] * bar_s
    intervals = int(held_s // (8 * 3600))
    return intervals * FUNDING_PER_8H


def enrich(rows: Sequence[dict], stop_mult: float) -> List[dict]:
    """Attach stop_frac / realized_R / cost_R / net_R. Drops unusable rows."""
    out = []
    for r in rows:
        sf = stop_frac(r, stop_mult)
        if not sf or sf <= 0:
            continue
        realized_R = r["forward_return"] / sf
        cost_R = (ROUNDTRIP + funding_cost_frac(r)) / sf
        rr = dict(r)
        rr.update({"stop_frac": sf, "realized_R": realized_R,
                   "cost_R": cost_R, "net_R": realized_R - cost_R})
        out.append(rr)
    return out


# ---------------------------------------------------------------------------
# stats helpers
# ---------------------------------------------------------------------------

def boot_ci(values: Sequence[float], n: int = BOOTSTRAP_N, seed: int = SEED) -> Tuple[float, float]:
    if len(values) < 3:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    v = list(values)
    means = []
    L = len(v)
    for _ in range(n):
        means.append(sum(v[rng.randrange(L)] for _ in range(L)) / L)
    means.sort()
    return (means[int(0.05 * len(means))], means[int(0.95 * len(means))])


def summarize(rows: Sequence[dict]) -> Optional[dict]:
    if not rows:
        return None
    net = [r["net_R"] for r in rows]
    gross = [r["realized_R"] for r in rows]
    cost = [r["cost_R"] for r in rows]
    lo, hi = boot_ci(net)
    # GROSS hit rate = did the price move in the signal's direction. This is a
    # property of the signal. The NET hit rate is NOT reported as "hit rate"
    # because with costs above 1R nothing can clear them, which made the number
    # meaningless (and produced a nonsensical 278% "breakeven").
    hit_gross = sum(1 for x in gross if x > 0) / len(rows)
    moves = [abs(r["forward_return"]) * 100.0 for r in rows]
    signed = [r["forward_return"] * 100.0 for r in rows]
    cost_pct = float(np.mean([r["cost_R"] * r["stop_frac"] for r in rows])) * 100.0
    # cost_R is a ratio (roundtrip / stop_frac). Its MEAN is an average-of-ratios:
    # a handful of rows with a tiny ATR get enormous notional (sizing is
    # risk/stop_distance) and therefore an enormous fee in R, which drags the mean
    # far above the typical trade. Report the median as "typical" and the mean as
    # "what it costs on average across this population", and never conflate them.
    mean_cost = float(np.mean(cost))
    return {
        "n": len(rows),
        "hit_gross": hit_gross,
        "gross_edge_R_1to1": 2 * hit_gross - 1.0,   # EV before costs at R:R 1:1
        "mean_gross_R": float(np.mean(gross)),
        "mean_cost_R": mean_cost,
        "median_cost_R": float(np.median(cost)),
        "p90_cost_R": float(np.percentile(cost, 90)),
        "pct_trades_cost_over_1R": 100.0 * sum(1 for x in cost if x > 1.0) / len(cost),
        "mean_net_R": float(np.mean(net)),
        "median_net_R": float(np.median(net)),
        "net_R_ci90": [lo, hi],
        "sum_net_R": float(np.sum(net)),
        "mean_gross_move_pct": float(np.mean(signed)),
        "mean_abs_move_pct": float(np.mean(moves)),
        "median_abs_move_pct": float(np.median(moves)),
        "cost_pct_of_price": cost_pct,
        "cost_over_move": (cost_pct / float(np.mean(moves))) if np.mean(moves) > 0 else float("nan"),
        "mean_stop_pct": float(np.mean([r["stop_frac"] for r in rows])) * 100.0,
        "median_stop_pct": float(np.median([r["stop_frac"] for r in rows])) * 100.0,
        "mean_atr_pct": float(np.mean([r["features"].get("atr_pct", 0.0) for r in rows])),
    }


def fmt(label: str, s: Optional[dict], width: int = 30) -> str:
    if not s:
        return f"{label:<{width}} {'-':>6}"
    lo, hi = s["net_R_ci90"]
    flag = "  <-- pays" if lo > 0 else ("  <-- loses" if hi < 0 else "")
    return (f"{label:<{width}} n={s['n']:>5} hitG={s['hit_gross']*100:>5.1f}% "
            f"grossR={s['mean_gross_R']:>+7.3f} costR={s['mean_cost_R']:>6.3f} "
            f"NET_R={s['mean_net_R']:>+8.3f} [{lo:>+7.3f},{hi:>+7.3f}]{flag}")


# ---------------------------------------------------------------------------
# view 1 — realized expectancy from recorded outcomes
# ---------------------------------------------------------------------------

def view_outcomes(rows: List[dict], stop_mult: float, min_n: int, top: int = 25) -> dict:
    directional = [r for r in rows if r["direction"] != 0]
    neutral = [r for r in rows if r["direction"] == 0]
    de = enrich(directional, stop_mult)

    print("=" * 108)
    print(f"VIEW 1 — REALIZED EXPECTANCY (net of {ROUNDTRIP*100:.2f}% roundtrip + funding), "
          f"stop = {stop_mult}xATR, R-normalized")
    print("=" * 108)
    print(f"resolved outcomes: {len(rows)}   directional: {len(directional)}   "
          f"dir-agnostic: {len(neutral)}")
    print(f"usable (directional + stored atr_pct): {len(de)}")
    if not de:
        print("\nNot enough data: no directional rows with a stored atr_pct feature.")
        return {"usable": 0}

    overall = summarize(de)
    print(f"\nOVERALL  {fmt('all directional signals', overall)}")
    print(f"   gross edge before costs at R:R 1:1 (2p-1): {overall['gross_edge_R_1to1']:+.3f}R "
          f"at a {overall['hit_gross']*100:.1f}% gross hit rate")
    print(f"   gross move per signal (signed): {overall['mean_gross_move_pct']:+.4f}%   "
          f"typical |move|: {overall['mean_abs_move_pct']:.4f}% (median {overall['median_abs_move_pct']:.4f}%)")
    print(f"   roundtrip cost: {ROUNDTRIP*100:.3f}% of price -> fees eat "
          f"{overall['cost_over_move']*100:.1f}% of a typical move on this mix")
    print(f"   cost in R: median {overall['median_cost_R']:.3f}R (typical trade) | "
          f"mean {overall['mean_cost_R']:.3f}R (average-of-ratios, dragged up by tiny-ATR rows) | "
          f"p90 {overall['p90_cost_R']:.2f}R")
    print(f"   trades whose cost ALONE exceeds 1R (a target hit still loses): "
          f"{overall['pct_trades_cost_over_1R']:.1f}%")
    print(f"   stop width at {stop_mult}xATR: median {overall['median_stop_pct']:.4f}% of price")
    print(f"   sum of net R across all signals: {overall['sum_net_R']:+.1f}R over {overall['n']} signals")

    # The stop-width vs cost diagnostic: this is what decides whether a timeframe
    # is tradeable at all, before any question about signal quality.
    print("\nCOST-STRUCTURE DIAGNOSTIC BY TIMEFRAME (the decisive table)")
    print(f"  {'tf':<5} {'n':>5} {'atr%':>8} {'med stop%':>10} {'med cost':>9} "
          f"{'%cost>1R':>9} {'gross 2p-1':>11} {'med NET_R':>10} {'verdict':>10}")
    for tf_name, grp in sorted(_group(de, "timeframe").items(),
                               key=lambda kv: -len(kv[1])):
        s = summarize(grp)
        verdict = ("unusable" if s["median_cost_R"] > 0.5 else
                   "tight" if s["median_cost_R"] > 0.2 else "viable?")
        print(f"  {tf_name:<5} {s['n']:>5} {s['mean_atr_pct']:>8.4f} "
              f"{s['median_stop_pct']:>9.4f}% {s['median_cost_R']:>8.3f}R "
              f"{s['pct_trades_cost_over_1R']:>8.1f}% {s['gross_edge_R_1to1']:>+11.3f} "
              f"{s['median_net_R']:>+10.3f} {verdict:>10}")

    # by group
    for key, title in (("signal_name", "BY SIGNAL"), ("symbol", "BY SYMBOL"),
                       ("timeframe", "BY TIMEFRAME"), ("signal_type", "BY TYPE")):
        groups: Dict[str, List[dict]] = defaultdict(list)
        for r in de:
            groups[r[key]].append(r)
        ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
        print(f"\n{title}")
        shown = 0
        for name, grp in ranked:
            s = summarize(grp)
            mark = ""
            if s["n"] >= min_n:
                shown += 1
                mark = ""
            else:
                mark = "   (n < min, not readable)"
            print("  " + fmt(str(name), s) + mark)
            if shown >= top:
                break

    # best and worst with enough data
    groups_all: Dict[str, List[dict]] = {}
    for k, v in _group(de, "signal_name").items():
        groups_all["sig:" + k] = v
    for k, v in _group(de, "symbol").items():
        groups_all["sym:" + k] = v
    enough = [(k, summarize(v)) for k, v in groups_all.items() if len(v) >= min_n]
    pays = [(k, s) for k, s in enough if s["net_R_ci90"][0] > 0]
    loses = [(k, s) for k, s in enough if s["net_R_ci90"][1] < 0]
    print(f"\n--- with n >= {min_n}: {len(enough)} groups readable | "
          f"{len(pays)} net-positive | {len(loses)} net-negative ---")
    for k, s in sorted(pays, key=lambda kv: -kv[1]["mean_net_R"]):
        print(f"  PAYS  {fmt(k, s)}")
    for k, s in sorted(loses, key=lambda kv: kv[1]["mean_net_R"])[:10]:
        print(f"  LOSES {fmt(k, s)}")

    # Direction-agnostic signals: no tradeable direction, only possible vol use
    if neutral:
        nd = enrich([r for r in neutral if r["features"].get("atr_pct")], stop_mult)
        if nd:
            absmove = float(np.mean([abs(r["forward_return"]) for r in nd]))
            allabs = float(np.mean([abs(r["forward_return"]) for r in de]))
            print(f"\nDIR-AGNOSTIC SIGNALS (Volume Climax/Spike/Dryup, BB Squeeze, ATR Expansion)")
            print(f"  n={len(nd)}  mean |move| over horizon = {absmove*100:.3f}%  "
                  f"vs {allabs*100:.3f}% for directional ones")
            print(f"  -> no side to trade; only usable as a volatility/exposure filter, "
                  f"never as an entry.")

    # Sample-drift check. The stored forward_return for dir-agnostic rows is the
    # RAW move (no direction adjustment), so its mean is the sample's drift. If
    # that drift is large, any "bearish signals work / bullish signals don't"
    # pattern is market beta, NOT signal skill — and with ~1 week of history the
    # two cannot be separated.
    raw = [r["forward_return"] * 100.0 for r in neutral if r["forward_return"] is not None]
    if raw:
        drift = float(np.mean(raw))
        bear_gross = float(np.mean([r["realized_R"] for r in de if r["direction"] < 0]))
        bull_gross = float(np.mean([r["realized_R"] for r in de if r["direction"] > 0]))
        print(f"\nSAMPLE DRIFT CHECK (raw horizon move across {len(raw)} dir-agnostic signals)")
        print(f"  mean raw move over the horizon: {drift:+.4f}%  "
              f"(median {float(np.median(raw)):+.4f}%)")
        print(f"  gross R: bearish signals {bear_gross:+.3f}  vs  bullish {bull_gross:+.3f}")
        if abs(drift) > 0.02:
            direction = "DOWN" if drift < 0 else "UP"
            print(f"  -> the sample period drifted {direction}. The bearish>bullish gap is "
                  f"consistent with market beta, not signal skill.")
            print(f"  -> with ~1 week of data in a single regime, skill and beta are "
                  f"NOT separable. Do not read a short-only edge out of this.")
        else:
            print("  -> drift is small; the bearish/bullish gap is not explained by drift alone.")
    return {"overall": overall, "usable": len(de), "drift_pct": (float(np.mean(raw)) if raw else None)}


def _group(rows: Sequence[dict], key: str) -> Dict[str, List[dict]]:
    g: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        g[r[key]].append(r)
    return dict(g)


# ---------------------------------------------------------------------------
# view 2 — gate ablation
# ---------------------------------------------------------------------------

def view_gates(rows: List[dict], stop_mult: float, min_n: int) -> dict:
    de = enrich([r for r in rows if r["direction"] != 0], stop_mult)
    if not de:
        print("\nno data for gate ablation")
        return {}
    base = summarize(de)
    print("\n" + "=" * 108)
    print("VIEW 2 — GATE ABLATION (does each gate pay for the signals it throws away?)")
    print("=" * 108)
    print(f"baseline: {fmt('all directional signals', base)}")
    print(f"{'variant':<40} {'kept':>6} {'%kept':>6} {'NET_R kept':>11} "
          f"{'NET_R dropped':>14} {'delta':>8}")
    print("-" * 108)

    results = {}
    variants: List[Tuple[str, callable]] = []

    # confluence (the engine's confluence_required knob)
    for k in (1, 2, 3, 4):
        variants.append((f"confluence >= {k}",
                         lambda r, k=k: (r["features"].get(
                             "bull_conf" if r["direction"] > 0 else "bear_conf", 0) or 0) >= k))
    # trend regime agreement (the EMA50 band gate)
    variants.append(("regime agrees with direction",
                     lambda r: r["features"].get("regime", 0) == r["direction"]))
    variants.append(("regime neutral only",
                     lambda r: r["features"].get("regime", 0) == 0))
    variants.append(("regime against direction (control)",
                     lambda r: r["features"].get("regime", 0) == -r["direction"]))
    # volatility level
    variants.append(("atr_rank < 0.33 (quiet)", lambda r: r["features"].get("atr_rank_pct", 0.5) < 0.33))
    variants.append(("atr_rank 0.33-0.67", lambda r: 0.33 <= r["features"].get("atr_rank_pct", 0.5) < 0.67))
    variants.append(("atr_rank >= 0.67 (volatile)", lambda r: r["features"].get("atr_rank_pct", 0.5) >= 0.67))
    # trend efficiency
    variants.append(("er20 >= 0.30 (trending)", lambda r: r["features"].get("er20", 0) >= 0.30))
    variants.append(("er20 < 0.30 (ranging)", lambda r: r["features"].get("er20", 0) < 0.30))
    # volume confirmation
    variants.append(("vol_ratio >= 1.5", lambda r: r["features"].get("vol_ratio", 1) >= 1.5))
    variants.append(("vol_ratio < 1.0 (dry)", lambda r: r["features"].get("vol_ratio", 1) < 1.0))
    # value-area location
    variants.append(("va_pos agrees with direction",
                     lambda r: r["features"].get("va_pos", 0) == r["direction"]))
    # RSI extremes
    variants.append(("rsi < 35 (oversold)", lambda r: r["features"].get("rsi", 50) < 35))
    variants.append(("rsi > 65 (overbought)", lambda r: r["features"].get("rsi", 50) > 65))

    for label, pred in variants:
        try:
            kept = [r for r in de if pred(r)]
        except Exception as e:
            print(f"{label:<40} predicate failed: {e}")
            continue
        kept_ids = {id(r) for r in kept}
        dropped = [r for r in de if id(r) not in kept_ids]
        if len(kept) < min_n:
            print(f"{label:<40} {len(kept):>6}  (n < {min_n}, skipped)")
            continue
        sk = summarize(kept)
        sd = summarize(dropped) if dropped else None
        delta = sk["mean_net_R"] - (sd["mean_net_R"] if sd else 0.0)
        covers = (sk["net_R_ci90"][0] > 0)
        print(f"{label:<40} {sk['n']:>6} {100*sk['n']/len(de):>5.0f}% "
              f"{sk['mean_net_R']:>+11.3f} "
              f"{(sd['mean_net_R'] if sd else float('nan')):>+14.3f} {delta:>+8.3f}"
              f"{'   kept-group pays' if covers else ''}")
        results[label] = {"kept": sk["n"], "kept_pct": 100 * sk["n"] / len(de),
                          "net_R_kept": sk["mean_net_R"], "ci90_kept": sk["net_R_ci90"],
                          "net_R_dropped": (sd["mean_net_R"] if sd else None),
                          "delta": delta}

    # hour-of-day (what the engine's time-gating mutes)
    print("\nBY HOUR (UTC) — the engine's time-gating mutes negative-expectancy hours")
    hours: Dict[int, List[dict]] = defaultdict(list)
    for r in de:
        h = datetime.fromtimestamp(r["fired_ts"], tz=timezone.utc).hour
        hours[h].append(r)
    for h in sorted(hours):
        s = summarize(hours[h])
        if s["n"] >= 8:
            print(f"  {h:02d}h  {fmt('', s, width=0)}")

    # the engine's actual outcome gate, with train-only discipline
    print("\nENGINE'S OUTCOME GATE (mute (symbol,signal) with negative TRAIN expectancy)")
    ordered = sorted(de, key=lambda r: r["fired_ts"])
    n = len(ordered)
    cut = int(n * 0.5)
    train, test = ordered[:cut], ordered[cut:]

    # Reachability first: if the configured min_n cannot be reached by
    # construction, the gate is dead config no matter how it is tuned.
    per_pair: Dict[Tuple[str, str], int] = defaultdict(int)
    for r in ordered:
        per_pair[(r["symbol"], r["signal_name"])] += 1
    counts = sorted(per_pair.values(), reverse=True)
    if counts:
        print(f"  sample size per (symbol, signal) pair over this window: "
              f"max={counts[0]} median={counts[len(counts)//2]} "
              f"p90={counts[max(0,int(len(counts)*0.1)-1)]} | {len(counts)} pairs")
        for thr in (5, 10, 15, 20, 30):
            reach = sum(1 for c in counts if c >= thr)
            print(f"    pairs reaching n>={thr:>2}: {reach:>3} of {len(counts)} "
                  f"({100*reach/len(counts):.0f}%)")
    print(f"  {'min_n':>6} {'keeps%':>7} {'NET_R kept':>11} {'NET_R all':>10} {'delta':>8}")
    for min_n_sp in (3, 5, 8, 10, 15, 20, 30):
        exp: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        for r in train:
            exp[(r["symbol"], r["signal_name"])].append(r["realized_R"])
        muted = {k for k, v in exp.items() if len(v) >= min_n_sp and float(np.mean(v)) < 0}
        kept = [r for r in test if (r["symbol"], r["signal_name"]) not in muted]
        sk = summarize(kept) if kept else None
        sa = summarize(test)
        if not sk or not sa:
            continue
        print(f"  {min_n_sp:>6} {100*len(kept)/len(test):>6.0f}% "
              f"{sk['mean_net_R']:>+11.3f} {sa['mean_net_R']:>+10.3f} "
              f"{sk['mean_net_R']-sa['mean_net_R']:>+8.3f}"
              f"   median kept {sk['median_net_R']:>+7.3f}")
        results[f"outcome_gate_min_n_{min_n_sp}"] = {
            "kept_pct": 100 * len(kept) / len(test),
            "net_R_kept": sk["mean_net_R"], "net_R_all": sa["mean_net_R"],
            "delta": sk["mean_net_R"] - sa["mean_net_R"]}

    print("\nGATES THAT CANNOT BE EVALUATED FROM STORED DATA (no verdict stored per signal):")
    for g, why in (
            ("blackout / cooldown",
             "wall-clock suppression — suppressed signals are never written to the DB"),
            ("Level-2 bridge gate",
             "the DOM/CVD/quant verdict is not persisted with the signal"),
            ("external macro gate",
             "macro fields live in market_data, not in the 41 numeric features"),
            ("multi-TF consolidation",
             "suppression by higher-TF candle window is not recorded"),
            ("divergence episode gating",
             "fires once per episode; suppressed repeats are not recorded")):
        print(f"  - {g}: {why}")
    print("  => these need COUNTERFACTUAL logging (record would-be signals too), "
          "which is a change to the live engine, not an analysis.")
    return {"baseline": base, "variants": results}


# ---------------------------------------------------------------------------
# view 3 — full-path simulation
# ---------------------------------------------------------------------------

def view_simulate(rows: List[dict], stop_mult: float, tp_mult: float, max_hold: int,
                  risk_pct: float, symbols: Optional[List[str]] = None,
                  window: int = 200, trades_path: Optional[str] = None) -> dict:
    """Fills on real high/low: corrects the horizon-exit assumption of view 1."""
    from ml_backfill import fetch_klines, TF_MS
    from features import FeatureExtractor

    de = [r for r in rows if r["direction"] != 0]
    if symbols:
        de = [r for r in de if r["symbol"] in symbols]
    by_st = defaultdict(list)
    for r in de:
        by_st[(r["symbol"], r["timeframe"])].append(r)

    costs = CostModel()
    print("\n" + "=" * 108)
    print(f"VIEW 3 — FULL-PATH SIMULATION (stop {stop_mult}xATR / target {tp_mult}xATR / "
          f"max {max_hold} bars, risk {risk_pct*100:.1f}%/trade, CostModel applied)")
    print("=" * 108)
    all_trades: List[dict] = []
    for (sym, tf), sigs in sorted(by_st.items(), key=lambda kv: -len(kv[1])):
        if len(sigs) < 5:
            continue
        step = TF_MS.get(tf, 300_000)
        stamps = []
        for s in sigs:
            try:
                stamps.append(int(datetime.fromisoformat(
                    s["signal_id"].split("_")[-1].replace("Z", "+00:00")).timestamp() * 1000))
            except Exception:
                pass
        if not stamps:
            continue
        kl = fetch_klines(sym, tf, min(stamps) - (window + 5) * step, max(stamps) + (max_hold + 5) * step)
        if not kl:
            print(f"  {sym} {tf}: no klines")
            continue
        o = np.array([float(k[1]) for k in kl]); h = np.array([float(k[2]) for k in kl])
        l = np.array([float(k[3]) for k in kl]); c = np.array([float(k[4]) for k in kl])
        ts_idx = {int(k[0]): i for i, k in enumerate(kl)}
        bpy = int((365 * 24 * 3600) / TF_SECONDS.get(tf, 300))
        fx = FeatureExtractor(window=window)
        n_used = 0
        for s in sigs:
            try:
                ms = int(datetime.fromisoformat(
                    s["signal_id"].split("_")[-1].replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                continue
            i = ts_idx.get(ms)
            if i is None or i + 1 >= len(c) or i - window < 0:
                continue
            atr = s["features"].get("atr_pct", 0.0) / 100.0 * c[i]
            if not atr or atr <= 0 or math.isnan(atr):
                vec = fx.vector(o, h, l, c, np.ones_like(c), i=i)
                atr = (vec.get("atr_pct", 0.0) / 100.0 * c[i]) if vec else 0.0
            if atr <= 0:
                continue
            side = 1 if s["direction"] > 0 else -1
            entry = float(o[i + 1]) * (1 + costs.slippage_pct * side)
            stop = entry - side * stop_mult * atr
            target = entry + side * tp_mult * atr
            exit_px, reason, bars = None, "end", 0
            for j in range(i + 1, min(i + 1 + max_hold, len(c))):
                bars = j - i
                if side > 0:
                    if l[j] <= stop:
                        exit_px, reason = stop, "stop"; break
                    if h[j] >= target:
                        exit_px, reason = target, "target"; break
                else:
                    if h[j] >= stop:
                        exit_px, reason = stop, "stop"; break
                    if l[j] <= target:
                        exit_px, reason = target, "target"; break
            if exit_px is None:
                j = min(i + max_hold, len(c) - 1)
                exit_px, bars = float(c[j]), j - i
            realized = exit_px * (1 - costs.slippage_pct * side)
            stop_dist = stop_mult * atr
            qty = (risk_pct * 1.0) / stop_dist
            gross = side * (realized - entry) * qty
            comm = (entry + realized) * qty * costs.commission_pct
            fund = costs.funding_cost(realized, bars, qty,
                                      bar_seconds=TF_SECONDS.get(tf, 300))
            net = gross - comm - fund
            all_trades.append({"name": s["signal_name"], "symbol": sym, "timeframe": tf,
                               "ret": net, "reason": reason, "bars": bars,
                               "regime": int(s["features"].get("regime", 0)),
                               "exit_idx": j, "entry_idx": i})
            n_used += 1
        if n_used:
            print(f"  {sym} {tf}: {n_used} signals simulated")

    if not all_trades:
        print("\nno simulated trades")
        return {}
    agg = compute_metrics(all_trades, int((365 * 24 * 3600) / 300))
    # The single most damning statistic: a target hit pays +1R GROSS exactly
    # (tp_mult == stop_mult), so every target exit whose net is negative is a
    # trade the signal got RIGHT and the fee still took the money.
    tgt = [t for t in all_trades if t["reason"] == "target"]
    tgt_win = [t for t in tgt if t["ret"] > 0]
    stp = [t for t in all_trades if t["reason"] == "stop"]
    print(f"\nAGGREGATE (fixed {risk_pct*100:.1f}% risk per trade, equity-compounding):")
    print(f"  trades={agg['n_trades']}  win={agg['win_rate']*100:.1f}%  "
          f"PF={agg['profit_factor']:.3f}  expectancy={agg['expectancy']*100:+.4f}% of equity")
    print(f"  total_return={agg['total_return']*100:+.2f}%  maxDD={agg['max_dd_pct']:.2f}%  "
          f"sharpe={agg['sharpe']:.2f}  effective_n={agg['effective_n']:.0f}")
    print(f"  exits: target={len(tgt)} stop={len(stp)} time/end="
          f"{len(all_trades)-len(tgt)-len(stp)}")
    print(f"  >>> of the {len(tgt)} trades that hit their TARGET, "
          f"{len(tgt)-len(tgt_win)} were STILL net-negative "
          f"({100*(len(tgt)-len(tgt_win))/max(len(tgt),1):.0f}% of correct calls paid to fees)")
    if trades_path:
        try:
            with open(trades_path, "w", encoding="utf-8") as f:
                for t in all_trades:
                    f.write(json.dumps(t, default=str) + "\n")
            print(f"  wrote {len(all_trades)} trade rows to {trades_path}")
        except OSError as e:
            print(f"  could not write {trades_path}: {e}")
    print("\nBY SIGNAL (top 12 by n):")
    byname = defaultdict(list)
    for t in all_trades:
        byname[t["name"]].append(t["ret"])
    for name, rets in sorted(byname.items(), key=lambda kv: -len(kv[1]))[:12]:
        wr = sum(1 for x in rets if x > 0) / len(rets)
        print(f"  {name:<26} n={len(rets):>4} win={wr*100:>5.1f}% "
              f"exp={np.mean(rets)*100:>+7.4f}% equity/trade sum={np.sum(rets)*100:>+8.2f}%")
    return {"aggregate": {k: v for k, v in agg.items() if k != "trades"}}


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Net-of-cost expectancy report (Step 1)")
    ap.add_argument("view", choices=["outcomes", "gates", "simulate", "all"])
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--stop", type=float, default=2.0, help="stop in ATR multiples")
    ap.add_argument("--tp", type=float, default=2.0, help="target in ATR multiples")
    ap.add_argument("--hold", type=int, default=12, help="max hold in bars")
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--min-n", type=int, default=10)
    ap.add_argument("--tf", default=None,
                    help="restrict to one timeframe — REQUIRED for a clean gate "
                         "ablation, because cost_R varies 100x across timeframes "
                         "and otherwise the timeframe mix dominates every delta")
    ap.add_argument("--symbol", default=None, help="limit the simulation to one symbol")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    rows = load_rows(a.db)
    if a.tf:
        rows = [r for r in rows if r["timeframe"] == a.tf]
    print(f"loaded {len(rows)} resolved outcomes "
          f"({sum(1 for r in rows if r['direction'] != 0)} directional, "
          f"{sum(1 for r in rows if r['features'])} with features)"
          + (f"  [timeframe filter: {a.tf}]" if a.tf else "") + "\n")

    report: Dict[str, object] = {"params": vars(a), "n_rows": len(rows)}
    if a.view in ("outcomes", "all"):
        report["outcomes"] = view_outcomes(rows, a.stop, a.min_n)
    if a.view in ("gates", "all"):
        report["gates"] = view_gates(rows, a.stop, a.min_n)
    if a.view in ("simulate", "all"):
        report["simulate"] = view_simulate(
            rows, a.stop, a.tp, a.hold, a.risk,
            symbols=[a.symbol] if a.symbol else None,
            trades_path=((os.path.splitext(a.json)[0] + "_trades.jsonl")
                         if a.json else None))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
