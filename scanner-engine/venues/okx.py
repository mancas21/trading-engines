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
import outcomes as OUT  # noqa: E402  shared exit-bar selection (ceil semantics)
import pairs as P  # noqa: E402  the registry owns the venue symbols

import json
import time
import urllib.request

OKX = "https://www.okx.com"


def _get(path: str, timeout: float = 15.0):
    return http_io.get(OKX + path, timeout=timeout)


def symbol_for(pair: str) -> str:
    """OKX venue symbol for a canonical pair.

    The REGISTRY decides. OKX lists PEPE as PEPE-USDT-SWAP (1x) while the
    pair the ATAS chart is on is 1000PEPEUSDT (1000x) — deriving the symbol
    from the pair name asks OKX for 1000PEPE-USDT-SWAP and gets
    `Instrument ID does not exist` (51001). The derivation below is only
    the fallback for pairs outside the registry.
    """
    try:
        s = P.venue_symbol(pair, "okx")
    except KeyError:
        s = None
    if s:
        return s
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


def fetch_trades(pair: str, limit: int = 500) -> list[dict]:
    """Recent trades, newest page the API will serve.

    The limit was 100. Measured on ETH 2026-10-11: 100 trades span 1.0s,
    which is not a flow measurement over any useful window. 500 is the API
    cap for this endpoint and spans ~5s. Going higher silently returns the
    same 500, so 500 is the whole available sample.
    """
    return parse_trades(_get(f"/api/v5/market/trades?instId={symbol_for(pair)}&limit={limit}"))


def fetch_candles(pair: str, bar: str = "1m", limit: int = 5) -> list[dict]:
    return parse_candles(
        _get(f"/api/v5/market/candles?instId={symbol_for(pair)}&bar={bar}&limit={limit}")
    )


# OKX bar strings are the same "1m"/"5m" shape; the candle's ts_ms is the OPEN
# time, so the close needs the bar's own length. (The close_ms used to be
# recorded as the open time — harmless while OKX was never a ref venue, wrong
# the moment it became one.)
OKX_BAR_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
              "30m": 1_800_000, "1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}


def fetch_closed_bar(pair: str, bar: str = "1m", at_ms: int | None = None,
                     now_ms: int | None = None):
    """The exit bar for a horizon measured from `at_ms` — see
    `outcomes.select_exit_bar` for why the selection is CEIL (first closed bar
    whose close is at or after at_ms), not floor.

    OKX candles carry an explicit trailing `confirm` flag (1 = closed), so this
    uses that rather than inferring closure from the timestamp alone.
    """
    if at_ms is None:
        at_ms = int(time.time() * 1000)
    rows = fetch_candles(pair, bar=bar, limit=50)
    step = OKX_BAR_MS.get(bar, 60_000)
    candidates = []
    for row in rows:
        if not row["closed"]:
            continue
        close_ms = int(row["ts_ms"]) + step          # ts_ms is the OPEN time
        candidates.append((close_ms, {
            "open_ms": int(row["ts_ms"]), "close_ms": close_ms,
            "close": row["close"], "volume": row["volume"],
        }))
    return OUT.select_exit_bar(candidates, at_ms, now_ms)


def fetch_open_interest(pair: str):
    raw = _get(f"/api/v5/public/open-interest?instType=SWAP&instId={symbol_for(pair)}")
    if raw.get("code") != "0":
        raise ValueError(f"okx error {raw.get('code')}: {raw.get('msg')}")
    return float(raw["data"][0]["oiUsd"])
