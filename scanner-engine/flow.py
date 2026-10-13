"""CVD (cumulative volume delta) layer — the corrected implementation.

WHY THIS FILE EXISTS
====================
The first design assumed one CVD source per venue, taken from
`futures/data/takerlongshortRatio`. That is wrong for a 1m/5m scanner:

  * Binance's taker-ratio has NO 1m period — it returns `[]` (verified
    across 5m/15m/30m/1h/4h, only those resolve). Verified 2026-10-08.
  * OKX `market/candles` has 9 fields and carries NO taker-buy volume,
    so per-bar CVD is impossible there. Verified 2026-10-08.

So per-bar CVD is available on exactly ONE venue. Every other venue must
derive flow from per-trade taker side.

THE TRAP THIS FILE PREVENTS
===========================
Summing a fixed TRADE COUNT is not a flow measurement. 50 trades on NVDA
can span 10 seconds; 50 trades on ETH span milliseconds. A count-normalized
sum makes a thin pair look violently active and a liquid pair look dead —
and then cross-venue comparison is meaningless noise.

Every venue is therefore normalized to a fixed TIME window, and the result
carries the coverage it actually achieved so a thin pair is visibly thin
rather than silently comparable.

RULES ENCODED HERE (each is a bug fixed elsewhere in this repo)
  * Absent data is declared, never imputed: no trades in window -> None,
    never 0.0. A zero flow and an absent flow are different claims.
  * Trades older than the window are dropped, not averaged in.
  * `coverage_s` is reported so a variable-quality reading is visible.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

# Window used when the caller does not specify one. Long enough to contain a
# meaningful number of trades on a liquid pair, short enough to stay
# actionable at 1m/5m.
DEFAULT_WINDOW_S = 60.0


@dataclass(frozen=True)
class WindowCVD:
    """Aggressor flow over a fixed time window."""

    buy: float
    sell: float
    delta: float
    n_trades: int
    coverage_s: float          # real span of trade activity in the window
    requested_window_s: float  # what the caller asked for
    first_price: float | None = None   # oldest trade in the window
    last_price: float | None = None    # newest trade in the window

    @property
    def total(self) -> float:
        return self.buy + self.sell

    @property
    def delta_ratio(self) -> float | None:
        """Buy share of total flow in [0,1]. None when there is no flow,
        so a silent pair cannot masquerade as perfectly balanced."""
        t = self.total
        if t <= 0:
            return None
        return self.buy / t

    @property
    def price_delta_bps(self) -> float | None:
        """Price move across the window, in bps. None when unknowable.

        This is the EFFORT-vs-RESULT leg: rule 2 claims a venue showing large
        buying whose price does NOT respond is absorbing rather than leading.
        Without this the claim cannot be asked at all, and rule 2 reported
        UNEVALUABLE forever — one of four rules permanently unfirable.
        """
        if not self.first_price or not self.last_price or self.first_price <= 0:
            return None
        return (self.last_price - self.first_price) / self.first_price * 10_000.0


@dataclass(frozen=True)
class BarCVD:
    """Per-bar CVD from a venue that publishes taker-buy volume directly."""

    buy: float
    sell: float
    delta: float
    closed: bool
    open_ms: int
    close_ms: int

    @property
    def delta_ratio(self) -> float | None:
        t = self.buy + self.sell
        if t <= 0:
            return None
        return self.buy / t


def window_cvd(
    trades: list[dict],
    now: float | None = None,
    window_s: float = DEFAULT_WINDOW_S,
) -> WindowCVD | None:
    """Sum taker buy/sell volume over the trailing `window_s` seconds.

    `trades` must be normalized venue trades: dicts with
    `ts` (epoch seconds), `size`, and `taker_side` in {'buy','sell'}.

    Returns None when no trade falls inside the window. It does NOT return a
    zeroed result: an absent reading and an absence of buying are different
    facts, and only one of them is bearish.
    """
    if now is None:
        now = time.time()
    cutoff = now - window_s

    buy = sell = 0.0
    newest = oldest = None
    n = 0
    first_price = last_price = None
    oldest_price = newest_price = None

    for t in trades:
        ts = t["ts"]
        if ts < cutoff or ts > now:
            # Stale trade (outside the window) or a clock-skewed future
            # timestamp. Either way it is not evidence about the window.
            continue
        size = float(t["size"])
        if t["taker_side"] == "buy":
            buy += size
        else:
            sell += size
        n += 1
        price = t.get("price")
        if newest is None or ts > newest:
            newest = ts
            if price is not None:
                newest_price = float(price)
        if oldest is None or ts < oldest:
            oldest = ts
            if price is not None:
                oldest_price = float(price)
        if first_price is None:
            first_price = price
        last_price = price

    if n == 0:
        return None

    coverage = (newest - oldest) if (newest is not None and oldest is not None) else 0.0

    return WindowCVD(
        buy=buy,
        sell=sell,
        delta=buy - sell,
        n_trades=n,
        coverage_s=coverage,
        requested_window_s=window_s,
        # OLDEST -> NEWEST, so the sign of the price move matches the sign of
        # the elapsed time and not the order the venue happened to list them.
        first_price=oldest_price,
        last_price=newest_price,
    )


# NOTE: there is deliberately no `is_fresh` here. A previous version carried a
# function by that name whose body checked coverage and window size but never
# the age of the data — it could not detect staleness, and it was called by
# nothing. Staleness IS enforced, one level up: `poller.live_pairs` drops any
# venue snapshot older than max_age_s before features ever see it, and
# `window_cvd` drops trades outside the window at build time. A freshness
# concern belongs at those two points; a third check that reads like a fourth
# guarantee is how a stale reading gets trusted.


def normalize_to_window(
    per_bar: list[BarCVD], now: float | None = None, window_s: float = DEFAULT_WINDOW_S
) -> WindowCVD | None:
    """Collapse per-bar CVD into the same WindowCVD shape the trade-derived
    venues produce, so cross-venue comparison uses one representation.

    Only CLOSED bars are counted. An in-progress bar has partial volume,
    which would understate flow and invent a delta that did not happen.
    """
    if now is None:
        now = time.time()
    cutoff_ms = (now - window_s) * 1000.0

    buy = sell = 0.0
    n = 0
    newest_close = oldest_close = None

    for bar in per_bar:
        if not bar.closed:
            continue
        if bar.close_ms < cutoff_ms or bar.close_ms > now * 1000.0:
            continue
        buy += bar.buy
        sell += bar.sell
        n += 1
        if newest_close is None or bar.close_ms > newest_close:
            newest_close = bar.close_ms
        if oldest_close is None or bar.close_ms < oldest_close:
            oldest_close = bar.close_ms

    if n == 0:
        return None

    coverage = ((newest_close - oldest_close) / 1000.0) if newest_close else 0.0

    return WindowCVD(
        buy=buy,
        sell=sell,
        delta=buy - sell,
        n_trades=n,
        coverage_s=coverage,
        requested_window_s=window_s,
    )
