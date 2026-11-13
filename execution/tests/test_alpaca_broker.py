"""Tests offline del cliente y del router Alpaca.

No tocan la red: las funciones de sizing/guard/validacion son puras y el
cliente se construye con un cli falso y asset cacheado a mano, sin llamar al
binario. Lo que se prueba es la politica, que es donde un error cuesta dinero:

  * dry-run por defecto y gate ALLOW_ALPACA_ORDERS (la regla 1 del modulo).
  * lista blanca y tope de nocional.
  * sizing por riesgo con shares enteras (minimo de riesgo/nocional/caja).
  * stop/TP del lado correcto del precio de referencia.
  * el envelope de error del CLI se detecta aunque exit sea 0.

Run:  python -m pytest execution/tests/test_alpaca_broker.py -v
"""
from __future__ import annotations

import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))          # execution/

import alpaca_broker as AB  # noqa: E402

FAKE_CLI = "C:/falso/alpaca.exe"


def mkclient(**kw) -> AB.AlpacaClient:
    base = dict(dry_run=True, allow_symbols=["AAPL", "SPY"], cli=FAKE_CLI)
    base.update(kw)
    return AB.AlpacaClient(**base)


AAPL_ACTIVE = {"symbol": "AAPL", "tradable": True, "status": "active",
               "fractionable": True, "shortable": True}


# ─────────────────────────────────────────────────── parseo del CLI ──
def test_error_envelope_is_detected_even_with_exit_zero():
    with pytest.raises(AB.AlpacaError, match="authentication required"):
        AB.parse_cli_output('{"code": 0, "error": "authentication required", '
                            '"hint": "", "status": 0}', 1)


def test_exit_nonzero_without_json_is_an_error():
    with pytest.raises(AB.AlpacaError):
        AB.parse_cli_output("algo raro", 2)


def test_plain_json_passes_through():
    assert AB.parse_cli_output('{"a": 1}', 0) == {"a": 1}
    assert AB.parse_cli_output("[1, 2]", 0) == [1, 2]


# ─────────────────────────────────────────────────────── guardas ──
def test_mercado_cerrado_no_entra():
    ok, why = AB.entry_guard({"is_open": False, "next_open": "2026-10-30T09:30:00-04:00"})
    assert not ok
    assert "next_open" in why


def test_mercado_cerrado_con_allow_closed_pasa():
    ok, why = AB.entry_guard({"is_open": False}, allow_closed=True)
    assert ok


def test_mercado_abierto_entra():
    ok, _ = AB.entry_guard({"is_open": True})
    assert ok


def test_stop_del_lado_equivocado_se_rechaza():
    # LONG: el stop debe quedar POR DEBAJO del ref
    assert AB.stop_side_check("LONG", 101.0, 100.0)[0] is False
    assert AB.stop_side_check("LONG", 100.0, 100.0)[0] is False
    assert AB.stop_side_check("LONG", 99.0, 100.0)[0] is True
    # SHORT: espejo
    assert AB.stop_side_check("SHORT", 99.0, 100.0)[0] is False
    assert AB.stop_side_check("SHORT", 101.0, 100.0)[0] is True


def test_tp_del_lado_equivocado_se_rechaza():
    assert AB.stop_side_check("LONG", 95.0, 100.0, take_profit=98.0)[0] is False
    assert AB.stop_side_check("LONG", 95.0, 100.0, take_profit=110.0)[0] is True
    assert AB.stop_side_check("SHORT", 105.0, 100.0, take_profit=102.0)[0] is False
    assert AB.stop_side_check("SHORT", 105.0, 100.0, take_profit=90.0)[0] is True


def test_side_invalido():
    assert AB.stop_side_check("LATERAL", 95.0, 100.0)[0] is False
    with pytest.raises(AB.SafetyError):
        AB.bracket_args(symbol="AAPL", qty=1, side="LATERAL", stop=95.0)


# ──────────────────────────────────────────────────────── sizing ──
def test_el_riesgo_manda_cuando_es_el_minimo():
    # 100k equity, 0.5% riesgo, stop a 2 USD del ref -> 250 shares
    # (nocional 30k = 300 shares y caja 500 shares quedan por encima)
    qty, binding = AB.size_order(equity=100_000, cash=50_000, price=100.0,
                                 stop=98.0, risk_pct=0.5, max_notional=30_000)
    assert binding == "riesgo"
    assert qty == pytest.approx(250.0)


