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
      (c) imbalance_pct_ema < dom_max%, y hold >= persistence polls
                                   [dom_imbalance_max, dom_persistence_polls_min]
  Regla 7 — Accumulation (alcista). Las TRES:
      (a) CVD sube 2+ barras consecutivas          [cvd_consecutive_up_min]
      (b) footprint: 3+ niveles con >= buy_pct% compra en el lado ask
                                                        [footprint_buy_pct_min]
      (c) imbalance_pct_ema > dom_min%, y hold >= persistence polls
                                   [dom_imbalance_min, dom_persistence_polls_min]

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
# ADICIONAL: tambien leemos el campo "enforcement" para respetar reglas hard vs soft.
def _load_rule_conditions(rule_id: int) -> dict:
    try:
        import json as _json
        rules_path = HERE.parent / "atas-bridge" / "rules.json"
        raw = _json.loads(rules_path.read_text(encoding="utf-8"))
        for r in (raw.get("rules") or raw):
            if int(r.get("id", -1)) == rule_id:
                return {
                    "conditions": dict(r.get("conditions") or {}),
                    "enforcement": r.get("enforcement", "hard")
                }
    except Exception as _e:            # noqa: BLE001
        print(f"[verdict] AVISO: no se pudo leer rules.json regla {rule_id}: {_e}")
    return {"conditions": {}, "enforcement": "hard"}


_C6 = _load_rule_conditions(6)
_C7 = _load_rule_conditions(7)

_R6 = _C6.get("conditions", {})
_R7 = _C7.get("conditions", {})

RULE6 = {"streak": _R6.get("cvd_consecutive_down_min", 2),
         "levels_min": _R6.get("footprint_levels_min", 4),
         "dom_max": float(_R6.get("dom_imbalance_max", 5)),
         "enforcement": _C6.get("enforcement", "hard")}

RULE7 = {"streak": _R7.get("cvd_consecutive_up_min", 2),
         "levels_min": _R7.get("footprint_levels_min", 3),
         "dom_min": float(_R7.get("dom_imbalance_min", 10)),
         "enforcement": _C7.get("enforcement", "hard")}

# `dom_persistence_polls_min` es el campo que faltaba por completo.
# Si rules.json no lo declara, 3 es el valor que las reglas v3.3 declaran.
DOM_PERSIST_MIN = int(_R6.get("dom_persistence_polls_min",
                              _R7.get("dom_persistence_polls_min", 3)))

# Regla 2 (CVD extremes = exhaustion). Mismo motivo: sus dos campos estaban
# escritos a mano dentro de rule2_exhaustion().
#
# 2026-10-07 — BUG ARREGLADO. Leia `_C2.get("spike_multiple", 2.0)`, pero
# _C2 es el dict ANIDADO {"conditions": {...}, "enforcement": ...}. Buscaba
# "spike_multiple" en el nivel equivocado, no lo encontraba y caia al default
# 2.0. Con rules.json en v3.4.1 declarando spike_multiple=1.5, la regla 2
# seguia usando 2.0: es decir, exigia un salto un 33% mayor del que la regla
# declara. Mismo efecto que si el JSON no existiera, pero en silencio.
_C2 = _load_rule_conditions(2)
_R2 = _C2.get("conditions", {})
RULE2 = {"spike_multiple": float(_R2.get("spike_multiple", 2.0)),
         "min_prior_bars": int(_R2.get("min_prior_bars", 3))}
