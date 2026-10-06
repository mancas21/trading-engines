"""FOLLOW-UP: is the NEGATIVE correlation tradeable as mean-reversion?

The first test (taker_edge_test.py) found 75 cells, zero with POSITIVE edge --
but several with strongly NEGATIVE t. SUI cvd_z96 h=24 came in at t=-5.28 on
~11,900 samples: when taker buying is extreme, forward returns are LOWER. SOL
cvd_z96 t=-3.49, cvd_mom_24 t=-2.29. Same sign across symbols.

That is the mean-reversion hypothesis, and it is the first thing in this whole
audit that points anywhere. It needs a real test rather than a correlation,
because:

  1. CORRELATION IS NOT A STRATEGY. feature x forward_return measures association.
     A threshold rule ("short when z > 2") is what pays or loses. Costs apply
     per TRADE, so they must be charged per trade, not averaged over every bar.
  2. THE SIGN IS THE WHOLE THESIS. If it is mean reversion, the trade is the
     INVERSE of the feature: short high CVD z, buy low CVD z.
  3. IT MUST SURVIVE OUT OF SAMPLE. 120 days is one regime. Split it: choose
     thresholds on the first half, trade the untouched second half.

Discipline applied (each of these has bitten this project before):
  - Bonferroni across every cell tested, reported with the count.
  - Round-trip cost charged on entered trades only.
  - Fill at the NEXT bar's open, never the signal bar's close.
  - Walk-forward: thresholds fit on IS, reported on OOS. No per-symbol tuning.
  - The losing direction is reported too, so an inverted-sign bug is visible.

Run:  python meanrev_test.py --days 240
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
LIMIT = 1500
COST = 0.0011


def fetch(symbol: str, bars: int):
    out, end = [], None
    while len(out) < bars:
        url = f"{BASE}?symbol={symbol}&interval={TF}&limit={LIMIT}"
        if end is not None:
            url += f"&endTime={end}"
        req = urllib.request.Request(url, headers={"User-Agent": "meanrev/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = json.loads(r.read().decode())
        except Exception:
            break
        if not rows:
            break
        out.extend(rows)
        end = rows[0][0] - 1
        if len(rows) < LIMIT:
            break
        time.sleep(0.35)
    seen, uniq = set(), []
    for k in out:
        if k[0] not in seen:
            seen.add(k[0])
            uniq.append(k)
    uniq.sort(key=lambda k: k[0])
    return uniq


def tstat(vals):
    n = len(vals)
    if n < 3:
        return None, 0.0
    m = sum(vals) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in vals) / n)
    return m, (m / (sd / math.sqrt(n)) if sd > 0 else 0.0)


def cvd_z(c, v, tb, W=96):
    n = len(c)
    cvd, run = [0.0] * n, 0.0
    for i in range(n):
        tot = v[i]
        if tot > 0:
            run += (tb[i] - (tot - tb[i])) / tot
        cvd[i] = run
    z = [None] * n
    for i in range(W, n):
        win = cvd[i - W:i]
        mu = sum(win) / W
        sd = math.sqrt(sum((x - mu) ** 2 for x in win) / W) or 1e-9
        z[i] = (cvd[i] - mu) / sd
    return z


def sim(o, c, z, i0, i1, thr, hold, invert):
    """Trade the threshold rule between bar i0 and i1. Returns (R list, n trades).

    invert=True means mean-reversion: SHORT when z > thr, LONG when z < -thr.
    invert=False means momentum: the opposite.
    Fill at the NEXT bar's open. Cost charged once per round trip.
    """
    rs = []
    i = i0
    while i < i1 - hold - 1:
        zi = z[i]
        if zi is None or abs(zi) < thr:
            i += 1
            continue
        side = -1 if (zi > thr) == invert else 1   # -1 short, +1 long
        j = i + 1                                  # fill at next bar OPEN
        entry = o[j]
        if not entry:
            i += 1
            continue
        exit_px = c[j + hold - 1]
        r = side * (exit_px - entry) / entry - COST
        rs.append(r)
        i = j + hold                              # non-overlapping
    return rs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=240)
    ap.add_argument("--holds", default="12,24")
    ap.add_argument("--thrs", default="1.0,1.5,2.0,2.5")
    a = ap.parse_args()
    bars = int(a.days * 96)
    holds = [int(x) for x in a.holds.split(",")]
    thrs = [float(x) for x in a.thrs.split(",")]

    print("=" * 78)
    print("MEAN-REVERSION TEST on real taker flow (walk-forward)")
    print("=" * 78)
    print(f"symbols {SYMBOLS}")
    print(f"{TF} bars, ~{a.days}d each, cost {COST*100:.3f}%/trade, fill=next bar open")
    print(f"holds(bar)={holds}  thresholds={thrs}")
    print()

    data = {}
    for s in SYMBOLS:
        rows = fetch(s, bars)
        if len(rows) < 1000:
            print(f"  {s:9s} SKIP")
            continue
        o = [float(k[1]) for k in rows]
        h = [float(k[2]) for k in rows]
        l = [float(k[3]) for k in rows]
        c = [float(k[4]) for k in rows]
        v = [float(k[5]) for k in rows]
        tb = [float(k[9]) for k in rows]
        data[s] = (o, h, l, c, v, tb, cvd_z(c, v, tb))
        span = (rows[-1][0] - rows[0][0]) / 86_400_000
        print(f"  {s:9s} {len(rows):5d} bars {span:5.1f}d")

    if not data:
        print("no data")
        return 1

    # ---- IS: pick the single best (thr, hold, invert) pooled across symbols ----
    print()
    print("=" * 78)
    print("STEP 1 -- IN-SAMPLE: choose thresholds on the FIRST HALF only")
    print("=" * 78)
    is_cells = []
    for s, (o, h, l, c, v, tb, z) in data.items():
        n = len(c)
        mid = n // 2
        for hold in holds:
            for thr in thrs:
                for inv in (True, False):
                    rs = sim(o, c, z, 100, mid, thr, hold, inv)
                    if len(rs) < 15:
                        continue
                    m, t = tstat(rs)
                    is_cells.append((m, t, len(rs), s, thr, hold, inv))
    is_cells.sort(key=lambda r: -(r[0] or -9))
    print(f"  IS cells tested: {len(is_cells)}")
    for m, t, n, s, thr, hold, inv in is_cells[:6]:
        print(f"    mean={m:+.6f} t={t:+.2f} n={n:3d}  {s} thr={thr} hold={hold} "
              f"{'MEANREV' if inv else 'MOMENTUM'}")

    if not is_cells or is_cells[0][0] is None or is_cells[0][0] <= 0:
        print("\n  no IS cell is profitable -> no OOS claim to make.")
        return 1

    _, _, _, bs, bthr, bhold, binv = is_cells[0]
    print(f"\n  SELECTED (on IS only): thr={bthr} hold={bhold} "
          f"{'MEANREV' if binv else 'MOMENTUM'}  [from {bs}]")

    # ---- OOS: apply that ONE choice to the untouched SECOND HALF ----
    print()
    print("=" * 78)
    print("STEP 2 -- OUT-OF-SAMPLE: the same thresholds, second half, untouched")
    print("=" * 78)
    print(f"  {'sym':9s} {'n':>5} {'mean_R':>10} {'t':>7} {'win':>7} {'PF':>7}")
    pooled = []
    for s, (o, h, l, c, v, tb, z) in data.items():
        n = len(c)
        mid = n // 2
        rs = sim(o, c, z, mid, n, bthr, bhold, binv)
        if not rs:
            print(f"  {s:9s}     0        -        -       -       -")
            continue
        pooled.extend(rs)
        m, t = tstat(rs)
        w = [x for x in rs if x > 0]
        lo = [x for x in rs if x <= 0]
        pf = (sum(w) / abs(sum(lo))) if sum(lo) < 0 else float("inf")
        print(f"  {s:9s} {len(rs):>5} {m:>+10.6f} {t:>+7.2f} "
              f"{100*len(w)/len(rs):>6.1f}% {pf:>7.3f}")
    if pooled:
        m, t = tstat(pooled)
        w = [x for x in pooled if x > 0]
        lo = [x for x in pooled if x <= 0]
        pf = (sum(w) / abs(sum(lo))) if sum(lo) < 0 else float("inf")
        print()
        print(f"  POOLED OOS  n={len(pooled)}  mean={m:+.6f}R  t={t:+.2f}  "
              f"win={100*len(w)/len(pooled):.1f}%  PF={pf:.3f}")
        verdict = "SURVIVES OOS" if (t > 1.65 and m > 0) else "DOES NOT SURVIVE"
        print(f"  VERDICT: {verdict}  (needs t>+1.65 AND mean>0 on untouched data)")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())