"""Measurement-integrity regressions.

Every test here pins a defect that was MEASURED on the running engine and then
fixed. They are grouped in one file because they share a single claim: a number
this engine reports is only as good as the measurement that produced it.

The defects, in the order they were found:

  1. The recorded fill was a depth-weighted mid across all venues while the
     exit was a single venue's close. On XAU that made a 30bps OKX-vs-Binance
     gold basis look like a -30bps loss on 599 of 599 rows, and the auditor
     read it as the only significant loser in the book.

  2. Overlapping rows were counted as independent evidence. The loop polls
     ~12s against a 5-minute horizon, so ~25 rows share one price move and
     |t| is inflated by about sqrt(25).

  3. Expectancy was quoted gross. Pooled gross mean was -0.90bps against a
     ~11bps round trip.

  4. No multiple-comparison correction across ~20 simultaneously graded cells.

  5. Rules 1, 2 and 3 could never reach MET, so the engine shipped four rules
     and could measure one.

  6. Declared thresholds that no code path read.

Run:  python -m pytest tests/test_measurement_integrity.py -v
"""
import json
import math
import pathlib
import random
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import expectancy as E
import features as F
import recorder as RC
import rules_eval as RE
import scanner as SC
from types_ import Book

RULES_PATH = pathlib.Path(__file__).resolve().parents[1] / "rules.json"


def _db(tmp_path, name="s.db"):
    return RC.open_db(str(tmp_path / name))


def _resolve(db, ids, vals):
    for rid, v in zip(ids, vals):
        RC.resolve(db, ids=[rid], return_by_pair={"A/USD": v})


def _series(n, mean, sd, rho=0.0, seed=3):
    """AR(1) draws with a given lag-1 autocorrelation, recentred on `mean`."""
    rng = random.Random(seed)
    out, prev = [], 0.0
    for _ in range(n):
        prev = rho * prev + math.sqrt(max(0.0, 1 - rho * rho)) * rng.gauss(0.0, sd)
        out.append(mean + prev)
    shift = mean - sum(out) / len(out)
    return [v + shift for v in out]


def _load(db, vals, *, direction=1, pair="A/USD", rule="4", basis_ok=True,
          spread_bps=1.0, ts0=None):
    import time as _t
    ts0 = ts0 if ts0 is not None else _t.time()
    ids = [RC.record_verdict(db, pair=pair, ts=ts0 + i, direction=direction,
                             forward_return=None, horizon_bars=5,
                             fill_price=100.0, venues_live=4,
                             rules_met=[rule], rules_unevaluable=[],
                             candidate=True, ref_venue="binance",
                             fill_spread_bps=spread_bps, price_basis_ok=basis_ok)
           for i in range(len(vals))]
    _resolve(db, ids, vals)
    return ids


# ── 1. the fill venue must be the exit venue ────────────────────────────────

def test_fill_is_the_reference_venue_mid_not_a_depth_weighted_cross_venue_blend(tmp_path):
    """THE XAU DEFECT.

    OKX quotes ~287,000 units of XAU depth against Binance's ~7, so a
    depth-weighted mid was 99.99% the OKX price. The exit was Binance's close.
    The recorded "forward return" was therefore the OKX-Binance gold basis,
    sign-flipped, and it produced the engine's only significant loser.

    The fill must now be ONE venue's mid, and the row must say which one.
    """
    # OKX trades 100 points away from Binance and carries 100000x the depth.
    okx = Book(bid=300.0, bids=[(300.0, 100000.0)], asks=[(300.2, 100000.0)])
    binance = Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)])
    others = Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)])

    sc = SC.Scanner(pairs=["ETH/USD"], db_path=str(tmp_path / "s.db"),
                    rules=RE.load_rules(RULES_PATH))
    sc.inject_fetchers({
        "binance": lambda p: (binance, None),
        "okx": lambda p: (okx, None),
        "kraken": lambda p: (others, None),
        "bybit": lambda p: (others, None),
    })
    sc.run_cycle()

    price, venue, spread = sc.db.execute(
        "SELECT fill_price, ref_venue, fill_spread_bps FROM verdicts").fetchone()
    assert venue == "binance", "the fill must be attributed to one venue"
    assert abs(price - binance.mid) < 1e-9, (
        f"fill {price} is not Binance's mid {binance.mid}; a depth-weighted "
        f"blend would have been pulled to OKX's 300.1")
    assert abs(spread - binance.spread_bps) < 1e-9
    # the blended value we must NOT be recording
    assert price < 150.0


