"""
orderflow_calls.py — generate validation-grade trade calls from Binance order flow.

WHY THIS EXISTS
---------------
`/calls` had 0 entries and `/calls/stats` returned HTTP 500 (KeyError 'wins' on an
empty log). Two reasons nothing accumulated:

  1. The only existing generator, `quant-alert-engine/orderflow_engine.py`, scores
     through `signal_engine.compute_signal`, which needs ATAS LiveExporter
     footprint/tape. With ATAS closed the data coverage is 25/100 < 50, so
     `trade_plan_allowed` is always False and it never logs a single call.
  2. Nothing was running the generator anyway.

But the bridge already exposes a RICH Binance-only order-flow surface that does
not need ATAS at all (verified live):

    /taker-cvd/{sym}      session taker CVD in qty AND usd, plus session high/low
    /dom/{sym}            20 levels, imbalance_pct, bid/ask walls + persistence
    /derivatives/{sym}    openInterest, fundingRate, longShortRatio
    /context/{sym}        value area (POC/VAH/VAL), vol regime, expected move
    /blackout/status      macro event blackout (FOMC, options expiry)

This module turns those into calls, attaches the full feature vector as the
call's `snapshot`, and POSTs to /calls so the bridge's own resolver does the
rest: activation inside the entry zone, first touch of T1/SL, MFE/MAE in R, and
a cost-aware expectancy in `/calls/stats` (CALLS_COST_BPS, gross vs net).

DESIGN RULE: every RAW component is logged in the snapshot, and the composite
score is a fixed, documented weighting. That way the score can be re-weighted and
re-bucketed OFFLINE on the same data, instead of having to re-fit and re-run the
generator to test a different thesis.

Usage:
    python orderflow_calls.py scan              # one pass, print components, post NOTHING
    python orderflow_calls.py monitor           # loop: score -> POST /calls
    python orderflow_calls.py monitor --dry-run # loop but never POST
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from plan_gate import gate_plan  # audit action 9: creation-time reject gate

BASE = os.getenv("BRIDGE_URL", "http://127.0.0.1:8000").rstrip("/")
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "").strip()

DEFAULT_SYMBOLS = ["SOLUSDT", "XRPUSDT", "SUIUSDT", "HYPEUSDT", "SPCXUSDT",
                   "NVDAUSDT", "XAUUSDT"]

# Component weights. Single registry: rules.json "trigger_weights" is the source
# of truth (audit follow-up: one file, no third set); the dict below is the
# fallback when rules.json is unreadable. signal_engine's "scoring_weights" is
# the OTHER layer (single-symbol scorecard) — different features, same file.
# The composite only decides WHEN to log; the raw components in the snapshot
# are what get analysed later.
def _load_trigger_weights() -> Dict[str, float]:
    _defaults = {
        "taker_flow": 3.0,
        "book_imbalance": 3.0,
        "wall_pressure": 2.0,
        "value_location": 2.0,
        "oi_delta": 1.5,
        "funding_crowding": 1.5,
        "ls_ratio_crowding": 1.5,
    }
    try:
        with open(os.path.join(os.path.dirname(__file__), "rules.json")) as _f:
            _tw = json.load(_f).get("trigger_weights", {})
        _out = {k: float(_tw[k]) for k in _defaults if k in _tw}
        return {**_defaults, **_out}
    except Exception:
        return dict(_defaults)


WEIGHTS: Dict[str, float] = _load_trigger_weights()
TOTAL_WEIGHT = sum(WEIGHTS.values())

HISTORY_S = 300.0   # trailing window for delta features
HISTORY_MAXLEN = 64

# ATAS LiveExporter freshness gate. The bridge already refuses files older than
# 300s (_LIVE_STALE_LIMIT_SECONDS), but 5 minutes of footprint attached to a live
# call is worthless — and worse, it looks valid. 30s is the working limit.
ATAS_MAX_AGE_S = 30.0


def _headers() -> Dict[str, str]:
    h = {"User-Agent": "orderflow-calls"}
    if BRIDGE_TOKEN:
        h["X-Bridge-Token"] = BRIDGE_TOKEN
    return h


def get(path: str, timeout: float = 8.0) -> Dict[str, Any]:
    try:
        req = urllib.request.Request(BASE + path, headers=_headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {str(e)[:110]}"}


def post(path: str, payload: dict, timeout: float = 6.0) -> Dict[str, Any]:
    try:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode(),
            headers={**_headers(), "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"_err": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {str(e)[:110]}"}


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _num(d: dict, key: str, default: float = 0.0) -> float:
    v = (d or {}).get(key)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default      # NaN guard


# ---------------------------------------------------------------------------
# ATAS LiveExporter enrichment (footprint + Smart Tape). Pure feature extraction
# so it can be tested against a real payload without ATAS running.
# ---------------------------------------------------------------------------

def atas_features(payload: Optional[dict], symbol: str, now: float,
                  max_age: float = ATAS_MAX_AGE_S) -> Tuple[Optional[dict], str]:
    """Derive the ATAS feature block for `symbol`, or None with a reason.

    THE CRITICAL RULE: the LiveExporter reflects whichever symbol ATAS has on the
    chart. Attaching that footprint to a call on a different symbol would silently
    poison the measurement — the call would say "footprint confirmed" while showing
    a different market's book. So a ticker mismatch and a stale sample both return
    None (with the reason recorded on the snapshot), never a partial attach.
    """
    if not payload:
        return None, "no_payload"
    if "status" in payload and "ticker" not in payload:
        return None, f"atas_inactive({payload.get('status')})"
    ticker = str(payload.get("ticker") or "").upper()
    if not ticker:
        return None, "no_ticker"
    if ticker != symbol.upper():
        # NOT an error — ATAS is simply showing another instrument.
        return None, f"ticker_mismatch({ticker})"

    ts_ms = payload.get("timestamp")
    age = None
    if ts_ms is not None:
        try:
            age = max(0.0, now - float(ts_ms) / 1000.0)
        except (TypeError, ValueError):
            age = None
    if age is not None and age > max_age:
        return None, f"stale({age:.0f}s>{max_age:.0f}s)"

    fp = payload.get("footprint") or {}
    tape = payload.get("tape") or {}
    dom = payload.get("dom") or {}
    price = payload.get("price") or {}
    vol = payload.get("volume") or {}
    levels = fp.get("top_levels") or []
    last = _num(price, "last") or _num(dom, "best_bid")

    bid_tot = sum(_num(lv, "bid") for lv in levels)
    ask_tot = sum(_num(lv, "ask") for lv in levels)
    delta_tot = sum(_num(lv, "delta") for lv in levels)
    fp_imb = ((bid_tot - ask_tot) / (bid_tot + ask_tot)) if (bid_tot + ask_tot) > 0 else 0.0

    def _lvl_dist(price_key: str) -> Optional[float]:
        p = fp.get(price_key)
        try:
            p = float(p)
        except (TypeError, ValueError):
            return None
        return ((p - last) / last * 100.0) if (last and last > 0) else None

    largest = _num(tape, "largest_trade")
    ltp = tape.get("largest_trade_price")
    try:
        ltp = float(ltp)
    except (TypeError, ValueError):
        ltp = None

    return {
        "atas_age_s": age,
        "atas_ticker": ticker,
        "atas_bar_index": payload.get("bar_index"),
        "atas_delta": _num(payload, "delta"),
        # --- footprint: traded volume per price level, split by aggressor. This is
        #     the part Binance depth CANNOT provide (depth is resting size; this is
        #     what actually traded and who crossed the spread).
        "atas_fp_levels": len(levels),
        "atas_fp_bid_total": bid_tot,
        "atas_fp_ask_total": ask_tot,
        "atas_fp_delta_total": delta_tot,
        "atas_fp_imbalance": fp_imb,
        "atas_fp_poc": fp.get("poc"),
        "atas_fp_vah": fp.get("value_area_high"),
        "atas_fp_val": fp.get("value_area_low"),
        "atas_fp_poc_dist_pct": ((float(fp["poc"]) - last) / last * 100.0)
                                if (fp.get("poc") and last) else None,
        "atas_fp_max_bid_level": fp.get("max_bid_level"),
        "atas_fp_max_ask_level": fp.get("max_ask_level"),
        "atas_fp_max_bid_dist_pct": _lvl_dist("max_bid_level"),
        "atas_fp_max_ask_dist_pct": _lvl_dist("max_ask_level"),
        "atas_fp_top_levels": levels[:24],   # raw levels, for offline re-derivation
        # --- Smart Tape: aggressor-split trades, single prints
        "atas_tape_buy_vol": _num(tape, "buy_vol"),
        "atas_tape_sell_vol": _num(tape, "sell_vol"),
        "atas_tape_net_vol": _num(tape, "net_vol"),
        "atas_tape_buy_count_pct": _num(tape, "buy_count_pct"),
        "atas_tape_trades": _num(tape, "trades"),
        "atas_tape_avg_size": _num(tape, "avg_trade_size"),
        "atas_tape_largest_trade": largest,
        "atas_tape_largest_side": tape.get("largest_trade_side"),
        "atas_tape_largest_dist_pct": (((ltp - last) / last * 100.0)
                                       if (ltp and last) else None),
        # --- Smart DOM: ATAS's own stacking / iceberg view
        "atas_dom_imbalance_pct": _num(dom, "imbalance_pct"),
        "atas_dom_bid_ask_ratio": _num(dom, "bid_ask_ratio"),
        "atas_dom_bid_stack": _num(dom, "bid_stack"),
        "atas_dom_ask_stack": _num(dom, "ask_stack"),
        "atas_dom_spread": _num(dom, "spread"),
        "atas_volume_bid": _num(vol, "bid"),
        "atas_volume_ask": _num(vol, "ask"),
    }, "ok"


def atas_extra_components(f: dict) -> Dict[str, float]:
    """ATAS-derived components, LOGGED with weight 0 in the live composite.

    Deliberate: adding them to the composite would change the trigger depending on
    whether ATAS happens to be open, which destroys the binance_only vs
    binance+atas A/B (two different decision rules instead of one rule with more
    data). They are recorded so they can be weighted OFFLINE on the same sample.
    """
    out: Dict[str, float] = {}
    tot = _num(f, "atas_fp_bid_total") + _num(f, "atas_fp_ask_total")
    if tot > 0:
        out["atas_footprint"] = clamp(_num(f, "atas_fp_imbalance") / 0.5)
    tape_tot = _num(f, "atas_tape_buy_vol") + _num(f, "atas_tape_sell_vol")
    if tape_tot > 0:
        out["atas_tape"] = clamp((_num(f, "atas_tape_buy_vol") - _num(f, "atas_tape_sell_vol"))
                                 / tape_tot / 0.5)
    out["atas_dom_stack"] = clamp((_num(f, "atas_dom_bid_stack")
                                   - _num(f, "atas_dom_ask_stack"))
                                  / max(_num(f, "atas_dom_bid_stack")
                                        + _num(f, "atas_dom_ask_stack"), 1.0))
    return out


# ---------------------------------------------------------------------------
# feature assembly (RAW components -> snapshot)
# ---------------------------------------------------------------------------

def collect(symbol: str, hist: Deque[Tuple[float, dict]],
            atas_payload: Optional[dict] = None,
            atas_max_age: float = ATAS_MAX_AGE_S) -> Optional[dict]:
    """One observation for a symbol. Returns the full snapshot dict, or None.

    `atas_payload` is the shared /indicators payload for this pass (one ATAS chart
    feeds one symbol, so it is fetched once and offered to every symbol; only the
    matching ticker actually attaches — see atas_features()).
    """
    cvd = get(f"/taker-cvd/{symbol}")
    dom = get(f"/dom/{symbol.lower()}")
    der = get(f"/derivatives/{symbol}")
    ctx = get(f"/context/{symbol}")
    if any("_err" in d for d in (cvd, dom, ctx)):
        return None

    now = time.time()
    mid = (_num(dom, "best_bid") + _num(dom, "best_ask")) / 2.0
    if mid <= 0:
        return None

    hist.append((now, {
        "cvd_usd": _num(cvd, "cvd_usd"),
        "oi": _num(der, "openInterest"),
        "mid": mid,
    }))
    while len(hist) > HISTORY_MAXLEN:
        hist.popleft()
    while hist and (now - hist[0][0]) > HISTORY_S:
        hist.popleft()

    def _delta(key: str) -> float:
        if len(hist) < 2:
            return 0.0
        return hist[-1][1][key] - hist[0][1][key]

    # Self-calibrating flow scale. The first version normalised the trailing taker
    # delta by the SESSION's cumulative USD range, which on SOL is ~113M — so a real
    # -44k USD 12s delta scored -0.004 and the highest-weight component was dead.
    # The right baseline for "is this flow aggressive" is the flow's own recent
    # variability, not the session total.
    _deltas = []
    for i in range(1, len(hist)):
        _deltas.append(abs(hist[i][1]["cvd_usd"] - hist[i - 1][1]["cvd_usd"]))
    cvd_flow_scale = (sum(_deltas) / len(_deltas)) if _deltas else 0.0

    va = ctx.get("value_area") or {}
    vr = ctx.get("volatility_regime") or {}

    def _vr(name: str) -> Optional[float]:
        d = vr.get(name) or {}
        v = d.get("value")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    em = ctx.get("expected_move") or {}
    em_med = em.get("value")
    if isinstance(em_med, dict):
        em_med = em_med.get("value")
    em_p90 = em.get("p90")
    if em_p90 is None:
        ci = em.get("confidence_interval")
        em_p90 = ci[1] if isinstance(ci, (list, tuple)) and len(ci) > 1 else None

    atas_f, atas_status = atas_features(atas_payload, symbol, now, atas_max_age)

    snap = {
        "ts": now,
        "symbol": symbol,
        "mid": mid,
        # --- taker flow (aggressive orders) ---
        "cvd_usd": _num(cvd, "cvd_usd"),
        "cvd_high_usd": _num(cvd, "high_cvd_usd"),
        "cvd_low_usd": _num(cvd, "low_cvd_usd"),
        "cvd_bars": _num(cvd, "bars"),
        "cvd_usd_delta_win": _delta("cvd_usd"),
        "cvd_flow_scale": cvd_flow_scale,
        "hist_seconds": (now - hist[0][0]) if len(hist) > 1 else 0.0,
        # --- resting depth ---
        "dom_imbalance_pct": _num(dom, "imbalance_pct"),
        # Same field, time-averaged over ~45s by the bridge (_DOM_IMB_EMA_TAU_S). Logged
        # alongside the raw value so the choice can be A/B'd on outcomes later: the raw
        # top-15 snapshot moves 5.4 bias points per 8s round against the EMA's 2.5 and flips
        # the composite's zone 29 vs 12 times in 12 rounds x 9 symbols (dom_ab.py).
        "dom_imbalance_ema_pct": _num(dom, "imbalance_pct_ema"),
        "spread_pct": _num(dom, "spread_pct"),
        "total_bid_vol": _num(dom, "total_bid_vol"),
        "total_ask_vol": _num(dom, "total_ask_vol"),
        "bid_wall_price": dom.get("bid_wall_price"),
        "bid_wall_vol": _num(dom, "bid_wall_vol"),
        "bid_wall_persistent": bool(dom.get("bid_wall_persistent")),
        "ask_wall_price": dom.get("ask_wall_price"),
        "ask_wall_vol": _num(dom, "ask_wall_vol"),
        "ask_wall_persistent": bool(dom.get("ask_wall_persistent")),
        # --- derivatives / crowding ---
        "funding_rate": _num(der, "fundingRate"),
        "open_interest": _num(der, "openInterest"),
        "long_short_ratio": _num(der, "longShortRatio"),
        "oi_delta_win": _delta("oi"),
        # --- context (bridge's own value/vol model) ---
        "poc": va.get("poc"), "vah": va.get("vah"), "val": va.get("val"),
        "va_location": va.get("location"),
        "atr_percentile_rank": _vr("atr_percentile_rank"),
        "vol_expansion_rate": _vr("expansion_rate"),
        "vol_clustering": _vr("clustering"),
        "exp_move_med": em_med,
        "exp_move_p90": em_p90,
        # --- provenance: what is MISSING tells you how to read the result ---
        # atas_status records WHY there is no footprint (inactive / ticker mismatch /
        # stale), so a binance_only call is never mistaken for an atas-backed one.
        "atas_present": atas_f is not None,
        "atas_status": atas_status,
        "source": ("binance+atas" if atas_f else "binance_only"),
    }
    if atas_f:
        snap.update(atas_f)
    return snap


# ---------------------------------------------------------------------------
# scoring (raw components -> documented composite)
# ---------------------------------------------------------------------------

def raw_components(s: dict) -> Dict[str, float]:
    """Signed raw components in [-1,+1] (or z-scored later), all from the snapshot.

    Split into two families on purpose:
      * VARIABLE components differ across symbols right now (flow, book, walls,
        OI build-up) -> usable as-is.
      * LEVEL components (funding, long/short ratio, where price sits in the value
        area) are market-wide levels. Measured live, they came out nearly
        IDENTICAL across all 7 symbols (ls_ratio -0.88..-1.00, value_location
        +1.00 on four of them), so as raw scores they carry no cross-sectional
        information and would simply tilt every call the same way. They are
        z-scored across the symbols sampled at the same instant instead, so only
        the relative outlier votes.
    """
    c: Dict[str, float] = {}

    # --- variable: aggressive taker flow, in units of its OWN recent variability.
    #     Using the session's cumulative range made this ~0 (a real -44k USD 12s
    #     delta against a ~113M session range scored -0.004), dead-weighting the
    #     highest-weight component. 3x the mean absolute recent delta = full vote.
    scale = _num(s, "cvd_flow_scale")
    if scale > 0:
        c["taker_flow"] = clamp(_num(s, "cvd_usd_delta_win") / (3.0 * scale))
    else:
        c["taker_flow"] = 0.0

    # --- variable: resting depth imbalance (already -100..100)
    # Input field is switchable (DOM_IMB_FIELD=ema|raw). Measured offline across 12 polls x 9
    # symbols: the raw top-15 snapshot moves 5.37 composite points per 8s round and flips the
    # zone label 29 times, the 45s EMA 2.50 and 12 — the raw field is dominated by sampling
    # noise. Both are logged in the snapshot, so this stays A/B-able on real outcomes.
    _imb_field = (os.environ.get("DOM_IMB_FIELD", "ema") or "ema").strip().lower()
    _imb_raw = _num(s, "dom_imbalance_pct")
    _imb_ema = _num(s, "dom_imbalance_ema_pct")
    if _imb_field == "raw" or _imb_ema is None:
        c["book_imbalance"] = clamp(_imb_raw / 40.0)
        _imb_used = "raw"
    else:
        c["book_imbalance"] = clamp(_imb_ema / 40.0)
        _imb_used = "ema"
    s["book_imbalance_input"] = _imb_used
    s["book_imbalance_from_raw"] = clamp(_imb_raw / 40.0)
    s["book_imbalance_from_ema"] = clamp(_imb_ema / 40.0)

    # --- variable: wall size imbalance, doubled when only one side is persistent
    bw, aw = _num(s, "bid_wall_vol"), _num(s, "ask_wall_vol")
    if bw + aw > 0:
        w = (bw - aw) / (bw + aw)
        if bool(s.get("bid_wall_persistent")) != bool(s.get("ask_wall_persistent")):
            w *= 2.0
        c["wall_pressure"] = clamp(w)
    else:
        c["wall_pressure"] = 0.0

    # --- variable: OI build-up as a PERCENTAGE of open interest. Normalising by
    #     the raw OI also made this ~0 (62 SOL on 8.1M is 0.0008%); 0.05% of OI
    #     change over the trailing window is what a real build-up looks like, so
    #     that maps to a full vote.
    oi = abs(_num(s, "open_interest"))
    if oi > 0:
        c["oi_delta"] = clamp((_num(s, "oi_delta_win") / oi * 100.0) / 0.05)
    else:
        c["oi_delta"] = 0.0

    # --- LEVEL components: NOT clamped here. Clamping before the cross-sectional
    #     z-score flattened the ordering (five symbols pinned at the same value),
    #     so the spread that carries the information was destroyed before the
    #     transform that needs it. Bounding happens after z-scoring.
    c["_level_funding"] = -(_num(s, "funding_rate") / 0.0005)
    lsr = _num(s, "long_short_ratio")
    c["_level_ls_ratio"] = (-((lsr - 1.0) / 1.5)) if lsr > 0 else 0.0
    poc, vah, val = _num(s, "poc"), _num(s, "vah"), _num(s, "val")
    mid = _num(s, "mid")
    width = (vah - val) if (vah > val) else 0.0
    if width > 0 and mid > 0:
        # negative = below the value area (discount), positive = above (premium)
        c["_level_value"] = -((mid - poc) / (0.5 * width))
    else:
        c["_level_value"] = 0.0
    return c


def score_cross_section(snaps: Dict[str, dict]) -> Dict[str, Tuple[float, Dict[str, float]]]:
    """Score all symbols together so market-wide levels cannot fake a signal."""
    # raw level key -> the WEIGHTS key it feeds. Kept explicit: an earlier version
    # derived the name with lstrip("_") and silently produced keys that matched no
    # weight, so the z-scored components scored as 0 and nothing said so.
    LEVEL_TO_WEIGHT = {"_level_funding": "funding_crowding",
                       "_level_ls_ratio": "ls_ratio_crowding",
                       "_level_value": "value_location"}
    raws = {sym: raw_components(s) for sym, s in snaps.items()}
    stats: Dict[str, Tuple[float, float]] = {}
    for lv in LEVEL_TO_WEIGHT:
        vals = [raws[s][lv] for s in snaps]
        if len(vals) >= 3:
            mu = sum(vals) / len(vals)
            sd = (sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
        else:
            mu, sd = 0.0, 0.0
        stats[lv] = (mu, sd if sd > 1e-9 else 0.0)

    out: Dict[str, Tuple[float, Dict[str, float]]] = {}
    for sym, raw in raws.items():
        c = dict(raw)
        for lv, wkey in LEVEL_TO_WEIGHT.items():
            mu, sd = stats[lv]
            # no cross-sectional spread -> the level carries no information here
            c[wkey] = clamp((raw[lv] - mu) / (2.0 * sd)) if sd > 0 else 0.0
            c.pop(lv, None)
        score = 100.0 * sum(WEIGHTS[k] * c.get(k, 0.0) for k in WEIGHTS) / TOTAL_WEIGHT
        out[sym] = (score, c)
    return out


def score_snapshot(s: dict) -> Tuple[float, Dict[str, float]]:
    """Single-snapshot convenience (no cross-section). The level components are
    zeroed rather than scored raw, because scoring them raw is exactly the bug
    that tilted every call the same way. Use score_cross_section() for real runs."""
    c = raw_components(s)
    for lv, wkey in (("_level_funding", "funding_crowding"),
                     ("_level_ls_ratio", "ls_ratio_crowding"),
                     ("_level_value", "value_location")):
        c[wkey] = 0.0
        c.pop(lv, None)
    score = 100.0 * sum(WEIGHTS[k] * c.get(k, 0.0) for k in WEIGHTS) / TOTAL_WEIGHT
    return score, c


def build_plan(s: dict, direction: str, entry_pct: float = 0.0005,
               sl_mult: float = 0.85, t1_mult: float = 1.0,
               t2_mult: float = 1.8) -> Optional[dict]:
    """Entry zone + SL/T1/T2 built from the bridge's own expected-move figures.

    Distances are a fraction of price (expected move is already in %), so this
    inherits the bridge's volatility model instead of inventing one.

    Audit C-2 action 2 (2026-09-17): cost-aware geometry. A tight stop is not
    "lower risk", it is a larger share of the trade paid to the exchange:
      - plans whose natural stop implies cost_r > MAX_COST_R are SUPPRESSED
        (returns None; the monitor logs the reason). At 8bp this suppresses
        XAU/NVDA/BTC/SPCX and keeps HYPE/SUI/XRP/SOL.
      - kept plans get T1 >= MIN_T1_OVER_SL x stop, so breakeven stays <= 46%.
    """
    max_cost_r = float(os.getenv("ORDERS_MAX_COST_R", "0.15"))
    min_t1_over_sl = float(os.getenv("ORDERS_MIN_T1_OVER_SL", "1.5"))
    cost_bps = float(os.getenv("CALLS_COST_BPS", "8.0"))
    mid = _num(s, "mid")
    med = s.get("exp_move_med")
    p90 = s.get("exp_move_p90")
    try:
        med = float(med)
    except (TypeError, ValueError):
        med = 0.0
    try:
        p90 = float(p90)
    except (TypeError, ValueError):
        p90 = 0.0
    if med <= 0:
        return None
    if med < 0.05:            # some producers emit fractions instead of percent
        med *= 100.0
    if p90 <= 0 or p90 < 0.05:
        p90 = med * 4.6
    if p90 != p90:
        p90 = med * 4.6

    half = mid * entry_pct
    sl_d = mid * (med / 100.0) * sl_mult
    t1_d = mid * (med / 100.0) * t1_mult
    t2_d = mid * (med / 100.0) * t2_mult * (p90 / med if med else 1.0)
    if mid > 0 and sl_d > 0:
        cost_r = (cost_bps / 1e4) * mid / sl_d
        if cost_r > max_cost_r:
            s["_suppress_reason"] = (
                f"cost_r {cost_r:.3f}R > {max_cost_r:.2f}R "
                f"(stop {100*sl_d/mid:.3f}% can't carry {cost_bps:.0f}bp)")
            return None
        t1_d = max(t1_d, min_t1_over_sl * sl_d)
    lo, hi = mid - half, mid + half
    if direction == "long":
        plan = {"entry_low": lo, "entry_high": hi, "sl": lo - sl_d,
                "t1": hi + t1_d, "t2": hi + t2_d}
    else:
        plan = {"entry_low": lo, "entry_high": hi, "sl": hi + sl_d,
                "t1": lo - t1_d, "t2": lo - t2_d}
    # the bridge rejects malformed geometry, so never send it one
    if not (plan["sl"] < lo < hi < plan["t1"]) and direction == "long":
        return None
    if direction == "short" and not (plan["t1"] < lo < hi < plan["sl"]):
        return None
    return {k: round(v, 8) for k, v in plan.items()}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _blackout() -> Optional[dict]:
    b = get("/blackout/status")
    return None if "_err" in b else b


def scan(symbols: List[str], atas_mode: str = "auto",
         atas_max_age: float = ATAS_MAX_AGE_S) -> int:
    print(f"bridge: {BASE}   symbols: {len(symbols)}   atas: {atas_mode}")
    bo = _blackout()
    if bo:
        print(f"blackout now={bo.get('blackout')}  next={((bo.get('next_event') or {}).get('name'))} "
              f"in {bo.get('seconds_to_next', 0)/3600:.1f}h")
    atas_payload = None if atas_mode == "off" else get("/indicators")
    if atas_mode != "off":
        at = (atas_payload or {}).get("ticker")
        st = (atas_payload or {}).get("status")
        print(f"ATAS /indicators -> ticker={at!r} status={st!r} "
              f"(one chart, one symbol: only its ticker can attach footprint)")
    print(f"\n{'symbol':<10} {'mid':>10} {'score':>7} {'atas':>22}  components (signed)")
    print("-" * 118)
    hists: Dict[str, Deque] = {s: deque(maxlen=HISTORY_MAXLEN) for s in symbols}
    snaps = {}
    for s in symbols:
        snap = collect(s, hists[s], atas_payload, atas_max_age)
        if snap:
            snaps[s] = snap
    scored = score_cross_section(snaps)
    for s in symbols:
        if s not in snaps:
            print(f"{s:<10} {'--':>10} {'--':>7} {'--':>22}  no data (bridge endpoint error)")
            continue
        sc, comp = scored[s]
        parts = " ".join(f"{k[:9]}={comp.get(k, 0.0):+.2f}" for k in WEIGHTS)
        tag = "+atas" if snaps[s].get("atas_present") else str(snaps[s].get("atas_status"))
        print(f"{s:<10} {snaps[s]['mid']:>10.5f} {sc:>+7.1f} {tag:>22}  {parts}")
    print("\nLevel components (funding / long-short / value location) are z-scored "
          "ACROSS these symbols: market-wide levels cannot discriminate between them.")
    print("NOTE: with a single pass the trailing-delta features are 0 (no history "
          "yet); run `monitor` so the 5-minute window fills.")
    return 0


def monitor(symbols: List[str], poll: float, cooldown: float, min_score: float,
            dry_run: bool, log_path: Optional[str], entry_pct: float,
            max_open: int, warmup: float = 90.0, atas_mode: str = "auto",
            atas_max_age: float = ATAS_MAX_AGE_S) -> int:
    hists: Dict[str, Deque] = {s: deque(maxlen=HISTORY_MAXLEN) for s in symbols}
    last_call: Dict[str, float] = {s: 0.0 for s in symbols}
    # Audit C-3 action 3 (2026-09-17): DOM leg-(c) persistence. A single top-15
    # snapshot is sampling noise, so the EMA leg side must hold 3 consecutive
    # polls before a plan may log. Deque holds +1 (EMA>+10) / -1 (EMA<+5) / 0.
    legc: Dict[str, Deque] = {s: deque(maxlen=3) for s in symbols}
    t0 = time.time()
    print(f"[orderflow-calls] {len(symbols)} symbols, poll {poll:.0f}s, "
          f"cooldown {cooldown:.0f}s, |score|>={min_score:.0f}, "
          f"warmup {warmup:.0f}s, interp {sys.executable}")
    print(f"[orderflow-calls] {'DRY RUN (no POST)' if dry_run else 'POSTing to ' + BASE + '/calls'}")
    if log_path:
        print(f"[orderflow-calls] logging payloads to {log_path}")
    print(f"[orderflow-calls] no calls are emitted during the warmup: with an empty "
          f"history the taker-flow and OI components are still 0, so the composite "
          f"would be scored on a partial feature set.")

    while True:
        try:
            bo = _blackout() or {}
            if bo.get("blackout"):
                print(f"[{time.strftime('%H:%M:%S')}] blackout active "
                      f"({((bo.get('active_event') or {}).get('name'))}) — no calls")
                time.sleep(poll)
                continue

            opened = get("/calls")
            n_open = len(opened.get("open", [])) if isinstance(opened, dict) else 0

            # One ATAS chart feeds one symbol: fetch /indicators once per pass and
            # hand it to every symbol; only the ticker match attaches.
            atas_payload = None if atas_mode == "off" else get("/indicators")

            snaps: Dict[str, dict] = {}
            for s in symbols:
                snap = collect(s, hists[s], atas_payload, atas_max_age)
                if snap:
                    snaps[s] = snap
            scored = score_cross_section(snaps)

            for s, snap in snaps.items():
                sc, comp = scored[s]
                now = time.time()
                # Leg-(c) history builds on every pass (even warmup/cooldown).
                ema_leg = snap.get("dom_imbalance_ema_pct")
                legc[s].append(1 if ema_leg is not None and ema_leg > 10
                               else (-1 if ema_leg is not None and ema_leg < 5 else 0))
                if (now - t0) < warmup:
                    continue
                if abs(sc) < min_score:
                    continue
                if (now - last_call[s]) < cooldown:
                    continue
                if n_open >= max_open:
                    print(f"[{time.strftime('%H:%M:%S')}] {max_open} calls already open "
                          f"— not adding more")
                    break
                direction = "long" if sc > 0 else "short"
                streak = list(legc[s])
                want = 1 if direction == "long" else -1
                if not (len(streak) == 3 and all(x == want for x in streak)):
                    continue
                snap["dom_legc_streak"] = streak
                snap["dom_legc_side"] = want
                plan = build_plan(snap, direction, entry_pct=entry_pct)
                if not plan:
                    _why = snap.pop("_suppress_reason", None) or (
                        "exp_move_med=" + str(snap.get("exp_move_med")))
                    print(f"  ! {s}: no valid plan ({_why})")
                    continue
                gate_ok, gate_why = gate_plan(plan)
                if not gate_ok:
                    print(f"  ! {s}: plan gate REJECT ({'; '.join(gate_why)})")
                    continue
                # ATAS components are logged but carry weight 0 in the live
                # composite, so the trigger stays identical with or without ATAS —
                # that is what keeps binance_only vs binance+atas comparable.
                comps = dict(comp)
                if snap.get("atas_present"):
                    comps.update(atas_extra_components(snap))
                atas_tag = ("+atas" if snap.get("atas_present")
                            else f"binance_only[{snap.get('atas_status')}]")
                payload = {
                    "symbol": s, "direction": direction,
                    **plan,
                    # bias_score REMOVED (audit C-1 action 1, 2026-09-17): the
                    # composite only triggers (|score|>=min_score), it never
                    # permits/forbids. Raw components in snapshot stay.
                    "note": (f"orderflow score {sc:+.1f} "
                             f"(taker {comp['taker_flow']:+.2f} book {comp['book_imbalance']:+.2f} "
                             f"wall {comp['wall_pressure']:+.2f}) snapshot={atas_tag}"),
                    "snapshot": {**snap, "components": comps},
                }
                if dry_run:
                    print(f"[{time.strftime('%H:%M:%S')}] DRY {s} {direction.upper()} "
                          f"score {sc:+.1f} snapshot={atas_tag} "
                          f"entry {plan['entry_low']}-{plan['entry_high']} SL {plan['sl']} T1 {plan['t1']}")
                    last_call[s] = now
                    continue
                res = post("/calls", payload)
                if "_err" in res:
                    print(f"  ! {s} POST failed: {res['_err']}")
                    continue
                last_call[s] = now
                n_open += 1
                print(f"[{time.strftime('%H:%M:%S')}] CALL {res.get('call_id')} {s} "
                      f"{direction.upper()} score {sc:+.1f} @ {snap['mid']} "
                      f"snapshot={atas_tag}")
                if log_path:
                    try:
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps({"call_id": res.get("call_id"),
                                                "payload": payload}, default=str) + "\n")
                    except OSError:
                        pass
        except KeyboardInterrupt:
            print("stopped")
            return 0
        except Exception as e:
            print(f"  ! loop error: {type(e).__name__}: {e}")
        time.sleep(poll)


def main() -> int:
    ap = argparse.ArgumentParser(description="Order-flow call generator (Binance-only)")
    ap.add_argument("cmd", choices=["scan", "monitor"])
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--poll", type=float, default=15.0)
    ap.add_argument("--cooldown", type=float, default=1200.0)
    ap.add_argument("--min-score", type=float, default=20.0)
    ap.add_argument("--entry-pct", type=float, default=0.0005)
    ap.add_argument("--max-open", type=int, default=12,
                    help="never hold more than N open calls (keeps the sample independent)")
    ap.add_argument("--warmup", type=float, default=90.0,
                    help="seconds before the first call, so flow/OI history exists")
    ap.add_argument("--atas", dest="atas_mode", default="auto", choices=["auto", "off"],
                    help="'auto' attaches ATAS footprint/tape when ATAS is showing "
                         "the same symbol; 'off' never attaches")
    ap.add_argument("--atas-max-age", type=float, default=ATAS_MAX_AGE_S,
                    help="reject ATAS samples older than this many seconds")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", default=None)
    a = ap.parse_args()
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if a.cmd == "scan":
        return scan(symbols, a.atas_mode, a.atas_max_age)
    return monitor(symbols, a.poll, a.cooldown, a.min_score, a.dry_run, a.log,
                   a.entry_pct, a.max_open, a.warmup, a.atas_mode, a.atas_max_age)


if __name__ == "__main__":
    sys.exit(main())
