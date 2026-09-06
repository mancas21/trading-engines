import asyncio, json, websockets

async def test():
    url = "wss://fstream.binance.com/stream?streams=solusdt@aggTrade/solusdt@depth20@100ms"
    async with websockets.connect(url, ping_interval=20) as ws:
        for i in range(8):
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            stream = data.get("stream", "")
            if "aggTrade" in stream:
                t = data.get("data", {})
                p = t.get("p")
                q = t.get("q")
                m = t.get("m")
                side = "SELL" if m else "BUY"
                usd = float(p) * float(q)
                print("  [TRADE] %s %.2f SOL @ $%s ($%,.0f)" % (side, float(q), p, usd))
            elif "depth" in stream:
                d = data.get("data", {})
                b = d.get("b", [])[:3]
                a = d.get("a", [])[:3]
                print("  [DOM] Best Bid: %s (%s) | Best Ask: %s (%s)" % (b[0][0], b[0][1], a[0][0], a[0][1]))

asyncio.run(test())
