#!/usr/bin/env python3
"""Skill-format live read: assembles every section of the quant-crypto-analysis format from the
current live stack (bridge :8000 + QAE :8081 + Binance klines). Read-only."""
import json
import math
import os
import time
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
B = "http://127.0.0.1:8000"
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "SUIUSDT", "HYPEUSDT",
        "SPCXUSDT", "NVDAUSDT", "XAUUSDT"]
FAPI = "https://fapi.binance.com/fapi/v1/klines"


def get(u, t=25):
    with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "live-read/1.0"}),
                               timeout=t) as r:
        return json.loads(r.read().decode())


_KL_CACHE = {}
_SNAP_CACHE = {}


def klines(sym, iv, lim=400):
    """Klines from the snapshot fetch_live already made, not a second network round-trip.

    Fix LAG-1: this re-pulled all 27 kline sets (9 symbols x 15m/1h/4h) from Binance even
    though fetch_live had just written them to raw/live_snapshot.json. That doubled the
    card's runtime and pushed the ATAS sample's apparent age out by the same amount.
    SNAP is loaded below this function on first use; falls back to a live fetch if the
    snapshot is missing that series.
    """
    snap = _SNAP_CACHE.get("binance")
    if snap is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "raw", "live_snapshot.json"), encoding="utf-8") as f:
                _SNAP_CACHE["binance"] = json.load(f).get("binance") or {}
        except Exception:  # noqa: BLE001
            _SNAP_CACHE["binance"] = {}
        snap = _SNAP_CACHE["binance"]
    rows = (snap.get(sym) or {}).get(iv)
    if not rows or not isinstance(rows, list):
        rows = get(f"{FAPI}?symbol={sym}&interval={iv}&limit={lim}")
    if not rows or not isinstance(rows, list):
        return None
    c = np.array([float(r[4]) for r in rows])
    h = np.array([float(r[2]) for r in rows])
    l = np.array([float(r[3]) for r in rows])
    v = np.array([float(r[5]) for r in rows])
    tbv = np.array([float(r[9]) for r in rows])
    return {"c": c, "h": h, "l": l, "v": v, "d": 2 * tbv - v}


def ema(x, n):
    a = 2.0 / (n + 1)
    out = x[0]
    for v in x[1:]:
        out = a * v + (1 - a) * out
    return out


def rsi(c, n=14):
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = float(np.mean(up[-n:]))
    ad = float(np.mean(dn[-n:]))
    return 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)


def atr(k, n=14):
    h, l, c = k["h"], k["l"], k["c"]
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    return float(np.mean(tr[-n:]))


def atr_pctile(k, n=90):
    h, l, c = k["h"], k["l"], k["c"]
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    if len(tr) < n + 5:
        return None
    a = np.convolve(tr, np.ones(14) / 14, "valid")
    cur = a[-1]
    return round(100.0 * float(np.mean(a[-n:] <= cur)), 1)


def corr_stats(closes_by_sym, n_ret=200):
    """(pc1_pct, eff_breadth) from the basket's 15m log-return correlation matrix.

    Replaces the hardcoded 60.4 / 1.65 (P3, 2026-09-19). pc1_pct is the share of
    variance the first principal component explains; eff_breadth is the
    participation ratio (sum λ)²/sum(λ²) — the effective number of independent
    bets. Returns (None, None) if there is not enough clean data.
    """
    try:
        syms = sorted(closes_by_sym)
        rets = []
        for s in syms:
            c = closes_by_sym[s]
            if c is None or len(c) < n_ret + 1:
                return None, None
            rets.append(np.diff(np.log(c[-(n_ret + 1):])))
        R = np.array(rets)                                   # (n_sym, n_ret)
        sd = R.std(axis=1, keepdims=True)
        if np.any(sd <= 0):
            return None, None
        R = (R - R.mean(axis=1, keepdims=True)) / sd
        ev = np.linalg.eigvalsh(np.corrcoef(R))
        ev = ev[ev > 0]
        if ev.size == 0 or ev.sum() <= 0:
            return None, None
        pc1 = round(100.0 * float(ev[-1] / ev.sum()), 1)
        breadth = round(float(ev.sum() ** 2 / (ev ** 2).sum()), 2)
        return pc1, breadth
    except Exception:  # noqa: BLE001
        return None, None


now = time.time()
out = {"ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "multi_tf": {}, "flow": {},
       "walls": {}, "tape": None, "quant": {}, "rules": {}, "risk": {}, "health": {}}

