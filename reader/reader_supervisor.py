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

# Contrato de codigos de salida del grabador (verdict_recorder.RECORDED/SKIP/
# FAIL, y tests/test_supervisor_health.py lo fija). Los tres viven aqui para que
# el supervisor no tenga que importar al grabador (que abre base y red).
RECORDER_RECORDED = 0   # se escribio fila
RECORDER_SKIP = 2       # "ATAS sin dato": condicion NORMAL, no consume crashes
RECORDER_FAIL = 1       # error real del proceso
# Codigo convencional de timeout (coreutils 124). Se devuelve en vez de 0 para
# que un hijo colgado no pueda parecer sano.
CHILD_TIMEOUT = 124
# Si el grabador lleva esto de seguido sin registrar, ya no es "ATAS parpadeo":
# es un servicio que no da servicio, y tiene que verse en el status.
MAX_CONSECUTIVE_SKIPS = 60      # ~30 min con RECORD_EVERY_S=30
# 2026-10-15 — Cadencia del CARRIL LENTO, al que se pasa cuando ya se agoto el
# tope de reintentos. Antes se devolvia 1 y el proceso MORIA (ver la rama).
RETRY_EXHAUSTED_S = 1800.0      # 30 min entre intentos ya agotado el tope

# Lock de instancia unica.
#
# Por que: el supervisor se lanza desde la carpeta Inicio, y TAMBIEN a mano
# cuando alguien lo revisa. Sin lock, dos supervisores graban a la vez y
# reader.db recibe veredictos DUPLICADOS — y ningun consumidor puede distinguir
# la duplicacion de una racha real. Es el mismo fallo que se verifico en vivo en
# scanner-engine ("two supervisor trees and two scanner children on one
# database"). Sin este lock, meter el lector en Inicio habria creado ese bug.
LOCK = HERE / "reader_supervisor.lock"

# Rutas de lock que ESTE proceso ha reclamado. El fichero de PID por si solo no
# puede expresar "un segundo objeto InstanceLock en el mismo interprete debe ser
# rechazado", porque el pid que contiene es el nuestro y por tanto siempre vivo.
# 2026-10-15 — `_CLAIMED` ELIMINADO aqui tambien (existia igual en
# scanner-engine/supervisor.py). Era un dict de "rutas de lock que YO he
# reclamado" que se declaraba, se escribia al adquirir y se borraba al soltar, y
# que NADIE LEIA. La proteccion que su comentario prometia — rechazar un segundo
# lock en el mismo interprete — la da la comprobacion del PID del fichero:
# `acquire` rechaza cuando el pid leido es el nuestro, que es exactamente ese
# caso. El dict solo acumulaba estado sin consumidor.


