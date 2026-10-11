"""Tests for the outcome resolver and the two-ledger split.

Gap 1 was a missing feature: `recorder.resolve()` existed, was tested, and
NOTHING CALLED IT. These tests pin the wiring that closes it.

Gap 2 is the circular dependency: a rule cannot be AUDITED without measured
outcomes, and outcomes cannot be measured without a direction, and no rule may
name a direction until AUDITED. The two-ledger split breaks that loop:

  CANDIDATE ledger — a rule fired MET, so the row carries that rule's
  provisional `direction_hint`. It enters expectancy math. It CANNOT suppress,
  alert, or gate. It is a hypothesis under test.

  VERDICT ledger — no rule fired, or the firing rule is not yet AUDITED, so the
  row stays direction=0 and is excluded from math. Downstream reads only this.

Run:  quant-alert-engine/venv/Scripts/python.exe -m pytest scanner-engine/tests/test_outcomes.py -v
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import outcomes as OUT
import recorder as RC


def _db(tmp_path):
    return RC.open_db(str(tmp_path / "s.db"))


def _book(mid):
    from types_ import Book
    return Book(bid=mid - 0.05, bids=[(mid - 0.05, 1.0)], asks=[(mid + 0.05, 1.0)])


# ── forward price lookup ───────────────────────────────────────────────────

def test_forward_return_is_the_move_between_two_prices():
    assert abs(OUT.forward_return(fill=100.0, later=101.0) - 0.01) < 1e-12
    assert abs(OUT.forward_return(fill=100.0, later=99.0) + 0.01) < 1e-12


def test_zero_move_is_zero_not_none():
    assert OUT.forward_return(fill=100.0, later=100.0) == 0.0


def test_unknown_price_yields_none_never_zero():
    """A missing future price is NOT a flat move. Zeroing it would fabricate
    a perfectly neutral outcome from absent data."""
    assert OUT.forward_return(fill=100.0, later=None) is None
    assert OUT.forward_return(fill=None, later=101.0) is None
    assert OUT.forward_return(fill=0.0, later=101.0) is None


# ── resolution gate: only CLOSED bars count ────────────────────────────────

def test_bar_is_only_resolvable_once_closed():
    now_ms = 1_700_000_000_000
    closed = {"open_ms": now_ms - 120_000, "close_ms": now_ms - 60_000}
    forming = {"open_ms": now_ms - 10_000, "close_ms": now_ms + 50_000}

    assert OUT.bar_is_closed(closed, now_ms=now_ms) is True
    assert OUT.bar_is_closed(forming, now_ms=now_ms) is False


def test_forming_bar_is_never_resolved():
    """Resolving against a partially formed bar invents a forward return."""
    assert OUT.resolve_row(bar={"close_ms": 0}, horizon_bars=1, now_ms=10**13,
                           fill=100.0) is None


# ── the resolver itself ────────────────────────────────────────────────────

def test_resolver_resolves_a_row_whose_horizon_has_elapsed(tmp_path):
    db = _db(tmp_path)
    rid = RC.record_verdict(db, pair="ETH/USD", ts=time.time() - 600,
                            direction=0, forward_return=None, horizon_bars=5,
                            fill_price=2000.0, venues_live=4,
                            rules_met=[], rules_unevaluable=["2"])
    n = OUT.resolve_pending(db, fetch_bar=lambda pair, at_ms: {
        "open_ms": at_ms - 60_000, "close_ms": at_ms, "close": 2020.0,
    }, now_ms=int(time.time() * 1000))
    assert n == 1
    fwd, res = db.execute(
        "SELECT forward_return, resolved FROM verdicts WHERE id=?", (rid,)).fetchone()
    assert res == 1
    assert abs(fwd - 0.01) < 1e-9


def test_resolver_leaves_a_row_alone_while_its_bar_is_forming(tmp_path):
    """The horizon bar must be in the FUTURE to count as still forming.

    The first version of this test used `close_ms = at_ms + 60_000` with an
    `at_ms` already 600s in the past, which made the bar closed -- so the row
    correctly resolved and the assertion was wrong, not the resolver.
    """
    db = _db(tmp_path)
    now_ms = int(time.time() * 1000)
    # record the row 5 seconds ago so its horizon lands well in the future
    RC.record_verdict(db, pair="ETH/USD", ts=(now_ms / 1000) - 5,
                      direction=0, forward_return=None, horizon_bars=5,
                      fill_price=2000.0, venues_live=4,
                      rules_met=[], rules_unevaluable=["2"])
    n = OUT.resolve_pending(db, fetch_bar=lambda pair, at_ms: {
        "open_ms": at_ms, "close_ms": at_ms + 60_000, "close": 2020.0,
    }, now_ms=now_ms)
    assert n == 0, "a forming bar must not be scored"
    assert db.execute("SELECT resolved FROM verdicts").fetchone()[0] == 0


def test_resolver_skips_rows_with_no_fill_price(tmp_path):
    """No fill price means no basis to compute a return from. Resolving it
    would invent one."""
    db = _db(tmp_path)
    RC.record_verdict(db, pair="ETH/USD", ts=time.time() - 600,
                      direction=0, forward_return=None, horizon_bars=5,
                      fill_price=None, venues_live=4,
                      rules_met=["4"], rules_unevaluable=[])
    n = OUT.resolve_pending(db, fetch_bar=lambda p, m: {
        "open_ms": m - 60_000, "close_ms": m, "close": 2020.0},
        now_ms=int(time.time() * 1000))
    assert n == 0


def test_resolver_does_not_double_resolve(tmp_path):
    db = _db(tmp_path)
    RC.record_verdict(db, pair="ETH/USD", ts=time.time() - 600, direction=0,
                      forward_return=None, horizon_bars=5, fill_price=2000.0,
                      venues_live=4, rules_met=["4"], rules_unevaluable=[])
    fetch = lambda p, m: {"open_ms": m - 60_000, "close_ms": m, "close": 2020.0}
    now = int(time.time() * 1000)
    assert OUT.resolve_pending(db, fetch_bar=fetch, now_ms=now) == 1
    assert OUT.resolve_pending(db, fetch_bar=fetch, now_ms=now) == 0


def test_resolver_stores_raw_not_direction_adjusted(tmp_path):
    """A candidate row on the SHORT side that moved down must store the raw
    negative move. The flip happens in SQL, once, and is re-derivable."""
    db = _db(tmp_path)
    RC.record_verdict(db, pair="ETH/USD", ts=time.time() - 600, direction=-1,
                      forward_return=None, horizon_bars=5, fill_price=2000.0,
                      venues_live=4, rules_met=["4"], rules_unevaluable=[])
    OUT.resolve_pending(db, fetch_bar=lambda p, m: {
        "open_ms": m - 60_000, "close_ms": m, "close": 1980.0},
        now_ms=int(time.time() * 1000))
    fwd = db.execute("SELECT forward_return FROM verdicts").fetchone()[0]
    assert abs(fwd - (-0.01)) < 1e-9, "storage must stay raw"


# ── the two ledgers ────────────────────────────────────────────────────────

def test_a_met_rule_marks_the_row_candidate(tmp_path):
    db = _db(tmp_path)
    rid = RC.record_verdict(db, pair="ETH/USD", ts=time.time(), direction=1,
                            forward_return=None, horizon_bars=5, fill_price=2000.0,
                            venues_live=4, rules_met=["4"], rules_unevaluable=[])
    assert db.execute("SELECT candidate FROM verdicts WHERE id=?", (rid,)).fetchone()[0] == 1


def test_a_row_with_no_met_rule_is_not_candidate(tmp_path):
    db = _db(tmp_path)
    rid = RC.record_verdict(db, pair="NVDA/USD", ts=time.time(), direction=0,
                            forward_return=None, horizon_bars=5, fill_price=230.0,
                            venues_live=3, rules_met=[], rules_unevaluable=["2", "3"])
    assert db.execute("SELECT candidate FROM verdicts WHERE id=?", (rid,)).fetchone()[0] == 0


def test_candidate_rows_enter_expectancy_and_verdict_rows_do_not(tmp_path):
    """THE SPLIT. A candidate row has a provisional side and is measured. A
    verdict row has none and is excluded. Same table, different population."""
    db = _db(tmp_path)
    now = time.time() - 600
    ids = []
    for i in range(5):
        ids.append(RC.record_verdict(db, pair="A/USD", ts=now + i, direction=1,
                                     forward_return=None, horizon_bars=5,
                                     fill_price=100.0, venues_live=4,
                                     rules_met=["4"], rules_unevaluable=[]))
    for i in range(50):
        ids.append(RC.record_verdict(db, pair="B/USD", ts=now + i, direction=0,
                                     forward_return=None, horizon_bars=5,
                                     fill_price=100.0, venues_live=4,
                                     rules_met=[], rules_unevaluable=["2"]))

    fetch = lambda p, m: {"open_ms": m - 60_000, "close_ms": m,
                          "close": 101.0 if p == "A/USD" else 100.0}
    # Loop: resolve_pending is a BATCH (12 rows), not a sweep. One call sees
    # only the 12 newest, and this test has 55 rows. The production loop drains
    # the backlog over successive cycles; the test must do the same.
    for _ in range(10):
        OUT.resolve_pending(db, fetch_bar=fetch, now_ms=int(time.time() * 1000))

    cand = db.execute("SELECT COUNT(*) FROM verdicts WHERE candidate=1 "
                      "AND resolved=1 AND direction!=0").fetchone()[0]
    verdict = db.execute("SELECT COUNT(*) FROM verdicts WHERE candidate=0 "
                         "AND resolved=1 AND direction!=0").fetchone()[0]
    assert cand == 5, f"expected all 5 candidate rows resolved, got {cand}"
    assert verdict == 0


def test_candidate_flag_is_independent_of_direction(tmp_path):
    """direction=0 can still be a candidate (a rule fired but its hint was
    null). candidate=1 with direction=0 must be retained but excluded."""
    db = _db(tmp_path)
    RC.record_verdict(db, pair="XAU/USD", ts=time.time() - 600, direction=0,
                      forward_return=None, horizon_bars=5, fill_price=4100.0,
                      venues_live=4, rules_met=["1"], rules_unevaluable=[])
    row = db.execute("SELECT candidate, direction FROM verdicts").fetchone()
    assert row[0] == 1 and row[1] == 0


# ── direction hints ────────────────────────────────────────────────────────

def test_hint_side_follow_maps_to_the_agreed_flow_direction():
    """'follow' means: the side the flow points to. Read from the agreeing
    venues' median delta_ratio, not invented."""
    hints = {"4": {"direction_hint": {"side": "follow"}}}
    r = OUT.hint_direction(hints, {"4": 0.72})
    assert r == 1, "positive flow -> long"


