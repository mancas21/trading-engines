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
  2. Estructura klines — EMA/RSI/ATR/posición computados de velas Binance
     (2026-11-14: sustituye a TradingView/CDP, que se midió SIN señal
     t≈0.13/-0.14/-0.92 y era la dependencia frágil del pipeline; ahora
     cubre TODOS los pares, no solo el chart)
  3. Binance   precio 24h + breadth
  4. Macro     contexto CMC/CoinGecko vía /health del motor

Las reglas 6/7 (Fase 3) solo usan ATAS, porque son reglas de order flow. Las
otras tres fuentes ACONTEXTUALIZAN y no pueden cambiar el veredicto — pero sí
pueden descalificarlo si el símbolo no es el que pediste.

Uso:
  python live_reader.py --text
  python live_reader.py --json
  python live_reader.py --telegram     # sólo si el sesgo CAMBIÓ
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BRIDGE = "http://127.0.0.1:8000"
# 2026-10-11: ENGINE (:8081, Quant Alert Engine) eliminado de este modulo.
# QAE es manual-only y no es fuente de nada. src_macro() va a CoinGecko directo.
STALE_S = 300.0
FAPI = "https://fapi.binance.com"
# 2026-11-14 — la cache de disco de TradingView (tv_structure_cache.json) queda
# obsoleta: la fuente 2 ya no lanza node/CDP (que podía tardar 120 s y colgó el
# notificador); ahora es un fetch de klines con cache en memoria de 60s.
TV_CACHE_S = 60.0


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


# ── Fuente 2: estructura desde klines de Binance ──
# 2026-11-14: ANTES era TradingView vía CDP (tv_live.mjs). Se midió sin señal
# (t=+0.13/-0.14/-0.92, ver structure_block) y era la dependencia frágil del
# pipeline (node+CDP colgó el notificador una vez). Las mismas métricas se
# computan de velas 5m de Binance para TODOS los pares — sin chart, sin CDP.

_KL_CACHE_S = 60.0
_KL_CACHE: dict = {}


def _target_symbol() -> str | None:
    try:
        raw = (ROOT / "atas-bridge" / "atas_target.json").read_text(encoding="utf-8")
        return json.loads(raw).get("symbol")
    except Exception:                               # noqa: BLE001
        return None


def _ema(vals: list[float], n: int) -> float:
    k = 2.0 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _wilder_rsi(closes: list[float], n: int) -> float:
    if len(closes) < n + 1:
        return float("nan")
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