def test_the_resolver_scores_the_exit_on_the_same_venue_it_filled_from(tmp_path):
    """If the exit could fall back to another venue, the mismatch would return
    the first time the reference venue hiccups."""
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:",
                    rules=RE.load_rules(RULES_PATH))
    sc._install_bar_fetchers()
    calls = []

    def bn(pair, bar, at_ms):
        calls.append("binance")
        return {"close": 100.0, "close_ms": at_ms}

    sc._bar_fetch = {"binance": bn, "okx": lambda *a: calls.append("okx"),
                     "kraken": lambda *a: calls.append("kraken"),
                     "bybit": lambda *a: calls.append("bybit")}
    bar = sc._fetch_horizon_bar("ETH/USD", int(1e12))
    assert calls == ["binance"], f"exit fetched from {calls}, not the fill venue"
    assert bar is not None


def test_a_pair_whose_reference_venue_is_absent_is_not_filled_from_another(tmp_path):
    """Losing the measurement beats measuring the wrong thing."""
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:",
                    rules=RE.load_rules(RULES_PATH))
    sc._install_bar_fetchers()
    called = []
    sc._bar_fetch = {"okx": lambda *a: called.append("okx"),
                     "bybit": lambda *a: called.append("bybit"),
                     "kraken": lambda *a: called.append("kraken"),
                     "binance": lambda *a: (_ for _ in ()).throw(RuntimeError("down"))}
    assert sc._fetch_horizon_bar("ETH/USD", int(1e12)) is None
    assert called == [], "must not fall back to another venue's close"


# ── 2. rows measured on an unknown basis never enter the math ───────────────

def test_rows_with_an_untrustworthy_price_basis_are_excluded(tmp_path):
    """Pre-fix rows filled on one basis and scored on another. They are kept,
    not deleted, and excluded from expectancy — exactly like direction=0."""
    db = _db(tmp_path)
    _load(db, _series(200, 0.0005, 0.002), basis_ok=False)
    assert E.audit(db)["verdict"] == "INSUFFICIENT DATA"
    assert E.by_rule(db) == {}
    assert RC.counts(db)["rows_bad_price_basis"] == 200
    assert RC.counts(db)["rows_total"] == 200, "rows are retained, not destroyed"


def test_migrating_an_old_database_marks_its_rows_as_bad_basis(tmp_path):
    """A database that predates the fill/exit fix must not silently re-enter
    the math with the new columns defaulted to trustworthy."""
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(path))
    legacy.execute("""CREATE TABLE verdicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, pair TEXT NOT NULL,
        direction INTEGER NOT NULL, venues_live INTEGER NOT NULL,
        rules_met TEXT NOT NULL, rules_unevaluable TEXT NOT NULL,
        score INTEGER NOT NULL DEFAULT 0, horizon_bars INTEGER NOT NULL,
        fill_price REAL, forward_return REAL, resolved INTEGER NOT NULL DEFAULT 0,
        bar_closed INTEGER NOT NULL DEFAULT 1)""")
    legacy.execute("INSERT INTO verdicts (ts,pair,direction,venues_live,rules_met,"
                   "rules_unevaluable,horizon_bars,fill_price,forward_return,resolved)"
                   " VALUES (1.0,'ETH/USD',1,4,'[\"4\"]','[]',5,100.0,0.01,1)")
    legacy.commit()
    legacy.close()

    db = RC.open_db(str(path))
    assert RC.counts(db)["rows_bad_price_basis"] == 1
    assert db.execute("SELECT price_basis_ok FROM verdicts").fetchone()[0] == 0
    assert E.audit(db)["verdict"] == "INSUFFICIENT DATA"


