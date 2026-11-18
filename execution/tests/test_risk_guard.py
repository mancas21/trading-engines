"""Regresion FAIL-CLOSED de risk_guard (2026-11-17).

El audit P0 encontro el patron peligroso: cuando no se podia leer el mark de
una posicion, el codigo viejo hacia `except: mark = 0.0`. El bruto se encogia,
`gross <= MAX_GROSS_USD` daba True y el bot autorizaba riesgo nuevo justo
cuando NO sabia cuanto riesgo habia. Estos tests fijan la regla contraria:
sin precio no hay exposicion verificable, y sin verificacion no se autoriza.

Run:  python -m pytest execution/tests/test_risk_guard.py -v
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import binance_testnet as BTL  # noqa: E402
import neutral_bot as NB  # noqa: E402
import risk_guard as RG  # noqa: E402


class _Account:
    """Cliente sin red: solo lo que risk_guard necesita (positions + mark)."""

    def __init__(self, positions, marks=None, boom_on_mark=False):
        self._positions = positions
        self._marks = marks or {}
        self._boom = boom_on_mark

    def positions(self):
        return self._positions

    def mark_price(self, symbol):
        if self._boom:
            raise BTL.SafetyError(f"sin precio para {symbol}")
        return self._marks.get(symbol, 0.0)


def _pos(symbol, amt, mark=None, upnl=0.0):
    p = {"symbol": symbol, "positionAmt": str(amt), "unRealizedProfit": str(upnl)}
    if mark is not None:
        p["markPrice"] = str(mark)
    return p


# ── el corazon del fail-closed ──────────────────────────────────────────────
def test_unreadable_mark_is_unknown_exposure_not_zero():
    """Un mark ilegible deja la exposicion DESCONOCIDA.

    Antes: `except: mark = 0.0` -> gross 0 -> `ok=True` -> el bot colocaba.
    """
    c = _Account([_pos("BTCUSDT", 0.5, mark=None)], boom_on_mark=True)
    snap = RG.exposure_snapshot(c)
    assert snap["unpriced"] == ["BTCUSDT"]
    assert snap["verifiable"] is False
    assert snap["ok"] is False
    assert "DESCONOCIDA" in snap["why"]
    # y el paso previo a la orden se niega, no devuelve un dict "ok"
    with pytest.raises(RG.RiskBlocked):
        RG.require_exposure_ok(c)


def test_non_finite_mark_is_unknown_exposure():
    """nan / inf no son precios: tampoco autorizan."""
    c = _Account([_pos("ETHUSDT", 2.0, mark="nan")])
    snap = RG.exposure_snapshot(c)
    assert snap["verifiable"] is False
    assert snap["ok"] is False
    with pytest.raises(RG.RiskBlocked):
        RG.require_exposure_ok(c)


def test_unreadable_positions_raises_instead_of_looking_flat():
    """Si positions() falla, el contrato es lanzar. El llamador lo trata como
    'exposicion desconocida'; convertirlo aqui en {} seria mentir."""
    class _Boom(_Account):
        def positions(self):
            raise BTL.SafetyError("HTTP 503")

    with pytest.raises(BTL.SafetyError):
        RG.exposure_snapshot(_Boom([]))


def test_positions_returning_non_list_is_not_ok():
    class _Weird(_Account):
        def positions(self):
            return {"error": "rate limited"}

    snap = RG.exposure_snapshot(_Weird([]))
    assert snap["verifiable"] is False
    assert snap["ok"] is False


# ── topes ───────────────────────────────────────────────────────────────────
def test_gross_over_cap_blocks_even_when_net_is_flat():
    """Dos lados opuestos: neto ~0 pero bruto por encima del envelope. El
    bruto es el que mide el riesgo correlacionado que se puede abrir."""
    half = RG.MAX_GROSS_USD * 0.6      # gross = 1.2 * tope
    c = _Account([_pos("AAAUSDT", half, mark=1.0),
                  _pos("BBBUSDT", -half, mark=1.0)])
    snap = RG.exposure_snapshot(c)
    assert snap["verifiable"] is True
    assert snap["gross"] > RG.MAX_GROSS_USD
    assert abs(snap["net"]) < 1e-6
    assert snap["ok"] is False
    assert "gross" in snap["why"]
    with pytest.raises(RG.RiskBlocked):
        RG.require_exposure_ok(c)


def test_net_over_cap_blocks_with_gross_inside_the_envelope():
    """Un libro direccional puede caber en bruto y pasarse de neto."""
    long_n = RG.MAX_NET_USD * 1.2
    short_n = 1.0
    if long_n + short_n > RG.MAX_GROSS_USD:      # configuracion inesperada
        pytest.skip("MAX_NET demasiado cerca de MAX_GROSS para el caso")
    c = _Account([_pos("AAAUSDT", long_n, mark=1.0),
                  _pos("BBBUSDT", -short_n, mark=1.0)])
    snap = RG.exposure_snapshot(c)
    assert snap["gross"] <= RG.MAX_GROSS_USD
    assert abs(snap["net"]) > RG.MAX_NET_USD
    assert snap["ok"] is False
    assert "net" in snap["why"]


def test_flat_account_inside_caps_is_ok():
    c = _Account([_pos("AAAUSDT", 0.0, mark=1.0),
                  _pos("BBBUSDT", 100.0, mark=1.0)])
    snap = RG.exposure_snapshot(c)
    assert snap["verifiable"] is True
    assert snap["ok"] is True
    assert "AAAUSDT" not in snap["positions"]      # amt 0 no es posicion
    assert snap["gross"] == pytest.approx(100.0)


# ── una sola politica: el bot importa los topes, no los redefine ────────────
def test_bot_inventory_caps_are_the_guard_caps():
    """Antes: bot 400/1.200 inline vs risk_guard 600/400. Dos cerebros.
    Ahora el bot importa las constantes: si divergen, este test cae."""
    assert NB.MAX_INVENTORY_USD_PER_SYMBOL == RG.MAX_INVENTORY_USD_PER_SYMBOL
    assert NB.MAX_INVENTORY_USD_TOTAL == RG.MAX_INVENTORY_USD_TOTAL
    # el bruto de la cuenta ES el inventario total: alias, no segundo numero
    assert RG.MAX_GROSS_USD == RG.MAX_INVENTORY_USD_TOTAL
    assert RG.exposure_snapshot(_Account([]))["caps"]["total"] == RG.MAX_INVENTORY_USD_TOTAL