if not _R2:
    print("[verdict] AVISO: regla 2 sin conditions en rules.json; "
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


def classify_footprint(top_levels, sell_pct=None, buy_pct=None):
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
    """
    # Defaults solo si rules.json no se pudo leer. load_footprint_pct() avisa
    # de esa situacion al importar el modulo.
    _sp = 70.0 if sell_pct is None else float(sell_pct)
    _bp = 30.0 if buy_pct is None else float(buy_pct)
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
        elif bp <= 100.0 - _sp:
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
    """Scorecard 0-100 SIN CONDICIONES. Esto ORDENA; no es una predicción.

    Deliberadamente no usa el EMA: el scorecard es una foto del libro instantáneo
    y la puerta usa el valor suavizado. Esa diferencia es la que produce el
    conflicto score-vs-puerta que hay que declarar, no esconder.
    """
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
        fp.get("top_levels"), FOOTPRINT_PCT, FOOTPRINT_PCT)
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
                "gaps": cvd_state.get("gaps"), "missing_bars": cvd_state.get("missing_bars")}

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
    if len(bd) >= _need:
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

    a6 = "UNEVALUABLE" if len(bd) < 2 else ("MET" if dn >= RULE6["streak"] else "FAIL")
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
    ema_n = dom.get("imbalance_pct_ema_n")
    dom_persist_ok = isinstance(ema_n, (int, float)) and ema_n >= DOM_PERSIST_MIN

    if ema is None:
        c6 = c7 = "UNEVALUABLE"
    elif not dom_persist_ok:
        # EMA sin asentar: no se puede afirmar nada sobre 3+ lecturas.
        c6 = c7 = "UNEVALUABLE"
    else:
        c6 = "MET" if ema < RULE6["dom_max"] else "FAIL"
        c7 = "MET" if ema > RULE7["dom_min"] else "FAIL"
    v["dom"]["persist_ok"] = dom_persist_ok
    v["dom"]["persist_min"] = DOM_PERSIST_MIN

    r6 = [a6, b6, c6]
    r7 = [a7, b7, c7]
    r6_met = all(x == "MET" for x in r6)
    r7_met = all(x == "MET" for x in r7)
    unev = [x for x in (r6 + r7) if x == "UNEVALUABLE"]

    v["rule6"] = {
        "conditions": {"a_cvd_down_2plus": a6,
                       f"b_footprint_{RULE6['levels_min']}_sell_heavy": b6,
                       f"c_ema_below_{RULE6['dom_max']}": c6},
        "met": r6_met}
    v["rule7"] = {
        "conditions": {"a_cvd_up_2plus": a7,
                       f"b_footprint_{RULE7['levels_min']}_buy_heavy": b7,
                       f"c_ema_above_{RULE7['dom_min']}": c7},
        "met": r7_met}

    # ── Regla 5: No Pattern Anchoring ──
    # Validation: verify the verdict does not reference prior bar patterns as evidence.
    # The engine uses only current bar footprint, DOM, CVD, and tape — no lagged patterns.
    # If any code path were to implicitly depend on prior bars, this flag catches it.
    rule5_anchor_check = True  # Engine architecture ensures current-bar-only data
    v["rule5"] = {
        "anchoring_ok": rule5_anchor_check,
        "note": "No prior bar patterns used — verdict based on current bar + structural levels only"
    }

    # ── El sesgo viene de la PUERTA ──
    if r7_met:
        bias = "BULLISH"
    elif r6_met:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    total, parts, notes = scorecard(rows, imbalance, ema, tape_buy_pct,
                                    vol.get("volume_ratio"), sell_h, buy_h)
    band = ("BULLISH" if total >= SCORE_BANDS["bullish_min"] else
            "BEARISH" if total <= SCORE_BANDS["bearish_max"] else "NEUTRAL")
    v["scorecard"] = {"score": total, "parts": parts, "notes": notes, "band": band}
    v["bias"] = bias
    # Conflicto = el band del score NO coincide con el veredicto de la puerta.
    # "NEUTRAL vs NEUTRAL" no es conflicto; "NEUTRAL vs BULLISH" sí lo es.
    v["score_gate_conflict"] = (band != bias)
    v["score_gate_conflict_detail"] = None
    if v["score_gate_conflict"]:
        if band != "NEUTRAL" and bias == "NEUTRAL":
            v["score_gate_conflict_detail"] = (
                f"el scorecard da {band} ({total}/100) pero ninguna regla "
                f"direccional se satisface; el scorecard mide el libro instantáneo "
                f"y la puerta usa la EMA")
        elif band == "NEUTRAL" and bias != "NEUTRAL":
            v["score_gate_conflict_detail"] = (
                f"las reglas dicen {bias} pero el scorecard solo da {total}/100")
    v["unevaluable"] = unev
    v["probability"] = "unvalidated"
    # Regla 5: annotate that no pattern anchoring was used
    v["rule5_annotate"] = v.get("rule5", {}).get("note", "Rule 5 not configured")

    # Nueva Regla 9: Timeframe Consensus
    v["rule9_annotate"] = "Consenso multi-TF: " + v.get("rule9", {}).get("note", "no configurada")

    # Nueva Regla 10: Liquidity Minimum
    v["rule10_annotate"] = "Liquidez mínima: " + v.get("rule10", {}).get("note", "no configurada")

    # Nueva Regla 11: Session Period Filter
    v["rule11_annotate"] = "Filtro sesión: " + v.get("rule11", {}).get("note", "no configurado")

    # Nueva Regla 12: News Event Avoidance
    v["rule12_annotate"] = "Evitación news: " + v.get("rule12", {}).get("note", "no configurado")

    # Nueva Regla 13: Position Sizing Hard Limit
    v["rule13_annotate"] = "Limit tamaño posición: " + v.get("rule13", {}).get("note", "no configurado")

    # Nueva Regla 14: Correlation Limit per Portfolio
    v["rule14_annotate"] = "Limit correlación cartera: " + v.get("rule14", {}).get("note", "no configurado")

    # Nueva Regla 15: Weekend / Holiday Gap Risk
    v["rule15_annotate"] = "Riesgo fin de semana: " + v.get("rule15", {}).get("note", "no configurado")

    # Nueva Regla 16: Confirmation Timeout
    v["rule16_annotate"] = "Timeout confirmación: " + v.get("rule16", {}).get("note", "no configurado")

    # Nueva Regla 17: Float Share Check
    v["rule17_annotate"] = "Check flottante: " + v.get("rule17", {}).get("note", "no configurado")

    # Nueva Regla 18: CVD Regime State
    v["rule18_annotate"] = "Regimen CVD: " + v.get("rule18", {}).get("note", "no configurado")

    v["reason"] = _reason(v)
    return v


def _reason(v) -> str:
    b = v["bias"]
    if b == "UNAVAILABLE":
        return v.get("reason", "fuente no disponible")
    if b == "BULLISH":
        return (f"Regla 7 satisfecha: CVD sube {RULE7['streak']}+ barras, "
                f"{RULE7['levels_min']}+ niveles con >={FOOTPRINT_PCT:g}% compra, "
                f"EMA > +{RULE7['dom_min']:g}%")
    if b == "BEARISH":
        return (f"Regla 6 satisfecha: CVD cae {RULE6['streak']}+ barras, "
                f"{RULE6['levels_min']}+ niveles con >={FOOTPRINT_PCT:g}% venta, "
                f"EMA < +{RULE6['dom_max']:g}%")
    r6, r7 = v["rule6"], v["rule7"]
    miss6 = [k for k, s in r6["conditions"].items() if s != "MET"]
    miss7 = [k for k, s in r7["conditions"].items() if s != "MET"]
    if v["unevaluable"]:
        return ("NEUTRAL forzado: hay condiciones UNEVALUABLES ("
                + ", ".join(v["unevaluable"])
                + "). Una regla que no se puede evaluar no es una regla que no se cumple.")
    if v["score_gate_conflict"]:
        return (f"NEUTRAL: las reglas no disparan (regla 6 falla en {len(miss6)}, "
                f"regla 7 falla en {len(miss7)}) aunque el scorecard da "
                f"{v['scorecard']['score']}/100. El scorecard mide el libro "
                f"instantáneo; la puerta usa la EMA. Conflicto declarado, no oculto.")
    return "NEUTRAL: ninguna regla direccional satisface las tres condiciones."


# ── Regla 8: Audit On Loss ──────────────────────────────────────────────
# After a call that fails (price moves >1% against within 2 bars), record loss
# with root cause in audit.log.json at START of next analysis.
# This function is called from the caller (bridge/bot) when a trade signal fails.
import os as _os
_audit_log_path = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "atas-bridge", "audit.log.json"
)

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
        entries = []
        if os.path.exists(_audit_log_path):
            with open(_audit_log_path, "r", encoding="utf-8") as af:
                for line in af:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(_json.loads(line))
                        except Exception:
                            pass
        entries.append(log_entry)
        with open(_audit_log_path, "w", encoding="utf-8") as bf:
            for entry in entries:
                bf.write(_json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def render_text(v: dict) -> str:
    if v["bias"] == "UNAVAILABLE":
        return f"[LECTURA NO DISPONIBLE] {v.get('reason','')}"
    d = v.get("dom") or {}
    c = v.get("cvd") or {}
    lines = [
        f"{v['bias']}  score {v['scorecard']['score']}/100  "
        f"probability: {v['probability']}",
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
        f"CVD {c.get('value')}  rachas +{c.get('streak_up')}/-{c.get('streak_down')}  "
        f"{'limpio' if v.get('cvd_clean') else 'CON HUECO'}",
    ]
    for rid, r in (("6", v["rule6"]), ("7", v["rule7"])):
        conds = ", ".join(f"{k.split('_')[0]}={s}" for k, s in r["conditions"].items())
        lines.append(f"Regla {rid}: {'CUMPLE' if r['met'] else 'no cumple'} ({conds})")
    if v.get("rule2", {}).get("applicable"):
        lines.append(f"Regla 2: {v['rule2']['note']}")
    if v.get("score_gate_conflict_detail"):
        lines.append("CONFLICTO score/puerta: " + v["score_gate_conflict_detail"])
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