def test_a_row_without_a_recorded_fill_venue_is_never_auditable(tmp_path):
    """ROLLING-UPGRADE HAZARD.

    `price_basis_ok` has a DEFAULT of 1. A scanner.py still running in memory
    from before the fix inserts rows that claim a trustworthy price basis while
    recording no venue at all, and they sailed straight into the expectancy math
    — nine phantom "auditable" rows were observed this way during the upgrade.

    The gate must therefore be derived from the EVIDENCE (a venue was recorded),
    not merely from the writer's assertion.
    """
    db = _db(tmp_path)
    rid = RC.record_verdict(db, pair="A/USD", ts=1.0, direction=1,
                           forward_return=None, horizon_bars=5,
                           fill_price=100.0, venues_live=4,
                           rules_met=["4"], rules_unevaluable=[],
                           candidate=True)
    # simulate the pre-fix writer: it never set ref_venue
    assert db.execute("SELECT ref_venue FROM verdicts WHERE id=?",
                      (rid,)).fetchone()[0] is None
    assert db.execute("SELECT price_basis_ok FROM verdicts WHERE id=?",
                      (rid,)).fetchone()[0] == 1, "the DEFAULT claims soundness"
    RC.resolve(db, ids=[rid], return_by_pair={"A/USD": 0.01})

    assert RC.counts(db)["rows_auditable"] == 0, (
        "a row with no recorded fill venue cannot have a trustworthy basis")
    assert RC.counts(db)["rows_bad_price_basis"] == 1
    assert E.by_rule(db) == {}
    assert E.audit(db)["verdict"] == "INSUFFICIENT DATA"


def test_a_row_that_records_its_fill_venue_is_auditable(tmp_path):
    db = _db(tmp_path)
    _load(db, _series(200, 0.0001, 0.002), basis_ok=True)
    assert RC.counts(db)["rows_auditable"] == 200
    assert E.by_rule(db)["4"]


def test_new_rows_are_marked_good_basis_by_default(tmp_path):
    db = _db(tmp_path)
    rid = RC.record_verdict(db, pair="A/USD", ts=1.0, direction=1,
                           forward_return=None, horizon_bars=5,
                           fill_price=100.0, venues_live=4,
                           rules_met=["4"], rules_unevaluable=[],
                           ref_venue="binance")
    assert db.execute("SELECT price_basis_ok FROM verdicts WHERE id=?",
                      (rid,)).fetchone()[0] == 1


# ── 3. overlapping rows are not independent evidence ────────────────────────

def test_heavily_overlapping_rows_do_not_reach_significance(tmp_path):
    """rho=0.8 means consecutive rows share ~80% of their variance.

    The naive t on this sample is comfortably past 1.65. The effective-n t is
    not, and the rule must NOT be graded on the naive one — this is precisely
    how the engine reported rule 4 as a significant loser at t=-2.13 from 721
    rows that contained about 305 independent observations.
    """
    db = _db(tmp_path)
    _load(db, _series(400, 0.0006, 0.0015, rho=0.8))
    r = E.audit(db)["rules"]["4"]

    assert abs(r["t_naive"]) > 1.65, "fixture should trip the naive threshold"
    assert r["autocorr"] > 0.4
    assert r["n_eff"] < r["n"] * 0.7, "effective n must shrink for an AR(1) sample"
    assert abs(r["t"]) < abs(r["t_naive"])
    assert r["bucket"] in ("inconclusive", "insufficient"), (
        f"overlapping evidence graded {r['bucket']} on t_naive={r['t_naive']:.2f}")


def test_the_same_mean_on_independent_rows_does_grade(tmp_path):
    """The control. Without this, the test above would pass merely because the
    auditor is broken in the other direction."""
    db = _db(tmp_path)
    _load(db, _series(400, 0.0006, 0.0015, rho=0.0))
    r = E.audit(db)["rules"]["4"]
    assert r["autocorr"] < 0.15
    assert r["n_eff"] > r["n"] * 0.85
    # with no overlap to correct for, the two statistics must agree
    assert abs(r["t"] - r["t_naive"]) < 0.05 * max(1.0, abs(r["t_naive"]))


def test_effective_n_is_bounded_by_the_sample_size():
    assert E.effective_n(_series(50, 0.0, 0.01, rho=0.0))[0] <= 50
    assert E.effective_n(_series(50, 0.0, 0.01, rho=0.95))[0] < 15
    assert E.effective_n([0.1, 0.2])[0] >= 1.0


# ── 4. costs are not assumed away ───────────────────────────────────────────

