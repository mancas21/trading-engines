"""Tests for the rule evaluator — the MET / UNMET / UNEVALUABLE contract.

Run:  quant-alert-engine/venv/Scripts/python.exe -m pytest scanner-engine/tests/test_rules_eval.py -v
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import rules_eval as RE
from features import CrossVenueFeatures

RULES_PATH = pathlib.Path(__file__).resolve().parents[1] / "rules.json"


def _f(**kw) -> CrossVenueFeatures:
    base = dict(
        n_venues=4, n_venues_with_tape=4,
        basis_bps={}, basis_z={},
        basis_dispersion_bps=None, dispersion_in_spreads=None,
        flow_by_venue={}, flow_divergence=None, flow_median=None,
        touch_imbalance={}, spread_bps={},
        thin_coverage=[], missing_venue_data=[],
    )
    base.update(kw)
    return CrossVenueFeatures(**base)


# ── the three-state contract ───────────────────────────────────────────────

def test_all_rules_unevaluable_when_no_venues():
    """No data must not read as 'no signal'."""
    v = RE.evaluate(_f(n_venues=0), "SUI/USD", min_venues=3, rules=RE.load_rules(RULES_PATH))
    assert v.evidence_ok is False
    assert set(v.evaluable_states) <= {"UNEVALUABLE"}
    assert v.direction == "neutral"
    assert v.reduced_evidence


def test_rule_with_two_venues_is_unevaluable_when_three_required():
    """THE REGRESSION. Rules 3 and 4 declare requires_venues 3. A 2-venue
    reading of a 3-venue condition is a DIFFERENT and weaker claim; it must be
    UNEVALUABLE, never silently evaluated."""
    r = RE.load_rules(RULES_PATH)
    v = RE.evaluate(_f(n_venues=2,
                       basis_z={"binance": 0.0, "okx": 0.0},
                       basis_dispersion_bps=None,
                       n_venues_with_tape=2,
                       flow_by_venue={"binance": 0.7, "okx": 0.7},
                       flow_median=0.7, flow_divergence=0.0),
                  "SUI/USD", min_venues=2, rules=r)

    assert v.rules["3"].state == "UNEVALUABLE"
    assert v.rules["3"].reason == "requires 3 live venue(s), 2 available"
    assert v.rules["4"].state == "UNEVALUABLE"
    # rule 1 needs only 2, so the floor does NOT block it
    assert v.rules["1"].state != "UNEVALUABLE"


def test_three_venue_rule_is_evaluable_at_three_venues():
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=3, n_venues_with_tape=3,
           touch_imbalance={"binance": 0.9, "okx": 0.9, "bybit": 0.9},
           flow_by_venue={"binance": 0.8, "okx": 0.8, "bybit": 0.8},
           flow_divergence=0.01, flow_median=0.8)
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r)
    # rule 4 is fully evaluable on this data
    assert v.rules["4"].state == "MET"
    # rule 3's (a) evaluates, but (b) needs a trailing depth median that does
    # not exist yet -> the rule is UNEVALUABLE, and (a) must still be reported
    assert v.rules["3"].subconditions["a"].state == "MET"
    assert v.rules["3"].state == "UNEVALUABLE"
    assert v.rules["3"].subconditions["b"].state == "UNEVALUABLE"


def test_unevaluable_is_not_unmet():
    """A rule with no data must never be reported as a clean negative."""
    r = RE.load_rules(RULES_PATH)
    v = RE.evaluate(_f(n_venues=0), "SUI/USD", min_venues=3, rules=r)
    assert v.rules["1"].state == "UNEVALUABLE"
    assert v.rules["1"].state != "UNMET"


# ── rule 1: basis dislocation ──────────────────────────────────────────────

def test_rule1_met_when_z_exceeds_threshold_and_persists():
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=4, basis_z={"binance": 0.0, "okx": 0.0, "kraken": 40.0, "bybit": 0.0},
           basis_dispersion_bps=30.0, dispersion_in_spreads=100.0)
    hist = {"1": {"z_max": 9.0, "cycles": 2}}          # already large last cycle
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r, history=hist)
    assert v.rules["1"].state == "MET"


def test_rule1_unmet_when_z_is_small():
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=4, basis_z={"binance": 0.0, "okx": 0.1, "kraken": -0.2, "bybit": 0.0},
           basis_dispersion_bps=0.4, dispersion_in_spreads=1.0)
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r,
                    history={"1": {"z_max": 0.1, "cycles": 3}})
    assert v.rules["1"].state == "UNMET"


def test_rule1_unmet_when_large_but_not_persistent():
    """A single-cycle spike is exactly the noise a persistence band exists to
    reject."""
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=4, basis_z={"binance": 0.0, "okx": 0.0, "kraken": 40.0, "bybit": 0.0},
           basis_dispersion_bps=30.0, dispersion_in_spreads=100.0)
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r,
                    history={"1": {"z_max": 0.2, "cycles": 1}})   # not consecutive
    assert v.rules["1"].state == "UNMET"


def test_rule1_unevaluable_without_z_data():
    r = RE.load_rules(RULES_PATH)
    # 4 venues ARE live, so this must fail for the missing basis, not the floor
    v = RE.evaluate(_f(n_venues=4, basis_z={}), "SUI/USD", min_venues=3, rules=r)
    assert v.rules["1"].state == "UNEVALUABLE"
    assert "basis" in v.rules["1"].reason


# ── rule 2: flow dominance ─────────────────────────────────────────────────

def test_rule2_subcondition_a_evaluates_when_flow_is_present():
    """(b) needs a recorded price-response window, which does not exist yet,
    so the rule stays UNEVALUABLE — but (a) must still be evaluated and
    reported, not skipped."""
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=4,
           flow_by_venue={"binance": 0.95, "okx": 0.85, "kraken": 0.10, "bybit": 0.88},
           flow_divergence=0.21, flow_median=0.865)
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r)
    assert v.rules["2"].state == "UNEVALUABLE"        # blocked by (b)
    assert v.rules["2"].subconditions["a"].state == "MET"
    assert "kraken" in v.rules["2"].subconditions["a"].detail


def test_rule2_unevaluable_when_too_few_venues_have_tape():
    """One venue's tape cannot disagree with itself."""
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=4, n_venues_with_tape=1,
           flow_by_venue={"binance": 0.9}, flow_divergence=None)
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r)
    assert v.rules["2"].state == "UNEVALUABLE"


