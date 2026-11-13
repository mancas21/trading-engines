"""PEPE verification — run the scanner's OWN machinery on the new pair.

Nothing here re-implements the engine: it calls the same registry, the same
adapters, the same scaling and the same features the running loop uses, then
prints what the reader's rule AT-6/AT-7 condition (c) actually consumes:
the per-venue top-5 book imbalance, plus the price-basis sanity check that
the mixed multiplier makes mandatory.

Run:  python _verify_pepe_pair.py
"""
import time

import features as F
import pairs as P
from venues import binance as bn
from venues import okx as okx
from venues import kraken as kr
from venues import bybit as bb
from flow import window_cvd, DEFAULT_WINDOW_S

PAIR = "1000PEPE/USD"

print("=" * 90)
print("PEPE — the new pair through the engine's own machinery",
      time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 90)

print("\n[registry]")
spec = P.spec_for(PAIR)
for v, s in spec.symbols.items():
    print("  {:8} symbol={:22} price_mult={:>8} size_mult={:>10}".format(
        v, s, P.venue_price_mult(PAIR, v), P.venue_size_mult(PAIR, v)))
print("  venues={} floor={} bar={} avg_1m_notional_usd={:,.0f}".format(
    P.venues_for(PAIR), P.min_venues_for(PAIR), P.bar_for(PAIR),
    spec.avg_1m_notional_usd))

FETCHERS = {
    "binance": (bn.fetch_depth, bn.fetch_trades),
    "okx": (okx.fetch_depth, okx.fetch_trades),
    "kraken": (kr.fetch_depth, kr.fetch_trades),
    "bybit": (bb.fetch_depth, bb.fetch_trades),
}


def scale_book(book, pm, sm):
    from types_ import Book
    if pm == 1.0 and sm == 1.0:
        return book
    bids = [(p * pm, q * sm) for p, q in book.bids]
    asks = [(p * pm, q * sm) for p, q in book.asks]
    return Book(bid=bids[0][0], bids=bids, asks=asks)


snaps = {}
for venue, (fb, ft) in FETCHERS.items():
    pm = P.venue_price_mult(PAIR, venue)
    sm = P.venue_size_mult(PAIR, venue)
    try:
        book = fb(PAIR)
        raw_mid = (book.bid + book.ask) / 2
        book = scale_book(book, pm, sm)
        tape = None
        try:
            trades = ft(PAIR)
            if pm != 1.0 or sm != 1.0:
                trades = [{"ts": t["ts"], "price": t["price"] * pm,
                           "size": t["size"] * sm, "taker_side": t["taker_side"]}
                          for t in trades]
            w = window_cvd(trades, window_s=DEFAULT_WINDOW_S)
            if w is not None:
                tape = {"delta_ratio": w.delta_ratio, "coverage_s": w.coverage_s,
                        "n_trades": w.n_trades}
        except Exception as e:  # noqa: BLE001
            print("  [{}] tape FAIL: {}".format(venue, e))
        snaps[venue] = {"book": book, "tape": tape}
        print("\n  [{}] raw mid {:,.10g} -> scaled mid {:.8f}  spread {:.2f}bps".format(
            venue, raw_mid, book.mid, book.spread_bps))
        d_bid, d_ask = book.depth_within(5)
        tot = d_bid + d_ask
        imb5 = (d_bid - d_ask) / tot if tot > 0 else None
        print("      book_imb_5 = {:+.4f}   depth_total = {:,.0f}".format(
            imb5, tot))
        if tape:
            print("      tape: n={} cov={:.1f}s delta_ratio={:+.3f}".format(
                tape["n_trades"], tape["coverage_s"], tape["delta_ratio"]))
        else:
            print("      tape: NONE")
    except Exception as e:  # noqa: BLE001
        print("\n  [{}] FAIL: {}".format(venue, e))

feat = F.compute(snaps)
print("\n" + "=" * 90)
print("[features]  n_venues={}  n_venues_with_tape={}".format(
    feat.n_venues, feat.n_venues_with_tape))
print("\n  basis_bps (per venue vs the cross-venue median centre):")
for v, b in sorted(feat.basis_bps.items()):
    print("    {:8} {:+10.3f} bps   (z={:+.3f}  own spread {:.3f}bps)".format(
        v, b, feat.basis_z.get(v, 0.0), feat.spread_bps.get(v, 0.0)))
print("\n  basis_dispersion_bps = {}".format(feat.basis_dispersion_bps))
print("  dispersion_in_spreads = {}".format(feat.dispersion_in_spreads))
print("  flow_by_venue        = {}".format(
    {k: round(v, 3) for k, v in feat.flow_by_venue.items()}))

print("\n" + "=" * 90)
print("[what the reader reads]  book_imb_5 per venue — the rule 6/7 condition (c) input")
imb5 = {}
for v, s in snaps.items():
    b = s.get("book")
    if b is None:
        continue
    d_bid, d_ask = b.depth_within(5)
    tot = d_bid + d_ask
    if tot > 0:
        imb5[v] = (d_bid - d_ask) / tot
for v, val in sorted(imb5.items()):
    print("    {:8} {:+.4f}".format(v, val))
print("\n  venues with a book = {}   (the reader needs >= 3)".format(len(imb5)))
cal = {k: (v["mean"], v["std"]) for k, v in
       __import__("json").load(open(
           r"C:\Users\Lenovo\trading-engines\reader\dom_vac_calibration.json",
           encoding="utf-8"))["venues"].items()}
print("\n  z against each venue's OWN calibration (the reader's normalization):")
for v, val in sorted(imb5.items()):
    m, sd = cal.get(v, (None, None))
    if m is None or not sd:
        print("    {:8} UNCALIBRATED".format(v))
        continue
    print("    {:8} z = {:+.3f}".format(v, (val - m) / sd))
