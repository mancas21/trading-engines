"""Tests del motor sintético de flujo (synthetic_flow.py).

Lo que se prueba es la mecánica donde un error cuesta señal falsa: el signo
del delta (flag m del trade), el bucketing de barras 1m, el POC/VA por
expansión 70%, la ventana de tape y la paridad del schema con el exporter.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from synthetic_flow import SyntheticFlow, _va_from_levels  # noqa: E402


def trade(f, ts_ms, price, qty, side, price_str=None):
    f.on_trade(price, qty, "buy" if side == "buy" else "sell", ts_ms, price_str)


def test_delta_sign_matches_aggressor():
    """m=True (buyer es maker) => el agresor VENDE. El caller ya traduce a
    side='sell'; aqui se verifica que el respete el side que le llega."""
    f = SyntheticFlow("X")
    trade(f, 60_000_000, 100.0, 5.0, "buy")
    trade(f, 60_001_000, 100.0, 2.0, "sell")
    snap = f.snapshot()
    assert snap["volume"]["delta"] == 3.0
    assert snap["volume"]["total"] == 7.0


def test_bar_bucketing_and_rollover():
    """Trades en minutos distintos => barras distintas; la barra cerrada
    aparece en closed_bars con su delta."""
    f = SyntheticFlow("X")
    trade(f, 60_000_000, 100.0, 5.0, "buy")      # barra 60_000_000
    trade(f, 61_500_000, 101.0, 3.0, "sell")     # barra siguiente: cierra la anterior
    assert len(f.closed) == 1
    assert f.closed[0]["delta"] == 5.0 and f.closed[0]["volume"] == 5.0
    snap = f.snapshot()
    assert snap["bar_index"] == 1
    assert snap["volume"]["delta"] == -3.0       # barra viva actual


def test_va_expansion_70pct():
    """POC = nivel de mayor volumen; VA = expansion 70% hacia el vecino mayor."""
    levels = {"100": [1.0, 0.0], "101": [5.0, 0.0], "102": [3.0, 0.0], "103": [1.0, 0.0]}
    poc, vah, val = _va_from_levels(levels)
    assert poc == 101.0
    # total 10 -> target 7: poc(5) + mayor vecino 3 (102) = 8 >= 7
    assert val == 101.0 and vah == 102.0


def test_va_empty():
    assert _va_from_levels({}) == (None, None, None)


def test_tape_window_and_session_vwap():
    f = SyntheticFlow("X", tape_n=3)
    for i in range(5):
        trade(f, 60_000_000 + i * 1000, 100.0 + i, 1.0, "buy")
    snap = f.snapshot()
    assert snap["tape"]["prints_sampled"] == 3          # ventana de 30 prints, 5 trades => cap
    assert snap["tape"]["buy_count_pct"] == 100.0
    # vwap = suma(p*q)/suma(q) con q=1 => media de precios
    assert abs(snap["vwap"] - (100.0 + 101.0 + 102.0 + 103.0 + 104.0) / 5) < 1e-9


def test_session_reset_on_new_day():
    f = SyntheticFlow("X")
    trade(f, 0, 100.0, 10.0, "buy")                       # dia 0
    trade(f, 86_400_000, 200.0, 5.0, "buy")               # dia siguiente: reset
    snap = f.snapshot()
    assert abs(snap["vwap"] - 200.0) < 1e-9               # el VWAP viejo no contamina


def test_snapshot_has_exporter_parity_fields():
    f = SyntheticFlow("PEPE")
    trade(f, 60_000_000, 0.004, 1000.0, "buy")
    snap = f.snapshot(dom={"imbalance_pct": 10.0})
    for k in ("schema_version", "source", "ticker", "timestamp", "price",
              "volume", "delta", "vwap", "footprint", "session_value_area", "dom"):
        assert k in snap, k
    assert snap["source"] == "synthetic_binance"
    assert snap["footprint"]["note"].startswith("delta por nivel")
    assert snap["dom"]["imbalance_pct"] == 10.0
