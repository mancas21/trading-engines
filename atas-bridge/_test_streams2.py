import asyncio, json, websockets

async def test():
    urls = [
        ("BTC aggTrade", "wss://fstream.binance.com/ws/btcusdt@aggTrade"),
        ("ETH aggTrade", "wss://fstream.binance.com/ws/ethusdt@aggTrade"),
        ("SOL aggTrade spot", "wss://stream.binance.com:9443/ws/solusdt@aggTrade"),
        ("SOL trade", "wss://fstream.binance.com/ws/solusdt@trade"),
    ]
    for name, url in urls:
        print("Testing %s" % name)
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(msg)
                print("  OK! Keys:", list(data.keys())[:8])
                if "p" in data:
                    print("  Price:", data.get("p"), "Qty:", data.get("q"), "m:", data.get("m"))
                elif "b" in data:
                    print("  Bids:", len(data.get("b", [])))
        except Exception as e:
            print("  ERROR:", str(e)[:100])
        print()

asyncio.run(test())
