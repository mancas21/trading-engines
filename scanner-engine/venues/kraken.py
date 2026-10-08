"""Kraken adapter — the one venue with two traps, both verified 2026-10-08.

TRAP 1 — the pair key is renamed, unpredictably
===============================================
Kraken returns the pair under ITS OWN key, and the mangling has no rule:

    requested   returned
    XBTUSD   -> XXBTZUSD
    XRPUSD   -> XXRPZUSD
    ETHUSD   -> XETHZUSD
    SUIUSD   -> SUIUSD      (unmangled)
    SOLUSD   -> SOLUSD      (unmangled)
    HYPEUSD  -> HYPEUSD     (unmangled)
    XAUTUSDT -> XAUTUSDT    (unmangled)

A hardcoded map would be wrong for at least one pair. `resolve_key` scans
`result` for the single key that is not `last`, and returns None when it is
ambiguous — a guessed pair produces confident wrong numbers, which is worse
than no reading at all.

TRAP 2 — trade order is NOT newest-first
========================================
`/public/Trades` returns trades in ASCENDING time order. Reading index 0 as
"the newest" makes every freshness check report a stale stream.

This was not theoretical: an early probe of this engine did exactly that and
concluded Kraken was 872s to 27 hours stale. Re-reading with max(ts) showed
the real age was 2-6 seconds. The freshness helper below exists so that
mistake cannot recur silently.

Flow: `/public/Trades` index 3 is the taker side ('b' = buy, 's' = sell).
`/public/OHLC` carries TOTAL volume only — no taker side — so per-bar CVD is
not derivable on Kraken.
"""
from __future__ import annotations

import json
import urllib.request

KRAKEN = "https://api.kraken.com"


def _get(path: str, timeout: float = 20.0):
    req = urllib.request.Request(KRAKEN + path, headers={"User-Agent": "scanner-engine/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def symbol_for(pair: str) -> str:
    """Kraken venue symbol for a canonical pair.

    Two non-obvious mappings, both verified live:
      BTC -> XBT   Kraken uses XBT, not BTC.
      XAU -> XAUT  Gold is XAUTUSD / XAUTUSDT. Plain XAUUSD returns
                   EQuery:Unknown asset pair.
    """
    base, _, _ = pair.partition("/")
    if base == "BTC":
        base = "XBT"
    elif base == "XAU":
        base = "XAUT"
    return base + "USD"


def resolve_key(result: dict) -> str | None:
    """Find Kraken's response key for the pair. Never assume it.

    Returns None when there is not exactly one candidate, because a wrong
    guess is silently plausible and that is the dangerous failure mode.
    """
    candidates = [k for k in result if k != "last"]
    if len(candidates) == 1:
        return candidates[0]
    return None


def parse_trades(raw: dict) -> list[dict] | None:
    """Normalize /public/Trades to the common trade shape.

    Returns None if the key cannot be resolved unambiguously.
    """
    if raw.get("error"):
        raise ValueError(f"kraken error: {raw['error']}")

    key = resolve_key(raw.get("result", {}))
    if key is None:
        return None

    out = []
    for t in raw["result"][key]:
        # layout: [price, volume, time, side, orderType, misc, tradeId]
        out.append({
            "ts": float(t[2]),
            "price": float(t[0]),
            "size": float(t[1]),
            "taker_side": "buy" if t[3] == "b" else "sell",
        })
    return out


def newest_trade_age_s(trades: list[dict], now: float) -> float:
    """Age of the MOST RECENT trade, using max() over timestamps.

    Do not use trades[0] — Kraken returns ascending order (Trap 2).
    """
    if not trades:
        return float("inf")
    return now - max(t["ts"] for t in trades)


def parse_depth(raw: dict):
    """-> (Book, key_used), or (None, None) if the key is ambiguous."""
    if raw.get("error"):
        raise ValueError(f"kraken error: {raw['error']}")

    key = resolve_key(raw.get("result", {}))
    if key is None:
        return None, None

    from types_ import Book

    d = raw["result"][key]
    bids = [(float(p), float(v)) for p, v, *_ in d["bids"]]
    asks = [(float(p), float(v)) for p, v, *_ in d["asks"]]
    if not bids or not asks:
        raise ValueError(f"kraken one-sided book for key {key}")
    return Book(bid=bids[0][0], bids=bids, asks=asks), key


# ── network fetchers ──────────────────────────────────────────────────────

def fetch_depth(pair: str, count: int = 20):
    book, key = parse_depth(_get(f"/0/public/Depth?pair={symbol_for(pair)}&count={count}"))
    if book is None:
        raise ValueError(f"kraken: could not resolve pair key for {pair}")
    return book


def fetch_trades(pair: str) -> list[dict]:
    trades = parse_trades(_get(f"/0/public/Trades?pair={symbol_for(pair)}"))
    if trades is None:
        raise ValueError(f"kraken: could not resolve pair key for {pair}")
    return trades


def fetch_ohlc(pair: str, interval: int = 1) -> list[dict]:
    """OHLC with TOTAL volume only. Cannot derive CVD from this."""
    raw = _get(f"/0/public/OHLC?pair={symbol_for(pair)}&interval={interval}")
    key = resolve_key(raw.get("result", {}))
    if key is None:
        raise ValueError(f"kraken: could not resolve pair key for {pair}")

    out = []
    for c in raw["result"][key]:
        out.append({
            "ts": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[6]),   # total base volume, no taker side
            "trades": int(c[7]),
        })
    return out
