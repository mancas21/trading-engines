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
    base = {"ok": True, "source": "tradingview", "symbol": "BINANCE:JUPUSDT.P",
            "tf": "240", "close": "0.3100", "ema9": "0.3200", "ema21": "0.3100",
            "ema50": "0.3000", "ema200": "0.2900",
            "stack": "BULLISH stack (+3.2%)", "rsi14": "61.0", "rsi6": "66.0",
            "atr14": "0.0100", "position_in_range": "78.0%",
            "volume_ratio": "1.20", "read_ts": 1_700_000_000.0}
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


def test_the_structure_cache_keeps_the_last_good_read_on_a_transient_failure(tmp_path, monkeypatch):
    """Un hipo de node/CDP no puede borrar la ultima lectura buena."""
    monkeypatch.setattr(L, "_TV_CACHE", tmp_path / "tv.json")
    calls = {"n": 0}

    def flaky(**k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_tv()
        return {"ok": False, "error": "timeout"}

    monkeypatch.setattr(L, "_tradingview_read", flaky)
    first = L.src_tradingview(force=True)
    assert first["ok"] is True

    second = L.src_tradingview(force=True)
    assert second.get("ok") is True, (
        "un fallo puntual borro la ultima lectura buena: el veredicto se queda "
        "sin estructura por un hipo")
    assert second.get("stale_error") == "timeout", (
        "la lectura reciclada no se marca: pareceria fresca")


def test_the_cache_serves_within_the_window_without_calling_node(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_TV_CACHE", tmp_path / "tv.json")
    calls = {"n": 0}

    def counted(**k):
        calls["n"] += 1
        return _fake_tv()

    monkeypatch.setattr(L, "_tradingview_read", counted)
    L.src_tradingview(force=True)
    L.src_tradingview(max_age_s=240.0)
    L.src_tradingview(max_age_s=240.0)
    assert calls["n"] == 1, (
        f"se lanzo node {calls['n']} veces: el cache no sirve y el ritmo de 30s "
        f"del reader queda a merced de un subproceso de hasta 120s")