# ---------------- services
# ---------------- data: one pull, read from disk (raw/live_snapshot.json) so the bridge is
# not hammered with a dozen sequential requests, then Binance klines fetched directly.
SNAP = json.load(open(os.path.join(HERE, "raw", "live_snapshot.json"), encoding="utf-8"))
BR = SNAP["bridge"]
h1 = BR["health"]
hd = BR["health/detailed"]
ctx = {s: (BR["context"].get(s) or {}) for s in SYMS}
dom = BR["dom"]
tcvd = BR["taker-cvd"]
der = BR["derivatives"]
quant = BR["quant"]
ind = BR["indicators"]
atr_cvd = BR["cvd"]
st = BR["calls/stats"]
blk = BR["blackout/status"]
qae = SNAP["qae"]["health"]
out["snapshot_as_of"] = SNAP["pulled_at_iso"]

# ---------------- degraded-feed report
# Every failed fetch used to arrive as a silent None, so a dead feed looked exactly like a
# neutral reading (e.g. "L/S n/a" vs a real 2.18). Collect the reasons so the card can say
# what is missing and why.
def _err(v, label=""):
    if isinstance(v, dict) and "__error__" in v:
        body = (v.get("__body__") or "").strip()[:60]
        return f"{label}: {v['__error__']}" + (f" {body}" if body else "")
    return None


degraded = []
for _ep, _v in BR.items():
    if _ep in ("context", "state_by_symbol"):
        continue
    _r = _err(_v, f"bridge /{_ep}")
    if _r:
        degraded.append(_r)
for _s, _v in (BR.get("context") or {}).items():
    _r = _err(_v, f"bridge /context/{_s}")
    if _r:
        degraded.append(_r)
for _ep, _v in (SNAP.get("qae") or {}).items():
    if _err(_v):
        # one consolidated line, added below if the process check says the engine is up
        _qae_health_failed = True
# A dead :8081 does NOT mean the engine is down. The health server is a diagnostic
# thread; the engine keeps running (and keeps alerting) without it — on 2026-09-27 the
# health thread had been failing since 17:37 with "Unable to configure formatter
# 'default'" while the engine emitted TG alerts normally. Only claim the engine is down
# if its LOG also says it stopped; otherwise say the health endpoint alone is down.
_qae_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qae_status.txt")
qae_proc = _qae_health_down = None
_qae_health_failed = any(_err(v) for v in (SNAP.get("qae") or {}).values())
try:
    import subprocess as _sp
    _proc_out = _sp.run(["powershell", "-NoProfile", "-Command",
                         "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*quant_alert*' -and $_.Name -like 'python*' } | Measure-Object).Count"],
                        capture_output=True, text=True, timeout=25).stdout.strip()
    qae_proc = int(_proc_out) if _proc_out.isdigit() else None   # NB: not `out` — that is the result dict
except Exception as _e:  # noqa: BLE001
    qae_proc = None
if qae_proc and _qae_health_failed:
    _qae_health_down = True
    degraded.append("QAE engine IS running but :8081/health does not answer "
                    "(health_server thread failed to bind — diagnostic endpoint only)")
elif not qae_proc and _qae_health_failed:
    degraded.append("QAE :8081 not answering and no engine process found — engine down")
_kl_err = [s for s, d in (SNAP.get("binance") or {}).items()
           if isinstance(d, dict) and any(isinstance(k, dict) and "__error__" in k for k in d.values())]
if _kl_err:
    degraded.append("binance klines: " + ", ".join(_kl_err))
if isinstance(der, dict) and der and all((v or {}).get("openInterest") is None
                                        for v in der.values() if isinstance(v, dict)):
    degraded.append("derivatives: reachable but every value null (funding/OI/L-S unusable)")
out["degraded"] = degraded

out["health"] = {"bridge": (h1.get("status") or ("down" if isinstance(h1, dict) and "__error__" in h1 else None)),
                 "qae": ("health-endpoint down (engine running)" if _qae_health_down
                         else ("down" if isinstance(qae, dict) and "__error__" in qae else qae.get("status"))),
                 "qae_processes": qae_proc,
                 "uptime_qae_h": round((qae.get("uptime_seconds") or 0) / 3600, 2),
                 "atas_ticker": ind.get("ticker"),
                 # Age the ATAS sample AGAINST THE MOMENT IT WAS PULLED, not against `now`.
                 # Fix LAG-1 (2026-09-27): this used `now - timestamp`, where `now` is taken
                 # after fetch_live has spent ~30s pulling 9 context blocks + 27 kline sets.
                 # The card therefore reported ITS OWN fetch duration as ATAS lag — 17.3s,
                 # 22.9s, 19.8s across recent cards — while the real age was under 1s
                 # (measured 0.1-1.0s over 12 polls, and -0.3s at the instant of the pull).
                 "atas_age_s": max(0.0, round(SNAP["pulled_at"] - (ind.get("timestamp") or 0) / 1000, 1)),
                 "atas_age_s_at_card": round(now - (ind.get("timestamp") or 0) / 1000, 1),
                 "live_indicators": hd["checks"]["live_indicators"],
                 "warnings": hd.get("warnings"), "blackout": blk.get("blackout"),
                 "next_event": (blk.get("next_event") or {}).get("name"),
                 "seconds_to_next": round(blk.get("seconds_to_next") or 0),
                 "ml_filter": (qae.get("checks", {}).get("ml_filter") or {})}

