"""The exit-bar selection decides WHAT the forward return measures.

Under the previous floor semantics ("last closed bar with close <= at_ms"), a
5m pair's exit bar was the bar CONTAINING the signal itself, so its realized
horizon was uniform in (0, 300s] with mean ~150s while the row claimed 300s —
and 5 of 8 registry pairs are 5m. Every test here pins the CEIL contract of
`outcomes.select_exit_bar`: the first CLOSED bar whose close is at or after
the horizon instant. A change to that semantics without a MEASUREMENT_VERSION
bump is how two eras of rows end up in one cell looking comparable.
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from outcomes import select_exit_bar          # noqa: E402


def _bars(closes_ms):
    return [(c, {"close_ms": c, "close": 100.0 + c / 1e9}) for c in closes_ms]


def test_ceil_picks_first_close_at_or_after_at_ms():
    bars = _bars([300_000, 600_000, 900_000])
    # horizon instant falls INSIDE the [300k, 600k) bar -> exit is 600k,
    # NOT 300k (which the floor semantics would have chosen)
    assert select_exit_bar(bars, 450_000, now_ms=2_000_000)["close_ms"] == 600_000
    # exactly at a close -> that bar qualifies (>=, not >)
    assert select_exit_bar(bars, 600_000, now_ms=2_000_000)["close_ms"] == 600_000
    # one ms past a close -> the NEXT bar
    assert select_exit_bar(bars, 600_001, now_ms=2_000_000)["close_ms"] == 900_000


def test_the_5m_regression_case_signals_containing_bar_is_not_returned():
    # The measured defect: signal at ts=420_000 (2:00 into the [300k, 600k)
    # bar), horizon 300s -> at_ms=720_000. Floor picked close=600_000, the bar
    # containing the SIGNAL, for a realized horizon of 180s. Ceil must pick
    # 900_000: a realized horizon of 480s >= the 300s the row promises.
    bars = _bars([300_000, 600_000, 900_000, 1_200_000])
    assert select_exit_bar(bars, 720_000, now_ms=2_000_000)["close_ms"] == 900_000


def test_still_forming_bar_never_qualifies():
    bars = _bars([300_000, 600_000, 900_000])
    # at_ms=800_000 wants the 900_000 bar, but "now" is 850_000 -> forming
    assert select_exit_bar(bars, 800_000, now_ms=850_000) is None
    # with a later now, the same bar is closed and returns
    assert select_exit_bar(bars, 800_000, now_ms=900_000)["close_ms"] == 900_000


def test_no_bar_at_or_after_at_ms_returns_none():
    bars = _bars([300_000, 600_000])
    assert select_exit_bar(bars, 700_000, now_ms=2_000_000) is None


def test_candidates_before_at_ms_are_ignored_even_if_uniquely_closed():
    bars = _bars([100_000, 500_000])
    assert select_exit_bar(bars, 200_000, now_ms=10_000_000)["close_ms"] == 500_000


def test_unordered_candidates_are_fine():
    # venue payloads arrive newest-first (Bybit) or oldest-first (Binance);
    # the selection must not care.
    bars = _bars([900_000, 300_000, 600_000])
    assert select_exit_bar(bars, 450_000, now_ms=2_000_000)["close_ms"] == 600_000


def test_backfill_bar_at_mirrors_live_selection():
    # The backfill reuses the same contract through its own index shape:
    # [(close_ms, price)] sorted ascending, chosen by bisect. A backfill that
    # selects bars differently from the live resolver makes backfilled rows
    # incomparable to live ones.
    from backfill_outcomes import bar_at
    idx = [(300_000, 100.0), (600_000, 101.0), (900_000, 102.0)]
    assert bar_at(idx, 450_000) == (600_000, 101.0)
    assert bar_at(idx, 600_000) == (600_000, 101.0)
    assert bar_at(idx, 600_001) == (900_000, 102.0)
    assert bar_at(idx, 999_999) is None
