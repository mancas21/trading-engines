"""PEPE probe #2 — the multiplier, measured in NOTIONAL, not assumed.

A cross-venue comparison needs the same underlying AND a comparable unit.
Binance/Bybit list 1000x PEPE (1000PEPEUSDT); OKX lists 1x PEPE-USDT-SWAP;
Kraken lists 1x PEPE/USD spot. The price ratio alone does not settle the SIZE
unit: OKX swaps quote `sz` in CONTRACTS (ctVal 10,000,000 PEPE each) and
Kraken quotes in tokens. The only ground truth is notional at the touch.

This uses the engine's OWN adapters (so the units printed are the units the
engine would actually ingest) and prints, per venue:
  touch, raw size, price x size, and the top-5 book imbalance.

Run:  python _probe_pepe_notional.py
"""
import time

from venues import binance as bn
from venues import okx as okx
from venues import kraken as kr
from venues import bybit as bb

PAIR = "1000PEPE/USD"


def show(name, book, size_note, mult_note):
    bids = book.bids
    asks = book.asks
    d_bid, d_ask = book.depth_within(5)
    tot = d_bid + d_ask
    imb = (d_bid - d_ask) / tot if tot > 0 else None
    print("\n[{}] {}".format(name, size_note))
    print("  touch {}/{}   raw size bid={:,.4f} ask={:,.4f}".format(
        bids[0][0], asks[0][0], bids[0][1], asks[0][1]))
    print("  price x size at touch: bid=${:,.6f}  ask=${:,.6f}".format(
        bids[0][0] * bids[0][1], asks[0][0] * asks[0][1]))
    print("  top5 imb={}  bid5={:,.4f} ask5={:,.4f}".format(
        "{:+.4f}".format(imb) if imb is not None else "n/a", d_bid, d_ask))
    print("  {}".format(mult_note))


print("=" * 88)
print("PEPE — per-venue NOTIONAL at the touch + top-5 book imbalance",
      time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 88)

try:
    b = bn.fetch_depth("1000PEPE/USD")
    show("binance", b, "1000PEPE/USDT -> 1000PEPEUSDT  (1000x perp, unit = 1000PEPE)",
         "-> to compare with a 1x venue, price x1000 and size /1000 keeps notional")
except Exception as e:  # noqa: BLE001
    print("\n[binance] FAIL:", e)

try:
    b = bb.fetch_depth("1000PEPE/USD")
    show("bybit", b, "1000PEPE/USDT -> 1000PEPEUSDT  (1000x perp, unit = 1000PEPE)",
         "-> to compare with a 1x venue, price x1000 and size /1000 keeps notional")
except Exception as e:  # noqa: BLE001
    print("\n[bybit] FAIL:", e)

try:
    b = okx.fetch_depth("1000PEPE/USD")
    show("okx", b, "1000PEPE/USD -> 1000PEPE-USDT-SWAP  (DOES NOT EXIST)",
         "-> the adapter derives the symbol; a per-venue override is required")
except Exception as e:  # noqa: BLE001
    print("\n[okx] via adapter symbol_for: FAIL:", e)
    import venues.okx as _okx
    raw = _okx._get("/api/v5/market/books?instId=PEPE-USDT-SWAP&sz=20")
    bk = raw["data"][0]
    from types_ import Book
    bids = [(float(p), float(q)) for p, q, *_ in bk["bids"]]
    asks = [(float(p), float(q)) for p, q, *_ in bk["asks"]]
    show("okx", Book(bid=bids[0][0], bids=bids, asks=asks),
         "PEPE-USDT-SWAP  (1x price, sz = CONTRACTS of 10,000,000 PEPE)",
         "-> price x1000 to reach the 1000x scale; a contract is 10,000,000 PEPE"
         " = 10,000 units of 1000PEPE, so size/1000 is NOT the notional fix")

try:
    b = kr.fetch_depth("1000PEPE/USD")
    show("kraken", b, "1000PEPE/USD -> 1000PEPEUSD  (DOES NOT EXIST)",
         "-> the adapter derives the symbol; a per-venue override is required")
except Exception as e:  # noqa: BLE001
    print("\n[kraken] via adapter symbol_for: FAIL:", e)
    import venues.kraken as _kr
    raw = _kr._get("/0/public/Depth?pair=PEPEUSD&count=25")
    bk, key = _kr.parse_depth(raw)
    show("kraken", bk, "PEPEUSD  (1x spot, unit = PEPE)",
         "-> price x1000 to reach the 1000x scale; size/1000 keeps notional")

print("\n" + "=" * 88)
print("SUMMARY — the shape of the problem")
print("=" * 88)
print("  binance/bybit : 1000x, unit = 1000PEPE")
print("  okx           : 1x price, unit = CONTRACTS (10,000,000 PEPE each)")
print("  kraken        : 1x price, unit = PEPE")
print("  book_imb_5 is a RATIO -> scale-invariant, so condition (c) can read")
print("  all four venues. basis_bps compares PRICES -> needs the 1000x scale")
print("  on every venue, and its depth weighting needs a common size unit.")
