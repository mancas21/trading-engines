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

# Codigos de salida del subcomando `record`. Son CONTRATO con el supervisor
# (reader_supervisor.py RECORDER_SKIP): 2 es "no hay dato", condicion normal,
# y NO consume el presupuesto de crashes. Ver do_record().
RECORDED = 0    # se escribio una fila
SKIP = 2        # sin dato utilizable (ATAS caido/viejo): no es un fallo
FAIL = 1        # error real (excepcion: base bloqueada, payload roto, ...)

# Reglas 6/7 de atas-bridge/rules.json. Los niveles de footprint se leen del
# modulo verdict (que los carga de rules.json) — antes estaban transcritos a
# mano AQUI con otros numeros, y la condicion (c) se evaluaba con umbrales
# (dom 5/10) que rules.json ya no declara: el ledger media una regla distinta
# de la que la puerta decia aplicar. La (c) ahora es la MISMA funcion que usa
# la puerta: verdict.dom_vac_eval (consenso multi-venue del scanner).
# ... y ahora TAMBIEN los niveles de footprint: verdict.RULE6["levels_min"] /
# verdict.RULE7["levels_min"], que verdict.py carga del mismo rules.json que la
# puerta. Estaban transcritos a mano aqui como {4} y {3} — justamente lo que el
# comentario de arriba dice que ya no se hace. Dos numeros hardcodeados que
# hubieran dejado de seguir a rules.json en cuanto alguien los tocara.


def get(path: str, timeout: float = 25.0):
    try:
        with urllib.request.urlopen(f"{BRIDGE}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:70]}"}


# ── IDENTIDAD DE BARRA (fix 2026-10-13) ──────────────────────────────────────
# POR QUE: bar_index es el indice POSICIONAL de ATAS sobre la serie del chart.
# Se reinicia desde 0 cuando el chart recarga o cambia de timeframe, asi que el
# MISMO numero vuelve a aparecer en dias distintos. Medido en reader.db: 109 de
# 127 indices aparecian en mas de un dia; bar#1163 cubria 7 dias con 27 filas.
# Medido ademas: los PRECIOS de esas filas son correctos (400/400 dentro del
# rango 5m real de Binance) — el fallo es de IDENTIDAD, no de precio. Por eso
# forward_return (que resuelve por TIEMPO) no estaba sesgado; lo que estaba roto
# era cualquier agrupacion por barra.
#
# QUE HACE: una clave de barra UNICA. La ancla es la ETIQUETA DE BARRA que
# publica ATAS (bar_time = "HH:mm:ss" del open de la vela), NO el segundo de
# lectura: todas las lecturas de la misma vela deben colapsar en la misma clave.
#
# TRES CASOS QUE LA CLAVE TIENE QUE SEPARAR (los tres existen en produccion):
#
#  1) manyadas de lecturas de la MISMA vela  -> misma clave.
#     Ancla en la etiqueta de barra, no en el reloj.
#
#  2) la misma etiqueta HH:mm:ss en OTRO dia
#     -> clave distinta. De aqui el ancla en epoch: floor(ts/86400).
#
#  3) un RELOAD que hace retroceder el reloj de ATAS
#     -> clave distinta aunque coincida etiqueta y dia. De aqui el segmento:
#     cuenta cada retroceso MONOTONO, con la etiqueta del salto congelada para
#     que dos retrocesos consecutivos no colapsen en el mismo segmento.
#
# La clave NO cambia el sesgo ni el resolver: solo da una identidad fiable para
# agrupar y para las condiciones "2+ barras consecutivas".
_BAR_SEGMENT = 0          # segmentos consumidos; NUNCA decrece
_last_bar_key = None      # clave emitida en la ultima lectura
_last_tag = None          # etiqueta de la ultima vela
_last_day = None          # dia (epoch//86400) de la ultima vela