def _wilder_atr(bars: list[dict], n: int) -> float:
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return float("nan")
    atr = sum(trs[:n]) / min(n, len(trs))
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def _klines_structure_read(symbol: str | None = None) -> dict:
    """EMA/RSI/ATR/posición desde velas 5m de Binance. MISMO schema de salida
    que el parser de tv_live.mjs para que structure_block() no cambie."""
    sym = (symbol or _target_symbol() or "").upper()
    if not sym:
        return {"ok": False, "error": "sin símbolo objetivo (atas_target.json ilegible)"}
    try:
        req = urllib.request.Request(
            f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=5m&limit=300",
            headers={"User-Agent": "live-reader/2.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read().decode())
    except Exception as e:                          # noqa: BLE001
        return {"ok": False, "error": f"klines no disponibles: {type(e).__name__}"}
    if not isinstance(raw, list) or len(raw) < 60:
        return {"ok": False, "error": f"klines insuficientes ({len(raw) if isinstance(raw, list) else 0})"}
    bars = [{"h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
            for k in raw]
    closes = [b["c"] for b in bars]
    close = closes[-1]
    ema9, ema21 = _ema(closes, 9), _ema(closes, 21)
    ema50 = _ema(closes[-250:], 50)
    ema200 = _ema(closes[-400:], 200) if len(closes) >= 200 else float("nan")
    if ema9 > ema21 > ema50:
        stack = "BULLISH stack"
    elif ema9 < ema21 < ema50:
        stack = "BEARISH stack"
    else:
        stack = "MIXED"
    hi = max(b["h"] for b in bars)
    lo = min(b["l"] for b in bars)
    pos = (close - lo) / (hi - lo) * 100.0 if hi > lo else 50.0
    vols = [b["v"] for b in bars]
    vol_ratio = vols[-1] / (sum(vols[-21:-1]) / 20.0) if len(vols) > 21 else float("nan")
    return {
        "ok": True,
        "symbol": sym,
        "tf": "5m",
        "close": close,
        "ema9": round(ema9, 8), "ema21": round(ema21, 8),
        "ema50": round(ema50, 8),
        "ema200": (round(ema200, 8) if ema200 == ema200 else None),
        "stack": stack,
        "rsi14": round(_wilder_rsi(closes, 14), 2),
        "rsi6": round(_wilder_rsi(closes, 6), 2),
        "atr14": round(_wilder_atr(bars, 14), 8),
        "position_in_range": f"{pos:.1f}%",
        "volume_ratio": round(vol_ratio, 3) if vol_ratio == vol_ratio else None,
        "source": "klines_binance",
        "note": ("estructura solamente (CONTEXTO, NO señal): mismas métricas que "
                 "daba tv_live.mjs, computadas de velas Binance para cualquier par"),
    }


def src_tradingview(max_age_s: float = TV_CACHE_S, force: bool = False,
                    symbol: str | None = None) -> dict:
    """Estructura del símbolo objetivo, CACHEADA (60s en memoria).

    Mantiene el nombre histórico para no romper a los callers; la fuente ya no
    es TradingView sino klines de Binance (ver _klines_structure_read)."""
    now = time.time()
    sym = (symbol or _target_symbol() or "").upper()
    if not sym:
        # una fuente ausente SE DECLARA — no se fetch-ea un simbolo vacio
        return {"ok": False, "read_ts": now,
                "error": "sin símbolo objetivo (atas_target.json ilegible)"}
    cached = _KL_CACHE.get(sym)
    if not force and isinstance(cached, dict) and             (now - float(cached.get("read_ts") or 0)) < max_age_s:
        return cached
    fresh = _klines_structure_read(sym)
    fresh["read_ts"] = now
    if fresh.get("ok"):
        _KL_CACHE[sym] = fresh
    return fresh


def structure_block(max_age_s: float = TV_CACHE_S) -> dict:
    """Bloque de estructura para el veredicto: CONTEXTO, nunca una señal.

    MEDIDO 2026-10-15 sobre 928 barras de 5m de JUPUSDT, mismo horizonte que el
    ledger (12 barras = 60 min): las señales de estructura ALCISTA no tienen
    edge — ruptura de pivote t=+0,13; EMA9>EMA21 con cierre encima t=-0,14;
    cruce de EMA al alza t=-0,92. El t=+4,13 del lado corto es la DERIVA del
    mercado, no predicción (el mismo error que puso la regla 6 en cuarentena).

    Por eso este bloque NO decide el sesgo (y la Regla 4 esta retirada). Existe
    para lo que SI es un defecto: que el silencio del motor fuera inauditable.
    Durante el rally de hoy el motor dijo 86 NEUTRAL seguidos y no habia forma de
    distinguir "lo mire y no hay señal" de "no lo mire".
    """
    tv = src_tradingview(max_age_s=max_age_s)
    if not tv.get("ok"):
        return {"ok": False, "error": tv.get("error"), "validated": False}
    return {
        "ok": True,
        "source": "klines_binance",
        "symbol": tv.get("symbol"),
        "tf": tv.get("tf"),
        "close": tv.get("close"),
        "ema9": tv.get("ema9"), "ema21": tv.get("ema21"),
        "ema50": tv.get("ema50"), "ema200": tv.get("ema200"),
        "stack": tv.get("stack"),
        "rsi14": tv.get("rsi14"), "rsi6": tv.get("rsi6"),
        "atr14": tv.get("atr14"),
        "position_in_range": tv.get("position_in_range"),
        "volume_ratio": tv.get("volume_ratio"),
        "read_age_s": round(time.time() - float(tv.get("read_ts") or 0), 1),
        "stale_error": tv.get("stale_error"),
        "validated": False,
        "note": ("CONTEXTO, NO señal: no decide el sesgo. Medido sin edge en el "
                 "lado alcista; existe para que el silencio del motor sea "
                 "auditable — ver lo que veia cuando dijo NEUTRAL."),
    }


# ── Fuente 3: precios 24h (CoinGecko) + breadth (el DOM del bridge) ──
#
# La fuente se llamaba `src_binance` y el render decía "BINANCE 24h", pero el
# endpoint era CoinGecko desde siempre. En un pipeline cuya regla central es
# que cada dato dice de dónde viene, una etiqueta que nombra un venue que NO
# sirvió el dato es el mismo defecto que un valor imputado — se corrige el
# nombre, no la proveniencia.
# Ticker de ATAS -> id de CoinGecko. Hace falta para que el contexto de 24 h
# describa el activo ANALIZADO. Antes la funcion pedia bitcoin,ripple,solana
# fijos: con el chart en JUPUSDT devolvia el cambio de SOL y nadie lo notaba,
# porque el numero se leia igual de plausible.
_CG_BY_TICKER = {
    "XRPUSDT": "ripple",
    "JUPUSDT": "jupiter-exchange-solana",
    "ARBUSDT": "arbitrum",
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
}


def src_prices_24h(symbol: str | None = None) -> dict:
    """Precio y cambio de 24 h del activo ANALIZADO (+ bitcoin como referencia).

    2026-10-15 — TIENE QUE RECIBIR EL SIMBOLO. Ped'ia 3 ids fijos y devolvia el
    cambio de SOL con el chart en JUPUSDT: el contexto describia otro activo.
    Ademas, el error iba en `price_error` mientras quien lee mira `error`, as�
    que un fallo se ve'ia como "fuente ausente" sin motivo.
    """
    out = {"ok": False, "source": "coingecko"}
    cg_id = _CG_BY_TICKER.get(str(symbol or "").upper())
    ids = "bitcoin" + ("," + cg_id if cg_id and cg_id != "bitcoin" else "")
    try:
        with urllib.request.urlopen(
                "https://api.coingecko.com/api/v3/simple/price?ids=" + ids +
                "&vs_currencies=usd&include_24hr_change=true", timeout=20) as r:
            out["prices"] = json.loads(r.read().decode())
        out["ok"] = True
        out["cg_id"] = cg_id
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:60]}"
        out["price_error"] = out["error"]
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
    """Macro REST directo. 2026-10-11: ya no consulta QAE (:8081).

    QAE paso a modo manual, asi que leer su /health no es una fuente de datos
    sino una medida de si el usuario abrio una app. Se sustituyo por
    CoinGecko directo, que es la unica pieza de macro que el pipeline de QAE
    aportaba y que se puede leer sin el.
    """
    try:
        d = _json("https://api.coingecko.com/api/v3/simple/price"
                  "?ids=ripple,bitcoin,ethereum&vs_currencies=usd"
                  "&include_24hr_change=true&include_24hr_vol=true", timeout=15.0)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}
    if d.get("_error"):
        return {"ok": False, "error": d["_error"]}
    rows = {k: {"usd": (v or {}).get("usd"),
                "change_24h": (v or {}).get("usd_24h_change"),
                "vol_24h": (v or {}).get("usd_24h_vol")}
            for k, v in d.items() if isinstance(v, dict)}
    return {"ok": bool(rows), "source": "coingecko", "coins": rows}