def test_round_trip_cost_matches_this_repos_established_figure():
    """The sibling engine's audit used 0.11% for a Binance perp round trip.

    The fee+slip component here is that same 11.0bps. On top of it sits one
    full venue spread, because these returns are measured mid-to-mid and a
    real fill crosses half the spread on each side.
    """
    fee_slip = 2 * (E.FEE_BPS_PER_SIDE + E.SLIPPAGE_BPS_PER_SIDE)
    assert abs(fee_slip - 11.0) < 1e-9, "must stay comparable with STATUS.md"
    assert abs(E.round_trip_bps(1.0) - 12.0) < 1e-9
    assert abs(E.round_trip_bps(0.0) - 11.0) < 1e-9


def test_a_gross_positive_signal_that_cannot_pay_the_fee_is_a_drop(tmp_path):
    """The whole point of measuring net.

    Gross mean here is +3.0bps per signal, comfortably positive. The round trip
    is ~11bps. Trading it loses money, and a scanner that says "keep" here is
    telling the user to do the exact thing that costs them 8bps a time.
    """
    db = _db(tmp_path)
    _load(db, _series(600, 0.0003, 0.002, rho=0.0))
    r = E.audit(db)["rules"]["4"]
    assert r["mean_gross"] > 0
    assert r["mean"] < 0, "net mean must be negative"
    assert r["bucket"] == "drop"


def test_a_signal_that_beats_the_fee_is_reported_as_a_keep(tmp_path):
    db = _db(tmp_path)
    _load(db, _series(600, 0.0040, 0.006, rho=0.0))
    r = E.audit(db)["rules"]["4"]
    assert r["mean_gross"] > 0 and r["mean"] > 0
    assert r["bucket"] == "keep"


def test_cost_uses_the_rows_own_measured_spread(tmp_path):
    """The spread is measured per row, not assumed uniform across venues —
    they differ by up to 100x on this book."""
    assert E.round_trip_bps(0.024) < E.round_trip_bps(2.391)


# ── 5. multiple comparisons ─────────────────────────────────────────────────

def test_the_threshold_widens_as_more_cells_are_graded():
    """Grading 20 cells at a nominal 5% and calling anything past 1.65 a
    finding produces about one false positive per pass, by construction."""
    assert E._bonferroni_t(1) < 1.75
    assert E._bonferroni_t(20) > E._bonferroni_t(1)
    assert E._bonferroni_t(20) > 2.5
    assert E._bonferroni_t(40) > E._bonferroni_t(20)


def test_the_audit_reports_how_many_cells_it_graded(tmp_path):
    """A correction nobody can see is a correction that gets removed."""
    db = _db(tmp_path)
    _load(db, _series(300, 0.0001, 0.002, rho=0.0), pair="A/USD")
    _load(db, _series(300, 0.0001, 0.002, rho=0.0, seed=9), pair="B/USD")
    a = E.audit(db)
    assert a["n_tests"] >= 2
    assert a["min_t"] == E._bonferroni_t(a["n_tests"])
    assert a["min_t"] > abs(E.DEFAULT_MIN_T)


def test_a_marginal_result_at_the_uncorrected_threshold_is_not_a_finding(tmp_path):
    """A result landing BETWEEN the bare 1.65 constant and the Bonferroni bar
    must not be graded as a finding in either direction.

    The fixture is built to land in that band rather than approximately near
    it: the net mean is set to `t_target * sd/sqrt(n)` with t_target chosen
    between the two thresholds, and the gross mean adds back exactly one round
    trip. Without that the test would be asserting on whatever the seed
    happened to produce.
    """
    n, sd = 300, 0.0015
    bar = E._bonferroni_t(4)
    t_target = (abs(E.DEFAULT_MIN_T) + bar) / 2.0
    net_target = t_target * (sd / math.sqrt(n))
    gross = net_target + E.round_trip_bps(1.0) / 10_000.0

    db = _db(tmp_path)
    _load(db, _series(n, gross, sd, rho=0.0), pair="A/USD")
    _load(db, _series(n, gross, sd, rho=0.0, seed=11), pair="B/USD")
    a = E.audit(db)

    in_band = 0
    for key in ("rules", "pairs"):
        for k, r in a[key].items():
            if r["t"] is None:
                continue
            if abs(E.DEFAULT_MIN_T) < abs(r["t"]) < bar:
                in_band += 1
                assert r["bucket"] in ("inconclusive", "insufficient"), (
                    f"{key}/{k} graded {r['bucket']} at t={r['t']:.2f}, inside "
                    f"the band ({abs(E.DEFAULT_MIN_T):.2f}, {bar:.2f}) the "
                    f"correction exists to reject")
    assert in_band > 0, "fixture did not land in the band; test would be vacuous"


