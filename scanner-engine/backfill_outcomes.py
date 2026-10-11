"""Recover outcomes for rows that were retired unscorable.

WHY THIS IS POSSIBLE AT ALL
===========================
`mark_unscorable` retires a row when its horizon bar can no longer be fetched.
That was true of the old fetcher: every venue's `fetch_closed_bar` requests the
LAST N bars (`/klines?...&limit=50`) and then picks the one covering `at_ms`.
For a row more than ~50 minutes old the covering bar is simply not in that
window, so the fetch returns None and the row is retired.

It is NOT true of the venue. Exchange REST klines accept a `startTime`, so the
bar for any past instant is still retrievable. The rows were never unresolvable
-- the fetcher just never asked for them.

Measured on this database: 1,504 retired rows without a forward return, of which
298 are candidates (signals), spanning 41.2h back. All 8 pairs.

EFFICIENCY
==========
One HTTP call per ROW would be 1,504 calls. Instead this fetches the full kline
history for each (pair, interval) ONCE -- paginated with `startTime` and
`limit=1500` -- builds an in-memory index by close time, and resolves every row
for that pair from the index. That is ~2-4 calls per pair, so roughly 30 total.

ARITHMETIC
==========
Reuses `outcomes.forward_return` verbatim. No second implementation of the
return calculation exists anywhere in this file -- a backfill that computes the
number differently from the live resolver would silently make the backfilled
rows incomparable to the live ones, which is worse than not backfilling at all.

Usage:
    python backfill_outcomes.py --dry-run     # report only, no writes
    python backfill_outcomes.py               # apply
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import outcomes as OUT                      # noqa: E402  (forward_return)
from recorder import _guard as DB_GUARD     # noqa: E402  (same lock as the loop)
import venues.binance as BN                 # noqa: E402

DB_PATH = HERE / "data" / "scanner.db"

# Binance caps /klines at 1500 rows per request.
PAGE = 1500
# fapi allows 2400 request-weight/min and a klines call with limit<=1500 costs 10.
# A 0.35s gap keeps a full run comfortably inside the budget.
SLEEP_S = 0.35


def fetch_history(pair: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
    """Every 1m-equivalent kline for `pair` from start_ms to end_ms, oldest first.

    Paginates on `startTime` because a single request is capped at 1500 rows and
    a 41h window of 1m bars is ~2,472.
    """
    sym = BN.symbol_for(pair)
    out: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        path = (f"/fapi/v1/klines?symbol={sym}&interval={interval}"
                f"&startTime={cursor}&limit={PAGE}")
        rows = BN._get(path)
        if not rows:
            break
        out.extend(rows)
        last_open = int(rows[-1][0])
        if len(rows) < PAGE:
            break
        cursor = last_open + 1
        time.sleep(SLEEP_S)
    return out


def index_by_close(rows: list[list]) -> list[tuple[int, float]]:
    """[(close_ms, close_price)], sorted so a bisect finds the right bar."""
    pairs = [(int(r[6]), float(r[4])) for r in rows]
    pairs.sort()
    return pairs


def bar_at(idx: list[tuple[int, float]], at_ms: int):
    """The close price of the most recent bar whose close time is <= at_ms.

    Mirrors the live fetcher's selection exactly: the most recent CLOSED bar
    covering the instant, never the forming one.
    """
    lo, hi = 0, len(idx)
    best = None
    while lo < hi:
        mid = (lo + hi) // 2
        if idx[mid][0] <= at_ms:
            best = idx[mid]
            lo = mid + 1
        else:
            hi = mid
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="cap rows, 0 = all")
    ap.add_argument("--all", action="store_true",
                    help="recompute EVERY row, not just the ones missing a "
                         "return. Needed once for the 5m pairs, whose stored "
                         "returns were computed against the wrong horizon.")
    args = ap.parse_args()

    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.row_factory = sqlite3.Row

    if args.all:
        # Recompute unconditionally. The 5m pairs' stored returns came from a
        # resolver that multiplied horizon_bars by 60_000 while the pairs'
        # horizons are counted in 5-minute bars, so their numbers describe a
        # 60-second move and are not salvageable -- they must be recomputed, not
        # trusted. Rows whose fetch fails are left EXACTLY as they were.
        where = "resolved = 1 AND fill_price IS NOT NULL"
    else:
        where = ("unscorable = 1 AND forward_return IS NULL "
                 "AND fill_price IS NOT NULL")
    todo = db.execute(
        f"""SELECT id, pair, ts, horizon_bars, fill_price
              FROM verdicts WHERE {where} ORDER BY ts ASC"""
    ).fetchall()
    if args.limit:
        todo = todo[:args.limit]

    if not todo:
        print("nada que recuperar")
        return 0

    now = time.time()
    oldest = min(r["ts"] for r in todo)
    print(f"filas a recuperar : {len(todo)}")
    print(f"ventana           : {(now-oldest)/3600:.1f}h  "
          f"(desde {time.strftime('%Y-%m-%d %H:%M', time.localtime(oldest))})")
    print(f"pares             : {len({r['pair'] for r in todo})}")
    print(f"modo              : {'DRY RUN (no escribe)' if args.dry_run else 'APLICAR'}")
    print()

    # The earliest row needs its bar at (ts + horizon), which is always AFTER
    # ts, so the window starts just before the earliest row's own timestamp.
    # (An earlier version subtracted the whole age from the earliest row and
    # requested ~82h of bars per pair instead of ~41h.)
    start_ms = int(oldest * 1000) - 2 * 60_000
    end_ms = int(now * 1000)

    by_pair: dict[str, list] = {}
    for r in todo:
        by_pair.setdefault(r["pair"], []).append(r)

    # The pair's OWN bar size. `horizon_bars` counts BARS of this size, not
    # minutes: scanner.horizon_for holds a fixed 5-minute wall-clock horizon, so
    # a 1m pair carries horizon_bars=5 and a 5m pair carries horizon_bars=1.
    # Multiplying by a flat 60_000 (which this script did at first, reproducing
    # the live resolver's bug) makes the 5m pairs measure 60 seconds.
    def bar_ms(pair: str) -> int:
        try:
            import pairs as _P
            return OUT.BAR_MS[_P.bar_for(pair)]
        except Exception:                                       # noqa: BLE001
            return 60_000

    total_ok = total_miss = total_err = 0
    updates: list[tuple[float, int]] = []

    for pair, rows in sorted(by_pair.items()):
        try:
            import pairs as _P
            bar = _P.bar_for(pair)
        except Exception:                                       # noqa: BLE001
            bar = "1m"
        try:
            hist = fetch_history(pair, bar, start_ms, end_ms)
        except Exception as e:                                  # noqa: BLE001
            print(f"  {pair:10s} FETCH FALLO: {type(e).__name__}: {e}")
            total_err += len(rows)
            continue
        if not hist:
            print(f"  {pair:10s} sin historia (el venue no lo sirve)")
            total_err += len(rows)
            continue

        idx = index_by_close(hist)
        ok = miss = 0
        step = bar_ms(pair)
        for r in rows:
            at_ms = int(r["ts"] * 1000) + int(r["horizon_bars"]) * step
            hit = bar_at(idx, at_ms)
            if hit is None:
                miss += 1
                continue
            fwd = OUT.forward_return(r["fill_price"], hit[1])
            if fwd is None:
                miss += 1
                continue
            updates.append((fwd, r["id"]))
            ok += 1

        total_ok += ok
        total_miss += miss
        span_h = (max(r["ts"] for r in rows) - min(r["ts"] for r in rows)) / 3600
        print(f"  {pair:10s} barras={len(idx):>6}  recuperadas={ok:>4}  "
              f"sin barra={miss:>3}  ({span_h:.1f}h cubiertas)")

    print()
    print(f"  recuperadas : {total_ok}")
    print(f"  sin barra   : {total_miss}   (el venue no tiene esa barra)")
    print(f"  errores     : {total_err}")

    if args.dry_run:
        print("\nDRY RUN: nada escrito.")
        return 0

    # `unscorable` is cleared because the row now HAS a measured outcome; leaving
    # it set would keep a scored row out of every expectancy query.
    with DB_GUARD(db):
        db.executemany(
            "UPDATE verdicts SET forward_return=?, unscorable=0, resolved=1 "
            "WHERE id=?", updates)
        db.commit()
    print(f"\n  {len(updates)} filas actualizadas (forward_return + unscorable=0)")

    for label, sql in (
        ("unscorable restantes", "SELECT COUNT(*) FROM verdicts WHERE unscorable=1"),
        ("con retorno medido   ",
         "SELECT COUNT(*) FROM verdicts WHERE forward_return IS NOT NULL"),
        ("candidatos medidos   ",
         "SELECT COUNT(*) FROM verdicts WHERE candidate=1 "
         "AND forward_return IS NOT NULL"),
    ):
        print(f"  {label} : {db.execute(sql).fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())