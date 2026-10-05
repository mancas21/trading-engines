"""
features.py — Canonical, parity-guaranteed ML feature extraction.

Single source of truth for the ML feature vector. BOTH the live logger
(`quant_alert_engine.AlertManager.tag_signal`) and the historical backfill
(`ml_backfill.py`) call `FeatureExtractor.vector()` over the SAME trailing
window of CLOSED bars, so a model trained on backfilled rows sees exactly the
numbers the engine computes in production. Without this, train/serve skew
silently destroys the model.

Design rules:
  * Every feature is CAUSAL: the vector at bar i uses bars [i-w+1 .. i] only.
    The forming (unclosed) candle is never included.
  * Every feature is SCALE-FREE (percent, rank, bounded ratio) so symbols with
    wildly different price scales (SUI ~0.8, NVDA ~225) pool into one model.
  * No lookahead, no forward-fill of future data, no repainting.

Why this file exists (see ml_train.py / ml_backfill.py docstrings): the
`signal_outcomes` table recorded only metadata (symbol, timeframe, signal_name,
fired_ts, price), so a logistic baseline on it scored AUC 0.443 — worse than
chance. Features were the bottleneck, not the model.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# NOTE: `TechnicalIndicators` lives in quant_alert_engine.py, which imports THIS
# module at startup. A top-level import here would be circular and silently
# resolve to None inside the engine's guarded import, disabling all feature
# logging. Resolve it lazily instead, once the engine module is fully loaded.
_INDD = None


def _get_indicators():
    global _INDD
    if _INDD is None:
        from quant_alert_engine import TechnicalIndicators as _TI
        _INDD = _TI()
    return _INDD

# Trailing window (bars) used for every rolling statistic. Must match the
# engine's OHLCV fetch (`SignalAnalyzer.fetch_ohlcv(limit=200)`), otherwise the
# live vector and the backfilled vector are computed over different history and
# the model silently degrades at serve time.
DEFAULT_WINDOW = 200

# ---------------------------------------------------------------------------
# Feature contract — ORDER IS PART OF THE MODEL FORMAT. Append only.
# ---------------------------------------------------------------------------
FEATURES: Tuple[str, ...] = (
    # momentum / oscillators
    "rsi",                # 0-100
    "rsi_dist50",         # rsi - 50
    "rsi_slope3",         # rsi[i] - rsi[i-3]
    "macd_line_pct",      # macd line as % of price
    "macd_signal_pct",
    "macd_hist_pct",      # histogram as % of price
    "macd_hist_slope3",
    "macd_above",         # 1 if macd_line > signal
    "stoch_k",            # 0-100
    "stoch_kd",           # %K - %D
    # volatility / bands
    "atr_pct",            # ATR as % of price  (the key vol feature)
    "atr_rank_pct",       # ATR percentile rank inside the window
    "atr_expansion",      # atr / sma(atr, 20)
    "bb_width_pct",       # (upper-lower)/mid * 100
    "bb_width_rank_pct",  # BB width percentile rank inside the window
    "bb_pos",             # (close - lower) / (upper - lower), 0-1
    "range_pct",          # (high-low)/close * 100 of the signal bar
    # volume / order flow proxy
    "vol_ratio",          # volume / sma(volume, 20)
    "vol_ratio5",         # mean(vol,5) / mean(vol,20)
    "delta_pct_vol",      # signed bar delta as % of bar volume
    "cvd_cum_pct",        # cumulative signed volume as % of window volume
    "cvd_slope5_pct",     # 5-bar CVD change as % of window volume
    "obv_slope5_pct",     # 5-bar OBV change as % of window volume
    # trend / structure
    "ema50_dist_pct",     # (close - ema50)/close * 100
    "ema200_dist_pct",
    "ret1_pct",
    "ret5_pct",
    "ret20_pct",
    "er20",               # Kaufman efficiency ratio 20 (0=range, 1=trend)
    "trend_consistency",  # fraction of last 20 bars closing up
    # value area (rolling volume profile)
    "va_pos",             # -1 below VAL, 0 inside, +1 above VAH
    "va_dist_poc_pct",    # (close - POC)/close * 100
    "va_width_pct",       # (VAH - VAL)/POC * 100
    # engine context (same numbers the alert pipeline already computes)
    "regime",             # -1 / 0 / +1 trend regime
    "bull_conf",          # 0-6 confluence count
    "bear_conf",
    "conf_diff",          # bull_conf - bear_conf
    # time-of-day seasonality (crypto trades 24/7 but funding/liquidity cycle)
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
)

N_FEATURES = len(FEATURES)
FEATURE_INDEX = {name: i for i, name in enumerate(FEATURES)}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _safe(x) -> float:
    """NaN/inf -> 0.0 so the model never sees a non-finite value."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _rank_pct(arr: np.ndarray, i: int, w: int) -> float:
    """Percentile rank (0-1) of arr[i] inside the trailing w bars."""
    if i < 0 or i >= len(arr):
        return 0.5
    start = max(0, i - w + 1)
    win = arr[start:i + 1]
    win = win[np.isfinite(win)]
    if len(win) < 5:
        return 0.5
    return float((win <= arr[i]).sum()) / float(len(win))


