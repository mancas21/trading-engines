import asyncio, json, websockets

async def test():
    url = "wss://fstream.binance.com/stream?streams=solusdt@aggTrade/solusdt@depth20@100ms"
    async with websockets.connect(url, ping_interval=20) as ws:
        count = 0
        while count < 20:
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
            data = json.loads(msg)
            stream = data.get("stream", "")
            if "aggTrade" in stream:
                t = data.get("data", {})
                p = t.get("p")
                q = t.get("q")
                m = t.get("m")
                side = "SELL" if m else "BUY"
                usd = float(p) * float(q)
                print("[TRADE] %s %.2f SOL @ $%s ($%,.0f)" % (side, float(q), p, usd))
                count += 1
            elif "depth" in stream:
                d = data.get("data", {})
                b = d.get("b", [])[:1]
                a = d.get("a", [])[:1]
                # silent on depth updates

asyncio.run(test())