def bar_key(bar_time: str | None, wall_ts: float) -> tuple:
    """Clave de barra estable: (dia, etiqueta_HHMMSS, segmento).

    - dia:        epoch//86400. Separa el mismo reloj de otro dia.
    - etiqueta:   open de la vela en HH:mm:ss. Identifica la vela DENTRO del dia
                  y colapsa las muchas lecturas que caen en ella.
    - segmento:   contador monotono de reinicios. Separa dos velas con la misma
                  etiqueta y el mismo dia, que solo puede pasar tras un reload.

    Devuelve una tupla; el llamante la serializa como texto para la columna.
    """
    global _BAR_SEGMENT, _last_bar_key, _last_tag, _last_day

    tag = (bar_time or "").strip() or "?"
    day = int(wall_ts) // 86400

    # Retroceso = la vela va hacia atras respecto de la ultima observada.
    # Puede venir por cambio de dia (nuevo dia, misma etiqueta) o por reload
    # (retroceso real dentro del mismo dia). Ambos son un reinicio de la serie
    # de ATAS, y ambos necesitan un segmento nuevo.
    retrocede = False
    if _last_tag is not None:
        if day != _last_day:
            retrocede = True              # rollover de dia: el reloj de ATAS
        elif tag < _last_tag:             # retrocede a una vela anterior
            retrocede = True

    if retrocede:
        _BAR_SEGMENT += 1
    elif _last_bar_key is None:
        _BAR_SEGMENT = 0                  # primera lectura de la sesion

    key = (day, tag, _BAR_SEGMENT)
    _last_tag = tag
    _last_day = day
    _last_bar_key = key
    return key


def bar_key_str(key: tuple) -> str:
    """Serializa la clave de barra para la columna TEXT.

    Separador '|': la etiqueta de ATAS ya contiene ':' ("01:00:00"), asi que
    un separador de dos puntos haria la clave ambigua al partirla.
    """
    return "|".join(str(x) for x in key)


def ema(vals):
    return vals[-1] if vals else None