def test_rule2_unevaluable_when_tape_coverage_too_thin():
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=4, n_venues_with_tape=3,
           flow_by_venue={"binance": 0.9, "okx": 0.9, "kraken": 0.1},
           thin_coverage=["kraken"], flow_divergence=0.1)
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r)
    assert v.rules["2"].state == "UNEVALUABLE"


# ── rule 3: liquidity vacuum ───────────────────────────────────────────────

def test_rule3_unevaluable_when_touch_imbalance_missing():
    """A book with no size at the touch returns None. Absent size must not be
    read as 'everyone selling'."""
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=3, touch_imbalance={"binance": None, "okx": None, "bybit": None})
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r)
    assert v.rules["3"].state == "UNEVALUABLE"
    assert "touch imbalance" in v.rules["3"].reason


def test_rule3_unevaluable_without_depth_history():
    """Condition (b) compares top-N depth to its own trailing median. With no
    history there is nothing to compare against."""
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=3, touch_imbalance={"binance": 0.95, "okx": 0.95, "bybit": 0.95})
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r)
    assert v.rules["3"].state == "UNEVALUABLE"


# ── verdict behaviour ──────────────────────────────────────────────────────

def test_no_directional_verdict_without_a_met_rule():
    """The scorecard must never substitute for the rules."""
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=4, basis_z={"binance": 0.0, "okx": 0.0, "kraken": 40.0, "bybit": 0.0},
           basis_dispersion_bps=30.0, dispersion_in_spreads=100.0)
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r, history={"1": {"z_max": 9.0, "cycles": 2}})
    assert v.rules["1"].state == "MET"
    assert v.direction == "neutral"          # a MET basis names a venue, not a side
    assert "rule1_side" in v.unknowns


def test_evidence_below_floor_forces_reduced_evidence():
    r = RE.load_rules(RULES_PATH)
    v = RE.evaluate(_f(n_venues=2), "SUI/USD", min_venues=3, rules=r)
    assert v.evidence_ok is False
    assert v.reduced_evidence
    assert v.direction == "neutral"


def test_missing_venue_data_is_named_in_the_verdict():
    r = RE.load_rules(RULES_PATH)
    f = _f(n_venues=4, missing_venue_data=["kraken"],
           basis_z={"binance": 0.0, "okx": 0.0, "bybit": 0.0},
           basis_dispersion_bps=1.0, dispersion_in_spreads=3.0)
    v = RE.evaluate(f, "SUI/USD", min_venues=3, rules=r)
    assert "kraken" in v.reduced_evidence


# ── rules.json contract ────────────────────────────────────────────────────

def test_rules_json_declares_no_footprint():
    """Rules 6/7 of the ATAS engine cannot be evaluated here. Shipping them
    would mean shipping conditions that can never fire."""
    r = RE.load_rules(RULES_PATH)
    assert r["capabilities"]["l2_footprint"] is False
    assert "6" not in r["rules"] and "7" not in r["rules"]


def test_every_rule_declares_its_evidence_floor_and_audit_status():
    r = RE.load_rules(RULES_PATH)
    for rid, spec in r["rules"].items():
        assert "requires_venues" in spec, f"rule {rid} has no requires_venues"
        assert "audit_status" in spec, f"rule {rid} has no audit_status"
        assert "rationale" in spec, f"rule {rid} has no rationale"


def test_gating_is_off_until_a_rule_is_audited():
    """No unaudited rule may suppress a signal."""
    r = RE.load_rules(RULES_PATH)
    assert r["gates"]["gate_signals"] is False


def test_rules_json_is_valid_json_and_has_a_version():
    r = RE.load_rules(RULES_PATH)
    assert r["version"]
    assert r["engine"] == "scanner-engine"