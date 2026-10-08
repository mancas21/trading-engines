"""TDD for the corrected CVD layer.

RED phase. These tests define the contract before any implementation exists.

Key design constraint these tests encode: CVD grain must be COMPARABLE
across venues. Binance offers per-bar CVD from klines; OKX/Kraken/Bybit offer
per-trade side only, and a fixed trade count spans a wildly variable time
window (50 trades on NVDA may be seconds; on ETH, milliseconds). Mixing
per-bar and per-50-trades would make cross-venue CVD comparison meaningless,
so every venue is normalized to a fixed TIME window.
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


# ── window CVD: the core contract ──────────────────────────────────────────

def test_window_cvd_sums_buy_and_sell_within_the_window():
    from flow import window_cvd
    # 3 trades at t=1000,1001,1002; window is [now-10, now] with now=1010
    trades = [
        {"ts": 1000.0, "price": 1.0, "size": 10.0, "taker_side": "buy"},
        {"ts": 1001.0, "price": 1.0, "size": 5.0, "taker_side": "sell"},
        {"ts": 1002.0, "price": 1.0, "size": 2.0, "taker_side": "buy"},
    ]
    res = window_cvd(trades, now=1010.0, window_s=10.0)
    assert res.buy == 12.0
    assert res.sell == 5.0
    assert res.delta == 7.0
    assert res.n_trades == 3


def test_window_cvd_excludes_trades_older_than_the_window():
    """A stale trade must not be silently averaged into a live reading.
    This is the ground rule: absent data is declared, never imputed."""
    from flow import window_cvd
    trades = [
        {"ts": 500.0, "price": 1.0, "size": 999.0, "taker_side": "buy"},  # stale
        {"ts": 1005.0, "price": 1.0, "size": 10.0, "taker_side": "buy"},  # live
    ]
    res = window_cvd(trades, now=1010.0, window_s=10.0)
    assert res.buy == 10.0, "stale trade leaked into the window"
    assert res.n_trades == 1


def test_window_cvd_returns_none_when_no_trades_in_window():
    """None, never 0.0 -- a zero flow and an absent flow are different claims."""
    from flow import window_cvd
    trades = [{"ts": 1.0, "price": 1.0, "size": 5.0, "taker_side": "buy"}]
    res = window_cvd(trades, now=1010.0, window_s=10.0)
    assert res is None


def test_window_cvd_reports_the_window_it_actually_covered():
    """A fixed trade count spans a variable time window; the result must
    state the real coverage so a thin pair is visibly thinner."""
    from flow import window_cvd
    trades = [
        {"ts": 1008.0, "price": 1.0, "size": 1.0, "taker_side": "buy"},
        {"ts": 1009.0, "price": 1.0, "size": 1.0, "taker_side": "buy"},
    ]
    res = window_cvd(trades, now=1010.0, window_s=60.0)
    # span between oldest (1008) and newest (1009) = 1s of real activity,
    # even though a 60s window was requested
    assert res.coverage_s == 1.0
    assert res.requested_window_s == 60.0


# ── per-venue parsers: each must normalize to the same trade shape ────────

def test_binance_aggtrade_m_flag_inverts_to_taker_side():
    """Binance's m=true means the BUYER is the maker, so the aggressor
    SOLD. Getting this backwards inverts the entire CVD sign."""
    from venues.binance import parse_aggtrades
    raw = [
        {"p": "1.0045", "q": "10.7", "T": 1791483166949, "m": True},   # buyer maker -> taker sell
        {"p": "1.0046", "q": "21.0", "T": 1791483167076, "m": False},  # seller maker -> taker buy
        {"p": "1.0047", "q": "544.9", "T": 1791483167440, "m": False},
    ]
    trades = parse_aggtrades(raw)
    assert trades[0]["taker_side"] == "sell"
    assert trades[1]["taker_side"] == "buy"
    assert trades[2]["taker_side"] == "buy"
    assert trades[0]["size"] == 10.7


def test_binance_klines_cvd_derives_delta_from_taker_buy_volume():
    """klines idx9 = takerBuyBaseVolume, idx5 = total base volume."""
    from venues.binance import cvd_from_klines
    raw = [[
        1791483060000, "1.0021", "1.0048", "1.0009", "1.0034",
        "361199.2", 1791483119999, "362165.07", 1792,
        "212744.9", "213360.94", "0",
    ]]
    bar = cvd_from_klines(raw[0])
    assert bar.buy == 212744.9
    assert bar.sell == pytest_approx(361199.2 - 212744.9)
    assert bar.delta == pytest_approx(212744.9 - (361199.2 - 212744.9))


def test_binance_klines_marks_the_in_progress_bar():
    """A partially formed bar must never feed feature or outcome math."""
    from venues.binance import cvd_from_klines
    now_ms = 1791483150000
    in_progress = [
        1791483120000, "1", "1", "1", "1", "13945.1",
        1791483179999, "1", 1, "9398.5", "1", "0",
    ]
    closed = [
        1791483060000, "1", "1", "1", "1", "157147.0",
        1791483119999, "1", 1, "67233.6", "1", "0",
    ]
    assert cvd_from_klines(in_progress, now_ms=now_ms).closed is False
    assert cvd_from_klines(closed, now_ms=now_ms).closed is True


def test_okx_trades_parse_side_lowercase():
    from venues.okx import parse_trades
    raw = {"code": "0", "data": [
        {"instId": "SUI-USDT-SWAP", "tradeId": "1", "px": "1.002", "sz": "10",
         "side": "buy", "ts": "1791483160000", "source": "1"},
        {"instId": "SUI-USDT-SWAP", "tradeId": "2", "px": "1.001", "sz": "4",
         "side": "sell", "ts": "1791483161000", "source": "1"},
    ]}
    trades = parse_trades(raw)
    assert trades[0]["taker_side"] == "buy"
    assert trades[1]["taker_side"] == "sell"
    assert trades[0]["ts"] == 1791483160.0     # ms -> s
    assert trades[1]["size"] == 4.0


def test_okx_trades_raise_on_error_code():
    import pytest
    from venues.okx import parse_trades
    with pytest.raises(ValueError):
        parse_trades({"code": "50014", "msg": "Parameter instType can not be empty.",
                      "data": []})


def test_bybit_trades_parse_side():
    from venues.bybit import parse_trades
    raw = {"retCode": 0, "result": {"list": [
        {"execId": "a", "symbol": "SUIUSDT", "price": "1.0015", "size": "20",
         "side": "Sell", "time": "1791482013461", "isBlockTrade": False},
        {"execId": "b", "symbol": "SUIUSDT", "price": "1.0016", "size": "5",
         "side": "Buy", "time": "1791482014000", "isBlockTrade": False},
    ]}}
    trades = parse_trades(raw)
    assert trades[0]["taker_side"] == "sell"    # normalized to lowercase
    assert trades[1]["taker_side"] == "buy"
    assert trades[0]["ts"] == 1791482013.461


def test_bybit_trades_raise_on_retcode():
    import pytest
    from venues.bybit import parse_trades
    with pytest.raises(ValueError):
        parse_trades({"retCode": 10001, "retMsg": "params error", "result": {"list": []}})


def test_kraken_trades_discover_mangled_key_not_assume_it():
    """Kraken renames inconsistently: XBTUSD->XXBTZUSD, XRPUSD->XXRPZUSD,
    ETHUSD->XETHZUSD, but SUIUSD/SOLUSD/HYPEUSD are unmangled. A hardcoded
    map would be wrong for at least one pair."""
    from venues.kraken import parse_trades
    for key in ("XXBTZUSD", "XXRPZUSD", "XETHZUSD", "SUIUSD", "SOLUSD", "HYPEUSD"):
        raw = {"error": [], "result": {
            key: [["1.0", "3.0", 1791483160.1, "b", "l", "", 1]],
            "last": "1791483160123456789",
        }}
        trades = parse_trades(raw)
        assert len(trades) == 1, f"failed to discover key {key}"
        assert trades[0]["taker_side"] == "buy"


def test_kraken_trades_return_none_on_ambiguous_key():
    """Two candidate keys must yield None, never a guess -- a guessed pair
    produces confident wrong numbers."""
    from venues.kraken import parse_trades
    raw = {"error": [], "result": {
        "AAAA": [["1.0", "1.0", 1.0, "b", "l", "", 1]],
        "BBBB": [["1.0", "1.0", 1.0, "s", "l", "", 2]],
    }}
    assert parse_trades(raw) is None


def test_kraken_trades_index_3_is_the_taker_side():
    from venues.kraken import parse_trades
    raw = {"error": [], "result": {
        "SUIUSD": [["1.0135", "1479.9", 1791480853.8, "s", "l", "", 8283544],
                   ["1.0131", "39.4", 1791480854.2, "b", "l", "", 8283545]],
    }}
    trades = parse_trades(raw)
    assert trades[0]["taker_side"] == "sell"
    assert trades[1]["taker_side"] == "buy"
    assert trades[0]["size"] == 1479.9


def test_kraken_gold_is_xautusd_not_xauusd():
    """XAUUSD does not exist on Kraken -- it returns
    EQuery:Unknown asset pair. The listing is XAUTUSD / XAUTUSDT.
    Mapping gold as plain XAU produces a hard failure on one venue."""
    from venues.kraken import symbol_for
    assert symbol_for("XAU/USD") == "XAUTUSD"


def test_kraken_newest_trade_age_uses_max_not_index_zero():
    """Trap 2 regression. Kraken returns trades in ASCENDING time order, so
    trades[0] is the OLDEST. An early probe of this engine read index 0 as
    newest and concluded Kraken was 872s-27h stale, when the real age was
    2-6s. This helper must never regress to index 0."""
    from venues.kraken import newest_trade_age_s
    trades = [
        {"ts": 1000.0, "size": 1.0, "taker_side": "buy"},   # oldest
        {"ts": 1009.0, "size": 1.0, "taker_side": "buy"},   # newest
        {"ts": 1005.0, "size": 1.0, "taker_side": "buy"},
    ]
    assert newest_trade_age_s(trades, now=1010.0) == 1.0


def test_binance_tradifi_perpetuals_are_tradable():
    """SPCXUSDT and NVDAUSDT are listed on Binance as contractType
    TRADIFI_PERPETUAL, NOT PERPETUAL. Filtering exchangeInfo on
    contractType=='PERPETUAL' wrongly reports them as absent -- which is
    exactly the mistake that produced a false '2 of 4 venues' coverage
    table. The instrument IS tradeable: depth, aggTrades, klines and OI
    all return data."""
    from venues.binance import is_tradable_contract_type
    assert is_tradable_contract_type("TRADIFI_PERPETUAL")
    assert is_tradable_contract_type("PERPETUAL")
    assert not is_tradable_contract_type("PERPETUAL_DELIVERING")
    assert not is_tradable_contract_type("")

def test_cross_venue_comparison_is_window_normalized_not_count_normalized():
    """50 trades on NVDA covers seconds; 50 trades on ETH covers
    milliseconds. Summing a fixed count makes a thin pair look like a
    violent flow. Both must be normalized to the same TIME window."""
    from flow import window_cvd
    now = 1010.0
    thin = [{"ts": now - i * 0.2, "price": 1.0, "size": 10.0, "taker_side": "buy"}
            for i in range(50)]                       # 50 trades over 10s
    dense = [{"ts": now - 1000 - i * 0.001, "price": 1.0, "size": 10.0,
              "taker_side": "buy"} for i in range(50)]  # 50 trades, all stale
    thin_res = window_cvd(thin, now=now, window_s=60.0)
    dense_res = window_cvd(dense, now=now, window_s=60.0)
    assert thin_res is not None
    assert dense_res is None, "stale dense flow was counted as live"


def pytest_approx(v, rel=1e-6):
    class _A:
        def __eq__(self, other):
            return abs(v - other) <= rel * max(1.0, abs(v))
    return _A()
