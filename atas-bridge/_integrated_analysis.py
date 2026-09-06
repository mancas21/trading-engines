"""
_integrated_analysis.py — Multi-source analysis for Telegram delivery.

Uses the deterministic signal engine (signal_engine.py) for scoring.
No independent voting logic — single source of truth.
"""

import json
import os
import sys
import urllib.request
from urllib.parse import quote

from signal_engine import build_snapshot_from_live, compute_signal

# Load .env (same pattern as bridge_server.py)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip()
                if _k and _v and not os.environ.get(_k):
                    os.environ[_k] = _v

SYMBOL = sys.argv[1].upper() if len(sys.argv) > 1 else "SOLUSDT"
SYMBOL_RAW = SYMBOL.replace("USDT", "usdt")
TRADE_VOL_UNIT = SYMBOL.replace("USDT", "")
BASE = "http://127.0.0.1:8000"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def jget(path):
    return json.loads(urllib.request.urlopen(f"{BASE}{path}").read())


try:
    atas = jget(f"/state/{SYMBOL}")
except Exception:
    atas = {"alerts": []}

dom = jget(f"/dom/{SYMBOL.lower()}")
live = jget("/indicators")

try:
    cvd = jget(f"/cvd/{SYMBOL}")
except Exception:
    cvd = None

SYM_SHORT = TRADE_VOL_UNIT
quant_path = f"/quant/{SYM_SHORT}" if SYM_SHORT != "SOL" else "/quant"
try:
    quant = jget(quant_path)
except Exception:
    quant = {}
quant_data = quant if isinstance(quant, list) else quant.get(SYM_SHORT, quant)

try:
    ctx = jget(f"/context/{SYMBOL}")
except Exception:
    ctx = {}
try:
    stats = jget("/calls/stats")
except Exception:
    stats = {}

atas_alerts = atas.get("alerts", [])
ts = dom.get("trade_summary", {})

bid_count = sum(1 for a in atas_alerts if "Best Bid" in a.get("raw", ""))
ask_count = sum(1 for a in atas_alerts if "Best Ask" in a.get("raw", ""))

quant_types = {}
if isinstance(quant_data, list):
    for a in quant_data:
        t = a.get("signal_type", "")
        n = a.get("signal_name", "")
        quant_types.setdefault(t, []).append(n)

lines = [
    f"<b>📊 {SYMBOL} — Análisis Integrado Completo</b>",
    f"━━━━━━━━━━━━━━━━━━━━",
    f"<b>Precio:</b> ${dom['best_bid']:.2f} | Spread ${dom['spread']:.2f}",
    f"<b>Imbalance DOM:</b> {dom['imbalance_pct']:+.1f}%",
]

# ── Signal Engine Score ──
ticker = live.get("ticker", "").upper() if live else "?"
snap = build_snapshot_from_live(live, cvd) if live else None
sig = compute_signal(snap) if snap else None

if sig:
    bias_icon = "🟢" if sig.bias_label == "bullish" else ("🔴" if sig.bias_label == "bearish" else "🟡")
    lines.append(f"\n<b>🎯 SIGNAL ENGINE SCORE: {sig.bias_score:.0f}/100 {bias_icon}</b>")
    lines.append(f"Bias: {sig.bias_label.upper()} {'✅' if sig.trade_plan_allowed else '⛔ No trade plan (Rule 10)'}")
    for k, v in sig.sub_scores.items():
        lines.append(f"  {k}: {v:.1f}/10")
    if sig.rule_2_exhaustion:
        lines.append(f"  ⚠️ Rule 2: CVD exhaustion — no continuation call")
    if sig.rule_6_active:
        lines.append(f"  📉 Rule 6: Distribution active")
    if sig.rule_7_active:
        lines.append(f"  📈 Rule 7: Accumulation active")

