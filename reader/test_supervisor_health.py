"""Contrato de salud del lector: crash != "sin dato".

Fija el bug de 2026-10-15 que mataba el servicio en produccion. Tres
contratos:

  1. `record` devuelve 0 grabando y 2 sin dato — NUNCA 1 por un SKIP. Devolvia
     1 (main hacia `0 if do_record(c) else 1`), el supervisor contaba ese rc
     como crash, y 5 SKIP seguidos pausaban + 20 rendian el servicio con
     gave_up=true. Medido: TODAS las PAUSA del supervisor.log eran
     "SKIP: ATAS no disponible", y nada lo relanzaba despues.
  2. Un SKIP NO puerta el resolve: resolve usa klines de Binance, no ATAS, y
     gatearlo con `rc_r == 0` dejaba filas pendientes sin resolver durante
     toda la caida.
  3. Un timeout SI es fallo: run_child devolvia 0 ("tratado como no-crash"),
     asi que un hijo permanentemente colgado dejaba el ciclo sano para siempre
     — un control que no puede fallar no es un control.

Ningun test de este fichero toca el status ni el log del supervisor vivo:
todo va a tmp_path.
"""
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scanner-engine"))

import reader_supervisor as RS       # noqa: E402
import verdict_recorder as VR        # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_status(tmp_path, monkeypatch):
    """Nunca escribir el status ni el log del supervisor que esta vivo."""
    monkeypatch.setattr(RS, "STATUS", tmp_path / "supervisor_status.json")
    monkeypatch.setattr(RS, "LOG", tmp_path / "supervisor.log")


# ── 1. El subcomando record ──────────────────────────────────────────────────

def test_do_record_returns_skip_code_on_no_data(monkeypatch):
    """Sin dato -> VR.SKIP (2), no 1. El `object()` nunca se toca: si el
    codigo volviera a tocar la base en el camino del SKIP, esto truena."""
    monkeypatch.setattr(VR, "read_live",
                        lambda: (None, "ATAS no disponible: RemoteDisconnected"))
    assert VR.do_record(object()) == VR.SKIP == 2


def test_record_subcommand_exit_code_is_not_inverted(monkeypatch):
    """EL BUG: `return 0 if do_record(c) else 1` convertia el 0 de SKIP en
    rc=1. El subcomando tiene que devolver el codigo tal cual."""
    monkeypatch.setattr(VR, "db", lambda: object())
    monkeypatch.setattr(VR, "do_record", lambda c: VR.SKIP)
    monkeypatch.setattr(sys, "argv", ["verdict_recorder.py", "record"])
    assert VR.main() == VR.SKIP, "main() volvio a invertir el codigo de salida"

    monkeypatch.setattr(VR, "do_record", lambda c: VR.RECORDED)
    assert VR.main() == VR.RECORDED


def test_recorded_is_zero_and_skip_is_not_zero():
    """La distincion tiene que existir: si ambos fueran 0, el supervisor no
    podria separar 'grabo' de 'no habia dato' (y notify depende de eso)."""
    assert VR.RECORDED == 0
    assert VR.SKIP != VR.RECORDED
    assert VR.SKIP != VR.FAIL


# ── 2. La decision de salud del ciclo ────────────────────────────────────────

def _fake_runner(monkeypatch, recorder_rc, recorder_out, resolve_rc=0,
                 notify_rc=0):
    """Sustituye subprocess: devuelve los rc programados y registra llamadas."""
    calls = []

    # El notificador se llama con --once Y --digest (c139605): --once conserva el
    # aviso inmediato por cambio de sesgo, --digest anade el resumen ejecutivo de
    # 15 min aunque el sesgo no cambie. El stub exige LAS DOS, para que quitar
    # --digest (o volver a no enviar nada) ponga esto rojo otra vez.
    NOTIFY_ARGS = (["--once", "--digest"], ["--once", "--digest", "--dry-run"])

    def run(script, args, timeout):
        calls.append(list(args))
        if args == ["record"]:
            return recorder_rc, recorder_out
        if args == ["resolve"]:
            return resolve_rc, "  resolved 3/4"
        if args in NOTIFY_ARGS:
            return notify_rc, "  notified"
        raise AssertionError(f"llamada inesperada: {args}")

    monkeypatch.setattr(RS, "run_child", run)
    return calls


SKIP_OUT = "  SKIP: ATAS no disponible: RemoteDisconnected"
REC_OUT = "  recorded JUPUSDT bar#1021 bias=NEUTRAL r6=False r7=False"


