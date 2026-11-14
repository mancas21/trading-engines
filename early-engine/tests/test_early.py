"""Tests for the pure parts of early-engine: the book and the detectors.

Live feeds are not exercised here. These tests pin the behaviour that makes
the signals trustworthy: a broken book must not emit, a stale book must not
be read, and each detector must fire on its own condition and stay silent
when that condition is absent.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import detectors as D  # noqa: E402
from book import BookMetrics, LocalBook  # noqa: E402


def snap(last_id, bids, asks):
    return {"lastUpdateId": last_id, "bids": bids, "asks": asks}


def ev(U, u, pu, b=(), a=()):
    return {"U": U, "u": u, "pu": pu, "b": [list(x) for x in b], "a": [list(x) for x in a]}


# ---------------------------------------------------------------- book

def test_book_bridges_snapshot_then_chains():
    bk = LocalBook("X")
    bk.load_snapshot(snap(100, [["99", "5"]], [["101", "5"]]))
    assert bk.apply(ev(99, 102, 98, b=[["99", "7"]])) is True  # bridges 100
    assert bk.valid
    assert bk.apply(ev(103, 105, 102, a=[["101", "0"]])) is True  # pu == 102
    assert bk.asks == {} and bk.bids[99.0] == 7.0


def test_book_detects_sequence_gap_and_invalidates():
    bk = LocalBook("X")
    bk.load_snapshot(snap(100, [["99", "5"]], [["101", "5"]]))
    bk.apply(ev(99, 102, 98))
    assert bk.apply(ev(200, 210, 150)) is False  # pu 150 != last u 102
    assert bk.valid is False
    assert bk.best() is None  # an invalid book must not be read


def test_book_drops_stale_events_before_snapshot():
    bk = LocalBook("X")
    bk.load_snapshot(snap(100, [["99", "5"]], [["101", "5"]]))
    assert bk.apply(ev(50, 60, 49, b=[["99", "999"]])) is True
    assert bk.bids[99.0] == 5.0  # untouched


def test_metrics_imbalance_sign():
    bk = LocalBook("X")
    bk.load_snapshot(snap(1, [["100", "9"]], [["100.01", "1"]]))
    bk.apply(ev(1, 2, 0))
    m = bk.metrics(band_bps=50)
    assert m.imbalance > 0.7  # bid-heavy


# ---------------------------------------------------------------- detectors

def view(**kw):
    base = dict(
        symbol="X", ts=0.0, price=100.0, atr_1m=0.5,
        levels=[], flow={"buy_z": 0.0, "sell_z": 0.0, "buy_qty": 0, "sell_qty": 0},
        book=BookMetrics(mid=100.0, best_bid=99.99, best_ask=100.01,
                         bid_depth=10, ask_depth=2, imbalance=0.67),
        book_baseline_ask=10.0, price_change_bps_window={15: 0.0},
        move_atr_window=0.0, extension_atr=0.0,
    )
    base.update(kw)
    return D.MarketView(**base)


CFG = D.Config()


def test_breakout_pressure_fires_below_resistance_with_flow_and_thin_asks():
    v = view(price=100.0,
             levels=[D.Level(price=100.05, kind="resistance")],
             flow={"buy_z": 3.0, "sell_z": 0.0})
    e = D.breakout_pressure(v, CFG)
    assert e is not None and e.name == "BREAKOUT_PRESSURE" and e.side == "long"


def test_breakout_pressure_silent_without_thin_asks():
    v = view(price=100.0,
             levels=[D.Level(price=100.05, kind="resistance")],
             flow={"buy_z": 3.0, "sell_z": 0.0},
             book_baseline_ask=2.0)  # current ask 2 == baseline, not depleted
    assert D.breakout_pressure(v, CFG) is None


def test_breakout_pressure_silent_after_level_broken():
    v = view(price=100.10,
             levels=[D.Level(price=100.05, kind="resistance", mitigated=True)],
             flow={"buy_z": 3.0, "sell_z": 0.0})
    assert D.breakout_pressure(v, CFG) is None


def test_bounce_absorption_fires_on_selling_that_fails_to_push_price_down():
    v = view(price=100.0,
             levels=[D.Level(price=99.995, kind="support")],
             flow={"buy_z": 0.0, "sell_z": 3.0},
             price_change_bps_window={15: -1.0})
    e = D.bounce_absorption(v, CFG)
    assert e is not None and e.name == "BOUNCE_ABSORPTION"


def test_bounce_absorption_silent_when_price_keeps_falling():
    v = view(price=100.0,
             levels=[D.Level(price=99.995, kind="support")],
             flow={"buy_z": 0.0, "sell_z": 3.0},
             price_change_bps_window={15: -30.0})
    assert D.bounce_absorption(v, CFG) is None


def test_fvg_retest_fires_inside_unmitigated_bull_gap():
    v = view(price=100.2,
             levels=[D.Level(price=0, kind="fvg_bull", bottom=100.0, top=100.4)],
             flow={"buy_z": 0.5, "sell_z": 0.0})
    e = D.fvg_retest(v, CFG)
    assert e is not None and e.name == "FVG_RETEST"


def test_fvg_detection_requires_real_gap():
    bars = [{"high": 100.0, "low": 99.0, "close": 99.5},
            {"high": 100.5, "low": 99.8, "close": 100.4},
            {"high": 101.0, "low": 100.2, "close": 100.9}]
    lvl = D.fvg_from_bars(bars, min_gap_atr=0.1, atr=0.5)
    assert lvl is not None and lvl.kind == "fvg_bull"
    assert lvl.bottom == 100.0 and lvl.top == 100.2
    # overlapping bars: no gap
    bars[2] = {"high": 101.0, "low": 99.9, "close": 100.9}
    assert D.fvg_from_bars(bars, min_gap_atr=0.1, atr=0.5) is None


def test_rally_ignition_requires_early_leg():
    early = view(move_atr_window=1.5, extension_atr=0.5,
                 flow={"buy_z": 2.5, "sell_z": 0.0})
    late = view(move_atr_window=1.5, extension_atr=3.0,
                flow={"buy_z": 2.5, "sell_z": 0.0})
    assert D.rally_ignition(early, CFG) is not None
    assert D.rally_ignition(late, CFG) is None  # already extended: not early


def test_zscore_is_zero_on_thin_or_flat_baseline():
    assert D.zscore(5.0, [1.0] * 5) == 0.0          # too few samples
    assert D.zscore(5.0, [1.0] * 50) == 0.0         # zero variance
    assert D.zscore(9.0, list(range(10))) > 0       # above the mean
    assert D.zscore(3.0, list(range(10))) < 0       # below the mean


# ---------------------------------------------------------------- runtime e2e

def test_runtime_path_fires_logs_and_settles(tmp_path):
    """End-to-end sintetico del camino del runtime (el WS/REST no se toca):
    SymbolState con libro valido + rafaga de compras -> view -> detector ->
    Output.record_event -> settle escribe el forward return."""
    import json

    import early_engine as EE

    cfg = D.Config()
    st = EE.SymbolState("BTCUSDT", cfg)

    # libro valido: snapshot + evento puente
    # bid/ask DENTRO de la banda de 10 bps del mid para que el imbalance signifique
    st.book.load_snapshot({"lastUpdateId": 10,
                           "bids": [["99.99", "50"]], "asks": [["100.01", "1"]]})
    assert st.book.apply({"U": 10, "u": 11, "pu": 9, "b": [], "a": []}) is True
    assert st.book.valid

    # baseline de flow: 12 muestras de 5 s con volumen moderado y VARIABLE.
    # Durante la fase baseline el libro tiene asks GORDAS: flow_z muestrea el
    # ask_depth en cada llamada, asi que el baseline de depletion sale de ahi.
    t0 = 1_000_000.0
    st.book.asks = {100.01: 50.0}
    for k in range(12):
        for _ in range(5 + (k % 3)):
            st.on_trade(t0 + k * 5, 100.0, 1.0, False)
        st.flow_z(t0 + k * 5 + 0.1)
    # rafaga agresiva compradora en la ventana actual
    for _ in range(200):
        st.on_trade(t0 + 65.0, 100.0, 1.0, False)

    # evento: asks AGOTADAS frente al baseline gordo (1 << 0.6*50)
    st.book.asks = {100.01: 1.0}
    st.levels = [D.Level(price=100.05, kind="resistance")]

    v = st.view(t0 + 66.0)
    assert v is not None and v.book is not None
    evs = D.run_all(v, cfg)
    names = [e.name for e in evs]
    assert "BREAKOUT_PRESSURE" in names, names

    # Output real contra fichero temporal
    out = EE.Output({"event_log": str(tmp_path / "ev.jsonl"), "mode": "shadow"})
    ev = next(e for e in evs if e.name == "BREAKOUT_PRESSURE")
    out.record_event(ev, t0)
    # sin bridge_url en modo shadow: nada se publica
    out.settle("BTCUSDT", ev.price * 1.001, t0 + 31.0)

    rows = [json.loads(l) for l in (tmp_path / "ev.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [r["kind"] for r in rows]
    assert "event" in kinds
    assert any(r["kind"] == "fwd" and r["horizon_s"] == 30 and r["ret_bps"] > 0
               for r in rows), rows

# ---------------------------------------------------- regímenes de libro

def test_bounce_absorption_fires_ask_heavy_book():
    """El caso que motivó la calibración: vendedores agresivos que NO mueven
    el precio con el libro 5:1 ask-heavy (exactamente el VAL de PEPE). Con
    absorb_book_any=True (shadow) DEBE disparar y etiquetar ask_heavy."""
    v = view(price=100.0,
             levels=[D.Level(price=99.995, kind="support")],
             flow={"buy_z": 0.0, "sell_z": 3.0},
             book=BookMetrics(mid=100.0, best_bid=99.99, best_ask=100.01,
                              bid_depth=2, ask_depth=10, imbalance=-0.67),
             price_change_bps_window={15: -1.0})
    cfg = D.Config(absorb_book_any=True)
    e = D.bounce_absorption(v, cfg)
    assert e is not None and e.name == "BOUNCE_ABSORPTION"
    assert e.features["book_regime"] == "ask_heavy"
    assert e.features["imbalance"] == -0.67


def test_bounce_absorption_silent_ask_heavy_in_live_mode():
    """Live mantiene el gate estricto hasta que el estudio lo cambie."""
    v = view(price=100.0,
             levels=[D.Level(price=99.995, kind="support")],
             flow={"buy_z": 0.0, "sell_z": 3.0},
             book=BookMetrics(mid=100.0, best_bid=99.99, best_ask=100.01,
                              bid_depth=2, ask_depth=10, imbalance=-0.67),
             price_change_bps_window={15: -1.0})
    assert D.bounce_absorption(v, D.Config(absorb_book_any=False)) is None


def test_fvg_retest_fires_ask_heavy_book():
    """Un dip DENTRO del gap es un momento de presión vendedora por definición."""
    v = view(price=100.2,
             levels=[D.Level(price=0, kind="fvg_bull", bottom=100.0, top=100.4)],
             flow={"buy_z": 0.5, "sell_z": 0.0},
             book=BookMetrics(mid=100.0, best_bid=99.99, best_ask=100.01,
                              bid_depth=2, ask_depth=10, imbalance=-0.67))
    e = D.fvg_retest(v, D.Config(fvg_book_any=True))
    assert e is not None and e.name == "FVG_RETEST"
    assert e.features["book_regime"] == "ask_heavy"
    assert D.fvg_retest(v, D.Config(fvg_book_any=False)) is None
