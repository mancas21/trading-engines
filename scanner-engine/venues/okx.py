"""OKX v5 adapter.

Two verified gotchas (both measured 2026-10-08):

1. `GET /api/v5/market/tickers` (PLURAL) returns HTTP 400 with
   `{"code":"50014","msg":"Parameter instType can not be empty."}`.
   The singular `/market/ticker` works and returns bidPx/askPx WITH sizes,
   which is exactly what touch imbalance needs.

2. `GET /api/v5/market/candles` has 9 fields and carries NO taker-buy
   volume. Per-bar CVD is impossible here; flow must come from per-trade
   side via `/market/trades`, which does carry `side` and a `ts`.

The candles response DOES carry a `confirm` flag as its last element
(0 = in-progress, 1 = closed), used to keep partial bars out of the math.
"""
from __future__ import annotations

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import http_io  # noqa: E402  shared transport (bypasses the local TLS-breaking proxy)

import json
import time
import urllib.request

OKX = "https://www.okx.com"


def _get(path: str, timeout: float = 15.0):
    return http_io.get(OKX + path, timeout=timeout)


def symbol_for(pair: str) -> str:
    """'BTC/USD' -> 'BTC-USDT-SWAP'. XAU maps to its own listing."""
    base, _, _ = pair.partition("/")
    return f"{base}-USDT-SWAP"


def parse_trades(raw: dict) -> list[dict]:
    """Normalize /market/trades to the common trade shape.

    Raises on a non-zero `code` instead of returning an empty list: an
    error response with `data: []` is indistinguishable from "no trades"
    if you only look at the data.
    """
    if raw.get("code") != "0":
        raise ValueError(f"okx error {raw.get('code')}: {raw.get('msg')}")

    out = []
    for t in raw.get("data", []):
        out.append({
            "ts": int(t["ts"]) / 1000.0,
            "price": float(t["px"]),
            "size": float(t["sz"]),
            "taker_side": t["side"].lower(),
        })
    return out


def parse_ticker(raw: dict):
    """Singular /market/ticker -> Book. Raises on error code."""
    if raw.get("code") != "0":
        raise ValueError(f"okx error {raw.get('code')}: {raw.get('msg')}")
    d = raw["data"][0]
    from types_ import Book

    bid, ask = float(d["bidPx"]), float(d["askPx"])
    bid_sz, ask_sz = float(d["bidSz"]), float(d["askSz"])
    if bid <= 0 or ask <= 0:
        raise ValueError(f"okx non-positive quote for {d.get('instId')}: {bid}/{ask}")

    return Book(bid=bid, bids=[(bid, bid_sz)], asks=[(ask, ask_sz)])


def parse_candles(raw: dict) -> list[dict]:
    """Candles carry a trailing confirm flag; keep it.

    Layout: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    """
    if raw.get("code") != "0":
        raise ValueError(f"okx error {raw.get('code')}: {raw.get('msg')}")
    out = []
    for row in raw.get("data", []):
        out.append({
            "ts_ms": int(row[0]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "closed": str(row[-1]) == "1",
        })
    return out


# ── network fetchers ──────────────────────────────────────────────────────

def fetch_depth(pair: str):
    raw = _get(f"/api/v5/market/books?instId={symbol_for(pair)}&sz=20")
    if raw.get("code") != "0":
        raise ValueError(f"okx error {raw.get('code')}: {raw.get('msg')}")
    from types_ import Book

    b = raw["data"][0]
    bids = [(float(p), float(q)) for p, q, *_ in b["bids"]]
    asks = [(float(p), float(q)) for p, q, *_ in b["asks"]]
    if not bids or not asks:
        raise ValueError(f"okx one-sided book for {pair}")
    return Book(bid=bids[0][0], bids=bids, asks=asks)


def fetch_ticker_book(pair: str):
    """Singular ticker — the fallback that avoids the 400 on plural."""
    return parse_ticker(_get(f"/api/v5/market/ticker?instId={symbol_for(pair)}"))


def fetch_trades(pair: str, limit: int = 100) -> list[dict]:
    return parse_trades(_get(f"/api/v5/market/trades?instId={symbol_for(pair)}&limit={limit}"))


def fetch_candles(pair: str, bar: str = "1m", limit: int = 5) -> list[dict]:
    return parse_candles(
        _get(f"/api/v5/market/candles?instId={symbol_for(pair)}&bar={bar}&limit={limit}")
    )


def fetch_closed_bar(pair: str, bar: str = "1m", at_ms: int | None = None):
    """The closed bar covering `at_ms`.

    OKX candles carry an explicit trailing `confirm` flag (1 = closed), so this
    uses that rather than inferring closure from the timestamp alone.
    """
    if at_ms is None:
        at_ms = int(time.time() * 1000)
    rows = fetch_candles(pair, bar=bar, limit=50)
    best = None
    for row in rows:
        if not row["closed"]:
            continue
        if row["ts_ms"] <= at_ms:
            best = row
    if best is None:
        return None
    return {"open_ms": best["ts_ms"], "close_ms": best["ts_ms"],
            "close": best["close"], "volume": best["volume"]}


def fetch_open_interest(pair: str):
    raw = _get(f"/api/v5/public/open-interest?instType=SWAP&instId={symbol_for(pair)}")
    if raw.get("code") != "0":
        raise ValueError(f"okx error {raw.get('code')}: {raw.get('msg')}")
    return float(raw["data"][0]["oiUsd"])
