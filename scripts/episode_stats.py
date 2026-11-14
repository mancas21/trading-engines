#!/usr/bin/env python3
"""episode_stats.py — estadística de episodios de caída: ¿hasta dónde puede llegar?

Compara el episodio ACTUAL del símbolo contra todos los episodios históricos
de su PROPIA serie (futuros Binance, velas diarias, todo el historial) de
profundidad parecida — drawdown vs el máximo de 7 días, con una tolerancia de
±`--width` puntos porcentuales — y reporta distribuciones condicionales:

  * contexto: dd 7d/30d/90d/ATH, retorno 24h, BTC 24h (idiosincrático o mercado)
  * forward 24h/72h/7d de los episodios gemelos (mediana / p25 / peor decil)
  * PISO del episodio (mínimo en 10 días) convertido a NIVELES de precio
  * recuperación a 95% del máx 7d dentro de 30 días (cuántas veces y en cuánto)
  * gauge de capitulación: funding + OI contra la firma histórica de piso
    (funding muy negativo + OI sangrando = combustible quemado)

Nada aquí predice: son distribuciones del propio histórico del activo. El
piso es estadístico, no un número mágico — la firma de capitulación es lo que
históricamente acompañó los suelos reales.

Uso:
  python scripts/episode_stats.py                     # sujeto = atas_target
  python scripts/episode_stats.py --symbol 1000PEPEUSDT
  python scripts/episode_stats.py --width 10          # cubeta más amplia
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import time
import urllib.request
from pathlib import Path

FAPI = "https://fapi.binance.com"
HERE = Path(__file__).resolve().parent


def get(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "episode-stats/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def daily(sym: str, limit: int = 1000) -> list[dict]:
    kl = get(f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=1d&limit={limit}")
    return [{"t": int(k[0]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])}
            for k in kl]


def med(xs):
    return st.median(xs) if xs else float("nan")


def pctile(xs, p):
    return sorted(xs)[min(int(len(xs) * p), len(xs) - 1)] if xs else float("nan")


def episode_floor_stats(sym: str, width: float = 7.5) -> dict | None:
    """Estadística del piso para los episodios gemelos del ACTUAL.

    Devuelve None si no hay historial suficiente o ningún episodio en cubeta.
    `dd_now` = drawdown actual vs máx 7d (%), negativo.
    """
    bars = daily(sym)
    if len(bars) < 60:
        return None
    now = bars[-1]["c"]
    h7_now = max(b["h"] for b in bars[-8:-1])
    dd_now = (now / h7_now - 1) * 100.0

    sig = []
    for i in range(8, len(bars) - 10):
        h7 = max(b["h"] for b in bars[i - 8:i])
        dd = (bars[i]["c"] / h7 - 1) * 100.0
        if dd_now - width <= dd <= dd_now + width:
            floor = min(b["l"] for b in bars[i:i + 10])
            extra = (floor / bars[i]["c"] - 1) * 100.0
            f72 = (bars[i + 3]["c"] / bars[i]["c"] - 1) * 100.0
            f7d = (bars[i + 7]["c"] / bars[i]["c"] - 1) * 100.0
            days = None
            for j in range(i + 1, min(i + 31, len(bars))):
                if bars[j]["c"] >= h7 * 0.95:
                    days = j - i
                    break
            sig.append({"f72": f72, "f7d": f7d, "extra": extra, "days": days})
    if not sig:
        return {"dd_now": dd_now, "n": 0, "now": now}
    ex = [s["extra"] for s in sig]
    rec = [s["days"] for s in sig if s["days"] is not None]
    lv = lambda p: now * (1 + p / 100.0)
    return {
        "dd_now": dd_now, "n": len(sig), "now": now,
        "f72_med": med([s["f72"] for s in sig]),
        "f7d_med": med([s["f7d"] for s in sig]),
        "extra_med": med(ex), "extra_p25": pctile(ex, 0.25),
        "extra_p10": pctile(ex, 0.10), "extra_worst": min(ex),
        "lvl_med": lv(med(ex)), "lvl_p25": lv(pctile(ex, 0.25)),
        "lvl_p10": lv(pctile(ex, 0.10)), "lvl_worst": lv(min(ex)),
        "recov_pct": len(rec) / len(sig), "recov_days_med": med(rec) if rec else None,
    }


def capitulation_gauge(sym: str) -> dict:
    """Firma de piso: funding extremo negativo + OI sangrando = combustible quemado."""
    pf = get(f"{FAPI}/fapi/v1/premiumIndex?symbol={sym}")
    fund = float(pf["lastFundingRate"]) * 100
    oi = get(f"{FAPI}/futures/data/openInterestHist?symbol={sym}&period=1h&limit=25")
    ois = [float(r["sumOpenInterest"]) for r in oi]
    oi24 = (ois[-1] / ois[0] - 1) * 100 if ois and ois[0] else 0.0
    if fund <= -0.05 and oi24 < -2:
        verdict = "FIRMA DE SUELO: funding muy negativo + OI sangrando (combustible quemado)"
    elif fund > 0.005 and oi24 <= 0:
        verdict = "combustible CARGADO: longs pagando en caída (liquidaciones posibles)"
    elif fund < -0.05:
        verdict = "funding muy negativo pero OI aún no sangra — piso prematuro"
    else:
        verdict = "funding neutro/positivo sin capitulación — no hay firma de suelo"
    return {"funding": fund, "oi24": oi24, "verdict": verdict}


def render(sym: str, width: float) -> str:
    bars = daily(sym)
    now = bars[-1]["c"]
    h7 = max(b["h"] for b in bars[-8:-1])
    h30 = max(b["h"] for b in bars[-31:-1])
    h90 = max(b["h"] for b in bars[-91:-1]) if len(bars) >= 91 else h30
    hall = max(b["h"] for b in bars)
    r24 = (now / bars[-2]["c"] - 1) * 100

    L = [f"EPISODIO — {sym} · cierre {now:g}"]
    L.append(f"  dd vs max 7d {(now/h7-1)*100:+.1f}% | 30d {(now/h30-1)*100:+.1f}% | "
             f"90d {(now/h90-1)*100:+.1f}% | ATH {(now/hall-1)*100:+.1f}%")
    L.append(f"  retorno 24h {r24:+.1f}%")

    try:
        btc = daily("BTCUSDT", 5)
        rbtc = (btc[-1]["c"] / btc[-2]["c"] - 1) * 100
        rel = r24 - rbtc
        L.append(f"  BTC 24h {rbtc:+.1f}% → {'MERCADO' if abs(rel) <= 3 else 'IDIOSINCRÁTICO'} (rel {rel:+.1f}pp)")
    except Exception:  # noqa: BLE001
        pass

    stx = episode_floor_stats(sym, width) or {"n": 0}
    if stx.get("n"):
        L.append(f"  episodios gemelos (dd en [{stx['dd_now']-width:.0f},{stx['dd_now']+width:.0f}]%): n={stx['n']}")
        L.append(f"  forward 72h mediana {stx['f72_med']:+.1f}% | 7d {stx['f7d_med']:+.1f}%")
        L.append(f"  PISO adicional 10d: mediana {stx['extra_med']:+.1f}% → {stx['lvl_med']:g}")
        L.append(f"    p25 {stx['extra_p25']:+.1f}% → {stx['lvl_p25']:g} | "
                 f"peor decil {stx['extra_p10']:+.1f}% → {stx['lvl_p10']:g} | "
                 f"peor histórico {stx['extra_worst']:+.1f}% → {stx['lvl_worst']:g}")
        L.append(f"  recuperación 95% del máx en 30d: {stx['recov_pct']:.0%} de las veces"
                 + (f" (mediana {stx['recov_days_med']:.0f} días)" if stx["recov_days_med"] else ""))
    else:
        L.append("  sin episodios gemelos en cubeta — historial corto o profundidad única")
    try:
        g = capitulation_gauge(sym)
        L.append(f"  gauge capitulación: funding {g['funding']:+.4f}% · OI 24h {g['oi24']:+.1f}% → {g['verdict']}")
    except Exception as e:  # noqa: BLE001
        L.append(f"  gauge no disponible: {e}")
    L.append("  (distribuciones condicionales del propio histórico; el piso es "
             "estadístico, no un número mágico)")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default=None,
                    help="símbolo fapi (default: atas_target.json del bridge)")
    ap.add_argument("--width", type=float, default=7.5,
                    help="media-tolerancia de la cubeta de drawdown (pp)")
    a = ap.parse_args()

    sym = a.symbol
    if not sym:
        tf = HERE.parent / "atas-bridge" / "atas_target.json"
        try:
            sym = json.loads(tf.read_text(encoding="utf-8"))["symbol"]
        except Exception:  # noqa: BLE001
            print("sin --symbol y no pude leer atas_target.json")
            return 2
    print(render(sym.upper(), a.width))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
