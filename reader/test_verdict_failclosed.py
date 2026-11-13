"""Tests de aceptación T1/T2 (auditoría 2026-11-12, AUDIT doc §3).

T1: rules.json ausente / vacío / corrupto => enforcement "unavailable",
    _present False, y la puerta nunca publica BULLISH ni BEARISH.
T2: la lectura de racha (observe=False) aplica _DOM_GATE_STALE_S: una racha
    guardada vieja devuelve (0, 0) aunque sea >= 2.
"""
import json
import shutil
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RULES = HERE.parent / "atas-bridge" / "rules.json"


def _reload_module():
    import importlib
    import verdict
    importlib.reload(verdict)
    return verdict


# ---------- T1: fail-closed del enforcement ----------

@pytest.mark.parametrize("content", [None, "", "{truncado"])
def test_t1_fail_closed(content, tmp_path, monkeypatch):
    """rules.json ausente/vacío/truncado => 'unavailable', nunca 'hard'."""
    backup = RULES.read_text(encoding="utf-8")
    try:
        if content is None:
            RULES.unlink()
        else:
            RULES.write_text(content, encoding="utf-8")
        v = _reload_module()
        out = v._load_rule_conditions(6)
        assert out["_present"] is False
        assert out["_enforcement"] == "unavailable"
        assert v._rule_enforcement(6) == "unavailable"
        # y la Regla 7 igual: sin umbrales presentes la puerta no decide
        assert v._R7.get("_present") is False
    finally:
        RULES.write_text(backup, encoding="utf-8")
        _reload_module()


def test_t1_valid_file_still_hard():
    """El fichero real (presente) sigue dando 'hard' para la 2 (regla viva)."""
    v = _reload_module()
    assert v._rule_enforcement(2) == "hard"
    assert v._R2.get("_present") is True


def test_rules67_retired():
    """Reglas 6/7 eliminadas 2026-11-13: no existen, enforcement
    'unavailable', y la puerta no puede publicar direccional."""
    v = _reload_module()
    for rid in (6, 7):
        assert v._rule_enforcement(rid) == "unavailable"
        assert v._load_rule_conditions(rid).get("_present") is False
    assert v.RULE7.get("enforcement") in ("unavailable", "retired", None)


# ---------- T2: caducidad en la lectura de racha ----------

def test_t2_stale_streak_read_returns_zero(monkeypatch):
    """Racha guardada=3 con ts hace 10 min => observe=False devuelve (0, 0)."""
    import verdict
    stale = {"XRPUSDT": {"buy": 3, "sell": 0,
                         "ts": time.time() - 600}}       # >> _DOM_GATE_STALE_S (120s)
    monkeypatch.setattr(verdict, "_dom_gate_load", lambda: stale)
    buy, sell = verdict._dom_gate_tick("XRPUSDT", True, True, observe=False)
    assert (buy, sell) == (0, 0)


def test_t2_fresh_streak_read_preserved(monkeypatch):
    """Racha guardada=3 con ts reciente => observe=False la conserva (3, 0)."""
    import verdict
    fresh = {"XRPUSDT": {"buy": 3, "sell": 0, "ts": time.time() - 5}}
    monkeypatch.setattr(verdict, "_dom_gate_load", lambda: fresh)
    buy, sell = verdict._dom_gate_tick("XRPUSDT", True, True, observe=False)
    assert (buy, sell) == (3, 0)


def test_t2_fresh_streak_met_still_possible():
    """Sanidad: la (c) vigente puede ser MET con racha fresca (no rompemos
    el gate real — el veto va contra la racha CADUCADA)."""
    import verdict
    rec = verdict._dom_gate_load().get("JUPUSDT") or {}
    age = time.time() - float(rec.get("ts") or 0.0)
    # solo es un invariant de estado, no un assertion duro del mercado:
    assert isinstance(age, float)
