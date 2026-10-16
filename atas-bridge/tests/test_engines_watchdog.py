"""Contrato del watchdog de scanner+reader (scripts/engines_watchdog.ps1).

MEDIDO 2026-10-16, en esta maquina. A las 12:41:57 locales un kill duro (exit
-1, firma de Stop-Process -Force / Administrador de tareas, NO de ningun script
de parada de este repo) tumbo TODO python vivo: arbol del scanner, supervisor
del reader, puente ATAS y los recorders EDGE. El puente y los recorders se
levantaron solos (ya tenian vigilante). El scanner y el reader NO: su unico
rearme automatico era el acceso directo de Inicio de sesion. El scanner paso
5h15m muerto con un lock rancio cuyo PID habia sido RECICLADO por otro proceso
vivo (los rearmes manuales de 13:00 a 14:10 chocaban con "another supervisor
already holds the lock"); el reader, 5h20m en el mismo silencio.

scripts/engines_watchdog.ps1 cierra ese hueco como tarea programada cada 2
minutos (misma pauta que el watchdog del puente). Esta prueba fija las
propiedades que no pueden degradarse sin que nos enteremos:

  1. SOLO ASCII. El fichero no lleva BOM; PS 5.1 puede leerlo en la codepage
     ANSI y descolocar literales no-ASCII. Es la misma clase de fallo que
     test_launcher_files.py documenta para cmd.exe, con otro parser.
  2. NUNCA mata procesos. Toda recuperacion pasa por start-scanner.ps1 /
     start-reader.ps1, que desde e4c2b9b llevan guarda de ancestros. Un
     Stop-Process/taskkill metido aqui convertiria al watchdog en lo que
     start-reader.ps1 era antes de la guarda: capaz de matar a su invocador.
  3. El boton rojo existe: con .engines_watchdog_pause presente, una parada
     intencional de los motores no es revertida por el vigilante.

Uso:  pytest atas-bridge/tests/
"""
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]                    # atas-bridge/tests -> atas-bridge -> repo
WATCHDOG = ROOT / "scripts" / "engines_watchdog.ps1"

KILL_PRIMITIVES = re.compile(r"Stop-Process|taskkill|Remove-Process", re.I)


def _code_without_comments():
    """Lineas de codigo, sin comentarios de linea completa (los headers CITAN
    esas primitivas al explicar por que no se usan; el codigo es lo que manda).
    errors=replace para no reventar aqui si alguien cuela bytes no-ASCII: de
    eso ya se encarga su propia prueba, con un mensaje mejor."""
    lines = WATCHDOG.read_text(encoding="ascii", errors="replace").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


def test_the_watchdog_exists():
    assert WATCHDOG.is_file(), f"falta {WATCHDOG}"


def test_the_watchdog_is_ascii_only():
    raw = WATCHDOG.read_bytes()
    bad = sorted({b for b in raw if b > 127})
    assert not bad, (
        f"bytes no-ASCII {bad} en {WATCHDOG.name}: sin BOM, PS 5.1 los puede "
        "leer como ANSI y descolocar el script. Ponlo en ASCII.")


def test_the_watchdog_never_kills_processes():
    code = _code_without_comments()
    hit = KILL_PRIMITIVES.search(code)
    assert not hit, (
        f"'{hit.group(0)}' aparece en el CODIGO del watchdog. Toda recuperacion "
        "debe pasar por start-scanner.ps1/start-reader.ps1 (con guarda de "
        "ancestros); un kill directo aqui repetiria el fallo de start-reader.ps1 "
        "pre-e4c2b9b (mataba a su invocador).")


def test_recovery_goes_through_the_guarded_start_scripts():
    code = _code_without_comments()
    assert "start-scanner.ps1" in code, "el scanner ya no se arranca por la via oficial"
    assert "start-reader.ps1" in code, "el reader ya no se arranca por la via oficial"


def test_the_pause_sentinel_is_honored():
    code = _code_without_comments()
    assert ".engines_watchdog_pause" in code, (
        "falta el boton rojo: sin sentinela, una parada intencional de los "
        "motores seria revertida por el vigilante en <=2 min")
