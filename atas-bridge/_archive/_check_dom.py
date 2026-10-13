import urllib.request, json

data = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/dom/solusdt", timeout=5).read())
rt = data.get("recent_trades", [])
ts = data.get("trade_summary", {})
print("Trades en buffer:", len(rt))
print("Imbalance DOM:", data.get("imbalance_pct"), "%")
print()
print("Trade Summary (10s window):")
print(json.dumps(ts, indent=2))
if rt:
    print()
    print("Last 5 trades:")
    for t in rt[-5:]:
        print("  %s %.2f SOL @ $%s ($%,.0f)" % (t["s"].upper(), t["q"], t["p"], t["usd"]))
else:
    print("\nNo trades yet - futures market quiet")