def _efficiency_ratio(c: np.ndarray, start: int, i: int, length: int) -> float:
    """|net move| / sum(|bar-to-bar moves|) over `length` bars. 0=range, 1=trend."""
    if i - length < start:
        return 0.0
    net = abs(c[i] - c[i - length])
    path = float(np.abs(np.diff(c[i - length:i + 1])).sum())
    return (net / path) if path > 0 else 0.0


def _value_location(c: np.ndarray, v: np.ndarray, start: int, i: int,
                    bins: int = 50) -> Tuple[float, float]:
    """Rolling volume profile over [start..i]: (c - POC)/c*100 and location.

    Mirrors `StrategyBacktester._value_location` so the backtester, the live
    logger and the model all use one value-area concept.
    """
    cw = c[start:i + 1]
    vw = v[start:i + 1]
    if len(cw) < 20 or cw.max() <= cw.min():
        return 0.0, 0.0
    hist, edges = np.histogram(cw, bins=bins, weights=vw)
    if hist.sum() <= 0:
        return 0.0, 0.0
    poc_idx = int(np.argmax(hist))
    poc = float((edges[poc_idx] + edges[poc_idx + 1]) / 2)
    total = hist.sum()
    target = total * 0.70
    acc = hist[poc_idx]
    lo = hi = poc_idx
    while acc < target and (lo > 0 or hi < len(hist) - 1):
        if lo == 0:
            hi += 1
        elif hi == len(hist) - 1:
            lo -= 1
        elif hist[lo - 1] > hist[hi + 1]:
            lo -= 1
        else:
            hi += 1
        acc = hist[lo:hi + 1].sum()
    val = float(edges[lo])
    vah = float(edges[hi + 1])
    cur = float(c[i])
    width = ((vah - val) / poc * 100.0) if poc > 0 else 0.0
    return (cur - poc) / cur * 100.0 if cur > 0 else 0.0, width


# ---------------------------------------------------------------------------
# extractor
# ---------------------------------------------------------------------------