# ── CVD ──
lines.append(f"\n<b>🔷 CVD — Cumulative Volume Delta</b>")
if cvd and cvd.get("bars", 0) > 0:
    c = cvd["cvd"]
    cvd_icon = "🟢" if c >= 0 else "🔴"
    lines.append(f"  CVD: {cvd_icon} {c:+,.0f} ({cvd['bars']} barras)")
    lines.append(f"  Rango: {cvd['low_cvd']:+,.0f} → {cvd['high_cvd']:+,.0f}")
    lines.append(f"  Última barra: {cvd['last_delta']:+,.0f} | Actual: {cvd['current_bar_delta']:+,.0f}")
    d = cvd.get("bar_deltas", [])
    if len(d) >= 2 and d[-1] > 0 and d[-2] > 0:
        lines.append(f"  Tendencia (2 barras): 🟢 subiendo")
    elif len(d) >= 2 and d[-1] < 0 and d[-2] < 0:
        lines.append(f"  Tendencia (2 barras): 🔴 cayendo")
    else:
        lines.append(f"  Tendencia (2 barras): 🟡 mixta")
else:
    lines.append(f"  ⚪ CVD no disponible (sin barras cerradas)")

# ── ATAS Smart Tape ──
lines.append(f"\n<b>🔷 SMART TAPE — ATAS</b>")
lines.append(f"Bid alerts: {bid_count} 🟢 | Ask alerts: {ask_count} 🔴")
if atas_alerts:
    last = atas_alerts[-1]
    lines.append(f"Última muralla: {last.get('raw', '')} → ${last.get('precio', '?')}")

# ── DOM + Trade Flow ──
lines.append(f"\n<b>🔷 DOM + TRADE FLOW — Binance</b>")
lines.append(f"Bid Wall: ${dom['bid_wall_price']:.2f} — {dom['bid_wall_vol']:.0f} {TRADE_VOL_UNIT}")
lines.append(f"Ask Wall: ${dom['ask_wall_price']:.2f} — {dom['ask_wall_vol']:.0f} {TRADE_VOL_UNIT}")
lines.append(f"Trade Flow (10s): 🟢 Buy {ts.get('buy_vol', 0):.1f} {TRADE_VOL_UNIT} | 🔴 Sell {ts.get('sell_vol', 0):.1f} {TRADE_VOL_UNIT}")
lines.append(f"Delta agresivo: {'🟢' if ts.get('net', 0) > 0 else '🔴'} {ts.get('net', 0):+.1f} {TRADE_VOL_UNIT} ({ts.get('net_usd', 0):+,.0f} USD)")

# ── ATAS DELTA EN VIVO ──
lines.append(f"\n<b>🔷 ATAS DELTA EN VIVO — LiveExporter</b>")
if live and "delta" in live:
    d = live["delta"]
    tv = live["volume"]["total"]
    ta = live["volume"]["ask"]
    tb = live["volume"]["bid"]
    lp = live["price"]["last"]
    bid_pct = round(tb / tv * 100, 1) if tv > 0 else 0
    ask_pct = round(ta / tv * 100, 1) if tv > 0 else 0
    delta_icon = "🟢" if d >= 0 else "🔴"
    lines.append(f"  Delta: {delta_icon} {d:+.1f}")
    lines.append(f"  Volumen: {tv:,.1f} {TRADE_VOL_UNIT} (Bid {bid_pct:.0f}% / Ask {ask_pct:.0f}%)")
    if bid_pct > 60:
        tp = f"🔴 {bid_pct:.0f}% Bid — oferta dominando demanda"
    elif ask_pct > 60:
        tp = f"🟢 {ask_pct:.0f}% Ask — demanda dominando oferta"
    elif bid_pct > 55:
        tp = f"🟡 {bid_pct:.0f}% Bid — leve presión vendedora"
    elif ask_pct > 55:
        tp = f"🟡 {ask_pct:.0f}% Ask — leve presión compradora"
    else:
        tp = f"⚪ Bid {bid_pct:.0f}% / Ask {ask_pct:.0f}% — balanceado"
    lines.append(f"  Tape Pressure: {tp}")
    bar_o = live.get("price", {}).get("open")
    if bar_o:
        bar_dir = "🟢" if lp >= bar_o else "🔴"
        lines.append(f"  Barra: {bar_dir} ${bar_o:.2f} → ${lp:.2f}")
        if (lp >= bar_o and d > 0) or (lp < bar_o and d < 0):
            lines.append(f"  ✅ Delta CONFIRMA dirección")
        else:
            lines.append(f"  ⚠️ Delta RECHAZA dirección — divergencia intra-bar")
