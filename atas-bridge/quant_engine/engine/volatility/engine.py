"""
engine.volatility — advanced volatility engine (Spec Section 2).

Extends the original volatility_regime module (ATR, ATR percentile,
realized vol, Parkinson estimator, term structure — all retained and
tested) with:
    - volatility expansion/contraction rate
    - volatility clustering (autocorrelation of squared returns)
    - intraday volatility curve (which hours are typically most active)
    - expected-move distributions (StatResult-wrapped, with N)

Every expected-move number here is a StatResult, not a bare float — the
spec (Section 2) shows an "expected move" as a single number, but per
Section 12 every statistic in the engine must carry its N and confidence,
so those numbers are the .value of a StatResult here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from engine.core.types import StatResult


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR (RMA smoothing of true range) — audit M2: replaced the SMA
    so ATR matches TradingView/industry standard (the quant engine repo already
    uses Wilder RMA; the bridge should not read regimes off a different ATR)."""
    return true_range(df).ewm(alpha=1.0 / period, adjust=False).mean()


def realized_vol(df: pd.DataFrame, window: int, periods_per_year: float) -> pd.Series:
    log_ret = np.log(df["close"] / df["close"].shift(1))
    return log_ret.rolling(window).std() * np.sqrt(periods_per_year)


def parkinson_vol(df: pd.DataFrame, window: int, periods_per_year: float) -> pd.Series:
    hl_ratio = np.log(df["high"] / df["low"]) ** 2
    factor = 1 / (4 * np.log(2))
    return np.sqrt(factor * hl_ratio.rolling(window).mean()) * np.sqrt(periods_per_year)


def infer_periods_per_year(df: pd.DataFrame) -> float:
    ts = pd.to_datetime(df["timestamp"])
    diffs = ts.diff().dropna()
    if len(diffs) == 0:
        return 365 * 24 * 4
    median_seconds = diffs.dt.total_seconds().median()
    return (365 * 24 * 3600) / median_seconds


def atr_percentile_rank(df: pd.DataFrame, period: int = 14, lookback: int = 90) -> StatResult:
    atr_series = atr(df, period).dropna()
    if atr_series.empty:
        return StatResult(value=float("nan"), n=0, lookback=f"{lookback} bars")
    current = atr_series.iloc[-1]
    window = atr_series.tail(lookback)
    rank = float((window < current).mean() * 100)
    return StatResult(value=rank, n=len(window), lookback=f"{lookback} bars", method="empirical_rank")


def volatility_expansion_rate(df: pd.DataFrame, period: int = 14, window: int = 20) -> StatResult:
    """
    Rate of change of ATR over `window` bars, normalized by its own level —
    positive = volatility expanding, negative = contracting.
    """
    atr_series = atr(df, period).dropna()
    if len(atr_series) < window + 1:
        return StatResult(value=float("nan"), n=len(atr_series), lookback=f"{window} bars")
    rate = float((atr_series.iloc[-1] - atr_series.iloc[-window]) / atr_series.iloc[-window])
    return StatResult(value=rate, n=window, lookback=f"{window} bars", method="pct_change")


def volatility_clustering(df: pd.DataFrame, lags: int = 10) -> StatResult:
    """
    Autocorrelation of squared returns at lag 1 — the standard signature
    of volatility clustering (GARCH-type behavior: large moves follow
    large moves). Value near 0 = no clustering, higher positive value =
    stronger clustering (volatile periods tend to persist).
    """
    log_ret = np.log(df["close"] / df["close"].shift(1)).dropna()
    sq_ret = log_ret ** 2
    if len(sq_ret) < lags + 2:
        return StatResult(value=float("nan"), n=len(sq_ret), lookback="full sample")
    autocorr = float(sq_ret.autocorr(lag=1))
    return StatResult(value=autocorr, n=len(sq_ret), lookback="full sample", method="autocorr_lag1_sq_returns")


def intraday_volatility_curve(df: pd.DataFrame, period: int = 14) -> dict:
    """
    Mean ATR-as-pct-of-price grouped by UTC hour — shows which hours are
    typically most/least active. Returns {hour: StatResult} for hours 0-23.
    """
    df = df.copy()
    df["atr_pct"] = atr(df, period) / df["close"] * 100
    df["hour"] = pd.to_datetime(df["timestamp"], utc=True).dt.hour

    out = {}
    for hour, group in df.groupby("hour"):
        vals = group["atr_pct"].dropna()
        if vals.empty:
            continue
        out[int(hour)] = StatResult(
            value=float(vals.mean()), n=len(vals), lookback="full sample",
            method="mean_atr_pct_by_hour",
        )
    return out


def expected_move_distribution(df: pd.DataFrame, horizon_bars: int, periods_per_year: float | None = None) -> StatResult:
    """
    Empirical distribution of |return| over a forward horizon of
    `horizon_bars`, used to produce "expected move" and "90% expected
    range" style outputs (Spec Section 2 example).

    This is empirical (resampled historical forward moves), not a
    parametric normal-distribution estimate — crypto returns are fat
    tailed, so empirical quantiles are more honest than +/-1.645*sigma.

    Value = median expected move (%); p90 stored in StatResult.p90 (audit M4:
    the old code stuffed (median, p90) into confidence_interval, mislabeling a
    percentile pair as a confidence interval). p90 is what a T2/sizing
    multiplier should anchor to, never a statistical CI.
    """
    close = df["close"].to_numpy()
    n = len(close)
    if n <= horizon_bars:
        return StatResult(value=float("nan"), n=0, lookback=f"{horizon_bars}-bar horizon")

    fwd_abs_ret = np.abs(close[horizon_bars:] - close[:-horizon_bars]) / close[:-horizon_bars] * 100
    median_move = float(np.median(fwd_abs_ret))
    p90 = float(np.percentile(fwd_abs_ret, 90))

    result = StatResult(
        value=median_move, n=len(fwd_abs_ret), lookback=f"{horizon_bars}-bar forward horizon",
        method="empirical_forward_abs_return",
    )
    result.p90 = p90
    return result


# --- CLI entry point -------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--atr-period", type=int, default=14)
    ap.add_argument("--lookback", type=int, default=90)
    ap.add_argument("--horizon-bars", type=int, default=4, help="Bars ahead for expected-move distribution")
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    ppy = infer_periods_per_year(df)

    output = {
        "atr_percentile_rank": atr_percentile_rank(df, args.atr_period, args.lookback).to_dict(),
        "volatility_expansion_rate": volatility_expansion_rate(df, args.atr_period).to_dict(),
        "volatility_clustering": volatility_clustering(df).to_dict(),
        "expected_move": expected_move_distribution(df, args.horizon_bars, ppy).to_dict(),
        "realized_vol_annualized_latest": float(realized_vol(df, 96, ppy).iloc[-1]),
        "parkinson_vol_annualized_latest": float(parkinson_vol(df, 96, ppy).iloc[-1]),
    }
    print(json.dumps(output, indent=2))
