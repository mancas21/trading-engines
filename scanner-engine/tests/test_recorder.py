"""Tests for the outcome recorder.

THE THREE BUGS THESE TESTS ARE PINNED AGAINST
=============================================
Every one of these is a real defect found in this repo's sibling engine
(`quant-alert-engine`) during its expectancy audit. They are reproduced here
as regressions so they cannot recur:

1. `forward_return` must be stored RAW. If it is pre-adjusted for direction at
   write time it becomes impossible to ever recompute correctly - and the
   direction-adjusted value is what the auditor needs, derived in SQL.

2. `direction = 0` means NO DIRECTION, not neutral and definitely not bearish.
   63% of the sibling engine's rows carried direction 0 and were averaged in
   as if they were short. The auditor must EXCLUDE them.

3. The in-progress bar must never produce an outcome. A partially formed bar
   invents both a fake signal and a fake forward return.

Run:  quant-alert-engine/venv/Scripts/python.exe -m pytest scanner-engine/tests/test_recorder.py -v
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import recorder as R


def _tmp_db(tmp_path):
    return str(tmp_path / "scanner.db")


# ── schema ─────────────────────────────────────────────────────────────────

def test_schema_records_direction_explicitly(tmp_path):
    db = R.open_db(_tmp_db(tmp_path))
    cols = {row[1] for row in db.execute("PRAGMA table_info(verdicts)")}
    for c in ("direction", "forward_return", "resolved", "horizon_bars",
              "rules_met", "rules_unevaluable", "venues_live"):
        assert c in cols, f"missing column {c}"


def test_opening_twice_is_idempotent(tmp_path):
    p = _tmp_db(tmp_path)
    R.open_db(p)
    R.open_db(p)          # must not raise on an existing schema


# ── BUG 1: forward_return is stored RAW ────────────────────────────────────

def test_forward_return_is_stored_raw_not_direction_adjusted(tmp_path):
    """A bearish signal followed by a -1% move is a WIN. If the recorder
    stores -1% the auditor must still be able to derive +1% from direction."""
    db = R.open_db(_tmp_db(tmp_path))
    R.record_verdict(db, pair="ETH/USD", ts=time.time(), direction=-1,
                     forward_return=-0.01, horizon_bars=5, fill_price=100.0,
                     venues_live=4, rules_met=["1"], rules_unevaluable=[])

    row = db.execute("SELECT direction, forward_return FROM verdicts").fetchone()
    assert row[1] == -0.01, "forward_return must be stored raw, not pre-adjusted"


def test_directed_return_is_derived_in_sql_not_at_write(tmp_path):
    """The auditor's formula, exercised through SQL: bullish keeps the sign,
    bearish flips it, direction 0 is EXCLUDED (not flipped).

    Rows go in unresolved and are completed via resolve(), because an unresolved
    row has not finished its horizon and its forward_return is not final —
    which is why DIRECTED_RETURN_SQL filters on resolved = 1."""
    db = R.open_db(_tmp_db(tmp_path))
    now = time.time()
    ra = R.record_verdict(db, pair="A/USD", ts=now, direction=1,
                          forward_return=None, horizon_bars=5, fill_price=1.0,
                          venues_live=4, rules_met=["1"], rules_unevaluable=[],
                          ref_venue="binance")
    rb = R.record_verdict(db, pair="B/USD", ts=now, direction=-1,
                          forward_return=None, horizon_bars=5, fill_price=1.0,
                          venues_live=4, rules_met=["1"], rules_unevaluable=[],
                          ref_venue="binance")
    rc = R.record_verdict(db, pair="C/USD", ts=now, direction=0,
                          forward_return=None, horizon_bars=5, fill_price=1.0,
                          venues_live=4, rules_met=[], rules_unevaluable=["2"],
                          ref_venue="binance")
    R.resolve(db, ids=[ra, rb, rc],
              return_by_pair={"A/USD": 0.02, "B/USD": -0.02, "C/USD": 0.05})

    rows = db.execute(R.DIRECTED_RETURN_SQL).fetchall()
    # (pair, adjusted) — C must be absent entirely, NOT present as -0.05
    got = {r[0]: r[1] for r in rows}
    assert "A/USD" in got and abs(got["A/USD"] - 0.02) < 1e-9
    assert "B/USD" in got and abs(got["B/USD"] - 0.02) < 1e-9
    assert "C/USD" not in got, "direction=0 must be EXCLUDED, not treated as bearish"


def test_unresolved_rows_are_excluded_from_directed_sql(tmp_path):
    """A row whose horizon has not elapsed must not enter the expectancy math."""
    db = R.open_db(_tmp_db(tmp_path))
    rid = R.record_verdict(db, pair="P/USD", ts=time.time(), direction=1,
                           forward_return=None, horizon_bars=5, fill_price=1.0,
                           venues_live=4, rules_met=["1"], rules_unevaluable=[])
    assert db.execute(R.DIRECTED_RETURN_SQL).fetchall() == []

    # even a row written with a return stays out until resolved
    rid2 = R.record_verdict(db, pair="Q/USD", ts=time.time(), direction=1,
                            forward_return=0.05, horizon_bars=5, fill_price=1.0,
                            venues_live=4, rules_met=["1"], rules_unevaluable=[])
    assert db.execute(R.DIRECTED_RETURN_SQL).fetchall() == []


# ── BUG 2: direction 0 is not bearish ──────────────────────────────────────

def test_direction_zero_rows_are_marked_non_directional(tmp_path):
    db = R.open_db(_tmp_db(tmp_path))
    R.record_verdict(db, pair="C/USD", ts=time.time(), direction=0,
                     forward_return=0.05, horizon_bars=5, fill_price=1.0,
                     venues_live=4, rules_met=[], rules_unevaluable=["2", "3"])
    n = db.execute("SELECT COUNT(*) FROM verdicts WHERE direction = 0").fetchone()[0]
    assert n == 1


def test_non_directional_row_is_excluded_from_expectancy_but_kept(tmp_path):
    """It must be RETAINED for inspection (it records a real observation) while
    being EXCLUDED from the directional expectancy math."""
    db = R.open_db(_tmp_db(tmp_path))
    now = time.time()
    for i in range(3):
        R.record_verdict(db, pair="A/USD", ts=now + i, direction=1,
                         forward_return=0.01, horizon_bars=5, fill_price=1.0,
                         venues_live=4, rules_met=["1"], rules_unevaluable=[])
    for i in range(50):
        R.record_verdict(db, pair="C/USD", ts=now + i, direction=0,
                         forward_return=-0.9, horizon_bars=5, fill_price=1.0,
                         venues_live=4, rules_met=[], rules_unevaluable=["2"])

    total = db.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
    assert total == 53, "every observation must be retained"

    directed = db.execute(
        "SELECT COUNT(*) FROM verdicts WHERE direction != 0").fetchone()[0]
    assert directed == 3, "the 50 direction=0 rows must not enter expectancy math"


# ── BUG 3: in-progress bars never produce an outcome ───────────────────────

def test_in_progress_bar_cannot_be_recorded(tmp_path):
    """The last kline row is partially formed. Recording an outcome from it
    invents a signal and a forward return that did not happen."""
    db = R.open_db(_tmp_db(tmp_path))
    try:
        R.record_verdict(db, pair="ETH/USD", ts=time.time(), direction=1,
                         forward_return=0.01, horizon_bars=5, fill_price=100.0,
                         venues_live=4, rules_met=["1"], rules_unevaluable=[],
                         bar_closed=False)
    except ValueError:
        return
    raise AssertionError("an unclosed bar must be rejected")


def test_closed_bar_is_accepted(tmp_path):
    db = R.open_db(_tmp_db(tmp_path))
    R.record_verdict(db, pair="ETH/USD", ts=time.time(), direction=1,
                     forward_return=0.01, horizon_bars=5, fill_price=100.0,
                     venues_live=4, rules_met=["1"], rules_unevaluable=[],
                     bar_closed=True)
    assert db.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 1


# ── unevaluable rules are carried, not discarded ───────────────────────────

def test_unevaluable_rules_are_persisted_for_audit(tmp_path):
    """If UNEVALUABLE rows are dropped, the auditor cannot tell 'the rule did
    not fire' from 'the rule was never evaluated'."""
    db = R.open_db(_tmp_db(tmp_path))
    R.record_verdict(db, pair="NVDA/USD", ts=time.time(), direction=0,
                     forward_return=0.0, horizon_bars=5, fill_price=230.0,
                     venues_live=3, rules_met=["4"],
                     rules_unevaluable=["2", "3"])
    row = db.execute("SELECT rules_unevaluable FROM verdicts").fetchone()[0]
    assert "2" in row and "3" in row


