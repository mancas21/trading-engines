"""Ventana de persistencia de las condiciones de las reglas 6/7.

MEDIDO (2026-10-15). Durante el rally de JUP (08:54 0.2903 -> 10:17 0.3151,
+8,55%) el motor emitio 86 NEUTRAL y 1 solo BULLISH, y ese uno salio a las
10:06 con el rally ya hecho. No faltaba evidencia — en esa ventana (a) estuvo
MET 34 veces, (b) 41 y (c) 10 — sino que la regla exigia las tres en la MISMA
lectura de 30 s, y son sensores que no conmutan a la vez. La linea temporal
muestra el near-miss: 09:37 con (a)=MET (b)=MET (c)=FAIL, y (c) habia estado
MET a las 09:35.

  R7 (a)Y(b)Y(c) en el mismo tick -> 14 filas de 3.339   primer disparo 10:06
  R7 cada una vista en 60 s       -> primer disparo 09:35 @ 0.3022

Contratos que fija este fichero:
  1. La ventana eleva FAIL -> MET si la condicion estuvo MET dentro del plazo.
  2. La ventana NUNCA eleva un UNEVALUABLE: la falta de dato sigue bloqueando.
  3. window_s=0 reproduce EXACTAMENTE el comportamiento estricto anterior.
  4. Solo el OBSERVADOR escribe estado; una lectura observe=False no deja
     rastro. Contar la misma lectura dos veces por ciclo es el fallo que ya se
     arreglo en la condicion (c).
  5. El valor vive en rules.json y el codigo lo lee: el contrato no puede
     divergir en silencio.
"""
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scanner-engine"))

import verdict as V                       # noqa: E402

_ALL_MET = {"r6_a": "MET", "r6_b": "MET", "r6_c": "MET",
            "r7_a": "MET", "r7_b": "MET", "r7_c": "MET"}
_ALL_FAIL = {k: "FAIL" for k in _ALL_MET}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Nunca tocar el estado del gate vivo."""
    p = tmp_path / "condition_window_state.json"
    monkeypatch.setattr(V, "_cond_state_path", lambda: p)


def test_window_raises_fail_to_met_when_the_condition_held_recently():
    """El near-miss de las 09:37: (c) estaba MET dos minutos antes."""
    V.condition_window_eval("JUPUSDT", _ALL_MET, 60.0, observe=True)

    # La condicion (c) vuelve a FAIL, pero (a) y (b) siguen MET.
    ahora = dict(_ALL_MET)
    ahora["r7_c"] = "FAIL"
    eff = V.condition_window_eval("JUPUSDT", ahora, 60.0, observe=True)

    assert eff["r7_c"] == "MET", (
        "la regla 7 no dispara: (c) estuvo MET dentro de la ventana y el "
        "desajuste de muestreo lo descarta")
    assert eff["r7_a"] == "MET" and eff["r7_b"] == "MET"


def test_window_never_raises_an_unevaluable():
    """Falta de dato no es desacuerdo: si hoy no se puede medir, sigue bloqueando."""
    V.condition_window_eval("JUPUSDT", _ALL_MET, 60.0, observe=True)

    ahora = dict(_ALL_MET)
    ahora["r7_c"] = "UNEVALUABLE"
    eff = V.condition_window_eval("JUPUSDT", ahora, 60.0, observe=False)

    assert eff["r7_c"] == "UNEVALUABLE", (
        "la ventana convirtio un 'no puedo medir' en un MET: eso inventaria "
        "evidencia que no existe")


def test_window_zero_is_exactly_the_old_strict_behaviour():
    """Poder volver al comportamiento estricto con un numero, no con un deploy."""
    V.condition_window_eval("JUPUSDT", _ALL_MET, 60.0, observe=True)
    ahora = dict(_ALL_MET)
    ahora["r7_c"] = "FAIL"

    eff = V.condition_window_eval("JUPUSDT", ahora, 0.0, observe=True)
    assert eff == ahora, "window_s=0 no reproduce la conjuncion estricta"


def test_only_the_observer_writes_state():
    """Una lectura observe=False no puede dejar rastro."""
    p = V._cond_state_path()
    V.condition_window_eval("JUPUSDT", _ALL_MET, 60.0, observe=False)
    assert not p.exists(), (
        "una lectura no-observadora escribio estado: la misma lectura se "
        "contaria dos veces por ciclo")

    V.condition_window_eval("JUPUSDT", _ALL_MET, 60.0, observe=True)
    assert p.exists()
    hist = json.loads(p.read_text(encoding="utf-8"))["JUPUSDT"]
    assert len(hist) == 1, f"el observador anoto {len(hist)} veces en una lectura"


def test_the_window_value_lives_in_rules_json_and_the_code_reads_it():
    """Contrato JSON <-> codigo, como el resto de umbrales del sistema."""
    raw = json.loads((ROOT / "atas-bridge" / "rules.json").read_text(encoding="utf-8"))
    for rid in (6, 7):
        spec = next(r for r in raw["rules"] if r["id"] == rid)
        assert "condition_window_s" in spec["conditions"], (
            f"la regla {rid} no declara condition_window_s")
    assert V.COND_WINDOW_S == 60.0, (
        f"el codigo lee {V.COND_WINDOW_S} y rules.json declara 60: el contrato "
        f"divergio en silencio")


def test_the_window_cannot_pull_a_met_from_outside_the_plazo():
    """La ventana tiene plazo. Si la condicion fue MET hace mucho, no vale."""
    V.condition_window_eval("JUPUSDT", _ALL_MET, 60.0, observe=True)
    ahora = dict(_ALL_MET)
    ahora["r7_c"] = "FAIL"

    # Ventana de 0 s: el MET es de la lectura anterior, fuera del plazo.
    eff = V.condition_window_eval("JUPUSDT", ahora, 0.0, observe=False)
    assert eff["r7_c"] == "FAIL"
