"""detectors.py — pure, stateless-per-call detectors for early movement.

Every detector takes a `MarketView` snapshot and returns an `Event` or None.
No I/O here, so every rule is unit-testable with synthetic data.

Design rules (quant, not vibes):
  * Each event carries the feature values that fired it, so the shadow log can
    be audited after the fact. A signal you cannot decompose is a signal you
    cannot falsify.
  * Thresholds are config, not constants buried in logic. They are starting
    points to calibrate in shadow mode, not claims of edge.
  * Flow is z-scored against its own recent baseline, not an absolute size.
    Absolute volume means different things on HYPE and BTC.
  * Each event declares its invalidation level. An event with no defined
    failure point is not a trade idea.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import math


@dataclass
class Config:
    band_bps: float = 10.0          # L2 depth band around mid
    level_band_bps: float = 8.0     # how close to a level counts as "at" it
    flow_window_s: float = 15.0     # taker-flow window
    flow_z_min: float = 2.0         # taker flow z-score to call pressure
    imb_min: float = 0.25           # book imbalance in the direction of the move
    ask_depletion_ratio: float = 0.6  # ask depth now / baseline, below this = thin
    absorb_max_drop_bps: float = 4.0  # price may not fall more than this while absorbing
    rally_move_atr: float = 1.0     # move over window, in ATRs, to call ignition
    rally_window_s: float = 30.0
    rally_max_extension_atr: float = 1.5  # beyond this, the move is no longer early


@dataclass
class Level:
    price: float
    kind: str            # "resistance" | "support" | "fvg_bull" | "fvg_bear"
    top: float = 0.0     # for zones: upper edge
    bottom: float = 0.0  # for zones: lower edge
    mitigated: bool = False


@dataclass
class MarketView:
    symbol: str
    ts: float
    price: float
    atr_1m: float                       # ATR on 1m closed bars, price units
    levels: list[Level]
    flow: dict                          # {"buy_z": f, "sell_z": f, "buy_qty": f, "sell_qty": f}
    book: Optional[object]              # book.BookMetrics or None
    book_baseline_ask: float = 0.0      # rolling median ask depth for depletion check
    book_baseline_bid: float = 0.0
    price_change_bps_window: dict = field(default_factory=dict)  # {window_s: bps}
    move_atr_window: float = 0.0        # signed move over rally window, in ATRs
    extension_atr: float = 0.0          # distance from start of move, in ATRs


@dataclass
class Event:
    name: str
    side: str               # "long" | "short"
    symbol: str
    ts: float
    price: float
    level: float
    invalidation: float
    features: dict

    def key(self) -> str:
        return f"{self.name}|{self.side}|{round(self.level, 6)}"


def _bps(a: float, b: float) -> float:
    return (a - b) / b * 1e4 if b else 0.0


def breakout_pressure(v: MarketView, c: Config) -> Optional[Event]:
    """Price is pressing into resistance with aggressive buying and thin asks.

    Fires BEFORE the level breaks. The bar-close engine only sees the break
    after the bar closes; this is the intrabar pressure that precedes it.
    """
    if v.book is None:
        return None
    for lvl in v.levels:
        if lvl.kind != "resistance" or lvl.mitigated:
            continue
        dist = _bps(lvl.price, v.price)              # >0 = level above price
        if not (0 <= dist <= c.level_band_bps):
            continue
        ask_thin = (v.book_baseline_ask > 0 and
                    v.book.ask_depth <= c.ask_depletion_ratio * v.book_baseline_ask)
        if v.flow.get("buy_z", 0.0) >= c.flow_z_min and v.book.imbalance >= c.imb_min and ask_thin:
            return Event(
                name="BREAKOUT_PRESSURE", side="long", symbol=v.symbol, ts=v.ts,
                price=v.price, level=lvl.price,
                invalidation=v.price * (1 - c.level_band_bps / 1e4),
                features={"dist_bps": dist, "buy_z": v.flow["buy_z"],
                          "imbalance": v.book.imbalance,
                          "ask_depth": v.book.ask_depth,
                          "ask_baseline": v.book_baseline_ask},
            )
    return None


def bounce_absorption(v: MarketView, c: Config) -> Optional[Event]:
    """Price is at support, sellers are hitting it, but it is not falling.

    Heavy aggressive selling that fails to move price down, with bid depth
    holding, is absorption. It precedes the bounce; the bar-close engine sees
    the bounce only after the bar has printed.
    """
    if v.book is None:
        return None
    for lvl in v.levels:
        if lvl.kind != "support" or lvl.mitigated:
            continue
        dist = _bps(v.price, lvl.price)              # >0 = price above level
        if not (0 <= dist <= c.level_band_bps):
            continue
        drop = v.price_change_bps_window.get(int(c.flow_window_s), 0.0)
        if (v.flow.get("sell_z", 0.0) >= c.flow_z_min
                and drop >= -c.absorb_max_drop_bps
                and v.book.imbalance >= c.imb_min):
            return Event(
                name="BOUNCE_ABSORPTION", side="long", symbol=v.symbol, ts=v.ts,
                price=v.price, level=lvl.price,
                invalidation=lvl.price * (1 - c.level_band_bps / 1e4),
                features={"dist_bps": dist, "sell_z": v.flow["sell_z"],
                          "window_change_bps": drop,
                          "imbalance": v.book.imbalance},
            )
    return None


def fvg_retest(v: MarketView, c: Config) -> Optional[Event]:
    """Price re-enters an unmitigated bullish FVG from above with bid support.

    The FVG itself is known at the close of its third candle. The retest is
    the intrabar event: the moment price trades into the gap with buyers
    defending it, which is earlier than any close-based confirmation.
    """
    if v.book is None:
        return None
    for lvl in v.levels:
        if lvl.kind != "fvg_bull" or lvl.mitigated:
            continue
        if not (lvl.bottom <= v.price <= lvl.top * (1 + c.level_band_bps / 1e4)):
            continue
        if v.book.imbalance >= c.imb_min and v.flow.get("buy_z", 0.0) >= 0:
            return Event(
                name="FVG_RETEST", side="long", symbol=v.symbol, ts=v.ts,
                price=v.price, level=lvl.top,
                invalidation=lvl.bottom,
                features={"zone_top": lvl.top, "zone_bottom": lvl.bottom,
                          "imbalance": v.book.imbalance,
                          "buy_z": v.flow.get("buy_z", 0.0)},
            )
    return None


def rally_ignition(v: MarketView, c: Config) -> Optional[Event]:
    """Early leg of a move: strong signed flow and a real move, not yet extended.

    The `extension` guard is what keeps this early. Without it the detector
    fires on a move that is already done, which is the same lag as the
    indicator engine.
    """
    if v.book is None or v.atr_1m <= 0:
        return None
    if (v.move_atr_window >= c.rally_move_atr
            and v.extension_atr <= c.rally_max_extension_atr
            and v.flow.get("buy_z", 0.0) >= c.flow_z_min
            and v.book.imbalance >= c.imb_min):
        return Event(
            name="RALLY_IGNITION", side="long", symbol=v.symbol, ts=v.ts,
            price=v.price, level=v.price,
            invalidation=v.price - c.rally_move_atr * v.atr_1m,
            features={"move_atr": v.move_atr_window,
                      "extension_atr": v.extension_atr,
                      "buy_z": v.flow["buy_z"], "imbalance": v.book.imbalance},
        )
    return None


ALL_DETECTORS = (breakout_pressure, bounce_absorption, fvg_retest, rally_ignition)


def run_all(v: MarketView, c: Config) -> list[Event]:
    out = []
    for det in ALL_DETECTORS:
        ev = det(v, c)
        if ev is not None:
            out.append(ev)
    return out


def zscore(x: float, sample: list[float]) -> float:
    """z of x against a baseline sample. Returns 0 when the baseline is degenerate."""
    n = len(sample)
    if n < 10:
        return 0.0
    mu = sum(sample) / n
    var = sum((s - mu) ** 2 for s in sample) / (n - 1)
    sd = math.sqrt(var)
    return (x - mu) / sd if sd > 1e-12 else 0.0


def fvg_from_bars(bars: list[dict], min_gap_atr: float, atr: float) -> Optional[Level]:
    """Bullish FVG from the last three closed bars: low of bar3 above high of bar1.

    `bars` is oldest->newest, length >= 3, closed bars only. Returns None when
    the gap is smaller than `min_gap_atr * atr`, which filters noise gaps.
    """
    if len(bars) < 3 or atr <= 0:
        return None
    b1, b3 = bars[-3], bars[-1]
    if b3["low"] > b1["high"] and (b3["low"] - b1["high"]) >= min_gap_atr * atr:
        return Level(price=b1["high"], kind="fvg_bull",
                     top=b3["low"], bottom=b1["high"])
    return None
