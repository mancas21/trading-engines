"""Cross-venue features — the layer that justifies building a second engine.

A single-venue engine (the ATAS one) structurally cannot see any of this. Every
feature here is a disagreement BETWEEN venues.

THE CENTRAL PROBLEM THIS FILE SOLVES
====================================
Venue spreads are wildly uneven. Measured 2026-10-08, same instrument, same
instant:

    XAU/USD   binance 0.024bps   bybit 0.024bps   okx 0.238bps   kraken 0.239bps
    HYPE/USD  binance 0.119bps   okx    0.119bps   bybit 1.194bps  kraken 2.391bps
    XRP/USD   kraken  0.072bps   binance/okx/bybit  0.723bps

A raw basis threshold therefore measures venue tick size, not signal. A
24-cent tick on Kraken's ETH would look identical to a real dislocation.

So the basis is normalized by each venue's OWN spread:

    z = (venue_mid - cross_venue_vwap_mid) / (own_spread_bps + eps)

A venue with a wide book needs a proportionally wider gap to register. Thin-book
venues cannot manufacture signal out of their own tick size.

Every absent input yields None or is excluded — never 0.0. "No data" and "no
buying" are different claims, and only one of them is bearish.
"""
from __future__ import annotations

from dataclasses import dataclass, field

EPS_BPS = 0.01
DISPERSION_MIN_VENUES = 3


@dataclass
class CrossVenueFeatures:
    n_venues: int = 0
    n_venues_with_tape: int = 0

    # per-venue, in bps, vs the volume-weighted cross-venue mid
    basis_bps: dict[str, float] = field(default_factory=dict)
    # per-venue, normalized by that venue's own spread
    basis_z: dict[str, float] = field(default_factory=dict)

    # max-min across venues, in bps. None unless >= DISPERSION_MIN_VENUES.
    basis_dispersion_bps: float | None = None
    # same, expressed in units of the median venue spread — comparable across
    # pairs whose absolute spreads differ by 100x
    dispersion_in_spreads: float | None = None

    flow_by_venue: dict[str, float] = field(default_factory=dict)
    flow_divergence: float | None = None
    flow_median: float | None = None

    touch_imbalance: dict[str, float | None] = field(default_factory=dict)
    spread_bps: dict[str, float] = field(default_factory=dict)

    thin_coverage: list[str] = field(default_factory=list)
    missing_venue_data: list[str] = field(default_factory=list)

    def z_max(self) -> float | None:
        if not self.basis_z:
            return None
        return max(abs(v) for v in self.basis_z.values())


def compute(snaps: dict[str, dict], min_coverage_s: float = 20.0,
            eps_bps: float = EPS_BPS) -> CrossVenueFeatures:
    """Build cross-venue features from the poller's live snapshots.

    `snaps` maps venue -> snapshot dict with `book` (types_.Book) and an
    optional `tape` dict carrying `delta_ratio`, `coverage_s`, `n_trades`.
    """
    f = CrossVenueFeatures()

    # ── books: mid, spread, basis ────────────────────────────────────────
    mids: dict[str, float] = {}
    sizes: dict[str, float] = {}

    for venue, s in snaps.items():
        b = s.get("book")
        if b is None:
            f.missing_venue_data.append(venue)
            continue
        mids[venue] = b.mid
        sp = b.spread_bps
        f.spread_bps[venue] = sp
        # depth proxy for the vwap weighting: prefer the venue carrying size,
        # falling back to equal weight. Never zero-division.
        d_bid, d_ask = b.depth_within(5)
        sizes[venue] = max(d_bid + d_ask, 1e-12)
        f.touch_imbalance[venue] = b.touch_imbalance

    f.n_venues = len(mids)
    if f.n_venues == 0:
        return f

    total_w = sum(sizes[v] for v in mids)
    consensus = sum(mids[v] * sizes[v] for v in mids) / total_w

    for venue, mid in mids.items():
        f.basis_bps[venue] = (mid - consensus) / consensus * 10_000.0

    # The RANKING uses a MEDIAN centre, not the weighted consensus.
    #
    # Why: if one venue merely has a wider book, a depth-weighted consensus
    # barely moves toward it, so the TIGHT-book venues all carry a large
    # NEGATIVE basis and rank as dislocated. That is backwards — a venue's
    # tick size is not a claim about price, and it must not make the venues
    # that quote tightly look wrong. Measured case: kraken book 23.9bps wide,
    # three venues 0.24bps wide -> weighted-consensus z gave binance -11.8 and
    # kraken +0.4, i.e. the liquid venues flagged and the thin one exonerated.
    #
    # The median centre puts the dislocation on whichever venue actually sits
    # apart from the others, and then z/s own-spread asks whether that gap is
    # large relative to THAT venue's own tick.
    med_mid = sorted(mids.values())[len(mids) // 2]
    for venue, mid in mids.items():
        bps_med = (mid - med_mid) / med_mid * 10_000.0
        f.basis_z[venue] = bps_med / (f.spread_bps[venue] + eps_bps)

    if f.n_venues >= DISPERSION_MIN_VENUES:
        disp = max(mids.values()) - min(mids.values())
        f.basis_dispersion_bps = disp / med_mid * 10_000.0
        med_spread = sorted(f.spread_bps.values())[len(f.spread_bps) // 2]
        if med_spread > 0:
            f.dispersion_in_spreads = f.basis_dispersion_bps / med_spread
    # <3 venues => dispersion stays None: two venues give a basis, not consensus

    # ── flow: only from venues that actually published a usable tape ─────
    for venue, s in snaps.items():
        t = s.get("tape")
        if not t:
            continue
        cov = t.get("coverage_s", 0.0)
        if cov < min_coverage_s or t.get("n_trades", 0) <= 0:
            f.thin_coverage.append(venue)
            continue
        r = t.get("delta_ratio")
        if r is None:                      # declared absent, not 0.0
            continue
        f.flow_by_venue[venue] = r

    f.n_venues_with_tape = len(f.flow_by_venue)
    if f.n_venues_with_tape >= 2:
        vals = sorted(f.flow_by_venue.values())
        med = vals[len(vals) // 2]
        f.flow_median = med
        # divergence = mean absolute deviation from the median, so one outlier
        # venue cannot manufacture a divergence by itself when all others agree
        f.flow_divergence = sum(abs(v - med) for v in vals) / len(vals)

    return f