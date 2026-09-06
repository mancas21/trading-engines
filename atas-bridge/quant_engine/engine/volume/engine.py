"""
engine.volume — dynamic volume profile (Spec Section 3).

Extends the original static volume_profile module (POC/VAH/VAL over a
fixed window — retained, tested, reused here as `build_profile` /
`value_area`) with:
    - session / daily / weekly / anchored profile helpers (same core
      logic, different slicing of the input DataFrame — the profile
      math doesn't change, only what data you feed it)
    - developing POC (POC recalculated as of each bar, causal/no lookahead)
    - POC migration (trend of the developing POC over time)
    - HVN/LVN detection (already had LVN; HVN added)
    - acceptance/rejection: POC direction + price direction + delta sign
      combined per Section 3's "important signal"

IMPORTANT: profiles are only meaningful for genuinely bounded windows
(a session, a day). Feeding an arbitrarily long history into
build_profile will smear the distribution — always pass a `groupby`-sliced
frame from `session_profile` / `daily_profile`, not the raw multi-week CSV.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from engine.core.types import StatResult


def build_profile(df: pd.DataFrame, bins: int) -> tuple:
    """Volume-weighted price histogram. Unchanged from the original module."""
    price_min = df["low"].min()
    price_max = df["high"].max()
    if price_max == price_min:
        return np.array([price_min]), np.array([df["volume"].sum()])
    edges = np.linspace(price_min, price_max, bins + 1)
    bin_volume = np.zeros(bins)

    for _, row in df.iterrows():
        lo, hi, vol = row["low"], row["high"], row["volume"]
        lo_idx = max(int((lo - price_min) / (price_max - price_min) * bins), 0)
        hi_idx = min(int((hi - price_min) / (price_max - price_min) * bins), bins - 1)
        span = hi_idx - lo_idx + 1
        for b in range(lo_idx, hi_idx + 1):
            bin_volume[b] += vol / span

    bin_centers = (edges[:-1] + edges[1:]) / 2
    return bin_centers, bin_volume


def value_area(bin_centers: np.ndarray, bin_volume: np.ndarray, va_pct: float = 0.70) -> dict:
    poc_idx = int(np.argmax(bin_volume))
    total_vol = bin_volume.sum()
    target = total_vol * va_pct

    included = {poc_idx}
    captured = bin_volume[poc_idx]
    lo, hi = poc_idx, poc_idx

    while captured < target and (lo > 0 or hi < len(bin_volume) - 1):
        vol_below = bin_volume[lo - 1] if lo > 0 else -1
        vol_above = bin_volume[hi + 1] if hi < len(bin_volume) - 1 else -1
        if vol_above >= vol_below:
            hi += 1
            captured += bin_volume[hi]
        else:
            lo -= 1
            captured += bin_volume[lo]

    return {
        "poc_price": float(bin_centers[poc_idx]),
        "vah_price": float(bin_centers[hi]),
        "val_price": float(bin_centers[lo]),
        "value_area_volume_pct_captured": float(captured / total_vol * 100),
    }


def hvn_lvn(bin_centers: np.ndarray, bin_volume: np.ndarray, hvn_pctile: float = 80, lvn_pctile: float = 20) -> dict:
    hvn_thresh = np.percentile(bin_volume, hvn_pctile)
    lvn_thresh = np.percentile(bin_volume, lvn_pctile)
    return {
        "hvn_prices": [float(bin_centers[i]) for i in range(len(bin_volume)) if bin_volume[i] >= hvn_thresh],
        "lvn_prices": [float(bin_centers[i]) for i in range(len(bin_volume)) if bin_volume[i] <= lvn_thresh],
    }


def session_profile(df: pd.DataFrame, session_date, bins: int = 75) -> dict:
    """Profile for a single calendar day's worth of bars (proxy for 'session' if not further split by UTC hour)."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    day_df = df[df["date"] == session_date]
    if day_df.empty:
        return {}
    centers, vols = build_profile(day_df, bins)
    return value_area(centers, vols)


