"""PEPE probe — which multiplier does each venue actually list?

The registry rule is "does it answer", not "is it listed". And the multiplier
matters: Binance/Bybit list BOTH 1x PEPE and 1000x 1000PEPE, OKX lists 1x
PEPE-USDT-SWAP only, Kraken lists 1x PEPE/USD spot. A cross-venue book
comparison must NOT mix 1000x contracts with 1x ones.

Run:  python _probe_pepe_venues.py
"""
import json
import time
import urllib.request


def get(url, timeout=25.0):
    req = urllib.request.Request(url, headers={"User-Agent": "scanner-probe/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def try_get(url, timeout=25.0):
    try:
        return get(url, timeout), None
    except Exception as e:  # noqa: BLE001
        return None, "{}: {}".format(type(e).__name__, e)


print("=" * 78)
print("PEPE — which multiplier does each venue list?", time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 78)

print("\n[binance fapi] PEPE symbols")
d, err = try_get("https://fapi.binance.com/fapi/v1/exchangeInfo")
if err:
    print("  FAIL:", err)
else:
    syms = {s["symbol"]: s for s in d["symbols"] if s.get("status") == "TRADING"}
    for cand in ("PEPEUSDT", "1000PEPEUSDT"):
        s = syms.get(cand)
        if s:
            print("  {:16} -> LISTED  contractType={} pricePrec={} qtyPrec={}".format(
                cand, s.get("contractType"), s.get("pricePrecision"), s.get("quantityPrecision")))
        else:
            print("  {:16} -> NOT LISTED".format(cand))

print("\n[okx] PEPE swap instruments")
d, err = try_get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
if err:
    print("  FAIL:", err)
else:
    rows = [x for x in d.get("data", []) if "PEPE" in x.get("instId", "")]
    if not rows:
        print("  (none)")
    for x in rows:
        print("  {:28} state={} ctVal={} ctValCcy={} tickSz={}".format(
            x.get("instId"), x.get("state"), x.get("ctVal"), x.get("ctValCcy"), x.get("tickSz")))

print("\n[bybit] PEPE linear instruments")
d, err = try_get("https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000")
if err:
    print("  FAIL:", err)
else:
    rows = [x for x in d.get("result", {}).get("list", []) if "PEPE" in x.get("symbol", "")]
    if not rows:
        print("  (none)")
    for x in rows:
        print("  {:18} status={} contractType={} qtyStep={} priceScale={}".format(
            x.get("symbol"), x.get("status"), x.get("contractType"),
            x.get("lotSizeFilter", {}).get("qtyStep"), x.get("priceScale")))

print("\n[kraken] PEPE asset pairs")
d, err = try_get("https://api.kraken.com/0/public/AssetPairs")
if err:
    print("  FAIL:", err)
else:
    result = d.get("result") or {}
    hits = []
    for key, val in result.items():
        alt = str(val.get("altname") or "").upper()
        if "PEPE" in key.upper() or "PEPE" in alt:
            hits.append((key, val))
    if not hits:
        print("  (none)")
    for key, val in hits:
        print("  wsname={:12} altname={:14} base={} quote={}".format(
            key, val.get("altname"), val.get("base"), val.get("quote")))
