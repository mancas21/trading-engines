"""Fase 5 — registro de evidencia. Cada veredicto se guarda, incluso NEUTRAL.

POR QUÉ ESTO ES LO MÁS IMPORTANTE DEL REDISEÑO
==============================================
Sin esto no habrá dataset nunca. El 2026-10-06 audité28 días de señales y la
respuesta fue "no hay edge, y con este tamaño de muestra tampoco puedo
probarlo". Ese impasse se repite EXACTAMENTE igual en 4 meses si no se empieza a
grabar hoy.

Lo que se guarda es el VEREDICTO (reglas 6/7 + scorecard), no una señal. Eso
importa porque el veredicto es lo que la gente realmenteteenth. Si dentro de un
año "Rule 6 = MET" no predice nada, el problema es la regla, no el muestreo.

ESQUEMA
=======
  verdicts    un registro por lectura (snapshot inmutable, incl. NEUTRAL)
  outcomes    el retorno futuro que realmente ocurrió (resuelto después)

`outcomes` se resuelve con el MISMO criterio que usa el resto del motor: fill en
el OPEN de la barra siguiente, nunca en el close de la barra de señal. Es el
error más caro posible aquí, porque produciría edge inexistente.

USO
===
  python verdict_recorder.py record            # una lectura ahora
  python verdict_recorder.py resolve           # resuelve pendientes
  python verdict_recorder.py status            # cuántas filas hay
  python verdict_recorder.py loop --interval 30
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
import urllib.request

# La regla 8 necesita el umbral de perdida y la funcion de registro. Se importan
# arriba del todo (no dentro del bucle) para que un fallo de importacion se vea
# al arrancar y no enmascarado dentro de un try por veredicto.
import verdict


def _audit_loss_pct() -> float:
    """Umbral de la regla 8 (>1% contra), con 1% como respaldo."""
    try:
        return float(verdict.AUDIT_LOSS_PCT)
    except Exception:
        return 0.01
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DB = HERE / "reader" / "reader.db"
BRIDGE = "http://127.0.0.1:8000"
LIVE_MAX_AGE_S = 300.0          # misma regla live-only que el resto del sistema
DEFAULT_HORIZON_BARS = 12

# Reglas 6/7 de atas-bridge/rules.json v3.3
R6 = {"levels_min": 4, "dom_imbalance_max": 5.0}
R7 = {"levels_min": 3, "dom_imbalance_min": 10.0}


def get(path: str, timeout: float = 25.0):
    try:
        with urllib.request.urlopen(f"{BRIDGE}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:70]}"}


def ema(vals):
    return vals[-1] if vals else None


def classify_footprint(top_levels):
    """Regla 6 cuenta niveles con bid%>70; regla 7 con bid%<30.

    La asimetría es intencional (4 vs 3): un footprint con 3 niveles
    predominantemente vendedores NO satisface ninguna de las dos.
    Lados sin contraparte (ask==0) cuentan como 100% bid y distorsionan el
    recuento, así que se marcan aparte.
    """
    sell_h = buy_h = thin = 0
    rows = []
    for lv in top_levels or []:
        if not isinstance(lv, dict):
            continue
        bid = lv.get("bid", 0) or 0
        ask = lv.get("ask", 0) or 0
        tot = bid + ask
        if not tot:
            continue
        bp = 100.0 * bid / tot
        is_thin = (bid == 0 or ask == 0)
        if is_thin:
            thin += 1
        if bp > 70.0:
            sell_h += 1
        elif bp < 30.0:
            buy_h += 1
        rows.append({"price": lv.get("price"), "bid_pct": round(bp, 1),
                     "delta": lv.get("delta", 0), "thin": is_thin})
    return sell_h, buy_h, thin, rows


def read_live():
    """Lee la fuente ATAS y exige frescura. Devuelve None si no es utilizable."""
    i = get("/indicators")
    if i.get("_error") or i.get("ticker") is None:
        return None, f"ATAS no disponible: {i.get('_error', 'payload nulo')}"
    ts = i.get("timestamp")
    if not ts:
        return None, "payload sin timestamp"
    age = time.time() - ts / 1000.0
    if age > LIVE_MAX_AGE_S:
        return None, f"dato NO vivo (edad {age:.0f}s > {LIVE_MAX_AGE_S:.0f}s)"

    dom = i.get("dom") or {}
    fp = i.get("footprint") or {}
    vol = i.get("volume") or {}
    tape = i.get("tape") or {}
    price = i.get("price") or {}

    sell_h, buy_h, thin, rows = classify_footprint(fp.get("top_levels"))

    bq = tape.get("buy_vol")
    sq = tape.get("sell_vol")
    if bq is None or sq is None:
        tr = tape.get("trades") or []
        bq = sum(t.get("q", 0) or 0 for t in tr if t.get("s") == "buy")
        sq = sum(t.get("q", 0) or 0 for t in tr if t.get("s") == "sell")
    tot = (bq or 0) + (sq or 0)
    tape_buy_pct = round(100.0 * bq / tot, 1) if tot else None

    ema_v = dom.get("imbalance_pct_ema")
    imbalance = dom.get("imbalance_pct")

    # ── Reglas 6/7: condiciones evaluadas por separado ──
    # (a) consecutive CVD bars vive en /cvd; aquí se registra lo observable y
    #     la regla se marca UNEVALUABLE si no hay serie.
    cv = get("/cvd")
    cvd_state = (cv.get("cvd") or cv).get(str(i.get("ticker")).upper()) or {}
    bd = cvd_state.get("bar_deltas") or []
    up = dn = 0
    for d in reversed(bd):
        if d > 0:
            if dn:
                break
            up += 1
        elif d < 0:
            if up:
                break
            dn += 1
        else:
            break
    cond_a6 = ("UNEVALUABLE" if len(bd) < 2 else
               ("MET" if dn >= 2 else "FAIL"))
    cond_a7 = ("UNEVALUABLE" if len(bd) < 2 else
               ("MET" if up >= 2 else "FAIL"))

    cond_b6 = "MET" if sell_h >= R6["levels_min"] else "FAIL"
    cond_b7 = "MET" if buy_h >= R7["levels_min"] else "FAIL"

    cond_c6 = ("UNEVALUABLE" if ema_v is None else
               ("MET" if ema_v < R6["dom_imbalance_max"] else "FAIL"))
    cond_c7 = ("UNEVALUABLE" if ema_v is None else
               ("MET" if ema_v > R7["dom_imbalance_min"] else "FAIL"))

    r6 = [cond_a6, cond_b6, cond_c6]
    r7 = [cond_a7, cond_b7, cond_c7]
    r6_ok = all(x == "MET" for x in r6)
    r7_ok = all(x == "MET" for x in r7)
    if r7_ok:
        bias = "BULLISH"
    elif r6_ok:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    # ── Scorecard: SEPARADO de la puerta, nunca la sustituye ──
    parts = {}
    parts["footprint_depth"] = min(25, 3 * len(rows))
    parts["dom_imbalance"] = (max(0, min(25, int(round(12.5 + ((imbalance or 0) / 100.0) * 12.5))))
                              if isinstance(imbalance, (int, float)) else 0)
    parts["tape_ratio"] = (max(0, min(20, int(round((tape_buy_pct or 0) / 100.0 * 20))))
                           if tape_buy_pct is not None else 0)
    vr = vol.get("volume_ratio")
    parts["volume_climax"] = (max(0, min(15, int(round(min(vr, 3.0) / 3.0 * 15))))
                              if isinstance(vr, (int, float)) and vr > 0 else 0)
    parts["footprint_confirmation"] = min(15, (5 if buy_h >= 3 else 0) + (5 if sell_h >= 3 else 0))
    score = sum(parts.values())

    rec = {
        "ts": time.time(),
        "symbol": str(i.get("ticker")).upper(),
        "bar_index": i.get("bar_index"),
        "bar_time": i.get("time"),
        "src_age_s": round(age, 1),
        "price_last": price.get("last"),
        "poc": fp.get("poc"),
        "value_area_low": fp.get("value_area_low"),
        "value_area_high": fp.get("value_area_high"),
        "levels": len(rows),
        "thin_levels": thin,
        "footprint_sell_heavy": sell_h,
        "footprint_buy_heavy": buy_h,
        "imbalance_pct": imbalance,
        "imbalance_pct_ema": ema_v,
        "imbalance_ema_n": dom.get("imbalance_pct_ema_n"),
        "tape_buy_pct": tape_buy_pct,
        "volume_ratio": vr,
        "cvd": cvd_state.get("cvd"),
        "cvd_bars": cvd_state.get("bars"),
        "cvd_streak_up": up,
        "cvd_streak_down": dn,
        "r6_a": cond_a6, "r6_b": cond_b6, "r6_c": cond_c6, "r6_met": int(r6_ok),
        "r7_a": cond_a7, "r7_b": cond_b7, "r7_c": cond_c7, "r7_met": int(r7_ok),
        "bias": bias,
        "score": score,
        "score_parts": json.dumps(parts),
        "horizon_bars": DEFAULT_HORIZON_BARS,
        "fill_price": None,
        "forward_return": None,
        "resolved": 0,
    }
    return rec, None


SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  symbol TEXT NOT NULL,
  bar_index INTEGER,
  bar_time TEXT,
  src_age_s REAL,
  price_last REAL,
  poc REAL, value_area_low REAL, value_area_high REAL,
  levels INTEGER, thin_levels INTEGER,
  footprint_sell_heavy INTEGER, footprint_buy_heavy INTEGER,
  imbalance_pct REAL, imbalance_pct_ema REAL, imbalance_ema_n INTEGER,
  tape_buy_pct REAL, volume_ratio REAL,
  cvd REAL, cvd_bars INTEGER, cvd_streak_up INTEGER, cvd_streak_down INTEGER,
  r6_a TEXT, r6_b TEXT, r6_c TEXT, r6_met INTEGER,
  r7_a TEXT, r7_b TEXT, r7_c TEXT, r7_met INTEGER,
  bias TEXT NOT NULL,
  score INTEGER NOT NULL,
  score_parts TEXT,
  horizon_bars INTEGER NOT NULL,
  fill_price REAL,
  forward_return REAL,
  resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_verdicts_ts ON verdicts(ts);
CREATE INDEX IF NOT EXISTS ix_verdicts_unres ON verdicts(resolved);
CREATE TABLE IF NOT EXISTS recorder_meta (
  k TEXT PRIMARY KEY, v TEXT
);
"""


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=20)
    c.executescript(SCHEMA)
    return c


