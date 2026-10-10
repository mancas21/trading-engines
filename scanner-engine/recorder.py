"""Outcome recorder — the loop that makes this engine auditable.

Nothing in this engine is trustworthy until every verdict is scored against a
forward window and the rules are judged on measured expectancy. This module is
that loop. It deliberately encodes three defects found in this repo's sibling
engine so they cannot recur here.

1. `forward_return` IS STORED RAW.
   Direction adjustment happens in SQL at audit time, never at write time. A
   pre-adjusted column cannot be re-derived correctly once you discover the
   adjustment was wrong — which is exactly what happened downstream.

2. `direction = 0` MEANS NO DIRECTION.
   Not neutral. Not bearish. Those rows are RETAINED (they record a real
   observation, including which rules were UNEVALUABLE) but EXCLUDED from
   directional expectancy via `DIRECTED_RETURN_SQL`. In the sibling engine 63%
   of rows carried direction 0 and were averaged in as if short, which inverted
   its entire outcome gate.

3. AN UNCLOSED BAR CANNOT PRODUCE AN OUTCOME.
   The final row of any kline series is partially formed. Recording an outcome
   from it invents a signal and a forward return that never happened — the
   standard way a backtest manufactures edge that does not exist.

`horizon_bars` is per row, never global. A 12-bar horizon on a 1m bar is 12
minutes and on a 1h bar is 12 hours; a single global value makes expectancy
incomparable across timeframes, which was a documented flaw upstream.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
from contextlib import contextmanager

# Directed return, derived in SQL. direction = 0 yields NULL, which AVG ignores
# and a WHERE ... IS NOT NULL filter removes — the exclusion is structural, not
# a matter of remembering to skip it.
DIRECTED_RETURN_SQL = """
    SELECT pair,
           CASE WHEN direction > 0 THEN  forward_return
                WHEN direction < 0 THEN -forward_return
           END AS directed_return
      FROM verdicts
     WHERE resolved = 1
       AND forward_return IS NOT NULL
       AND direction != 0
     ORDER BY id
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 REAL    NOT NULL,
    pair               TEXT    NOT NULL,
    direction          INTEGER NOT NULL,      -- -1 / 0 / +1 ; 0 = NO DIRECTION
    venues_live        INTEGER NOT NULL,      -- how many venues were live
    rules_met          TEXT    NOT NULL,      -- JSON list
    rules_unevaluable  TEXT    NOT NULL,      -- JSON list, carried not discarded
    score              INTEGER NOT NULL DEFAULT 0,
    horizon_bars       INTEGER NOT NULL,      -- per row, never global
    fill_price         REAL,
    forward_return     REAL,                  -- RAW move; adjusted at audit time
    resolved           INTEGER NOT NULL DEFAULT 0,
    bar_closed         INTEGER NOT NULL DEFAULT 1,
    candidate          INTEGER NOT NULL DEFAULT 0,  -- hypothesis under test
    unscorable         INTEGER NOT NULL DEFAULT 0   -- horizon bar no longer exists
);

CREATE INDEX IF NOT EXISTS idx_verdicts_pair   ON verdicts(pair);
CREATE INDEX IF NOT EXISTS idx_verdicts_resolv ON verdicts(resolved, id);
CREATE INDEX IF NOT EXISTS idx_verdicts_dir    ON verdicts(direction, resolved);

CREATE TABLE IF NOT EXISTS recorder_meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def open_db(path: str | pathlib.Path) -> sqlite3.Connection:
    """Open (creating if needed) the scanner database. Idempotent.

    `check_same_thread=False` plus a module lock. The HTTP surface runs on
    ThreadingHTTPServer worker threads while the scan loop owns the writer, and
    sqlite3 refuses a connection used from a thread other than the one that
    created it — measured: /health raised
    "SQLite objects created in a thread can only be used in that same thread"
    and returned an empty body. The lock serializes access so the threading is
    safe rather than merely permitted.
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(p), check_same_thread=False)
    db.executescript(SCHEMA)
    _migrate(db)
    db.commit()
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    An engine that has been running must not require deleting its data because
    a schema field was added. Measured: the running scanner.db already held 336
    rows before `candidate` existed.
    """
    cols = {r[1] for r in db.execute("PRAGMA table_info(verdicts)")}
    if "candidate" not in cols:
        db.execute("ALTER TABLE verdicts "
                   "ADD COLUMN candidate INTEGER NOT NULL DEFAULT 0")
    if "unscorable" not in cols:
        db.execute("ALTER TABLE verdicts "
                   "ADD COLUMN unscorable INTEGER NOT NULL DEFAULT 0")


# Serializes every DB touch. A scan cycle writes while /health may read.
DB_LOCK = threading.RLock()


@contextmanager
def _guard(db: sqlite3.Connection):
    with DB_LOCK:
        yield db