def test_el_nocional_manda():
    qty, binding = AB.size_order(equity=100_000, cash=50_000, price=100.0,
                                 stop=98.0, risk_pct=0.5, max_notional=1_000)
    assert binding == "nocional"
    assert qty == pytest.approx(10.0)


def test_la_caja_manda():
    qty, binding = AB.size_order(equity=100_000, cash=500.0, price=100.0,
                                 stop=98.0, risk_pct=0.5, max_notional=10_000)
    assert binding == "caja"
    assert qty == pytest.approx(5.0)


def test_distancia_nula_es_error():
    with pytest.raises(AB.SafetyError):
        AB.size_order(equity=100_000, cash=50_000, price=100.0, stop=100.0,
                      risk_pct=0.5, max_notional=10_000)


def test_round_qty_entero_y_fraccional():
    assert AB.round_qty(12.9734, fractionable=False) == 12.0
    assert AB.round_qty(0.9999, fractionable=False) == 0.0
    assert AB.round_qty(12.97349, fractionable=True) == 12.9734
    assert AB.round_qty(12.99999, fractionable=True) == 12.9999


# ──────────────────────────────────────────────────── check_order ──
def test_whitelist_bloquea():
    c = mkclient()
    with pytest.raises(AB.SafetyError, match="ALLOW_ALPACA_SYMBOLS"):
        c.check_order("TSLA", "BUY", 1, ref_price=100.0)


def test_asset_no_tradable_bloquea():
    c = mkclient()
    c._assets["AAPL"] = {"tradable": False, "status": "active"}
    with pytest.raises(AB.SafetyError, match="tradable"):
        c.check_order("AAPL", "BUY", 1, ref_price=100.0)


def test_tope_de_nocional_bloquea():
    c = mkclient(max_notional_usd=100.0)
    c._assets["AAPL"] = dict(AAPL_ACTIVE)
    with pytest.raises(AB.SafetyError, match="MAX_ALPACA_NOTIONAL_USD"):
        c.check_order("AAPL", "BUY", 1, ref_price=350.0)  # 350 > 100


def test_qty_entera_y_minimo_una_share():
    c = mkclient(max_notional_usd=1_000_000)
    c._assets["AAPL"] = dict(AAPL_ACTIVE)
    chk = c.check_order("AAPL", "BUY", 2.7, ref_price=100.0)
    assert chk["qty"] == 2.0
    with pytest.raises(AB.SafetyError, match="redondea a 0"):
        c.check_order("AAPL", "BUY", 0.5, ref_price=100.0)


# ───────────────────────────────────────────────────── bracket_args ──
def test_bracket_args_long_built_correctly():
    args = AB.bracket_args(symbol="AAPL", qty=10, side="LONG", stop=95.5,
                           take_profit=112.0, client_id="qae-ent-123")
    assert args[args.index("--symbol") + 1] == "AAPL"
    assert args[args.index("--qty") + 1] == "10"
    assert args[args.index("--side") + 1] == "buy"
    assert args[args.index("--order-class") + 1] == "bracket"
    assert args[args.index("--stop-loss") + 1] == '{"stop_price": 95.5}'
    assert args[args.index("--take-profit") + 1] == '{"limit_price": 112.0}'
    assert args[args.index("--client-order-id") + 1] == "qae-ent-123"


def test_bracket_args_short_sin_tp():
    args = AB.bracket_args(symbol="SPY", qty=3, side="SHORT", stop=705.0)
    assert args[args.index("--side") + 1] == "sell"
    assert "--take-profit" not in args
    assert args[args.index("--stop-loss") + 1] == '{"stop_price": 705.0}'


# ─────────────────────────────────────────────────────── find_cli ──
def test_find_cli_env_override(tmp_path):
    fake = tmp_path / "alpaca.exe"
    fake.write_bytes(b"")
    import os
    old = os.environ.get("ALPACA_CLI")
    os.environ["ALPACA_CLI"] = str(fake)
    try:
        assert AB.find_cli() == fake
        os.environ["ALPACA_CLI"] = str(tmp_path / "nope.exe")
        with pytest.raises(AB.SafetyError):
            AB.find_cli()
    finally:
        if old is None:
            os.environ.pop("ALPACA_CLI", None)
        else:
            os.environ["ALPACA_CLI"] = old


def test_find_cli_sin_binario(tmp_path, monkeypatch):
    monkeypatch.setattr(AB, "DEFAULT_CLI", tmp_path / "nada.exe")
    monkeypatch.setattr(AB.shutil, "which", lambda _: None)
    import os
    monkeypatch.setenv("ALPACA_CLI", "")
    with pytest.raises(AB.SafetyError):
        AB.find_cli()
