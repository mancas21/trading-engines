"""Contrato de la superficie de salud del puente. PRIMER test de atas-bridge.

MEDIDO 2026-10-15. `/health/detailed` publicaba cuatro cosas que NO PODIAN
moverse nunca:

  - `checks.calls_checker` -> SIEMPRE {"age_s": null, "note": "no check yet"}.
    El subsistema /calls se borro en la Fase 6 y `last_calls_check_ts` no lo
    escribia nadie desde entonces, asi que el bloque no podia salir por la rama
    `if` jamas. Un humano lee "no check yet" como "un chequeo que aun no ha
    corrido" cuando el chequeo YA NO EXISTE — un control retirado que se sigue
    anunciando ocupa sitio en la lista de salud y enseña a ignorarla.
  - `watchdog.dom_updates` -> 0 para siempre. Declarado, exportado en to_dict()
    y sin un solo incremento en todo el arbol.
  - `watchdog.ws_last_msg_ts` -> dict vacio para siempre. `record_ws_msg()`
    existia y nadie la llamaba.
  - `watchdog.is_healthy()` -> definida con el docstring "Quick boolean for
    supervisor scripts" y sin una sola llamada.

La regla que se aplica: un control que no puede moverse no es un control — o se
cablea o se quita. Aqui se cablean dom_updates/ws_last_msg_ts/is_healthy y se
retira calls_checker.

Uso:  pytest atas-bridge/tests/
"""
import dataclasses
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                      # atas-bridge/
sys.path.insert(0, str(ROOT))

import watchdog as W                    # noqa: E402


def test_the_retired_calls_checker_is_not_announced_any_more():
    """El subsistema /calls no existe: la salud no puede seguir anunciandolo."""
    out = W.collect()
    assert "calls_checker" not in out.get("checks", {}), (
        "se sigue publicando un chequeo de un subsistema que ya no existe: "
        f"{out['checks'].get('calls_checker')!r}")


def test_the_state_does_not_carry_a_field_nobody_writes():
    names = [f.name for f in dataclasses.fields(W.STATE)]
    assert "last_calls_check_ts" not in names, (
        "el campo retirado sigue en el estado")
    assert "last_calls_check_ts" not in W.STATE.to_dict()


def test_health_reports_the_boolean_a_supervisor_needs():
    """`is_healthy()` existia y nadie la llamaba. Se publica en el mismo sitio
    donde se calcula el estado, para que el supervisor no tenga que
    reimplementar la regla."""
    out = W.collect()
    assert "healthy" in out, "collect() no publica el booleano de salud"
    assert out["healthy"] is (out["status"] != "critical")


def test_dom_updates_and_ws_msg_are_writable():
    """Los dos contadores que estaban muertos tienen que poder moverse. Se
    comprueba sobre una instancia NUEVA para no tocar el singleton del proceso."""
    st = W.WatchdogState()
    assert st.dom_updates == 0
    st.record_dom_update()
    st.record_dom_update()
    assert st.dom_updates == 2, "dom_updates no se incrementa"
    assert st.ws_last_msg_ts == {}
    st.record_ws_msg("jupusdt")
    assert "jupusdt" in st.ws_last_msg_ts, "record_ws_msg no escribe"
    assert st.to_dict()["dom_updates"] == 2
    assert "jupusdt" in st.to_dict()["ws_last_msg_ts"]


def test_stale_dom_still_degrades_the_report():
    """Que se hayan retirado cosas no puede haber aflojado lo que SI vigila: un
    feed DOM congelado tiene que seguir saliendo en la lista."""
    dom = {"jupusdt": {"ts": 1.0}}       # epoch 1 = congelado desde siempre
    out = W.collect(dom_state=dom)
    assert out["checks"]["dom"]["stale_symbols"], "un DOM congelado ya no se nombra"
    assert out["status"] in ("degraded", "critical")
    assert out["healthy"] is (out["status"] != "critical")