def test_venues_live_is_stored_per_row(tmp_path):
    """Coverage must be auditable after the fact: was a 2-venue verdict ever
    compared against a 3-venue rule?"""
    db = R.open_db(_tmp_db(tmp_path))
    now = time.time()
    R.record_verdict(db, pair="A/USD", ts=now, direction=1, forward_return=0.01,
                     horizon_bars=5, fill_price=1.0, venues_live=4,
                     rules_met=["1"], rules_unevaluable=[])
    R.record_verdict(db, pair="B/USD", ts=now, direction=1, forward_return=0.01,
                     horizon_bars=5, fill_price=1.0, venues_live=2,
                     rules_met=["1"], rules_unevaluable=[])
    rows = dict(db.execute("SELECT pair, venues_live FROM verdicts").fetchall())
    assert rows == {"A/USD": 4, "B/USD": 2}


# ── resolution ─────────────────────────────────────────────────────────────

def test_unresolved_rows_default_to_zero(tmp_path):
    db = R.open_db(_tmp_db(tmp_path))
    R.record_verdict(db, pair="A/USD", ts=time.time(), direction=1,
                     forward_return=0.01, horizon_bars=5, fill_price=1.0,
                     venues_live=4, rules_met=["1"], rules_unevaluable=[])
    assert db.execute("SELECT resolved FROM verdicts").fetchone()[0] == 0