def do_record(c) -> int:
    rec, err = read_live()
    if rec is None:
        print(f"  SKIP: {err}")
        return 0
    cols = ("ts,symbol,bar_index,bar_time,src_age_s,price_last,poc,value_area_low,"
            "value_area_high,levels,thin_levels,footprint_sell_heavy,"
            "footprint_buy_heavy,imbalance_pct,imbalance_pct_ema,imbalance_ema_n,"
            "tape_buy_pct,volume_ratio,cvd,cvd_bars,cvd_streak_up,cvd_streak_down,"
            "r6_a,r6_b,r6_c,r6_met,r7_a,r7_b,r7_c,r7_met,bias,score,score_parts,"
            "horizon_bars")
    vals = tuple(rec[k] for k in (
        "ts", "symbol", "bar_index", "bar_time", "src_age_s", "price_last", "poc",
        "value_area_low", "value_area_high", "levels", "thin_levels",
        "footprint_sell_heavy", "footprint_buy_heavy", "imbalance_pct",
        "imbalance_pct_ema", "imbalance_ema_n", "tape_buy_pct", "volume_ratio",
        "cvd", "cvd_bars", "cvd_streak_up", "cvd_streak_down",
        "r6_a", "r6_b", "r6_c", "r6_met", "r7_a", "r7_b", "r7_c", "r7_met",
        "bias", "score", "score_parts", "horizon_bars"))
    c.execute(f"INSERT INTO verdicts ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    c.execute("INSERT OR REPLACE INTO recorder_meta VALUES ('last_record_ts', ?)",
              (str(rec["ts"]),))
    c.commit()
    print(f"  recorded {rec['symbol']} bar#{rec['bar_index']} bias={rec['bias']} "
          f"score={rec['score']} r6={rec['r6_met']} r7={rec['r7_met']}")
    return 1


