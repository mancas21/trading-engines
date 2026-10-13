import asyncio, json, websockets

async def test():
    # Just test SOLUSDT aggTrade with more patience
    url = "wss://fstream.binance.com/ws/solusdt@aggTrade"
    print("Connecting to SOLUSDT aggTrade...")
    try:
        async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
            print("Connected! Waiting up to 60s for first trade...")
            msg = await asyncio.wait_for(ws.recv(), timeout=60)
            data = json.loads(msg)
            print("Got trade!")
            print(json.dumps(data, indent=2))
    except asyncio.TimeoutError:
        print("No trade in 60s - SOLUSDT futures is very quiet")
        print("Trying spot market instead...")
        url2 = "wss://stream.binance.com:9443/ws/solusdt@aggTrade"
        try:
            async with websockets.connect(url2, ping_interval=20, close_timeout=5) as ws:
                print("Connected to spot! Waiting...")
                msg = await asyncio.wait_for(ws.recv(), timeout=15)
                data = json.loads(msg)
                print("Got trade!")
                print(json.dumps(data, indent=2))
        except Exception as e:
            print("Spot also failed:", e)
    except Exception as e:
        print("Error:", e)

asyncio.run(test())
