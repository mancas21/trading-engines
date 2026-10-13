import asyncio, json, websockets

async def test():
    url = "wss://fstream.binance.com/ws/solusdt@depth20@100ms"
    async with websockets.connect(url, ping_interval=20) as ws:
        for i in range(3):
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            bids = data.get("b", [])
            asks = data.get("a", [])
            best_bid = bids[0][0] if bids else "?"
            best_ask = asks[0][0] if asks else "?"
            bid_wall = max(bids[:15], key=lambda x: float(x[1]))
            ask_wall = max(asks[:15], key=lambda x: float(x[1]))
            print(f"--- Update {i+1} ---")
            print(f"  Best Bid: {best_bid} | Best Ask: {best_ask}")
            print(f"  Bid wall: {bid_wall[0]} ({bid_wall[1]} SOL)")
            print(f"  Ask wall: {ask_wall[0]} ({ask_wall[1]} SOL)")
            total_bid = sum(float(b[1]) for b in bids[:15])
            total_ask = sum(float(a[1]) for a in asks[:15])
            imb = ((total_bid - total_ask) / (total_bid + total_ask)) * 100
            print(f"  Imbalance: {imb:+.1f}%")
            print()

asyncio.run(test())
