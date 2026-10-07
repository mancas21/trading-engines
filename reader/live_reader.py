"""Fase 2 — lector multi-fuente. Un solo punto de entrada para las 4 fuentes.

POR QUÉ
=======
Antes cada lectura era un proceso distinto que podía omitir una fuente sin que
nadie lo notara. Este módulo es el ÚNICO camino, y su regla central es:

  UNA FUENTE AUSENTE SE DECLARA. NUNCA SE IMPUTA.

Un veredicto sin TradingView, o sin macro, o con un símbolo distinto al
objetivo ATAS, es un veredicto con menos evidencia — y eso tiene que estar
escrito en la salida, no assumptions silenciosas.

LAS 4 FUENTES
=============
  1. ATAS L2   footprint/DOM/tape — el ÚNICO símbolo con L2 real
  2. TradingView  estructura: EMA/RSI/ATR vía CDP :9222
  3. Binance   precio 24h + breadth de 9 símbolos
  4. Macro     contexto CMC/CoinGecko vía /health del motor

Las reglas 6/7 (Fase 3) solo usan ATAS, porque son reglas de order flow. Las
otras tres fuentes ACONTEXTUALIZAN y no pueden cambiar el veredicto — pero sí
pueden競 disqualificarlo si el símbolo no es el que pediste.

Uso:
  python live_reader.py --text
  python live_reader.py --json
  python live_reader.py --telegram     # sólo si el sesgo CAMBIÓ
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BRIDGE = "http://127.0.0.1:8000"
ENGINE = "http://127.0.0.1:8081"
CDP = "http://127.0.0.1:9222"
TV_SCRIPT = ROOT / "tradingview-bridge" / "tv_live.mjs"
STALE_S = 300.0


def _json(url: str, timeout: float = 20.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:70]}"}


# ── Fuente 1: ATAS (la única con L2 real) ──
def src_atas() -> dict:
    import verdict as V
    try:
        v = V.build_verdict()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:90]}"}
    h = _json(f"{BRIDGE}/health")
    tgt = h.get("atas_target") or {}
    out = {
        "ok": v.get("bias") != "UNAVAILABLE",
        "symbol": v.get("symbol"),
        "src_age_s": v.get("src_age_s"),
        "target": tgt.get("target"),
        "target_match": tgt.get("match"),
        "verdict": v,
    }
    if tgt.get("match") is False:
        out["warning"] = (f"el objetivo ATAS es {tgt.get('target')} pero el gráfico "
                          f"es {v.get('symbol')}: todo el L2 de abajo describe "
                          f"otro activo")
    return out


# ── Fuente 2: TradingView (estructura, NUNCA order flow) ──
def src_tradingview() -> dict:
    """Estructura desde el chart real.

    OJO: el nivel gratuito de TradingView NO tiene feed L2, así que cualquier
    'delta' o 'order flow' de un Pine es un proxy de OHLC. Aquí sólo se leen
    EMA/RSI/ATR/posición en rango — estructura. La orden de flujo viene de ATAS.
    """
    try:
        with urllib.request.urlopen(f"{CDP}/json/version", timeout=10) as r:
            ver = json.loads(r.read().decode())
        ua = ver.get("User-Agent", "")
        if "TVDesktop/" not in ua:
            return {"ok": False, "error": f"no es TradingView Desktop: {ua[:60]}"}
    except Exception as e:
        return {"ok": False, "error": f"CDP no disponible: {type(e).__name__}"}
    try:
        p = subprocess.run(["node", str(TV_SCRIPT)], capture_output=True,
                           text=True, timeout=120, cwd=str(TV_SCRIPT.parent))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "tv_live.mjs tardó más de 120s"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:60]}"}
    # tv_live.mjs emits WHITESPACE-PADDED labels ("close        1.4967"), not
    # "key: value" -- an earlier parser split on ":" and silently matched nothing.
    # Split on whitespace runs instead, and take the first token as the numeric.
    kv = {}
    for line in p.stdout.splitlines():
        s = line.strip()
        if not s or s in ("CONNECTED", "DONE"):
            continue
        if s.startswith("SYMBOL:"):
            rest = s[len("SYMBOL:"):].strip()
            kv["symbol"] = rest.split("|")[0].strip()
            if "TF:" in rest:
                kv["tf"] = rest.split("TF:")[1].strip()
            continue
        if s.startswith("BARS:"):
            kv["bars"] = s[len("BARS:"):].strip()
            continue
        if s.startswith("position in range"):
            kv["position_in_range"] = s.split("(")[0].replace(
                "position in range:", "").strip()
            continue
        # "EMA9 vs 21   BULLISH stack" is the STACK line, not an EMA value. It
        # must be matched BEFORE the generic key/value rule, or ema9 gets the
        # literal string "vs".
        if s.startswith("EMA9 vs 21"):
            kv["stack"] = s[len("EMA9 vs 21"):].strip()
            continue
        parts = s.split()
        if len(parts) >= 2 and parts[0] in (
                "close", "EMA9", "EMA21", "EMA50", "EMA200", "RSI14",
                "RSI6", "ATR14"):
            key = parts[0].lower()
            # "RSI14        62.96   RSI6 29.41" packs BOTH on one line.
            kv[key] = parts[1]
            kv[key + "_rest"] = " ".join(parts[2:])
            if key == "rsi14":
                rest = kv[key + "_rest"]
                if "RSI6" in rest:
                    kv["rsi6"] = rest.split("RSI6", 1)[1].strip()
            continue
    if not kv.get("close"):
        return {"ok": False, "error": "tv_live.mjs sin salida parseable",
                "raw": p.stdout[-300:]}
    kv["ok"] = True
    kv["note"] = ("estructura solamente: TradingView free no tiene L2, su "
                  "order-flow es proxy OHLC")
    return kv


# ── Fuente 3: Binance (precio 24h + breadth) ──
def src_binance() -> dict:
    out = {"ok": False}
    try:
        with urllib.request.urlopen(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ripple,solana"
                "&vs_currencies=usd&include_24hr_change=true", timeout=20) as r:
            out["prices"] = json.loads(r.read().decode())
        out["ok"] = True
    except Exception as e:
        out["price_error"] = f"{type(e).__name__}: {str(e)[:60]}"
    d = _json(f"{BRIDGE}/dom")
    ds = d.get("dom") or d
    if isinstance(ds, dict):
        rows = [(v.get("imbalance_pct"), k) for k, v in ds.items()
                if isinstance(v, dict) and isinstance(v.get("imbalance_pct"), (int, float))]
        rows.sort()
        out["breadth"] = {k: imb for imb, k in rows}
        out["negative"] = sum(1 for i, _ in rows if i < 0)
        out["total"] = len(rows)
    return out


# ── Fuente 4: Macro (contexto, vía el motor) ──
def src_macro() -> dict:
    h = _json(f"{ENGINE}/health")
    if h.get("_error"):
        return {"ok": False, "error": h["_error"]}
    mc = (h.get("checks", {}).get("macro_context", {}) or {}).get("metrics", {}) or {}
    return {"ok": bool(h.get("status")),
            "engine_status": h.get("status"),
            "web_fresh": mc.get("web_fresh"), "web_eligible": mc.get("web_eligible"),
            "symbols_cached": mc.get("symbols_cached")}


def read_all() -> dict:
    t0 = time.time()
    atas = src_atas()
    tv = src_tradingview()
    bn = src_binance()
    mc = src_macro()
    present = [n for n, s in (("atas", atas), ("tradingview", tv),
                              ("binance", bn), ("macro", mc)) if s.get("ok")]
    missing = [n for n, s in (("atas", atas), ("tradingview", tv),
                              ("binance", bn), ("macro", mc)) if not s.get("ok")]
    v = (atas.get("verdict") or {})
    # Si el veredicto se emitió sobre un símbolo que NO es el objetivo, no es
    # el veredicto pedido. Se marca en vez de colarse.
    scope_ok = atas.get("target_match") is not False
    return {
        "ts": t0, "elapsed_s": round(time.time() - t0, 2),
        "sources_present": present, "sources_missing": missing,
        "complete": not missing,
        "scope_ok": scope_ok,
        "bias": v.get("bias"), "score": (v.get("scorecard") or {}).get("score"),
        "probability": v.get("probability"),
        "atas": atas, "tradingview": tv, "binance": bn, "macro": mc,
    }


def render(r: dict) -> str:
    v = (r["atas"].get("verdict") or {})
    L = []
    L.append(f"LECTURA {time.strftime('%Y-%m-%d %H:%M:%S')}  "
             f"fuentes: {len(r['sources_present'])}/4"
             f"{'' if r['complete'] else '  FALTAN: ' + ','.join(r['sources_missing'])}")
    if not r["scope_ok"]:
        L.append(f"AVISO DE ALCANCE: {r['atas'].get('warning')}")
    L.append("")
    L.append(f"VEREDICTO  {r['bias']}   score {r['score']}/100   "
             f"probability: {r['probability']}")
    L.append(f"  {v.get('reason','')}")
    L.append("")
    a = r["atas"]
    if a.get("ok"):
        d = v.get("dom") or {}; c = v.get("cvd") or {}; fp = v.get("footprint") or {}
        L.append(f"ATAS L2 ({v.get('symbol')}, edad {v.get('src_age_s')}s)")
        L.append(f"  px {v.get('price_last')}  POC {v.get('poc')}  VA {v.get('value_area')}")
        L.append(f"  EMA {d.get('imbalance_pct_ema')}% (n={d.get('ema_n')}) vs "
                 f"instantáneo {d.get('imbalance_pct')}%")
        L.append(f"  footprint {fp.get('sell_heavy')} venta>70% / "
                 f"{fp.get('buy_heavy')} compra>70%  tape compra {v.get('tape_buy_pct')}%")
        L.append(f"  CVD {c.get('value')} rachas +{c.get('streak_up')}/-{c.get('streak_down')}"
                 f"  {'limpio' if v.get('cvd_clean') else 'CON HUECO'}")
    else:
        L.append(f"ATAS L2: NO DISPONIBLE — {a.get('error')}")
    L.append("")
    tv = r["tradingview"]
    if tv.get("ok"):
        L.append(f"TRADINGVIEW ({tv.get('symbol')} {tv.get('tf','?')}) — estructura, no order flow")
        L.append(f"  close {tv.get('close')}  EMA9 {tv.get('ema9')}  EMA21 {tv.get('ema21')}"
                 f"  EMA200 {tv.get('ema200')}")
        L.append(f"  RSI14 {tv.get('rsi14')}  RSI6 {tv.get('rsi6')}  {tv.get('stack')}")
        L.append(f"  posición en rango {tv.get('position_in_range')}")
    else:
        L.append(f"TRADINGVIEW: NO DISPONIBLE — {tv.get('error')}")
    L.append("")
    bn = r["binance"]
    if bn.get("prices"):
        px = " ".join(f"{k.upper()} ${v.get('usd')}({v.get('usd_24h_change',0):+.2f}%)"
                      for k, v in bn["prices"].items())
        L.append(f"BINANCE 24h: {px}")
    if bn.get("breadth"):
        L.append(f"  breadth: {bn['negative']}/{bn['total']} símbolos con imbalance negativo")
    if bn.get("price_error"):
        L.append(f"  precios: {bn['price_error']}")
    mc = r["macro"]
    L.append("")
    if mc.get("ok"):
        L.append(f"MACRO: motor {mc.get('engine_status')}  web_fresh "
                 f"{mc.get('web_fresh')}/{mc.get('web_eligible')}  "
                 f"symbols {mc.get('symbols_cached')}")
    else:
        L.append(f"MACRO: NO DISPONIBLE — {mc.get('error')}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    r = read_all()
    if a.out:
        Path(a.out).write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    if a.text:
        print(render(r))
    elif not a.out:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())