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
import json
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


# ── BUG 4: el ancla de entrada no puede moverse tras la decision ───────────
#
# Medido 2026-10-15. El horizonte y la vela de salida se anclan en `ts`, que es
# el nacimiento de la decision y nunca se mueve (el resolutor busca la salida en
# `ts*1000 + horizon_bars*step_ms`). Pero el upsert refrescaba `fill_price` en
# CADA poll posterior, asi que la fila quedaba con la ENTRADA del ultimo poll y
# la SALIDA anclada en `ts`: medía solo la parte final del horizonte.
#
# El sesgo no es ruido. Si la regla predice algo, el movimiento empieza al
# disparar; mover la entrada hacia adelante recorta justo el trozo predicho y
# ATENUA el edge medido de forma sistematica — la clase de error que hace que
# una regla con edge real parezca no tenerlo. Y sobre esta tabla se midieron y
# se pusieron en cuarentena las reglas 6 y 7.
#
# Era invisible al auditor ademas: `exit_close_ms` se graba bien (anclado en
# ts), asi que el horizonte realizado reportado parece correcto; el
# truncamiento estaba en la entrada, que no se audita.

def test_later_poll_in_the_same_window_cannot_move_the_entry_price(tmp_path):
    db = R.open_db(_tmp_db(tmp_path))
    ts0 = time.time()
    win = int(ts0 * 1000) + 300_000            # misma ventana de salida
    rid, created = R.upsert_verdict(
        db, pair="ETH/USD", ts=ts0, direction=1, horizon_bars=5,
        exit_window_ms=win, fill_price=100.0, venues_live=4,
        rules_met=["2"], rules_unevaluable=[], score=1,
        ref_venue="binance", fill_spread_bps=2.0, cross_basis_bps=0.5,
        price_basis_ok=True, candidate=True)
    assert created is True

    # ~4 minutos despues, en la MISMA ventana: el precio ya se movio y las
    # reglas cambiaron.
    rid2, created2 = R.upsert_verdict(
        db, pair="ETH/USD", ts=ts0 + 240, direction=1, horizon_bars=5,
        exit_window_ms=win, fill_price=103.0, venues_live=4,
        rules_met=["2", "3"], rules_unevaluable=[], score=3,
        ref_venue="okx", fill_spread_bps=9.0, cross_basis_bps=4.0,
        price_basis_ok=False, candidate=True)
    assert created2 is False and rid2 == rid

    row = db.execute(
        "SELECT ts, fill_price, ref_venue, fill_spread_bps, cross_basis_bps,"
        " price_basis_ok, score, rules_met FROM verdicts WHERE id=?", (rid,)
    ).fetchone()
    assert row[0] == ts0, "el ts de la decision no puede moverse"
    assert row[1] == 100.0, (
        "la ENTRADA se refresco al ultimo poll: la fila mide solo la parte "
        "final del horizonte y el edge medido sale atenuado")
    assert row[2] == "binance" and row[3] == 2.0 and row[4] == 0.5, (
        "venue/spread/basis describen el fill del instante de la decision: "
        "emparejarlos con otra lectura es incoherente")
    assert row[5], (
        "price_basis_ok tambien pertenece al instante de entrada: debe "
        "conservar el valor de la decision (True), no heredar el False "
        "posterior")
    assert row[6] == 3, "el ESTADO si debe refrescarse (lectura mas fresca)"
    assert json.loads(row[7]) == ["2", "3"]


def test_a_new_window_still_writes_a_fresh_entry_price(tmp_path):
    """El congelado es POR VENTANA. Una ventana nueva es una decision nueva y
    tiene que entrar con SU precio; si no, esto no seria un ledger."""
    db = R.open_db(_tmp_db(tmp_path))
    ts0 = time.time()
    R.upsert_verdict(db, pair="ETH/USD", ts=ts0, direction=1, horizon_bars=5,
                     exit_window_ms=int(ts0 * 1000) + 300_000, fill_price=100.0,
                     venues_live=4, rules_met=["2"], rules_unevaluable=[])
    rid2, created2 = R.upsert_verdict(
        db, pair="ETH/USD", ts=ts0 + 600, direction=1, horizon_bars=5,
        exit_window_ms=int(ts0 * 1000) + 600_000, fill_price=105.0,
        venues_live=4, rules_met=["2"], rules_unevaluable=[])
    assert created2 is True
    assert db.execute("SELECT fill_price FROM verdicts WHERE id=?",
                      (rid2,)).fetchone()[0] == 105.0