"""Integridad de la ventana de CVD que miran las condiciones (a) de AT-6/AT-7.

Cuatro contratos que este fichero fija:

  1. `bars_since_gap=None` NO es "el campo no viene". bridge_server.py lo
     inicializa así y lo documenta como "nunca hubo hueco" (L772); su propio
     payload lo traduce a `continuity="clean"` (L1074-1078). Un lector que lo
     trate como campo ausente declara no verificable a la serie más sana
     posible — cero huecos, cero barras perdidas — y deja (a) en UNEVALUABLE,
     con lo cual las reglas 6 y 7 (las únicas direccionales; la 4 está
     deshabilitada) no pueden dispararse NUNCA tras un reinicio del puente.
     Ese era el bug, y este test lo impide volver.
  2. El campo realmente ausente sigue siendo un no-verificable honesto: sin
     `continuity` tampoco se puede afirmar nada sobre contigüidad.
  3. Un hueco DENTRO de la ventana que la regla examina invalida la ventana.
     Un hueco más lejos, no: los huecos de hace horas no bloquean el sistema
     de forma permanente.
  4. `len(bd) < need_bars` es warmup, y warmup es UNEVALUABLE, no un no.
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import verdict as V                          # noqa: E402


def _state(**kw):
    """Estado CVD con los mínimos que cvd_integrity mira."""
    base = {"bar_deltas": [-43945.0, -87373.0],
            "continuity": "clean",
            "bars_since_gap": 1}
    base.update(kw)
    return base


def test_never_gapped_is_clean_not_missing():
    """None + continuity="clean" => la serie es contigua, no un campo ausente."""
    st = _state(bars_since_gap=None, continuity="clean")
    r = V.cvd_integrity(st, need_bars=2, window=4)
    assert r["ok"] is True, (
        "una serie que NUNCA hueco (None, continuity clean, gaps=0) se declaró "
        "no verificable — es exactamente el bug que este test detiene: "
        + r["reason"])


def test_field_genuinely_absent_is_still_unevaluable():
    """Sin `continuity` no hay forma de saber qué significa None: se dice."""
    st = _state(bars_since_gap=None, continuity=None)
    r = V.cvd_integrity(st, need_bars=2, window=4)
    assert r["ok"] is False
    assert "continuity" in r["reason"]


def test_gap_inside_window_invalidates_it():
    """Hueco hace 1 barra: la regla mira las últimas 4 y hay discontinuidad."""
    st = _state(bars_since_gap=1, bar_deltas=[1.0, 2.0, 3.0, 4.0])
    r = V.cvd_integrity(st, need_bars=2, window=4)
    assert r["ok"] is False
    assert "hueco" in r["reason"]


def test_gap_beyond_window_is_fine():
    """Hueco hace 5 barras con ventana de 4: lo que mira la regla está limpio."""
    st = _state(bars_since_gap=5, bar_deltas=[1.0] * 10)
    r = V.cvd_integrity(st, need_bars=2, window=4)
    assert r["ok"] is True, r


def test_warmup_is_unevaluable_not_a_no():
    """Menos barras que las que la regla necesita: no se puede preguntar."""
    st = _state(bar_deltas=[-43945.0], bars_since_gap=None, continuity="clean")
    r = V.cvd_integrity(st, need_bars=2, window=4)
    assert r["ok"] is False
    assert "barra" in r["reason"]


def test_condition_a_fires_on_clean_never_gapped_series():
    """El bug entero, extremo a extremo: con las barras reales de JUP
    (2 barras a la baja, sin huecos), la condición (a) tiene que poder
    evaluarse y salir MET — no UNEVALUABLE."""
    st = _state(bars_since_gap=None, continuity="clean")
    assert V.cvd_integrity(st, V.RULE6["streak"], V.CVD_INTEGRITY_WINDOW)["ok"]

    bd = st["bar_deltas"]
    up, dn = V.streak_from_bar_deltas(bd)
    assert (up, dn) == (0, 2), "dos barras a la baja: racha down=2"
    assert dn >= V.RULE6["streak"], "una racha de 2 cumple el mínimo de la regla 6"