# ── 6. min_n applies to EFFECTIVE n, not raw rows ───────────────────────────

def test_min_n_is_applied_to_effective_n(tmp_path):
    db = _db(tmp_path)
    _load(db, _series(400, 0.0002, 0.0015, rho=0.95))
    r = E.audit(db)["rules"]["4"]
    assert r["n"] >= 50
    assert r["n_eff"] < 50
    assert r["bucket"] == "insufficient"
    assert "n_eff" in r["why"]


# ── 7. the three dead rules can now fire ────────────────────────────────────

def _feat(**kw):
    base = dict(n_venues=4, n_venues_with_tape=4, basis_bps={}, basis_z={},
                basis_dispersion_bps=None, dispersion_in_spreads=None,
                flow_by_venue={}, flow_divergence=None, flow_median=None,
                touch_imbalance={}, spread_bps={}, price_move_bps={},
                depth_total={}, thin_coverage=[], missing_venue_data=[])
    base.update(kw)
    return F.CrossVenueFeatures(**base)


def test_rule1_reaches_met_once_the_dislocation_persists():
    """Rule 1 condition (b) is about persistence. `evaluate()` is a pure
    function, so it needed a `history` the loop never supplied — and the rule
    could never fire for the life of the engine."""
    rules = RE.load_rules(RULES_PATH)
    hist = RE.RuleHistory()
    thr = rules["rules"]["1"]["thresholds"]["z_threshold"]
    hot = _feat(n_venues=3, basis_z={"okx": thr + 4.0}, spread_bps={"okx": 0.24})

    # cycle 1: no prior state -> condition (b) cannot be satisfied yet
    v1 = RE.evaluate(hot, "ETH/USD", 3, rules, history=hist.snapshot("ETH/USD"))
    assert v1.rules["1"].state != "MET"
    hist.observe("ETH/USD", hot, z_threshold=thr)

    v2 = RE.evaluate(hot, "ETH/USD", 3, rules, history=hist.snapshot("ETH/USD"))
    hist.observe("ETH/USD", hot, z_threshold=thr)

    v3 = RE.evaluate(hot, "ETH/USD", 3, rules, history=hist.snapshot("ETH/USD"))
    assert v3.rules["1"].state == "MET", (
        "rule 1 still cannot fire; condition (b) never sees persistence")
    assert v3.rules["1"].subconditions["b"].state == "MET"


def test_rule1_streak_resets_when_the_dislocation_clears():
    rules = RE.load_rules(RULES_PATH)
    hist = RE.RuleHistory()
    thr = rules["rules"]["1"]["thresholds"]["z_threshold"]
    hot = _feat(n_venues=3, basis_z={"okx": thr + 4.0}, spread_bps={"okx": 0.24})
    cold = _feat(n_venues=3, basis_z={"okx": 0.01}, spread_bps={"okx": 0.24})

    hist.observe("ETH/USD", hot, z_threshold=thr)
    hist.observe("ETH/USD", cold, z_threshold=thr)      # streak must break
    hist.observe("ETH/USD", hot, z_threshold=thr)
    v = RE.evaluate(hot, "ETH/USD", 3, rules, history=hist.snapshot("ETH/USD"))
    assert v.rules["1"].state != "MET", "a cleared dislocation reset the streak"


def test_rule2_reaches_met_when_the_deviating_venue_does_not_move():
    """Condition (b) was a hard-coded UNEVALUABLE for the engine's whole life,
    which made rule 2 structurally impossible to fire."""
    rules = RE.load_rules(RULES_PATH)
    # kraken's tape is far from the median; its price barely moved
    f = _feat(n_venues=4, flow_median=0.865,
              flow_by_venue={"binance": 0.95, "okx": 0.85, "kraken": 0.10,
                             "bybit": 0.88},
              price_move_bps={"binance": 40.0, "okx": 35.0, "kraken": 0.2,
                              "bybit": 30.0})
    v = RE.evaluate(f, "SUI/USD", 3, rules)
    assert v.rules["2"].subconditions["a"].state == "MET"
    assert v.rules["2"].subconditions["b"].state == "MET"
    assert v.rules["2"].state == "MET"