def do_resolve(c) -> int:
    """Resolve pendientes: fill en el OPEN de la barra siguiente al horizonte.

    Nunca se usa el close de la barra de señal: ese precio solo se conoce
    después de que la barra cerró, y llenarse ahí inventa edge.
    """
    sym = c.execute("SELECT v FROM recorder_meta WHERE k='symbol'").fetchone()
    if not sym:
        row = c.execute("SELECT symbol FROM verdicts ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            print("  no hay verdicts que resolver")
            return 0
        sym = (row[0],)
    pending = c.execute(
        "SELECT id,symbol,bar_index,horizon_bars,bias FROM verdicts "
        "WHERE resolved=0 AND bar_index IS NOT NULL ORDER BY id").fetchall()
    if not pending:
        print("  nada pendiente")
        return 0

    # El sesgo va con cada veredicto: la regla 8 necesita saber si la perdida es
    # CONTRA lo que el sistema Recommended, no si el precio bajo.
    row_bias = {r[0]: r[4] for r in pending}

    by_sym: dict[str, list] = {}
    for vid, s, bi, hz, _bs in pending:
        by_sym.setdefault(s, []).append((vid, bi, hz))

    resolved = 0
    for s, items in by_sym.items():
        api = s.split("/")[0].replace("-", "").upper()
        url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={api}"
               f"&interval=15m&limit=1500")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "reader/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = json.loads(r.read().decode())
        except Exception as e:
            print(f"  {s}: klines failed {type(e).__name__}")
            continue
        # index bar_index -> open time. Binance bar_index is its own numbering,
        # so resolve by TIME using the recorded bar_time instead of the index.
        by_time = {int(k[0]): k for k in rows}
        # Bar identity MUST be the ms epoch, never the HH:MM:SS string.
        # Measured: matching ATAS `bar_time` ("00:05:00") against Binance 15m
        # opens matched ZERO bars -- ATAS stamps bar_time in a different clock
        # than the exchange. Nearest-open on the recorded epoch is unambiguous.
        opens = [int(k[0]) for k in rows]
        for vid, bi, hz in items:
            r2 = c.execute("SELECT ts, horizon_bars FROM verdicts WHERE id=?",
                           (vid,)).fetchone()
            if not r2 or not r2[0]:
                continue
            h = hz or 12
            rec_ms = r2[0] * 1000.0
            # The signal bar is the one CONTAINING the record instant.
            sig_i = None
            for j, o in enumerate(opens):
                if o <= rec_ms:
                    sig_i = j
                else:
                    break
            if sig_i is None:
                continue
            fill_i = sig_i + 1                       # fill at the NEXT bar's open
            exit_i = sig_i + h
            if exit_i >= len(rows):
                continue
            fill_open = float(rows[fill_i][1])       # open, not close
            exit_px = float(rows[exit_i][4])
            fr = (exit_px - fill_open) / fill_open if fill_open else None
            if fr is None:
                continue
            c.execute("UPDATE verdicts SET fill_price=?, forward_return=?, resolved=1 "
                      "WHERE id=?", (round(fill_open, 10), round(fr, 8), vid))
            resolved += 1
            # ── REGLA 8 (Audit On Loss) ──────────────────────────────────────
            # rules.json v3.4.1: "After a call that fails (price moves >1%
            # against within 2 bars), record loss with root cause".
            #
            # El threshold de la regla es 1% en 2 barras; aqui el horizonte ya
            # viene resuelto por veredicto (horizon_bars). Lo que se registra es
            # la perdida CONTRA el sesgo del veredicto, que es la direccion que el
            # sistema habria tomdo: un BULLISH que cae es la perdida, un BEARISH
            # que sube tambien. Sin esa correccion, un bearish que va a favor se
            # contaria como perdida y el registro diria lo contrario de lo que
            # ocurrio.
            _bias = row_bias.get(vid)
            _directed = fr if _bias == "BULLISH" else (-fr if _bias == "BEARISH" else None)
            if _directed is not None and _directed < -_audit_loss_pct():
                try:
                    verdict._audit_record_loss(
                        symbol=s, price_last=exit_px, price_entry=fill_open,
                        loss_pct=_directed,
                        root_cause=(f"veredicto {_bias} no confirmo en {hz} barras: "
                                    f"{_directed*100:.2f}% contra"))
                except Exception as _e:
                    print(f"  regla 8: no se pudo registrar la perdida ({_e})")
    c.commit()
    print(f"  resolved {resolved}/{len(pending)}")
    return resolved


