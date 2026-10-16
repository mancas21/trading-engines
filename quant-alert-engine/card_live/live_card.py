#!/usr/bin/env python3
"""Live quant card — the short delivery format. SINGLE SOURCE OF TRUTH.

This file is the ONLY implementation of the card. The chat analysis and the Telegram
alert (card_alert.py -> quant_alert_engine.send_periodic_snapshot) both run THIS file, so
the two channels cannot drift apart.

One card, <= ~20 lines, primary symbol = whatever chart ATAS is on, then a basket read
and engine status. Numbers come from live_read.json (single pull).

  python fetch_live.py && python live_read.py && python live_card.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
D = json.load(open(os.path.join(HERE, "live_read.json"), encoding="utf-8"))
SY = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "SUIUSDT", "HYPEUSDT", "SPCXUSDT",
      "NVDAUSDT", "XAUUSDT"]
LINE = "─" * 62
READER_DIR = r"C:\Users\Lenovo\trading-engines\reader"


def gate_states():
    """Las reglas 6 y 7 y el sesgo, LEIDOS de la puerta (reader/verdict.py).

    La puerta es la unica implementacion de las reglas: sus umbrales salen de
    atas-bridge/rules.json y su condicion (c) es el consenso multi-venue
    normalizado. Esta tarjeta los IMPRIME, no los recalcula. Devuelve
    (veredicto, None), o (None, motivo) cuando la puerta no puede opinar; en ese
    caso el llamante lo dice y NO inventa un sustituto, porque un calculo local
    seria un veredicto distinto con la misma autoridad aparente.
    """
    try:
        if READER_DIR not in sys.path:
            sys.path.insert(0, READER_DIR)
        import verdict
        v = verdict.build_verdict()
        if v.get("bias") == "UNAVAILABLE":
            return None, v.get("reason") or "la puerta no pudo construir veredicto"
        return v, None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def clip(x):
    return max(-1.0, min(1.0, x or 0.0))


def bias(imb, m, f, w):
    wr = (w["bid"][1] or 0) / (w["ask"][1] or 1e-9)
    ls = f["ls_ratio"] if f["ls_ratio"] else 2.0        # missing ratio is neutral, not zero
    parts = [(30, clip((m["delta_4n"] or 0.0) / 3.0)), (15, clip((m["cvd_slope_16"] or 0.0) / 3.0)),
             (20, clip((imb or 0) / 20.0)), (10, clip((wr - 1))),
             (10, clip(-(f["funding_ann_pct"] or 0.0) / 20.0)),
             (5, clip((2.0 - ls) / 1.5)),
             (10, clip((f["dist_poc_atr"] or 0.0) / 2.0))]
    tot = sum(x for x, _ in parts)
    return round(50 + 50 * sum(x * v for x, v in parts) / tot, 1)


def zone(b):
    return "BULLISH" if b >= 60 else "BEARISH" if b <= 40 else "NEUTRAL"


sc = {}
for s in SY:
    m, f, w = D["multi_tf"][s], D["flow"][s], D["walls"][s]
    sc[s] = {"ema": bias(f["imb_ema"], m, f, w), "raw": bias(f["imb_raw"], m, f, w)}

tk = D["health"]["atas_ticker"]
m, f, w, t = D["multi_tf"][tk], D["flow"][tk], D["walls"][tk], D["tape"]
b_ema, b_raw = sc[tk]["ema"], sc[tk]["raw"]
sym_disp = tk.replace("USDT", "/USDT")

_dg = list(D.get("degraded") or [])
if (D["health"].get("atas_age_s") or 0) > 15:
    _dg.append(f"ATAS sample is {D['health']['atas_age_s']}s old at pull time "
               f"(bridge stale threshold 15s) — footprint data genuinely lagging")
if _dg:
    print(f"⚠️  FEED DEGRADED — {len(_dg)} source(s) down. Blanks below are data gaps, not zeros.")
    for _d in _dg[:6]:
        print(f"   · {_d}")
    if len(_dg) > 6:
        print(f"   · (+{len(_dg) - 6} more)")
print(LINE)
print(f"{sym_disp}  ${f['mid']:.4f}   bias {b_ema} {zone(b_ema)}   "
      f"{D.get('snapshot_as_of', '')[11:19]}")
print(f"  15m {m['15m']['trend']} ({m['15m']['close_vs_e20_pct']:+.2f}% vs e20) · "
      f"1h {m['1h']['trend']} ({m['1h']['close_vs_e20_pct']:+.2f}%) · "
      f"4h {m['4h']['trend']} ({m['4h']['close_vs_e20_pct']:+.2f}%) · "
      f"RSI {m['15m']['rsi']:.0f}/{m['1h']['rsi']:.0f}/{m['4h']['rsi']:.0f} · "
      f"vol {m['vol_mult']:.2f}x med")
print(LINE)
print(f"FLOW    bar δ {t['delta']:,.0f} · {100 * t['volume']['bid'] / max(t['volume']['total'], 1):.0f}% of vol into bid")
print(f"        tape last {t['tape']['buy_count'] + t['tape']['sell_count']} prints: "
      f"{t['tape']['buy_count_pct']:.0f}% buy · net {t['tape']['net_vol']:+,.0f} units · "
      f"largest {t['tape']['largest_trade']:,.0f} {t['tape']['largest_trade_side']}"
      f" (${t['largest_usd']:,.0f})")
print(f"        DOM   ATAS {t['dom_imbalance']:+.1f}%  vs  Binance raw {f['imb_raw']:+.1f}% / avg45s {f['imb_ema']:+.1f}%")
print(f"        CVD   1m {f['cvd_1m']:+,.0f} · 5m {f['cvd_5m']:+,.0f} USD · closed bars "
      f"{t['cons_up_closed']}up/{t['cons_dn_closed']}dn · exhaustion {t['rule2_multiple']}x vs 2x flag")
print(f"LEVELS  bid wall {w['bid'][0]} ×{w['bid'][1]:,.0f}{'  (held)' if w['bid'][2] else ''}  │  "
      f"ask wall {w['ask'][0]} ×{w['ask'][1]:,.0f}{'  (held)' if w['ask'][2] else ''}")
_poc_atr = f["dist_poc_atr"]
_poc_txt = f"{_poc_atr:+.1f} ATR from price" if _poc_atr is not None else "POC dist n/a"
_depth = f" · depth ${w['bid_depth_usd']:,}/${w['ask_depth_usd']:,}"
if f["poc"] is None:
    print(f"        POC n/a (bridge /context unavailable){_depth}")
else:
    print(f"        POC {f['poc']} = {_poc_txt} · VA {f['val']}–{f['vah']} "
          f"({f['va_loc']}){_depth}")
# ── RULES: salida de la PUERTA, no una copia propia ─────────────────────────
#
# Esta tarjeta tenia su PROPIA implementacion de las reglas 6 y 7, con TRES
# problemas, medidos en vivo el 2026-10-16:
#   1. umbrales ESCRITOS A MANO (`cons_dn_closed>=2`, `sell_levels>=4`,
#      `buy_levels>=3`) en vez de leerse de atas-bridge/rules.json;
#   2. su PROPIA condicion (c): `imb_ema < 5 / > 10`, que es el imbalance
#      ABSOLUTO del libro de Binance — justo el enfoque que la puerta ABANDONO
#      tras medirlo (mide "que venue vive fuera de su banda", no acuerdo);
#   3. un PASS/FAIL que ademas usaba SOLO la pata de Binance e ignoraba la de
#      ATAS, pese a imprimirla.
# Efecto medido ese dia: la tarjeta imprimia `R6 a ok (3dn)` mientras la puerta
# decia `a_cvd_down_2plus=FAIL`, y su (c) daba `FAIL` contra `SPLIT` /
# `UNEVALUABLE` de la puerta. Dos canales, dos veredictos sobre lo mismo.
#
# Ahora se IMPRIME el veredicto de la puerta. No se recalcula: si la puerta no
# puede opinar se dice, y no se sustituye por un calculo local.
GATE, _gate_err = gate_states()
print("RULES   desde la PUERTA (reader/verdict.py) — no recalculadas aqui")
if GATE is None:
    print(f"        puerta no disponible: {_gate_err}")
    print("        sin veredicto de reglas (no se sustituye por un calculo local)")
else:
    for _tag, _key in (("R6", "rule6"), ("R7", "rule7")):
        _r = GATE[_key]
        _conds = " · ".join(f"{k}={v}" for k, v in _r["conditions"].items())
        print(f"        {_tag} {'MET' if _r['met'] else 'no'}   {_conds}")
    _r2 = GATE.get("rule2") or {}
    _r2txt = _r2.get("note", "sin nota") if _r2.get("applicable") else "no aplicable"
    _enf = GATE["rule6"].get("enforcement", "?")
    _counts = "" if GATE["rule6"].get("counts_toward_bias", True) else " (no cuenta para el sesgo)"
    _age = GATE.get("src_age_s")
    _agetxt = f" · datos del gate {_age:.0f}s" if isinstance(_age, (int, float)) else ""
    print(f"        sesgo de la puerta {GATE['bias']} · R2 {_r2txt} · R6={_enf}{_counts}{_agetxt}")
# El generador de scores (orderflow_calls.score_cross_section) YA NO EXISTE: se
# borro en d9b5d15 ("limpieza de la ruta perdedora"), asi que la linea TRIGGER
# solo podia fallar. Y fallaba MINTIENDO sobre el motivo: imprimia "generator
# score below the trigger", que sugiere que el score existe y esta bajo, cuando
# lo cierto es que el modulo no esta. El plan se lee del sesgo de la PUERTA,
# que es lo unico que decide algo en esta tarjeta.
if GATE is None:
    print("PLAN    sin plan — la puerta no puede evaluar las reglas")
else:
    print("PLAN    " + {"BULLISH": "watch long — R7 MET",
                       "BEARISH": "watch short — R6 MET"}
          .get(GATE["bias"], "NO TRADE — las reglas no disparan"))
print(f"        setup invalidation needs 2 closed 15m bars, not a wick (R1) · "
      f"flip level {w['ask'][0]} up / {w['bid'][0]} down")
_vol_txt = f"{f['rng4h_vs_em']}x" if f['rng4h_vs_em'] is not None else "n/a"
print(f"RISK    vol {_vol_txt} engine E[move] · "
      f"funding {f['funding_ann_pct']:+.1f}%/ann" if f['funding_ann_pct'] is not None
      else f"RISK    vol {_vol_txt} engine E[move] · funding n/a", end="")
print(f" · L/S {f['ls_ratio']:.2f}" if f["ls_ratio"] else " · L/S n/a (feed returning null)", end="")
print(f" · spread {f['spread_bps']:.2f}bp" if f["spread_bps"] is not None else " · spread n/a")
print(LINE)
order = sorted(sc, key=lambda s: -sc[s]["ema"])
top = " · ".join(f"{s.replace('USDT','')} {sc[s]['ema']:.0f}{zone(sc[s]['ema'])[:1]}" for s in order[:3])
bot = " · ".join(f"{s.replace('USDT','')} {sc[s]['ema']:.0f}{zone(sc[s]['ema'])[:1]}" for s in order[-2:])
_pc1, _br = D["risk"].get("corr_pc1_pct"), D["risk"].get("eff_breadth")
_corr_txt = (f"corr PC1 {_pc1}% (~{_br} independent bets)"
             if _pc1 is not None and _br is not None
             else "corr PC1 n/a (insufficient klines)")
print(f"BASKET  top {top}  |  bottom {bot}")
thin = min(sc, key=lambda s: D["walls"][s]["ask_depth_usd"])
print(f"        thinnest book: {thin.replace('USDT','')} ${D['walls'][thin]['bid_depth_usd']:,} bid / "
      f"${D['walls'][thin]['ask_depth_usd']:,} ask · {_corr_txt}")
print(LINE)
al = [(a["age_min"], tf, a["symbol"], a["signal"]) for tf, alist in (D["quant"] or {}).items()
      for a in alist]
al.sort()
alerts = " · ".join(f"[{tf}] {s} {sig} {age:.0f}m" for age, tf, s, sig in al[:3]) or "none"
print(f"ENGINE  bridge {D['health']['bridge']} · QAE {D['health']['qae']} · ATAS {tk} "
      f"{D['health']['atas_age_s']}s old · blackout {D['health']['blackout']}")
print(f"        freshest alerts: {alerts}")
print(LINE)
