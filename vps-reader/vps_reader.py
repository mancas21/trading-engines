#!/usr/bin/env python3
"""vps_reader.py — grabador headless de VENUES, 24/7 en el VPS.

POR QUE EXISTE
==============
El reader del PC necesita ATAS (footprint/DOM/tape) y TradingView Desktop, y el
PC se apaga cada noche. Medido sobre reader.db el 2026-10-16: 195,3 h de motor
apagado de 232 h de registro (16 % de cobertura), y la caida del 16-oct
(-4,7 %, 0.3140 -> 0.2993) ocurrio ENTERA dentro de un hueco de 17,49 h.

La condicion (c) — consenso de libro multi-venue — NO depende de ATAS: la sirve
scanner-engine, que YA corre 24/7 en este VPS via systemd. Esto la graba de
forma continua e ininterrumpida.

QUE EVALUA Y QUE NO — sin adornos
=================================
  (c) consenso multi-venue   EVALUABLE   scanner-engine :8090 (unica fuente viva
                                         en el VPS: 4 venues por REST publico)
  (a) racha de CVD           UNEVALUABLE el CVD del PC lo sirve atas-bridge por
                                         WebSocket de Binance; desde la IP de
                                         este VPS Binance responde HTTP 418
                                         (throttled). Se DECLARA, no se
                                         sustituye por otro venue: un CVD de
                                         okx/bybit/kraken NO es el CVD de
                                         Binance y compararlos seria inventar.
  (b) footprint              UNEVALUABLE el footprint lo sirve ATAS, app de
                                         escritorio de Windows.

Por eso NO emite señales direccionales: las reglas 6/7 son (a) AND (b) AND (c) y
sin dos de las tres nunca pueden cumplirse. ES UN GRABADOR, NO UN ORACULO.

Su valor concreto: (c) es justo la condicion que FALLA su propio criterio
pre-registrado (rules.json 7.1: <=10 transiciones MET<->no-MET por dia; medido
61-63/dia sobre 27,5 h) y que NO se puede decidir bien sin datos de dias
completos. En el PC eso es imposible: el motor solo vive el 16 % del tiempo.
Aqui si.

PATRON (copiado de paper-monitor)
=================================
Proceso CORTO, idempotente e independiente, lanzado por cron cada minuto. Fallo
de red -> "CYCLE SKIPPED", estado intacto, el siguiente tick de cron retoma.
Nada que se quede colgado y nada que haya que "arrancar bien".

UNA SOLA IMPLEMENTACION DE (c)
==============================
No reimplementa nada. Importa `verdict.py` (el mismo fichero que usa el gate del
PC, copiado en el deploy) y llama a `verdict.dom_vac_eval()`. La calibracion
tambien se reutiliza sin cambios: el esquema de la tabla `verdicts` es el MISMO
que el del reader, asi que `build_dom_vac_calibration.py` funciona tal cual.

CADENCIA — la diferencia que hay que saber
==========================================
El reader del PC corre cada ~30 s. Aqui cron da 1 minuto como minimo, asi que
`dom_vac_polls_min = 2` significa 2 minutos de hold en vez de 1. `_DOM_GATE_POLL_S`
(15 s) y `_DOM_GATE_STALE_S` (120 s) del propio verdict.py siguen satisfechos: un
tick de cron es una observacion nueva y la racha no muere entre ticks.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
READER_DIR = HERE / "reader"
# verdict.py vive en reader/ y resuelve `HERE.parent/atas-bridge/rules.json`,
# asi que el arbol desplegado replica el del repo:
#   vps-reader/{vps_reader.py, reader/{verdict.py,reader.db,...}, atas-bridge/rules.json}
sys.path.insert(0, str(READER_DIR))

DB = READER_DIR / "reader.db"
GATE_STATE = READER_DIR / "dom_gate_state.json"
SYMBOL = (os.environ.get("VPS_READER_SYMBOL") or "JUPUSDT").upper()

UNEVALUABLE = "UNEVALUABLE"
REASON_A = ("sin fuente en el VPS: el CVD lo sirve atas-bridge por WebSocket de "
            "Binance; desde esta IP Binance responde HTTP 418 (throttled)")
REASON_B = "sin fuente en el VPS: el footprint lo sirve ATAS (escritorio Windows)"

# El gate se IMPORTA. Cualquier fallo aqui es un fallo de despliegue, no de datos:
# por eso este import va fuera del try/except de red del ciclo.
import verdict  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                   REAL NOT NULL,
  ts_iso               TEXT,
  symbol               TEXT,
  pair                 TEXT,
  run_no               INTEGER,
  host                 TEXT,

  -- Condicion (c) — lo unico evaluable aqui
  venues_live          INTEGER,
  c_buy_state          TEXT,
  c_sell_state         TEXT,
  c_buy_streak         INTEGER,
  c_sell_streak        INTEGER,
  c_counts             TEXT,
  c_transition_buy     INTEGER,
  c_transition_sell    INTEGER,
  c_transitions_today  INTEGER,

  -- Reglas 6/7 tal y como las compone el gate (a/b son siempre UNEVALUABLE aqui)
  r6_a TEXT, r6_b TEXT, r6_c TEXT, r6_met INTEGER,
  r7_a TEXT, r7_b TEXT, r7_c TEXT, r7_met INTEGER,
  bias TEXT,
  note TEXT,

  -- Columnas que existen para no divergir del esquema del reader y poder
  -- sincronizar/analizar con las mismas herramientas. Aqui quedan a NULL.
  cvd REAL,
  footprint_sell_heavy INTEGER,
  footprint_buy_heavy INTEGER,

  -- Crudos de (c): {"raw": {venue: book_imb_5}, "counts": {buy, sell}}.
  -- MISMO formato que reader.verdicts.dom_vac_raw — de ahi que el generador de
  -- calibracion del PC valga sin cambios.
  dom_vac_raw TEXT
);
CREATE TABLE IF NOT EXISTS vps_meta (k TEXT PRIMARY KEY, v TEXT);
"""


