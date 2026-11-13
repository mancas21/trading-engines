"""Third ARB probe — the 60s TAPE window per venue.

rules.json's `trade_floor` is 50 trades / 5s window (measured for JUP: Binance
23 trades in 60s meant NO venue contributed flow_by_venue). The same question
must be asked for ARB before its registry note can claim anything about flow.

Also measures the 5-level book imbalance per venue, because that is what
rule 3/4 (VAC) actually reads -- book_imb_5 does not depend on the tape.

Run:  ../../quant-alert-engine/venv/Scripts/python.exe _probe_arb_tape.py
"""
import json
import pathlib
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

WIN = 60.0


def get(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": "scanner-probe/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def try_get(url):
    try:
        return get(url), None
    except Exception as e:                                      # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


now = time.time()
print("=" * 74)
print(f"ARB — tape window {WIN:.0f}s per venue,", time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 74)

# ── Binance ────────────────────────────────────────────────────────────────
t, err = try_get("https://fapi.binance.com/fapi/v1/aggTrades?symbol=ARBUSDT&limit=1000")
if err:
    print("[binance] FALLO:", err)
else:
    win = [x for x in t if now - float(x["T"]) / 1000 <= WIN]
    buy = sum(1 for x in win if not x["m"])   # m=True -> buyer was maker -> aggressor SOLD
    usd = sum(float(x["p"]) * float(x["q"]) for x in win)
    print(f"[binance] trades_60s={len(win)}  taker_buy={buy}  taker_sell={len(win)-buy}  "
          f"notional={usd:,.0f}  (trade_floor=50 -> "
          f"{'PASS' if len(win) >= 50 else 'FAIL'})")

d, err = try_get("https://fapi.binance.com/fapi/v1/depth?symbol=ARBUSDT&limit=5")
if not err:
    b5 = sum(float(x[1]) for x in d["bids"][:5])
    a5 = sum(float(x[1]) for x in d["asks"][:5])
    print(f"           book_imb_5 (notional) = {(b5 - a5) / (b5 + a5):+.4f}")

# ── OKX ────────────────────────────────────────────────────────────────────
t, err = try_get("https://www.okx.com/api/v5/market/trades?instId=ARB-USDT-SWAP&limit=100")
if err:
    print("[okx] FALLO:", err)
else:
    tr = t.get("data") or []
    win = [x for x in tr if now - float(x["ts"]) / 1000 <= WIN]
    buy = sum(1 for x in win if x["side"] == "buy")
    print(f"[okx] trades_60s={len(win)}  taker_buy={buy}  taker_sell={len(win)-buy}  "
          f"({'PASS' if len(win) >= 50 else 'FAIL'} vs floor 50)")

# ── Bybit ──────────────────────────────────────────────────────────────────
t, err = try_get("https://api.bybit.com/v5/market/recent-trade?category=linear&symbol=ARBUSDT&limit=60")
if err:
    print("[bybit] FALLO:", err)
else:
    tr = t["result"]["list"]
    win = [x for x in tr if now - float(x["time"]) / 1000 <= WIN]
    buy = sum(1 for x in win if x["side"] == "Buy")
    print(f"[bybit] trades_60s={len(win)}  taker_buy={buy}  taker_sell={len(win)-buy}  "
          f"({'PASS' if len(win) >= 50 else 'FAIL'} vs floor 50)")

# ── Kraken ─────────────────────────────────────────────────────────────────
t, err = try_get("https://api.kraken.com/0/public/Trades?pair=ARBUSD")
if err:
    print("[kraken] FALLO:", err)
elif t.get("error"):
    print("[kraken] ERROR:", t["error"])
else:
    keys = [k for k in t["result"] if k != "last"]
    for kk in keys:
        tr = t["result"][kk]
        win = [x for x in tr if now - float(x[2]) <= WIN]
        buy = sum(1 for x in win if x[3] == "b")
        print(f"[kraken:{kk}] trades_60s={len(win)}  taker_buy={buy}  "
              f"taker_sell={len(win)-buy}  ({'PASS' if len(win) >= 50 else 'FAIL'} vs floor 50)")

print("\n" + "=" * 74)
