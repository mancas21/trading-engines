"""Normalized snapshot types shared by every venue adapter.

Named `types_` because `types` shadows the stdlib module.

The one rule encoded here: a book must be two-sided. A one-sided book cannot
produce a spread, and a zero or negative spread is a data bug, not a free
trade. Raising is better than silently emitting a fabricated mid.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Book:
    bid: float
    bids: list[tuple[float, float]]   # (price, size) descending by price
    asks: list[tuple[float, float]]   # (price, size) ascending by price

    def __post_init__(self):
        if not self.bids or not self.asks:
            raise ValueError(
                f"one-sided book: bids={len(self.bids)} asks={len(self.asks)}"
            )
        if self.bids[0][0] <= 0:
            raise ValueError(f"non-positive bid {self.bids[0][0]}")

    @property
    def ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        m = self.mid
        if m <= 0:
            return float("nan")
        return self.spread / m * 10_000.0

    @property
    def bid_size(self) -> float:
        return self.bids[0][1]

    @property
    def ask_size(self) -> float:
        return self.asks[0][1]

    @property
    def touch_imbalance(self) -> float | None:
        """Bid size / (bid+ask) at the touch, in [0,1].

        None when both sides are empty at the touch. It must never be 0.0:
        "no size" and "all selling" are different claims.
        """
        b, a = self.bid_size, self.ask_size
        if b <= 0 and a <= 0:
            return None
        return b / (b + a)

    def depth_within(self, levels: int) -> tuple[float, float]:
        """Cumulative bid/ask size within the top N levels."""
        return (
            sum(q for _, q in self.bids[:levels]),
            sum(q for _, q in self.asks[:levels]),
        )


@dataclass(frozen=True)
class Tape:
    """Aggregated aggressor flow for a window. Mirrors flow.WindowCVD."""
    last: float
    buy_vol: float
    sell_vol: float
    n_trades: int
    coverage_s: float

    @property
    def delta(self) -> float:
        return self.buy_vol - self.sell_vol


@dataclass(frozen=True)
class Derivative:
    open_interest: float | None = None
    funding_rate: float | None = None
    long_short_ratio: float | None = None


@dataclass(frozen=True)
class Snapshot:
    pair: str
    venue: str
    ts: float
    book: Book | None = None
    tape: Tape | None = None
    deriv: Derivative | None = None
    cvd_window: object | None = None   # flow.WindowCVD

    def is_stale(self, max_age_s: float) -> bool:
        return (time.time() - self.ts) > max_age_s

    def age_s(self) -> float:
        return time.time() - self.ts
