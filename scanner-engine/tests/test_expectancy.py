"""Tests for the expectancy auditor.

THE POINT OF THIS MODULE IS THAT IT SAYS "INSUFFICIENT DATA" ON DAY ONE.
These tests protect that, and pin the sibling-engine mistakes as regressions.

Run:  quant-alert-engine/venv/Scripts/python.exe -m pytest scanner-engine/tests/test_expectancy.py -v
"""
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import expectancy as E
import recorder as RC


def _db(tmp_path):
    return RC.open_db(str(tmp_path / "s.db"))


def _center(vals, target):
    """Shift a sample so its EMPIRICAL mean is exactly `target`.

    Without this, a seeded draw only lands near the requested mean, and a test
    that means to sit at exactly zero net can drift a hair to one side and flip
    the bucket on the strength of a single seed. The fixtures here exist to
    pin the statistic, so the statistic is pinned exactly.
    """
    if not vals:
        return vals
    return [v - (sum(vals) / len(vals) - target) for v in vals]


def _fill(db, n, direction, fwd, pair="A/USD", rule="1", sd=0.004, seed=7):
    """Insert n resolved directed rows with a controlled mean and stdev.

    IID draws from a seeded RNG, NOT a linear ramp.

    The previous helper built a symmetric ramp, which is deterministic and
    therefore almost perfectly autocorrelated (lag-1 r approaches 1). That is
    not a return series, it is a sorted list, and once the auditor started
    correcting for overlap every one of these fixtures collapsed to
    n_eff ~ 1 and bucketed "insufficient" regardless of the mean it was
    constructed to carry. A fixture that cannot exercise the statistic it
    exists to test is worse than no fixture.

    `seed` is fixed so the suite stays deterministic.
    """
    import random
    import time as _t

    rng = random.Random(seed)
    if sd <= 0:
        vals = [fwd] * n
    else:
        vals = [rng.gauss(fwd, sd) for _ in range(n)]
    vals = _center(vals, fwd)

    base = _t.time()
    ids = [RC.record_verdict(db, pair=pair, ts=base + i, direction=direction,
                             forward_return=None, horizon_bars=5,
                             fill_price=100.0, venues_live=4,
                             rules_met=[rule], rules_unevaluable=[],
                             ref_venue="binance", price_basis_ok=True)
           for i in range(n)]
    for rid, r in zip(ids, vals):
        RC.resolve(db, ids=[rid], return_by_pair={pair: r})
    return ids


def _fill_overlapping(db, n, direction, fwd, pair="O/USD", rule="1",
                      sd=0.004, rho=0.8, seed=11):
    """Rows that genuinely OVERLAP: an AR(1) series.

    This is what the live corpus looks like — the loop polls faster than the
    horizon elapses, so consecutive rows share one underlying price move. It is
    the fixture that makes the overlap correction observable: a strong mean
    with high rho must NOT read as significant just because n is large.
    """
    import math
    import random
    import time as _t

    rng = random.Random(seed)
    vals, prev = [], 0.0
    for _ in range(n):
        prev = rho * prev + math.sqrt(1 - rho ** 2) * rng.gauss(0.0, sd)
        vals.append(fwd + prev)
    vals = _center(vals, fwd)

    base = _t.time()
    ids = [RC.record_verdict(db, pair=pair, ts=base + i, direction=direction,
                             forward_return=None, horizon_bars=5,
                             fill_price=100.0, venues_live=4,
                             rules_met=[rule], rules_unevaluable=[],
                             ref_venue="binance", price_basis_ok=True)
           for i in range(n)]
    for rid, r in zip(ids, vals):
        RC.resolve(db, ids=[rid], return_by_pair={pair: r})
    return ids


# ── day one: the honest answer ─────────────────────────────────────────────

def test_empty_database_reports_insufficient_data(tmp_path):
    a = E.audit(_db(tmp_path))
    assert a["verdict"] == "INSUFFICIENT DATA"
    assert "direction" in a["reason"]


def test_database_with_only_nondirectional_rows_is_insufficient(tmp_path):
    """Rows that all carry direction=0 cannot support any expectancy claim."""
    db = _db(tmp_path)
    _fill(db, 100, direction=0, fwd=0.05)
    a = E.audit(db)
    assert a["verdict"] == "INSUFFICIENT DATA"


def test_unresolved_rows_are_not_insufficient_but_are_not_data_either(tmp_path):
    """Unresolved rows exist but have not finished their horizon."""
    db = _db(tmp_path)
    import time as _t
    for i in range(100):
        RC.record_verdict(db, pair="A/USD", ts=_t.time() + i, direction=1,
                          forward_return=None, horizon_bars=5, fill_price=1.0,
                          venues_live=4, rules_met=["1"], rules_unevaluable=[])
    a = E.audit(db)
    assert a["verdict"] == "INSUFFICIENT DATA"


# ── BUG 1: direction=0 is never treated as bearish ─────────────────────────

def test_nondirectional_rows_never_enter_the_math(tmp_path):
    """300 non-directional rows at -50% must not move any expectancy."""
    db = _db(tmp_path)
    _fill(db, 60, direction=1, fwd=0.01, pair="A/USD", rule="1")
    before = E.audit(db)["rules"]["1"]

    _fill(db, 300, direction=0, fwd=-0.50, pair="Z/USD", rule="1")
    after = E.audit(db)["rules"]["1"]

    assert before["n"] == after["n"], "direction=0 rows entered the math"
    assert abs(before["mean"] - after["mean"]) < 1e-12


