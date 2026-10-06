#!/usr/bin/env python3
"""Quant audit of the alert corpus: is there any tradeable edge, net of costs?

Re-run this as rows accumulate. The current corpus cannot demonstrate edge; the
point of this script is to make the verdict REPRODUCIBLE and to say plainly when
the sample becomes big enough to revisit.

Usage:
    "C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python312\\python.exe" audit_edge.py
    "C:\\...\\Python312\\python.exe" audit_edge.py --db data/alerts.db --min-n 15

Stdlib only. Read-only.
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "data", "alerts.db")

# Direction-adjusted return. direction == 0 means "no directional intent" and
# MUST be excluded, never folded in as -1 -- doing so flips the sign of every
# non-directional row (see STATUS.md 2026-10-06 and the qae-workflow skill).
DIR = "(CASE WHEN direction>0 THEN forward_return WHEN direction<0 THEN -forward_return END)"

# Binance USDT-M perp taker fee 0.05%/side + ~0.01% slippage round trip.
COST = 0.0011
# One-sided alpha at p<0.05.
T_CRIT = 1.65


def tstat(vals):
    """Population-sd t-statistic. Returns (mean, sd, t) or None."""
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in vals) / n)
    return m, sd, (m / (sd / math.sqrt(n)) if sd > 0 else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--min-n", type=int, default=15,
                    help="min rows per cell before reporting a t-stat")
    ap.add_argument("--target-n", type=int, default=5000,
                    help="directed rows wanted before the verdict is revisited")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        print(f"corpus not found: {a.db}")
        return 1
    con = sqlite3.connect(a.db)

    print("=" * 70)
    print("EDGE AUDIT -- quant-alert-engine corpus")
    print("=" * 70)

    total = con.execute("select count(*) from signal_outcomes").fetchone()[0]
    lo, hi = con.execute("select min(fired_ts),max(fired_ts) from signal_outcomes").fetchone()
    days = (hi - lo) / 86400.0
    print(f"corpus   : {a.db}")
    print(f"rows     : {total}")
    print(f"span     : {dt.datetime.fromtimestamp(lo):%Y-%m-%d} -> "
          f"{dt.datetime.fromtimestamp(hi):%Y-%m-%d}  ({days:.1f} days)")

    vals = [r[0] for r in con.execute(
        f"select {DIR} from signal_outcomes "
        "where direction!=0 and forward_return is not null") if r[0] is not None]
    n = len(vals)
    m, sd, t = tstat(vals)
    print(f"directed : {n}  ({100.0*n/total:.0f}% of rows; the rest have no direction)")
    print()
    print("--- THE NUMBER THAT SETTLES IT ---")
    print(f"  mean direction-adjusted return : {m:+.6f}  ({m*100:+.4f}%)")
    print(f"  round-trip cost                : {-COST:+.6f}  ({-COST*100:+.4f}%)")
    print(f"  NET per signal                 : {m-COST:+.6f}  ({(m-COST)*100:+.4f}%)")
    wins = sum(1 for x in vals if x > 0)
    print(f"  win rate                       : {100.0*wins/n:.1f}%  (break-even ~50.1% at 1:1)")
    print(f"  verdict                        : "
          f"{'POSITIVE -- review gates' if m > COST else 'NEGATIVE -- no tradeable edge'}")

    print("\n--- PER-CELL (n >= %d) ---" % a.min_n)
    print(f"  {'|t|':>6} {'t':>7} {'n':>5} {'mean':>12}  cell")
    cells = {}
    for st, sym in con.execute(
            "select distinct signal_type,symbol from signal_outcomes "
            "where direction!=0 and forward_return is not null"):
        vv = [r[0] for r in con.execute(
            f"select {DIR} from signal_outcomes where signal_type=? and symbol=? "
            "and direction!=0 and forward_return is not null", (st, sym)) if r[0] is not None]
        if len(vv) >= a.min_n:
            s = tstat(vv)
            if s:
                cells[(st, sym)] = (s[0], s[1], s[2], len(vv))
    ranked = sorted(cells.items(), key=lambda kv: -abs(kv[1][2]))
    for (st, sym), (mm, sdd, tt, cn) in ranked:
        print(f"  {abs(tt):6.2f} {tt:+7.2f} {cn:>5} {mm:+12.6f}  "
              f"{st} {sym.split('/')[0]}")
    pos = [(k, v) for k, v in cells.items() if v[2] > T_CRIT]
    neg = [(k, v) for k, v in cells.items() if v[2] < -T_CRIT]
    # Bonferroni-ish: normal quantile for 0.05/(2*ncells)
    ncell = len(cells)
    bonf = 2.4 if ncell >= 20 else 1.96
    print(f"\n  cells tested              : {ncell}")
    print(f"  positive t > +{T_CRIT}        : {len(pos)}")
    print(f"  negative t < -{T_CRIT}        : {len(neg)}")
    print(f"  expected false positives : ~{ncell*0.05:.1f} at |t|>{T_CRIT}")
    print(f"  Bonferroni threshold     : t={bonf}")
    if ranked:
        (bk, bv) = ranked[0]
        print(f"  strongest |t|            : {abs(bv[2]):.2f} "
              f"({'POSITIVE' if bv[2] > 0 else 'NEGATIVE'}) on {bk[0]} {bk[1].split('/')[0]}")
    if not pos:
        print("\n  VERDICT: no positive-expectancy cell. Nothing to optimise.")

    print("\n--- POWER ---")
    for edge in (0.0005, 0.001, 0.002):
        need = (1.96 * sd / edge) ** 2
        print(f"  detect {edge*100:.2f}% edge -> need n>={need:>7.0f}  "
              f"{'feasible now' if need <= n else 'UNDERPOWERED'}")
    rate = n / days if days else 0
    if rate > 0:
        eta = (a.target_n - n) / rate
        print(f"\n  directed rows {n}/{a.target_n}  "
              f"-> ~{eta:.0f} more days ({eta/30.4:.1f} months) at {rate:.0f}/day")
        if n < a.target_n:
            print("  VERDICT: sample too small to settle the question either way.")

    print("\n--- FOOTPRINT: fixed forward_bars makes expectancy incomparable ---")
    for fb, cnt in con.execute(
            "select forward_bars, count(*) from signal_outcomes group by forward_bars"):
        print(f"  forward_bars={fb}: {cnt} rows")

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())