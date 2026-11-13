"""Second ARB probe — does every venue actually ANSWER depth and trades?

The registry rule is not "is it listed" but "does it answer". JUP's note says
all four venues answer depth AND trades, which is what sets its floor at 4.
This checks the same for ARB, including Kraken's renamed-pair discovery.

Run:  ../../quant-alert-engine/venv/Scripts/python.exe _probe_arb_venues.py
"""
import json
import pathlib
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def get(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": "scanner-probe/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def try_get(url):
    try:
        return get(url), None
    except Exception as e:                                      # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


print("=" * 74)
print("ARB — depth + trades per venue,", time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 74)

# ── Binance ────────────────────────────────────────────────────────────────
print("\n[binance] ARBUSDT")
d, err = try_get("https://fapi.binance.com/fapi/v1/depth?symbol=ARBUSDT&limit=20")
if err:
    print("  depth FALLO:", err)
else:
    bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
    bid5 = sum(float(x[1]) for x in d["bids"][:5])
    ask5 = sum(float(x[1]) for x in d["asks"][:5])
    print(f"  touch {bid}/{ask} spread {(ask-bid)/((bid+ask)/2)*10000:.2f}bps "
          f"top5 bid {bid5:,.0f} ARB ask {ask5:,.0f} ARB "
          f"imb={(bid5-ask5)/(bid5+ask5)*100:+.1f}%")

t, err = try_get("https://fapi.binance.com/fapi/v1/aggTrades?symbol=ARBUSDT&limit=1000")
if err:
    print("  aggTrades FALLO:", err)
else:
    now = time.time()
    ages = [now - float(x["T"]) / 1000 for x in t]
    print(f"  aggTrades OK  n={len(t)}  newest age {min(ages):.1f}s  "
          f"median size {sorted(float(x['q']) for x in t)[len(t)//2]:,.0f} ARB")

# ── OKX ────────────────────────────────────────────────────────────────────
print("\n[okx] ARB-USDT-SWAP")
d, err = try_get("https://www.okx.com/api/v5/market/books?instId=ARB-USDT-SWAP&sz=20")
if err:
    print("  books FALLO:", err)
elif not d.get("data"):
    print("  books vacio:", d.get("msg"))
else:
    b = d["data"][0]
    bid, ask = float(b["bids"][0][0]), float(b["asks"][0][0])
    bid5 = sum(float(x[1]) for x in b["bids"][:5])
    ask5 = sum(float(x[1]) for x in b["asks"][:5])
    print(f"  touch {bid}/{ask} spread {(ask-bid)/((bid+ask)/2)*10000:.2f}bps "
          f"top5 bid {bid5:,.0f} ask {ask5:,.0f} ARB "
          f"imb={(bid5-ask5)/(bid5+ask5)*100:+.1f}%")

t, err = try_get("https://www.okx.com/api/v5/market/trades?instId=ARB-USDT-SWAP&limit=100")
if err:
    print("  trades FALLO:", err)
else:
    tr = t.get("data") or []
    if not tr:
        print("  trades VACIO:", t.get("msg"))
    else:
        now = time.time() * 1000
        ages = [now - float(x["ts"]) for x in tr]
        sides = [x["side"] for x in tr]
        print(f"  trades OK n={len(tr)} newest age {min(ages)/1000:.1f}s "
              f"buy {sides.count('buy')}/{len(sides)}")

# ── Bybit ──────────────────────────────────────────────────────────────────
print("\n[bybit] ARBUSDT")
d, err = try_get("https://api.bybit.com/v5/market/orderbook?category=linear&symbol=ARBUSDT&limit=25")
if err:
    print("  orderbook FALLO:", err)
elif d.get("retCode") != 0:
    print("  orderbook error:", d.get("retMsg"))
else:
    b = d["result"]
    bid, ask = float(b["b"][0][0]), float(b["a"][0][0])
    bid5 = sum(float(x[1]) for x in b["b"][:5])
    ask5 = sum(float(x[1]) for x in b["a"][:5])
    print(f"  touch {bid}/{ask} spread {(ask-bid)/((bid+ask)/2)*10000:.2f}bps "
          f"top5 bid {bid5:,.0f} ask {ask5:,.0f} ARB "
          f"imb={(bid5-ask5)/(bid5+ask5)*100:+.1f}%")

t, err = try_get("https://api.bybit.com/v5/market/recent-trade?category=linear&symbol=ARBUSDT&limit=60")
if err:
    print("  recent-trade FALLO:", err)
elif t.get("retCode") != 0:
    print("  recent-trade error:", t.get("retMsg"))
else:
    tr = t["result"]["list"]
    now = time.time() * 1000
    ages = [now - float(x["time"]) for x in tr]
    sides = [x["side"] for x in tr]
    print(f"  recent-trade OK n={len(tr)} newest age {min(ages)/1000:.1f}s "
          f"buy {sides.count('Buy')}/{len(sides)}")

# ── Kraken ─────────────────────────────────────────────────────────────────
print("\n[kraken] ARBUSD")
d, err = try_get("https://api.kraken.com/0/public/Depth?pair=ARBUSD&count=20")
if err:
    print("  Depth FALLO:", err)
elif d.get("error"):
    print("  Depth ERROR:", d["error"])
else:
    keys = [k for k in d["result"] if k != "last"]
    print(f"  keys={keys}")
    for kk in keys:
        bids = [(float(p), float(v)) for p, v, *_ in d["result"][kk]["bids"]]
        asks = [(float(p), float(v)) for p, v, *_ in d["result"][kk]["asks"]]
        bid, ask = bids[0][0], asks[0][0]
        print(f"  {kk}: touch {bid}/{ask} spread {(ask-bid)/((bid+ask)/2)*10000:.2f}bps "
              f"top5 bid {sum(v for _, v in bids[:5]):,.0f} ask {sum(v for _, v in asks[:5]):,.0f} ARB")

t, err = try_get("https://api.kraken.com/0/public/Trades?pair=ARBUSD")
if err:
    print("  Trades FALLO:", err)
elif t.get("error"):
    print("  Trades ERROR:", t["error"])
else:
    keys = [k for k in t["result"] if k != "last"]
    for kk in keys:
        tr = t["result"][kk]
        if not tr:
            print(f"  {kk}: Trades VACIO")
            continue
        now = time.time()
        ages = [now - float(x[2]) for x in tr]
        buys = sum(1 for x in tr if x[3] == "b")
        print(f"  {kk}: Trades OK n={len(tr)} newest age {min(ages):.1f}s "
              f"taker buy {buys}/{len(tr)}")

print("\n" + "=" * 74)
