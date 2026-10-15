"""Fase 3 — motor de veredictos. Implementa rules.json v3.3 como salida principal.

POR QUÉ ESTE MÓDULO
===================
El motor viejo generaba señales y no tenía capa de veredicto: por eso sus
lecturas no sostenían una conclusión. Aquí las REGLAS son la salida, y el
scorecard es un RANKING que nunca sustituye a la puerta.

Las tres reglas duras que este módulo implementa (verificadas contra
atas-bridge/rules.json v3.3, no de memoria):

  Regla 6 — Distribution (bajista). Las TRES (umbrales LEIDOS de rules.json):
      (a) CVD cae 2+ barras consecutivas          [cvd_consecutive_down_min]
      (b) footprint: 4+ niveles con >= sell_pct% venta en el lado bid
                                                        [footprint_sell_pct_min]
      (c) consenso de libro multi-venue del scanner: N venues con asks
          pesados fuera de banda, sostenido P lecturas del reader
                                   [dom_source, dom_vac_thr, dom_vac_venues_min,
                                    dom_vac_polls_min - Fix 2-A, 2026-10-11]
  Regla 7 — Accumulation (alcista). Las TRES:
      (a) CVD sube 2+ barras consecutivas          [cvd_consecutive_up_min]
      (b) footprint: 3+ niveles con >= buy_pct% compra en el lado ask
                                                        [footprint_buy_pct_min]
      (c) consenso de libro multi-venue del scanner: N venues con bids
          pesados fuera de banda, sostenido P lecturas del reader
                                   [dom_source, dom_vac_thr, dom_vac_venues_min,
                                    dom_vac_polls_min - Fix 2-A, 2026-10-11]

  ESTE FICHERO NO TIENE NUMEROS FIJOS. Los lee todos de atas-bridge/rules.json
  al importar el modulo, y avisa por stdout si no puede leerlos. Si un numero de
  rules.json no aparece aqui, es porque no se usa.
  Regla 4 — Score, Don't Predict. Scorecard 0-100; >=60 alcista, <=40 bajista.

LA ASIMETRÍA ES INTENCIONAL: regla 6 pide 4 niveles, regla 7 pide 3. Un footprint
con 3 niveles predominantemente vendedores NO satisface ninguna de las dos.

TRES REGLAS DE DISCIPLINA QUE ESTE MÓDULO APLICA
=================================================
1. El sesgo viene de la PUERTA, no del scorecard. Si las reglas no disparan,
   el veredicto es NEUTRAL aunque el score sea 66. Cuando score y puerta
   discrepan, se declara el conflicto y la razón: miden cosas distintas.
2. `probability: unvalidated` SIEMPRE que no exista un hit rate validado por
   backtest. Hoy no existe, así que siempre.
3. Una condición que no se puede evaluar NO es "no satisfied": es
   UNEVALUABLE, y fuerza NEUTRAL. Una regla que nunca puede evaluarse es peor
   que no tenerla, porque parece estar activa y no lo está.

Uso:
  python verdict.py --json          # veredicto de una lectura
  python verdict.py --text          # salida para Telegram
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.request
from pathlib import Path

BRIDGE = "http://127.0.0.1:8000"
LIVE_MAX_AGE_S = 300.0
HERE = Path(__file__).resolve().parent

# ── Umbrales: LEIDOS de atas-bridge/rules.json v3.3 ──
# 2026-10-07: estaban transcritos a mano aqui. Eso es exactamente lo que causo el
# bug que se acaba de arreglar: las reglas 6/7 declaraban
# `dom_persistence_polls_min: 3` y el codigo, al transcribir solo los tres
# numeros que le interesaban, se dejo ese campo fuera — y nadie lo noto porque
# los otros tres si coincidian. Una transcripcion manual es una fuente de verdad
# mas que puede quedarse obsoleta en silencio.
#
# Ahora se leen del fichero. Si rules.json cambia, esto cambia con el, y si el
# fichero no existe o no trae el campo, se avisa en vez de adivinar.
#
# 2026-10-07 — ESTA FUNCION DEVUELVE UN DICT PLANO, con las conditions
# mezcladas con el enforcement bajo una clave "_enforcement". Antes devolvia
# {"conditions": {...}, "enforcement": ...} y cada consumidor tenia que acordarse
# de descender un nivel. La regla 2 se olvido (bug: leia spike_multiple del
# nivel equivocado y caia al default 2.0 en silencio), y cualquier consumidor
# nuevo tiene el mismo error a la vista.
#
# Con el dict plano no existe el error: o hay la clave o no la hay.
# `_enforcement` lleva el prefijo para que no choque con ningun nombre de campo
# de las condiciones.
def _load_rule_conditions(rule_id: int) -> dict:
    try:
        import json as _json
        rules_path = HERE.parent / "atas-bridge" / "rules.json"
        raw = _json.loads(rules_path.read_text(encoding="utf-8"))
        for r in (raw.get("rules") or raw):
            if int(r.get("id", -1)) == rule_id:
                out = dict(r.get("conditions") or {})
                out["_enforcement"] = r.get("enforcement", "hard")
                out["_title"] = r.get("title", "")
                out["_present"] = True          # la regla EXISTE en el JSON
                return out
        print(f"[verdict] AVISO: la regla {rule_id} NO existe en rules.json "
              f"(version {raw.get('version')}). Se usan umbrales por defecto.")
    except Exception as _e:            # noqa: BLE001
        print(f"[verdict] AVISO: no se pudo leer rules.json regla {rule_id}: {_e}")
    # Sin fichero o sin esa regla: se marca _present=False para que el consumidor
    # pueda distinguir "la regla dice que no" de "no hay regla que consultar".
    return {"_enforcement": "hard", "_title": "", "_present": False}


# Las condiciones son PLANAS: _R6["dom_vac_thr"], sin descender niveles.
_R6 = _load_rule_conditions(6)
_R7 = _load_rule_conditions(7)
_R2 = _load_rule_conditions(2)

# FIX 2-A (docs/FIXES-gate-DOM.md, 2026-10-11): la condición (c) ya NO es la EMA
# del libro de Binance (std 7.5, 19-23 cambios de estado/día — no era un gate).
# Ahora es consenso multi-venue del scanner: N venues con touch_imbalance del
# lado de la regla más allá de la banda, sostenido P lecturas del reader.
# La fuente se declara en rules.json ("dom_source") y el test de contrato
# comprueba que este código lee EXACTAMENTE lo que el JSON declara.
DOM_SOURCE = "scanner_multi_venue"
if _R6.get("dom_source") != DOM_SOURCE or _R7.get("dom_source") != DOM_SOURCE:
    print(f"[verdict] AVISO: rules.json declara dom_source="
          f"{_R6.get('dom_source')!r}/{_R7.get('dom_source')!r} y este código "
          f"solo implementa {DOM_SOURCE!r} — contrato roto, no evaluar en serio.")

RULE6 = {"streak": int(_R6.get("cvd_consecutive_down_min", 2)),
         "levels_min": int(_R6.get("footprint_levels_min", 4)),
         "vac_thr": float(_R6.get("dom_vac_thr", 0.15)),
         "vac_venues": int(_R6.get("dom_vac_venues_min", 3)),
         "vac_polls": int(_R6.get("dom_vac_polls_min", 2)),
         "enforcement": _R6.get("_enforcement", "hard")}

RULE7 = {"streak": int(_R7.get("cvd_consecutive_up_min", 2)),
         "levels_min": int(_R7.get("footprint_levels_min", 3)),
         "vac_thr": float(_R7.get("dom_vac_thr", 0.15)),
         "vac_venues": int(_R7.get("dom_vac_venues_min", 3)),
         "vac_polls": int(_R7.get("dom_vac_polls_min", 2)),
         "enforcement": _R7.get("_enforcement", "hard")}

# La regla 7 lee el lado COMPRADOR del consenso y la 6 el VENDEDOR: la
# acumulación pide bids descansando bajo el precio; la distribución, asks encima.
RULE6_SIDE, RULE7_SIDE = "sell", "buy"

# ── Condición (c) multi-venue: consenso de libro N-de-M del scanner ─────────
# La fuente es scanner-engine (poller continuo, 4 venues, UNEVALUABLE declarado
# cuando falta dato — nunca el libro de una GUI). La persistencia vive en un
# fichero de estado porque cada lectura del reader es un ciclo del gate: dos
# lecturas consecutivas por encima de banda son lo que la regla llama "hold".

SCANNER_URL = "http://127.0.0.1:8090"
_DOM_GATE_STATE = HERE / "dom_gate_state.json"


def _symbol_to_scanner_pair(symbol: str) -> str | None:
    """'XRPUSDT' -> 'XRP/USD', solo si el scanner tiene el par registrado."""
    base = str(symbol or "").upper().replace("-", "")
    changed = True
    while changed:
        changed = False
        for suffix in ("SWAP", "USDT", "USD", "PERP"):
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[:-len(suffix)]
                changed = True
                break
    return base + "/USD" if base else None


def _scanner_book_imb5(symbol: str, timeout: float = 4.0) -> dict | None:
    """Imbalance del libro top-5 por venue (escala ±1), desde el scanner.
    None si no hay dato vivo — y None NO es 0: 'no hay dato' y 'libro
    balanceado' son claims distintos y solo uno de ellos es bajista.

    NOTA de calibración (2026-10-11): el touch (mejor nivel) se DESCARTÓ como
    base del consenso — medido, es estructuralmente unilateral (mediana
    |x-0.5|=0.275) y desacoplado entre venues (consenso 3-de-4: 0 en 480
    ciclos). El consenso vive en el libro acumulado, misma construcción que
    los números DOM de ATAS/Binance.
    """
    pair = _symbol_to_scanner_pair(symbol)
    if not pair:
        return None
    try:
        import urllib.parse as _up
        url = f"{SCANNER_URL}/features?pair={_up.quote(pair, safe='')}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        x = (d or {}).get(pair) or {}
        if not x.get("live"):
            return None
        imb = x.get("book_imb_5") or {}
        return {k: float(v) for k, v in imb.items() if isinstance(v, (int, float))} or None
    except Exception:
        return None


def _dom_gate_load() -> dict:
    try:
        return json.loads(_DOM_GATE_STATE.read_text(encoding="utf-8"))
    except Exception:                               # noqa: BLE001
        return {}


def _dom_gate_save(s: dict) -> None:
    try:
        tmp = _DOM_GATE_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
        tmp.replace(_DOM_GATE_STATE)
    except Exception:                               # noqa: BLE001
        pass                                        # el gate degrada, no truena


# 2026-10-15 — BUG DE PERSISTENCIA (mismo patrón que ya costó un día aquí).
#
# rules.json declara `dom_vac_polls_min: 2` = "lecturas del reader (~30s)".
# Pero UN ciclo del supervisor invoca el gate DOS veces sobre el MISMO snapshot
# del scanner: `verdict_recorder.record` (reader_supervisor.py:261) y, si llegó
# a registrar, el notificador vía `build_verdict` (reader_supervisor.py:306 →
# verdict.py:878). Las dos lecturas ocurren separadas por milisegundos y el
# scanner solo refresca cada ~12s, asi que la "persistencia" exigida por AT-6 y
# AT-7 se cumplia con UNA sola observacion real: el gate direccional disparaba
# con media evidencia.
#
# Solución: una racha solo avanza cuando la observación es NUEVA. Cualquier
# segunda llamada dentro de _DOM_GATE_POLL_S LEE la racha sin moverla, así que
# el doble tick deja de ser posible venga de donde venga.
_DOM_GATE_POLL_S = 15.0    # una observación nueva del reader (ciclo ~30s)
# Y no sobrevive a una parada: si nadie ha leído el gate durante más de esto,
# las lecturas anteriores dejan de valer. Una caída de ATAS o una pausa del
# supervisor no puede dar por buena una racha empezada horas atrás — era el
# contrato prometido por el propio docstring ("la racha muere") y solo lo
# cumplía si moría el scanner.
_DOM_GATE_STALE_S = 120.0


def _dom_gate_tick(symbol: str, buy_met: bool, sell_met: bool,
                   observe: bool = True) -> tuple[int, int]:
    """Rachas consecutivas de consenso por lado. Un fallo en cualquier fuente
    (scanner caído, venues insuficientes) pasa buy_met/sell_met=False y la
    racha muere — conservador por diseño.

    Solo avanza una vez por observación real: ver _DOM_GATE_POLL_S. Con
    `observe=False` solo se LEE (ver dom_vac_eval).
    """
    st = _dom_gate_load()
    rec = st.get(symbol) or {"buy": 0, "sell": 0}
    if not observe:
        return int(rec.get("buy", 0)), int(rec.get("sell", 0))
    now = time.time()
    ts = float(rec.get("ts") or 0.0)
    age = now - ts
    if age < _DOM_GATE_POLL_S:
        # Misma observación (el otro hijo del mismo ciclo): se lee, no se avanza.
        return int(rec.get("buy", 0)), int(rec.get("sell", 0))
    if age > _DOM_GATE_STALE_S:
        rec["buy"], rec["sell"] = 0, 0      # la racha muere con la parada
    rec["ts"] = now
    rec["buy"] = int(rec.get("buy", 0)) + 1 if buy_met else 0
    rec["sell"] = int(rec.get("sell", 0)) + 1 if sell_met else 0
    st[symbol] = rec
    _dom_gate_save(st)
    return rec["buy"], rec["sell"]


def dom_vac_eval(symbol: str, observe: bool = True) -> dict:
    """La condición (c) completa para las reglas AT-6/AT-7, compartida por la
    puerta (build_verdict) y por el grabador (verdict_recorder): UNA sola
    implementación, o las dos desincronizan como ya pasó con los umbrales.

    Devuelve {"buy": {"state", "detail"}, "sell": {...}, "venues": n} donde
    state es MET/FAIL/UNEVALUABLE. MET exige: >= vac_venues venues del lado,
    durante vac_polls lecturas consecutivas del reader.

    `observe=False` LEE la racha sin avanzarla. Solo el poll del grabador
    (`verdict_recorder.record`, el ciclo real del reader) es una observación:
    sin esto, el notificador del MISMO ciclo y `live_reader --text` (invocado a
    mano) también contarían, y dos lecturas de un CLI hecho a pulso podrian
    llevar la racha a MET sin que el grabador nunca la observara.
    """
    # 2026-10-15 — cada lado con LOS UMBRALES QUE DECLARA. Antes se leían los de
    # la regla 7 para los DOS lados mientras la etiqueta anunciaba los de la 6
    # (`c_vac_sell_{RULE6['vac_venues']}` / "... {RULE6['vac_thr']:g}"):
    # coincidían solo porque rules.json hoy pone 0.25/3/2 en las dos, así que
    # ni el test de contrato lo veía. Si alguien tunesa solo la 6, el motor
    # seguiría aplicando los de la 7 y publicando los de la 6 — la clase
    # exacta de "declara una cosa y evalúa otra" que ya costó un día.
    tb = _scanner_book_imb5(symbol)
    sides = (("buy", RULE7), ("sell", RULE6))   # 7 = acumulación (lado bid)
    if tb is None:                              # 6 = distribución (lado ask)
        _dom_gate_tick(symbol, False, False, observe=observe)
        return {"buy": {"state": "UNEVALUABLE",
                        "detail": "scanner multi-venue no disponible"},
                "sell": {"state": "UNEVALUABLE",
                         "detail": "scanner multi-venue no disponible"},
                "venues": 0}
    need = max(RULE6["vac_venues"], RULE7["vac_venues"])
    if len(tb) < need:
        _dom_gate_tick(symbol, False, False, observe=observe)
        det = (f"solo {len(tb)} venue(s) vivo(s); el consenso pide {need}")
        return {"buy": {"state": "UNEVALUABLE", "detail": det},
                "sell": {"state": "UNEVALUABLE", "detail": det},
                "venues": len(tb)}
    counts = {s: sum(1 for v in tb.values()
                     if (v > R["vac_thr"] if s == "buy" else v < -R["vac_thr"]))
              for s, R in sides}
    b_n, s_n = _dom_gate_tick(symbol, counts["buy"] >= RULE7["vac_venues"],
                               counts["sell"] >= RULE6["vac_venues"],
                               observe=observe)
    streaks = {"buy": b_n, "sell": s_n}
    out = {"buy": {}, "sell": {}, "venues": len(tb),
           "detail": (f"{counts['buy']}C/{counts['sell']}V venues por lado "
                      f"({len(tb)} vivos); racha C={b_n} V={s_n}")}
    for side, R in sides:
        count, streak, thr = counts[side], streaks[side], R["vac_thr"]
        if count < R["vac_venues"]:
            out[side] = {"state": "FAIL",
                         "detail": f"{count} venues del lado {side} con |imb5|>{thr:g} (piden {R['vac_venues']})"}
        elif streak < R["vac_polls"]:
            out[side] = {"state": "UNEVALUABLE",
                         "detail": f"consenso {side} en racha {streak} de {R['vac_polls']} lecturas"}
        else:
            out[side] = {"state": "MET",
                         "detail": f"{count} venues del lado {side}, racha {streak}>= {R['vac_polls']}"}
    return out

# ── COBERTURA DE REGLAS ────────────────────────────────────────────────────
# Que reglas declara rules.json y cuales implementa REALMENTE este modulo.
#
# El JSON declara 8 (v3.4.1). Este modulo implementa 2, 6 y 7 como puertas
# direccionales, 3 y 4 como limites de salida, y 8 como registro de perdidas (que se
# dispara desde verdict_recorder al resolver un veredicto, no aqui). La 5 es una
# regla de CONDUCTA — "no anclar en patrones pasados" — que un programa no puede
# comprobar; lo que si hace el sistema es no persistir el analisis anterior como
# entrada, y eso ya ocurre porque cada veredicto se evalua solo con la lectura
# actual.
#
# La 1 no tiene codigo propio porque es la condicion (a) de las reglas 6 y 7.
_IMPLEMENTED = {
    1: "condicion (a) de las reglas 6 y 7: exige 2+ barras consecutivas",
    2: "rule2_exhaustion() sobre los deltas por barra",
    3: "solo puede emitir 'unvalidated' (lineas probability)",
    4: "scorecard() + SCORE_BANDS",
    6: "3 condiciones AND, umbrales de rules.json",
    7: "3 condiciones AND, umbrales de rules.json",
    8: "_audit_record_loss(), llamada desde verdict_recorder al resolver",
}
_NOT_IMPLEMENTED = {
    5: "regla de conducta: no comprobable por codigo",
}


def _rule_coverage() -> list:
    """Lee rules.json y declara, por regla, si este modulo la implementa."""
    out = []
    try:
        import json as _json
        raw = _json.loads((HERE.parent / "atas-bridge" / "rules.json")
                          .read_text(encoding="utf-8"))
        rules = raw.get("rules") or []
    except Exception as _e:            # noqa: BLE001
        return [{"id": None, "error": f"no se pudo leer rules.json: {_e}"}]
    for r in rules:
        rid = int(r.get("id", -1))
        if rid in _IMPLEMENTED:
            impl, why = True, _IMPLEMENTED[rid]
        elif rid in _NOT_IMPLEMENTED:
            impl, why = False, _NOT_IMPLEMENTED[rid]
        else:
            impl, why = False, "sin implementacion en este modulo"
        out.append({"id": rid, "title": r.get("title", ""),
                    "enforcement": r.get("enforcement", "hard"),
                    "implemented": impl, "how": why})
    return out


# Regla 2 (CVD extremes = exhaustion). Sus dos campos estaban escritos a mano
# dentro de rule2_exhaustion(); despues se leyeron del JSON, pero del nivel
# equivocado del dict anidado y caian al default en silencio. Ver 9d0e90f.
# _R2 se cargo arriba, junto a _R6/_R7, ahora que el dict es plano.
RULE2 = {"spike_multiple": float(_R2.get("spike_multiple", 2.0)),
         "min_prior_bars": int(_R2.get("min_prior_bars", 3))}
if not _R2.get("_present"):
    print("[verdict] AVISO: la regla 2 no esta en rules.json; "
          "spike_multiple usara el default 2.0.")
# 2026-10-07 — Umbrales del footprint, LEIDOS de rules.json. Los dos son
# `footprint_sell_pct_min` / `footprint_buy_pct_min`; las reglas 6 y 7 los
# declaran iguales, asi que se toma el de la 6 y se avisa si difieren.
_FP_PCT = None
for _rid, _r in ((6, _R6), (7, _R7)):
    _v = _r.get("footprint_sell_pct_min", _r.get("footprint_buy_pct_min"))
    if _v is None:
        continue
    if _FP_PCT is None:
        _FP_PCT = float(_v)
    elif float(_v) != _FP_PCT:
        print(f"[verdict] AVISO: regla {_rid} declara {_v}% pero la otra declara "
              f"{_FP_PCT}%. Se usa {_FP_PCT}%. Las reglas deben coincidir.")
FOOTPRINT_PCT = 70.0 if _FP_PCT is None else _FP_PCT
if _FP_PCT is None:
    print("[verdict] AVISO: rules.json no declara footprint_sell_pct_min; "
          "se usa el default 70/30, que puede NO cumplir rules.json.")

if not _R6 or not _R7:
    print("[verdict] AVISO: rules.json no leido; se usan umbrales por defecto. "
          "Los veredictos pueden NO cumplir rules.json.")
SCORE_BANDS = {"bullish_min": 60, "bearish_max": 40}
WEIGHTS = {"footprint_depth": 25, "dom_imbalance": 25, "tape_ratio": 20,
           "volume_climax": 15, "footprint_confirmation": 15}


def _rule_enforcement(rule_id: int) -> str:
    """'hard' | 'soft' | 'disabled', leido de rules.json. Default 'hard'.

    Una regla 'disabled' sigue evaluandose y se publica su detalle, pero no
    cuenta para el sesgo. Sirve para poner en cuarentena una regla con evidencia
    en contra sin perder la trazabilidad de por que se cumple.
    """
    return _load_rule_conditions(rule_id).get("_enforcement", "hard")


# El v2 del scorecard se reprodujo inyectando la calibracion aqui. Antes se
# cargaba desde disco al importar el modulo, lo que hacia que este motor
# leyera un .json en cada arranque aunque no lo fuera a usar nunca: una
# dependencia de runtime sobre una medicion retirada.
_CAL_OVERRIDE = None


# ── SCORECARD: RETIRADO DE PRODUCCION (2026-10-13, commit a93147f) ──────────
# MEDICION sobre 1.205 filas de reader.db con forward_return resuelto:
#
#   pearson(score, forward_return)  = +0.0175    (ruido)
#   spearman(score, forward_return) = -0.0156
#   tercil alto ("mejor" score): winrate 32,8%, retorno medio -0.392%
#   tercil bajo               : winrate 37,9%, retorno medio -0.382%
#
# El "mejor" score ganaba MENOS y perdia MAS que el peor. El componente de
# peso 50 (dom_ema) media spread +27,3pp sobre top150/bottom150, pero con
# autocorrelacion de forward_return rho=+0.995 eso equivale a ~1 observacion
# independiente (z=0.35): la calibracion no tenia respaldo estadistico.
#
# QUEDE ESTE CODIGO, POR QUE
# =========================
# _CAL, _percentile_rank(), _scorecard_percentile(), _scorecard_v341() y
# scorecard() NO los llama ninguna ruta de produccion: build_verdict() dejo de
# publicar v["scorecard"] y v["score_gate_conflict"]. Se conservan para poder
# REPRODUCIR la medicion que lo retiro -- incluido el v2 percentile-median que
# se construyo el 2026-10-11 sobre los mismos 1.205 registros, y que sufrio la
# misma falta de respaldo al ser medido sobre la misma muestra autocorrelada.
#
# Mover scorecard_calibration.json a reader/build_scorecard_calibration.py y
# ejecutar ese script reconstruye la calibracion completa. Despues:
#   verdict._CAL_OVERRIDE = json.load(open("calib_features.json"))
#   verdict.scorecard(None, None, ema, None, None, sell_h, buy_h)
#
# SI ALGUIEN REACTIVA EL SCORECARD: no reutilices el spread +27,3pp de esta
# cabecera como evidencia. Con rho=+0.995 no es un spread de 1.205
# observaciones, es aproximadamente una. Ver LINT en verdict.py.


def _get(path: str, timeout: float = 25.0):
    try:
        with urllib.request.urlopen(f"{BRIDGE}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:70]}"}


# ── Regla 2: los extremos de CVD son AGOTAMIENTO, no continuación ──
def rule2_exhaustion(cvd: float | None, high: float | None, low: float | None,
                     prev_high: float | None, prev_low: float | None) -> dict:
    """Un salto de CVD >2x el máximo previo de una barra es agotamiento.

    Flag como agotamiento; NO llamar continuación en ninguna dirección.
    """
    if cvd is None:
        return {"applicable": False, "reason": "sin CVD"}
    out = {"applicable": False}

    # 2026-10-07 — TRES DEFECTOS ARREGLADOS aqui:
    #
    # (1) `spike_multiple` (2.0) y `min_prior_bars` (3) estan en rules.json y
    #     estaban escritos a mano. Mismo fallo que la condicion (c) de 6/7.
    # (2) `abs(high) > 2*abs(prev_high) > 0` es una COMPARACION ENCADENADA de
    #     Python, no lo que parece. Se evalua como
    #     (abs(high) > 2*abs(prev_high)) AND (2*abs(prev_high) > 0).
    #     Con prev_high == 0 —el estado inicial, y el valor por defecto en el
    #     puente— la segunda mitad es 0 > 0 = False, asi que la regla nunca se
    #     activaba en el primer tramo. No era "siempre aplicable" como sospeche al
    #     principio: era "nunca aplicable", por una razon distinta.
    # (3) `high`/`low` son extremos ACUMULADOS de la sesion, no el maximo de una
    #     barra. La regla habla de "un salto >2x el max previo", es decir, de un
    #     maximo de una barra comparado con el maximo previo. Comparar el acumulado
    #     de la sesion contra el acumulado anterior compara dos ventanas
    #     distintas y no dice nada sobre el salto de UNA barra.
    #
    # Ahora: si no hay referencia previa utilizable, no se inventa — se declara no
    # aplicable con el motivo. Un extremo sin comparacion previa no se puede
    # clasificar como agotamiento.
    if prev_high is None or prev_low is None:
        return {"applicable": False,
                "reason": "sin referencia previa (prev_high/prev_low no disponibles)"}
    if not (abs(prev_high) > 0 or abs(prev_low) > 0):
        return {"applicable": False,
                "reason": "referencia previa en cero: aun no hay historial comparable"}

    mult = RULE2.get("spike_multiple", 2.0)
    if abs(high or 0) > mult * abs(prev_high) > 0:
        out = {"applicable": True, "side": "up",
               "note": (f"CVD en {abs(high or 0):.0f} supera {mult}x el maximo "
                        f"previo {abs(prev_high):.0f}: AGOTAMIENTO, no continuacion")}
    elif abs(low or 0) > mult * abs(prev_low) > 0:
        out = {"applicable": True, "side": "down",
               "note": (f"CVD en {abs(low or 0):.0f} supera {mult}x el minimo "
                        f"previo {abs(prev_low):.0f}: AGOTAMIENTO, no continuacion")}
    return out


# ── REGLA 1: INTEGRIDAD DE LA SERIE QUE USA LA REGLA ─────────────────────
# 2026-10-07. Las reglas 6 y 7 miran una RACHA de 2 barras de bar_deltas, y la
# regla 2 mira las ultimas 4. Nada de eso exige que los 200 ultimos minutos de CVD
# esten completos: exige que las barras CONTIGUAS que se comparan esten ahi.
#
# Seacia lo contrario. Una comprobacion `missing_bars == 0` sobre la serie entera
# seria correcta en teoria y toxica en la practica: hoy missing_bars=148 de 204
# barras por un corte de hace horas, y el veredicto no podria emitir sesgo
# durante semanas por huecos que no afectan a la racha que se esta evaluando. Un
# gate que bloquea todo el tiempo es un gate que nadie lee.
#
# Lo que se comprueba es lo que la regla usa:
#   1. que el endpoint trae suficientes barras (len >= lo que pide la regla)
#   2. que la ultima barra no es un hueco disfrazado de barra
#   3. que hay barras limpias recientes desde el ultimo hueco, suficientes para
#      cubrir la ventana que la regla examina
#
# Si algo falla, la condicion (a) de las reglas 6 y 7 pasa a UNEVALUABLE, que es
# lo que ya hace cuando no hay barras: neutral, no direccion.
CVD_INTEGRITY_MIN_BARS = 2        # lo que necesitan las reglas 6 y 7
CVD_INTEGRITY_WINDOW = 4          # ventana que se quiere limpia (regla 2 usa 4)


def cvd_integrity(cvd_state: dict, need_bars: int = CVD_INTEGRITY_MIN_BARS,
                  window: int = CVD_INTEGRITY_WINDOW) -> dict:
    """Comprueba la integridad de la FRAGMENTO de CVD que la regla va a mirar.

    No juzga la serie historica completa: los huecos de hace horas no invalidan
    una racha de las ultimas 3 barras. Si los juzgara, un hueco antiguo
    bloquearia el sistema de forma permanente.
    """
    out = {"ok": True, "reason": ""}
    bd = cvd_state.get("bar_deltas") or []
    if len(bd) < need_bars:
        out.update(ok=False,
                   reason=f"solo {len(bd)} barra(s) cerrada(s); la regla necesita {need_bars}")
        return out
    if cvd_state.get("continuity") not in (None, "clean"):
        out.update(ok=False, reason=f"continuity={cvd_state.get('continuity')}")
        return out
    # Cuantas barras limpias hay DESDE el ultimo hueco. El puente lo publica como
    # bars_since_gap.
    #
    # 2026-10-14 — BUG ARREGLADO: `None` significaba DOS cosas a la vez.
    # bridge_server.py inicializa bars_since_gap=None y lo documenta como "nunca
    # hubo hueco" (L772), y su PROPIO payload lo traduce a continuity="clean"
    # (L1074-1078). Este lector interpretaba ese mismo None como "el campo no
    # viene" y devolvia ok=False. Consecuencia medida: la serie MAS sana posible
    # (gaps=0, missing_bars=0, continuity clean) se declaraba no verificable,
    # (a) quedaba UNEVALUABLE y las reglas 6/7 — las unicas direccionales, la 4
    # esta deshabilitada — no podian dispararse tras un reinicio del puente.
    # Peor aun: el incremento del puente (L996) esta guardado con `is not None`,
    # asi que None se quedaba None hasta que ocurriera un hueco por accidente.
    # Un centinela con dos significados, y el lector elegia el malo.
    #
    # Distincion ahora explicita: el campo realmente ausente va SIN continuity
    # (quien no calcula bars_since_gap tampoco calcula esa continuidad). Si
    # continuity viene, None es "nunca hueco" y TODAS las barras que hay son
    # contiguas por definicion.
    since = cvd_state.get("bars_since_gap")
    if since is None:
        if cvd_state.get("continuity") is None:
            out.update(ok=False,
                       reason="el puente no publica bars_since_gap ni continuity; "
                              "no se puede confirmar que las barras usadas sean "
                              "contiguas")
            return out
        since = len(bd)          # nunca hueco: todas las barras son contiguas
    # La ventana son las ultimas `window` barras. Si el hueco esta mas lejos, las
    # barras que mira la regla son limpias; si esta dentro, no.
    if since < min(window, len(bd)):
        out.update(ok=False,
                   reason=(f"hueco hace {since} barra(s); la regla mira las ultimas "
                           f"{min(window, len(bd))} y hay una discontinuity"))
        return out
    out["bars_since_gap"] = since
    out["window"] = min(window, len(bd))
    return out


def streak_from_bar_deltas(bar_deltas):
    """(streak_up, streak_down) sobre las barras CERRADAS, de la más nueva atrás.

    Termina en el primer cambio de signo: dos barras hijas alternas NO son
    'streak', y esa es exactamente la condición (a) de las reglas 6 y 7.
    """
    up = dn = 0
    for d in reversed(bar_deltas or []):
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
    return up, dn


def classify_footprint(top_levels, sell_pct=None):
    """Clasifica niveles del footprint como venta/compra dominante.

    Regla 6 cuenta niveles con `footprint_sell_pct_min`% o mas de volumen en el
    lado bid; regla 7 cuenta los que lo tienen en el lado ask.

    2026-10-07 — BUG ARREGLADO. Los umbrales estaban FIJOS en la funcion
    (`bp > 70.0` / `bp < 30.0`) mientras rules.json declara otros. Con v3.4.1
    los umbrales son 65, asi que:

      - el codigo rechazaba niveles entre 65% y 70% que la regla 6 cuenta
        (con 4 niveles pedidos, el codigo podia devolver 3 y la regla no se
        cumplia)
      - el complemento estaba mal por construccion: con 65 el ask-dominante es
        bid<35, no bid<30. Entre 30% y 35% hay una franja que la regla 7 cuenta
        y el codigo ignoraba — justo la de ask-dominante, que es la que importa
        para la regla 7.

    Ahora se leen de rules.json. Los defaults (70/30) solo aplican si el JSON no
    se puede leer, y avisa de eso al cargar el modulo.

    2026-10-15 — TRAMPA ELIMINADA. La firma traia un `buy_pct` que se guardaba
    en `_bp` y NUNCA se usaba: el lado compra se evaluaba con `100.0 - _sp`.
    El comportamiento era el correcto (ask-dominante = complemento exacto del
    umbral de venta: con 65, bid<=35 es ask>=65), pero el parametro invitaba a
    creer lo contrario. Llamadas reales pasaban `buy_pct=FOOTPRINT_PCT` (=65),
    y quien "arreglara" el parametro muerto para que se usara pasaria a aceptar
    `bid <= 65%` como compra dominante — casi todo el libro — y romperia la
    regla 7 de golpe. El complemento es una identidad, no una configuracion.
    """
    # Defaults solo si rules.json no se pudo leer. load_footprint_pct() avisa
    # de esa situacion al importar el modulo.
    _sp = 70.0 if sell_pct is None else float(sell_pct)
    _low = 100.0 - _sp            # lado ask dominante = complemento exacto
    rows, sell_h, buy_h, thin = [], 0, 0, 0
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
        if bp >= _sp:
            sell_h += 1
            tag = f"SELL>{_sp:g}"
        elif bp <= _low:
            buy_h += 1
            tag = f"BUY>{_sp:g}"
        else:
            tag = "mixed"
        rows.append({"price": lv.get("price"), "bid_pct": round(bp, 1),
                     "bid": round(bid, 1), "ask": round(ask, 1),
                     "delta": lv.get("delta", 0), "tag": tag, "thin": is_thin})
    rows.sort(key=lambda r: (r["price"] is None, r["price"]))
    return rows, sell_h, buy_h, thin


def scorecard(rows, imbalance, ema, tape_buy_pct, volume_ratio, sell_h, buy_h):
    """RETIRADO (2026-10-13). No llamar. Se conserva solo para poder reproducir
    la medicion que la retiro; ninguna ruta de produccion la usa.

    MEDICION que la retiro (sobre 1.205 filas de reader.db con forward_return):

      pearson(score, forward_return)  = +0.0175
      spearman(score, forward_return) = -0.0156
      tercil alto (el "mejor" score): winrate 32.8%, retorno medio -0.392%
      tercil bajo                    : winrate 37.9%, retorno medio -0.382%

    El score no correlaciona con el retorno y su tercil alto perdia MAS que el
    bajo. Ademas el componente de peso 50 (dom_ema = imbalance_pct_ema, el libro
    Binance retirado del gate) media un spread de +27.3pp sobre top150/bottom150
    que con autocorr rho=+0.995 equivale a ~1 observacion independiente
    (z=0.35, no significativo): la calibracion no tenia respaldo.

    El sesgo nunca dependio de esto: sale de `if r7_ok / elif r6_ok / else`.

    NINGUNA RUTA DE PRODUCCION LLAMA A ESTA FUNCION. Se conserva unicamente
    para reproducir la medicion que retiro el scorecard.

    El v2 percentile-median (2026-10-11) tambien esta aqui, pero comparte el
    mismo problema que se corrigio arriba: se calibro sobre los MISMOS 1.205
    registros autocorrelacionados. Que "se centrara solo" y "?spread +36pp?"
    no lo convierte en valido.
    """
    # Sin _CAL (retirado del import en el mismo commit que esta nota) la
    # reproduccion del v2 requiere cargarlo a mano:
    #   import json; verdict._CAL = json.load(open(CAL_PATH))
    cal = _CAL_OVERRIDE
    if cal:
        return _scorecard_percentile(cal, ema, sell_h, buy_h)
    return _scorecard_v341(rows, imbalance, ema, tape_buy_pct, volume_ratio,
                           sell_h, buy_h)


def _percentile_rank(values, x):
    """Rank de x dentro de una lista de percentiles calibrada. Devuelve 0-100."""
    if x is None or not values:
        return 50.0
    import bisect as _bs
    return _bs.bisect_left(values, float(x)) / len(values) * 100.0


def _scorecard_percentile(cal, ema, sell_h, buy_h):
    dists = cal.get("distributions", {})
    weights = cal.get("weights", {})
    raw = {
        "dom_ema": ema,
        "footprint_sell_heavy": sell_h,
        "footprint_buy_heavy_inv": buy_h,
    }
    parts, notes = {}, {}
    total = 0.0
    wsum = 0.0
    for comp, w in weights.items():
        v = raw.get(comp)
        pr = _percentile_rank(dists.get(comp), v)
        if comp.endswith("_inv"):
            pr = 100.0 - pr
        parts[comp] = round(pr * w / 100.0, 1)
        notes[comp] = f"{v} -> percentil {pr:.0f}" + (" (invertido)" if comp.endswith("_inv") else "")
        total += pr * w
        wsum += w
    score = round(total / wsum, 1) if wsum else 50.0
    ev = cal.get("evidence", {})
    notes["_useful_spread"] = {k: ev[k]["useful_spread"] for k in parts if k in ev}
    notes["_method"] = "percentile-median (v2) — RETIRADO, ver nota de cabecera"
    notes["_calib_sample"] = cal.get("sample_size")
    notes["_WARNING"] = ("calibrado sobre la misma muestra autocorrelada que la "
                         "medicion que retiro el scorecard (rho=+0.995)")
    return score, parts, notes


def _scorecard_v341(rows, imbalance, ema, tape_buy_pct, volume_ratio, sell_h, buy_h):
    """v3.4.1 original. MEDIDO COMO ANTI-PREDICTIVO (spread -4,0 medido)."""
    parts, notes = {}, {}

    parts["footprint_depth"] = min(WEIGHTS["footprint_depth"], 3 * len(rows))
    notes["footprint_depth"] = f"{len(rows)} niveles"

    if isinstance(imbalance, (int, float)):
        v = max(0, min(25, int(round(12.5 + (imbalance / 100.0) * 12.5))))
    else:
        v = 0
    parts["dom_imbalance"] = v
    notes["dom_imbalance"] = (f"instantáneo {imbalance:+.2f}%" if isinstance(imbalance, (int, float))
                               else "instantáneo AUSENTE")

    if tape_buy_pct is None:
        t = 0
        notes["tape_ratio"] = "tape AUSENTE"
    else:
        t = max(0, min(20, int(round(tape_buy_pct / 100.0 * 20))))
        notes["tape_ratio"] = f"compra {tape_buy_pct:.1f}%"
    parts["tape_ratio"] = t

    if isinstance(volume_ratio, (int, float)) and volume_ratio > 0:
        vc = max(0, min(15, int(round(min(volume_ratio, 3.0) / 3.0 * 15))))
        notes["volume_climax"] = f"ratio {volume_ratio:.2f}x"
    else:
        vc = 0
        notes["volume_climax"] = "sin baseline de volumen"
    parts["volume_climax"] = vc

    parts["footprint_confirmation"] = min(15, (5 if buy_h >= 3 else 0) + (5 if sell_h >= 3 else 0))
    notes["footprint_confirmation"] = f"clusters {buy_h}/{sell_h}"

    return sum(parts.values()), parts, notes


def build_verdict() -> dict:
    """Un veredicto. Nunca lanza: una fuente caída produce un veredicto que la nombra."""
    now = time.time()
    v = {"ts": now, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))}

    ind = _get("/indicators")
    if ind.get("_error") or ind.get("ticker") is None:
        v["bias"] = "UNAVAILABLE"
        v["reason"] = f"ATAS no disponible: {ind.get('_error', 'payload nulo')}"
        v["probability"] = "unvalidated"
        # La cobertura se declara tambien en los retornos tempranos: si falta,
        # un fallo de ATAS oculta que reglas existen y cuales estan activas.
        v["rule_coverage"] = _rule_coverage()
        v["rules_not_implemented"] = [r["id"] for r in v["rule_coverage"]
                                      if not r["implemented"]]
        return v

    ts = ind.get("timestamp")
    age = (now - ts / 1000.0) if ts else None
    v["symbol"] = str(ind.get("ticker")).upper()
    v["src_age_s"] = round(age, 1) if age is not None else None
    v["bar_index"] = ind.get("bar_index")
    v["bar_time"] = ind.get("time")

    # Regla live-only: un dato viejo NO es dato. Se descarta y se dice.
    if age is None or age > LIVE_MAX_AGE_S:
        v["bias"] = "UNAVAILABLE"
        v["reason"] = (f"dato NO vivo (edad {v['src_age_s']}s > {LIVE_MAX_AGE_S:.0f}s); "
                       f"no se emite veredicto sobre datos congelados")
        v["probability"] = "unvalidated"
        v["rule_coverage"] = _rule_coverage()
        v["rules_not_implemented"] = [r["id"] for r in v["rule_coverage"]
                                      if not r["implemented"]]
        return v

    price = ind.get("price") or {}
    dom = ind.get("dom") or {}
    fp = ind.get("footprint") or {}
    vol = ind.get("volume") or {}
    tape = ind.get("tape") or {}
    v["price_last"] = price.get("last")
    v["poc"] = fp.get("poc")
    v["value_area"] = [fp.get("value_area_low"), fp.get("value_area_high")]

    rows, sell_h, buy_h, thin = classify_footprint(
        fp.get("top_levels"), FOOTPRINT_PCT)
    imbalance = dom.get("imbalance_pct")
    ema = dom.get("imbalance_pct_ema")
    v["footprint"] = {"levels": len(rows), "sell_heavy": sell_h,
                     "buy_heavy": buy_h, "thin": thin, "table": rows}

    bq, sq = tape.get("buy_vol"), tape.get("sell_vol")
    if bq is None or sq is None:
        tr = tape.get("trades") or []
        bq = sum(t.get("q", 0) or 0 for t in tr if t.get("s") == "buy")
        sq = sum(t.get("q", 0) or 0 for t in tr if t.get("s") == "sell")
    tot = (bq or 0) + (sq or 0)
    tape_buy_pct = round(100.0 * bq / tot, 1) if tot else None
    v["tape_buy_pct"] = tape_buy_pct
    v["volume_ratio"] = vol.get("volume_ratio")
    v["dom"] = {"imbalance_pct": imbalance, "imbalance_pct_ema": ema,
                "ema_n": dom.get("imbalance_pct_ema_n"),
                "bid_ask_ratio": dom.get("bid_ask_ratio")}

    # ── Condición (a): rachas de CVD ──
    cv = _get("/cvd")
    cvd_state = (cv.get("cvd") or cv).get(v["symbol"]) or {}
    bd = cvd_state.get("bar_deltas") or []
    up, dn = streak_from_bar_deltas(bd)
    v["cvd"] = {"value": cvd_state.get("cvd"), "bars": cvd_state.get("bars"),
                "streak_up": up, "streak_down": dn,
                "continuity": cvd_state.get("continuity"),
                "gaps": cvd_state.get("gaps"), "missing_bars": cvd_state.get("missing_bars"),
                # Explicito, porque missing_bars por si solo no dice si AFECTA a
                # la ventana que mira la regla. Un total de 148 huecos con 90
                # barras limpias despues del ultimo hueco es una serie sana para
                # una racha de 3; el mismo total con un hueco hace una barra es
                # una serie rota. Sin este campo no se puede distinguir.
                "bars_since_gap": cvd_state.get("bars_since_gap")}

    # ── Regla 2 ──
    # 2026-10-07: se evaluaba contra `high_cvd`/`low_cvd`, que son los extremos
    # ACUMULADOS de la sesion. La regla habla de "un salto >2x el maximo previo
    # en una barra", asi que lo que corresponde son los deltas POR BARRA
    # (`bar_deltas`), no la serie acumulada. Con los acumulados se comparaba la
    # ventana completa contra la ventana anterior: no dice nada sobre el salto de
    # una barra, y puede marcarse por un extremo de hace 100 barras.
    #
    # min_prior_bars = 3 barras de historico antes de poder afirmar un extremo.
    _need = RULE2["min_prior_bars"]
    _int_r2 = cvd_integrity(cvd_state, _need, _need + 1)
    if not _int_r2["ok"]:
        v["rule2"] = {"applicable": False,
                      "reason": "serie CVD no integra: " + _int_r2["reason"]}
    elif len(bd) >= _need:
        _prior = bd[:-1]
        _cur = bd[-1]
        _prior_hi = max(_prior)
        _prior_lo = min(_prior)
        v["rule2"] = rule2_exhaustion(
            _cur,
            max(_prior_hi, _cur) if _cur > 0 else 0.0,
            min(_prior_lo, _cur) if _cur < 0 else 0.0,
            _prior_hi, _prior_lo)
    else:
        v["rule2"] = {"applicable": False,
                      "reason": (f"solo {len(bd)} barra(s) cerrada(s); la regla 2 "
                                 f"exige {RULE2['min_prior_bars']} de historico")}
    # ── Regla 8: CVD roto por un hueco no es una serie limpia ──
    v["cvd_clean"] = (cvd_state.get("continuity") == "clean"
                      and not (cvd_state.get("missing_bars") or 0))

    # REGLA 1 aplicada a la condicion (a): no basta con que haya 2 barras, tienen
    # que ser CONTIGUAS. Antes solo se comprobaba len(bd) >= 2, que cuenta barras
    # pero no dice si estan seguidas: un hueco en medio produce una racha de dos
    # barras que nunca fueron consecutivas.
    _int_a = cvd_integrity(cvd_state, RULE6["streak"], CVD_INTEGRITY_WINDOW)
    _int_b = cvd_integrity(cvd_state, RULE7["streak"], CVD_INTEGRITY_WINDOW)
    v["cvd"]["integrity"] = {"rule6_window": _int_a, "rule7_window": _int_b}
    v["cvd"]["integrity_ok"] = _int_a["ok"] and _int_b["ok"]

    if not _int_a["ok"]:
        a6 = "UNEVALUABLE"
    else:
        a6 = "UNEVALUABLE" if len(bd) < 2 else ("MET" if dn >= RULE6["streak"] else "FAIL")
    if not _int_b["ok"]:
        a7 = "UNEVALUABLE"
    else:
        a7 = "UNEVALUABLE" if len(bd) < 2 else ("MET" if up >= RULE7["streak"] else "FAIL")
    b6 = "MET" if sell_h >= RULE6["levels_min"] else "FAIL"
    b7 = "MET" if buy_h >= RULE7["levels_min"] else "FAIL"
    # 2026-10-07 — BUG ARREGLADO: la condicion (c) se evaluaba SOLO con el valor
    # del EMA. rules.json v3.3 exige ademas `dom_persistence_polls_min: 3` en las
    # reglas 6 Y 7: el imbalance debe hold 3+ lecturas antes de contar. El puente
    # ya publicaba ese contador (`imbalance_pct_ema_n`) y el veredicto lo leia para
    # IMPRIMIRLO, pero no lo usaba para decidir MET/FAIL — es decir, se evaluaba
    # un gate con menos datos de los que la regla pide.
    #
    # Consecuencia real medida: con n=1 la EMA todavia no se ha asentado y su
    # valor es practicamente el instantaneo. Una regla "hard" evaluada sobre una
    # muestra unica no es una regla hard.
    #
    # Ahora, si no hay 3+ muestras, la condicion es UNEVALUABLE — igual que ya
    # se hacia cuando el EMA era None. Y como las reglas son AND estricto,
    # UNEVALUABLE hace que la regla entera NO se cumpla: neutral, no direccion.
    # ── Condición (c): consenso de libro multi-venue (Fix 2-A) ──
    # ANTES: imbalance_pct_ema del libro Binance WS — std 7.5, el gate cambiaba
    # de estado 19-23 veces/día. AHORA: N venues del scanner fuera de banda,
    # sostenido P lecturas. La EMA se SIGUE PUBLICANDO como contexto (v["dom"])
    # pero ya no decide nada — un número que se muestra y un número que gatean
    # dejan de ser el mismo por accidente.
    gate = dom_vac_eval(v["symbol"], observe=False)   # la observación es del
    c6 = gate[RULE6_SIDE]["state"]                    # grabador, no de aquí
    c7 = gate[RULE7_SIDE]["state"]
    v["dom_vac"] = {
        "symbol": v["symbol"],
        # Cada lado con LOS SUYOS: antes ambos reportaban los de la regla 7
        # mientras las claves de conditions (mas abajo) citaban los de la 6.
        # Coincidian solo porque rules.json hoy pone lo mismo en las dos.
        "thr": {"sell": RULE6["vac_thr"], "buy": RULE7["vac_thr"]},
        "venues_min": {"sell": RULE6["vac_venues"], "buy": RULE7["vac_venues"]},
        "polls_min": {"sell": RULE6["vac_polls"], "buy": RULE7["vac_polls"]},
        "buy": gate["buy"], "sell": gate["sell"],
        "detail": gate.get("detail", ""),
    }

    r6 = [a6, b6, c6]
    r7 = [a7, b7, c7]
    r6_met = all(x == "MET" for x in r6)
    r7_met = all(x == "MET" for x in r7)
    unev = [x for x in (r6 + r7) if x == "UNEVALUABLE"]

    v["rule6"] = {
        "conditions": {"a_cvd_down_2plus": a6,
                       f"b_footprint_{RULE6['levels_min']}_sell_heavy": b6,
                       f"c_vac_sell_{RULE6['vac_venues']}deM_thr{RULE6['vac_thr']:g}": c6},
        "met": r6_met}
    v["rule7"] = {
        "conditions": {"a_cvd_up_2plus": a7,
                       f"b_footprint_{RULE7['levels_min']}_buy_heavy": b7,
                       f"c_vac_buy_{RULE7['vac_venues']}deM_thr{RULE7['vac_thr']:g}": c7},
        "met": r7_met}

    # ── Regla 5: No Pattern Anchoring ──
    # Validation: verify the verdict does not reference prior bar patterns as evidence.
    # The engine uses only current bar footprint, DOM, CVD, and tape — no lagged patterns.
    # HONESTO (2026-10-11 audit): None, no True. Un flag que no puede fallar no
    # es un control — era True por construccion mientras la cobertura de reglas
    # declara la 5 como "no comprobable por codigo". Lo que SI es cierto: las
    # entradas del veredicto son solo de la barra/lectura actual (arquitectura),
    # y eso se dice como tal en vez de simularse como chequeo pasado.
    v["rule5"] = {
        "anchoring_ok": None,       # no computable; ver _rule_coverage()
        "note": "Regla de conducta: no verificable por codigo. Cubierta por "
                "diseno — las entradas del veredicto son solo la lectura actual "
                "(footprint/DOM/CVD/tape de esta barra); ningun estado de "
                "analisis anterior entra como evidencia."
    }

    # ── El sesgo viene de la PUERTA ──
    # Regla 6 en cuarentena desde 2026-10-11: enforcement "disabled" en
    # rules.json por PRECAUCION, NO por anti-edge medido — su evidencia es
    # n_eff=2,7 sobre 159 filas solapadas (autocorr +0,97), que es ruido, y es
    # toda de la era del gate viejo. Ver el `disabled_reason` de rules.json.
    # Se sigue EVALUANDO y se publica el detalle, pero no decide el sesgo: una
    # senal con apariencia de analisis sin base cuesta mas que no dar ninguna.
    # Volver a "hard" exige n_eff >= min_n sobre el gate multi-venue.
    r6_enforcement = _rule_enforcement(6)
    if r7_met:
        bias = "BULLISH"
    elif r6_met and r6_enforcement != "disabled":
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    v["rule6"]["enforcement"] = r6_enforcement
    v["rule6"]["counts_toward_bias"] = (r6_enforcement != "disabled")
    if r6_met and r6_enforcement == "disabled":
        v["rule6"]["note"] = (
            "condiciones satisfechas pero la regla esta DESACTIVADA "
            "(anti-edge medido 2026-10-11); no aporta al sesgo")

    # ── SCORECARD RETIRADO (2026-10-13) ──
    # Antes se calculaba aqui y se publicaba v["scorecard"] + v["score_gate_conflict"].
    # Retirado por medicion sobre 1.205 filas de reader.db con forward_return:
    #
    #   pearson(score, forward_return)  = +0.0175     (ruido)
    #   spearman(score, forward_return) = -0.0156
    #   tercil alto ("mejor" score): winrate 32.8%, retorno medio -0.392%
    #   tercil bajo                : winrate 37.9%, retorno medio -0.382%
    #
    # No correlaciona con el retorno y el tercil alto perdia MAS que el bajo. El
    # componente de peso 50 (dom_ema = imbalance_pct_ema, el libro Binance que se
    # retiro del gate) media spread +27.3pp sobre top150/bottom150, que con
    # autocorr rho=+0.995 son ~1 observacion independiente (z=0.35): la
    # calibracion no tenia respaldo estadistico.
    #
    # Se eliminan tambien `score_gate_conflict` y su detalle: comparaban el
    # veredicto de la puerta contra un numero sin poder predictivo, asi que el
    # "conflicto" que reportaban era ruido, no una senal de desacuerdo real.
    #
    # El sesgo no cambia: sigue saliendo de if r7_ok / elif r6_ok / else.
    v["bias"] = bias
    v["unevaluable"] = unev
    v["probability"] = "unvalidated"
    # ── COBERTURA DE REGLAS, generada desde rules.json ────────────────────────
    # 2026-10-07. Antes havia 11 lineas de la forma
    #     v["ruleN_annotate"] = "..." + v.get("ruleN", {}).get("note", "no configurado")
    # para las reglas 5 y 9-18. Esas reglas NO EXISTEN en rules.json v3.4.1 (8
    # reglas), y v["ruleN"] nunca se creaba, asi que las once imprimian
    # literalmente "no configurado". Un veredicto con diez lineas de "no
    # configurado" parece un motor con diez reglas mas de las que tiene, y es
    # justo lo que hace que alguien asuma que estan activas.
    #
    # Ahora se lee rules.json y se declara, por regla, si este modulo la
    # implementa o no. Un sitio unico, imposible de desincronizar del JSON.
    v["rule_coverage"] = _rule_coverage()
    v["rules_not_implemented"] = [r["id"] for r in v["rule_coverage"]
                                  if not r["implemented"]]

    v["reason"] = _reason(v)
    return v


def _reason(v) -> str:
    b = v["bias"]
    if b == "UNAVAILABLE":
        return v.get("reason", "fuente no disponible")
    if b == "BULLISH":
        return (f"Regla 7 satisfecha: CVD sube {RULE7['streak']}+ barras, "
                f"{RULE7['levels_min']}+ niveles con >={FOOTPRINT_PCT:g}% compra, "
                f"consenso de libro multi-venue comprador ({RULE7['vac_venues']} venues, |imb5|>{RULE7['vac_thr']:g}, {RULE7['vac_polls']} lecturas)")
    if b == "BEARISH":
        return (f"Regla 6 satisfecha: CVD cae {RULE6['streak']}+ barras, "
                f"{RULE6['levels_min']}+ niveles con >={FOOTPRINT_PCT:g}% venta, "
                f"consenso de libro multi-venue vendedor ({RULE6['vac_venues']} venues, |imb5|>{RULE6['vac_thr']:g}, {RULE6['vac_polls']} lecturas)")
    r6, r7 = v["rule6"], v["rule7"]
    miss6 = [k for k, s in r6["conditions"].items() if s != "MET"]
    miss7 = [k for k, s in r7["conditions"].items() if s != "MET"]
    if v["unevaluable"]:
        return ("NEUTRAL forzado: hay condiciones UNEVALUABLES ("
                + ", ".join(v["unevaluable"])
                + "). Una regla que no se puede evaluar no es una regla que no se cumple.")
    # El ramal "aunque el scorecard da N/100" se retiro con el scorecard
    # (2026-10-13): citaba un numero medido sin poder predictivo, asi que
    # adornaba la explicacion con precision falsa.
    _r6note = ("; la regla 6 esta DESACTIVADA (evidencia insuficiente, "
               "n_eff=2.7)" if r6.get("enforcement") == "disabled" else "")
    return (f"NEUTRAL: las reglas no disparan (regla 6 falla en {len(miss6)}, "
            f"regla 7 falla en {len(miss7)}){_r6note}.")


# ── Regla 8: Audit On Loss ──────────────────────────────────────────────
# After a call that fails (price moves >1% against within 2 bars), record loss
# with root cause in audit.log.json at START of next analysis.
# This function is called from the caller (bridge/bot) when a trade signal fails.
import os as _os
# 2026-10-07 — RUTA CORREGIDA. Apuntaba a atas-bridge/audit.log.json, que es el
# fichero de 19.848 bytes del subsistema /calls ya BORRADO (call_id, entry,
# outcome_r, mfe_r, mae_r). Escribir ahi mezclaria el registro de perdidas del
# grabador con los restos de un sistema que no existe. Ahora va a su propio
# fichero dentro de reader/, que es quien lo produce.
#
# Tambien se corrigio `import os as _os` + `os.path.exists(...)`: funcionaba por
# el `import os` de la cabecera del modulo, pero leerlo era pedir un NameError
# si alguien quita ese import.
_audit_log_path = _os.path.join(HERE, "audit_losses.jsonl")

# Umbral de la regla 8: "price moves >1% against within 2 bars". Se lee de
# rules.json si la regla lo declara; si no, 1% es el valor que la regla v3.x dice.
AUDIT_LOSS_PCT = 0.01
try:
    _r8 = _load_rule_conditions(8)
    if _r8.get("_present"):
        AUDIT_LOSS_PCT = float(_r8.get("loss_pct", AUDIT_LOSS_PCT))
except Exception:
    pass

def _audit_record_loss(symbol: str, price_last: float, price_entry: float,
                       loss_pct: float, root_cause: str) -> None:
    """Record a failed call with root cause in audit.log.json for post-analysis."""
    import json as _json
    import time as _time
    log_entry = {
        "ts": round(_time.time(), 1),
        "symbol": symbol,
        "price_last": price_last,
        "price_entry": price_entry,
        "loss_pct": round(loss_pct, 4),
        "root_cause": root_cause,
    }
    try:
        # JSONL: se ANADE una linea, no se reescribe el fichero entero. La version
        # anterior lo reescribia completo en cada perdida: O(n) por escritura y
        # cualquier fallo a mitad dejaba el registro truncado.
        with open(_audit_log_path, "a", encoding="utf-8") as bf:
            bf.write(_json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def render_text(v: dict) -> str:
    if v["bias"] == "UNAVAILABLE":
        return f"[LECTURA NO DISPONIBLE] {v.get('reason','')}"
    d = v.get("dom") or {}
    c = v.get("cvd") or {}
    lines = [
        f"{v['bias']}  probability: {v['probability']}",
        f"{v['symbol']}  px {v.get('price_last')}  barra #{v.get('bar_index')} "
        f"({v.get('bar_time')})  edad {v.get('src_age_s')}s",
        f"EMA {d.get('imbalance_pct_ema')}% (n={d.get('ema_n')}) vs "
        f"instantáneo {d.get('imbalance_pct')}%  |  tape compra "
        f"{v.get('tape_buy_pct')}%"
        + (f"  |  vol ratio {v['volume_ratio']:.2f}x"
           if isinstance(v.get("volume_ratio"), (int, float)) else "  |  vol ratio n/d"),
        f"footprint: {v['footprint']['sell_heavy']} venta>={FOOTPRINT_PCT:g}% / "
        f"{v['footprint']['buy_heavy']} compra>={FOOTPRINT_PCT:g}%  POC {v.get('poc')}  "
        f"VA {v.get('value_area')}",
        f"CVD {c.get('value')}  rachas +{c.get('streak_up')}/-{c.get('streak_down')}"
        + (f"  [contiguas: {c.get('bars_since_gap')} barras desde el ultimo hueco]"
           if c.get('integrity_ok') else
           f"  [NO integra: {(c.get('integrity') or {}).get('rule6_window', {}).get('reason', '?')}]")
        + "  "
        # cvd_clean mira la serie ENTERA (incluye huecos de hace horas) y
        # integrity_ok mira solo la VENTANA que usa la regla. Con 148 huecos
        # historicos y 90 barras limpias despues, los dos pueden ser ciertos a la
        # vez y decir cosas distintas. Se muestran los dos, con su nombre, en vez
        # de un "CON HUECO" que parece un aviso cuando la racha es perfectamente
        # utilizable.
        f"[serie {'limpia' if v.get('cvd_clean') else 'con huecos antiguos'} | "
        f"ventana de la regla {'utilizable' if c.get('integrity_ok') else 'NO utilizable'}]",
    ]
    for rid, r in (("6", v["rule6"]), ("7", v["rule7"])):
        conds = ", ".join(f"{k.split('_')[0]}={s}" for k, s in r["conditions"].items())
        # AT-: namespace de atas-bridge/rules.json (el scanner tiene sus
        # propias reglas 1-4 con los mismos numeros — ver Fix 4 de
        # docs/FIXES-gate-DOM.md).
        lines.append(f"Regla AT-{rid}: {'CUMPLE' if r['met'] else 'no cumple'} ({conds})")
    if v.get("rule2", {}).get("applicable"):
        lines.append(f"Regla 2: {v['rule2']['note']}")
    lines.append(v["reason"])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--out", help="write JSON to this path")
    a = ap.parse_args()
    v = build_verdict()
    if a.out:
        Path(a.out).write_text(json.dumps(v, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    if a.text:
        print(render_text(v))
    elif a.json or not a.out:
        print(json.dumps(v, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())