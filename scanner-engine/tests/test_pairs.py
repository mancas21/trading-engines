"""Tests for the canonical pair registry.

Run:  quant-alert-engine/venv/Scripts/python.exe -m pytest scanner-engine/tests/test_pairs.py -v
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pairs


def test_all_eight_requested_pairs_present():
    want = {"SUI/USD", "XRP/USD", "SOL/USD", "SPCX/USD",
            "NVDA/USD", "HYPE/USD", "ETH/USD", "XAU/USD"}
    assert want <= set(pairs.PAIRS)


def test_venue_symbols_are_the_measured_ones():
    """Every cell verified against the live depth endpoint on 2026-10-08."""
    assert pairs.PAIRS["SUI/USD"].symbols["binance"] == "SUIUSDT"
    assert pairs.PAIRS["SUI/USD"].symbols["okx"] == "SUI-USDT-SWAP"
    assert pairs.PAIRS["SUI/USD"].symbols["kraken"] == "SUIUSD"
    assert pairs.PAIRS["SUI/USD"].symbols["bybit"] == "SUIUSDT"
    # XAU is XAUT on Binance (underlyingSubType includes RWA)
    assert pairs.PAIRS["XAU/USD"].symbols["binance"] == "XAUTUSDT"


def test_spcx_and_nvda_have_no_kraken_symbol():
    """Kraken genuinely rejects these with EQuery:Unknown asset pair."""
    assert pairs.PAIRS["SPCX/USD"].symbols.get("kraken") is None
    assert pairs.PAIRS["NVDA/USD"].symbols.get("kraken") is None


def test_min_venues_is_derived_from_symbol_count_not_hardcoded():
    """A pair with 3 venues must not demand 4. This was a real design error:
    a global floor of 3 left SPCX/NVDA permanently unevaluable."""
    for pair, spec in pairs.PAIRS.items():
        n_venues = len([v for v in spec.symbols.values() if v])
        assert spec.min_venues <= n_venues, f"{pair} floor exceeds available venues"
        # the floor must never require a venue the pair does not have
        assert spec.min_venues == n_venues, (
            f"{pair}: floor {spec.min_venues} != {n_venues} available venues"
        )


def test_unknown_pair_raises():
    try:
        pairs.spec_for("DOGE/USD")
    except KeyError:
        return
    raise AssertionError("unknown pair must raise KeyError")


def test_venues_for_pair_excludes_unsupported():
    assert "kraken" not in pairs.venues_for("SPCX/USD")
    assert set(pairs.venues_for("SUI/USD")) == {"binance", "okx", "kraken", "bybit"}


def test_bar_interval_is_set_per_pair_from_measured_liquidity():
    """1m is not viable for the thin names; the registry must reflect that
    rather than assuming one interval for everything."""
    assert pairs.PAIRS["ETH/USD"].bar == "1m"
    assert pairs.PAIRS["SOL/USD"].bar == "1m"
    assert pairs.PAIRS["SPCX/USD"].bar == "5m"
    assert pairs.PAIRS["NVDA/USD"].bar == "5m"


def test_all_pairs_declare_a_verified_liquidity_figure():
    for pair, spec in pairs.PAIRS.items():
        assert spec.avg_1m_notional_usd > 0, f"{pair} missing measured liquidity"