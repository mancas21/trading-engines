import sys, json
from pathlib import Path
sys.path.insert(0, r"C:\Users\Lenovo\trading-engines\execution")
from binance_testnet import TestnetClient

SYMS = ["HYPEUSDT", "SUIUSDT", "ETHUSDT", "SOLUSDT", "1000PEPEUSDT",
        "BTCUSDT", "ARBUSDT", "XRPUSDT", "JUPUSDT"]
c = TestnetClient(dry_run=True); c.sync_time()
st = json.loads(Path(r"C:\Users\Lenovo\trading-engines\execution\state\grid_state.json")
                .read_text(encoding="utf-8"))
a = c.account()
print(f"equity={float(a['totalMarginBalance']):.2f} wallet={float(a['totalWalletBalance']):.2f} "
      f"upnl={float(a['totalUnrealizedProfit']):+.2f} disp={float(a['availableBalance']):.2f}")
print()
print(f"{'simbolo':14s} {'pos':>12s} {'nocional':>9s} {'upnl':>8s} {'B':>2s} {'S':>2s} {'slots':>5s} {'ancla':>12s}")
tot = 0.0; nord = 0; tup = 0.0
for s in SYMS:
    ps = [p for p in c.positions(s) if float(p.get("positionAmt", 0)) != 0]
    amt = float(ps[0]["positionAmt"]) if ps else 0.0
    mark = float(ps[0]["markPrice"]) if ps else c.mark_price(s)
    up = float(ps[0]["unRealizedProfit"]) if ps else 0.0
    n = abs(amt) * mark; tot += n; tup += up
    oo = c.open_orders(s); nord += len(oo)
    b = sum(1 for o in oo if o["side"] == "BUY"); se = len(oo) - b
    sy = st["symbols"].get(s, {})
    print(f"{s:14s} {amt:>12.4f} {n:>9.2f} {up:>+8.2f} {b:>2d} {se:>2d} "
          f"{len(sy.get('slots', {})):>5d} {str(sy.get('anchor'))[:12]:>12s}")
print(f"\ninventario bruto={tot:.2f}  upnl={tup:+.2f}  ordenes abiertas={nord}")
