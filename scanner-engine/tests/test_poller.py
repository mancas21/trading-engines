"""Tests for the poller: staleness drop, per-venue isolation, 429 backoff.

Run:  quant-alert-engine/venv/Scripts/python.exe -m pytest scanner-engine/tests/test_poller.py -v
"""
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from poller import Poller, Throttle
from types_ import Book


def _book(bid=100.0, ask=100.1):
    return Book(bid=bid, bids=[(bid, 1.0)], asks=[(ask, 1.0)])


def _snap(pair, venue, ts=None, book=None):
    return {"pair": pair, "venue": venue, "ts": ts if ts is not None else time.time(),
            "book": book or _book()}


# ── staleness: ground rule 1 ───────────────────────────────────────────────

def test_stale_venue_is_dropped_not_imputed():
    """A venue whose last ok poll is older than max_age must not appear in
    live snapshots. It must be NAMED in the frozen report instead."""
    p = Poller(max_age_s=300.0)
    p.record("binance", "SUI/USD", _snap("SUI/USD", "binance", ts=time.time() - 400))

    assert p.live_pairs("SUI/USD") == {}
    frozen = p.frozen_report()
    assert "binance" in frozen["SUI/USD"]
    assert 380 < frozen["SUI/USD"]["binance"] < 420


def test_fresh_venue_is_returned():
    p = Poller(max_age_s=300.0)
    p.record("binance", "SUI/USD", _snap("SUI/USD", "binance"))
    assert set(p.live_pairs("SUI/USD")) == {"binance"}


def test_one_stale_venue_does_not_poison_the_others():
    """Kraken goes quiet; binance and okx must still be live."""
    p = Poller(max_age_s=300.0)
    p.record("binance", "ETH/USD", _snap("ETH/USD", "binance"))
    p.record("okx", "ETH/USD", _snap("ETH/USD", "okx"))
    p.record("kraken", "ETH/USD", _snap("ETH/USD", "kraken", ts=time.time() - 5000))

    live = p.live_pairs("ETH/USD")
    assert set(live) == {"binance", "okx"}
    assert "kraken" in p.frozen_report()["ETH/USD"]


def test_never_polled_venue_is_absent_from_both_maps():
    """A venue that has never returned is not 'frozen' (that implies it was
    live once) — it is simply unknown, and must not be counted as evidence."""
    p = Poller(max_age_s=300.0)
    p.record("binance", "SUI/USD", _snap("SUI/USD", "binance"))
    assert "kraken" not in p.live_pairs("SUI/USD")
    # No frozen venue for this pair => no key at all, not an empty-dict entry.
    assert "kraken" not in p.frozen_report().get("SUI/USD", {})


# ── throttling: one venue must not starve the others ───────────────────────

def test_throttle_opens_on_429_and_closes_after_cooldown():
    t = Throttle(base_cooldown_s=2.0)
    assert not t.is_open("okx")
    t.penalize("okx")
    assert t.is_open("okx")
    assert not t.is_open("binance")          # isolation


def test_throttle_clears_on_success():
    t = Throttle(base_cooldown_s=0.01)
    t.penalize("okx")
    time.sleep(0.03)
    assert not t.is_open("okx")


def test_repeated_penalties_lengthen_cooldown():
    t = Throttle(base_cooldown_s=1.0)
    t.penalize("okx")
    first = t.cooldown_remaining("okx")
    t.penalize("okx")
    assert t.cooldown_remaining("okx") >= first


def test_poller_reports_throttled_venue_in_health():
    p = Poller(max_age_s=300.0)
    p.penalize("okx")
    h = p.health()
    assert h["throttled_venues"] == ["okx"]
    assert h["poll_cycles"] == 0


# ── failures are declared, never zeroed ───────────────────────────────────

def test_failed_poll_does_not_become_a_zeroed_snapshot():
    """A venue that errors must be recorded as failed, not as a snapshot with
    empty book / zero volume. Ground rule 6."""
    p = Poller(max_age_s=300.0)
    p.record_failure("binance", "SUI/USD", "ValueError: one-sided book")
    assert "SUI/USD" not in p.live_pairs("SUI/USD")
    errs = p.failure_report()
    assert "binance" in errs["SUI/USD"]
    assert "one-sided book" in errs["SUI/USD"]["binance"]["error"]


