import asyncio, json, websockets

async def test():
    url = "wss://fstream.binance.com/ws/solusdt@aggTrade"
    print("Testing SOLUSDT Futures aggTrade...")
    print("Connected! Waiting for trades (max 30s)...")
    try:
        async with websockets.connect(url, ping_interval=20, close_timeout=10) as ws:
            count = 0
            while count < 5:
                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                data = json.loads(msg)
                print("GOT TRADE:", json.dumps(data))
                count += 1
    except asyncio.TimeoutError:
        print("NO TRADES in 30s - stream may be broken or symbol inactive on Futures")
    except Exception as e:
        print("ERROR:", e)

asyncio.run(test())