def read_all() -> dict:
    t0 = time.time()
    atas = src_atas()
    tv = src_tradingview()
    px = src_prices_24h(symbol=(atas.get("verdict") or {}).get("symbol"))
    mc = src_macro()
    present = [n for n, s in (("atas", atas), ("tradingview", tv),
                              ("prices_24h", px), ("macro", mc)) if s.get("ok")]
    missing = [n for n, s in (("atas", atas), ("tradingview", tv),
                              ("prices_24h", px), ("macro", mc)) if not s.get("ok")]
    v = (atas.get("verdict") or {})
    # Si el veredicto se emitió sobre un símbolo que NO es el objetivo, no es
    # el veredicto pedido. Se marca en vez de colarse.
    scope_ok = atas.get("target_match") is not False
    return {
        "ts": t0, "elapsed_s": round(time.time() - t0, 2),
        "sources_present": present, "sources_missing": missing,
        "complete": not missing,
        "scope_ok": scope_ok,
        "bias": v.get("bias"),
        "probability": v.get("probability"),
        "atas": atas, "tradingview": tv, "prices_24h": px, "macro": mc,
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
    bn = r["prices_24h"]
    if bn.get("prices"):
        px = " ".join(f"{k.upper()} ${v.get('usd')}({v.get('usd_24h_change',0):+.2f}%)"
                      for k, v in bn["prices"].items())
        # La fuente real es CoinGecko; la etiqueta lo dice. (Decía "BINANCE 24h"
        # mientras el endpoint era coingecko — una etiqueta que miente sobre su
        # proveniencia es el defecto que este módulo existe para evitar.)
        L.append(f"24H ({bn.get('source', 'coingecko')}): {px}")
    if bn.get("breadth"):
        L.append(f"  breadth: {bn['negative']}/{bn['total']} símbolos con imbalance negativo")
    if bn.get("price_error"):
        L.append(f"  precios: {bn['price_error']}")
    mc = r["macro"]
    L.append("")
    if mc.get("ok"):
        px = "  ".join(f"{k.upper()} ${v.get('usd')}({v.get('change_24h',0):+.2f}%)"
                       for k, v in mc.get("coins", {}).items())
        L.append(f"MACRO (coingecko directo): {px}")
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