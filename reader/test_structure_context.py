"""El bloque de estructura: CONTEXTO auditable, nunca una señal.

MEDIDO (2026-10-15) sobre 928 barras de 5m de JUPUSDT, mismo horizonte que el
ledger (12 barras = 60 min): las señales de estructura ALCISTA no tienen edge —
ruptura de pivote t=+0,13; EMA9>EMA21 con cierre encima t=-0,14; cruce de EMA al
alza t=-0,92. El t=+4,13 del lado corto es la DERIVA del mercado, no predicción
(el mismo error que puso la regla 6 en cuarentena).

Por eso el bloque NO decide el sesgo. Existe por el defecto que SÍ es real: que
el silencio del motor fuera INAUDITABLE. Durante el rally de hoy el motor dijo 86
NEUTRAL seguidos y no había forma de distinguir "lo miré y no hay señal" de "no
lo miré".

Contratos que fija este fichero:
  1. El bloque NO cambia el sesgo. Ni con estructura alcista clara, ni sin
     estructura, ni con el chart caído.
  2. Va etiquetado `validated: false` y con su nota: nadie puede confundirlo con
     una señal validada.
  3. Declara la TEMPORALIDAD. Un EMA9 en 1m y en 4h no significan lo mismo, y el
     chart cambió de TF durante la propia sesión en que se escribió esto.
  4. Si el chart no responde, el bloque lo dice y el veredicto sigue saliendo.
  5. El valor no puede romper el registro del ledger.
"""
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scanner-engine"))

import live_reader as L                   # noqa: E402


def _fake_tv(**over):
    """Lectura de estructura en el formato NUEVO (klines_binance)."""
    base = {"ok": True, "source": "klines_binance", "symbol": "1000PEPEUSDT",
            "tf": "5m", "close": 0.31, "ema9": 0.32, "ema21": 0.31,
            "ema50": 0.30, "ema200": 0.29,
            "stack": "BULLISH stack", "rsi14": 61.0, "rsi6": 66.0,
            "atr14": 0.01, "position_in_range": "78.0%",
            "volume_ratio": 1.20, "read_ts": 1_700_000_000.0}
    base.update(over)
    return base


def test_structure_block_is_labelled_unvalidated_and_carries_its_note(monkeypatch):
    monkeypatch.setattr(L, "src_tradingview", lambda **k: _fake_tv())
    b = L.structure_block()
    assert b["ok"] is True
    assert b["validated"] is False, (
        "el bloque se presenta como validado: es contexto, y su edge alcista "
        "esta medido como nulo")
    assert "NO señal" in b["note"] or "NO se\u00f1al" in b["note"]


def test_structure_block_declares_the_timeframe(monkeypatch):
    """Un EMA9 en 1m y en 4h no son lo mismo. El chart CAMBIO de TF durante la
    sesion en que se escribio esto (240 -> 1), asi que la temporalidad tiene que
    viajar con el numero o el numero no significa nada."""
    monkeypatch.setattr(L, "src_tradingview", lambda **k: _fake_tv(tf="1"))
    b = L.structure_block()
    assert b["tf"] == "1", "el bloque perdio la temporalidad del chart"


def test_a_dead_chart_does_not_break_the_block(monkeypatch):
    monkeypatch.setattr(L, "src_tradingview",
                        lambda **k: {"ok": False, "error": "CDP no disponible"})
    b = L.structure_block()
    assert b["ok"] is False and b["validated"] is False
    assert "CDP" in (b.get("error") or ""), (
        "un chart caido tiene que DECIRSE, no devolver un bloque vacio que "
        "parezca 'sin señal'")


def test_a_transient_failure_is_declared_not_imputed(monkeypatch):
    """2026-11-14 (klines): un fallo de lectura SE DECLARA — la regla del
    modulo es que una fuente ausente nunca se imputa. La vieja politica de
    servir la ultima lectura buena era parte del contrato CDP/node; con un
    fetch de klines el reintento llega en el siguiente ciclo."""
    monkeypatch.setattr(L, "_KL_CACHE", {})
    monkeypatch.setattr(L, "_klines_structure_read",
                        lambda symbol=None: {"ok": False, "error": "klines no disponibles: TimeoutError"})
    out = L.src_tradingview(force=True)
    assert out.get("ok") is False and "klines" in (out.get("error") or "")


def test_a_dead_target_symbol_is_declared(monkeypatch):
    monkeypatch.setattr(L, "_KL_CACHE", {})
    monkeypatch.setattr(L, "_target_symbol", lambda: None)
    out = L.src_tradingview(force=True)
    assert out.get("ok") is False and "objetivo" in (out.get("error") or "")


def test_the_cache_serves_within_the_window_without_refetching(monkeypatch):
    monkeypatch.setattr(L, "_KL_CACHE", {})
    calls = {"n": 0}

    def counted(symbol=None):
        calls["n"] += 1
        return _fake_tv()

    monkeypatch.setattr(L, "_klines_structure_read", counted)
    L.src_tradingview(force=True)
    L.src_tradingview()
    L.src_tradingview()
    assert calls["n"] == 1, (
        f"se refetcheo klines {calls['n']} veces dentro de la ventana: el cache "
        f"en memoria no sirve y el ritmo de 30s del reader paga el fetch")
