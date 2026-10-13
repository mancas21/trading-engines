import asyncio, json, websockets

async def test():
    # Try just aggTrade on its own
    url = "wss://fstream.binance.com/ws/solusdt@aggTrade"
    print("Connecting to:", url)
    async with websockets.connect(url, ping_interval=20) as ws:
        print("Connected! Waiting for trades...")
        count = 0
        while count < 5:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=20)
                data = json.loads(msg)
                p = data.get("p")
                q = data.get("q")
                m = data.get("m")
                side = "SELL" if m else "BUY"
                usd = float(p) * float(q)
                print("[TRADE] %s %.2f SOL @ $%s ($%,.0f)" % (side, float(q), p, usd))
                count += 1
            except asyncio.TimeoutError:
                print("No trade in 20s - market may be quiet")
                break

asyncio.run(test())