class InstanceLock:
    """Un fichero de PID exclusivo entre procesos y reclamable si el titular murio.

    La reclamabilidad importa: si al supervisor lo matan a lo bruto, el fichero
    de PID queda; un supervisor que se niega a arrancar por el fichero de un
    titular muerto es indistinguible de uno que esta trabado.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._held = False

    def _read_pid(self):
        try:
            txt = self.path.read_text(encoding="utf-8").strip()
            return int(txt.split()[0]) if txt else None
        except (FileNotFoundError, ValueError, IndexError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        """Esta vivo `pid`?

        `os.kill(pid, 0)` NO es portable aqui: en Windows lanza
        `OSError: [WinError 87]` para un pid cualquiera, lo que haria que todo
        lock pareciese de un titular muerto y dejaria reclamarlo a un segundo
        supervisor. Usa OpenProcess en Windows y os.kill en el resto.
        """
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True

        if sys.platform == "win32":
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            try:
                import ctypes
                k32 = ctypes.windll.kernel32          # noqa: N806
                h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not h:
                    # ERROR_ACCESS_DENIED (5) significa que existe pero no es
                    # nuestro; cualquier otra cosa significa que no esta.
                    return k32.GetLastError() == 5
                try:
                    code = ctypes.c_ulong()
                    if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                        return True
                    return code.value == STILL_ACTIVE
                finally:
                    k32.CloseHandle(h)
            except Exception:                              # noqa: BLE001
                return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError):
            return False
        except PermissionError:
            return True
        return True

    def acquire(self) -> bool:
        if self._held:
            return True

        # Reclamo ATOMICO via O_CREAT|O_EXCL, y despues comprobacion de vida.
        #
        # La version secuencial (leer pid, comprobar vida, escribir) es una
        # carrera TOCTOU: dos supervisores arrancando en el mismo segundo leen
        # "sin lock", ambos escriben, y AMBOS corren — verificado en vivo en
        # scanner-engine, dos arboles de supervisor y dos hijos sobre una base.
        # O_EXCL hace que el propio create sea el reclamo: exactamente uno gana,
        # incluso entre procesos. Un fichero rancio se resuelve despues mirando
        # si el pid de dentro sigue vivo.
        self.path.parent.mkdir(parents=True, exist_ok=True)

        for _ in range(2):                     # un reintento tras limpiar rancio
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self._read_pid()
                if holder is not None and holder != os.getpid() and self._alive(holder):
                    return False               # un proceso vivo lo tiene
                if holder == os.getpid():
                    # El fichero dice NUESTRO pid: u otro InstanceLock del mismo
                    # proceso lo tiene (debe rechazarse — es la exclusividad de
                    # la que depende el supervisor), o ya lo tenemos nosotros,
                    # caso que el `self._held` de arriba ya resolvio. Rechaza.
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            except OSError:
                return False
            else:
                try:
                    os.write(fd, f"{os.getpid()}\n".encode())
                finally:
                    os.close(fd)
                self._held = True
                return True

        return False

    def release(self) -> None:
        if not self._held:
            return
        # solo borra el fichero si sigue siendo nuestro
        if self._read_pid() == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self._held = False


_LOCK = InstanceLock(LOCK)


def do_loop(interval: float, dry_run: bool = False) -> int:
    # Instancia unica ANTES de tocar nada: si ya hay un supervisor vivo, este
    # sale sin grabar. Dos grabadores sobre reader.db duplican veredictos.
    if not _LOCK.acquire():
        log(f"Ya hay otro supervisor del reader activo (lock tomado en {LOCK}). "
            f"Este proceso sale sin grabar.")
        return ALREADY_RUNNING_EXIT
    try:
        return _loop_body(interval, dry_run=dry_run)
    finally:
        _LOCK.release()


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
        # 2026-10-15 — DEVOLVIA 0 ("tratado como no-crash"), asi que un hijo
        # PERMANENTEMENTE colgado dejaba el ciclo con ok=True para siempre:
        # fails a 0, sin pausa, sin gave_up y sin aviso. Un control que no
        # puede fallar no es un control. El timeout es un codigo real; quien
        # decide si eso pausa el servicio es el llamante (el notificador puede
        # colgarse en tv_live.mjs sin que el grabador tenga la culpa).
        return CHILD_TIMEOUT, "timeout (hijo no termino)"
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

    # 2026-10-13 — BUG QUE HACIA FALTA EL RESOLVER.
    #
    # El ciclo ejecutaba UN solo hijo: verdict_recorder.py record. Graba el
    # veredicto, pero `record` NO resuelve nada: resolve es un subcomando
    # aparte (verdict_recorder.py do_resolve). Nadie lo llamaba. Medido:
    #
    #     2026-10-07   1003 filas   1003 resueltas (100,0%)
    #     2026-10-11     87 filas      0 resueltas (  0,0%)
    #     2026-10-12    285 filas      0 resueltas (  0,0%)
    #     2026-10-13    903 filas      0 resueltas (  0,0%)
    #
    # Las 1.205 filas resueltas son exactamente las anteriores al 11-oct: el
    # resolver se dejo de lanzar ese dia y nadie lo noto porque el grabador
    # seguia dando rc=0 y el supervisor logueaba "recorded ... bias=NEUTRAL"
    # como si todo fuera bien.
    #
    # Por que importa mas de lo que parece: forward_return es lo unico que
    # convierte un veredicto en evidencia. Sin el, desde el 11-oct el motor
    # acumula filas que nadie puede medir, y TODA calibracion futura — la
    # regla 6 incluida — se hace sobre las 1.205 antigas o no se hace.
    #
    # Se resuelve antes de notificar: notificar un veredicto cuyo resultado ya
    # se conoce evita el "sesgo sin cambio" sobre una fila que si cambia.
    # 2026-10-15 — SEPARAR "no hay dato" de "fallo el proceso".
    #
    # Antes TODO pasaba por `rc_r == 0`: con ATAS caido el SKIP salia con rc=1
    # y el ciclo entero se comportaba como un crash (fallo el resolve, fallo el
    # notify, sumaba fails). Medido: 5 SKIP seguidos pausaban el servicio y 20
    # mas lo rendian con gave_up=true, y NADA lo relanzaba despues.
    #
    # `healthy` = el proceso funciono (0 grabo, 2 no habia dato). Solo eso
    # consume el presupuesto de crashes. `recorded` = ademas escribio fila, y
    # eso es lo que exige notificar.
    healthy = rc_r in (RECORDER_RECORDED, RECORDER_SKIP)
    recorded = rc_r == RECORDER_RECORDED

    # resolve usa klines de Binance, NO el puente: una caida de ATAS no debe
    # impedir resolver las filas pendientes de antes. Antes se gateaba con
    # rc_r == 0, asi que durante toda la caida de ATAS no se resolvia nada —
    # exactamente el incidente documentado arriba (1.163 filas sin medir).
    rc_v = None
    out_v = "no-op"
    if healthy:
        rc_v, out_v = run_child(RECORDER, ["resolve"], timeout=120)
        st["resolver_rc"] = rc_v
        st["resolver_tail"] = out_v.strip().splitlines()[-1] if out_v.strip() else ""
        # Antes solo se guardaba el ultimo `resolver_rc` y nadie acumulaba
        # fallos: Binance devolviendo 418 podia estar rompiendo TODA la
        # medicion durante semanas con consecutive_failures: 0 y sin aviso.
        st["resolver_consecutive_failures"] = (
            st.get("resolver_consecutive_failures", 0) + 1 if rc_v != 0 else 0)
    else:
        st["resolver_rc"] = None
        st["resolver_tail"] = "no se resuelve: el grabador fallo (no es un SKIP)"
    st["recorder_rc"] = rc_r
    st["recorder_tail"] = out_r.strip().splitlines()[-1] if out_r.strip() else ""

    # El notificador solo se evalua si el grabador registro algo justo antes:
    # notificar sin dato nuevo es re-notificar el mismo veredicto.
    #
    # BUG QUE CASI SE ENTREGA: esto llamaba al notificador con --dry-run, asi
    # que NUNCA enviaba nada y la Fase 4 entera era decoracion — el mismo fallo
    # de "un control que se calcula, se imprime y nunca se consume". Se envia de
    # verdad; el anti-spam lo garantiza el CAMBIO DE SESGO del notificador, no
    # un dry-run aqui.
    if recorded and "recorded" in out_r:
        nargs = ["--once"] + (["--dry-run"] if dry_run else [])
        rc_n, out_n = run_child(NOTIFIER, nargs, timeout=150)
        st["notify_rc"] = rc_n
        st["notify_mode"] = "dry-run" if dry_run else "send"
        st["notify_tail"] = out_n.strip().splitlines()[-1] if out_n.strip() else ""
        st["notify_consecutive_failures"] = (
            st.get("notify_consecutive_failures", 0) + 1 if rc_n != 0 else 0)
    else:
        st["notify_rc"] = None
        st["notify_mode"] = None
        st["notify_tail"] = "sin veredicto nuevo: no se evalua notificacion"

    # Sin dato seguido: SE VE, pero no es un crash del proceso, asi que no
    # consume el presupuesto de reintentos. Matar el servicio porque ATAS este
    # caido era exactamente lo que dejaba al reader muerto y sin relanzador
    # (el unico relanzador es el acceso Reader.lnk de Inicio de sesion).
    skips = (st.get("consecutive_skips", 0) + 1) if rc_r == RECORDER_SKIP else 0
    st["consecutive_skips"] = skips
    st["no_data"] = bool(skips >= MAX_CONSECUTIVE_SKIPS)
    if skips == MAX_CONSECUTIVE_SKIPS:
        log(f"ATAS sin dato {skips} ciclos seguidos (~"
            f"{skips * RECORD_EVERY_S / 60:.0f} min): servicio vivo y listo para "
            f"reanudar solo. ultimo: {st['recorder_tail'][:70]}")

    ok = healthy
    write_status(st)
    return ok, st.get("recorder_tail", "")


def _loop_body(interval: float, dry_run: bool = False) -> int:
    log(f"reader supervisor started (interval {interval:.0f}s, child "
        f"=record + notificar si cambia)")
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    fails = 0
    attempts = 0
    paused_was = False
    # Una sola vez por proceso: se usa para limpiar el gave_up viejo al primer
    # ciclo correcto (ver comentario dentro del bucle).
    _cleared_gave_up = [False]
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

        # gave_up es un flag que ESCRIBE UN PROCESO AL MORIR y que nadie limpia
        # despues. Medido 2026-10-14: el supervisor del 14-oct agoto 20
        # reintentos a las 11:45 y dejo gave_up=true al salir; al relanzarlo,
        # el proceso nuevo ciclaba con exito (recorder/resolve/notify rc=0) pero
        # el archivo seguia diciendo gave_up=true. Cualquiera que leyera el
        # status —un monitor, un humano— veia "servicio muerto" con el servicio
        # vivo y grabando.
        #
        # La rama de recuperacion de abajo NO lo arregla: exige paused_was, que
        # a su vez exige attempts>0, y en un proceso recien arrancado attempts
        # arranca en 0. Solo el primer ciclo correcto de ESTE proceso puede
        # decir que el gave_up anterior ya no describe la realidad.
        # 2026-10-15 — ESTADO FANTASMA DEL PROCESO ANTERIOR.
        #
        # `gave_up` era un flag que ESCRIBE UN PROCESO AL MORIR y que nadie
        # limpia: medido 2026-10-14, el supervisor agoto 20 reintentos a las
        # 11:45, dejo gave_up=true al salir, y al relanzarlo el proceso nuevo
        # ciclaba bien pero el archivo seguia diciendo "servicio muerto".
        #
        # Aqui vivia la limpieza SOLO de `gave_up`. Pero `paused`, `paused_at`,
        # `retry_attempt`, `retry_backoff_s` y `degraded` se escriben igual y se
        # limpian igual de poco: el status de hoy (10:10) seguia anunciando
        # `paused_at: 09:41` con el servicio grabando perfectamente. Es el MISMO
        # fallo —un estado que describe a un proceso que ya no existe— y
        # limpiarlo solo en la rama de recuperacion no basta: esa rama exige
        # `paused_was`, que exige attempts>0, y en un proceso recien arrancado
        # attempts vale 0.
        #
        # El primer ciclo correcto de ESTE proceso es la unica prueba de que el
        # estado anterior ya no describe la realidad.
        if ok and not _cleared_gave_up[0]:
            _cleared_gave_up[0] = True
            stale = [k for k in ("gave_up", "paused", "paused_at",
                                 "retry_attempt", "retry_backoff_s",
                                 "degraded", "degraded_at") if k in st]
            if stale:
                for k in ("gave_up", "paused", "paused_at", "retry_attempt",
                          "retry_backoff_s", "degraded", "degraded_at"):
                    st.pop(k, None)
                st["gave_up"] = False
                st["gave_up_cleared_at"] = datetime.now().isoformat()
                log(f"estado del proceso anterior ya no aplica ({', '.join(stale)}): "
                    f"este proceso cicla bien. limpiado a las "
                    f"{st['gave_up_cleared_at']}")

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
                # 2026-10-15 — CARRIL LENTO en vez de MORIR.
                #
                # El diseño original era "queda PARADO y visible, no en bucle"
                # (ver arriba). La premisa — que alguien lo veria — es
                # empiricamente FALSA: el gave_up del 14-oct a las 11:45 paso
                # horas inadvertido hasta que se relanzo a mano. Y la auditoria
                # de fiabilidad confirmo que NADA relanza este proceso:
                # reader_supervisor.bat es de un solo tiro (lanza y sale), no
                # hay tarea programada, y el unico relanzador es el acceso
                # Reader.lnk de Inicio de sesion.
                #
                # O sea: "rendirse" no era "quedar visible", era MORIR — y morir
                # por una causa transitoria (la base bloqueada un rato, un
                # payload raro del puente) dejaba al reader sin grabar para
                # siempre.
                #
                # El carril lento conserva la propiedad que el diseño original
                # protegia: NO hay bucle rapido. Un intento cada
                # RETRY_EXHAUSTED_S, con `degraded` marcado en el status, y se
                # recupera solo cuando la causa cede. Y NO se relanza ningun
                # proceso — solo se reintenta la lectura; un reinicio de proceso
                # es lo que provoco la pila de dialogos del incidente "Already
                # running".
                log(f"AGOTADOS {attempts} reintentos seguidos: paso a CARRIL "
                    f"LENTO (un intento cada {RETRY_EXHAUSTED_S / 60:.0f} min). "
                    f"NO me rindo: nada me relanza, rendirse es morir. Me "
                    f"recupero solo cuando la causa ceda. ultimo: {tail[:70]}")
                st["paused"] = True
                st["gave_up"] = False
                st["degraded"] = True
                st["degraded_at"] = datetime.now().isoformat()
                write_status(st)
                backoff = RETRY_EXHAUSTED_S
                attempts = MAX_RETRY_ATTEMPTS      # no crece sin limite
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
            # paused_at SE ESCRIBIA al pausar pero no se borraba aqui: el status
            # vivo dejo visible `paused_at` con `gave_up=false` y sin `paused`,
            # o sea una pausa que no existe. Cualquier lector la veia.
            st.pop("paused_at", None)
            st.pop("retry_attempt", None)
            st.pop("retry_backoff_s", None)
            # Tambien el carril lento: si no se limpia, el status sigue
            # anunciando "degradado" con el servicio ya sano — el mismo fallo de
            # estado fantasma que sufria `paused_at` unas lineas mas arriba.
            st.pop("degraded", None)
            st.pop("degraded_at", None)
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
    # La senal MAS importante (servicio rendido) no salia aqui: solo se
    # imprimian recorder/notify/consecutive_failures, asi que `--status`
    # podia verse verde con gave_up=true. Es justo lo que un humano mira
    # para saber si el reader esta vivo.
    for k in ("gave_up", "gave_up_cleared_at", "paused", "paused_at",
              "degraded", "degraded_at", "retry_attempt", "recovered_at"):
        if k in st:
            print(f"  {k:14s} {st.get(k)}")
    for k in ("recorder_rc", "recorder_tail", "consecutive_failures",
              "consecutive_skips", "no_data", "resolver_rc", "resolver_tail",
              "resolver_consecutive_failures", "notify_rc", "notify_tail",
              "notify_consecutive_failures"):
        print(f"  {k:14s} {st.get(k)}")
    if st.get("gave_up"):
        print("  AVISO: gave_up=true — NADA relanza el supervisor; hace falta "
              "start-reader.ps1 (o el acceso Reader.lnk de Inicio de sesion)")
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