"""Tests offline de las decisiones del auto-router y del reconciliador.

No tocan la red: `risk_for` y `decision` son funciones puras, y los helpers del
reconciliador solo formatean/parsean. Lo que se prueba es la politica, que es
donde un error cuesta dinero:

  * `conc_passes` ausente NUNCA se lee como aprobado (haircut, no confianza).
  * una entrada tardia no se persigue.
  * una posicion alineada no se toca; una contraria se revierte.
  * sin senal y con posicion se cierra.

Run:  python -m pytest execution/tests/test_automation_logic.py -v
"""
from __future__ import annotations

import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))          # execution/
sys.path.insert(0, str(HERE.parents[2] / "scanner-engine"))

import auto_router as AR  # noqa: E402
import binance_testnet as BTL  # noqa: E402
import reconcile as RC  # noqa: E402


def test_concentration_false_and_unknown_get_the_same_haircut():
    assert AR.risk_for({"conc_passes": True}) == (AR.RISK_PCT, "conc_passes=true")
    risk, why = AR.risk_for({"conc_passes": False})
    assert risk == AR.RISK_PCT * AR.CONC_HAIRCUT
    assert "x0.5" in why
    risk, why = AR.risk_for({})
    assert risk == AR.RISK_PCT * AR.CONC_HAIRCUT
    assert "ausente" in why


def test_flat_with_a_fresh_signal_opens():
    d = AR.decision(sig={"direction": 1, "trusted": True, "bars_held": 0},
                    pos_amt=0.0, max_lag=24)
    assert d["action"] == "open"


def test_late_entry_is_not_chased():
    d = AR.decision(sig={"direction": -1, "trusted": True, "bars_held": 25},
                    pos_amt=0.0, max_lag=24)
    assert d["action"] == "skip"
    assert "tardia" in d["why"]


def test_aligned_position_is_kept_even_if_old():
    d = AR.decision(sig={"direction": 1, "trusted": True, "bars_held": 186},
                    pos_amt=12.5, max_lag=24)
    assert d["action"] == "keep"


def test_flat_signal_closes_and_no_signal_waits():
    assert AR.decision(sig={"direction": 0, "trusted": True, "bars_held": None},
                       pos_amt=2.0, max_lag=24)["action"] == "close"
    assert AR.decision(sig={"direction": 0, "trusted": True, "bars_held": None},
                       pos_amt=0.0, max_lag=24)["action"] == "wait"


def test_opposite_position_reverses():
    d = AR.decision(sig={"direction": 1, "trusted": True, "bars_held": 0},
                    pos_amt=-3.0, max_lag=24)
    assert d["action"] == "reverse"


def test_opposite_but_stale_closes_without_chasing():
    """Cerrar la posicion contraria es obligatorio; abrir la pata nueva cuando
    el engine lleva 60 barras en esa direccion es perseguir, y no se hace."""
    d = AR.decision(sig={"direction": 1, "trusted": True, "bars_held": 60},
                    pos_amt=-3.0, max_lag=24)
    assert d["action"] == "close"
    assert "tardia" in d["why"]


def test_untrusted_window_never_trades():
    d = AR.decision(sig={"direction": 1, "trusted": False, "bars_held": 0},
                    pos_amt=0.0, max_lag=24)
    assert d["action"] == "skip"


def test_dust_position_counts_as_flat():
    d = AR.decision(sig={"direction": 1, "trusted": True, "bars_held": 1},
                    pos_amt=1e-13, max_lag=24)
    assert d["action"] == "open"


def test_entry_stop_uses_the_engine_level_for_a_late_open():
    """Entrar 24 barras despues no debe reanclar el stop al precio actual: la
    invalidacion de la estrategia es el nivel del engine. ETH 2026-10-25:
    engine stop 2394.85 con mark 2346.29; el mark-based daba 2400.22."""
    sig = {"direction": -1, "stop": 2394.8486, "sl_dist": 53.9257,
           "sl_dist_last": 43.2261}
    stop, why = AR.entry_stop(sig, 2346.29, "open")
    assert stop == 2394.8486 and why == ""


def test_entry_stop_refuses_if_price_already_crossed_it():
    """Un SHORT cuyo stop ya esta por debajo del precio entraria directo al
    stop. Mejor no abrir."""
    sig = {"direction": -1, "stop": 2394.85, "sl_dist": 53.9}
    stop, why = AR.entry_stop(sig, 2400.0, "open")
    assert stop is None
    assert "ya cruzo" in why
    sig_long = {"direction": 1, "stop": 100.0, "sl_dist": 2.0}
    assert AR.entry_stop(sig_long, 99.0, "open")[0] is None