def do_status(c) -> int:
    n, nb = c.execute("SELECT COUNT(*), SUM(bias='BULLISH')+SUM(bias='BEARISH') "
                      "FROM verdicts").fetchone()
    res = c.execute("SELECT COUNT(*) FROM verdicts WHERE resolved=1").fetchone()[0]
    span = c.execute("SELECT MIN(ts), MAX(ts) FROM verdicts").fetchone()
    print(f"  verdicts : {n}  (resolved {res})")
    if span and span[0]:
        d = (span[1] - span[0]) / 86400.0
        print(f"  span     : {d:.2f} days")
    for r in c.execute("SELECT bias, COUNT(*) FROM verdicts GROUP BY bias "
                       "ORDER BY 2 DESC"):
        print(f"    {r[0]:8s} {r[1]}")
    for r in c.execute("SELECT r6_met, COUNT(*) FROM verdicts GROUP BY r6_met"):
        print(f"    rule6_met={r[0]} {r[1]}")
    for r in c.execute("SELECT r7_met, COUNT(*) FROM verdicts GROUP BY r7_met"):
        print(f"    rule7_met={r[0]} {r[1]}")
    print(f"\n  target for an edge verdict: 5000 verdicts "
          f"(need {max(0, 5000 - n)} more)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["record", "resolve", "status", "loop"])
    ap.add_argument("--interval", type=float, default=30.0)
    a = ap.parse_args()
    c = db()
    if a.cmd == "record":
        return 0 if do_record(c) else 1
    if a.cmd == "resolve":
        do_resolve(c)
        return 0
    if a.cmd == "status":
        return do_status(c)
    print(f"  loop: record every {a.interval:.0f}s + resolve every 5th tick (Ctrl-C to stop)")
    tick = 0
    while True:
        try:
            do_record(c)
            tick += 1
            if tick % 5 == 0:
                do_resolve(c)
        except KeyboardInterrupt:
            print("\n  stopped")
            return 0
        except Exception as e:
            print(f"  loop error: {type(e).__name__}: {str(e)[:90]}")
        time.sleep(a.interval)


if __name__ == "__main__":
    raise SystemExit(main())