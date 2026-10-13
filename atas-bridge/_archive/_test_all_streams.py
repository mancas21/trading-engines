import asyncio, json, websockets

async def test():
    # Try different stream formats
    streams = [
        ("Futures aggTrade", "wss://fstream.binance.com/ws/solusdt@aggTrade"),
        ("Futures trade", "wss://fstream.binance.com/ws/solusdt@trade"),
        ("Spot aggTrade", "wss://stream.binance.com:9443/ws/solusdt@aggTrade"),
        ("Spot trade", "wss://stream.binance.com:9443/ws/solusdt@trade"),
        ("Futures combined", "wss://fstream.binance.com/stream?streams=solusdt@aggTrade/solusdt@depth20@100ms"),
    ]
    
    for name, url in streams:
        print(f"\n=== Testing {name} ===")
        try:
            async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(msg)
                stream = data.get("stream", "")
                d = data.get("data", data)
                print(f"  OK! Stream: {stream}")
                print(f"  Keys: {list(d.keys())}")
                if "p" in d:
                    print(f"  Price: {d.get('p')} Qty: {d.get('q')} m: {d.get('m')}")
        except asyncio.TimeoutError:
            print("  No data in 10s")
        except Exception as e:
            print(f"  ERROR: {e}")

asyncio.run(test())