def test_rule2_is_unmet_when_the_deviating_venue_actually_moves():
    """Effort with a matching result is not absorption. Reporting MET here
    would invert the rule's entire claim."""
    rules = RE.load_rules(RULES_PATH)
    f = _feat(n_venues=4, flow_median=0.865,
              flow_by_venue={"binance": 0.95, "okx": 0.85, "kraken": 0.10,
                             "bybit": 0.88},
              price_move_bps={"binance": 40.0, "okx": 35.0, "kraken": 90.0,
                              "bybit": 30.0})
    v = RE.evaluate(f, "SUI/USD", 3, rules)
    assert v.rules["2"].subconditions["b"].state == "UNMET"
    assert v.rules["2"].state == "UNMET"


def test_rule2_stays_unevaluable_without_a_price_leg():
    """"We could not check" must never be reported as "no"."""
    rules = RE.load_rules(RULES_PATH)
    f = _feat(n_venues=4, flow_median=0.865,
              flow_by_venue={"binance": 0.95, "okx": 0.85, "kraken": 0.10,
                             "bybit": 0.88})
    v = RE.evaluate(f, "SUI/USD", 3, rules)
    assert v.rules["2"].state == "UNEVALUABLE"


def test_rule3_reaches_met_when_depth_falls_below_its_own_trailing_median():
    """Condition (b) needs a trailing baseline; it previously had no code path
    to ever be MET, because nothing populated the depth history."""
    rules = RE.load_rules(RULES_PATH)
    hist = RE.RuleHistory()
    # The baseline needs REAL dispersion. A depth history that never varies has
    # a stdev of zero, and a z-score against it is undefined rather than large.
    for i in range(RE.MIN_DEPTH_POINTS + 5):
        per = 100.0 + (i % 5) * 4.0
        hist.observe("ETH/USD", _feat(
            n_venues=3, touch_imbalance={"binance": 0.95, "okx": 0.96,
                                          "bybit": 0.94},
            depth_total={v: per for v in ("binance", "okx", "bybit")}))

    # liquidity leaves: depth collapses while the books stay bid-heavy
    thin = _feat(n_venues=3, touch_imbalance={"binance": 0.95, "okx": 0.96,
                                              "bybit": 0.94},
                depth_total={"binance": 5.0, "okx": 5.0, "bybit": 5.0})
    v = RE.evaluate(thin, "ETH/USD", 3, rules, history=hist.snapshot("ETH/USD"))
    assert v.rules["3"].subconditions["a"].state == "MET"
    assert v.rules["3"].subconditions["b"].state == "MET"
    assert v.rules["3"].state == "MET"


def test_rule3_is_unevaluable_when_the_depth_history_has_no_dispersion():
    """A perfectly flat depth history gives stdev 0. Dividing by it would
    manufacture an enormous z from nothing; the honest answer is that the
    baseline is degenerate."""
    rules = RE.load_rules(RULES_PATH)
    hist = RE.RuleHistory()
    flat = _feat(n_venues=3, touch_imbalance={"binance": 0.95, "okx": 0.96,
                                              "bybit": 0.94},
                 depth_total={"binance": 100.0, "okx": 100.0, "bybit": 100.0})
    for _ in range(RE.MIN_DEPTH_POINTS + 5):
        hist.observe("ETH/USD", flat)
    thin = _feat(n_venues=3, touch_imbalance={"binance": 0.95, "okx": 0.96,
                                              "bybit": 0.94},
                 depth_total={"binance": 5.0, "okx": 5.0, "bybit": 5.0})
    v = RE.evaluate(thin, "ETH/USD", 3, rules, history=hist.snapshot("ETH/USD"))
    assert v.rules["3"].subconditions["b"].state == "UNEVALUABLE"
    assert v.rules["3"].state == "UNEVALUABLE"


