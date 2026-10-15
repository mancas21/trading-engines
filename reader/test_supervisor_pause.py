"""Regresion de la politica de pausa/reintento del supervisor del reader.

El fallo real (2026-10-07): el puente se reinicia solo cada ~20-40 min, y cada
reinicio mataba el grabador porque se rendia a los 5 fallos. Nadie lo relanzaba.

Contratos que fija este fichero:
  1. Fallo TRANSITORIO: PAUSA xN -> RECUPERADO -> sigue grabando, y el estado
     de pausa se limpia entero (incluido `paused_at`, que antes se quedaba
     como estado fantasma en el status).
  2. Fallo PERMANENTE: PAUSA xN -> AGOTADOS -> CARRIL LENTO. El proceso NO
     muere. El diseño original devolvia 1 ("queda parado y visible"), pero la
     premisa — que alguien lo veria — es falsa: NADA relanza este proceso
     (reader_supervisor.bat es de un solo tiro y no hay tarea programada). El
     gave_up del 14-oct a las 11:45 paso horas inadvertido. Rendirse no era
     "quedar visible", era morir; y morir por una causa transitoria dejaba al
     reader sin grabar para siempre.
  3. El carril lento conserva lo que el diseño original protegia: NO hay bucle
     rapido (un intento cada RETRY_EXHAUSTED_S) y NO se relanza ningun proceso.

CONVERSION A TEST (2026-10-15). Antes esto era un SCRIPT que llamaba a
`rs.do_loop()` a NIVEL DE MODULO:
  - rompia la coleccion de pytest (importar el fichero ejecutaba el bucle);
  - secuestraba el lock del supervisor VIVO y le borraba el
    supervisor_status.json de produccion en la primera linea
    (`(D/"supervisor_status.json").unlink()`).
Ahora es un modulo de test normal: nada corre al importar y TODO el estado
(STATUS, LOG, LOCK) vive en tmp_path.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scanner-engine"))


def _load_supervisor():
    """Carga reader_supervisor.py como modulo `rs`.

    Se carga por ruta (no por import normal) porque el fichero vive junto a los
    demas tests y no es un paquete; es lo unico que el script original hacia
    bien y se conserva.
    """
    spec = importlib.util.spec_from_file_location("rs", HERE / "reader_supervisor.py")
    rs = importlib.util.module_from_spec(spec)
    sys.modules["rs"] = rs
    spec.loader.exec_module(rs)
    return rs


rs = _load_supervisor()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Ninguna ruta de este fichero puede tocar el supervisor VIVO."""
    monkeypatch.setattr(rs, "STATUS", tmp_path / "supervisor_status.json")
    monkeypatch.setattr(rs, "LOG", tmp_path / "supervisor.log")
    monkeypatch.setattr(rs, "LOCK", tmp_path / "reader_supervisor.lock")
    monkeypatch.setattr(rs, "_LOCK", rs.InstanceLock(tmp_path / "reader_supervisor.lock"))
    # Caps a milisegundos para no esperar horas.
    monkeypatch.setattr(rs, "MAX_FAST_FAILS", 2)
    monkeypatch.setattr(rs, "RETRY_BACKOFF_S", 0.01)
    monkeypatch.setattr(rs, "RETRY_BACKOFF_MAX_S", 0.02)
    monkeypatch.setattr(rs, "MAX_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(rs, "RETRY_EXHAUSTED_S", 0.02)


def _drive(monkeypatch, fake_cycle, hard_stop_after=40):
    """Corre do_loop con un ciclo falso que se detiene con SystemExit.

    SystemExit es BaseException: `_loop_body` solo captura Exception alrededor
    de cycle(), asi que la parada escapa del bucle de verdad.
    """
    logs = []
    monkeypatch.setattr(rs, "log", lambda m: logs.append(str(m)))
    n = {"c": 0}

    def _cycle(dry_run=False):
        n["c"] += 1
        if n["c"] > hard_stop_after:
            raise SystemExit(0)
        return fake_cycle(n["c"])

    monkeypatch.setattr(rs, "cycle", _cycle)
    with pytest.raises(SystemExit):
        rs.do_loop(interval=0.01)
    return logs


def _status():
    return json.loads(rs.STATUS.read_text(encoding="utf-8"))


def test_the_suite_never_touches_the_live_supervisor_state():
    prod = HERE / "supervisor_status.json"
    assert pathlib.Path(rs.STATUS) != prod, (
        "este test apunta al status del supervisor VIVO: cada `pytest` le "
        "borraria/reescribiria el estado al proceso en ejecucion")
    assert not str(rs.STATUS).startswith(str(HERE))
    assert pathlib.Path(rs.LOCK) != HERE / "reader_supervisor.lock"


def test_transient_failure_pauses_then_recovers(monkeypatch):
    """Fallo transitorio: PAUSA xN -> RECUPERADO, y el estado queda limpio."""
    def fake(n):
        if n <= 6:
            return False, "SKIP: RemoteDisconnected (simulado)"
        return True, "recorded XRPUSDT NEUTRAL"

    logs = _drive(monkeypatch, fake, hard_stop_after=12)
    st = _status()

    assert any(l.startswith("PAUSA") for l in logs), "no pauso ante el fallo"
    assert len([l for l in logs if l.startswith("RECUPERADO")]) == 1, logs
    assert st.get("paused") is None, "quedo `paused` tras recuperarse"
    assert st.get("paused_at") is None, (
        "quedo `paused_at` como estado fantasma: el status vivo anunciaba una "
        "pausa que ya no existia (con gave_up=false y sin `paused`)")
    assert st.get("gave_up") is False
    assert st.get("recovered_at")


def test_exhausted_retries_go_to_slow_lane_instead_of_dying(monkeypatch):
    """Fallo permanente: NO muere. Pasa a carril lento y sigue visible."""
    def always_fail(n):
        return False, "boom: base bloqueada"

    logs = _drive(monkeypatch, always_fail, hard_stop_after=30)
    st = _status()

    assert any("CARRIL LENTO" in l for l in logs), (
        "agoto los reintentos y no hay rastro del carril lento: volvio a morir")
    assert st.get("degraded") is True, "el estado degradado no se publica"
    assert st.get("degraded_at")
    assert st.get("gave_up") is False, (
        "el servicio volvio a marcarse como rendido: nada lo relanza, rendirse "
        "es morir")


def test_a_run_of_failures_never_returns_a_fatal_code(monkeypatch):
    """El bucle solo puede salir por la parada dura del test, nunca por `return 1`."""
    def always_fail(n):
        return False, "boom"

    # Si `_loop_body` devolviera 1, do_loop volveria normalmente y el
    # pytest.raises(SystemExit) fallaria — que es exactamente lo que queremos.
    logs = _drive(monkeypatch, always_fail, hard_stop_after=25)
    assert logs, "nunca se ejecuto el bucle"
