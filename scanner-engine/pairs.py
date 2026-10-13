"""Canonical pair registry — the single source of truth for what this engine
scans and on which venues.

EVERY FIELD HERE WAS MEASURED, NOT ASSUMED. Provenance per field is in
`docs/VENUE_FACTS.md`; the numbers below are the ones that produced this file
on 2026-10-08.

TWO THINGS THIS FILE EXISTS TO PREVENT
======================================

1. A GLOBAL `min_venues` FLOOR IS WRONG.
   SPCX and NVDA are absent from Kraken (`EQuery:Unknown asset pair`). With a
   global floor of 3 they would be permanently UNEVALUABLE; with a global floor
   of 2 the liquid pairs could verdict on a single exchange. So the floor is
   DERIVED per pair from its own venue count, and asserted equal to it.

2. VENUE NAMING IS NOT PREDICTABLE.
   Kraken renames some pairs (`XBTUSD`->`XXBTZUSD`, `ETHUSD`->`XETHZUSD`) and
   leaves others alone (`SUIUSD`, `SOLUSD`). Binance lists gold as `XAUTUSDT`.
   Tokenized equities are `TRADIFI_PERPETUAL`, not `PERPETUAL`. A symbol map
   guessed from convention is wrong somewhere, so these are recorded facts and
   Kraken's own key is still discovered at request time.

`avg_1m_notional_usd` drives the bar interval: 1m is not viable for the thin
names and pretending otherwise produces a scanner whose 1m verdicts on noise.
"""
from __future__ import annotations

from typing import NamedTuple


class PairSpec(NamedTuple):
    symbols: dict           # venue -> venue symbol (None = not listed)
    min_venues: int         # evidence floor, == count of listed venues
    bar: str                # "1m" or "5m", from measured liquidity
    avg_1m_notional_usd: float
    note: str = ""
    # The venue the FILL is taken from AND the exit is scored against. A forward
    # return is a price move on one venue; mixing a cross-venue fill with a
    # single-venue exit measures the venue spread instead of the signal. See
    # recorder.py defect 4 for the measured consequence (XAU, -30bps on 599/599
    # rows). Binance is the default because it lists all eight pairs and carries
    # a klines endpoint that serves closed bars for every one of them.
    ref_venue: str = "binance"


# avg_1m_notional_usd: Binance USDT-M, mean over 5 x 1m bars, 2026-10-08.
_PAIRS_RAW: dict[str, PairSpec] = {
    "ETH/USD": PairSpec(
        symbols={"binance": "ETHUSDT", "okx": "ETH-USDT-SWAP",
                 "kraken": "ETHUSD", "bybit": "ETHUSDT"},
        min_venues=4, bar="1m", avg_1m_notional_usd=24_864_062.0,
        note="most liquid pair in the universe",
    ),
    "SOL/USD": PairSpec(
        symbols={"binance": "SOLUSDT", "okx": "SOL-USDT-SWAP",
                 "kraken": "SOLUSD", "bybit": "SOLUSDT"},
        min_venues=4, bar="1m", avg_1m_notional_usd=3_579_140.0,
    ),
    "XRP/USD": PairSpec(
        symbols={"binance": "XRPUSDT", "okx": "XRP-USDT-SWAP",
                 "kraken": "XRPUSD", "bybit": "XRPUSDT"},
        min_venues=4, bar="1m", avg_1m_notional_usd=968_655.0,
    ),
    "HYPE/USD": PairSpec(
        symbols={"binance": "HYPEUSDT", "okx": "HYPE-USDT-SWAP",
                 "kraken": "HYPEUSD", "bybit": "HYPEUSDT"},
        min_venues=4, bar="5m", avg_1m_notional_usd=435_231.0,
        note="thin at 1m; Kraken/Bybit books are ~10x wider than Binance/OKX",
    ),
    "SUI/USD": PairSpec(
        symbols={"binance": "SUIUSDT", "okx": "SUI-USDT-SWAP",
                 "kraken": "SUIUSD", "bybit": "SUIUSDT"},
        min_venues=4, bar="5m", avg_1m_notional_usd=345_261.0,
        note="thin at 1m",
    ),
    "XAU/USD": PairSpec(
        symbols={"binance": "XAUTUSDT", "okx": "XAU-USDT-SWAP",
                 "kraken": "XAUTUSDT", "bybit": "XAUUSDT"},
        min_venues=4, bar="5m", avg_1m_notional_usd=125_722.0,
        note="Binance lists gold as XAUTUSDT; measured ~23bps cross-venue basis",
    ),
    "SPCX/USD": PairSpec(
        symbols={"binance": "SPCXUSDT", "okx": "SPCX-USDT-SWAP",
                 "kraken": None, "bybit": "SPCXUSDT"},
        min_venues=3, bar="5m", avg_1m_notional_usd=10_000.0,
        note="Kraken does not list it; TRADIFI_PERPETUAL on Binance/Bybit",
    ),
    "NVDA/USD": PairSpec(
        symbols={"binance": "NVDAUSDT", "okx": "NVDA-USDT-SWAP",
                 "kraken": None, "bybit": "NVDAUSDT"},
        min_venues=3, bar="5m", avg_1m_notional_usd=18_000.0,
        note="Kraken does not list it; OKX askSz was 0.55 vs bidSz 41.96 at the touch",
    ),
}

PAIRS: dict[str, PairSpec] = _PAIRS_RAW


def spec_for(pair: str) -> PairSpec:
    """Registry lookup. Raises KeyError for a pair that is not configured —
    an unknown pair must never be silently skipped."""
    return _PAIRS_RAW[pair]


def venues_for(pair: str) -> list[str]:
    """Venues that actually list this pair."""
    return [v for v, s in spec_for(pair).symbols.items() if s]


def min_venues_for(pair: str) -> int:
    return spec_for(pair).min_venues


def bar_for(pair: str) -> str:
    return spec_for(pair).bar


def ref_venue_for(pair: str) -> str:
    """The venue used for BOTH the recorded fill and the exit.

    Raises KeyError for an unconfigured pair — callers that must degrade
    (the running loop) catch it and fall back rather than crashing the cycle.
    """
    return spec_for(pair).ref_venue


def all_pairs() -> list[str]:
    return list(_PAIRS_RAW)


def coverage() -> dict:
    """Venue-count summary, for /health."""
    return {
        p: {
            "venues": len(venues_for(p)),
            "min_venues": min_venues_for(p),
            "missing": [v for v, s in spec_for(p).symbols.items() if not s],
            "bar": bar_for(p),
        }
        for p in _PAIRS_RAW
    }


if __name__ == "__main__":
    print(f"{'PAIR':10} {'VENUES':>6} {'FLOOR':>5} {'BAR':>4}  MISSING")
    print("-" * 58)
    for p, c in coverage().items():
        miss = ",".join(c["missing"]) or "-"
        print(f"{p:10} {c['venues']:>6} {c['min_venues']:>5} {c['bar']:>4}  {miss}")