def classify_footprint(top_levels):
    """UNA sola implementación: delega en `verdict.classify_footprint`.

    2026-10-15 — DERIVA CORREGIDA. Había DOS clasificadores del footprint y ya
    discrepaban en tres cosas a la vez, pese a que el propio docstring del
    módulo documenta el fallo histórico ("la media una regla distinta de la
    que la puerta decía aplicar"):

      1. FRONTERA — la puerta contaba `bp >= 65`, este `bp > 65`. Un nivel con
         bid_pct exactamente 65.0 (bid 13 / ask 7, alcanzable con enteros)
         contaba para el veredicto publicado y NO para la fila de reader.db:
         `r6_b` del ledger y `b6` de la puerta podían ser distintos en la misma
         lectura, y sobre esa muestra se midió y se puso en cuarentena la
         regla 6.
      2. ORDEN del tuple — `(sell_h, buy_h, thin, rows)` aquí vs
         `(rows, sell_h, buy_h, thin)` allí: un anzuelo listo para el siguiente
         copy-paste.
      3. CAMPOS — aquí faltaban `bid`/`ask` y el `tag`, que la puerta sí
         publica.

    Devuelve el orden histórico de este módulo para no tocar sus call sites.
    """
    rows, sell_h, buy_h, thin = verdict.classify_footprint(
        top_levels, verdict.FOOTPRINT_PCT)
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
    up, dn = verdict.streak_from_bar_deltas(bd)

    # 2026-10-15 — EL LEDGER MEDIABA LA MISMA REGLA QUE LA PUERTA.
    #
    # Antes aqui se reimplementaba la condicion (a) a mano con `len(bd) < 2`
    # y un `>= 2` literal, mientras la puerta exigia ademas CONTIGUIDAD via
    # verdict.cvd_integrity (verdict.py:896-908). Con un hueco dentro de la
    # ventana la puerta emitia UNEVALUABLE y, por ser AND estricto, NEUTRAL; el
    # ledger grababa r6_a=MET en la misma lectura. Dos implementaciones de una
    # decision — la clase de fallo que este propio modulo documenta en su
    # docstring como ya ocurrida con los umbrales. Encima, esa muestra
    # inflaba rule6_fired_resolved y toda la medicion de la regla 6.
    int_a = verdict.cvd_integrity(cvd_state, verdict.RULE6["streak"],
                                  verdict.CVD_INTEGRITY_WINDOW)
    int_b = verdict.cvd_integrity(cvd_state, verdict.RULE7["streak"],
                                  verdict.CVD_INTEGRITY_WINDOW)
    if not int_a["ok"]:
        cond_a6 = "UNEVALUABLE"
    else:
        cond_a6 = ("UNEVALUABLE" if len(bd) < 2 else
                   ("MET" if dn >= verdict.RULE6["streak"] else "FAIL"))
    if not int_b["ok"]:
        cond_a7 = "UNEVALUABLE"
    else:
        cond_a7 = ("UNEVALUABLE" if len(bd) < 2 else
                   ("MET" if up >= verdict.RULE7["streak"] else "FAIL"))

    cond_b6 = "MET" if sell_h >= verdict.RULE6["levels_min"] else "FAIL"
    cond_b7 = "MET" if buy_h >= verdict.RULE7["levels_min"] else "FAIL"

    # (c) multi-venue: LA MISMA funcion que evalua la puerta (verdict.py).
    # Cada lectura del grabador es un ciclo del gate — ver verdict.dom_vac_eval.
    _gate = verdict.dom_vac_eval(str(i.get("ticker")).upper())
    cond_c6 = _gate["sell"]["state"]     # distribucion = lado vendedor del libro
    cond_c7 = _gate["buy"]["state"]      # acumulacion = lado comprador del libro

    r6 = [cond_a6, cond_b6, cond_c6]
    r7 = [cond_a7, cond_b7, cond_c7]
    r6_ok = all(x == "MET" for x in r6)
    r7_ok = all(x == "MET" for x in r7)

    # El sesgo viene de la PUERTA, y la puerta respeta `enforcement`.
    #
    # Antes esto era `elif r6_ok: bias = "BEARISH"` a secas, sin mirar el flag.
    # Efecto medido: la fila id=1479 (11/10 20:46:17) quedo grabada como
    # BEARISH por una regla YA DESHABILITADA (r6 MET/MET/MET), cuando la puerta
    # habria dicho NEUTRAL. reader.db publicaba una direccion que el motor no
    # habia dado — y ningun consumidor puede distinguir eso de una senal real.
    # Es el mismo desincronizado del scorecard: dos implementaciones de una
    # decision. El flag se lee de la MISMA funcion que usa verdict.py.
    r6_enforcement = verdict._rule_enforcement(6)
    if r7_ok:
        bias = "BULLISH"
    elif r6_ok and r6_enforcement != "disabled":
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    # ── SCORECARD: ELIMINADO (2026-10-13) ──
    #
    # Antes se llamaba verdict.scorecard() y se guardaba score/score_parts.
    # Se quito por medicion, no por gusto:
    #
    #   pearson(score, forward_return)  = +0.0175   (ruido)
    #   spearman(score, forward_return) = -0.0156
    #   tercil alto: winrate 32.8%, retorno medio -0.392%   <- el "mejor" score PERDIA MAS
    #   tercil bajo: winrate 37.9%, retorno medio -0.382%
    #
    #   y el componente de peso 50 (dom_ema = imbalance_pct_ema, el libro
    #   Binance que se RETIRO del gate por ruidoso) tenia spread +27.3pp sobre
    #   top150/bottom150 — pero con autocorr de forward_return rho=+0.995 eso
    #   son ~1 observacion independiente, z=0.35, NO significativo. Es el mismo
    #   error de muestra solapada que inhabilita la regla 6.
    #
    # Conclusion: el score no ordenaba, no predice, y su calibracion no tiene
    # respaldo estadistico. Publicarlo daba apariencia de informacion.
    #
    # Las columnas score/score_parts de reader.db se CONSERVAN con su historico
    # (1.205 filas medidas) para que la evidencia de esta decision siga siendo
    # auditable; las filas nuevas las dejan en NULL.
    #
    # El sesgo NUNCA dependio del score: sale de `if r7_ok / elif r6_ok / else`.
    # Esto no cambia ningun veredicto, quita un numero que no significaba nada.
    score, parts = None, None

    # Clave de barra estable. Se calcula ANTES del dict para que ts y key
    # provengan del mismo instante leido.
    _wall = time.time()
    _bk = bar_key(i.get("time"), _wall)

    rec = {
        "ts": _wall,
        "symbol": str(i.get("ticker")).upper(),
        "bar_index": i.get("bar_index"),
        "bar_time": i.get("time"),
        "bar_key": bar_key_str(_bk),
        "bar_seg": _bk[2],
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
        "volume_ratio": vol.get("volume_ratio"),
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

# Fix 2026-10-13: identidad de barra estable. Se anade por MIGRACION, no en
# CREATE TABLE: reader.db ya existe con su historico y CREATE TABLE IF NOT
# EXISTS no alteraria el esquema de una tabla viva.
_MIGRATIONS = [
    # bar_key: la clave estable que sustituye a bar_index como identidad.
    ("verdicts", "bar_key", "TEXT"),
    # bar_seg: segmento de reloj (0 = sesion normal, >0 tras un reinicio).
    ("verdicts", "bar_seg", "INTEGER"),
]


def _migrate(c):
    """Anade columnas nuevas sin perder historico.

    CREATE TABLE IF NOT EXISTS no toca una tabla que ya existe, asi que anadir
    columnas tiene que ser un ALTER TABLE explicito e idempotente.
    """
    for tabla, col, tipo in _MIGRATIONS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({tabla})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
    c.execute("CREATE INDEX IF NOT EXISTS ix_verdicts_barkey "
              "ON verdicts(bar_key)")


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=20)
    c.executescript(SCHEMA)
    _migrate(c)
    c.commit()
    return c


