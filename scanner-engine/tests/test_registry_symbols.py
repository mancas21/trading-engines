"""The registry and the adapters must agree on every venue symbol.

WHY THIS TEST EXISTS
====================
Until 2026-11-12 each venue adapter DERIVED its symbol from the pair name
(`base + "USDT"`, `base + "-USDT-SWAP"`, ...) and the registry's own
`symbols` map was dead data that nothing read. Making the registry
authoritative is the correct fix -- a venue that lists a pair under a
different name cannot be derived -- but it turns every wrong registry
entry into a silent behaviour change.

It found one immediately: the registry recorded Kraken's gold as
`XAUTUSDT` while the adapter derived `XAUTUSD`, and the adapter was the
one that had been trading. Kraken lists BOTH (ZUSD-quoted and
USDT-quoted); this engine trades the ZUSD quote and the registry now
says so.

So the rule is: for a pair whose symbols are all derivable, the registry
must agree with the adapters, and the pair must be listed as derivable
here. A pair with a genuine override must NOT be in that list -- and
there is exactly one today (1000PEPE/USD), because OKX lists PEPE at 1x
while the pair is the 1000x contract.

Run:  python -m pytest scanner-engine/tests/ -q
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import pairs as P  # noqa: E402
from venues import binance as bn  # noqa: E402
from venues import okx as okx  # noqa: E402
from venues import kraken as kr  # noqa: E402
from venues import bybit as bb  # noqa: E402

ADAPTERS = {"binance": bn, "okx": okx, "kraken": kr, "bybit": bb}


def _naive_symbol(venue, pair):
    """What the adapter derived BEFORE the registry became authoritative.

    The adapters now read the registry, so comparing the registry against
    them would be circular. This is the derivation rule they used, written
    out: it is what the engine actually traded for every pair before
    2026-11-12, and therefore what the registry must still agree with
    wherever the venue's listing matches the name.
    """
    base = pair.split("/")[0]
    if venue == "binance":
        if base == "XAU":
            base = "XAUT"
        return base + "USDT"
    if venue == "okx":
        return "{}-USDT-SWAP".format(base)
    if venue == "kraken":
        if base == "BTC":
            base = "XBT"
        elif base == "XAU":
            base = "XAUT"
        return base + "USD"
    if venue == "bybit":
        return base + "USDT"
    raise AssertionError("unknown venue " + venue)


# Pairs whose venue symbols are all derivable from the pair name. A pair
# appears here only if EVERY venue that lists it derives its symbol.
DERIVABLE = {
    "ETH/USD", "SOL/USD", "XRP/USD", "HYPE/USD", "SUI/USD", "XAU/USD",
    "SPCX/USD", "NVDA/USD", "ARB/USD", "JUP/USD", "BTC/USD",
}


def _overrides(pair):
    """Venues whose registry symbol differs from the naive derivation."""
    spec = P.spec_for(pair)
    out = []
    for venue in spec.symbols:
        recorded = spec.symbols[venue]
        if not recorded:
            continue
        if recorded != _naive_symbol(venue, pair):
            out.append(venue)
    return out


def test_every_pair_has_a_registry_entry():
    for pair in P.all_pairs():
        spec = P.spec_for(pair)
        assert spec.min_venues == len(P.venues_for(pair)), (
            "{}: min_venues {} != {} listed venues".format(
                pair, spec.min_venues, len(P.venues_for(pair))))


def test_derivable_pairs_match_the_adapter_derivation():
    """Where the name IS derivable, the registry must not disagree.

    A registry entry that differs from the derivation is a behaviour
    change waiting to happen: the registry is now what the adapters read.
    """
    mismatches = []
    for pair in P.all_pairs():
        if pair not in DERIVABLE:
            continue
        spec = P.spec_for(pair)
        for venue in P.venues_for(pair):
            derived = _naive_symbol(venue, pair)
            if spec.symbols[venue] != derived:
                mismatches.append("{} / {}: registry {} != derived {}".format(
                    pair, venue, spec.symbols[venue], derived))
    assert not mismatches, (
        "registry and the derivation rule disagree:\n  " + "\n  ".join(mismatches))


def test_the_derivable_list_is_exactly_the_derivable_pairs():
    """A pair whose symbols are NOT derivable must not be in DERIVABLE (it
    would be checked against a derivation that does not apply), and a pair
    that IS derivable must be in it (or the check above stops covering it)."""
    for pair in P.all_pairs():
        has_override = bool(_overrides(pair))
        if has_override and pair in DERIVABLE:
            raise AssertionError(
                "{} has an override ({}), so it cannot be in DERIVABLE".format(
                    pair, ", ".join(_overrides(pair))))
        if not has_override and pair not in DERIVABLE:
            raise AssertionError(
                "{} has no override -- add it to DERIVABLE so the agreement "
                "check covers it".format(pair))


def test_multiplier_pairs_declare_their_scale():
    """A pair whose venues quote different contract multipliers must carry
    the multipliers, or the cross-venue price comparison is meaningless."""
    for pair in P.all_pairs():
        spec = P.spec_for(pair)
        pm = P.venue_price_mult(pair, "binance")
        for venue in P.venues_for(pair):
            if P.venue_price_mult(pair, venue) != pm:
                assert spec.price_mult, (
                    "{}: venue {} has a different price scale but the pair "
                    "declares no price_mult".format(pair, venue))
                assert spec.size_mult, (
                    "{}: venue {} has a different price scale but the pair "
                    "declares no size_mult".format(pair, venue))


def test_multiplier_preserves_notional():
    """price_mult * size_mult must equal the token count per price unit, so
    the scaled notional equals the real one."""
    # 1000PEPE: Binance quotes per 1000 PEPE (mult 1/1). OKX quotes per PEPE
    # with size in contracts of 10,000,000 PEPE -> 1000 x 10,000. Kraken
    # quotes per PEPE with size in PEPE -> 1000 x 0.001.
    pair = "1000PEPE/USD"
    assert P.venue_price_mult(pair, "okx") * P.venue_size_mult(pair, "okx") == 10_000_000.0
    assert P.venue_price_mult(pair, "kraken") * P.venue_size_mult(pair, "kraken") == 1.0
    assert P.venue_price_mult(pair, "binance") * P.venue_size_mult(pair, "binance") == 1.0
    assert P.venue_price_mult(pair, "bybit") * P.venue_size_mult(pair, "bybit") == 1.0