def test_hint_side_follow_reads_the_flow_sign():
    hints = {"4": {"direction_hint": {"side": "follow"}}}
    assert OUT.hint_direction(hints, {"4": 0.31}) == -1
    assert OUT.hint_direction(hints, {"4": 0.50}) == 0, "no flow lean -> no side"


def test_null_hint_yields_no_side_even_when_a_rule_fired():
    """Rule 1 (basis dislocation) has no directional claim: 'one venue is
    apart' does not imply the pair moves either way. That is honest, and the
    auditor must be able to conclude exactly that."""
    hints = {"1": {"direction_hint": None}}
    assert OUT.hint_direction(hints, {"1": 0.9}) == 0


def test_fade_hint_inverts_the_flow_sign():
    hints = {"4": {"direction_hint": {"side": "fade"}}}
    assert OUT.hint_direction(hints, {"4": 0.72}) == -1


def test_hint_is_ignored_for_a_rule_that_did_not_fire():
    hints = {"4": {"direction_hint": {"side": "follow"}}}
    assert OUT.hint_direction(hints, {"2": 0.9}) == 0


def test_only_audited_rules_may_reach_the_verdict_ledger(tmp_path):
    """A candidate side is a hypothesis. Nothing downstream may read it."""
    db = _db(tmp_path)
    rid = RC.record_verdict(db, pair="ETH/USD", ts=time.time(), direction=1,
                            forward_return=None, horizon_bars=5, fill_price=2000.0,
                            venues_live=4, rules_met=["4"], rules_unevaluable=[])
    c, d = db.execute("SELECT candidate, direction FROM verdicts WHERE id=?",
                      (rid,)).fetchone()
    assert c == 1 and d == 1     # both recorded
    # but promotion must be explicit; the row is not verdict-eligible yet
    assert OUT.promoted_direction(
        rules={"4": {"audit_status": "UNVALIDATED",
                     "direction_hint": {"side": "follow"}}},
        rule_ids=["4"], flow_by_rule={"4": 0.72}) == 0