def test_a_bearish_row_counts_as_a_win_when_it_moves_down(tmp_path):
    """A -1 verdict on a -2% move is a WIN. The stored return is raw, so the
    SQL flips it and the win shows up as positive."""
    db = _db(tmp_path)
    rid = RC.record_verdict(db, pair="A/USD", ts=0.0, direction=-1,
                            forward_return=None, horizon_bars=5, fill_price=100.0,
                            venues_live=4, rules_met=["1"], rules_unevaluable=[],
                            ref_venue="binance")
    RC.resolve(db, ids=[rid], return_by_pair={"A/USD": -0.02})
    vals = E.by_rule(db)["1"]
    assert abs(vals[0] - 0.02) < 1e-12, "bearish win must be positive in the math"


# ── BUG 2: no conclusion without significance ──────────────────────────────

def test_negative_mean_with_small_n_is_not_a_drop(tmp_path):
    """60 rows all at -0.10 with zero spread: mean is clearly negative but the
    distribution is degenerate, so t is undefined and the verdict must not be
    'drop'."""
    db = _db(tmp_path)
    _fill(db, 60, direction=1, fwd=-0.10, pair="A/USD", rule="1", sd=0.0)
    a = E.audit(db)
    r = a["rules"]["1"]
    assert r["mean"] < 0
    assert r["bucket"] != "drop", "a negative mean alone must not drop a rule"


def test_small_n_is_insufficient_not_inconclusive(tmp_path):
    db = _db(tmp_path)
    _fill(db, 10, direction=1, fwd=0.01, pair="A/USD", rule="1")
    r = E.audit(db)["rules"]["1"]
    assert r["bucket"] == "insufficient"
    assert "min_n" in r["why"]


def test_significant_negative_is_a_drop(tmp_path):
    """A genuinely negative, well-distributed sample at n >= min_n."""
    db = _db(tmp_path)
    _fill(db, 80, direction=1, fwd=-0.01, pair="A/USD", rule="1", sd=0.004)
    r = E.audit(db)["rules"]["1"]
    assert r["bucket"] == "drop"
    assert r["t"] <= -1.65
    assert r["n"] >= 50


def test_significant_positive_is_a_keep(tmp_path):
    db = _db(tmp_path)
    _fill(db, 80, direction=1, fwd=0.01, pair="A/USD", rule="1", sd=0.004)
    r = E.audit(db)["rules"]["1"]
    assert r["bucket"] == "keep"
    assert r["t"] >= 1.65


def test_min_t_is_the_default_and_is_not_loosened(tmp_path):
    assert E.DEFAULT_MIN_T == -1.65
    assert E.DEFAULT_MIN_N == 50


def test_inconclusive_bucket_exists_for_mixed_samples(tmp_path):
    """Net mean near zero with real spread and enough n -> neither keep nor drop.

    The fixture used to pass `fwd=0.0001`, which was "near zero" only under a
    gross reading. With the cost model the net mean is what buckets, so a gross
    mean of 0.01bps is actually a clear loss once the round trip is paid. The
    mean is therefore set to exactly one round trip: gross positive, net zero.
    """
    db = _db(tmp_path)
    _fill(db, 200, direction=1, fwd=E.round_trip_bps() / 10_000.0,
          pair="A/USD", rule="1", sd=0.05)
    r = E.audit(db)["rules"]["1"]
    assert r["bucket"] == "inconclusive"


# ── the audit must not grade rules it never evaluated ─────────────────────

def test_rules_with_no_rows_are_absent_not_scored(tmp_path):
    db = _db(tmp_path)
    _fill(db, 80, direction=1, fwd=0.01, pair="A/USD", rule="1")
    a = E.audit(db)
    assert "1" in a["rules"]
    assert "2" not in a["rules"], "a rule that never fired must not be graded"


# ── per-pair and per-hour breakdown ────────────────────────────────────────

def test_per_pair_breakdown_isolates_the_winner(tmp_path):
    db = _db(tmp_path)
    _fill(db, 80, direction=1, fwd=0.01, pair="GOOD/USD", rule="1", sd=0.004)
    _fill(db, 80, direction=1, fwd=-0.01, pair="BAD/USD", rule="1", sd=0.004)
    a = E.audit(db)
    assert a["pairs"]["GOOD/USD"]["bucket"] == "keep"
    assert a["pairs"]["BAD/USD"]["bucket"] == "drop"


def test_hour_cells_are_not_graded(tmp_path):
    # The PER LOCAL HOUR cells were retired 2026-10-11: hour-of-day buckets on
    # a days-long sample measure the session, not the rule, and they diluted
    # the Bonferroni threshold of every real cell. This test pins the
    # retirement so they cannot creep back in without a deliberate change.
    import time as _t
    db = _db(tmp_path)
    now = _t.time()
    ids = []
    for i in range(60):
        ids.append(RC.record_verdict(db, pair="A/USD", ts=now, direction=1,
                                     forward_return=None, horizon_bars=5,
                                     fill_price=1.0, venues_live=4,
                                     rules_met=["1"], rules_unevaluable=[],
                                     ref_venue="binance"))
    RC.resolve(db, ids=ids, return_by_pair={"A/USD": 0.01})
    a = E.audit(db)
    assert "hours" not in a
    # the Bonferroni count covers rules + pairs only
    assert a["n_tests"] == len(a["rules"]) + len(a["pairs"])


# ── rendering ──────────────────────────────────────────────────────────────

def test_render_says_insufficient_on_day_one(tmp_path):
    out = E.render(E.audit(_db(tmp_path)))
    assert "INSUFFICIENT DATA" in out
    assert "correct day-one answer" in out.lower() or "correct day-one" in out.lower()


def test_render_lists_both_sections(tmp_path):
    db = _db(tmp_path)
    _fill(db, 80, direction=1, fwd=0.01, pair="A/USD", rule="1", sd=0.004)
    out = E.render(E.audit(db))
    assert "PER RULE" in out and "PER PAIR" in out
    assert "PER LOCAL HOUR" not in out