def test_rule3_is_unevaluable_until_it_has_enough_depth_history():
    """A trailing median over five points has no usable dispersion; dividing
    by a near-zero stdev would manufacture an enormous z from rounding noise."""
    rules = RE.load_rules(RULES_PATH)
    hist = RE.RuleHistory()
    thin = _feat(n_venues=3, touch_imbalance={"binance": 0.95, "okx": 0.96,
                                              "bybit": 0.94},
                 depth_total={"binance": 5.0, "okx": 5.0, "bybit": 5.0})
    for _ in range(3):
        hist.observe("ETH/USD", thin)
    v = RE.evaluate(thin, "ETH/USD", 3, rules, history=hist.snapshot("ETH/USD"))
    assert v.rules["3"].subconditions["b"].state == "UNEVALUABLE"
    assert v.rules["3"].state == "UNEVALUABLE"


def test_the_scanner_advances_history_so_rules_are_not_permanently_dead(tmp_path):
    """End-to-end: after enough cycles the loop must be able to produce a MET
    on a rule other than 4. The engine shipped four rules and measured one."""
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=str(tmp_path / "s.db"),
                    rules=RE.load_rules(RULES_PATH))
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)],
                             asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    for _ in range(5):
        sc.run_cycle()
    assert sc.history.depth_median("ETH/USD") is not None
    assert len(sc.history.snapshot("ETH/USD")["3"]["depth_history"]) == 5


# ── 8. declared thresholds must be binding ─────────────────────────────────

def test_the_tape_coverage_floor_comes_from_rules_json():
    """Rule 4 declares min_coverage_s and the engine must use it.

    It declared 30.0, which no code path read and no venue could deliver; it is
    now 5.0, measured. A config entry that does not change behaviour is a false
    contract, and one that is enforced but unachievable is a different false
    contract: it makes the rule permanently UNEVALUABLE and reads as
    "insufficient data" rather than "this threshold is impossible".
    """
    rules = RE.load_rules(RULES_PATH)
    assert rules["rules"]["4"]["thresholds"]["min_coverage_s"] == 5.0
    assert RE.coverage_floor(rules) == 5.0


def test_the_trade_count_floor_comes_from_rules_json():
    """A delta_ratio from fewer trades than this is a coin-flip, and a
    span-only gate cannot catch it: Kraken returns as few as 4 trades over 25s
    on thin pairs and would sail past any span threshold."""
    rules = RE.load_rules(RULES_PATH)
    assert rules["rules"]["4"]["thresholds"]["min_trades"] == 50
    assert RE.trade_floor(rules) == 50


def test_a_thin_but_wide_tape_is_still_rejected():
    """Kraken's measured shape on a thin pair: 4 trades spanning 25s.

    A span-only gate accepts this. It must be rejected, because a buy/sell
    ratio from four trades can only be 0, 0.25, 0.5, 0.75 or 1.
    """
    okx = Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)])
    snaps = {
        "binance": {"book": okx, "tape": {"delta_ratio": 0.9, "coverage_s": 60.0,
                                           "n_trades": 400}},
        "kraken": {"book": okx, "tape": {"delta_ratio": 0.1, "coverage_s": 25.0,
                                          "n_trades": 4}},
    }
    f = F.compute(snaps, min_coverage_s=5.0, min_trades=50)
    assert "binance" in f.flow_by_venue
    assert "kraken" not in f.flow_by_venue
    assert "kraken" in f.thin_coverage


def test_a_dense_but_short_tape_is_accepted():
    """OKX's measured shape: 500 trades over 5.8s. Plenty of observations."""
    okx = Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)])
    snaps = {"okx": {"book": okx, "tape": {"delta_ratio": 0.7, "coverage_s": 5.8,
                                            "n_trades": 500}}}
    f = F.compute(snaps, min_coverage_s=5.0, min_trades=50)
    assert f.flow_by_venue["okx"] == 0.7


def test_a_venue_below_the_declared_floor_is_excluded_from_flow():
    okx = Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)])
    snaps = {
        "binance": {"book": okx, "tape": {"delta_ratio": 0.9, "coverage_s": 60.0,
                                           "n_trades": 50}},
        "okx": {"book": okx, "tape": {"delta_ratio": 0.9, "coverage_s": 2.0,
                                        "n_trades": 3}},
    }
    f = F.compute(snaps, min_coverage_s=30.0)
    assert "binance" in f.flow_by_venue
    assert "okx" not in f.flow_by_venue
    assert "okx" in f.thin_coverage


