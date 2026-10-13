import asyncio, json, websockets

async def test():
    # Try BTCUSDT which trades much more frequently
    url = "wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@depth20@100ms"
    print("Connecting to:", url)
    async with websockets.connect(url, ping_interval=20) as ws:
        print("Connected! Waiting for trades...")
        count = 0
        while count < 10:
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
                print("[TRADE] %s %.4f BTC @ $%s ($%,.0f)" % (side, float(q), p, usd))
                count += 1

asyncio.run(test())