def record_verdict(
    db: sqlite3.Connection,
    pair: str,
    ts: float,
    direction: int,
    forward_return: float | None,
    horizon_bars: int,
    fill_price: float | None,
    venues_live: int,
    rules_met: list[str],
    rules_unevaluable: list[str],
    score: int = 0,
    bar_closed: bool = True,
    candidate: bool | None = None,
) -> int:
    """Insert one observation. Returns its row id.

    Raises ValueError on an unclosed bar — see the module docstring, bug 3.

    `candidate` defaults to "did any rule fire MET". A candidate row carries a
    PROVISIONAL side and enters expectancy math; it cannot suppress, alert or
    gate. That restriction lives in `outcomes.promoted_direction`.
    """
    if not bar_closed:
        raise ValueError(
            "refusing to record an outcome from an in-progress bar: it would "
            "invent both a signal and a forward return"
        )
    if direction not in (-1, 0, 1):
        raise ValueError(f"direction must be -1/0/+1, got {direction!r}")
    if horizon_bars <= 0:
        raise ValueError(f"horizon_bars must be > 0, got {horizon_bars}")

    cand = 1 if (bool(rules_met) if candidate is None else candidate) else 0
    with _guard(db):
        cur = db.execute(
            """INSERT INTO verdicts
                 (ts, pair, direction, venues_live, rules_met, rules_unevaluable,
                  score, horizon_bars, fill_price, forward_return, resolved,
                  bar_closed, candidate)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, pair, int(direction), int(venues_live),
             json.dumps(list(rules_met)), json.dumps(list(rules_unevaluable)),
             int(score), int(horizon_bars), fill_price, forward_return,
             0, 1, cand),
        )
        db.commit()
        return int(cur.lastrowid)


def resolve(db: sqlite3.Connection, ids: list[int],
            return_by_pair: dict[str, float]) -> int:
    """Write the realized forward move for rows whose horizon has elapsed.

    `return_by_pair` maps pair -> RAW forward return (a plain fractional move,
    e.g. -0.015 for -1.5%). A pair absent from the map leaves its rows
    unresolved rather than writing a zero, because "no data yet" and "the move
    was exactly zero" are different facts.

    Returns the number of rows resolved.
    """
    n = 0
    with _guard(db):
        for rid in ids:
            row = db.execute("SELECT pair FROM verdicts WHERE id=?", (rid,)).fetchone()
            if row is None:
                continue
            pair = row[0]
            if pair not in return_by_pair:
                continue                  # stays unresolved; NOT zeroed
            db.execute(
                "UPDATE verdicts SET forward_return=?, resolved=1 WHERE id=?",
                (float(return_by_pair[pair]), rid),
            )
            n += 1
        db.commit()
    return n


def unresolved(db: sqlite3.Connection, limit: int = 500) -> list[tuple[int, str, float]]:
    """Rows awaiting resolution: (id, pair, ts)."""
    with _guard(db):
        return db.execute(
            "SELECT id, pair, ts FROM verdicts WHERE resolved=0 ORDER BY ts LIMIT ?",
            (limit,),
        ).fetchall()


def counts(db: sqlite3.Connection) -> dict:
    """Coverage summary for /health and the auditor's sanity checks."""
    with _guard(db):
        row = db.execute(
            """SELECT COUNT(*),
                      SUM(resolved),
                      SUM(CASE WHEN resolved=1 AND direction!=0 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN direction=0 THEN 1 ELSE 0 END),
                      MIN(ts), MAX(ts),
                      SUM(CASE WHEN resolved=1 AND direction!=0 AND candidate=1
                           THEN 1 ELSE 0 END),
                  SUM(CASE WHEN unscorable=1 THEN 1 ELSE 0 END)
                 FROM verdicts"""
        ).fetchone()
    total = row[0] or 0
    return {
        "rows_total": total,
        "rows_resolved": row[1] or 0,
        "rows_directed_resolved": row[2] or 0,
        "rows_non_directional": row[3] or 0,
        "first_ts": row[4],
        "last_ts": row[5],
        "rows_candidate_resolved": row[6] or 0,
        "rows_unscorable": row[7] or 0,
    }


if __name__ == "__main__":
    import sys
    import tempfile
    tmp = tempfile.mkdtemp()
    conn = open_db(f"{tmp}/scanner.db")
    import time as _t
    record_verdict(conn, "ETH/USD", _t.time(), 1, 0.01, 5, 100.0, 4, ["1"], [])
    record_verdict(conn, "NVDA/USD", _t.time(), 0, None, 5, 230.0, 3, [], ["2", "3"])
    print(json.dumps(counts(conn), indent=2))
    print("\ndirected rows (direction=0 excluded structurally):")
    for pair, dr in conn.execute(DIRECTED_RETURN_SQL):
        print(f"   {pair:12} {dr:+.6f}")