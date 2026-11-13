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
    # Per-venue multipliers that bring a venue's QUOTED price and size onto
    # the pair's reference scale.
    #
    # WHY THIS EXISTS: a venue may list the same underlying under a different
    # contract multiplier. Binance and Bybit list 1000PEPEUSDT (1000x, price is
    # per 1000 PEPE); OKX lists PEPE-USDT-SWAP (1x price, size in CONTRACTS of
    # 10,000,000 PEPE); Kraken lists PEPEUSD (1x spot, size in PEPE). Mixing
    # those in one pair makes basis_bps meaningless -- the cross-venue mid
    # becomes a blend of two different scales and every venue's basis is off by
    # a factor of 1000.
    #
    # price_mult: multiply the venue's quoted PRICE to reach the reference.
    # size_mult:  multiply the venue's quoted SIZE to reach the reference.
    # The product preserves NOTIONAL: reference notional == true notional.
    #
    # None means "every venue is already on the reference scale" (the default
    # for every pair that is not a multiplier case). A dict, not a mutable
    # default: NamedTuple cannot take a default_factory.
    price_mult: dict | None = None
    size_mult: dict | None = None


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
                 "kraken": "XAUTUSD", "bybit": "XAUUSDT"},
        min_venues=4, bar="5m", avg_1m_notional_usd=125_722.0,
        note=("TWO GOLD CONTRACTS EXIST AND THIS PAIR MIXES THEM. Measured 2026-11-12: binance/Bybit list BOTH XAUUSDT (3860-3871) and XAUTUSDT (3859-3860), ~30bps apart; OKX lists only XAU-USDT-SWAP (the XAU quote) and Kraken only XAUTUSD (the XAUT quote). No gold contract is on all four venues, so the ~23-32bps cross-venue basis this pair has always shown is an INSTRUMENT DIFFERENCE, not a dislocation -- read it as noise, never as rule-1 evidence. The symbols below are EXACTLY what the adapters traded before the registry became authoritative (2026-11-12): binance XAUTUSDT, okx XAU-USDT-SWAP, kraken XAUTUSD, bybit XAUUSDT. The registry had recorded kraken=XAUTUSDT and bybit=XAUTUSDT instead; neither was ever read, and both would have silently changed this pair on the day the registry took over."),
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
    "ARB/USD": PairSpec(
        symbols={"binance": "ARBUSDT", "okx": "ARB-USDT-SWAP",
                 "kraken": "ARBUSD", "bybit": "ARBUSDT"},
        min_venues=4, bar="5m", avg_1m_notional_usd=37_209.0,
        note=("Added 2026-11-10 from live measurement (_probe_arb.py, "
              "_probe_arb_venues.py, _probe_arb_tape.py). All four venues list "
              "it and all four answer depth AND trades, so the floor is 4. "
              "60 x 1m notional USD: binance 37,209 (median 32,606), okx 21,115, "
              "bybit 17,959, kraken 1,043 (median 57). 5m because 1m is only "
              "defensible for ETH/SOL/XRP; ARB's own 1m book is ~37K USD/min. "
              "Kraken trades only 2 in 60s -> it fails the rules' trade_floor "
              "(50) and contributes NO flow_by_venue, exactly like JUP; its "
              "book_imb_5 is unaffected. Kraken's touch spread is 6.32bps vs "
              "0.63bps on the three perps -- it is a thin spot book, so the "
              "cross-venue basis must be read against each venue's OWN spread."),
    ),
    "JUP/USD": PairSpec(
        symbols={"binance": "JUPUSDT", "okx": "JUP-USDT-SWAP",
                 "kraken": "JUPUSD", "bybit": "JUPUSDT"},
        min_venues=4, bar="5m", avg_1m_notional_usd=6_792.0,
        note=("THINNEST pair in this registry. All four venues list it and all "
              "four answer depth/trades, so the floor is 4. Measured 2026-10-14, "
              "60 x 1m bars: binance 6,792 USD/min (median 4,935) -- below SPCX. "
              "Kraken is effectively dead here: median 5 USD/min. 5m is not a "
              "judgement call, it is the only defensible interval at this size. "
              "Binance is PERPETUAL, not TRADIFI."),
    ),
    "BTC/USD": PairSpec(
        symbols={"binance": "BTCUSDT", "okx": "BTC-USDT-SWAP",
                 "kraken": "XBTUSD", "bybit": "BTCUSDT"},
        min_venues=4, bar="1m", avg_1m_notional_usd=9_000_000.0,
        note=("Most liquid pair in the universe. Measured 2026-10-18 "
              "(5 x 1m bars, Binance USDT-M): ~9.0M USD/min. Kraken lists as "
              "XBTUSD (discovered at request time). 1m interval is safe at "
              "this liquidity."),
    ),
    "1000PEPE/USD": PairSpec(
        symbols={"binance": "1000PEPEUSDT", "okx": "PEPE-USDT-SWAP",
                 "kraken": "PEPEUSD", "bybit": "1000PEPEUSDT"},
        min_venues=4, bar="5m", avg_1m_notional_usd=399_924.0,
        # The reference scale is the 1000x contract (Binance/Bybit price is
        # per 1000 PEPE).
        price_mult={"binance": 1.0, "bybit": 1.0, "okx": 1000.0, "kraken": 1000.0},
        # OKX quotes size in CONTRACTS of 10,000,000 PEPE (ctVal), so one
        # contract is 10,000 units of 1000PEPE. Kraken quotes in PEPE, so one
        # token is 0.001 units of 1000PEPE.
        size_mult={"binance": 1.0, "bybit": 1.0, "okx": 10_000.0, "kraken": 0.001},
        note=("Added 2026-11-12 from live measurement (_probe_pepe_venues.py, "
              "_probe_pepe_notional.py, _probe_pepe_liquidity.py). THE "
              "MULTIPLIER IS NOT UNIFORM ACROSS VENUES and that is the whole "
              "point of this entry: Binance and Bybit list ONLY 1000PEPEUSDT "
              "(1000x; fapi does not list 1x PEPE at all), OKX lists "
              "PEPE-USDT-SWAP (1x price, ctVal 10,000,000 PEPE per contract) "
              "and Kraken lists PEPEUSD (1x spot, size in PEPE). All four "
              "venues answer depth AND trades, so the floor is 4 -- which is "
              "what the ATAS reader's rule 6/7 condition (c) needs (>=3 "
              "venues with a top-5 book consensus). Measured 60 closed 1m "
              "bars: binance notional mean 399,924 USD (median 345,129), okx "
              "mean 270,271 (median 237,693). 5m because 1m is not defensible "
              "at ~400K USD/min -- the same standard that puts HYPE (435K) on "
              "5m. Touch spreads: binance 0.23bps, okx 0.23bps, bybit 2.28bps, "
              "kraken 2.28bps. price_mult/size_mult put OKX and Kraken on the "
              "1000x reference scale; the product preserves notional "
              "(okx 17 contracts x 10M PEPE at 4.38e-06 = $744.9 == 170,000 "
              "units of 1000PEPE x 0.00438). Kraken is spot, not a perp."),
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


def venue_symbol(pair: str, venue: str) -> str | None:
    """The registry's symbol for this pair on this venue, or None.

    This is the single source of truth for venue symbols. The adapters used to
    DERIVE their symbol from the pair name, which is wrong whenever a venue
    lists the pair under a different name (Kraken's XBTUSD, Binance's
    XAUTUSDT, OKX's PEPE-USDT-SWAP for a pair Binance calls 1000PEPEUSDT).
    Deriving it invented instruments that do not exist: the adapter asked
    OKX for 1000PEPE-USDT-SWAP and got `Instrument ID does not exist`.

    None means "this venue does not list the pair" — a fact, not a fault.
    """
    return spec_for(pair).symbols.get(venue)


def venue_price_mult(pair: str, venue: str) -> float:
    """Multiplier that brings this venue's quoted PRICE onto the pair scale."""
    spec = spec_for(pair)
    if not spec.price_mult:
        return 1.0
    return float(spec.price_mult.get(venue, 1.0))


def venue_size_mult(pair: str, venue: str) -> float:
    """Multiplier that brings this venue's quoted SIZE onto the pair scale."""
    spec = spec_for(pair)
    if not spec.size_mult:
        return 1.0
    return float(spec.size_mult.get(venue, 1.0))


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