def test_skip_is_not_a_crash_but_resolve_still_runs(monkeypatch):
    """Un ciclo con ATAS caido tiene que ser sano Y seguir resolviendo."""
    calls = _fake_runner(monkeypatch, RS.RECORDER_SKIP, SKIP_OUT)
    ok, tail = RS.cycle()

    assert ok is True, "un SKIP esta haciendo parecer que el proceso fallo"
    assert tail.startswith("SKIP:"), tail
    # resolve usa Binance, no ATAS: tiene que correr aunque no haya fila nueva
    assert ["resolve"] in calls, (
        "resolve quedo gateado tras el SKIP: las filas pendientes de antes "
        "dejan de resolverse durante toda la caida")
    # ... pero notify no, porque no hay veredicto nuevo
    assert not any(a and a[0] == "--once" for a in calls), (
        "notify corrio sin fila nueva: re-notificaria el mismo veredicto")

    st = json.loads(RS.STATUS.read_text(encoding="utf-8"))
    assert st["consecutive_skips"] == 1
    assert st["no_data"] is False


def test_a_run_of_skips_becomes_visible_without_killing_the_service(monkeypatch):
    """Sin dato seguido tiene que VERSE en el status, pero no puede rendir el
    servicio: matarlo por una caida de ATAS es lo que dejaba al reader muerto
    y sin relanzador."""
    monkeypatch.setattr(RS, "MAX_CONSECUTIVE_SKIPS", 3)
    _fake_runner(monkeypatch, RS.RECORDER_SKIP, SKIP_OUT)
    for _ in range(3):
        ok, _ = RS.cycle()
    assert ok is True, "un servicio sin dato no tiene que rendirse"

    st = json.loads(RS.STATUS.read_text(encoding="utf-8"))
    assert st["consecutive_skips"] == 3
    assert st["no_data"] is True, "30 min sin dato no se veia en ningun sitio"

    # vuelve el dato: el contador y el flag se limpian solos
    _fake_runner(monkeypatch, RS.RECORDER_RECORDED, REC_OUT)
    ok, _ = RS.cycle()
    assert ok is True
    st = json.loads(RS.STATUS.read_text(encoding="utf-8"))
    assert st["consecutive_skips"] == 0
    assert st["no_data"] is False


def test_recorded_cycle_runs_notify(monkeypatch):
    calls = _fake_runner(monkeypatch, RS.RECORDER_RECORDED, REC_OUT)
    ok, _ = RS.cycle()
    assert ok is True
    assert ["resolve"] in calls
    assert ["--once", "--digest"] in calls, (
        "el notificador perdio --digest: el resumen ejecutivo de 15 min dejaria "
        "de enviarse y nada lo avisaria (es justo el defecto que arreglo c139605)")


def test_recorder_crash_is_a_crash(monkeypatch):
    """Los codigos de error REALES siguen contando: si no, habria arreglado el
    SKIP a costa de que nada detecte un grabador roto."""
    _fake_runner(monkeypatch, 1, "  Traceback: base bloqueada")
    ok, _ = RS.cycle()
    assert ok is False, "un error real del grabador dejo de detectarse"


def test_a_hung_child_is_not_reported_as_healthy(monkeypatch):
    """run_child devolvia 0 en timeout: un grabador colgado para siempre
    dejaba falls=0, sin pausa, sin gave_up y sin aviso. Ahora es 124."""
    rc, out = RS.run_child(RS.RECORDER, ["record"], timeout=0.001)
    assert rc == RS.CHILD_TIMEOUT == 124, "el timeout vuelve a parecer sano"
    assert rc != 0

    # y el ciclo lo trata como fallo
    _fake_runner(monkeypatch, RS.CHILD_TIMEOUT, "timeout (hijo no termino)")
    ok, _ = RS.cycle()
    assert ok is False


def test_resolver_and_notify_failures_accumulate(monkeypatch):
    """Antes solo se guardaba el ultimo rc: una klines de Binance en 418 o un
    token invalido podian estar fallando SEMANAS con consecutive_failures: 0 y
    sin ningun aviso."""
    _fake_runner(monkeypatch, RS.RECORDER_RECORDED, REC_OUT, resolve_rc=1, notify_rc=1)
    RS.cycle()
    st = json.loads(RS.STATUS.read_text(encoding="utf-8"))
    assert st["resolver_consecutive_failures"] == 1, "fallo del resolver invisible"
    assert st["notify_consecutive_failures"] == 1, "fallo del notify invisible"

    # al recuperarse, el contador baja a 0 solo
    _fake_runner(monkeypatch, RS.RECORDER_RECORDED, REC_OUT, resolve_rc=0, notify_rc=0)
    RS.cycle()
    st = json.loads(RS.STATUS.read_text(encoding="utf-8"))
    assert st["resolver_consecutive_failures"] == 0
    assert st["notify_consecutive_failures"] == 0