else:
    lines.append(f"  ⚪ LiveExporter no disponible")

# ── Quant Engine ──
lines.append(f"\n<b>🔷 QUANT ENGINE — Indicadores</b>")
for sig_type, sig_names in quant_types.items():
    unique = list(set(sig_names))
    icons = {"VOL": "🟡", "RSI": "🟣", "MACD": "🟠", "STOCH": "🔘"}
    icon = icons.get(sig_type, "⚪")
    for name in unique:
        c = "🟢" if any(x in name for x in ["Bull", "Up", "Above"]) else ("🔴" if any(x in name for x in ["Bear", "Down", "Below"]) else "🟡")
        lines.append(f"  {c} [{sig_type}] {name}")

# ── Trade Plan (Rule 10 — live-anchored) ──
live_price = live.get("price", {}).get("last") if live else None
# Expected move from context — p90 now rides on its own field (audit M4)
exp_med = ctx.get("expected_move", {}).get("value", 0.44) if isinstance(ctx, dict) else 0.44
exp_p90 = ctx.get("expected_move", {}).get("p90") if isinstance(ctx, dict) else None
if exp_p90 is None:
    ci = ctx.get("expected_move", {}).get("confidence_interval", None) if isinstance(ctx, dict) else None
    exp_p90 = ci[1] if isinstance(ci, (list, tuple)) and len(ci) > 1 else None
if exp_p90 is None:
    exp_p90 = exp_med * 4.6
if isinstance(exp_med, dict):
    exp_med = exp_med.get("value", 0.44)
# Normalize fraction vs percent
if exp_med < 0.05:
    exp_med *= 100
if exp_p90 < 0.05:
    exp_p90 *= 100
if live_price and sig:
    if not sig.trade_plan_allowed:
        # Audit C2/C4: show WHY the plan is blocked (rule veto / coverage / neutral)
        reasons = getattr(sig, "plan_blocked_reasons", []) or []
        cov = getattr(sig, "data_coverage", 1.0)
        if reasons:
            why = " | ".join(reasons)
        else:
            why = "zona neutral 40-59 (Rule 10)"
        lines.append(f"\n<b>📋 TRADE PLAN</b>")
        lines.append(f"  ⛔ WAIT FOR CONFIRMATION — bias {sig.bias_score:.0f} (cov {cov:.0%}). Sin plan: {why}.")
    else:
        direction = "LONG" if sig.bias_label == "bullish" else "SHORT"
        # Entry zone = live ±0.15% (tied to spread/va), SL = entry ±0.85*exp_med, T1 = entry ±1.0*exp_med, T2 = entry ±1.8*exp_p90
        half_entry = live_price * 0.0015
        sl_dist = live_price * (exp_med/100) * 0.85
        t1_dist = live_price * (exp_med/100)
        t2_dist = live_price * (exp_med/100) * 1.8
        if direction == "LONG":
            entry_low, entry_high = live_price - half_entry, live_price + half_entry
            sl, t1, t2 = entry_low - sl_dist, entry_high + t1_dist, entry_high + t2_dist
        else:
            entry_low, entry_high = live_price - half_entry, live_price + half_entry
            sl, t1, t2 = entry_high + sl_dist, entry_low - t1_dist, entry_low - t2_dist
        lines.append(f"\n<b>📋 TRADE PLAN — {direction} (bias {sig.bias_score:.0f}, cov {getattr(sig, 'data_coverage', 1.0):.0%})</b>")
        lines.append(f"  Entry: ${entry_low:.2f} – ${entry_high:.2f} (live ${live_price:.2f} ±0.15%)")
        lines.append(f"  SL: ${sl:.2f} (invalida {'cierre < SL' if direction=='LONG' else 'cierre > SL'})")
        lines.append(f"  T1: ${t1:.2f} | T2: ${t2:.2f} (med {exp_med:.2f}% / p90 {exp_p90:.2f}%)")
        # Auto-log call for track record (Rule 10 + audit) — attach compact
        # order-flow snapshot so the call is signal-validated (phase 2).
        try:
            import urllib.request as _ur
            import uuid as _uuid, time as _t
            _live = live or {}
            _fp = _live.get("footprint", {}) or {}
            _tape = _live.get("tape", {}) or {}
            _cvd_d = (cvd or {}) or {}
            snap_compact = {
                "ts": _t.time(),
                "price": _live.get("price", {}).get("last") if _live.get("price") else None,
                "delta": _live.get("delta"),
                "volume_total": (_live.get("volume", {}) or {}).get("total"),
                "poc": _fp.get("poc"),
                "va_high": _fp.get("value_area_high"),
                "va_low": _fp.get("value_area_low"),
                "top_levels": _fp.get("top_levels", []),
                "dom_imbalance": (_live.get("dom", {}) or {}).get("imbalance_pct"),
                "tape_buy_vol": _tape.get("buy_vol"),
                "tape_sell_vol": _tape.get("sell_vol"),
                "cvd": _cvd_d.get("cvd"),
                "cvd_bars": _cvd_d.get("bars"),
                "cvd_bar_deltas": _cvd_d.get("bar_deltas", []),
                "value_area": (ctx.get("value_area", {}) if isinstance(ctx, dict) else {}),
                "expected_move": (ctx.get("expected_move", {}) if isinstance(ctx, dict) else {}),
                "bias_score": sig.bias_score,
                "data_coverage": getattr(sig, "data_coverage", 1.0),
            }
            _bt = os.getenv("BRIDGE_TOKEN", "")
            payload = json.dumps({"symbol": SYMBOL, "direction": direction.lower(),
                                  "entry_low": round(entry_low,4), "entry_high": round(entry_high,4),
                                  "sl": round(sl,4), "t1": round(t1,4), "t2": round(t2,4),
                                  "bias_score": sig.bias_score, "snapshot": snap_compact,
                                  "note": f"auto-plan live-anchored {live_price:.2f}"}).encode()
            hdrs = {"Content-Type": "application/json"}
            if _bt:
                hdrs["X-Bridge-Token"] = _bt
            _ur.urlopen(_ur.Request(f"{BASE}/calls", data=payload, headers=hdrs), timeout=3)
            lines.append(f"  ✅ Logged via POST /calls (snapshot-validated)")
        except Exception as _e:
            lines.append(f"  ⚠️ Not logged: {_e}")

