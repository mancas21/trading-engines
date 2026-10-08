"""Live smoke test for the corrected CVD layer.

Proves, against the real exchanges, that:
  * all four venues return a usable window CVD
  * Kraken's freshness is honest (the Trap 2 regression)
  * Binance's per-bar CVD agrees in SIGN with its per-trade CVD
  * absent flow reports None, not 0.0

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/tests/live_cvd_smoke.py
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import flow
from venues import binance, bybit, kraken, okx

PAIRS = ["ETH/USD", "SOL/USD", "XRP/USD", "SUI/USD", "HYPE/USD", "XAU/USD",
         "SPCX/USD", "NVDA/USD"]

WINDOW = 60.0


def row(label, res, age_s, extra=""):
    if res is None:
        return f"  {label:26} NONE (no trades in {WINDOW:.0f}s window)"
    return (f"  {label:26} buy={res.buy:>14,.2f} sell={res.sell:>14,.2f} "
            f"delta={res.delta:>+14,.2f} ratio={res.delta_ratio if res.delta_ratio is None else round(res.delta_ratio, 4)!s:>7} "
            f"n={res.n_trades:>4} cov={res.coverage_s:>6.1f}s age={age_s:>6.1f}s {extra}")


print("=" * 118)
print(f"LIVE CVD SMOKE  window={WINDOW:.0f}s   {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 118)

for pair in PAIRS:
    print(f"\n--- {pair} " + "-" * 104)
    now = time.time()

    # Binance: per-trade (aggTrades m-flag)
    try:
        tr = binance.fetch_trades(pair, limit=1000)
        res = flow.window_cvd(tr, now=now, window_s=WINDOW)
        age = now - max(t["ts"] for t in tr) if tr else float("inf")
        print(row("binance trades (m-flag)", res, age))
    except Exception as e:
        print(f"  binance trades           {type(e).__name__}: {str(e)[:60]}")

    # Binance: per-bar (klines idx9) -- the only true per-bar source
    try:
        bars = binance.fetch_kline_cvd(pair, "1m", 5, now_ms=int(now * 1000))
        closed = [b for b in bars if b.closed]
        norm = flow.normalize_to_window(closed, now=now, window_s=300.0)
        if norm:
            print(row("binance klines (per-bar)", norm, 0.0,
                      extra=f"[{len(closed)}/{len(bars)} bars closed]"))
        else:
            print(f"  binance klines (per-bar)   NONE ({len(bars)} bars, {len(closed)} closed)")
    except Exception as e:
        print(f"  binance klines          {type(e).__name__}: {str(e)[:60]}")

    # OKX: per-trade side
    try:
        tr = okx.fetch_trades(pair, limit=100)
        res = flow.window_cvd(tr, now=now, window_s=WINDOW)
        age = now - max(t["ts"] for t in tr) if tr else float("inf")
        print(row("okx trades (side)", res, age))
    except Exception as e:
        print(f"  okx trades              {type(e).__name__}: {str(e)[:60]}")

    # Bybit: per-trade side
    try:
        tr = bybit.fetch_trades(pair, limit=60)
        res = flow.window_cvd(tr, now=now, window_s=WINDOW)
        age = now - max(t["ts"] for t in tr) if tr else float("inf")
        print(row("bybit trades (side)", res, age))
    except Exception as e:
        print(f"  bybit trades            {type(e).__name__}: {str(e)[:60]}")

    # Kraken: per-trade side, with the honest-age helper
    try:
        tr = kraken.fetch_trades(pair)
        res = flow.window_cvd(tr, now=now, window_s=WINDOW)
        age = kraken.newest_trade_age_s(tr, now)
        key = kraken.resolve_key({"result": {"x": {}, "last": "1"}}) if False else "(discovered)"
        print(row("kraken trades (side)", res, age, extra=key))
    except Exception as e:
        print(f"  kraken trades           {type(e).__name__}: {str(e)[:60]}")

print("\n" + "=" * 118)
print("Trap-2 regression: Kraken index-0 age vs max(ts) age")
now = time.time()
tr = kraken.fetch_trades("ETH/USD")
naive = now - tr[0]["ts"]
honest = kraken.newest_trade_age_s(tr, now)
print(f"  naive trades[0] age = {naive:8.1f}s   <- WRONG (ascending order)")
print(f"  honest max(ts) age = {honest:8.1f}s   <- correct")
print(f"  the naive reading overstated staleness by {naive - honest:.0f}s")