def test_audited_rule_can_reach_the_verdict_ledger():
    rules = {"4": {"audit_status": "AUDITED keep t=+2.14 n=412",
                   "direction_hint": {"side": "follow"}}}
    assert OUT.promoted_direction(rules=rules, rule_ids=["4"],
                                  flow_by_rule={"4": 0.72}) == 1


def test_mixed_fired_rules_promote_only_if_all_are_audited():
    """Rule 3 fired with rule 4, but rule 3 is unaudited: the combined claim
    is not earned, so nothing is promoted."""
    rules = {"3": {"audit_status": "UNVALIDATED",
                   "direction_hint": {"side": "follow"}},
             "4": {"audit_status": "AUDITED keep t=+2.14",
                   "direction_hint": {"side": "follow"}}}
    assert OUT.promoted_direction(rules=rules, rule_ids=["3", "4"],
                                  flow_by_rule={"3": 0.8, "4": 0.72}) == 0
    rules2 = {"3": {"audit_status": "AUDITED keep",
                    "direction_hint": {"side": "follow"}},
              "4": {"audit_status": "AUDITED keep",
                    "direction_hint": {"side": "follow"}}}
    assert OUT.promoted_direction(rules=rules2, rule_ids=["3", "4"],
                                  flow_by_rule={"3": 0.8, "4": 0.72}) == 1


