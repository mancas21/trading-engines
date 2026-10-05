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

HERE = os.path.dirname(os.path.abspath(__file__))
D = json.load(open(os.path.join(HERE, "live_read.json"), encoding="utf-8"))
SY = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "SUIUSDT", "HYPEUSDT", "SPCXUSDT",
      "NVDAUSDT", "XAUUSDT"]
LINE = "─" * 62
BRIDGE_DIR = r"C:\Users\Lenovo\trading-engines\atas-bridge"
MIN_SCORE = 20.0


def real_scores():
    """The ACTUAL generator scores, from orderflow_calls.score_cross_section().

    The card used to print its own home-made composite here and call it |score| — that
    number never gated anything. On live data it disagreed with the generator on
    direction and magnitude (SUI: card +9.4 vs generator -18.8), so the TRIGGER line
    was reporting the wrong thing. If the import fails we say so rather than invent a
    substitute.
    """
    try:
        import sys
        from collections import deque
        if BRIDGE_DIR not in sys.path:
            sys.path.insert(0, BRIDGE_DIR)
        import orderflow_calls as ofc
        hists = {s: deque(maxlen=ofc.HISTORY_MAXLEN) for s in SY}
        atas = ofc.get("/indicators")
        snaps = {s: sn for s in SY
                 if (sn := ofc.collect(s, hists[s], atas, ofc.ATAS_MAX_AGE_S))}
        scored = ofc.score_cross_section(snaps)
        return {s: scored[s][0] for s in scored}, {s: snaps[s].get("atas_status") for s in scored}
    except Exception as e:  # noqa: BLE001
        return None, {"_err": f"{type(e).__name__}: {e}"}


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
r6a = t["cons_dn_closed"] >= 2
r6b = len(t["sell_levels"]) >= 4
r7a = t["cons_up_closed"] >= 2
r7b = len(t["buy_levels"]) >= 3
r6c_a, r7c_a = (t["dom_imbalance"] or 0) < 5, (t["dom_imbalance"] or 0) > 10
r6c_b, r7c_b = f["imb_ema"] < 5, f["imb_ema"] > 10
ok = lambda v: "ok" if v else "no"
print(f"RULES   R6 [a {ok(r6a)} ({t['cons_dn_closed']}dn) · b {ok(r6b)} ({len(t['sell_levels'])} lvl) · "
      f"c ATAS{ok(r6c_a)}/{t['dom_imbalance']:+.0f} BNB{ok(r6c_b)}/{f['imb_ema']:+.1f}] "
      f"→ {'PASS' if r6a and r6b and r6c_b else 'FAIL'}")
print(f"        R7 [a {ok(r7a)} ({t['cons_up_closed']}up) · b {ok(r7b)} ({len(t['buy_levels'])} lvl) · "
      f"c ATAS{ok(r7c_a)} BNB{ok(r7c_b)}] → {'PASS' if r7a and r7b and r7c_b else 'FAIL'}"
      f"{'   ← legs pass on ATAS DOM, BNB avg says no' if r7a and r7b and r7c_a and not r7c_b else ''}")
print(f"        R1 {'ok' if max(t['cons_up_closed'], t['cons_dn_closed']) >= 2 else 'no'} · "
      f"R2 exhaustion {t['rule2_multiple']}x (flag at 2x) · R3 no probability quoted · "
      f"R10 deleted in v3.3 (no band gate)")
# The trigger is the GENERATOR's composite (orderflow_calls.py monitor: |score| >=
# min_score, default 20), z-scored across symbols. The card's own bias is a different
# number with different weights and it gates nothing.
GEN, ATAS_STATUS = real_scores()
g = (GEN or {}).get(tk)
if GEN is None:
    print("TRIGGER generator score unavailable (orderflow_calls import failed) — "
          f"card bias {b_ema} is NOT the trigger")
elif g is None:
    print(f"TRIGGER no generator snapshot for {tk}")
else:
    fires = abs(g) >= MIN_SCORE
    print(f"TRIGGER {tk.replace('USDT','/USDT')} generator score {g:+.1f} vs min_score "
          f"{MIN_SCORE:.0f} → {'FIRES' if fires else 'below trigger, no plan'}"
          f"   (card bias {b_ema} is a separate, non-gating number)")
_fires = GEN is not None and g is not None and abs(g) >= MIN_SCORE
print(f"PLAN    " + ("NO TRADE — generator score below the trigger (v3.3: score ranks, never gates a band)"
                            if not _fires
                            else ("watch long" if g > 0 else "watch short") + " — R6/R7 above must confirm"))
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
