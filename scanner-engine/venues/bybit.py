"""Bybit v5 adapter (category=linear, public).

Verified gotchas (2026-10-08):
  * orderbook `limit` must be one of {1, 50, 200, 500}; arbitrary values are
    rejected.
  * recent-trade carries an explicit `side` and millisecond `time`, both
    ideal for window CVD.
  * All errors come back with HTTP 200 and `retCode != 0`, so the retCode
    must be checked — a non-zero retCode with `result.list == []` looks
    exactly like "no trades" otherwise.
"""
from __future__ import annotations

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import http_io  # noqa: E402  shared transport (bypasses the local TLS-breaking proxy)

import json
import urllib.request

BYBIT = "https://api.bybit.com"

# Only these orderbook limits are accepted.
VALID_BOOK_LIMITS = {1, 50, 200, 500}


def _get(path: str, timeout: float = 15.0):
    return http_io.get(BYBIT + path, timeout=timeout)


def symbol_for(pair: str) -> str:
    """'BTC/USD' -> 'BTCUSDT'. Bybit lists gold as XAUUSDT."""
    base, _, _ = pair.partition("/")
    return base + "USDT"


def _check(raw: dict) -> dict:
    if raw.get("retCode") != 0:
        raise ValueError(f"bybit error {raw.get('retCode')}: {raw.get('retMsg')}")
    return raw


def parse_trades(raw: dict) -> list[dict]:
    """Normalize /v5/market/recent-trade to the common trade shape."""
    _check(raw)
    out = []
    for t in raw["result"]["list"]:
        out.append({
            "ts": int(t["time"]) / 1000.0,
            "price": float(t["price"]),
            "size": float(t["size"]),
            "taker_side": t["side"].lower(),
        })
    return out


def parse_orderbook(raw: dict):
    """-> Book. Bybit returns bids under 'b' and asks under 'a'."""
    _check(raw)
    from types_ import Book

    r = raw["result"]
    bids = [(float(p), float(q)) for p, q in r["b"]]
    asks = [(float(p), float(q)) for p, q in r["a"]]
    if not bids or not asks:
        raise ValueError("bybit one-sided book")
    return Book(bid=bids[0][0], bids=bids, asks=asks)


# ── network fetchers ──────────────────────────────────────────────────────

def fetch_depth(pair: str, limit: int = 50):
    if limit not in VALID_BOOK_LIMITS:
        raise ValueError(f"limit {limit} not in {sorted(VALID_BOOK_LIMITS)}")
    return parse_orderbook(
        _get(f"/v5/market/orderbook?category=linear&symbol={symbol_for(pair)}&limit={limit}")
    )


def fetch_trades(pair: str, limit: int = 60) -> list[dict]:
    return parse_trades(
        _get(f"/v5/market/recent-trade?category=linear&symbol={symbol_for(pair)}&limit={limit}")
    )


def fetch_klines(pair: str, interval: str = "1", limit: int = 5):
    """Bybit uses '1'/'5'/'15' etc. for minute intervals."""
    return _check(
        _get(f"/v5/market/kline?category=linear&symbol={symbol_for(pair)}"
             f"&interval={interval}&limit={limit}")
    )["result"]["list"]


def fetch_open_interest(pair: str, interval: str = "5min"):
    raw = _check(
        _get(f"/v5/market/open-interest?category=linear&symbol={symbol_for(pair)}"
             f"&intervalTime={interval}&limit=2")
    )
    return float(raw["result"]["list"][0]["openInterest"])


BYBIT_INTERVAL = {"1m": "1", "3m": "3", "5m": "5", "15m": "15",
                  "30m": "30", "1h": "60", "4h": "240", "1d": "D"}


def fetch_closed_bar(pair: str, bar: str = "1m", at_ms: int | None = None):
    """The closed bar covering `at_ms`.

    Bybit's kline endpoint uses numeric interval codes ("1", "5"), not "1m".
    """
    if at_ms is None:
        at_ms = int(time.time() * 1000)
    code = BYBIT_INTERVAL.get(bar, "1")
    raw = _get(f"/v5/market/kline?category=linear&symbol={symbol_for(pair)}"
               f"&interval={code}&limit=50")
    if raw.get("retCode") != 0:
        raise ValueError(f"bybit error {raw.get('retCode')}: {raw.get('retMsg')}")
    best = None
    for row in raw["result"]["list"]:          # newest first
        start_ms = int(row[0])
        end_ms = start_ms + BAR_MS[code]
        if end_ms <= at_ms:
            if best is None or start_ms > best[0]:
                best = (start_ms, end_ms, row)
    if best is None:
        return None
    start_ms, end_ms, row = best
    return {"open_ms": start_ms, "close_ms": end_ms,
            "close": float(row[4]), "volume": float(row[5])}


BAR_MS = {"1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000,
          "30": 1_800_000, "60": 3_600_000, "240": 14_400_000, "D": 86_400_000}
