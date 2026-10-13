import asyncio, json, websockets

async def test():
    # Try individual streams separately
    urls = [
        ("aggTrade", "wss://fstream.binance.com/ws/solusdt@aggTrade"),
        ("depth", "wss://fstream.binance.com/ws/solusdt@depth20@100ms"),
    ]
    for name, url in urls:
        print("Testing %s: %s" % (name, url))
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(msg)
                print("  OK! Got keys:", list(data.keys())[:5])
                if name == "aggTrade":
                    print("  Price:", data.get("p"), "Qty:", data.get("q"), "BuyerMaker:", data.get("m"))
                else:
                    print("  Bids:", len(data.get("b", [])), "Asks:", len(data.get("a", [])))
        except Exception as e:
            print("  ERROR:", e)
        print()

asyncio.run(test())