# ---------------- multi-TF structure + flow
_closes15 = {}
for s in SYMS:
    k15, k1h, k4h = klines(s, "15m"), klines(s, "1h"), klines(s, "4h")
    _closes15[s] = k15["c"]
    row = {}
    for name, k in (("15m", k15), ("1h", k1h), ("4h", k4h)):
        c = k["c"]
        e20, e50 = ema(c[-80:], 20), ema(c[-120:], 50)
        last = c[-1]
        trend = ("up" if e20 > e50 and last > e20 else
                 "down" if e20 < e50 and last < e20 else "range")
        row[name] = {"trend": trend, "rsi": round(rsi(c), 1),
                     "atr_pct": round(100 * atr(k) / last, 3),
                     "close_vs_e20_pct": round(100 * (last - e20) / e20, 3),
                     "atr_pctile": atr_pctile(k)}
    d16 = k15["d"][-16:]
    row["delta_4n"] = round(float(k15["d"][-4:].sum() / max(3 * np.mean(np.abs(d16)), 1e-9)), 2)
    row["delta_16n"] = round(float(d16.sum() / max(3 * np.mean(np.abs(d16)), 1e-9)), 2)
    row["cvd_slope_16"] = round(float(np.polyfit(np.arange(16), np.cumsum(k15["d"])[-16:], 1)[0]
                                     / max(np.mean(np.abs(k15["d"])), 1e-9)), 2)
    row["last"] = float(k15["c"][-1])
    row["vol_mult"] = round(float(k15["v"][-1] / max(np.median(k15["v"][-50:]), 1e-9)), 2)
    out["multi_tf"][s] = row

    d = dom.get(s.lower()) or {}
    t = tcvd.get(s) or {}
    dv = der.get(s) or {}
    cx = ctx.get(s) or {}
    va = cx.get("value_area") or {}
    vr = cx.get("volatility_regime") or {}
    em = cx.get("expected_move") or {}
    a = atr(k15)
    out["flow"][s] = {
        "mid": d.get("best_bid"), "spread_bps": round(10000 * (d.get("spread") or 0) / (d.get("best_bid") or 1), 2),
        "imb_raw": d.get("imbalance_pct"), "imb_ema": d.get("imbalance_pct_ema"),
        "imb_ema_n": d.get("imbalance_ema_samples"),
        "cvd_usd": t.get("cvd_usd"), "cvd_1m": t.get("cvd_usd_1m"), "cvd_5m": t.get("cvd_usd_5m"),
        "cvd_pos": t.get("range_pos_usd"), "cvd_age_s": t.get("age_s"),
        "funding_ann_pct": round((dv.get("fundingRate") or 0) * 3 * 365 * 100, 2),
        "ls_ratio": dv.get("longShortRatio"), "oi": dv.get("openInterest"),
        "va_loc": va.get("location"), "poc": va.get("poc"), "vah": va.get("vah"), "val": va.get("val"),
        "dist_poc_atr": None if not va.get("poc") else round((row["last"] - va["poc"]) / a, 2),
        "atr_pctile_ctx": (vr.get("atr_percentile_rank") or {}).get("value"),
        "vol_clustering": round((vr.get("clustering") or {}).get("value") or 0, 3),
        "exp_move_pct": em.get("value"), "exp_move_p90": em.get("p90"),
        "rng4h_vs_em": None if not em.get("value") else round(
            100 * (float(k15["h"][-16:].max()) - float(k15["l"][-16:].min())) / row["last"] / em["value"], 2),
    }
    out["walls"][s] = {"bid": [d.get("bid_wall_price"), d.get("bid_wall_vol"), d.get("bid_wall_persistent")],
                       "ask": [d.get("ask_wall_price"), d.get("ask_wall_vol"), d.get("ask_wall_persistent")],
                       "bid_depth_usd": round((d.get("total_bid_vol") or 0) * (d.get("best_bid") or 0)),
                       "ask_depth_usd": round((d.get("total_ask_vol") or 0) * (d.get("best_ask") or 0)),
                       "recent_trades_n": len(d.get("recent_trades") or [])}

