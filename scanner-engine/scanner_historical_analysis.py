#!/usr/bin/env python3
"""Scanner Historical Analysis — klines 1h de Binance fapi para todos los pares del scanner.

Calcula: drawdown desde máximo, subida posible al máximo, posición en rango,
volatilidad horaria, volumen ratio, y niveles de rebote (p25, media, p75, máximo).

Uso:
    python scanner_historical_analysis.py
"""

import urllib.request
import json
import datetime
import statistics
import math
import time
import sys

PAIRS_MAP = {
    "BTC/USD": "BTCUSDT",
    "ETH/USD": "ETHUSDT",
    "HYPE/USD": "HYPEUSDT",
    "JUP/USD": "JUPUSDT",
    "SOL/USD": "SOLUSDT",
    "SUI/USD": "SUIUSDT",
    "XRP/USD": "XRPUSDT",
    "1000PEPE/USD": "1000PEPEUSDT",
    "ARB/USD": "ARBUSDT",
    "NVDA/USD": "NVDAUSDT",
    "SPCX/USD": "SPCXUSDT",
    "XAU/USD": "XAUUSDT",
}


def get_binance_klines(symbol: str, interval: str = "1h", limit: int = 200):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def percentile(data, p):
    s = sorted(data)
    k = (len(s) - 1) * p / 100
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def analyze_pair(pair: str, symbol: str) -> dict:
    try:
        klines = get_binance_klines(symbol, "1h", 200)
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        times = [datetime.datetime.fromtimestamp(k[6] / 1000) for k in klines]

        current = closes[-1]
        max_px = max(highs)
        min_px = min(lows)
        max_idx = highs.index(max_px)
        min_idx = lows.index(min_px)

        dd_from_max = (max_px - current) / max_px * 100
        gain_to_max = (max_px - current) / current * 100
        drop_from_start = (closes[0] - current) / closes[0] * 100

        rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
        vol = statistics.stdev(rets) * 100 if len(rets) > 1 else 0

        pos_in_range = (current - min_px) / (max_px - min_px) * 100 if max_px != min_px else 50
        p25 = min_px + (max_px - min_px) * 0.25
        p75 = min_px + (max_px - min_px) * 0.75
        avg_vol = statistics.mean(volumes)
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0

        return {
            "pair": pair,
            "symbol": symbol,
            "current": current,
            "max": max_px,
            "min": min_px,
            "dd_from_max": dd_from_max,
            "gain_to_max": gain_to_max,
            "drop_from_start": drop_from_start,
            "vol_pct": vol,
            "pos_in_range": pos_in_range,
            "p25": p25,
            "p75": p75,
            "vol_ratio": vol_ratio,
            "n": len(closes),
            "max_time": times[max_idx],
            "min_time": times[min_idx],
            "error": None,
        }
    except Exception as e:
        return {
            "pair": pair,
            "symbol": symbol,
            "error": str(e)[:80],
        }


def main():
    print("=" * 80)
    print("SCANNER HISTORICAL ANALYSIS — Binance fapi 1h, últimas 200 velas")
    print(f"Ejecutado: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print("=" * 80)

    results = []
    for pair, symbol in PAIRS_MAP.items():
        r = analyze_pair(pair, symbol)
        results.append(r)
        time.sleep(0.3)

    results.sort(
        key=lambda x: x.get("dd_from_max", -999) if "error" not in x else -999,
        reverse=True,
    )

    print(
        f"{'Par':<14} {'Precio':>10} {'Máx':>10} {'Mín':>10} {'DD%':>7} {'Subida%':>8} "
        f"{'Rango%':>7} {'Vol%':>6} {'VolRatio':>8} {'Estado'}"
    )
    print("-" * 110)

    for r in results:
        if "error" in r and r["error"]:
            print(f"{r['pair']:<14} {'ERROR':>10} {'':>10} {'':>10} {'':>7} {'':>8} "
                  f"{'':>7} {'':>6} {'':>8} {r['error']}")
        else:
            pos = r["pos_in_range"]
            estado = (
                "SOBREVENTA" if pos < 30 else
                ("SOBRECOMPRA" if pos > 80 else
                 ("DÉBIL" if pos < 40 else ("FUERTE" if pos > 60 else "NEUTRAL")))
            )
            print(f"{r['pair']:<14} {r['current']:>10.4f} {r['max']:>10.4f} {r['min']:>10.4f} "
                  f"{r['dd_from_max']:>6.1f}% {r['gain_to_max']:>7.1f}% {r['pos_in_range']:>6.1f}% "
                  f"{r['vol_pct']:>5.2f}% {r['vol_ratio']:>7.2f}x {estado}")

    print()
    print("=== DETALLE POR PAR ===")
    for r in results:
        if "error" in r and r["error"]:
            print(f"\n{r['pair']} ({r['symbol']}): {r['error']}")
            continue
        print(f"\n{r['pair']} ({r['symbol']})")
        print(f"  Precio actual: {r['current']:.6f}")
        print(f"  Máximo: {r['max']:.6f} (en {r['max_time']})")
        print(f"  Mínimo: {r['min']:.6f} (en {r['min_time']})")
        print(f"  Drawdown desde máximo: {r['dd_from_max']:.2f}%")
        print(f"  Subida posible al máximo: {r['gain_to_max']:.2f}%")
        print(f"  Caída desde inicio período: {r['drop_from_start']:.2f}%")
        print(f"  Volatilidad horaria: {r['vol_pct']:.2f}%")
        print(f"  Posición en rango: {r['pos_in_range']:.1f}% (0=mín, 100=máx)")
        print(f"  p25: {r['p25']:.6f} | p75: {r['p75']:.6f}")
        print(f"  Vol ratio (última vs promedio): {r['vol_ratio']:.2f}x")

    print()
    print("=== RESUMEN EJECUTIVO ===")
    valid = [r for r in results if not r.get("error")]
    if valid:
        valid.sort(key=lambda x: x["dd_from_max"], reverse=True)
        print(f"Mayor caída: {valid[0]['pair']} ({valid[0]['dd_from_max']:.1f}% desde máximo)")
        print(f"Menor caída: {valid[-1]['pair']} ({valid[-1]['dd_from_max']:.1f}% desde máximo)")
        oversold = [r for r in valid if r["pos_in_range"] < 30]
        overbought = [r for r in valid if r["pos_in_range"] > 70]
        print(f"Pares en sobreventa (<30% rango): {[r['pair'] for r in oversold]}")
        print(f"Pares en sobrecompra (>70% rango): {[r['pair'] for r in overbought]}")
        errors = [r for r in results if r.get("error")]
        if errors:
            print(f"Pares con error: {[r['pair'] for r in errors]}")


if __name__ == "__main__":
    main()