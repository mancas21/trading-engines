"""Tests for cross-venue features, including the normalization that the
measured venue spreads force.

Run:  quant-alert-engine/venv/Scripts/python.exe -m pytest scanner-engine/tests/test_features.py -v
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from types_ import Book
import features as F


def _book(bid, ask):
    return Book(bid=bid, bids=[(bid, 1.0)], asks=[(ask, 1.0)])


def _snap(venue, book, ts=None, tape=None):
    import time
    return {"venue": venue, "book": book, "ts": ts if ts is not None else time.time(),
            "tape": tape}


def _tape(delta_ratio, coverage=50.0, n=120):
    """A tape that has already cleared the quality gate.

    The default was n=40, which is below `features.DEFAULT_MIN_TRADES` (50).
    Any tape that reaches the flow-divergence computation has by definition
    passed the trade-count gate, so a fixture with 40 trades described a state
    the code can no longer produce. These tests are about the divergence math,
    not the gate; the gate has its own tests.
    """
    return {"delta_ratio": delta_ratio, "coverage_s": coverage, "n_trades": n}


# ── venue basis: the median centre, not the weighted consensus ─────────────

def test_wide_book_venue_is_the_one_flagged_not_the_tight_ones():
    """THE REGRESSION THAT MATTERS.

    One venue quoting a wide book (kraken) against three tight books must NOT
    make the tight venues look dislocated. Measured with a depth-weighted
    consensus centre this returned binance z=-11.8 and kraken z=+0.4 - the
    liquid venues flagged, the thin one exonerated, exactly backwards. A venue's
    tick size is not a claim about price."""
    snaps = {
        "kraken": _snap("kraken", _book(100.0, 100.239)),     # wide book
        "binance": _snap("binance", _book(100.0, 100.0024)),  # tight
        "okx": _snap("okx", _book(100.0, 100.0024)),
        "bybit": _snap("bybit", _book(100.0, 100.0024)),
    }
    f = F.compute(snaps)

    # kraken's mid (100.1195) sits ~12c above the others' 100.0012
    assert abs(f.basis_z["kraken"]) > abs(f.basis_z["binance"]), (
        f"wide-book venue must carry the larger z: "
        f"kraken={f.basis_z['kraken']:.2f} binance={f.basis_z['binance']:.2f}"
    )


def test_three_agreeing_venues_have_near_zero_z_against_each_other():
    snaps = {
        "kraken": _snap("kraken", _book(100.0, 100.239)),
        "binance": _snap("binance", _book(100.0, 100.0024)),
        "okx": _snap("okx", _book(100.0, 100.0024)),
        "bybit": _snap("bybit", _book(100.0, 100.0024)),
    }
    f = F.compute(snaps)
    # binance/okx/bybit all quote the same mid -> each is ~0 from the median
    for v in ("binance", "okx", "bybit"):
        assert abs(f.basis_z[v]) < 5.0, f"{v} should sit at the median, got {f.basis_z[v]:.2f}"


def test_basis_is_normalized_by_own_spread_so_tight_books_score_higher():
    """The venue whose OWN spread is tight must show the larger |z| for the
    same absolute offset.

    Here kraken sits ~36c above the median with a 24bps spread (z=+1.50). If
    the normalization were removed, that same 36c gap would look identical on
    every venue — the wide book would register the same dislocation as a
    one-tick gap on a liquid venue."""
    snaps = {
        "kraken": _snap("kraken", _book(100.24, 100.48)),     # mid 100.36, spread 24bps
        "binance": _snap("binance", _book(100.0, 100.0024)),
        "okx": _snap("okx", _book(100.0, 100.0024)),
        "bybit": _snap("bybit", _book(100.0, 100.0024)),
    }
    f = F.compute(snaps)
    # the 3 tight venues ARE the median, so they sit at exactly 0
    assert f.basis_z["binance"] == 0.0
    # kraken's 36c gap is only 1.5x its own 24bps spread -> modest z
    assert 1.0 < abs(f.basis_z["kraken"]) < 2.0


def test_z_max_picks_the_largest_magnitude():
    snaps = {
        "kraken": _snap("kraken", _book(100.5, 100.51)),   # far from the others
        "binance": _snap("binance", _book(100.0, 100.0024)),
        "okx": _snap("okx", _book(100.0, 100.0024)),
        "bybit": _snap("bybit", _book(100.0, 100.0024)),
    }
    f = F.compute(snaps)
    assert f.z_max() == max(abs(v) for v in f.basis_z.values())
    # kraken is 50.4bps from the median on a ~1bps spread -> z ~50
    assert abs(f.basis_z["kraken"]) > 40
    # the three venues forming the median are all exactly 0
    assert f.basis_z["binance"] == 0.0 and f.basis_z["okx"] == 0.0


# ── dispersion needs consensus ─────────────────────────────────────────────

def test_no_dispersion_with_one_venue():
    f = F.compute({"binance": _snap("binance", _book(100.0, 100.1))})
    assert f.basis_dispersion_bps is None


def test_one_venue_has_a_basis_of_zero_not_a_fake_ranking():
    """A single venue is trivially its own consensus. z=0 is honest here; the
    dispersion field stays None because there is nothing to disperse."""
    f = F.compute({"binance": _snap("binance", _book(100.0, 100.1))})
    assert f.basis_z == {"binance": 0.0}


def test_no_dispersion_with_two_venues_is_reported_not_zero():
    """Two venues give a basis but NOT consensus. Reporting 0.0 would claim
    'venues agree perfectly', which two venues cannot establish."""
    f = F.compute({
        "binance": _snap("binance", _book(100.0, 100.1)),
        "okx": _snap("okx", _book(100.0, 100.1)),
    })
    assert f.n_venues == 2
    assert f.basis_dispersion_bps is None      # needs >=3 for dispersion


def test_dispersion_needs_three_venues():
    snaps = {
        "binance": _snap("binance", _book(100.0, 100.1)),
        "okx": _snap("okx", _book(100.0, 100.1)),
        "kraken": _snap("kraken", _book(100.5, 100.6)),
    }
    f = F.compute(snaps)
    assert f.basis_dispersion_bps is not None
    assert f.basis_dispersion_bps > 0


def test_dispersion_expressed_in_spreads_is_scale_free():
    """A 50c dislocation on a 0.24bps book and on a 24bps book are not
    comparable in raw bps. dispersion_in_spreads is."""
    tight = {
        "binance": _snap("binance", _book(100.0, 100.0024)),
        "okx": _snap("okx", _book(100.0, 100.0024)),
        "kraken": _snap("kraken", _book(100.5, 100.5024)),
    }
    wide = {
        "binance": _snap("binance", _book(100.0, 100.24)),
        "okx": _snap("okx", _book(100.0, 100.24)),
        "kraken": _snap("kraken", _book(100.5, 100.74)),
    }
    ft, fw = F.compute(tight), F.compute(wide)
    # same absolute dislocation (50c) on a 100x wider book -> fewer "spreads"
    assert ft.dispersion_in_spreads > fw.dispersion_in_spreads


# ── flow divergence: MAD from median, so one venue cannot fake it ─────────

def test_flow_divergence_splits_are_larger_than_a_single_outlier():
    """Mean-absolute-deviation from the median means 1 dissenting venue among 4
    moves the metric less than a genuine 2-vs-2 split. Verified:
    1-outlier=0.2075, 2-vs-2=0.3775, all-agree=0.0100."""
    def run(ratios):
        sn = {v: _snap(v, _book(100.0, 100.1)) for v in ("binance", "okx", "kraken", "bybit")}
        for v, r in zip(("binance", "okx", "kraken", "bybit"), ratios):
            sn[v]["tape"] = _tape(r)
        return F.compute(sn).flow_divergence

    one_outlier = run([0.90, 0.85, 0.10, 0.88])
    split = run([0.90, 0.15, 0.88, 0.12])
    assert split > one_outlier


def test_flow_divergence_detects_disagreement():
    snaps = {
        "binance": _snap("binance", _book(100.0, 100.1), tape=_tape(0.90)),
        "okx": _snap("okx", _book(100.0, 100.1), tape=_tape(0.85)),
        "kraken": _snap("kraken", _book(100.0, 100.1), tape=_tape(0.10)),
        "bybit": _snap("bybit", _book(100.0, 100.1), tape=_tape(0.88)),
    }
    f = F.compute(snaps)
    assert f.flow_divergence is not None and f.flow_divergence > 0.15


def test_flow_divergence_near_zero_when_all_agree():
    snaps = {
        "binance": _snap("binance", _book(100.0, 100.1), tape=_tape(0.88)),
        "okx": _snap("okx", _book(100.0, 100.1), tape=_tape(0.86)),
        "kraken": _snap("kraken", _book(100.0, 100.1), tape=_tape(0.87)),
        "bybit": _snap("bybit", _book(100.0, 100.1), tape=_tape(0.89)),
    }
    f = F.compute(snaps)
    assert f.flow_divergence is not None and f.flow_divergence < 0.05


def test_flow_needs_two_venues():
    """One venue cannot disagree with itself."""
    snaps = {"binance": _snap("binance", _book(100.0, 100.1), tape=_tape(0.9))}
    assert F.compute(snaps).flow_divergence is None


def test_missing_tape_is_excluded_not_zeroed():
    """A venue with no tape must be skipped, not counted as ratio 0.0 (= all
    selling). That would invent a bearish flow divergence."""
    snaps = {
        "binance": _snap("binance", _book(100.0, 100.1), tape=_tape(0.85)),
        "kraken": _snap("kraken", _book(100.0, 100.1)),   # no tape
        "okx": _snap("okx", _book(100.0, 100.1), tape=_tape(0.86)),
        "bybit": _snap("bybit", _book(100.0, 100.1), tape=_tape(0.84)),
    }
    f = F.compute(snaps)
    assert f.n_venues_with_tape == 3
    assert "kraken" not in f.flow_by_venue


def test_thin_coverage_tape_is_flagged_and_excluded():
    """A CVD built from 2 seconds of trades is not comparable to one covering
    55s, even if both report the same ratio."""
    snaps = {
        "binance": _snap("binance", _book(100.0, 100.1), tape=_tape(0.9, coverage=55.0)),
        "kraken": _snap("kraken", _book(100.0, 100.1), tape=_tape(0.1, coverage=3.0)),
        "okx": _snap("okx", _book(100.0, 100.1), tape=_tape(0.88, coverage=50.0)),
    }
    f = F.compute(snaps, min_coverage_s=20.0)
    assert "kraken" in f.thin_coverage
    assert "kraken" not in f.flow_by_venue


def test_flow_none_when_no_venue_has_tape():
    snaps = {
        "binance": _snap("binance", _book(100.0, 100.1)),
        "okx": _snap("okx", _book(100.0, 100.1)),
    }
    f = F.compute(snaps)
    assert f.flow_divergence is None
    assert f.n_venues_with_tape == 0


# ── liquidity ──────────────────────────────────────────────────────────────

def test_touch_imbalance_is_per_venue():
    snaps = {
        "binance": _snap("binance", Book(bid=100.0, bids=[(100.0, 9.0)], asks=[(100.1, 1.0)])),
        "okx": _snap("okx", Book(bid=100.0, bids=[(100.0, 1.0)], asks=[(100.1, 9.0)])),
    }
    f = F.compute(snaps)
    assert f.touch_imbalance["binance"] > 0.85
    assert f.touch_imbalance["okx"] < 0.15


def test_thin_book_must_not_look_like_a_vacuum():
    """A book with no size on the touch returns None imbalance. That is not
    'everyone selling' and must not read as a vacuum signal."""
    snaps = {"binance": _snap("binance", Book(bid=100.0, bids=[(100.0, 0.0)], asks=[(100.1, 0.0)]))}
    assert F.compute(snaps).touch_imbalance["binance"] is None


def test_missing_book_is_declared():
    snaps = {"binance": {"venue": "binance", "book": None, "ts": 0.0}}
    f = F.compute(snaps)
    assert f.n_venues == 0
    assert "binance" not in f.touch_imbalance
    assert "binance" in f.missing_venue_data