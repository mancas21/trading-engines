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

import json
import urllib.request

BYBIT = "https://api.bybit.com"

# Only these orderbook limits are accepted.
VALID_BOOK_LIMITS = {1, 50, 200, 500}


def _get(path: str, timeout: float = 15.0):
    req = urllib.request.Request(BYBIT + path, headers={"User-Agent": "scanner-engine/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


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
