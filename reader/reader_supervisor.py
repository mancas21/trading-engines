"""Supervisor del grabador + notificador del redesigned reader.

POR QUE EXISTE
==============
El grabador y el notificador eran procesos de mi sesión: al cerrarla paraban, y
con ellos se perdía la evidencia que justificó todo el rediseño. Esto los
convierte en servicios que sobreviven cierres de sesión y reinicios del motor.

POLÍTICA DE REINICIO (corregida tras el incidente de los "Already running")
==========================================================================
En el motor, el supervisor reiniciaba el hijo en CUALQUIER salida no-cero,
incluida la negativa por lock. Eso producía una pila infinita de diálogos
flasheando. Aquí hay TRES estados, no dos:

  salida 0            -> el operador paró el servicio. PARAR, sin relanzar.
  ALREADY_RUNNING(=3) -> ya hay otro servicio. PARAR (el otro manda).
  cualquier otra      -> crash real. Relanzar, con tope de fallos rápidos.

Además, si el hijo reinicia demasiadas veces en poco tiempo, se para y lo dice:
un bucle de reinicios no es recuperación, es un fallo que hay que mirar.

Uso:
  python reader_supervisor.py --once        # un ciclo
  python reader_supervisor.py --loop         # supervise para siempre
  python reader_supervisor.py --status      # estado, pausa, reintentos
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOG = HERE / "supervisor.log"
STATUS = HERE / "supervisor_status.json"

# Python 3.12: el mismo intérprete que el motor, con aiohttp/pandas.
PYTHON = r"C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"

RECORDER = HERE / "verdict_recorder.py"
NOTIFIER = HERE / "verdict_notify.py"

# Ritmo de cada hijo. El grabador cada 30s (la granularidad del footprint es de
# barra, asi que mas fino no anade nada); el notificador cada 60s.
RECORD_EVERY_S = 30.0
NOTIFY_EVERY_S = 60.0

# Codigo que el notificador usa para decir "ya hay otro servicio".
ALREADY_RUNNING_EXIT = 3

# Techo de fallos rapidos antes de PAUSAR (ya no de rendirse).
MAX_FAST_FAILS = 5
FAST_FAIL_SECONDS = 120.0
# Politica de pausa: espera creciente hasta este tope, y como mucho este numero
# de intentos. Agotados, el supervisor .bat sale y el servicio queda parado y
# visible, nunca en bucle.
RETRY_BACKOFF_S = 60.0
RETRY_BACKOFF_MAX_S = 300.0
MAX_RETRY_ATTEMPTS = 20


def log(msg: str) -> None:
    line = f"[{datetime.now():%a %d/%m/%Y %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass


def write_status(state: dict) -> None:
    try:
        STATUS.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def run_child(script: Path, args: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run([PYTHON, str(script)] + args, cwd=str(HERE),
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        # El notificador con --once puede tardar si tv_live.mjs se cuelga; no es
        # un crash, asi que se distingue de un fallo real.
        return 0, "timeout (tratado como no-crash)"
    except Exception as e:
        return 1, f"{type(e).__name__}: {str(e)[:120]}"


def cycle(dry_run: bool = False) -> tuple[bool, str]:
    """Un ciclo: graba un veredicto y evalua notificar. Devuelve (ok, msg)."""
    st = {}
    if STATUS.exists():
        try:
            st = json.loads(STATUS.read_text(encoding="utf-8"))
        except Exception:
            st = {}
    st["last_cycle"] = time.time()
    st["last_cycle_iso"] = datetime.now().isoformat()

    rc_r, out_r = run_child(RECORDER, ["record"], timeout=90)
    st["recorder_rc"] = rc_r
    st["recorder_tail"] = out_r.strip().splitlines()[-1] if out_r.strip() else ""

    # El notificador solo se evalua si el grabador registro algo justo antes:
    # notificar sin dato nuevo es re-notificar el mismo veredicto.
    #
    # nuevo es re-notificar el mismo veredicto.
    #
    # BUG QUE CASI SE ENTREGA: esto llamaba al notificador con --dry-run, asi
    # que NUNCA enviaba nada y la Fase 4 entera era decoracion — el mismo fallo
    # de "un control que se calcula, se imprime y nunca se consume". Se envia de
    # verdad; el anti-spam lo garantiza el CAMBIO DE SESGO del notificador, no
    # un dry-run aqui.
    if rc_r == 0 and "recorded" in out_r:
        nargs = ["--once"] + (["--dry-run"] if dry_run else [])
        rc_n, out_n = run_child(NOTIFIER, nargs, timeout=150)
        st["notify_rc"] = rc_n
        st["notify_mode"] = "dry-run" if dry_run else "send"
        st["notify_tail"] = out_n.strip().splitlines()[-1] if out_n.strip() else ""
    else:
        st["notify_rc"] = None
        st["notify_mode"] = None
        st["notify_tail"] = "sin veredicto nuevo: no se evalua notificacion"

    ok = rc_r == 0
    write_status(st)
    return ok, st.get("recorder_tail", "")


def do_loop(interval: float, dry_run: bool = False) -> int:
    log(f"reader supervisor started (interval {interval:.0f}s, child "
        f"=record + notificar si cambia)")
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    fails = 0
    attempts = 0
    paused_was = False
    while True:
        t0 = time.time()
        try:
            ok, tail = cycle(dry_run=dry_run)
        except Exception as e:
            ok, tail = False, f"{type(e).__name__}: {str(e)[:90]}"
        ran = time.time() - t0
        if ok:
            fails = 0
            paused_was = bool(attempts)
        else:
            fails = fails + 1 if ran < FAST_FAIL_SECONDS else 0
        st = {}
        try:
            st = json.loads(STATUS.read_text(encoding="utf-8"))
        except Exception:
            pass
        st["consecutive_failures"] = fails
        write_status(st)
        if fails >= MAX_FAST_FAILS:
            # OPCION A (2026-10-07): PAUSAR y reintentar, en vez de morir.
            #
            # Por que cambio: el PUENTE se reinicia solo cada ~20-40 min
            # (bridge_supervisor.log: "Bridge exited (code -1), restarting in
            # 3s"). Con el comportamiento de rendirse, cada uno de esos
            # reinicios mataba el grabador, y nadie lo relanzaba: el servicio
            # quedaba muerto hasta que alguien lo arrancaba a mano. Medido: tres
            # muertes por rendicion en una sesion.
            #
            # Por que NO es el lazo ciego del incidente "Already running":
            # aqui NO se relanza ningun hijo. Solo se reintenta la LECTURA, con
            # espera creciente, tope de intentos, y una linea de log cada vez.
            # Un reinicio automatico de proceso es lo que provoco aquella pila
            # de dialogos; un reintento de lectura HTTP no puede hacer eso.
            #
            # Si tras el tope sigue fallando, se devuelve 1: el supervisor
            # .bat entonces sale y queda PARADO y visible, no en bucle.
            attempts += 1
            backoff = min(RETRY_BACKOFF_MAX_S, RETRY_BACKOFF_S * attempts)
            log(f"PAUSA: {fails} fallos seguidos -> reintento en {backoff:.0f}s "
                f"(intento {attempts}/{MAX_RETRY_ATTEMPTS}). ultimo: {tail[:70]}")
            st = {}
            try:
                st = json.loads(STATUS.read_text(encoding="utf-8"))
            except Exception:
                pass
            st["paused"] = True
            st["paused_at"] = datetime.now().isoformat()
            st["retry_attempt"] = attempts
            st["retry_backoff_s"] = backoff
            write_status(st)
            if attempts >= MAX_RETRY_ATTEMPTS:
                log(f"AGOTADOS {attempts} reintentos. El servicio queda PARADO "
                    f"(no en bucle). Revisa la causa antes de relanzar.")
                st["paused"] = False
                st["gave_up"] = True
                write_status(st)
                return 1
            time.sleep(backoff)
            continue

        if paused_was:
            log(f"RECUPERADO tras {attempts} pausa(s): la lectura vuelve a "
                f"funcionar.ULTIMO: {tail[:70]}")
            attempts = 0
            st = {}
            try:
                st = json.loads(STATUS.read_text(encoding="utf-8"))
            except Exception:
                pass
            st.pop("paused", None)
            st.pop("retry_attempt", None)
            st.pop("retry_backoff_s", None)
            st["gave_up"] = False
            st["recovered_at"] = datetime.now().isoformat()
            write_status(st)
        paused_was = False

        try:
            time.sleep(max(0.0, interval - ran))
        except KeyboardInterrupt:
            log("supervisor exiting (SIGINT)")
            return 0


def do_status() -> int:
    if not STATUS.exists():
        print("  sin status: el supervisor no ha corrido nunca")
        return 1
    st = json.loads(STATUS.read_text(encoding="utf-8"))
    print(f"  ultimo ciclo : {st.get('last_cycle_iso')}")
    for k in ("recorder_rc", "recorder_tail", "notify_rc", "notify_tail",
              "consecutive_failures"):
        print(f"  {k:14s} {st.get(k)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="no enviar Telegram; solo evaluar el cambio")
    a = ap.parse_args()
    if a.status:
        return do_status()
    if a.loop:
        return do_loop(a.interval, dry_run=a.dry_run)
    ok, tail = cycle(dry_run=a.dry_run)
    print(f"  ok={ok}  {tail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())