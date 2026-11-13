"""Rellena huecos de datos leyendo de backtest-system (solo lectura) y completando
por API publica de Binance fapi en backtest_prompts100/data/. Nunca escribe en backtest-system."""
import json, time, urllib.request
from pathlib import Path

SRC = Path(r"C:\Users\Lenovo\Documents\backtest-system\data")
DST = Path(__file__).parent / "data"
DST.mkdir(exist_ok=True)


def fetch_klines(symbol, interval, start_ms):
    rows, cur = [], start_ms
    while True:
        url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}"
               f"&interval={interval}&startTime={cur}&limit=1500")
        with urllib.request.urlopen(url, timeout=15) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        rows += batch
        cur = batch[-1][0] + 1
        if len(batch) < 1500:
            break
        time.sleep(0.15)
    out = [{"open_ts": r[0], "open": r[1], "high": r[2], "low": r[3],
            "close": r[4], "volume": r[5]} for r in rows]
    return out


def main():
    import pandas as pd

    # JUP 1h y 15m no existen en el repo local -> API desde el listing del perp
    for tf, start in [("1h", 1704067200000), ("15m", 1704067200000)]:  # 2024-01-01
        fp = DST / f"JUPUSDT_{tf}.csv"
        if fp.exists():
            continue
        try:
            rows = fetch_klines("JUPUSDT", tf, start)
            pd.DataFrame(rows).to_csv(fp, index=False)
            print("JUP", tf, len(rows), "ok")
        except Exception as e:
            print("JUP", tf, "FALLO API:", e)

    # HYPE 1h del repo termina 2026-06-07 -> anadir cola
    base = SRC / "binance_klines_1h" / "HYPEUSDT_1h.csv"
    df = pd.read_csv(base)
    tail_fp = DST / "HYPEUSDT_1h_tail.csv"
    if not tail_fp.exists():
        try:
            rows = fetch_klines("HYPEUSDT", "1h", int(df["open_ts"].iloc[-1]) + 1)
            pd.DataFrame(rows).to_csv(tail_fp, index=False)
            print("HYPE 1h tail", len(rows), "ok")
        except Exception as e:
            print("HYPE 1h tail FALLO:", e)


if __name__ == "__main__":
    main()
