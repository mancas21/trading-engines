import asyncio
import json
import os
import time
import sys
from threading import Lock
from urllib.request import urlopen

import numpy as np
import pandas as pd

# vendor the engine subpackage (engine.volume / engine.volatility / engine.core)
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "quant_engine"))

from engine.volume.engine import build_profile, value_area, hvn_lvn
from engine.volatility.engine import (
    atr_percentile_rank,
    volatility_expansion_rate,
    volatility_clustering,
    expected_move_distribution,
)

# --- config ---------------------------------------------------------------
KLINES_URL = (
    "https://fapi.binance.com/fapi/v1/klines"
    "?symbol={symbol}&interval={interval}&limit={limit}"
)
CONTEXT_TTL = float(os.getenv("CONTEXT_TTL_SECONDS", "60"))
PROFILE_DAYS = int(os.getenv("CONTEXT_PROFILE_DAYS", "3"))
HORIZON_BARS = int(os.getenv("CONTEXT_HORIZON_BARS", "16"))
PROFILE_BINS = int(os.getenv("CONTEXT_BINS", "75"))
KLINES_LIMIT = int(os.getenv("CONTEXT_KLINES_LIMIT", "1000"))

_context_cache: dict = {}          # symbol -> {"ts": float, "data": dict}
_context_lock = Lock()


# --- data fetch -----------------------------------------------------------
def _fetch_klines(symbol: str, interval: str, limit: int):
    """Raw Binance Futures klines (blocking — run in executor)."""
    url = KLINES_URL.format(symbol=symbol, interval=interval, limit=limit)
    data = None
    for attempt in range(2):
        try:
            data = json.loads(urlopen(url, timeout=10).read())
            break
        except Exception:
            if attempt == 0:
                time.sleep(0.5)
    if data is None:
        return None
    rows = []
    for k in data:
        rows.append(
            [int(k[0]), float(k[1]), float(k[2]), float(k[3]),
             float(k[4]), float(k[5])]
        )
    return rows


def _build_frame(klines) -> pd.DataFrame:
    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    df = pd.DataFrame(klines, columns=cols)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    d = df.set_index("timestamp").sort_index()
    out = d.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}
    ).dropna()
    return out.reset_index()


# --- computation ----------------------------------------------------------
def _safe(label: str, fn, *args, **kwargs):
    """Run an engine function, returning its .to_dict() or an error stub."""
    try:
        res = fn(*args, **kwargs)
        d = res.to_dict() if hasattr(res, "to_dict") else dict(res)
        d["_ok"] = True
        return d
    except Exception as e:
        return {"_ok": False, "_error": f"{label}: {type(e).__name__}: {e}"}


def compute_context(symbol: str) -> dict:
    """Run the volume + volatility engine over Binance klines for one symbol."""
    sym = symbol.upper().strip("/")
    klines = _fetch_klines(sym, "15m", KLINES_LIMIT)
    if not klines:
        return {"symbol": sym, "error": "no_klines"}

    df_15m = _build_frame(klines)
    if df_15m.empty:
        return {"symbol": sym, "error": "empty_frame"}

    last_price = float(df_15m["close"].iloc[-1])

    # Bounded profile window (last N days) — avoids multi-week smear.
    cutoff = df_15m["timestamp"].max() - pd.Timedelta(days=PROFILE_DAYS)
    profile_df = df_15m[df_15m["timestamp"] >= cutoff]
    if profile_df.empty:
        profile_df = df_15m

    centers, vols = build_profile(profile_df, PROFILE_BINS)
    va = value_area(centers, vols)
    hl = hvn_lvn(centers, vols)

    poc, vah, val = va["poc_price"], va["vah_price"], va["val_price"]
    if last_price > vah:
        location = "above_value"
    elif last_price < val:
        location = "below_value"
    else:
        location = "in_value"

    df_1h = _resample(df_15m, "1h")
    df_4h = _resample(df_15m, "4h")

    return {
        "symbol": sym,
        "last_price": last_price,
        "value_area": {
            "poc": round(poc, 6),
            "vah": round(vah, 6),
            "val": round(val, 6),
            "location": location,
            "profile_window_bars": int(len(profile_df)),
            "profile_window_days": PROFILE_DAYS,
            "hvn_prices": [round(p, 6) for p in hl["hvn_prices"]],
            "lvn_prices": [round(p, 6) for p in hl["lvn_prices"]],
        },
        "volatility_regime": {
            "atr_percentile_rank": _safe("atr_percentile_rank",
                                        atr_percentile_rank, df_1h),
            "expansion_rate": _safe("expansion_rate",
                                    volatility_expansion_rate, df_1h),
            "clustering": _safe("clustering",
                                volatility_clustering, df_1h),
        },
        "expected_move": _safe("expected_move",
                               expected_move_distribution,
                               df_15m, HORIZON_BARS),
        "meta": {
            "klines_15m": int(len(df_15m)),
            "horizon_bars": HORIZON_BARS,
            "computed_at": time.time(),
        },
        "_disclaimer": (
            "CONTEXT only — value area / vol regime / expected move. "
            "Not a trade signal."
        ),
    }


def get_context(symbol: str) -> dict:
    """Cached accessor (TTL). Computed synchronously; call from an executor.
    On live-fetch failure, falls back to the last-good cached context marked
    with meta.stale — so a Binance blip never hard-503s the endpoint."""
    sym = symbol.upper().strip("/")
    with _context_lock:
        hit = _context_cache.get(sym)
    if hit and (time.time() - hit["ts"]) < CONTEXT_TTL:
        return hit["data"]
    data = compute_context(sym)
    if "error" in data:
        if hit and "error" not in hit["data"]:
            stale = dict(hit["data"])
            stale["meta"] = dict(stale.get("meta", {}))
            stale["meta"]["stale"] = True
            stale["meta"]["stale_error"] = data["error"]
            stale["meta"]["stale_since_ts"] = hit["ts"]
            with _context_lock:
                _context_cache[sym] = {"ts": time.time(), "data": stale}
            return stale
        return data
    with _context_lock:
        _context_cache[sym] = {"ts": time.time(), "data": data}
    return data


async def get_context_async(symbol: str, loop=None) -> dict:
    """Async wrapper — runs the blocking fetch+compute in an executor."""
    loop = loop or asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_context, symbol)


def reset_context(symbol: str = None) -> dict:
    """Clear the cache (one symbol, or all if None)."""
    with _context_lock:
        if symbol:
            _context_cache.pop(symbol.upper().strip("/"), None)
        else:
            _context_cache.clear()
    return {"status": "ok"}


# --- CLI ------------------------------------------------------------------
if __name__ == "__main__":
    sym = (sys.argv[1] if len(sys.argv) > 1 else "SOLUSDT").upper()
    print(json.dumps(compute_context(sym), indent=2, default=str))