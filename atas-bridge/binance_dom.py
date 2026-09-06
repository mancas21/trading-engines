#!/usr/bin/env python3
"""
Binance Futures WebSocket — Smart DOM en vivo
Muestra depth de SOLUSDT en terminal + opción Telegram
"""
import asyncio
import json
import time
import os
import sys
from datetime import datetime

try:
    import websockets
except ImportError:
    print("Instalando websockets...")
    os.system("pip install websockets")
    import websockets

try:
    import urllib.request
except ImportError:
    pass

# ── Config ──────────────────────────────────────────────────────
# Load .env (same pattern as bridge_server.py)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip()
                if _k and _v and not os.environ.get(_k):
                    os.environ[_k] = _v

SYMBOL = "solusdt"
WS_URL = f"wss://fstream.binance.com/ws/{SYMBOL}@depth20@100ms"
DEPTH_LEVELS = 15
TELEGRAM_BOT = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID")
SEND_TELEGRAM = False
UPDATE_INTERVAL = 1.0
# ────────────────────────────────────────────────────────────────

last_update = 0
last_snapshot = {}

def format_number(n):
    n = float(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.1f}K"
    else:
        return f"{n:.1f}"

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def render_dom(data):
    global last_update, last_snapshot
    now = time.time()
    if now - last_update < UPDATE_INTERVAL:
        return
    last_update = now

    bids = data.get("bids", data.get("b", []))
    asks = data.get("asks", data.get("a", []))

    if not bids or not asks:
        return

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    spread = best_ask - best_bid
    spread_pct = (spread / best_bid) * 100

    max_bid_vol = max(float(b[1]) for b in bids[:DEPTH_LEVELS]) if bids else 1
    max_ask_vol = max(float(a[1]) for a in asks[:DEPTH_LEVELS]) if asks else 1
    max_vol = max(max_bid_vol, max_ask_vol)

    clear_screen()
    ts = datetime.now().strftime("%H:%M:%S")

    print(f"{'='*70}")
    print(f"  SMART DOM — SOLUSDT Perpetual — {ts}")
    print(f"{'='*70}")
    print(f"  Best Bid: ${best_bid:.2f}  |  Best Ask: ${best_ask:.2f}")
    print(f"  Spread: ${spread:.2f} ({spread_pct:.4f}%)")
    print(f"{'='*70}")

    print(f"\n  {'ASKS (Sellers)':^66}")
    print(f"  {'─'*66}")

    display_asks = list(reversed(asks[:DEPTH_LEVELS]))
    for price_str, vol_str in display_asks:
        price = float(price_str)
        vol = float(vol_str)
        bar_len = int((vol / max_vol) * 30) if max_vol > 0 else 0
        vol_formatted = format_number(vol)
        usd = vol * price
        usd_formatted = format_number(usd)
        bar = '█' * bar_len + '░' * (30 - bar_len)
        print(f"  ${price:<8.2f} {bar} {vol_formatted:>7s} ({usd_formatted:>6s})")

    print(f"\n  {'─'*66}")
    mid = (best_bid + best_ask) / 2
    print(f"  {'MID: $' + f'{mid:.2f}':^66}")
    print(f"  {'─'*66}")

    for price_str, vol_str in bids[:DEPTH_LEVELS]:
        price = float(price_str)
        vol = float(vol_str)
        bar_len = int((vol / max_vol) * 30) if max_vol > 0 else 0
        vol_formatted = format_number(vol)
        usd = vol * price
        usd_formatted = format_number(usd)
        bar = '█' * bar_len + '░' * (30 - bar_len)
        print(f"  ${price:<8.2f} {bar} {vol_formatted:>7s} ({usd_formatted:>6s})")

    print(f"\n  {'BIDS (Buyers)':^66}")
    print(f"{'='*70}")

    bid_wall = max(bids[:DEPTH_LEVELS], key=lambda x: float(x[1]))
    ask_wall = max(asks[:DEPTH_LEVELS], key=lambda x: float(x[1]))
    bid_wall_vol = float(bid_wall[1])
    ask_wall_vol = float(ask_wall[1])

    bid_usd = bid_wall_vol * float(bid_wall[0])
    ask_usd = ask_wall_vol * float(ask_wall[0])

    print(f"  WHALE WALLS:")
    print(f"  Bid wall: ${float(bid_wall[0]):.2f} — {format_number(bid_wall_vol)} ({format_number(bid_usd)})")
    print(f"  Ask wall: ${float(ask_wall[0]):.2f} — {format_number(ask_wall_vol)} ({format_number(ask_usd)})")

    total_bid_vol = sum(float(b[1]) for b in bids[:DEPTH_LEVELS])
    total_ask_vol = sum(float(a[1]) for a in asks[:DEPTH_LEVELS])
    imbalance = ((total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol)) * 100

    if imbalance > 10:
        signal = "🟢 BUYERS DOMINANT"
    elif imbalance < -10:
        signal = "🔴 SELLERS DOMINANT"
    else:
        signal = "⚖️  BALANCED"

    print(f"\n  IMBALANCE: {imbalance:+.1f}% — {signal}")
    print(f"  Bid depth: {format_number(total_bid_vol)} | Ask depth: {format_number(total_ask_vol)}")
    print(f"{'='*70}")

    last_snapshot = {
        "bid": best_bid,
        "ask": best_ask,
        "spread": spread,
        "imbalance": imbalance,
        "bid_wall": float(bid_wall[0]),
        "ask_wall": float(ask_wall[0]),
        "ts": ts
    }

def send_telegram(msg):
    if not SEND_TELEGRAM:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage"
        data = json.dumps({"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except:
        pass

async def listen():
    print(f"Conectando a Binance WebSocket...")
    print(f"Symbol: SOLUSDT | Stream: depth20@100ms")
    print(f"Press Ctrl+C to stop\n")

    async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
        print("Conectado! Recibiendo datos...\n")
        async for msg in ws:
            try:
                data = json.loads(msg)
                render_dom(data)
            except Exception as e:
                pass

if __name__ == "__main__":
    try:
        asyncio.run(listen())
    except KeyboardInterrupt:
        print("\n\nDOM monitor stopped.")
    except Exception as e:
        print(f"\nError: {e}")
