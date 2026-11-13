"""Audit the registry's venue symbols against the live APIs.

Making the registry authoritative (pairs.venue_symbol) exposed a latent
registry error: XAU's Kraken symbol was recorded as XAUTUSDT while the
adapter derived XAUTUSD -- and the adapter was right.

This checks EVERY registered symbol against the venue that must serve it,
so no other recorded-but-wrong symbol is waiting to be used.

Run:  python _audit_registry_symbols.py
"""
import json
import time
import urllib.request

import pairs as P


def get(url, timeout=20.0):
    req = urllib.request.Request(url, headers={"User-Agent": "scanner-audit/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ok(url):
    try:
        get(url)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, "{}: {}".format(type(e).__name__, e)


def probe(venue, symbol):
    """Does this venue actually serve this symbol? A book, not a listing."""
    if venue == "binance":
        url = "https://fapi.binance.com/fapi/v1/depth?symbol={}&limit=5".format(symbol)
    elif venue == "okx":
        url = "https://www.okx.com/api/v5/market/books?instId={}&sz=5".format(symbol)
    elif venue == "bybit":
        url = ("https://api.bybit.com/v5/market/orderbook"
               "?category=linear&symbol={}&limit=5".format(symbol))
    elif venue == "kraken":
        url = "https://api.kraken.com/0/public/Depth?pair={}&count=5".format(symbol)
    else:
        return None, "unknown venue"
    good, err = ok(url)
    if not good:
        return False, err
    if venue == "okx":
        # 51001 is an HTTP 200 with a non-zero code.
        raw = get(url)
        if raw.get("code") != "0":
            return False, "okx code {}: {}".format(raw.get("code"), raw.get("msg"))
    if venue == "bybit":
        raw = get(url)
        if raw.get("retCode") != 0:
            return False, "bybit retCode {}: {}".format(raw.get("retCode"), raw.get("retMsg"))
    if venue == "kraken":
        raw = get(url)
        if raw.get("error"):
            return False, "kraken {}".format(raw["error"])
    return True, ""


print("=" * 92)
print("registry symbol audit — every registered symbol against the live venue",
      time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 92)

bad = []
for pair in P.all_pairs():
    spec = P.spec_for(pair)
    print("\n{}".format(pair))
    for venue, symbol in spec.symbols.items():
        if not symbol:
            print("  {:8} — not listed on this venue".format(venue))
            continue
        good, err = probe(venue, symbol)
        flag = "OK " if good else "BAD"
        if not good:
            bad.append((pair, venue, symbol, err))
        print("  {:8} {:22} {} {}".format(venue, symbol, flag, "" if good else err))

print("\n" + "=" * 92)
if bad:
    print("{} BROKEN registry symbol(s):".format(len(bad)))
    for pair, venue, symbol, err in bad:
        print("  {} / {} -> {}   ({})".format(pair, venue, symbol, err))
else:
    print("every registered symbol answers on its venue.")
