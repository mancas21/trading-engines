"""Binance USDT-M adapter — depth, trades, and per-bar CVD.

Public data only. No API key, no signing.

CVD SOURCES (both verified live 2026-10-08)
==========================================
Per-bar, from klines:
    idx 5 = base volume, idx 9 = takerBuyBaseVolume.
    delta = takerBuy - (volume - takerBuy).
    This is the ONLY venue source of true per-bar CVD in this engine.

Per-trade, from aggTrades:
    The `m` field means "the BUYER is the maker". So m=True -> the aggressor
    SOLD. This inversion is the difference between a correct CVD and a
    perfectly inverted one, so it is pinned by a test.

The `futures/data/takerlongshortRatio` endpoint is deliberately NOT used:
it has no 1m period (returns []), and this engine runs at 1m/5m.
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

from flow import BarCVD

FAPI = "https://fapi.binance.com"


def _get(path: str, timeout: float = 15.0):
    return http_io.get(FAPI + path, timeout=timeout)


# ── symbol mapping ─────────────────────────────────────────────────────────

# Contract types Binance actually trades against the futures API.
#
# TRADIFI_PERPETUAL matters and was a real bug here. SPCXUSDT and NVDAUSDT
# (tokenized equities) are listed under contractType TRADIFI_PERPETUAL, not
# PERPETUAL. Filtering exchangeInfo on contractType == "PERPETUAL" therefore
# reports them as unlisted when they are live and tradeable — which is how a
# false "SPCX/NVDA available on only 2 of 4 venues" table was produced for
# this engine. Verified 2026-10-08 against the live API: depth, aggTrades,
# klines and openInterest all return data for both symbols.
TRADABLE_CONTRACT_TYPES = frozenset({"PERPETUAL", "TRADIFI_PERPETUAL"})


def is_tradable_contract_type(contract_type: str) -> bool:
    """True if Binance will serve this contractType on the futures API."""
    return contract_type in TRADABLE_CONTRACT_TYPES


def symbol_for(pair: str) -> str:
    """Binance venue symbol for a canonical pair.

    The REGISTRY decides. Deriving the symbol from the pair name is a
    guess, and for a pair whose contract is a multiplier of the underlying
    (1000PEPE -> 1000PEPEUSDT) the guess and the listing can disagree in
    either direction. The derivation below stays only as the fallback for
    pairs that are not in the registry.

    XAU -> XAUT: Binance lists gold as XAUTUSDT. Verified live 2026-10-08
    (underlyingSubType includes 'RWA').
    """
    try:
        s = P.venue_symbol(pair, "binance")
    except KeyError:
        s = None
    if s:
        return s
    base, _, _ = pair.partition("/")
    if base == "XAU":
        base = "XAUT"
    return base + "USDT"


# ── pure parsers (no network — these are what the tests exercise) ─────────

def parse_depth(raw: dict):
    """Raw /fapi/v1/depth payload -> Book. Raises on a one-sided book."""
    from types_ import Book

    bids = [(float(p), float(q)) for p, q in raw["bids"]]
    asks = [(float(p), float(q)) for p, q in raw["asks"]]
    if not bids or not asks:
        raise ValueError(f"one-sided book: bids={len(bids)} asks={len(asks)}")
    return Book(bid=bids[0][0], bids=bids, asks=asks)


def parse_aggtrades(raw: list) -> list[dict]:
    """Normalize Binance aggTrades to the common trade shape.

    Binance trades arrive newest-first. Timestamps are milliseconds.
    `m=True` means the buyer was the maker, i.e. the aggressor sold.
    """
    out = []
    for t in raw:
        out.append({
            "ts": int(t["T"]) / 1000.0,
            "price": float(t["p"]),
            "size": float(t["q"]),
            # m=True -> buyer is maker -> the aggressor took the other side
            "taker_side": "sell" if t["m"] else "buy",
        })
    return out


def cvd_from_klines(row: list, now_ms: int | None = None) -> BarCVD:
    """Derive one bar of CVD from a Binance kline row.

    Row layout (verified): [0]=openTime [5]=volume [6]=closeTime
                           [9]=takerBuyBaseVolume
    `closed` is derived from closeTime vs now, because an in-progress bar
    has partial volume and would understate flow.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    total = float(row[5])
    buy = float(row[9])
    sell = total - buy
    open_ms = int(row[0])
    close_ms = int(row[6])

    return BarCVD(
        buy=buy,
        sell=sell,
        delta=buy - sell,
        closed=close_ms <= now_ms,
        open_ms=open_ms,
        close_ms=close_ms,
    )


# ── network fetchers ──────────────────────────────────────────────────────

def fetch_depth(pair: str, limit: int = 20):
    """Return (bid, ask, bids, asks) from the live order book."""
    from types_ import Book

    raw = _get(f"/fapi/v1/depth?symbol={symbol_for(pair)}&limit={limit}")
    bids = [(float(p), float(q)) for p, q in raw["bids"]]
    asks = [(float(p), float(q)) for p, q in raw["asks"]]
    if not bids or not asks:
        raise ValueError(f"one-sided book for {pair}: bids={len(bids)} asks={len(asks)}")
    return Book(bid=bids[0][0], bids=bids, asks=asks)


def fetch_trades(pair: str, limit: int = 1000) -> list[dict]:
    return parse_aggtrades(_get(f"/fapi/v1/aggTrades?symbol={symbol_for(pair)}&limit={limit}"))


def fetch_kline_cvd(pair: str, interval: str = "1m", limit: int = 5,
                    now_ms: int | None = None) -> list[BarCVD]:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    raw = _get(f"/fapi/v1/klines?symbol={symbol_for(pair)}&interval={interval}&limit={limit}")
    return [cvd_from_klines(row, now_ms) for row in raw]


def fetch_closed_bar(pair: str, bar: str = "1m", at_ms: int | None = None,
                     now_ms: int | None = None):
    """The exit bar for a horizon measured from `at_ms`: the CLOSED bar whose
    close is the EARLIEST one at or after `at_ms` (ceil semantics).

    Uses /klines. A bar still forming is never returned: scoring against a
    partially formed bar invents a forward return that did not happen.

    WHY CEIL, not "last close <= at_ms": the floor selection returns the bar
    CONTAINING `at_ms` whenever at_ms falls inside a forming bar. For a 1m pair
    that is a 60s approximation; for a 5m pair (horizon_bars=1) it returned the
    bar containing the SIGNAL itself, so the realized horizon was uniform in
    (0, 300s] — mean ~150s, never the 300s the row claims. Ceil guarantees the
    move spans at least the intended horizon for every bar size; the overshoot
    (up to one bar) is recorded per row as `exit_close_ms` so it stays visible.
    See `outcomes.select_exit_bar` for the full argument.
    """
    if at_ms is None:
        at_ms = int(time.time() * 1000)
    raw = _get(f"/fapi/v1/klines?symbol={symbol_for(pair)}&interval={bar}&limit=50")
    candidates = []
    for row in raw:
        candidates.append((int(row[6]), {
            "open_ms": int(row[0]), "close_ms": int(row[6]),
            "close": float(row[4]), "volume": float(row[5]),
            "taker_buy": float(row[9]),
        }))
    return OUT.select_exit_bar(candidates, at_ms, now_ms)


def fetch_open_interest(pair: str):
    raw = _get(f"/fapi/v1/openInterest?symbol={symbol_for(pair)}")
    return float(raw["openInterest"])
