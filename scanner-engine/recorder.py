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

4. THE FILL VENUE MUST BE THE EXIT VENUE.
   A forward return is a price move *on one venue*. Recording the fill as a
   depth-weighted mid across venues and then scoring it against a single
   venue's bar close silently measures the cross-venue spread, not the signal.

   This was measured, not theorised. XAU's recorded fill was ~99.99% OKX
   (OKX quotes depth ~287k units against Binance's ~7), while the exit was
   Binance's close. OKX gold sat ~30bps above Binance, so every XAU row
   resolved to a near-identical negative return:

       XAU/USD  n=599  raw mean -0.003029 (-30.3 bps)  sd 0.000165
                negative in 599 of 599 rows

   599/599 the same sign with ~1.6bps of variance is not a distribution, it is
   a constant offset. The auditor reported it as the one pair with a
   significant loser (t=-2.80). `price_basis_ok` is the exclusion: rows whose
   fill venue is not knowable are retained but never enter expectancy math,
   exactly like `direction = 0`.

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

# A row may only enter expectancy math if its fill venue is known AND the
# writer asserted the basis was sound. Requiring `ref_venue IS NOT NULL` as
# well is deliberate: `price_basis_ok` has a DEFAULT of 1, so any writer that
# predates the column — including an OLD `scanner.py` still running in memory
# during a rolling upgrade — inserts rows that claim a trustworthy basis while
# recording no venue at all. Deriving the gate from the evidence rather than
# from the writer's assertion closes that hole.
PRICE_BASIS_SQL = "price_basis_ok = 1 AND ref_venue IS NOT NULL"

# Directed return, derived in SQL. direction = 0 yields NULL, which AVG ignores
# and a WHERE ... IS NOT NULL filter removes — the exclusion is structural, not
# a matter of remembering to skip it.
#
# `price_basis_ok` is the second structural exclusion, for a different
# reason: a row whose fill venue is not the exit venue does not measure a
# signal, it measures a venue spread. See module docstring, defect 4.
DIRECTED_RETURN_SQL = f"""
    SELECT pair,
           CASE WHEN direction > 0 THEN  forward_return
                WHEN direction < 0 THEN -forward_return
           END AS directed_return
      FROM verdicts
     WHERE resolved = 1
       AND forward_return IS NOT NULL
       AND direction != 0
       AND {PRICE_BASIS_SQL}
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
    unscorable         INTEGER NOT NULL DEFAULT 0,  -- horizon bar no longer exists
    ref_venue          TEXT,                  -- venue the FILL was taken from
    fill_spread_bps    REAL,                  -- that venue's own spread at fill time
    cross_basis_bps    REAL,                  -- cross-venue dispersion at fill time
    price_basis_ok     INTEGER NOT NULL DEFAULT 1,  -- fill venue == exit venue?
    exit_close_ms      INTEGER,               -- close time of the bar that served
                                              -- as the exit; the REALIZED horizon
                                              -- is exit_close_ms/1000 - ts
    exit_window_ms     INTEGER                -- the DECISION WINDOW this row
                                              -- belongs to: the first bar close
                                              -- at or after ts+horizon. One row
                                              -- per (pair, exit_window_ms) — the
                                              -- dedup key of the decision-point
                                              -- ledger (UNIQUE partial index).
);

CREATE TABLE IF NOT EXISTS recorder_meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

# Indexes are created SEPARATELY from the tables. An index on a column added by
# a later migration cannot be created until that column exists, so building them
# in one executescript against a legacy database fails with
# "no such column: price_basis_ok" before `_migrate` ever runs.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_verdicts_pair   ON verdicts(pair);
CREATE INDEX IF NOT EXISTS idx_verdicts_resolv ON verdicts(resolved, id);
CREATE INDEX IF NOT EXISTS idx_verdicts_dir    ON verdicts(direction, resolved);
CREATE INDEX IF NOT EXISTS idx_verdicts_basis  ON verdicts(price_basis_ok, candidate);
-- The decision-point ledger's dedup key. Partial (exit_window_ms IS NOT NULL)
-- so legacy rows — which have no window — can never conflict, and created
-- here rather than in SCHEMA because it references a migrated column.
CREATE UNIQUE INDEX IF NOT EXISTS idx_verdicts_window
    ON verdicts(pair, exit_window_ms) WHERE exit_window_ms IS NOT NULL;
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
    # indexes last: they may reference columns that `_migrate` just added
    db.executescript(SCHEMA_INDEXES)
    db.commit()
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    An engine that has been running must not require deleting its data because
    a schema field was added. Measured: the running scanner.db already held 336
    rows before `candidate` existed.

    The price-basis columns carry a migration decision that matters: pre-fix
    rows are backfilled with `price_basis_ok = 0`, NOT 1. Their fill venue is
    unrecoverable, so they cannot be honestly scored and must not enter the
    expectancy math. They are kept, not deleted — "we do not know how this row
    was measured" is a fact worth retaining.
    """
    cols = {r[1] for r in db.execute("PRAGMA table_info(verdicts)")}
    added = False
    for name, decl in (
        ("candidate", "INTEGER NOT NULL DEFAULT 0"),
        ("unscorable", "INTEGER NOT NULL DEFAULT 0"),
        ("ref_venue", "TEXT"),
        ("fill_spread_bps", "REAL"),
        ("cross_basis_bps", "REAL"),
        ("price_basis_ok", "INTEGER NOT NULL DEFAULT 1"),
        ("exit_close_ms", "INTEGER"),
        ("exit_window_ms", "INTEGER"),
    ):
        if name in cols:
            continue
        db.execute(f"ALTER TABLE verdicts ADD COLUMN {name} {decl}")
        added = True

    if added and "price_basis_ok" not in cols:
        # Pre-fix rows: fill came from a cross-venue weighted mid, exit from a
        # single venue. Unscorable in principle, so excluded from the math.
        # `ref_venue IS NULL` already excludes them structurally; this makes the
        # flag honest for anything reading it directly.
        db.execute("UPDATE verdicts SET price_basis_ok = 0 WHERE ref_venue IS NULL")


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
    ref_venue: str | None = None,
    fill_spread_bps: float | None = None,
    cross_basis_bps: float | None = None,
    price_basis_ok: bool = True,
) -> int:
    """Insert one observation. Returns its row id.

    Raises ValueError on an unclosed bar — see the module docstring, bug 3.

    `candidate` defaults to "did any rule fire MET". A candidate row carries a
    PROVISIONAL side and enters expectancy math; it cannot suppress, alert or
    gate. That restriction lives in `outcomes.promoted_direction`.

    `ref_venue` names the venue the fill was taken from and MUST be the venue
    the resolver scores the exit against. `price_basis_ok=False` marks a row
    whose fill/exit basis is not trustworthy; it is retained but excluded from
    expectancy, the same treatment `direction = 0` gets.
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
                  bar_closed, candidate, ref_venue, fill_spread_bps,
                  cross_basis_bps, price_basis_ok)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, pair, int(direction), int(venues_live),
             json.dumps(list(rules_met)), json.dumps(list(rules_unevaluable)),
             int(score), int(horizon_bars), fill_price, forward_return,
             0, 1, cand, ref_venue, fill_spread_bps, cross_basis_bps,
             1 if price_basis_ok else 0),
        )
        db.commit()
        return int(cur.lastrowid)


def upsert_verdict(
    db: sqlite3.Connection,
    pair: str,
    ts: float,
    direction: int,
    horizon_bars: int,
    exit_window_ms: int,
    fill_price: float | None,
    venues_live: int,
    rules_met: list[str],
    rules_unevaluable: list[str],
    score: int = 0,
    ref_venue: str | None = None,
    fill_spread_bps: float | None = None,
    cross_basis_bps: float | None = None,
    price_basis_ok: bool = True,
    candidate: bool | None = None,
) -> tuple[int, bool]:
    """The decision-point ledger's writer: one row per (pair, exit_window_ms).

    The scan loop polls every ~12s, but ~25 consecutive polls share ONE exit
    window (the first bar close at or after ts+horizon). Recording each poll
    was 25 rows of nearly the same observation — the overlap the auditor then
    has to patch with an effective-sample-size correction. This writer keeps
    the FIRST row born in a window (its `ts` is the decision's birth and never
    moves) and refreshes the STATE fields on every later poll, so the row the
    resolver scores carries the freshest reading taken before the exit bar
    closed.

    STATE (refreshed on later polls): direction, venues_live, rules_met,
    rules_unevaluable, score, candidate.
    EXECUTION (frozen at `ts`, NEVER refreshed): fill_price, ref_venue,
    fill_spread_bps, cross_basis_bps, price_basis_ok. The horizon and the exit
    bar are anchored at `ts`, so the entry price has to be too — refreshing the
    entry while the exit stays at `ts` truncates the measured horizon and
    attenuates any real edge. See the comment in the UPDATE below.

    Returns (row_id, created). `created=False` means an existing window row
    was updated. The unique partial index on (pair, exit_window_ms) is the
    structural guarantee: two rows for one window cannot exist, in this
    process or after a restart, because the lookup runs in the same locked
    transaction as the write.

    `exit_window_ms` MUST be computed the same way the resolver picks the exit
    bar (first bar close >= ts + horizon) — scanner.run_cycle derives it from
    the pair's bar size, so the dedup key and the realized exit can never
    disagree.
    """
    if direction not in (-1, 0, 1):
        raise ValueError(f"direction must be -1/0/+1, got {direction!r}")
    if horizon_bars <= 0:
        raise ValueError(f"horizon_bars must be > 0, got {horizon_bars}")
    if not exit_window_ms or int(exit_window_ms) <= 0:
        raise ValueError(f"exit_window_ms must be a positive epoch-ms bar close, "
                         f"got {exit_window_ms!r}")
    cand = 1 if (bool(rules_met) if candidate is None else candidate) else 0
    with _guard(db):
        row = db.execute(
            "SELECT id FROM verdicts WHERE pair=? AND exit_window_ms=?",
            (pair, int(exit_window_ms)),
        ).fetchone()
        if row is not None:
            rid = int(row[0])
            # 2026-10-15 — EL ANCLA DE ENTRADA NO SE REFRESCA.
            #
            # `ts` es el nacimiento de la decision y NUNCA se mueve (correcto:
            # es la clave del dedup y el ancla del horizonte — el resolutor
            # busca la vela de salida en `ts*1000 + horizon_bars*step_ms`).
            # Pero este UPDATE refrescaba `fill_price` en cada poll posterior:
            # la fila quedaba con la ENTRADA del ultimo poll y la SALIDA
            # anclada en `ts`. Como los polls van cada ~12s y la ventana dura
            # hasta ~5 min, el retorno medido cubria solo la parte FINAL del
            # horizonte: la fila decia un horizonte y medía otro mas corto.
            #
            # El sesgo NO es ruido. Si la regla predice algo, el movimiento
            # empieza al disparar; mover la entrada hacia adelante recorta
            # justo el trozo que la regla predijo, asi que ATENUA el edge
            # medido de forma sistematica. Es la clase de error que hace que
            # una regla con edge real parezca no tenerlo — y sobre esta tabla
            # se midieron y se pusieron en cuarentena las reglas 6 y 7.
            #
            # Y era INVISIBLE al auditor: `exit_close_ms` se graba bien
            # (anclado en ts), asi que el horizonte realizado que se reporta
            # parece correcto. El truncamiento esta en la ENTRADA, y la
            # entrada no se audita.
            #
            # Se congelan por tanto los campos de EJECUCION — el precio al que
            # la decision se habria ejecutado y la calidad de ese fill (venue,
            # spread, basis) — porque describen el instante `ts`. Se sigue
            # refrescando el ESTADO (reglas, venues_live, score, candidate),
            # que es lo que el docstring pide y lo unico que no lleva su propio
            # origen temporal: quien lo lee quiere la lectura mas fresca.
            db.execute(
                """UPDATE verdicts SET
                       direction=?, venues_live=?, rules_met=?, rules_unevaluable=?,
                       score=?, candidate=?
                     WHERE id=?""",
                (int(direction), int(venues_live),
                 json.dumps(list(rules_met)), json.dumps(list(rules_unevaluable)),
                 int(score), cand, rid),
            )
            db.commit()
            return rid, False
        cur = db.execute(
            """INSERT INTO verdicts
                 (ts, pair, direction, venues_live, rules_met, rules_unevaluable,
                  score, horizon_bars, fill_price, forward_return, resolved,
                  bar_closed, candidate, ref_venue, fill_spread_bps,
                  cross_basis_bps, price_basis_ok, exit_window_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, pair, int(direction), int(venues_live),
             json.dumps(list(rules_met)), json.dumps(list(rules_unevaluable)),
             int(score), int(horizon_bars), fill_price, None,
             0, 1, cand, ref_venue, fill_spread_bps, cross_basis_bps,
             1 if price_basis_ok else 0, int(exit_window_ms)),
        )
        db.commit()
        return int(cur.lastrowid), True


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
                      SUM(CASE WHEN unscorable=1 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN NOT (price_basis_ok = 1 AND ref_venue IS NOT NULL)
                               THEN 1 ELSE 0 END),
                      SUM(CASE WHEN resolved=1 AND direction!=0
                                AND price_basis_ok=1 AND ref_venue IS NOT NULL
                                AND candidate=1
                           THEN 1 ELSE 0 END)
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
        "rows_bad_price_basis": row[8] or 0,
        # the ONLY population any expectancy number may be computed from
        "rows_auditable": row[9] or 0,
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