"""
ml_backfill.py — Reconstruct the ML feature vector for every historical signal.

The engine only started persisting features in v2.8, so the existing track
record (1,683 resolved outcomes going back a week) has metadata but no
features. This script fetches Binance klines once per (symbol, timeframe),
replays each signal's bar through the SAME `features.py` extractor the live
engine uses, and writes the vector into `signal_features`.

Because both paths call `FeatureExtractor.vector()` over the same trailing
window of closed bars, a backfilled row and a live row are the same numbers —
which is what makes a model trained here usable live.

Also stamps `signal_outcomes.direction` for legacy rows (name-inferred, which is
what the resolver used when it computed their forward return, so the stored
sign stays correct and consistent going forward).

Usage:
    python ml_backfill.py                     # all symbols/timeframes in the DB
    python ml_backfill.py --symbol SOL/USDT:USDT --timeframe 5m
    python ml_backfill.py --dry-run           # compute, print, write nothing
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from features import DEFAULT_WINDOW, FEATURES, FeatureExtractor

FAPI = "https://fapi.binance.com/fapi/v1/klines"
TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}
MAX_LIMIT = 1500  # Binance USDⓈ-M futures klines cap


# ---------------------------------------------------------------------------
# klines
# ---------------------------------------------------------------------------

def to_binance_symbol(symbol: str) -> str:
    """'SOL/USDT:USDT' -> 'SOLUSDT'"""
    return symbol.split(":")[0].replace("/", "").upper()


def fetch_klines(symbol: str, tf: str, start_ms: int, end_ms: int,
                 pause: float = 0.12) -> List[list]:
    """Paginated OHLCV from Binance USDⓈ-M futures. Returns raw kline rows."""
    bsym = to_binance_symbol(symbol)
    step = TF_MS.get(tf, 300_000)
    out: List[list] = []
    cursor = start_ms
    guard = 0
    while cursor < end_ms and guard < 500:
        guard += 1
        limit = min(MAX_LIMIT, max(1, int((end_ms - cursor) / step) + 1))
        url = (f"{FAPI}?symbol={bsym}&interval={tf}"
               f"&startTime={int(cursor)}&endTime={int(end_ms)}&limit={limit}")
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                batch = json.loads(r.read())
        except Exception as e:
            print(f"    ! klines fetch failed ({bsym} {tf} @ {cursor}): {e}")
            break
        if not batch:
            break
        out.extend(batch)
        last_open = int(batch[-1][0])
        nxt = last_open + step
        if nxt <= cursor:            # no progress -> stop rather than spin
            break
        cursor = nxt
        if len(batch) < limit:
            break
        time.sleep(pause)
    return out


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------

def backfill_symbol_tf(conn: sqlite3.Connection, symbol: str, tf: str,
                       window: int, dry_run: bool = False,
                       verbose: bool = True) -> Dict[str, int]:
    """Rebuild features for every signal of one (symbol, timeframe)."""
    cur = conn.execute(
        """SELECT signal_id, signal_name, signal_type, direction, bar_key, fired_ts, price
           FROM signal_outcomes WHERE symbol = ? AND timeframe = ?
           ORDER BY fired_ts""", (symbol, tf))
    rows = cur.fetchall()
    if not rows:
        return {"rows": 0, "written": 0, "missed": 0}

    def bar_ms(bk: str) -> Optional[int]:
        try:
            return int(datetime.fromisoformat(bk.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return None

    stamps = [bar_ms(r[4]) for r in rows]
    stamps = [s for s in stamps if s]
    if not stamps:
        return {"rows": len(rows), "written": 0, "missed": len(rows)}

    step = TF_MS.get(tf, 300_000)
    # need `window` bars of history BEFORE the earliest signal bar
    start_ms = min(stamps) - (window + 5) * step
    end_ms = max(stamps) + 2 * step

    if verbose:
        print(f"  fetching {to_binance_symbol(symbol)} {tf} klines "
              f"({datetime.fromtimestamp(start_ms/1000, tz=timezone.utc):%Y-%m-%d %H:%M}"
              f" -> {datetime.fromtimestamp(end_ms/1000, tz=timezone.utc):%Y-%m-%d %H:%M})")
    kl = fetch_klines(symbol, tf, start_ms, end_ms)
    if verbose:
        print(f"  got {len(kl)} bars for {len(rows)} signals")
    if not kl:
        return {"rows": len(rows), "written": 0, "missed": len(rows)}

    ts = np.array([int(k[0]) for k in kl], dtype=np.int64)
    o = np.array([float(k[1]) for k in kl])
    h = np.array([float(k[2]) for k in kl])
    l = np.array([float(k[3]) for k in kl])
    c = np.array([float(k[4]) for k in kl])
    v = np.array([float(k[5]) for k in kl])
    pos = {int(t): i for i, t in enumerate(ts)}

    fx = FeatureExtractor(window=window)
    written = missed = 0
    for signal_id, name, stype, direction, bar_key, fired_ts, price in rows:
        ms = bar_ms(bar_key)
        idx = pos.get(ms) if ms else None
        if idx is None or idx + 1 < window:
            missed += 1
            continue
        vec = fx.vector(o, h, l, c, v, i=idx)
        if not vec:
            missed += 1
            continue
        vec = FeatureExtractor.add_time_features(vec, ms / 1000.0)
        d = int(direction or 0)
        if d == 0:  # legacy row: stamp the same inference the resolver used
            from quant_alert_engine import get_signal_direction
            d = int(get_signal_direction(name))
        if dry_run:
            written += 1
            continue
        conn.execute(
            """INSERT OR REPLACE INTO signal_features
               (signal_id, symbol, timeframe, signal_name, signal_type,
                direction, fired_ts, features_json) VALUES (?,?,?,?,?,?,?,?)""",
            (signal_id, symbol, tf, name, stype, d, float(fired_ts or 0.0),
             json.dumps(vec)))
        if int(direction or 0) == 0 and d != 0:
            conn.execute("UPDATE signal_outcomes SET direction = ? WHERE signal_id = ?",
                         (d, signal_id))
        written += 1
    if not dry_run:
        conn.commit()
    return {"rows": len(rows), "written": written, "missed": missed}


def ensure_schema(conn: sqlite3.Connection):
    """Create/migrate the tables this script needs, independent of engine start.

    The engine performs the same migration on boot; doing it here too means the
    backfill works on a DB that has not been opened by the new engine yet.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS signal_features
                    (signal_id TEXT PRIMARY KEY,
                     symbol TEXT, timeframe TEXT, signal_name TEXT,
                     signal_type TEXT, direction INTEGER,
                     fired_ts REAL, features_json TEXT)""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(signal_outcomes)")}
    if "direction" not in cols:
        conn.execute("ALTER TABLE signal_outcomes ADD COLUMN direction INTEGER DEFAULT 0")
        print("migrated signal_outcomes: added direction column")
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill ML features for historical signals")
    ap.add_argument("--db", default="data/alerts.db")
    ap.add_argument("--symbol", default=None, help="limit to one symbol")
    ap.add_argument("--timeframe", default=None, help="limit to one timeframe")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                    help="trailing bars per vector (must match ml.window)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_schema(conn)

    q = "SELECT DISTINCT symbol, timeframe FROM signal_outcomes WHERE resolved = 1"
    params: Tuple = ()
    if a.symbol:
        q += " AND symbol = ?"
        params += (a.symbol,)
    if a.timeframe:
        q += " AND timeframe = ?"
        params += (a.timeframe,)
    q += " ORDER BY symbol, timeframe"
    pairs = conn.execute(q, params).fetchall()
    if not pairs:
        print("no resolved signals matched")
        return 1

    t0 = time.time()
    totals = {"rows": 0, "written": 0, "missed": 0}
    for symbol, tf in pairs:
        print(f"[{symbol} {tf}]")
        st = backfill_symbol_tf(conn, symbol, tf, a.window, dry_run=a.dry_run)
        for k in totals:
            totals[k] += st[k]
        print(f"  -> rows={st['rows']} written={st['written']} missed={st['missed']}")

    n_feat = conn.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    joined = conn.execute(
        """SELECT COUNT(*) FROM signal_outcomes o
           JOIN signal_features f ON f.signal_id = o.signal_id
           WHERE o.resolved = 1""").fetchone()[0]
    print(f"\n{'DRY RUN — nothing written' if a.dry_run else 'written'}: "
          f"{totals['written']} vectors, {totals['missed']} missed, "
          f"{len(FEATURES)} features each, {time.time()-t0:.1f}s")
    print(f"signal_features rows now: {n_feat} | trainable (resolved+features): {joined}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
