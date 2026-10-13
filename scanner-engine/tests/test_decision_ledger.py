"""The decision-point ledger: ONE row per (pair, exit window).

Before 2026-10-11 the scan loop inserted one row per ~12s poll, so ~25 rows
shared a single exit window and the auditor had to repair the overlap with an
effective-sample-size correction. The upsert collapses those to one row whose
state fields carry the freshest pre-exit reading. These tests pin the contract:
same window -> same row, updated in place, original birth ts intact; different
window -> different row; and the resolver can still score the row like any
other.
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import recorder as RC                    # noqa: E402


def _upsert(db, pair="ETH/USD", exit_window_ms=3_600_000, direction=1,
            score=10, ts=1_700_000_000.0, **kw):
    return RC.upsert_verdict(
        db, pair=pair, ts=ts, direction=direction, horizon_bars=5,
        exit_window_ms=exit_window_ms, fill_price=kw.pop("fill_price", 1.0),
        venues_live=kw.pop("venues_live", 4),
        rules_met=kw.pop("rules_met", ["4"]),
        rules_unevaluable=kw.pop("rules_unevaluable", []),
        score=score, ref_venue="binance", **kw)


def test_same_window_updates_in_place(tmp_path):
    db = RC.open_db(tmp_path / "s.db")
    rid1, created1 = _upsert(db, direction=1, score=10)
    rid2, created2 = _upsert(db, direction=-1, score=20, ts=1_700_000_012.0)
    assert created1 and not created2
    assert rid1 == rid2, "la misma ventana DEBE ser la misma fila"
    row = db.execute(
        "SELECT direction, score, ts FROM verdicts WHERE id=?", (rid1,)
    ).fetchone()
    assert row[0] == -1, "el estado se refresca al poll mas reciente"
    assert row[1] == 20
    assert row[2] == 1_700_000_000.0, "el ts de NACIMIENTO nunca se mueve"


def test_distinct_windows_are_distinct_rows(tmp_path):
    db = RC.open_db(tmp_path / "s.db")
    rid1, _ = _upsert(db, exit_window_ms=3_600_000)
    rid2, created = _upsert(db, exit_window_ms=3_660_000, ts=1_700_000_012.0)
    assert created and rid1 != rid2


def test_window_key_survives_restart(tmp_path):
    # The dedup runs through the DB, not an in-memory cache: a scanner restart
    # must not duplicate the open window's row.
    path = tmp_path / "s.db"
    db = RC.open_db(path)
    rid1, _ = _upsert(db)
    db.close()
    db2 = RC.open_db(path)
    rid2, created = _upsert(db2, ts=1_700_000_024.0)
    assert not created and rid1 == rid2


def test_window_must_be_positive_epoch_ms(tmp_path):
    db = RC.open_db(tmp_path / "s.db")
    with pytest.raises(ValueError):
        _upsert(db, exit_window_ms=0)
    with pytest.raises(ValueError):
        _upsert(db, exit_window_ms=None)


def test_unique_index_blocks_raw_double_insert(tmp_path):
    # The upsert looks before it writes; the partial UNIQUE index is the
    # structural backstop if any future call site forgets to look.
    db = RC.open_db(tmp_path / "s.db")
    _upsert(db)
    with pytest.raises(Exception):
        db.execute(
            """INSERT INTO verdicts (ts, pair, direction, venues_live, rules_met,
                                     rules_unevaluable, score, horizon_bars,
                                     exit_window_ms)
               VALUES (1, 'ETH/USD', 1, 4, '[]', '[]', 0, 5, 3_600_000)""")
        db.commit()


def test_window_row_resolves_like_any_row(tmp_path):
    db = RC.open_db(tmp_path / "s.db")
    rid, _ = _upsert(db)
    RC.resolve(db, ids=[rid], return_by_pair={"ETH/USD": 0.002})
    row = db.execute(
        "SELECT resolved, forward_return FROM verdicts WHERE id=?", (rid,)
    ).fetchone()
    assert row == (1, 0.002)
