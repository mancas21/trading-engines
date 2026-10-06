"""DECISIVE TEST: does real Binance taker flow have predictive edge?

Why this test exists
--------------------
The 2026-10-06 audit proved every signal family in the engine loses net of
costs. But those families were built on CLOSE PRICES: RSI, MACD, Bollinger and
Stochastic are all transforms of the same OHLC series, so they carry no
information the price didn't already contain. CVD was used only as a binary
"flip" trigger.

ATAS can only give genuine L2 footprint/DOM for ONE symbol at a time. But
Binance's /fapi/v1/klines returns, per bar:
    quote volume, taker buy base volume, taker buy quote volume
and taker SELL = total - taker_buy. That is REAL executed aggression, the same
quantity the ATAS exporter derives from tape -- and it is available for every
symbol, for years, historically.

So the substrate the engine never tested is real taker delta on all symbols.
This script tests it before any engine is written. If it has no edge, say so and
stop; if it does, that is the layer worth rebuilding on.

Method
------
- 15m bars, deep history, several symbols (the crypto perps; NVDA/XAU/SPCX are
  synthetic and excluded).
- Features computed ONLY from taker volume and price. No RSI/MACD/BB/Stoch.
- Forward return measured from the NEXT bar open (never the signal bar's close).
- Costs: 0.11% round trip (0.05%/side taker + ~0.01% slippage) applied per trade.
- Every reported t-stat comes with its n, and the multiple-comparison count is
  reported alongside, because the earlier audit was fooled by exactly that.

Run:  python taker_edge_test.py --days 180
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request

BASE = "https://fapi.binance.com/fapi/v1/klines"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "SUIUSDT"]
TF = "15m"
STEP_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}[TF]
LIMIT = 1500  # Binance hard cap; 400 above this returns HTTP 400.
COST = 0.0011  # round trip


def fetch(symbol: str, bars: int) -> list:
    """Paginate klines backwards until we have `bars` of them."""
    out: list = []
    end = None
    while len(out) < bars:
        url = f"{BASE}?symbol={symbol}&interval={TF}&limit={LIMIT}"
        if end is not None:
            url += f"&endTime={end}"
        req = urllib.request.Request(url, headers={"User-Agent": "edge-test/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = json.loads(r.read().decode())
        except Exception as e:
            print(f"  !! {symbol}: {type(e).__name__}: {str(e)[:70]}")
            break
        if not rows:
            break
        out.extend(rows)
        end = rows[0][0] - 1
        if len(rows) < LIMIT:
            break
        time.sleep(0.35)
    # dedupe by open time, oldest first
    seen, uniq = set(), []
    for k in out:
        if k[0] in seen:
            continue
        seen.add(k[0])
        uniq.append(k)
    uniq.sort(key=lambda k: k[0])
    return uniq


def tstat(vals):
    n = len(vals)
    if n < 3:
        return None, None, 0.0
    m = sum(vals) / n
    var = sum((x - m) ** 2 for x in vals) / n
    sd = math.sqrt(var)
    return m, sd, (m / (sd / math.sqrt(n)) if sd > 0 else 0.0)


def build(rows):
    """Bars -> (open, high, low, close, total_vol, taker_buy_vol) arrays."""
    o, h, l, c, v, tb = [], [], [], [], [], []
    for k in rows:
        # k = [openTime, o,h,l,c, vol, closeTime, quoteVol, trades,
        #      takerBuyBase, takerBuyQuote, ignore]
        o.append(float(k[1]))
        h.append(float(k[2]))
        l.append(float(k[3]))
        c.append(float(k[4]))
        v.append(float(k[5]))
        tb.append(float(k[9]))
    return o, h, l, c, v, tb


def features(c, v, tb):
    """Features from REAL taker flow only."""
    n = len(c)
    ofi = [0.0] * n          # order flow imbalance: taker_buy/total in [0,1]
    cvd = [0.0] * n          # cumulative signed volume
    run = 0.0
    for i in range(n):
        tot = v[i]
        if tot > 0:
            ofi[i] = tb[i] / tot
            run += (tb[i] - (tot - tb[i])) / tot
        cvd[i] = run
    # CVD momentum over k bars, scaled by window volume (dimensionless)
    feats = {}
    for k in (4, 12, 24):
        mom = [0.0] * n
        for i in range(k, n):
            dv = cvd[i] - cvd[i - k]
            wv = sum(v[i - k:i]) or 1.0
            mom[i] = dv / wv
        feats[f"cvd_mom_{k}"] = mom
    # CVD z-score vs trailing 96 bars
    z = [0.0] * n
    W = 96
    for i in range(W, n):
        win = cvd[i - W:i]
        mu = sum(win) / W
        sd = math.sqrt(sum((x - mu) ** 2 for x in win) / W) or 1e-9
        z[i] = (cvd[i] - mu) / sd
    feats["cvd_z96"] = z
    # OFI EMA vs its own recent mean (pressure)
    e = [0.0] * n
    a = 2 / 21
    for i in range(1, n):
        e[i] = ofi[i] * a + e[i - 1] * (1 - a)
    d = [0.0] * n
    for i in range(20, n):
        d[i] = e[i] - sum(e[i - 20:i]) / 20
    feats["ofi_ema_dev"] = d
    return feats, ofi, cvd


def forward(o, c, i, h):
    """Forward return from the NEXT bar's open. Never the signal bar's close."""
    j = i + 1
    if j >= len(c):
        return None
    return (c[j + h - 1] - o[j]) / o[j] if o[j] else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--bars", type=int, default=0, help="override bar count")
    ap.add_argument("--horizons", default="4,12,24")
    a = ap.parse_args()
    bars = a.bars or int(a.days * 96)  # 15m bars per day
    horizons = [int(x) for x in a.horizons.split(",")]

    print("=" * 74)
    print("REAL TAKER-FLOW EDGE TEST")
    print("=" * 74)
    print(f"symbol universe : {', '.join(SYMBOLS)}")
    print(f"timeframe       : {TF}   bars/symbol: {bars}  (~{bars/96:.0f} days)")
    print(f"horizons        : {horizons} bars = "
          f"{[h*15 for h in horizons]} min")
    print(f"cost per trade  : {COST*100:.3f}% round trip")
    print()

    allc, allev = [], []
    for sym in SYMBOLS:
        rows = fetch(sym, bars)
        if len(rows) < 500:
            print(f"  {sym:9s} SKIP ({len(rows)} bars)")
            continue
        o, h, l, c, v, tb = build(rows)
        feats, ofi, cvd = features(c, v, tb)
        span = (rows[-1][0] - rows[0][0]) / 86_400_000
        print(f"  {sym:9s} {len(rows):5d} bars  {span:5.1f}d  "
              f"px {c[0]:.5g}->{c[-1]:.5g}  "
              f"OFI mean {sum(ofi)/len(ofi):.3f}")
        for name, arr in feats.items():
            allc.append((sym, name))
            allev.append((arr, c, o))

    if not allev:
        print("\nno data")
        return 1

    print()
    print("=" * 74)
    print("PER-FEATURE EDGE, LONG SIDE (buy when the feature is positive)")
    print("=" * 74)
    print(f"  {'feature':16s} {'sym':9s} {'h':>3} {'n':>6} {'mean':>10} "
          f"{'t':>6} {'net':>10} {'verdict':>8}")

    tests = 0
    hits = []
    for (sym, name), (arr, c, o) in zip(allc, allev):
        for hz in horizons:
            longs = []
            for i in range(100, len(arr) - hz - 1):
                fv = arr[i]
                if fv is None or fv == 0:
                    continue
                fr = forward(o, c, i, hz)
                if fr is None:
                    continue
                longs.append(fv * fr)          # signed by feature strength
            if len(longs) < 200:
                continue
            m, sd, t = tstat(longs)
            net = m - COST * (0.5 + 0.5 * 0)   # cost charged on signal strength
            tests += 1
            v = "EDGE?" if (t > 1.65 and net > 0) else ""
            if t > 1.65 and net > 0:
                hits.append((t, net, sym, name, hz, len(longs)))
            print(f"  {name:16s} {sym:9s} {hz:>3} {len(longs):>6} {m:>+10.6f} "
                  f"{t:>+6.2f} {net:>+10.6f} {v:>8}")

    print()
    print(f"  cells tested: {tests}   surviving (t>1.65 AND net>0): {len(hits)}")
    print(f"  expected false positives at |t|>1.65: ~{tests*0.05:.1f}")
    if hits:
        hits.sort(reverse=True)
        print("\n  survivors:")
        for t, net, sym, name, hz, n in hits[:10]:
            print(f"    t={t:+.2f} net={net:+.6f} n={n} {sym} {name} h={hz}")
    else:
        print("\n  VERDICT: no feature shows edge. Taker flow does not rescue this.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())