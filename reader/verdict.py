"""Fase 3 — motor de veredictos. Implementa rules.json v3.3 como salida principal.

POR QUÉ ESTE MÓDULO
===================
El motor viejo generaba señales y no tenía capa de veredicto: por eso sus
lecturas no sostenían una conclusión. Aquí las REGLAS son la salida, y el
scorecard es un RANKING que nunca sustituye a la puerta.

Las tres reglas duras que este módulo implementa (verificadas contra
atas-bridge/rules.json v3.3, no de memoria):

  Regla 6 — Distribution (bajista). Las TRES:
      (a) CVD cae 2+ barras consecutivas
      (b) footprint: 4+ niveles con >70% venta en el lado bid
      (c) imbalance_pct_ema < +5%
  Regla 7 — Accumulation (alcista). Las TRES:
      (a) CVD sube 2+ barras consecutivas
      (b) footprint: 3+ niveles con >70% compra en el lado ask
      (c) imbalance_pct_ema > +10%
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
import time
import urllib.request
from pathlib import Path

BRIDGE = "http://127.0.0.1:8000"
LIVE_MAX_AGE_S = 300.0
HERE = Path(__file__).resolve().parent

# ── Umbrales, transcritos de atas-bridge/rules.json v3.3 ──
RULE6 = {"streak": 2, "levels_min": 4, "dom_max": 5.0}
RULE7 = {"streak": 2, "levels_min": 3, "dom_min": 10.0}
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
    if prev_high is not None and high is not None and abs(high) > 2 * abs(prev_high) > 0:
        out = {"applicable": True, "side": "up",
               "note": "salto de CVD >2x el máximo previo: AGOTAMIENTO, no continuación"}
    elif prev_low is not None and low is not None and abs(low) > 2 * abs(prev_low) > 0:
        out = {"applicable": True, "side": "down",
               "note": "caída de CVD >2x el mínimo previo: AGOTAMIENTO, no continuación"}
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


def classify_footprint(top_levels):
    """Regla 6 cuenta bid%>70; regla 7 cuenta bid%<30.

    Lados sin contraparte (ask==0 o bid==0) se marcan `thin`: inflan ambos
    recuentos y no son evidencia. Se informan aparte, nunca se ocultan.
    """
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
        if bp > 70.0:
            sell_h += 1
            tag = "SELL>70"
        elif bp < 30.0:
            buy_h += 1
            tag = "BUY>70"
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

    rows, sell_h, buy_h, thin = classify_footprint(fp.get("top_levels"))
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
    v["rule2"] = rule2_exhaustion(cvd_state.get("cvd"), cvd_state.get("high_cvd"),
                                  cvd_state.get("low_cvd"),
                                  cvd_state.get("prev_high_cvd"),
                                  cvd_state.get("prev_low_cvd"))
    # ── Regla 8: CVD roto por un hueco no es una serie limpia ──
    v["cvd_clean"] = (cvd_state.get("continuity") == "clean"
                      and not (cvd_state.get("missing_bars") or 0))

    a6 = "UNEVALUABLE" if len(bd) < 2 else ("MET" if dn >= RULE6["streak"] else "FAIL")
    a7 = "UNEVALUABLE" if len(bd) < 2 else ("MET" if up >= RULE7["streak"] else "FAIL")
    b6 = "MET" if sell_h >= RULE6["levels_min"] else "FAIL"
    b7 = "MET" if buy_h >= RULE7["levels_min"] else "FAIL"
    c6 = ("UNEVALUABLE" if ema is None else
          ("MET" if ema < RULE6["dom_max"] else "FAIL"))
    c7 = ("UNEVALUABLE" if ema is None else
          ("MET" if ema > RULE7["dom_min"] else "FAIL"))

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
    v["reason"] = _reason(v)
    return v


def _reason(v) -> str:
    b = v["bias"]
    if b == "UNAVAILABLE":
        return v.get("reason", "fuente no disponible")
    if b == "BULLISH":
        return "Regla 7 satisfecha: CVD sube 2+ barras, 3+ niveles con >70% compra, EMA > +10%"
    if b == "BEARISH":
        return "Regla 6 satisfecha: CVD cae 2+ barras, 4+ niveles con >70% venta, EMA < +5%"
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
        f"footprint: {v['footprint']['sell_heavy']} venta>70% / "
        f"{v['footprint']['buy_heavy']} compra>70%  POC {v.get('poc')}  "
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