def test_rule2_reads_its_absorption_threshold_from_the_spec():
    """max_price_response_bps came from rules.json; hardcoding it would make
    the entry decorative the moment anyone tuned it."""
    rules = RE.load_rules(RULES_PATH)
    thr = rules["rules"]["2"]["thresholds"]["max_price_response_bps"]
    f = _feat(n_venues=4, flow_median=0.5,
              flow_by_venue={"binance": 0.5, "okx": 0.5, "kraken": 0.05,
                             "bybit": 0.5},
              price_move_bps={"binance": 0.0, "okx": 0.0, "kraken": thr + 1.0,
                              "bybit": 0.0})
    v = RE.evaluate(f, "SUI/USD", 3, rules)
    assert v.rules["2"].subconditions["b"].state == "UNMET"


# ── 9. promotion must not be more permissive than the auditor ──────────────

def test_promotion_refuses_a_significant_loser():
    import promotion as PROM
    spec = {"audit_status": "UNVALIDATED",
            "direction_hint": {"side": "follow"}}
    ok, why = PROM.promote_if_earned(
        {"n": 500, "n_eff": 500, "mean": -0.0002, "t": -3.0}, spec, 50, 2.5)
    assert ok is False and "REFUTED" in why


def test_promotion_refuses_noise_that_happens_to_have_a_positive_mean():
    """The old test granted on `t >= -min_t`, which with a positive threshold
    is true for essentially every sample."""
    import promotion as PROM
    spec = {"audit_status": "UNVALIDATED",
            "direction_hint": {"side": "follow"}}
    ok, why = PROM.promote_if_earned(
        {"n": 500, "n_eff": 500, "mean": 0.0001, "t": 0.4}, spec, 50, 2.5)
    assert ok is False, why


def test_promotion_grants_only_a_clear_positive_result():
    import promotion as PROM
    spec = {"audit_status": "UNVALIDATED",
            "direction_hint": {"side": "follow"}}
    ok, why = PROM.promote_if_earned(
        {"n": 500, "n_eff": 500, "mean": 0.004, "t": 3.4}, spec, 50, 2.5)
    assert ok is True and "EARNED" in why


def test_promotion_applies_the_same_bar_the_auditor_used(tmp_path):
    """The gate must not be looser than the measurement that feeds it."""
    db = _db(tmp_path)
    _load(db, _series(600, 0.004, 0.006, rho=0.0))
    a = E.audit(db)
    assert a["rules"]["4"]["bucket"] == "keep"
    import promotion as PROM
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    report = PROM.promotion_report(db, rules, a)
    r4 = next(r for r in report if r["rule"] == "4")
    assert r4["state"] == "PROMOTE"
    assert r4["n_eff"] == a["rules"]["4"]["n_eff"]


def test_promotion_requires_the_auditors_threshold_not_the_bare_constant(tmp_path):
    db = _db(tmp_path)
    _load(db, _series(600, 0.0004, 0.002, rho=0.0))
    a = E.audit(db)
    import promotion as PROM
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    report = PROM.promotion_report(db, rules, a)
    r4 = next(r for r in report if r["rule"] == "4")
    # t clears the bare 1.65 constant but not the Bonferroni bar
    assert r4["t"] is not None
    if abs(r4["t"]) > abs(E.DEFAULT_MIN_T):
        assert r4["t"] < a["min_t"]
        assert r4["state"] != "PROMOTE"


# ── 10. the audit has to be runnable ────────────────────────────────────────

def test_expectancy_default_db_path_points_at_the_real_database():
    """`python expectancy.py` printed 'the recorder has not run yet' on an
    engine that had been recording for 42 hours, because the default path
    omitted data/."""
    import inspect
    src = inspect.getsource(E)
    assert '"data" / "scanner.db"' in src


def test_health_names_the_reference_venue_for_every_pair(tmp_path):
    """If the fill venue is not visible, a mismatch can only be found by
    reading the code."""
    sc = SC.Scanner(pairs=["ETH/USD", "SPCX/USD"], db_path=":memory:",
                    rules=RE.load_rules(RULES_PATH))
    h = sc.health()
    assert h["reference_venues"] == {"ETH/USD": "binance", "SPCX/USD": "binance"}
    assert h["tape_coverage_floor_s"] == 5.0
    assert h["tape_trade_floor"] == 50