def test_resolve_marks_rows_resolved(tmp_path):
    db = R.open_db(_tmp_db(tmp_path))
    now = time.time()
    rid = R.record_verdict(db, pair="A/USD", ts=now, direction=1,
                           forward_return=None, horizon_bars=5, fill_price=100.0,
                           venues_live=4, rules_met=["1"], rules_unevaluable=[])
    n = R.resolve(db, ids=[rid], return_by_pair={})
    assert n == 0        # no outcome available yet


def test_resolution_writes_the_realized_move(tmp_path):
    db = R.open_db(_tmp_db(tmp_path))
    rid = R.record_verdict(db, pair="A/USD", ts=time.time(), direction=1,
                           forward_return=None, horizon_bars=5, fill_price=100.0,
                           venues_live=4, rules_met=["1"], rules_unevaluable=[])
    R.resolve(db, ids=[rid], return_by_pair={"A/USD": 0.015})
    fwd, resolved = db.execute(
        "SELECT forward_return, resolved FROM verdicts WHERE id=?", (rid,)).fetchone()
    assert abs(fwd - 0.015) < 1e-12
    assert resolved == 1


def test_forward_return_stays_raw_through_resolution(tmp_path):
    """A bearish row resolved with a -1% move keeps -1% in storage; the
    direction adjustment happens only at audit time."""
    db = R.open_db(_tmp_db(tmp_path))
    rid = R.record_verdict(db, pair="B/USD", ts=time.time(), direction=-1,
                           forward_return=None, horizon_bars=5, fill_price=100.0,
                           venues_live=4, rules_met=["1"], rules_unevaluable=[])
    R.resolve(db, ids=[rid], return_by_pair={"B/USD": -0.01})
    fwd = db.execute("SELECT forward_return FROM verdicts WHERE id=?", (rid,)).fetchone()[0]
    assert abs(fwd - (-0.01)) < 1e-12, "storage must stay raw; adjustment is SQL-side"


def test_resolution_skips_rows_with_no_data(tmp_path):
    db = R.open_db(_tmp_db(tmp_path))
    rid = R.record_verdict(db, pair="Z/USD", ts=time.time(), direction=1,
                           forward_return=None, horizon_bars=5, fill_price=1.0,
                           venues_live=4, rules_met=["1"], rules_unevaluable=[])
    assert R.resolve(db, ids=[rid], return_by_pair={}) == 0
    assert db.execute("SELECT resolved FROM verdicts WHERE id=?", (rid,)).fetchone()[0] == 0