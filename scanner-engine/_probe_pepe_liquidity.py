"""PEPE probe #3 — liquidity + "does the venue ANSWER", per venue.

The registry's floor is not "is it listed" but "does it answer": depth AND
trades, with enough notional to keep 1m defensible. Measured 2026-11-12.

Run:  python _probe_pepe_liquidity.py
"""
import statistics
import time

from venues import binance as bn
from venues import okx as okx
from venues import kraken as kr
from venues import bybit as bb

PAIR = "1000PEPE/USD"
NOW = time.time()

print("=" * 88)
print("PEPE — per-venue liquidity + trade/depth health", time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 88)

# ── binance: 1000PEPEUSDT, 1m klines for notional ─────────────────────────
print("\n[binance] 1000PEPEUSDT")
try:
    raw = bn._get("/fapi/v1/klines?symbol=1000PEPEUSDT&interval=1m&limit=61")
    rows = [r for r in raw if r[6] <= int(NOW * 1000)][-60:]
    notionals = [float(r[7]) for r in rows]        # idx 7 = quote asset volume (USD)
    print("  60 closed 1m bars, USD notional:")
    print("    mean={:,.0f}  median={:,.0f}  min={:,.0f}  max={:,.0f}".format(
        statistics.mean(notionals), statistics.median(notionals),
        min(notionals), max(notionals)))
    print("    -> avg_1m_notional_usd = {:,.0f}".format(statistics.mean(notionals)))
    d = bn.fetch_depth(PAIR)
    print("  depth OK  touch {}/{}  spread {:.2f}bps".format(
        d.bid, d.ask, d.spread_bps))
    t = bn.fetch_trades(PAIR, limit=1000)
    ages = [NOW - x["ts"] for x in t]
    print("  aggTrades OK n={}  newest age {:.1f}s  median size {:,.0f}".format(
        len(t), min(ages), statistics.median([x["size"] for x in t])))
except Exception as e:  # noqa: BLE001
    print("  FAIL:", e)

# ── bybit: 1000PEPEUSDT ───────────────────────────────────────────────────
print("\n[bybit] 1000PEPEUSDT")
try:
    d = bb.fetch_depth(PAIR)
    print("  depth OK  touch {}/{}  spread {:.2f}bps".format(
        d.bid, d.ask, d.spread_bps))
    t = bb.fetch_trades(PAIR, limit=1000)
    ages = [NOW - x["ts"] for x in t]
    print("  recent-trade OK n={}  newest age {:.1f}s  median size {:,.0f}".format(
        len(t), min(ages), statistics.median([x["size"] for x in t])))
except Exception as e:  # noqa: BLE001
    print("  FAIL:", e)

# ── okx: PEPE-USDT-SWAP (1x) ──────────────────────────────────────────────
print("\n[okx] PEPE-USDT-SWAP")
try:
    raw = okx._get("/api/v5/market/candles?instId=PEPE-USDT-SWAP&bar=1m&limit=61")
    rows = [r for r in raw.get("data", []) if r[8] == "1"][-60:]
    notionals = [float(r[7]) for r in rows]        # volCcyQuote = quote volume
    print("  60 closed 1m candles, USD notional:")
    print("    mean={:,.0f}  median={:,.0f}".format(
        statistics.mean(notionals), statistics.median(notionals)))
    d, _ = okx._get("/api/v5/market/books?instId=PEPE-USDT-SWAP&sz=20"), None
    bk = d["data"][0]
    bids = [(float(p), float(q)) for p, q, *_ in bk["bids"]]
    asks = [(float(p), float(q)) for p, q, *_ in bk["asks"]]
    mid = (bids[0][0] + asks[0][0]) / 2
    print("  books OK  touch {}/{}  spread {:.2f}bps".format(
        bids[0][0], asks[0][0], (asks[0][0] - bids[0][0]) / mid * 1e4))
    tr = okx._get("/api/v5/market/trades?instId=PEPE-USDT-SWAP&limit=100")
    tl = tr.get("data", [])
    ages = [NOW - float(x["ts"]) / 1000 for x in tl]
    print("  trades OK n={}  newest age {:.1f}s  median sz {:,.1f} contracts".format(
        len(tl), min(ages) if ages else -1,
        statistics.median([float(x["sz"]) for x in tl]) if tl else 0))
except Exception as e:  # noqa: BLE001
    print("  FAIL:", e)

# ── kraken: PEPEUSD (1x spot) ─────────────────────────────────────────────
print("\n[kraken] PEPEUSD")
try:
    bk, key = kr.parse_depth(kr._get("/0/public/Depth?pair=PEPEUSD&count=25"))
    mid = (bk.bid + bk.ask) / 2
    print("  depth OK  touch {}/{}  spread {:.2f}bps".format(
        bk.bid, bk.ask, bk.spread_bps))
    tr = kr._get("/0/public/Trades?pair=PEPEUSD")
    tl = tr["result"][key]
    ages = [NOW - float(x["time"]) for x in tl]
    print("  trades OK n={}  newest age {:.1f}s  median size {:,.0f} PEPE".format(
        len(tl), min(ages), statistics.median([float(x["volume"]) for x in tl])))
except Exception as e:  # noqa: BLE001
    print("  FAIL:", e)