# ---------------- footprint symbol (ATAS) : tape + rules 6/7
tk = (ind.get("ticker") or "").upper()
if tk:
    fp = ind.get("footprint") or {}
    lv = fp.get("top_levels") or []
    cv = atr_cvd.get(tk) or {}
    deltas = cv.get("bar_deltas") or []
    closed = deltas[:-1]
    cons_up = cons_dn = 0
    for x in reversed(closed):
        if x > 0:
            cons_up += 1
        else:
            break
    for x in reversed(closed):
        if x < 0:
            cons_dn += 1
        else:
            break
    sell = [(l["price"], round(100 * l["bid"] / l["volume"], 1)) for l in lv
            if l["volume"] > 0 and l["bid"] / l["volume"] > 0.70]
    buy = [(l["price"], round(100 * l["ask"] / l["volume"], 1)) for l in lv
           if l["volume"] > 0 and l["ask"] / l["volume"] > 0.70]
    tape = ind.get("tape") or {}
    px = (ind.get("price") or {}).get("last") or 0
    out["tape"] = {"ticker": tk, "bar": ind.get("bar_index"), "bar_time_utc": ind.get("time"),
                   "price": px, "delta": ind.get("delta"),
                   "volume": (ind.get("volume") or {}),
                   "poc": fp.get("poc"), "vah": fp.get("value_area_high"),
                   "val": fp.get("value_area_low"), "levels": len(lv),
                   "buy_levels": buy, "sell_levels": sell,
                   "tape": {k: v for k, v in tape.items() if not isinstance(v, list)},
                   "largest_usd": None if not tape.get("largest_trade") else round(tape["largest_trade"] * px),
                   "top_levels": [(l["price"], l["volume"], l["bid"], l["ask"], l["delta"], l["ticks"])
                                  for l in lv[:6]],
                   "cvd_deltas": deltas[-8:], "continuity": cv.get("continuity"),
                   "bars_since_gap": cv.get("bars_since_gap"),
                   "cons_up_closed": cons_up, "cons_dn_closed": cons_dn,
                   "rule2_multiple": None if not closed else round(
                       abs(ind.get("delta") or 0) / max(abs(x) for x in closed[-16:]), 2),
                   "dom_imbalance": (ind.get("dom") or {}).get("imbalance_pct")}

# ---------------- quant engine alerts by TF
q = {}
for sym, blob in (quant or {}).items():
    for al in (blob or {}).get("alerts", []):
        q.setdefault(al.get("timeframe") or "?", []).append(
            {"symbol": sym, "signal": al.get("signal_name"), "price": al.get("precio"),
             "color": al.get("color"), "age_min": round((now - (al.get("ts") or now)) / 60, 1)})
out["quant"] = q

# ---------------- scorecard + rule verdict for the ATAS symbol
try:
    R1 = json.load(open(os.path.join(HERE, "results.json"), encoding="utf-8"))
    out["scorecard"] = {r["symbol"]: {"bias": r["bias"], "zone": r["zone"]} for r in R1["scorecard"]}
    out["scorecard_meta"] = {"as_of": R1["pulled_at_iso"]}
except FileNotFoundError:
    out["scorecard"] = {}
R7 = None
try:
    R7 = json.load(open(os.path.join(HERE, "results7.json"), encoding="utf-8"))
except FileNotFoundError:
    pass
out["track_record"] = {k: st.get(k) for k in ("n_traded", "wins", "losses", "winrate", "expectancy_r_net",
                                              "expectancy_r_gross", "profit_factor_gross", "confidence",
                                              "cancelled", "open", "cost_bps")}
out["track_record"]["validated"] = {k: (st.get("validated") or {}).get(k)
                                    for k in ("n", "winrate", "expectancy_r_net", "by_direction")}
out["exit_variants"] = None if not R7 else R7["summary"]
_pc1, _breadth = corr_stats(_closes15)
out["risk"] = {"corr_pc1_pct": _pc1, "eff_breadth": _breadth, "qae_gates": {
    k: (v.get("metrics") if isinstance(v, dict) else None)
    for k, v in (qae.get("checks") or {}).items()}}

json.dump(out, open(os.path.join(HERE, "live_read.json"), "w", encoding="utf-8"), indent=1)
print(f"live_read.json written — {out['iso']}")
print("services:", out["health"]["bridge"], "/", out["health"]["qae"],
      "| ATAS on", out["health"]["atas_ticker"], f"({out['health']['atas_age_s']}s)")
print("blackout:", out["health"]["blackout"], "| next:", out["health"]["next_event"])
