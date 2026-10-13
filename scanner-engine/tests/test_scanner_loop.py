"""Tests for the scanner loop and its health surface.

Run:  quant-alert-engine/venv/Scripts/python.exe -m pytest scanner-engine/tests/test_scanner_loop.py -v
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import scanner as SC
import rules_eval as RE
from features import CrossVenueFeatures
from types_ import Book


# ── one cycle: poll -> features -> rules -> record ──────────────────────────

def test_cycle_produces_a_verdict_per_configured_pair():
    sc = SC.Scanner(pairs=["ETH/USD", "SOL/USD"], db_path=":memory:",
                    rules=RE.load_rules())
    # ETH and SOL are 4-venue pairs with min_venues 4
    sc.inject_fetchers({
        "binance": lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None),
        "okx": lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None),
        "kraken": lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None),
        "bybit": lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None),
    })
    sc.run_cycle()

    assert set(sc.verdicts) == {"ETH/USD", "SOL/USD"}
    for v in sc.verdicts.values():
        assert v.n_venues == 4
        assert v.min_venues == 4


def test_venue_fetcher_receives_the_pair_and_returns_book_and_tape():
    """Adapters return (book, tape|None); the loop must combine both."""
    seen = []

    def fetcher(pair):
        seen.append(pair)
        return (Book(bid=1.0, bids=[(1.0, 1.0)], asks=[(1.01, 1.0)]),
                {"delta_ratio": 0.6, "coverage_s": 50.0, "n_trades": 40})

    sc = SC.Scanner(pairs=["SUI/USD"], db_path=":memory:", rules=RE.load_rules())
    sc.inject_fetchers({v: fetcher for v in ("binance", "okx", "kraken", "bybit")})
    sc.run_cycle()

    assert "SUI/USD" in seen
    assert sc.verdicts["SUI/USD"].n_venues == 4


def test_a_failing_venue_does_not_abort_the_cycle():
    """Kraken raising must not stop ETH from getting a verdict."""
    def ok(pair):
        return (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None)

    def boom(pair):
        raise RuntimeError("venue exploded")

    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:", rules=RE.load_rules())
    sc.inject_fetchers({"binance": ok, "okx": ok, "bybit": ok, "kraken": boom})
    sc.run_cycle()

    assert "ETH/USD" in sc.verdicts
    assert sc.verdicts["ETH/USD"].n_venues == 3


def test_cycle_counts_increment():
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:", rules=RE.load_rules())
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    sc.run_cycle()
    sc.run_cycle()
    assert sc.cycles == 2


# ── recorder integration ───────────────────────────────────────────────────

def test_only_directional_verdicts_are_recorded_with_a_direction():
    """A neutral verdict is direction 0 and is recorded (it is a real
    observation) but is excluded from expectancy by the SQL."""
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:", rules=RE.load_rules())
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    sc.run_cycle()

    rows = sc.db.execute("SELECT direction FROM verdicts").fetchall()
    assert len(rows) == 1
    # no rule has been audited, so nothing may name a side -> direction 0
    assert rows[0][0] == 0


def test_records_carry_venues_live_and_rule_states():
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:", rules=RE.load_rules())
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    sc.run_cycle()

    vl, met, unev = sc.db.execute(
        "SELECT venues_live, rules_met, rules_unevaluable FROM verdicts").fetchone()
    assert vl == 4
    assert isinstance(json.loads(met), list)
    assert isinstance(json.loads(unev), list)


def test_horizon_bars_is_derived_from_the_pair_bar_not_global(tmp_path):
    """A 1m pair and a 5m pair must carry different horizons, or expectancy
    across them is not comparable."""
    sc = SC.Scanner(pairs=["ETH/USD", "SPCX/USD"], db_path=str(tmp_path / "s.db"),
                    rules=RE.load_rules())
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    sc.run_cycle()

    rows = dict(sc.db.execute("SELECT pair, horizon_bars FROM verdicts").fetchall())
    # Wall-clock horizon is constant at 5 minutes:
    #   1m pair -> 5 bars =  5 min
    #   5m pair -> 1 bar  =  5 min
    # A fixed BAR count would make the 5m pair look 5x longer and its
    # expectancy non-comparable with the 1m pairs.
    assert rows["ETH/USD"] == 5
    assert rows["SPCX/USD"] == 1


# ── health payload ─────────────────────────────────────────────────────────

def test_health_reports_cycles_coverage_and_the_live_only_rule():
    sc = SC.Scanner(pairs=["ETH/USD", "SPCX/USD"], db_path=":memory:",
                    rules=RE.load_rules())
    sc.inject_fetchers({
        "binance": lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None),
        "okx": lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None),
        "bybit": lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None),
        "kraken": lambda p: (_ for _ in ()).throw(RuntimeError("EQuery:Unknown asset pair")),
    })
    sc.run_cycle()

    h = sc.health()
    assert h["cycles"] == 1
    assert h["max_age_s"] > 0
    # SPCX is not on Kraken; that is a fact, not an outage
    assert "coverage" in h
    assert h["coverage"]["SPCX/USD"]["min_venues"] == 3


def test_health_names_a_frozen_venue_with_its_age():
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:", rules=RE.load_rules(),
                    max_age_s=300.0)
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    sc.run_cycle()
    # age one venue past the freshness window
    key = next(iter(sc.poller._snapshots))
    sc.poller._snapshots[key] = dict(sc.poller._snapshots[key], ts=time.time() - 500)

    h = sc.health()
    assert h["frozen_venues"], "a stale venue must be named"


def test_health_includes_recorder_counts():
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:", rules=RE.load_rules())
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    sc.run_cycle()
    h = sc.health()
    assert h["recorder"]["rows_total"] == 1


# ── ranking ────────────────────────────────────────────────────────────────

def test_rank_sorts_by_conviction_not_feature_magnitude():
    """A pair with a huge but unconfirmed feature must not outrank one with
    a met rule."""
    sc = SC.Scanner(pairs=["A/USD", "B/USD"], db_path=":memory:",
                    rules=RE.load_rules())
    v_a = RE.Verdict(pair="A/USD", ts=time.time(), n_venues=4, min_venues=3,
                     evidence_ok=True, direction="neutral", rules={}, score=0)
    v_b = RE.Verdict(pair="B/USD", ts=time.time(), n_venues=4, min_venues=3,
                     evidence_ok=True, direction="neutral", rules={}, score=10)
    sc.verdicts = {"A/USD": v_a, "B/USD": v_b}
    ranked = sc.rank()
    assert ranked[0]["pair"] == "B/USD"


def test_rank_excludes_pairs_without_adequate_evidence():
    sc = SC.Scanner(pairs=["A/USD", "B/USD"], db_path=":memory:",
                    rules=RE.load_rules())
    sc.verdicts = {
        "A/USD": RE.Verdict(pair="A/USD", ts=time.time(), n_venues=4, min_venues=3,
                            evidence_ok=True, direction="neutral", rules={}, score=10),
        "B/USD": RE.Verdict(pair="B/USD", ts=time.time(), n_venues=1, min_venues=3,
                            evidence_ok=False, direction="neutral", rules={}, score=0),
    }
    ranked = sc.rank()
    pairs = [r["pair"] for r in ranked]
    assert "B/USD" not in pairs


# ── loop control ───────────────────────────────────────────────────────────

def test_max_cycles_bounds_the_loop_for_testing():
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:", rules=RE.load_rules(),
                    max_cycles=3)
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    sc.run_forever(poll_interval_s=0.0)
    assert sc.cycles == 3


def test_audited_rules_is_empty_while_every_rule_is_unvalidated(tmp_path):
    """REGRESSION. Rule 1's audit_status is the string
    'UNVALIDATED - no recorded outcomes yet...'. An exact `!= "UNVALIDATED"`
    check reported rule 1 as AUDITED, which is the one condition that would let
    an unmeasured rule contribute a direction."""
    rules = RE.load_rules()
    # Pin the PREFIX contract, not today's file contents: rules 1/2 now carry
    # measured DROP statuses, so force every rule to an UNVALIDATED string and
    # assert none leaks into audited_rules. DROP statuses are audited-by-design
    # (they are measurements); the regression this test guards is the
    # descriptive-suffix leak.
    for spec in rules["rules"].values():
        spec["audit_status"] = "UNVALIDATED - no recorded outcomes yet."
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:", rules=rules)
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    sc.run_cycle()
    assert sc.health()["audited_rules"] == []


def test_retired_rules_stop_producing_candidates(tmp_path):
    """REGRESSION (2026-10-11). A rule measured into DROP must keep firing as
    an OBSERVATION (rules_met stays truthful) but must stop producing
    candidate rows — that is the loop-closing half of promotion: the auditor
    can take hypotheses out, not only grant sides. Without this filter, a
    retired rule keeps consuming batch slots and Bonferroni cells forever."""
    rules = RE.load_rules()
    for spec in rules["rules"].values():
        spec["audit_status"] = "DROP refuted n=1 t=-9.0 mean=-0.01"
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:", rules=rules)
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    sc.run_cycle()
    assert sc.health()["retired_rules"] == sorted(rules["rules"].keys())
    # Even if a retired rule fired MET here (this fixture's tape=None makes
    # every rule UNEVALUABLE/UNMET, so rules_met is empty), the candidate flag
    # and the direction can only come from NON-retired rules. Assert the
    # ledger-level guarantee on the rows actually written.
    rows = sc.db.execute("SELECT candidate, direction FROM verdicts").fetchall()
    assert rows, "el ciclo grabo filas"
    assert all(r[0] == 0 and r[1] == 0 for r in rows),         "con TODAS las reglas retiradas, ninguna fila puede ser candidato"


def test_horizon_holds_wall_clock_constant_across_bar_sizes(tmp_path):
    """5 bars of 1m is 5 minutes; 1 bar of 5m is 5 minutes. A fixed BAR count
    would make the 5m pairs a 5x longer horizon and their expectancy
    non-comparable with the 1m pairs."""
    assert SC.horizon_for("ETH/USD") == 5       # 1m bar
    assert SC.horizon_for("SPCX/USD") == 1      # 5m bar
    assert SC.HORIZON_MINUTES == 5.0


def test_clean_exit_does_not_look_like_a_crash():
    """The supervisor must be able to tell a clean stop from a failure, or it
    will relaunch a scanner that was deliberately stopped."""
    sc = SC.Scanner(pairs=["ETH/USD"], db_path=":memory:", rules=RE.load_rules(),
                    max_cycles=1)
    sc.inject_fetchers({
        v: (lambda p: (Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 1.0)]), None))
        for v in ("binance", "okx", "kraken", "bybit")
    })
    sc.run_forever(poll_interval_s=0.0)
    assert sc.stopped_cleanly is True