def do_record(c) -> int:
    """Devuelve un CODIGO DE SALIDA, no un booleano.

    2026-10-15 — BUG QUE MATABA EL SERVICIO. Devolvia 1 al grabar y 0 en SKIP,
    mientras `main` hacía `return 0 if do_record(c) else 1`: un SKIP salia con
    rc=1, o sea exactamente al reves. El supervisor (`ok = rc_r == 0`) contaba
    ese rc como crash, consumia el presupuesto de 5 fallos rapidos y luego los
    20 reintentos, y salia con gave_up=true. Medido en supervisor.log: TODAS
    las PAUSA eran "SKIP: ATAS no disponible: RemoteDisconnected", y
    supervisor_launch.log muestra salidas con code 1 que nadie relanzaba.

    Peor aun: ese mismo rc=1 puerta el resolve (reader_supervisor.py:288) y el
    notify (:306), asi que "ATAS esta caido" se traducia en "no se resuelve
    NADA y no se notifica NADA" — de ahi el `resolver_tail` vivo
    "no se resuelve: record fallo".

    El propio start-reader.ps1 ya documentaba el contrato que el codigo
    incumplia: "si ATAS esta cerrado el grabador simplemente no graba y lo
    dice" (condicion normal, no fallo).
    """
    rec, err = read_live()
    if rec is None:
        print(f"  SKIP: {err}")
        return SKIP
    cols = ("ts,symbol,bar_index,bar_time,bar_key,bar_seg,src_age_s,price_last,"
            "poc,value_area_low,value_area_high,levels,thin_levels,"
            "footprint_sell_heavy,"
            "footprint_buy_heavy,imbalance_pct,imbalance_pct_ema,imbalance_ema_n,"
            "tape_buy_pct,volume_ratio,cvd,cvd_bars,cvd_streak_up,cvd_streak_down,"
            "r6_a,r6_b,r6_c,r6_met,r7_a,r7_b,r7_c,r7_met,bias,score,score_parts,"
            "horizon_bars,resolved")
    # `resolved` se escribe EXPLICITO a 0. La tabla real de reader.db no tiene
    # DEFAULT en esa columna (el CREATE TABLE solo aplica a bases nuevas), asi
    # que omitirla deja NULL y el filtro `resolved=0` del resolver NUNCA la
    # encuentra: la fila se grababa pero no se resolvia jamais, en silencio.
    vals = tuple(rec[k] for k in (
        "ts", "symbol", "bar_index", "bar_time", "bar_key", "bar_seg",
        "src_age_s", "price_last", "poc",
        "value_area_low", "value_area_high", "levels", "thin_levels",
        "footprint_sell_heavy", "footprint_buy_heavy", "imbalance_pct",
        "imbalance_pct_ema", "imbalance_ema_n", "tape_buy_pct", "volume_ratio",
        "cvd", "cvd_bars", "cvd_streak_up", "cvd_streak_down",
        "r6_a", "r6_b", "r6_c", "r6_met", "r7_a", "r7_b", "r7_c", "r7_met",
        "bias", "score", "score_parts", "horizon_bars")) + (0,)
    c.execute(f"INSERT INTO verdicts ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    c.execute("INSERT OR REPLACE INTO recorder_meta VALUES ('last_record_ts', ?)",
              (str(rec["ts"]),))
    c.commit()
    print(f"  recorded {rec['symbol']} bar#{rec['bar_index']} bias={rec['bias']} "
          f"r6={rec['r6_met']} r7={rec['r7_met']}")
    return RECORDED


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
    # Una fila es resoluble si tiene identidad de barra: bar_key (la clave estable,
    # fix 2026-10-13) o, para el historico previo al fix, bar_index. El filtro
    # anterior exigia bar_index IS NOT NULL, lo que ya no describe la realidad:
    # la identidad estable manda, y bar_index es solo el respaldo legacy.
    pending = c.execute(
        "SELECT id,symbol,bar_index,horizon_bars,bias FROM verdicts "
        "WHERE COALESCE(resolved,0)=0 "
        "AND (bar_key IS NOT NULL OR bar_index IS NOT NULL) "
        "ORDER BY id").fetchall()
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
        # Ya es un codigo de salida: 0=grabado, 2=sin dato, 1=error. Antes era
        # `0 if do_record(c) else 1`, que invertia el SKIP. Las excepciones de
        # `do_record` escapan y Python sale con 1 por su cuenta (ese si es el
        # camino de error real).
        return do_record(c)
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