# ── queue ordering: the batch must not starve the rows that are ready ──────

def test_pending_batch_is_oldest_first(tmp_path):
    """A time-gated consumer must drain from the FRONT of the queue.

    This is the bug that lost half the signals. `pending_rows` ordered
    `ts DESC` -- newest first -- but `resolve_pending` can only score a row
    once its horizon bar has CLOSED, and a 5-bar horizon closes 5 minutes
    after the row is written. So the newest rows are exactly the ones that
    cannot be resolved yet: the resolver spent every cycle fetching its
    batch of newest rows, resolved none of them, and never reached the older
    rows that were ready. Those aged out and were retired unscorable.

    Measured on the live database before the fix: 296 candidate rows (52% of
    all 569 signals) retired unscorable, and rows sitting at age 1790s with
    horizon_bars=5 -- ready at ~300s, still waiting at 1790s, one poll away
    from the 1800s retirement.

    Here 24 rows are staged: 12 old enough to score, 12 too new. With a batch
    of 12, DESC resolves 0 of them; ASC resolves all 12 ready ones.
    """
    db = _db(tmp_path)
    now = time.time()
    ready_ids, young_ids = [], []
    for i in range(12):
        # 600s old, horizon 5 -> its bar closed ~300s ago
        ready_ids.append(RC.record_verdict(
            db, pair="ETH/USD", ts=now - 600 - i, direction=0,
            forward_return=None, horizon_bars=5, fill_price=2000.0,
            venues_live=4, rules_met=[], rules_unevaluable=[]))
    for i in range(12):
        # 5s old, horizon 5 -> its bar is still ~295s in the future
        young_ids.append(RC.record_verdict(
            db, pair="ETH/USD", ts=now - 5 - (i * 0.1), direction=0,
            forward_return=None, horizon_bars=5, fill_price=2000.0,
            venues_live=4, rules_met=[], rules_unevaluable=[]))

    # a bar that closes exactly at at_ms: closed for the old rows, still
    # forming for the young ones
    n = OUT.resolve_pending(
        db,
        fetch_bar=lambda pair, at_ms: {"open_ms": at_ms - 60_000,
                                       "close_ms": at_ms, "close": 2020.0},
        now_ms=int(now * 1000), limit=12)

    assert n == 12, f"batch resolved {n} of 12 READY rows -- queue is starved"
    done = {r[0] for r in db.execute(
        "SELECT id FROM verdicts WHERE resolved=1")}
    assert done == set(ready_ids), "the OLD ready rows are the ones that must resolve"
    assert not (done & set(young_ids)), "a forming bar must never be scored"


def test_a_second_batch_drains_the_rest(tmp_path):
    """The batch is a BATCH, not a cap: the next cycle continues.

    True before and after the ordering fix, but it is the property that makes
    ASC safe -- a backlog drains at `limit` per cycle instead of starving.
    """
    db = _db(tmp_path)
    now = time.time()
    for i in range(20):
        RC.record_verdict(db, pair="ETH/USD", ts=now - 600 - i, direction=0,
                          forward_return=None, horizon_bars=5,
                          fill_price=2000.0, venues_live=4,
                          rules_met=[], rules_unevaluable=[])
    bar = lambda pair, at_ms: {"open_ms": at_ms - 60_000,
                               "close_ms": at_ms, "close": 2020.0}
    now_ms = int(now * 1000)
    first = OUT.resolve_pending(db, fetch_bar=bar, now_ms=now_ms, limit=8)
    second = OUT.resolve_pending(db, fetch_bar=bar, now_ms=now_ms, limit=8)
    third = OUT.resolve_pending(db, fetch_bar=bar, now_ms=now_ms, limit=8)
    assert (first, second, third) == (8, 8, 4), "each cycle must drain its batch"