def test_reverse_ignores_the_old_positions_stop():
    """En un reverse el stop guardado es de la posicion contraria: la pata
    nueva usa la distancia ATR desde el mark."""
    sig = {"direction": 1, "stop": 90.0, "sl_dist": 2.0}
    stop, _ = AR.entry_stop(sig, 100.0, "reverse")
    assert stop == 98.0


def test_entry_stop_falls_back_and_rejects_nan():
    sig = {"direction": 1, "sl_dist": 2.0}
    assert AR.entry_stop(sig, 100.0, "open")[0] == 98.0
    nan = {"direction": 1, "sl_dist": float("nan"), "sl_dist_last": None}
    assert AR.entry_stop(nan, 100.0, "open")[0] is None


def test_reconcile_parses_state_timestamps_and_ignores_junk():
    ms = RC._ms("2026-10-25T16:02:31Z")
    assert ms == 1792944151000
    assert RC._ms(None) == 0
    assert RC._ms("no-es-fecha") == 0


def test_reconcile_recognises_stops_by_either_field_name():
    """La API devuelve `orderType`; artefactos viejos usan `type`."""
    algo = [{"orderType": "STOP_MARKET", "algoId": 1},
            {"type": "stop_market", "algoId": 2},
            {"orderType": "TAKE_PROFIT_MARKET", "algoId": 3}]
    assert [a["algoId"] for a in RC._stops(algo)] == [1, 2]
    assert [a["algoId"] for a in RC._tps(algo)] == [3]


class _FakeClient(BTL.TestnetClient):
    """Cliente sin red: solo filtros y mark. Para probar la guarda local."""

    def __init__(self, max_notional: float = 100.0):
        super().__init__(dry_run=True)
        self.max_notional = max_notional
        self.allow_symbols = ["XRPUSDT"]

    def rules(self, symbol: str) -> dict:
        return {"status": "TRADING", "tick": 0.0001, "step": 0.1,
                "min_qty": 0.1, "max_qty": 1_000_000.0,
                "min_notional": 5.0, "price_precision": 4, "qty_precision": 1,
                "percent_price": {"down": 0.95, "up": 1.05}}

    def mark_price(self, symbol: str) -> float:
        return 1.30


def test_close_orders_are_exempt_from_the_notional_cap():
    """El tope limita exposicion NUEVA. Si tambien bloqueara los cierres, una
    posicion mayor que el tope no se podria aplanar (ni flatten ni el stop de
    emergencia)."""
    c = _FakeClient(max_notional=100.0)
    with pytest.raises(BTL.SafetyError):
        c.check_order("XRPUSDT", "SELL", 1000.0)          # 1300 USDT, abre
    ok = c.check_order("XRPUSDT", "SELL", 1000.0, reduce_only=True)
    assert ok["qty"] == 1000.0
    assert ok["notional"] > 100.0

    # un cierre por debajo del minimo notional tambien debe poder salir
    dust = c.check_order("XRPUSDT", "SELL", 1.0, reduce_only=True)
    assert dust["notional"] < 5.0


def test_exit_classification_covers_gaps_both_sides():
    """Un stop con hueco puede llenar por debajo del nivel; sigue siendo stop.
    Un cierre a mercado lejos del nivel NO se atribuye al stop."""
    # LONG: stop 100, salida 99.5 (hueco) -> stop; 101 -> no es stop
    assert RC.classify_exit(99.5, 100.0, None, True) == "stop"
    assert RC.classify_exit(101.0, 100.0, None, True) == "otro (manual o mercado)"
    # SHORT: stop 100, salida 100.6 -> stop; 99 -> no
    assert RC.classify_exit(100.6, 100.0, None, False) == "stop"
    assert RC.classify_exit(99.0, 100.0, None, False) == "otro (manual o mercado)"
    # TP en ambos lados
    assert RC.classify_exit(120.0, 90.0, 120.0, True) == "take_profit"
    assert RC.classify_exit(80.0, 110.0, 80.0, False) == "take_profit"
    # sin niveles no se inventa una causa
    assert RC.classify_exit(123.0, None, None, True) == "otro (manual o mercado)"