# ── utilidades de fila ─────────────────────────────────────────────────────

def _is_met(state) -> bool:
    """MET es MET. SPLIT, FAIL y UNEVALUABLE NO son MET.

    Deliberadamente explicito: el criterio pre-registrado cuenta transiciones
    'MET <-> no-MET', y tratar SPLIT como un tercer estado haria que un mercado
    partido que va y viene contase como 0 transiciones, que es justo el error que
    se quiere medir.
    """
    return state == "MET"


def _day_start_utc(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _streaks() -> tuple[int, int]:
    """Rachas del gate multi-venue. Se LEEN del estado que verdict.py acaba de
    escribir; no se recalculan (seria una segunda implementacion de 'hold')."""
    try:
        st = json.loads(GATE_STATE.read_text(encoding="utf-8"))
        rec = st.get(SYMBOL) or {}
        return int(rec.get("buy", 0) or 0), int(rec.get("sell", 0) or 0)
    except Exception:                                   # noqa: BLE001
        return 0, 0


def _prev_states(con: sqlite3.Connection):
    row = con.execute(
        "SELECT c_buy_state, c_sell_state FROM verdicts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def cycle() -> int:
    now = time.time()
    iso = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    con = sqlite3.connect(DB, timeout=10)
    try:
        con.executescript(SCHEMA)
        prev_buy, prev_sell = _prev_states(con)
        run_no = (con.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] or 0) + 1

        # ── (c): la unica condicion con fuente en el VPS ──
        # observe=True: este tick ES la observacion que avanza la racha de
        # persistencia (el gate del PC lee con observe=False porque alli el
        # grabador es el que observa; aqui grabador y gate son el mismo proceso).
        gate = verdict.dom_vac_eval(SYMBOL, observe=True)
        buy = gate.get("buy") or {}
        sell = gate.get("sell") or {}
        buy_state = buy.get("state") or UNEVALUABLE
        sell_state = sell.get("state") or UNEVALUABLE
        venues = gate.get("venues")

        # ── El CRUDO se graba SIEMPRE, con o sin calibración ──────────────────
        #
        # dom_vac_eval devuelve `raw`/`counts` SOLO cuando hay calibración
        # (verdict.py 395-403: sin banda no hay votos que contar). Grabar
        # dependiendo del gate seria un huevo-y-gallina: la calibración se
        # construye DESDE dom_vac_raw, asi que sin crudo nunca habria banda y el
        # grabador quedaria mudo para siempre. `_scanner_book_imb5` es la MISMA
        # funcion que usa el gate (GET sin cache ni efectos): aqui es la entrada
        # del grabador, no una segunda implementacion de nada.
        tb_raw = verdict._scanner_book_imb5(SYMBOL)
        raw = gate.get("raw") or ({k: round(float(v), 4) for k, v in tb_raw.items()}
                                  if tb_raw else {})
        # `counts` NO se inventa a cero: sin calibración es DESCONOCIDO (None),
        # y 0 significaria "ningun venue vota", que es un claim distinto.
        counts = gate.get("counts")
        b_streak, s_streak = _streaks()

        # ── (a) y (b): sin fuente. Se declaran UNEVALUABLE, nunca FAIL ──
        # FAIL diria "se midio y no se cumplia"; aquí no se ha medido nada.
        r6 = (UNEVALUABLE, UNEVALUABLE, sell_state)
        r7 = (UNEVALUABLE, UNEVALUABLE, buy_state)
        r6_met = 1 if all(x == "MET" for x in r6) else 0
        r7_met = 1 if all(x == "MET" for x in r7) else 0

        # El contrato de verdict.py: una condicion UNEVALUABLE fuerza NEUTRAL.
        # Como (a) y (b) lo son SIEMPRE en el VPS, el sesgo es siempre NEUTRAL —
        # y eso es informacion, no un fallo: significa "el gate no puede opinar".
        bias = "NEUTRAL"
        note = ("headless: (a) y (b) UNEVALUABLE -> la regla AND no puede "
                "cumplirse; (c) si evaluada desde scanner multi-venue")

        # ── transiciones MET<->no-MET de (c) (criterio pre-registrado 7.1) ──
        t_buy = t_sell = 0
        if prev_buy is not None:
            t_buy = 1 if _is_met(buy_state) != _is_met(prev_buy) else 0
            t_sell = 1 if _is_met(sell_state) != _is_met(prev_sell) else 0
        t_today = con.execute(
            "SELECT COUNT(*) FROM verdicts WHERE ts >= ? "
            "AND (c_transition_buy = 1 OR c_transition_sell = 1)",
            (_day_start_utc(now),),
        ).fetchone()[0] or 0
        t_today += (1 if (t_buy or t_sell) else 0)

        con.execute(
            """INSERT INTO verdicts (ts, ts_iso, symbol, pair, run_no, host,
                 venues_live, c_buy_state, c_sell_state, c_buy_streak,
                 c_sell_streak, c_counts, c_transition_buy, c_transition_sell,
                 c_transitions_today, r6_a, r6_b, r6_c, r6_met, r7_a, r7_b, r7_c,
                 r7_met, bias, note, cvd, footprint_sell_heavy,
                 footprint_buy_heavy, dom_vac_raw)
               VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?, NULL, NULL, NULL, ?)""",
            (now, iso, SYMBOL, verdict._symbol_to_scanner_pair(SYMBOL), run_no,
             socket.gethostname(), venues, buy_state, sell_state, b_streak,
             s_streak, (json.dumps(counts, sort_keys=True) if counts else None),
             t_buy, t_sell,
             t_today, r6[0], r6[1], r6[2], r6_met, r7[0], r7[1], r7[2], r7_met,
             bias, note, json.dumps({"raw": raw, "counts": counts})),
        )
        con.commit()

        # ── dashboard: UNA linea por ciclo (esto es lo que crece en cycle.log) ──
        raw_txt = " ".join(f"{k}={raw[k]:+.3f}" for k in sorted(raw)) or "-"
        cnt_txt = (f"{counts.get('buy', '?')}C/{counts.get('sell', '?')}V"
                   if counts else "sin-calib")
        calib = "si" if (READER_DIR / "dom_vac_calibration.json").exists() else "NO"
        print(
            f"{iso} run={run_no} {SYMBOL} venues={venues} calib={calib} | "
            f"(c) sell={sell_state}({s_streak}) buy={buy_state}({b_streak}) "
            f"counts={cnt_txt} trans_today={t_today} | [{raw_txt}] | "
            f"(a){UNEVALUABLE} (b){UNEVALUABLE} -> {bias}"
        )
        return 0
    finally:
        con.close()


def main() -> int:
    try:
        return cycle()
    except Exception as e:                              # noqa: BLE001
        # Fallo de red/API -> la corrida se salta, estado intacto, el siguiente
        # tick de cron retoma. Mismo contrato que paper_monitor.py.
        print(f"CYCLE SKIPPED: {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
