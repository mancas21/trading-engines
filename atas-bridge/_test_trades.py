import asyncio, json, websockets

async def test():
    url = "wss://fstream.binance.com/ws/solusdt@aggTrade"
    async with websockets.connect(url, ping_interval=20) as ws:
        for i in range(5):
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            price = data.get("p")
            qty = data.get("q")
            is_buyer_maker = data.get("m")
            side = "SELL" if is_buyer_maker else "BUY"
            usd = float(price) * float(qty)
            print(f"{side} {qty} SOL @ ${price} (${usd:,.0f})")

asyncio.run(test())