def daily_profile(df: pd.DataFrame, bins: int = 75) -> dict:
    """Profile per calendar day across the whole sample. Returns {date: value_area_dict}."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    out = {}
    for date, group in df.groupby("date"):
        centers, vols = build_profile(group, bins)
        out[str(date)] = value_area(centers, vols)
    return out


def anchored_profile(df: pd.DataFrame, anchor_timestamp: str, bins: int = 75) -> dict:
    """Profile from an anchor point (e.g. a major swing low/high, a news event) to the latest bar."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    anchored = df[df["timestamp"] >= pd.to_datetime(anchor_timestamp)]
    if anchored.empty:
        return {}
    centers, vols = build_profile(anchored, bins)
    return value_area(centers, vols)


def developing_poc(df: pd.DataFrame, bins: int = 75, min_bars: int = 20) -> pd.Series:
    """
    Causal (no-lookahead) developing POC: recomputed using only bars up to
    and including each point in time. Expensive (recomputes the profile
    each step) — intended for end-of-day/session analysis, not a tight
    real-time loop. For live use, maintain the profile incrementally
    instead of recalling this per bar.
    """
    poc_series = []
    for i in range(len(df)):
        if i < min_bars:
            poc_series.append(np.nan)
            continue
        window = df.iloc[: i + 1]
        centers, vols = build_profile(window, bins)
        poc_series.append(float(centers[np.argmax(vols)]))
    return pd.Series(poc_series, index=df.index)


def poc_migration(poc_series: pd.Series, window: int = 20) -> StatResult:
    """
    Slope of the developing POC over the last `window` bars, normalized
    by price — positive = POC migrating higher (bullish acceptance
    building), negative = migrating lower.
    """
    valid = poc_series.dropna()
    if len(valid) < window + 1:
        return StatResult(value=float("nan"), n=len(valid), lookback=f"{window} bars")
    recent = valid.tail(window)
    slope = float((recent.iloc[-1] - recent.iloc[0]) / recent.iloc[0])
    return StatResult(value=slope, n=window, lookback=f"{window} bars", method="poc_slope_pct")


def acceptance_rejection(price_series: pd.Series, poc_series: pd.Series, delta_series: pd.Series | None,
                          window: int = 20) -> str:
    """
    Implements Section 3's "important signal":
        POC rising + price rising + positive delta  = bullish acceptance
        POC falling + price falling + negative delta = bearish acceptance
    Falls back to price/POC only if delta_series is unavailable.
    """
    price_slope = price_series.tail(window).iloc[-1] - price_series.tail(window).iloc[0]
    poc_valid = poc_series.dropna().tail(window)
    if len(poc_valid) < 2:
        return "insufficient_data"
    poc_slope = poc_valid.iloc[-1] - poc_valid.iloc[0]

    delta_sign = None
    if delta_series is not None and len(delta_series) >= window:
        delta_sign = "positive" if delta_series.tail(window).sum() > 0 else "negative"

    if price_slope > 0 and poc_slope > 0:
        if delta_sign in (None, "positive"):
            return "bullish_acceptance"
        return "bullish_price_action_unconfirmed_by_delta"
    if price_slope < 0 and poc_slope < 0:
        if delta_sign in (None, "negative"):
            return "bearish_acceptance"
        return "bearish_price_action_unconfirmed_by_delta"
    return "rejection_or_rotation"  # price/POC disagree — value area rejection, not acceptance


# --- CLI entry point -------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--bins", type=int, default=75)
    ap.add_argument("--mode", choices=["static", "developing"], default="static")
    args = ap.parse_args()

    df = pd.read_csv(args.csv)

    if args.mode == "static":
        centers, vols = build_profile(df, args.bins)
        va = value_area(centers, vols, 0.70)
        hl = hvn_lvn(centers, vols)
        current_price = float(df["close"].iloc[-1])
        output = {**va, **hl, "current_price": current_price}
    else:
        poc_series = developing_poc(df, args.bins)
        migration = poc_migration(poc_series)
        delta = None
        if "taker_buy_volume" in df.columns:
            delta = df["taker_buy_volume"] - (df["volume"] - df["taker_buy_volume"])
        signal = acceptance_rejection(df["close"], poc_series, delta)
        output = {
            "poc_migration": migration.to_dict(),
            "acceptance_rejection_signal": signal,
            "latest_developing_poc": float(poc_series.dropna().iloc[-1]) if poc_series.dropna().size else None,
        }

    print(json.dumps(output, indent=2))