# ── Footer: live timestamp + validated probability (Rule 3 / quant-research) ──
import datetime as _dt
live_ts = live.get("timestamp") if live and "timestamp" in live else None
if live_ts:
    # LiveExporter timestamp is ms since epoch
    try:
        dt = _dt.datetime.fromtimestamp(live_ts/1000, tz=_dt.timezone.utc).astimezone()
        age_s = int(__import__("time").time() - live_ts/1000)
        lines.append(f"\n<i>Live: {dt.strftime('%Y-%m-%d %H:%M:%S %Z')} (age {age_s}s) | Bar {live.get('bar_index','?')} | CVD {cvd.get('bars','?')} bars</i>")
    except Exception:
        pass
# Probability footer — must cite Rule 3 + stats
n_traded = stats.get("n_traded", 0) if isinstance(stats, dict) else 0
conf = stats.get("confidence", "insufficient") if isinstance(stats, dict) else "insufficient"
if n_traded >= 30 and conf in ("medium","high"):
    wr = stats.get("winrate"); pf = stats.get("profit_factor"); exp = stats.get("expectancy_r")
    lines.append(f"<i>Probability: validated — n={n_traded} winrate {wr:.1%} PF {pf} exp {exp:+.3f}R (Rule 3)</i>")
else:
    lines.append(f"<i>Probability: unvalidated — not backtested (Rule 3). n={n_traded} {conf}, need N≥30 (audit N≥185 for 60% @p<0.05). Use quant-research backtest before sizing.</i>")

resp = "\n".join(lines)
print(resp)
print("---END---")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID not set in .env — skipping send")
    sys.exit(0)

body = (f"chat_id={TELEGRAM_CHAT_ID}&text=" + urllib.parse.quote(resp) + "&parse_mode=HTML").encode()
req = urllib.request.Request(
    TELEGRAM_API,
    data=body, method="POST"
)
urllib.request.urlopen(req, timeout=5)
print("Sent to Telegram")