def test_success_clears_previous_failure():
    p = Poller(max_age_s=300.0)
    p.record_failure("okx", "ETH/USD", "HTTP 429")
    assert "okx" in p.failure_report()["ETH/USD"]
    p.record("okx", "ETH/USD", _snap("ETH/USD", "okx"))
    assert "okx" not in p.failure_report().get("ETH/USD", {})


# ── min live venues is per-pair, not global ────────────────────────────────

def test_min_venues_is_configurable_per_pair():
    """SPCX and NVDA exist on only 3 venues, so their floor must be lower
    than the 4-venue pairs. A single global floor would leave them
    permanently unevaluable."""
    p = Poller(max_age_s=300.0)
    p.set_min_venues("SPCX/USD", 2)
    p.set_min_venues("SUI/USD", 3)

    assert p.min_venues("SPCX/USD") == 2
    assert p.min_venues("SUI/USD") == 3
    assert p.min_venues("XAU/USD") == 3          # default


def test_has_evidence_reports_whether_floor_is_met():
    p = Poller(max_age_s=300.0)
    p.set_min_venues("SPCX/USD", 2)
    p.record("okx", "SPCX/USD", _snap("SPCX/USD", "okx"))
    p.record("bybit", "SPCX/USD", _snap("SPCX/USD", "bybit"))
    assert p.has_evidence("SPCX/USD") is True

    p.set_min_venues("SPCX/USD", 3)
    assert p.has_evidence("SPCX/USD") is False


# ── permanent vs transient failure (the silent-drop bug) ─────────────────

def test_permanent_error_does_not_throttle_the_whole_venue():
    """REGRESSION. Kraken's permanent rejection of SPCX opened a venue-wide
    cooldown that silently removed Kraken evidence for OTHER pairs (HYPE, ETH
    reported 3 venues with no failure recorded). A per-pair permanent error
    must not penalize the venue."""
    p = Poller(max_age_s=300.0)
    p.record_failure("kraken", "SPCX/USD", "ValueError: kraken error: ['EQuery:Unknown asset pair']")

    assert not p.throttle.is_open("kraken"), "permanent error throttled the venue"
    assert not p.throttle.throttled()


def test_transient_error_does_throttle_the_venue():
    p = Poller(max_age_s=300.0)
    p.record_failure("okx", "SUI/USD", "HTTPError: 429 Too Many Requests")
    assert p.throttle.is_open("okx")


def test_permanent_failure_is_classified_and_separable():
    p = Poller(max_age_s=300.0)
    p.record_failure("kraken", "SPCX/USD", "kraken error: ['EQuery:Unknown asset pair']")
    p.record_failure("binance", "SUI/USD", "HTTPError: 429")

    unsup = p.unsupported_pairs("kraken")
    assert "SPCX/USD" in unsup
    assert unsup["SPCX/USD"]["permanent"] is True
    assert "binance" not in unsup

    # the transient one is a failure, not "not listed"
    # failure_report() is keyed {pair: {venue: ...}}
    allf = p.failure_report()
    assert allf["SUI/USD"]["binance"].get("permanent") is False


def test_permanent_failure_does_not_stop_other_pairs_on_that_venue():
    """The concrete symptom: after SPCX fails permanently on Kraken, a
    successful HYPE poll on Kraken must still be accepted."""
    p = Poller(max_age_s=300.0)
    p.record_failure("kraken", "SPCX/USD", "EQuery:Unknown asset pair")
    p.record("kraken", "HYPE/USD", _snap("HYPE/USD", "kraken"))

    assert "kraken" in p.live_pairs("HYPE/USD")
    assert "kraken" not in p.live_pairs("SPCX/USD")


# ── cycle bookkeeping ──────────────────────────────────────────────────────

def test_health_counts_cycles_and_live_pairs():
    p = Poller(max_age_s=300.0)
    p.record("binance", "SUI/USD", _snap("SUI/USD", "binance"))
    p.record("okx", "SUI/USD", _snap("SUI/USD", "okx"))
    p.begin_cycle()

    h = p.health()
    assert h["poll_cycles"] == 1
    assert "SUI/USD" in h["live_pairs"]
    assert h["live_pairs"]["SUI/USD"] == 2