class FeatureExtractor:
    """Computes the canonical feature vector. Cheap: reuse one instance."""

    def __init__(self, config=None, window: int = DEFAULT_WINDOW):
        self.config = config
        self.ind = _get_indicators()
        self.window = int(window)
        self._cfg = self._read_cfg()

    def _read_cfg(self) -> Dict[str, float]:
        c = self.config
        g = (lambda k, d: c.get(k, d)) if c is not None else (lambda k, d: d)
        return {
            "rsi_len": int(g("rsi.length", 14)),
            "macd_fast": int(g("macd.fast", 12)),
            "macd_slow": int(g("macd.slow", 26)),
            "macd_signal": int(g("macd.signal", 9)),
            "k_len": int(g("stochastic.k_len", 14)),
            "d_len": int(g("stochastic.d_len", 3)),
            "k_smooth": int(g("stochastic.smooth", 3)),
            "bb_len": int(g("volatility.bb_len", 20)),
            "bb_mult": float(g("volatility.bb_mult", 2.0)),
            "atr_len": int(g("volatility.atr_len", 14)),
            "vol_avg_len": int(g("volume.avg_len", 20)),
            "regime_len": int(g("general.regime_trend_len", 50)),
        }

    def vector(self, o: np.ndarray, h: np.ndarray, l: np.ndarray,
               c: np.ndarray, v: np.ndarray,
               i: Optional[int] = None) -> Dict[str, float]:
        """Feature dict at bar index i (default: last bar).

        Only bars before/at i are read. Returns {} when there is not enough
        history — callers must treat that as "no features", never as zeros.
        """
        cfg = self._cfg
        n = len(c)
        if n < 60:
            return {}
        if i is None:
            i = n - 1
        if i < 55 or i >= n:
            return {}
        start = max(0, i - self.window + 1)
        w = i - start + 1
        if w < 60:
            return {}

        price = float(c[i])
        if not math.isfinite(price) or price <= 0:
            return {}

        co = c[start:i + 1]
        ho = h[start:i + 1]
        lo = l[start:i + 1]
        vo = v[start:i + 1]

        # --- indicators over the trailing window (parity with live) ---
        rsi = self.ind.rsi(co, cfg["rsi_len"])
        ml, sl, hist = self.ind.macd(co, cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"])
        k, d = self.ind.stochastic(ho, lo, co, cfg["k_len"], cfg["d_len"], cfg["k_smooth"])
        bb_u, bb_m, bb_l = self.ind.bollinger_bands(co, cfg["bb_len"], cfg["bb_mult"])
        atr = self.ind.atr(ho, lo, co, cfg["atr_len"])
        ema_reg = self.ind.ema(co, cfg["regime_len"])
        ema200 = self.ind.ema(co, min(200, max(60, w // 2)))
        vol_avg = self.ind.sma(vo, cfg["vol_avg_len"])

        # signed bar volume (tick-rule proxy) -> CVD + OBV, same math as engine
        signed = np.where(np.diff(co, prepend=co[0]) >= 0, vo, -vo)
        cvd = np.cumsum(signed)
        obv = np.cumsum(signed)

        j = w - 1  # last index inside the window

        def at(arr, idx):
            return float(arr[idx]) if idx >= 0 and idx < len(arr) and np.isfinite(arr[idx]) else 0.0

        rsi_v = at(rsi, j)
        ml_v, sl_v, hist_v = at(ml, j), at(sl, j), at(hist, j)
        k_v, d_v = at(k, j), at(d, j)
        u_v, m_v, l_v = at(bb_u, j), at(bb_m, j), at(bb_l, j)
        atr_v = at(atr, j)
        vol_v = float(vo[j]) if np.isfinite(vo[j]) else 0.0
        vol_avg_v = at(vol_avg, j)

        bb_width = ((u_v - l_v) / m_v * 100.0) if m_v > 0 else 0.0
        bb_pos = ((price - l_v) / (u_v - l_v)) if (u_v - l_v) > 0 else 0.5
        win_vol_sum = float(vo.sum()) or 1.0

        # delta of the signal bar, as % of its own volume
        delta_pct_vol = 0.0
        if vol_v > 0:
            up = co[j] >= co[j - 1] if j >= 1 else True
            delta_pct_vol = (100.0 if up else -100.0)

        cvd_slope5 = at(cvd, j) - at(cvd, max(0, j - 5))
        obv_slope5 = at(obv, j) - at(obv, max(0, j - 5))
        va_dist_poc, va_width = _value_location(co, vo, 0, j)

        # value-area location vs VAL/VAH
        if va_width > 0:
            # reconstruct band from POC +/- half of the computed width
            poc = price - (va_dist_poc / 100.0) * price
            half = (va_width / 100.0) * poc / 2.0
            if price > poc + half:
                va_pos = 1.0
            elif price < poc - half:
                va_pos = -1.0
            else:
                va_pos = 0.0
        else:
            va_pos = 0.0

        # trend regime, identical to SignalAnalyzer._trend_regime
        regime = 0.0
        ema_v = at(ema_reg, j)
        if ema_v > 0 and atr_v > 0:
            band = 0.25 * atr_v
            if price > ema_v + band:
                regime = 1.0
            elif price < ema_v - band:
                regime = -1.0

        # confluence counts, identical to SignalAnalyzer._confluence
        bull = bear = 0
        if rsi_v > 0:
            bull += 1 if rsi_v > 50 else 0
            bear += 1 if rsi_v < 50 else 0
        if ml_v or sl_v:
            bull += 1 if ml_v > sl_v else 0
            bear += 1 if ml_v < sl_v else 0
        if k_v or d_v:
            bull += 1 if k_v > d_v else 0
            bear += 1 if k_v < d_v else 0
        if m_v:
            bull += 1 if price > m_v else 0
            bear += 1 if price < m_v else 0
        if vol_avg_v:
            bull += 1 if vol_v > vol_avg_v else 0
            bear += 1 if vol_v < vol_avg_v else 0
        if ema_v:
            bull += 1 if price > ema_v else 0
            bear += 1 if price < ema_v else 0

        # time-of-day from the signal bar's own timestamp is added by the caller
        vec = {
            "rsi": rsi_v,
            "rsi_dist50": rsi_v - 50.0,
            "rsi_slope3": rsi_v - at(rsi, max(0, j - 3)),
            "macd_line_pct": ml_v / price * 100.0,
            "macd_signal_pct": sl_v / price * 100.0,
            "macd_hist_pct": hist_v / price * 100.0,
            "macd_hist_slope3": (hist_v - at(hist, max(0, j - 3))) / price * 100.0,
            "macd_above": 1.0 if ml_v > sl_v else 0.0,
            "stoch_k": k_v,
            "stoch_kd": k_v - d_v,
            "atr_pct": atr_v / price * 100.0,
            "atr_rank_pct": _rank_pct(atr, j, w),
            "atr_expansion": (atr_v / at(atr, max(0, j - 20))) if at(atr, max(0, j - 20)) > 0 else 1.0,
            "bb_width_pct": bb_width,
            "bb_width_rank_pct": _rank_pct((bb_u - bb_l) / np.where(bb_m > 0, bb_m, np.nan), j, w),
            "bb_pos": bb_pos,
            "range_pct": (float(ho[j]) - float(lo[j])) / price * 100.0,
            "vol_ratio": (vol_v / vol_avg_v) if vol_avg_v > 0 else 1.0,
            "vol_ratio5": (float(np.mean(vo[-5:])) / float(np.mean(vo))) if len(vo) >= 20 else 1.0,
            "delta_pct_vol": delta_pct_vol,
            "cvd_cum_pct": at(cvd, j) / win_vol_sum * 100.0,
            "cvd_slope5_pct": cvd_slope5 / win_vol_sum * 100.0,
            "obv_slope5_pct": obv_slope5 / win_vol_sum * 100.0,
            "ema50_dist_pct": ((price - ema_v) / price * 100.0) if ema_v > 0 else 0.0,
            "ema200_dist_pct": ((price - at(ema200, j)) / price * 100.0) if at(ema200, j) > 0 else 0.0,
            "ret1_pct": ((price - float(co[j - 1])) / float(co[j - 1]) * 100.0) if j >= 1 and co[j - 1] > 0 else 0.0,
            "ret5_pct": ((price - float(co[j - 5])) / float(co[j - 5]) * 100.0) if j >= 5 and co[j - 5] > 0 else 0.0,
            "ret20_pct": ((price - float(co[j - 20])) / float(co[j - 20]) * 100.0) if j >= 20 and co[j - 20] > 0 else 0.0,
            "er20": _efficiency_ratio(co, 0, j, 20),
            "trend_consistency": float(np.mean(np.diff(co[-20:]) > 0)) if len(co) >= 20 else 0.5,
            "va_pos": va_pos,
            "va_dist_poc_pct": va_dist_poc,
            "va_width_pct": va_width,
            "regime": regime,
            "bull_conf": float(bull),
            "bear_conf": float(bear),
            "conf_diff": float(bull - bear),
        }
        return vec

    @staticmethod
    def add_time_features(vec: Dict[str, float], ts: float) -> Dict[str, float]:
        """Attach cyclical time-of-day / day-of-week from the signal bar's UTC ts."""
        d = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        vec["hour_sin"] = math.sin(2 * math.pi * d.hour / 24.0)
        vec["hour_cos"] = math.cos(2 * math.pi * d.hour / 24.0)
        vec["dow_sin"] = math.sin(2 * math.pi * d.weekday() / 7.0)
        vec["dow_cos"] = math.cos(2 * math.pi * d.weekday() / 7.0)
        return vec

    def to_vector(self, vec: Dict[str, float]) -> np.ndarray:
        """Dict -> fixed-order float array. Missing keys become 0.0."""
        return np.array([_safe(vec.get(k, 0.0)) for k in FEATURES], dtype=np.float64)


def vector_from_last_bar(o, h, l, c, v, ts: float, config=None,
                         window: int = DEFAULT_WINDOW) -> Dict[str, float]:
    """Convenience for the live path: features at the last CLOSED bar."""
    fx = FeatureExtractor(config=config, window=window)
    vec = fx.vector(np.asarray(o), np.asarray(h), np.asarray(l),
                    np.asarray(c), np.asarray(v))
    if not vec:
        return {}
    return FeatureExtractor.add_time_features(vec, ts)
