"""book.py — local L2 order book for Binance USDT-M futures.

Maintains a full book from the diff stream `<sym>@depth@100ms` using the
documented procedure:

  1. Open the WS and buffer events.
  2. Fetch REST snapshot /fapi/v1/depth -> lastUpdateId.
  3. Drop buffered events with u < lastUpdateId.
  4. The first applied event must satisfy U <= lastUpdateId <= u.
  5. Every later event must satisfy pu == previous event's u. Otherwise the
     book is stale: mark it invalid and resync from step 2.

Detectors must never read a book that is not `valid`. A silently wrong book
produces phantom walls and phantom imbalance, which is worse than no signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BookMetrics:
    mid: float
    best_bid: float
    best_ask: float
    bid_depth: float      # base qty resting within `band_bps` of mid
    ask_depth: float
    imbalance: float      # (bid - ask) / (bid + ask) in [-1, 1]


@dataclass
class LocalBook:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_u: int | None = None
    valid: bool = False
    gaps: int = 0

    def load_snapshot(self, snap: dict) -> None:
        self.bids = {float(p): float(q) for p, q in snap.get("bids", []) if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in snap.get("asks", []) if float(q) > 0}
        self.last_u = int(snap["lastUpdateId"])
        self.valid = False  # becomes valid once the first bridging event is applied

    def apply(self, ev: dict) -> bool:
        """Apply one diff event. Returns False when the book must resync."""
        if self.last_u is None:
            return False
        U, u, pu = int(ev["U"]), int(ev["u"]), int(ev.get("pu", -1))

        if u < self.last_u:
            return True  # stale event from before the snapshot: ignore

        if not self.valid:
            # first event that reaches past the snapshot must bridge it
            if not (U <= self.last_u <= u):
                return False
            self.valid = True
        elif pu != self.last_u:
            self.gaps += 1
            self.valid = False
            return False

        for p, q in ev.get("b", []):
            self._set(self.bids, float(p), float(q))
        for p, q in ev.get("a", []):
            self._set(self.asks, float(p), float(q))
        self.last_u = u
        return True

    @staticmethod
    def _set(side: dict[float, float], price: float, qty: float) -> None:
        if qty <= 0:
            side.pop(price, None)
        else:
            side[price] = qty

    def best(self) -> tuple[float, float] | None:
        if not self.valid or not self.bids or not self.asks:
            return None
        return max(self.bids), min(self.asks)

    def metrics(self, band_bps: float = 10.0) -> BookMetrics | None:
        top = self.best()
        if top is None:
            return None
        best_bid, best_ask = top
        mid = (best_bid + best_ask) / 2.0
        lo, hi = mid * (1 - band_bps / 1e4), mid * (1 + band_bps / 1e4)
        bid_depth = sum(q for p, q in self.bids.items() if p >= lo)
        ask_depth = sum(q for p, q in self.asks.items() if p <= hi)
        tot = bid_depth + ask_depth
        imb = (bid_depth - ask_depth) / tot if tot > 0 else 0.0
        return BookMetrics(mid, best_bid, best_ask, bid_depth, ask_depth, imb)
