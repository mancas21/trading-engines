"""Probe JUP across the four venues before adding it to the registry.

The registry's own doctrine is "EVERY FIELD HERE WAS MEASURED, NOT ASSUMED", so
this writes down what each venue actually answers rather than extrapolating from
the symbol convention.

Reports, per venue:
  * whether the symbol exists and what contract type it is
  * a 5 x 1m notional mean in USD (drives the 1m/5m bar choice)
  * the touch spread and top-5 depth, for the "is it thin" judgement

Run:  ../../quant-alert-engine/venv/Scripts/python.exe _probe_jup.py
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
print("JUP — measurement across venues,", time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 74)

# ── Binance ────────────────────────────────────────────────────────────────
print("\n[binance] JUPUSDT")
raw, err = try_get(
    "https://fapi.binance.com/fapi/v1/exchangeInfo")
if err:
    print("  exchangeInfo FALLO:", err)
else:
    hit = [s for s in raw["symbols"] if s["symbol"] == "JUPUSDT"]
    if not hit:
        print("  NO LISTADO en Binance USD-M futures")
    else:
        s = hit[0]
        print(f"  status={s['status']}  contractType={s.get('contractType')}  "
              f"underlyingSubType={s.get('underlyingSubType')}")
        print(f"  tickSize={s['filters'][0].get('tickSize')}  "
              f"pricePrecision={s.get('pricePrecision')}  "
              f"quantityPrecision={s.get('quantityPrecision')}")
        print(f"  onboardDate={s.get('onboardDate')}  "
              f"liquidationFee={s.get('liquidationFee')}")

k, err = try_get("https://fapi.binance.com/fapi/v1/klines"
                 "?symbol=JUPUSDT&interval=1m&limit=5")
if err:
    print("  klines FALLO:", err)
else:
    notional = [float(r[7]) for r in k]        # quote asset volume
    print(f"  5 x 1m notional USD: {[round(n) for n in notional]}")
    print(f"  mean = {sum(notional)/len(notional):,.0f}")
    print(f"  last close = {float(k[-1][4])}")

d, err = try_get("https://fapi.binance.com/fapi/v1/depth?symbol=JUPUSDT&limit=5")
if err:
    print("  depth FALLO:", err)
else:
    bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
    mid = (bid + ask) / 2
    print(f"  touch {bid} / {ask}   spread = {(ask-bid)/mid*10000:.2f} bps")
    print(f"  top5 bid depth = {sum(float(x[1]) for x in d['bids']):,.0f} JUP   "
          f"ask = {sum(float(x[1]) for x in d['asks']):,.0f} JUP")

# ── OKX ────────────────────────────────────────────────────────────────────
print("\n[okx] JUP-USDT-SWAP")
raw, err = try_get("https://www.okx.com/api/v5/public/instruments"
                   "?instType=SWAP&instId=JUP-USDT-SWAP")
if err:
    print("  FALLO:", err)
elif not raw.get("data"):
    print("  NO LISTADO:", raw.get("msg") or raw)
else:
    s = raw["data"][0]
    print(f"  state={s.get('state')}  ctVal={s.get('ctVal')}  "
          f"tickSz={s.get('tickSz')}  lotSz={s.get('lotSz')}")

c, err = try_get("https://www.okx.com/api/v5/market/candles"
                 "?instId=JUP-USDT-SWAP&bar=1m&limit=5")
if err:
    print("   candles FALLO:", err)
elif not c.get("data"):
    print("   candles vacio:", c.get("msg") or c)
else:
    ns = [float(r[7]) for r in c["data"]]      # volCcyQuote
    print(f"  5 x 1m notional USD: {[round(n) for n in ns]}  mean = {sum(ns)/len(ns):,.0f}")

# ── Bybit ──────────────────────────────────────────────────────────────────
print("\n[bybit] JUPUSDT")
raw, err = try_get("https://api.bybit.com/v5/market/instruments-info"
                   "?category=linear&symbol=JUPUSDT")
if err:
    print("  FALLO:", err)
elif not raw.get("result", {}).get("list"):
    print("  NO LISTADO:", raw.get("retMsg"))
else:
    s = raw["result"]["list"][0]
    print(f"  status={s.get('status')}  contractType={s.get('contractType')}  "
          f"tickSize={s.get('priceFilter',{}).get('tickSize')}")

k, err = try_get("https://api.bybit.com/v5/market/kline"
                 "?category=linear&symbol=JUPUSDT&interval=1&limit=5")
if err:
    print("  kline FALLO:", err)
elif k.get("retCode") != 0:
    print("  kline error:", k.get("retMsg"))
else:
    ns = [float(r[6]) for r in k["result"]["list"]]   # turnover
    print(f"  5 x 1m notional USD: {[round(n) for n in ns]}  mean = {sum(ns)/len(ns):,.0f}")

# ── Kraken ─────────────────────────────────────────────────────────────────
print("\n[kraken] JUPUSD")
raw, err = try_get("https://api.kraken.com/0/public/OHLC?pair=JUPUSD&interval=1")
if err:
    print("  FALLO:", err)
elif raw.get("error"):
    print("  NO LISTADO:", raw["error"])
else:
    keys = [k for k in raw["result"] if k != "last"]
    print(f"  keys={keys}")
    for kk in keys:
        rows = raw["result"][kk]
        ns = [float(r[6]) * float(r[4]) for r in rows[-5:]]   # vol * close
        print(f"  {kk}: 5 x 1m notional USD mean = {sum(ns)/len(ns):,.0f}")

print("\n" + "=" * 74)
