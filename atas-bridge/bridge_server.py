"""
ATAS X Alert Bridge
Receives live indicator alerts from ATAS X and maintains a rolling context
window per ticker. Analysis is performed by Claude when queried.

Run:
    pip install -r requirements.txt
    python bridge_server.py

ATAS X webhook config:
    URL:  http://127.0.0.1:8000/alerta-atas
    Body: (uses ATAS native format with messages array)

Environment variables (optional):
    BUFFER_SIZE  - Rolling buffer size per ticker (default: 12)
    HOST         - Bind address (default: 127.0.0.1)
    PORT         - Listen port (default: 8000)
"""

import asyncio
import json as _json
import logging
import math
import os
import random
import re
import time
import uuid
from collections import defaultdict, deque
from urllib.parse import quote
from urllib.request import urlopen, Request as URLRequest

import uvicorn
import decision_engine as _de
import _shared_read
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

# Load .env file if present
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip()
                if _k and _v and not os.environ.get(_k):
                    os.environ[_k] = _v

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    logging.warning("websockets not installed — DOM feed disabled")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BUFFER_MAXLEN = int(os.getenv("BUFFER_SIZE", "12"))
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage" if TELEGRAM_TOKEN else ""

# Only ONE process may poll getUpdates per bot token (409 Conflict otherwise).
# The dedicated two-way listener (telegram-bot.js) owns polling; bridge polls only if enabled.
TELEGRAM_POLL = os.getenv("TELEGRAM_POLL", "0").lower() in ("1", "true", "yes")

BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "")  # shared-secret for POST endpoints

# ---------------------------------------------------------------------------
# Logging (rotating: 10MB x 5 — audit P1 fix)
# ---------------------------------------------------------------------------
import logging.handlers as _logging_handlers

_log_file = os.path.join(os.path.dirname(__file__), "bridge_debug.log")
log = logging.getLogger("atas-bridge")
log.setLevel(logging.INFO)
if not log.handlers:
    _file_handler = _logging_handlers.RotatingFileHandler(
        _log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(_file_handler)
    # Also log to console for when running interactively
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    log.addHandler(console)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="ATAS X Alert Bridge", version="3.1.0")


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_dom_feeds())
    asyncio.create_task(_cvd_tracker())
    asyncio.create_task(_calls_checker())
    asyncio.create_task(_derivatives_feed())
    asyncio.create_task(_liquidation_feed())
    asyncio.create_task(_heartbeat())
    # load taker CVD synchronously; feed will persist on updates
    _taker_cvd_load()
    asyncio.create_task(_taker_cvd_saver())
    if TELEGRAM_POLL:
        asyncio.create_task(telegram_polling())
        log.info("Telegram bot polling enabled")
    else:
        log.info("Telegram polling disabled (telegram-bot.js owns getUpdates)")
    log.info("Binance DOM+Trade feed started")

# ---------------------------------------------------------------------------
# Per-ticker rolling buffer
# ---------------------------------------------------------------------------
ticker_buffers: dict[str, deque] = defaultdict(
    lambda: deque(maxlen=BUFFER_MAXLEN)
)
ticker_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# ---------------------------------------------------------------------------
# Binance DOM + Trade Flow WebSocket feed
# ---------------------------------------------------------------------------
dom_state: dict = {}
dom_lock = asyncio.Lock()

RECENT_TRADES_MAX = 30
TRADE_SUMMARY_WINDOW = 10

BINANCE_DEPTH_URL = "wss://fstream.binance.com/ws/{symbol}@depth20@100ms"
BINANCE_TRADE_URL = "wss://fstream.binance.com/ws/{symbol}@trade"
BINANCE_DEPTH_REST_URL = "https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit=20"


async def _dom_feeds():
    """Background task: maintain live DOM snapshots + trade flow."""
    if not HAS_WEBSOCKETS:
        return
    symbols = ["solusdt", "suiusdt", "hypeusdt", "xrpusdt", "btcusdt", "ethusdt", "spcxusdt", "nvdausdt", "xauusdt"]
    # P6: DOM persistence tracking — require wall to persist across consecutive reads
    dom_persist: dict[str, dict] = {}
    DOM_PERSIST_REQUIRED = 3
    for sym in symbols:
        dom_persist[sym] = {
            "bid_wall_hits": 0, "bid_wall_price": 0,
            "ask_wall_hits": 0, "ask_wall_price": 0,
        }
        dom_state[sym] = {
            "symbol": sym.upper(),
            "best_bid": 0, "best_ask": 0,
            "spread": 0, "spread_pct": 0,
            "bid_wall_price": 0,             "bid_wall_vol": 0,
            "ask_wall_price": 0, "ask_wall_vol": 0,
            "bid_wall_persistent": False,
            "ask_wall_persistent": False,
            "total_bid_vol": 0, "total_ask_vol": 0,
            "imbalance_pct": 0,
            "bids": [], "asks": [],
            "recent_trades": [],
            "trade_summary": {"buy_vol": 0, "sell_vol": 0, "buy_count": 0, "sell_count": 0, "net": 0},
            "ts": time.time(),
        }
        # taker CVD per-symbol (independent of ATAS)
        try:
            _taker_cvd_state.setdefault(sym.upper(), _taker_cvd_new_state())
        except Exception:
            pass
    async def _run_symbol_feed(sym: str):
        # P1: Sequence validation state
        last_update_id = None
        last_msg_time = 0
        
        async def _fetch_depth_snapshot():
            """Fetch REST depth snapshot to resync after gap."""
            try:
                import urllib.request
                url = BINANCE_DEPTH_REST_URL.format(symbol=sym.upper())
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(
                    None, lambda: _json.loads(urllib.request.urlopen(url, timeout=5).read())
                )
                return data.get("lastUpdateId")
            except Exception as e:
                log.warning("Failed to fetch depth snapshot for %s: %s", sym, e)
                return None
        
        while True:
            try:
                depth_url = BINANCE_DEPTH_URL.format(symbol=sym)
                trade_url = BINANCE_TRADE_URL.format(symbol=sym)
                
                # Reset sequence state on reconnect
                last_update_id = None
                last_msg_time = 0
                
                # Reset DOM persistence on reconnect (P1)
                dom_persist[sym]["bid_wall_hits"] = 0
                dom_persist[sym]["bid_wall_price"] = 0
                dom_persist[sym]["ask_wall_hits"] = 0
                dom_persist[sym]["ask_wall_price"] = 0
                
                async with websockets.connect(depth_url, ping_interval=20, ping_timeout=10) as d_ws, \
                           websockets.connect(trade_url, ping_interval=20, close_timeout=5) as t_ws:
                    log.info("DOM+Trade feed connected for %s", sym)

                    async def _process_depth():
                        nonlocal last_update_id, last_msg_time
                        async for raw in d_ws:
                            try:
                                data = _json.loads(raw)
                                
                                # P1: Sequence validation — detect gaps in depth updates
                                U = data.get("U")  # first update ID in event
                                u = data.get("u")  # final update ID in event
                                pu = data.get("pu")  # final update ID in previous event
                                
                                if last_update_id is not None:
                                    # Check for gap: previous event's final ID should match our last
                                    if pu is not None and pu != last_update_id:
                                        log.warning("Sequence gap for %s: expected pu=%s, got pu=%s — resyncing", sym, last_update_id, pu)
                                        # Gap detected — discard and resync from REST
                                        snap_id = await _fetch_depth_snapshot()
                                        if snap_id:
                                            last_update_id = snap_id
                                        continue
                                
                                # Update last message time for heartbeat
                                last_msg_time = time.time()
                                if u is not None:
                                    last_update_id = u
                                
                                bids = data.get("b", [])
                                asks = data.get("a", [])
                                if not bids or not asks:
                                    continue
                                best_bid = float(bids[0][0])
                                best_ask = float(asks[0][0])
                                spread = best_ask - best_bid
                                bid_wall = max(bids, key=lambda x: float(x[1]))
                                ask_wall = max(asks, key=lambda x: float(x[1]))
                                bid_wall_price = float(bid_wall[0])
                                bid_wall_vol = float(bid_wall[1])
                                ask_wall_price = float(ask_wall[0])
                                ask_wall_vol = float(ask_wall[1])
                                total_bid = sum(float(b[1]) for b in bids[:15])
                                total_ask = sum(float(a[1]) for a in asks[:15])
                                imb = ((total_bid - total_ask) / (total_bid + total_ask)) * 100 if (total_bid + total_ask) > 0 else 0

                                # P6: DOM persistence filter — require wall to persist across reads
                                dp = dom_persist[sym]

                                # P7: time-averaged book imbalance. One top-15 snapshot is
                                # dominated by sampling noise (mean |Δ| between 15s polls measured
                                # at 1.44x the whole cross-symbol spread), so publish an
                                # exponentially-weighted average next to the raw value. Consumers
                                # that score on imbalance should use imbalance_pct_ema.
                                import math as _math
                                _now_ema = time.time()
                                _prev_ema = dp.get("imb_ema")
                                if _prev_ema is None:
                                    imb_ema = imb
                                else:
                                    _dt = max(_now_ema - dp.get("imb_ema_ts", _now_ema), 0.0)
                                    _alpha = 1.0 - _math.exp(-_dt / _DOM_IMB_EMA_TAU_S)
                                    imb_ema = _prev_ema + _alpha * (imb - _prev_ema)
                                dp["imb_ema"] = imb_ema
                                dp["imb_ema_ts"] = _now_ema
                                dp["imb_ema_n"] = int(dp.get("imb_ema_n", 0)) + 1
                                if dp["bid_wall_price"] == bid_wall_price:
                                    dp["bid_wall_hits"] = min(dp["bid_wall_hits"] + 1, DOM_PERSIST_REQUIRED)
                                else:
                                    dp["bid_wall_price"] = bid_wall_price
                                    dp["bid_wall_hits"] = 1
                                if dp["ask_wall_price"] == ask_wall_price:
                                    dp["ask_wall_hits"] = min(dp["ask_wall_hits"] + 1, DOM_PERSIST_REQUIRED)
                                else:
                                    dp["ask_wall_price"] = ask_wall_price
                                    dp["ask_wall_hits"] = 1

                                async with dom_lock:
                                    s = dom_state[sym]
                                    s["best_bid"] = best_bid
                                    s["best_ask"] = best_ask
                                    s["spread"] = round(spread, 4)
                                    s["spread_pct"] = round((spread / best_bid) * 100, 4) if best_bid else 0
                                    s["bid_wall_price"] = bid_wall_price
                                    s["bid_wall_vol"] = bid_wall_vol
                                    s["ask_wall_price"] = ask_wall_price
                                    s["ask_wall_vol"] = ask_wall_vol
                                    s["total_bid_vol"] = round(total_bid, 2)
                                    s["total_ask_vol"] = round(total_ask, 2)
                                    s["imbalance_pct"] = round(imb, 1)
                                    s["imbalance_pct_ema"] = round(imb_ema, 1)
                                    s["imbalance_ema_samples"] = dp["imb_ema_n"]
                                    s["bids"] = [[p, v] for p, v in bids[:15]]
                                    s["asks"] = [[p, v] for p, v in asks[:15]]
                                    s["bid_wall_persistent"] = dp["bid_wall_hits"] >= DOM_PERSIST_REQUIRED
                                    s["ask_wall_persistent"] = dp["ask_wall_hits"] >= DOM_PERSIST_REQUIRED
                                    s["ts"] = time.time()
                            except Exception:
                                pass

                    async def _process_trades():
                        async for raw in t_ws:
                            try:
                                data = _json.loads(raw)
                                price = float(data.get("p", 0))
                                qty = float(data.get("q", 0))
                                is_buyer_maker = data.get("m", False)
                                side = "sell" if is_buyer_maker else "buy"
                                usd = price * qty
                                now = time.time()
                                trade = {"p": price, "q": qty, "usd": round(usd, 2), "s": side, "t": now}
                                async with dom_lock:
                                    s = dom_state[sym]
                                    rt = s["recent_trades"]
                                    rt.append(trade)
                                    cutoff = now - TRADE_SUMMARY_WINDOW
                                    # Time-based eviction: filter by window first, cap only after
                                    s["recent_trades"] = [t for t in rt if t["t"] >= cutoff]
                                    valid_trades = s["recent_trades"]
                                    if len(valid_trades) > RECENT_TRADES_MAX * 3:
                                        s["recent_trades"] = valid_trades[-RECENT_TRADES_MAX * 3:]
                                        valid_trades = s["recent_trades"]
                                    buy_vol = sum(t["q"] for t in valid_trades if t["s"] == "buy")
                                    sell_vol = sum(t["q"] for t in valid_trades if t["s"] == "sell")
                                    buy_usd = sum(t["usd"] for t in valid_trades if t["s"] == "buy")
                                    sell_usd = sum(t["usd"] for t in valid_trades if t["s"] == "sell")
                                    buy_count = sum(1 for t in valid_trades if t["s"] == "buy")
                                    sell_count = sum(1 for t in valid_trades if t["s"] == "sell")
                                    s["trade_summary"] = {
                                        "buy_vol": round(buy_vol, 4),
                                        "sell_vol": round(sell_vol, 4),
                                        "buy_usd": round(buy_usd, 2),
                                        "sell_usd": round(sell_usd, 2),
                                        "buy_count": buy_count,
                                        "sell_count": sell_count,
                                        "net": round(buy_vol - sell_vol, 4),
                                        "net_usd": round(buy_usd - sell_usd, 2),
                                        "window_sec": TRADE_SUMMARY_WINDOW,
                                    }
                                    s["ts"] = now
                                # taker CVD per-symbol update (outside dom_lock to avoid deadlock; uses its own lock)
                                try:
                                    delta_qty = qty if side == "buy" else -qty
                                    delta_usd = usd if side == "buy" else -usd
                                    _taker_cvd_update(sym, delta_qty, delta_usd)
                                except Exception:
                                    pass
                            except Exception:
                                pass

                    async def _heartbeat_check():
                        """P2: Check for stale WebSocket feed (no messages for >30s)."""
                        while True:
                            await asyncio.sleep(10)
                            if last_msg_time > 0 and time.time() - last_msg_time > 30:
                                log.warning("Heartbeat timeout for %s: no message for 30s — reconnecting", sym)
                                raise ConnectionError(f"Heartbeat timeout for {sym}")
                    
                    await asyncio.gather(_process_depth(), _process_trades(), _heartbeat_check())
            except Exception as e:
                log.warning("DOM feed error for %s: %s", sym, e)
                try:
                    import watchdog as _wd
                    _wd.STATE.record_ws_reconnect(sym)
                except Exception:
                    pass
                await asyncio.sleep(5)

    tasks = [asyncio.create_task(_run_symbol_feed(sym)) for sym in symbols]
    await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class AtasAlert(BaseModel):
    ticker: str = Field(default="UNKNOWN", min_length=1, max_length=50)
    volumen: str = Field(default="0", max_length=30)
    delta: str = Field(default="0", max_length=30)
    precio: str = Field(default="0", max_length=30)


class BridgeState(BaseModel):
    ticker: str
    window_size: int
    alerts: list[dict]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def enrich_buffer_ages(buf: deque, now: float) -> None:
    for e in buf:
        e["t"] = now - e["ts"]


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
LAST_TELEGRAM_SEND = 0

# ATAS Telegram dedup (spam fix): ATAS re-fires a persistent wall every few
# seconds, and some setups send the same BigTrade twice (generic + side-
# prefixed). Without filtering, every webhook becomes a Telegram message and
# floods the rolling buffer. Two gates:
#   1. exact-msg dedup: identical raw string within ATAS_TG_EXACT_DEDUP_SEC
#   2. DOM wall cooldown: same (ticker|side|price) within ATAS_TG_DOM_COOLDOWN_SEC
ATAS_TG_EXACT_DEDUP_SEC = float(os.getenv("ATAS_TG_EXACT_DEDUP_SEC", "10"))
ATAS_TG_DOM_COOLDOWN_SEC = float(os.getenv("ATAS_TG_DOM_COOLDOWN_SEC", "90"))
_ATG_LAST_EXACT: dict[str, float] = {}
_ATG_LAST_DOM: dict[str, float] = {}

async def send_telegram(text: str) -> None:
    global LAST_TELEGRAM_SEND
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "none":
        return
    
    # Simple rate limiting: 2 seconds between messages
    now = time.time()
    if now - LAST_TELEGRAM_SEND < 2.0:
        await asyncio.sleep(2.0 - (now - LAST_TELEGRAM_SEND))
    
    payload = f"chat_id={TELEGRAM_CHAT_ID}&text={quote(text)}&parse_mode=HTML"
    try:
        req = URLRequest(TELEGRAM_API, data=payload.encode(), method="POST")
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: urlopen(req, timeout=5)
        )
        LAST_TELEGRAM_SEND = time.time()
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


TELEGRAM_GET_UPDATES_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"


async def _send_telegram_to_chat(chat_id: int, text: str) -> None:
    """Send a Telegram message to a specific chat ID."""
    global LAST_TELEGRAM_SEND
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    # Simple rate limiting: 2 seconds between messages
    now = time.time()
    if now - LAST_TELEGRAM_SEND < 2.0:
        await asyncio.sleep(2.0 - (now - LAST_TELEGRAM_SEND))
        
    payload = f"chat_id={chat_id}&text={quote(text)}&parse_mode=HTML"
    try:
        req = URLRequest(api, data=payload.encode(), method="POST")
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: urlopen(req, timeout=5)
        )
        LAST_TELEGRAM_SEND = time.time()
    except Exception as e:
        log.warning("Telegram send to %s failed: %s", chat_id, e)


def _build_status_text() -> str:
    """Build a quick status string from live indicators."""
    data = _read_live_data()
    if not data:
        return "⚠️ No live data available."
    p = data.get("price", {})
    dom = data.get("dom", {})
    vol = data.get("volume", {})
    dt = data.get("delta", 0)
    imbalance = dom.get("imbalance_pct", 0)
    ticker = data.get("ticker", "UNKNOWN")  # P3 FIX: Use actual ticker from live data
    lines = [
        f"<b>{ticker} LIVE STATUS</b>",
        f"Price: ${p.get('last', '?')}",
        f"OHLC: {p.get('high', '?')} / {p.get('low', '?')}",
        f"Delta: {dt:+.1f}",
        f"Volume: {vol.get('total', 0):,.0f}",
        f"DOM Imbalance: {'+' if imbalance >= 0 else ''}{imbalance:.1f}%",
    ]
    return "\n".join(lines)


async def telegram_polling():
    """Background task: polls Telegram for incoming commands and responds."""
    offset = 0
    while True:
        try:
            url = f"{TELEGRAM_GET_UPDATES_API}?offset={offset}&timeout=30"
            req = URLRequest(url)
            resp = await asyncio.get_running_loop().run_in_executor(
                None, lambda: urlopen(req, timeout=35)
            )
            data = _json.loads(resp.read().decode())
            if data.get("ok"):
                for update in data.get("result", []):
                    update_id = update.get("update_id", 0)
                    offset = max(offset, update_id + 1)
                    msg = update.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    text = (msg.get("text", "") or "").strip().lower()
                    if not chat_id or not text:
                        continue
                    log.info("TG command received: '%s' from chat_id=%s", text, chat_id)
                    if text in ("/analizar", "/analysis", "/a"):
                        live = _read_live_data()
                        ticker = live.get("ticker", "SUIUSDT") if live else "SUIUSDT"
                        log.info("Building analysis for ticker=%s", ticker)
                        async with ticker_locks[ticker]:
                            snapshot = list(ticker_buffers[ticker])
                        resp_text = _build_analysis(ticker, snapshot)
                        log.info("Analysis built (%d chars), sending to chat %s", len(resp_text), chat_id)
                        await _send_telegram_to_chat(chat_id, resp_text)
                        log.info("Analysis sent successfully")
                    elif text in ("/status", "/price", "/s"):
                        await _send_telegram_to_chat(chat_id, _build_status_text())
                    elif text in ("/ayuda", "/help", "/start", "/h", "/comandos"):
                        help_text = (
                            "<b>🤖 ATAS Bridge Commands</b>\n\n"
                            "/analizar — Full analysis\n"
                            "/status — Price + DOM snapshot\n"
                            "/help — This message"
                        )
                        await _send_telegram_to_chat(chat_id, help_text)
        except Exception as e:
            log.warning("Telegram polling error: %s", e)
        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Live indicators from ATAS exporter
# ---------------------------------------------------------------------------
# LiveExporter output path. Resolved relative to THIS bridge's directory so the
# bridge never depends on a hardcoded absolute path (was C:\atas-bridge, which
# got deleted when the repo moved to trading-engines\atas-bridge — the source
# of "frozen /indicators" data). Overridable via ATAS_BRIDGE_DIR env.
_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_LIVE_INDICATORS_FILE = os.environ.get(
    "ATAS_BRIDGE_DIR",
    os.path.join(_BRIDGE_DIR, "_live_indicators.json"),
)
# Where the compiled LiveExporter.dll actually writes its per-symbol files.
# The DLL (LiveExporter.cs) writes to THIS directory by default (matching
# the bridge's own dir). Overridable via ATAS_BRIDGE_DIR env.
_LIVEEXPORTER_OUTPUT_DIR = os.environ.get(
    "ATAS_BRIDGE_DIR",
    _BRIDGE_DIR,
)
# Max age (s) for a LiveExporter snapshot to be considered "live" — older than
# this is frozen and must NOT be served as current data (kills the "same stale
# alert every 15 min" behaviour at the source).
_LIVE_STALE_LIMIT_SECONDS = 300

# ── LIVE-ONLY GATE ────────────────────────────────────────────────────────
# Standing rule: analysis may ONLY use data that is live at the moment of the
# ask. Nothing frozen, restored-from-disk, or carried over from a previous run
# may be served as if it were current. Two mechanisms:
#
#   1. Per-symbol freshness. The exporter writes one file PER SYMBOL, so a
#      single frozen symbol must never be answered with a sibling's data or
#      with its own stale snapshot. Every read surface filters on payload age.
#   2. Derived-state freshness. CVD / taker-CVD / alert buffers are accumulated
#      state; a symbol whose source stopped is DROPPED rather than served at a
#      frozen value, because a stale CVD reads as a live one on a chart.
#
# Default 300s was chosen because the exporter publishes every ~300ms while a
# chart is calculating, and a stopped chart is what we want to surface. Override
# with ATAS_LIVE_MAX_AGE_SECONDS when a symbol legitimately updates slower.
_LIVE_MAX_AGE_SECONDS = float(os.environ.get("ATAS_LIVE_MAX_AGE_SECONDS", "300"))


def _payload_age_seconds(payload: dict):
    """Age (s) of a LiveExporter payload from its own `timestamp`, else None."""
    if not isinstance(payload, dict):
        return None
    ts = payload.get("timestamp")
    if ts is None:
        return None
    try:
        return max(0.0, time.time() - (float(ts) / 1000.0))
    except (TypeError, ValueError):
        return None


def _is_live_payload(payload: dict) -> bool:
    """True only if the payload was written within the live window."""
    age = _payload_age_seconds(payload)
    return age is not None and age <= _LIVE_MAX_AGE_SECONDS


def _live_indicator_symbols():
    """Map symbol -> age_seconds for EVERY currently-live exporter file.

    A file that exists but is frozen (chart stopped calculating) is simply
    absent from the result — callers use this to exclude it rather than to
    discover it late.
    """
    import glob as _glob
    now = time.time()
    out = {}
    dirs = []
    for d in (_LIVEEXPORTER_OUTPUT_DIR, _BRIDGE_DIR):
        if d and d not in dirs and os.path.isdir(d):
            dirs.append(d)
    for d in dirs:
        for path in _glob.glob(os.path.join(d, "_live_indicators_*.json")):
            try:
                # mtime alone is not enough: a direct-overwrite fallback write
                # can bump mtime, so trust the payload's own timestamp.
                content = _shared_read.read_text(path)
                if not content.strip():
                    continue
                payload = _json.loads(content)
            except Exception:
                continue
            age = _payload_age_seconds(payload)
            if age is None or age > _LIVE_MAX_AGE_SECONDS:
                continue
            if now - os.path.getmtime(path) > _LIVE_MAX_AGE_SECONDS:
                continue
            sym = str(payload.get("ticker") or "").upper().strip()
            if not sym:
                base = os.path.basename(path)
                sym = base[len("_live_indicators_"):-len(".json")].upper()
            if sym:
                out[sym] = age
    return out

# P2: Feature/tick history — append-only JSONL per poll
_TICK_HISTORY_PATH = os.path.join(os.path.dirname(__file__), "_tick_history.jsonl")
_TICK_HISTORY_MAX_BYTES = 20 * 1024 * 1024  # rotate at 20MB (audit P1 fix)
# P2 FIX: Use threading.Lock instead of asyncio.Lock to avoid blocking event loop
import threading
_TICK_HISTORY_LOCK = threading.Lock()

async def _append_tick_history(entry: dict):
    """Persist a feature snapshot to append-only JSONL.
    P2 FIX: Add error handling and use threading.Lock to avoid blocking event loop."""
    try:
        line = _json.dumps(entry, default=str) + "\n"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: _append_to_file_with_lock(_TICK_HISTORY_PATH, line))
    except Exception as e:
        log.warning("Failed to append tick history: %s", e)

def _append_to_file_with_lock(path: str, text: str):
    """Append to file with thread-safe locking and size-based rotation."""
    with _TICK_HISTORY_LOCK:
        try:
            if os.path.exists(path) and os.path.getsize(path) > _TICK_HISTORY_MAX_BYTES:
                backup1 = path + ".1"
                if os.path.exists(backup1):
                    os.replace(backup1, path + ".2")
                os.replace(path, backup1)
        except Exception as e:
            log.warning("Tick history rotation failed: %s", e)
        with open(path, "a") as f:
            f.write(text)

def _append_to_file(path: str, text: str):
    with open(path, "a") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# CVD (Cumulative Volume Delta) tracker
# ---------------------------------------------------------------------------
# Polls _live_indicators.json and accumulates closed-bar deltas per symbol.
# Bar rollover is detected via bar_index change; the closed bar's final delta
# is the last live delta observed for that bar (LiveExporter writes every
# 300ms, so this is accurate within one write cycle).
_CVD_STATE_PATH = os.path.join(os.path.dirname(__file__), "_cvd_state.json")
_CVD_POLL_SECONDS = float(os.getenv("CVD_POLL_SECONDS", "0.2"))
_CVD_MAX_BAR_DELTAS = 48            # closed-bar deltas kept per symbol
_CVD_STATE_MAX_AGE_H = 48.0         # prune symbols older than this on load
# A single bar advance faster than this is not a trustworthy close (quick
# symbol flip-back or sub-minute chart) — skip the bar, keep the segment.
_CVD_MIN_BAR_SECONDS = float(os.getenv("CVD_MIN_BAR_SECONDS", "20.0"))
# Bar index advancing at file-write cadence = history backfill/replay
# streaming in after a chart reload. A sustained race resets the segment.
_CVD_RACE_SECONDS = float(os.getenv("CVD_RACE_SECONDS", "2.0"))
_CVD_RACE_RESET_AFTER = int(os.getenv("CVD_RACE_RESET_AFTER", "3"))

_cvd_state: dict[str, dict] = {}
_CVD_LOCK = threading.Lock()
_cvd_dirty = False
# Why the current segment started, and when. A DLL redeploy or timeframe switch
# resets the segment (bar_index goes backwards -> bars not comparable), which
# silently un-evaluates rules.json rules 6/7 condition (a): they need 2+
# consecutive CLOSED bars, and a fresh segment starts with zero. The log line
# says so, but nothing a consumer reads reported it. /cvd now publishes
# segment_started_ts + segment_reason, and /health flags a young segment.
_cvd_segment_reason: str = ""
_cvd_segment_ts: float = 0.0

# A segment younger than this has too few closed bars for rules 6/7 condition
# (a) to be satisfiable. Reported, not enforced.
_CVD_WARMUP_BARS = 2


def _cvd_new_state() -> dict:
    return {
        "cvd": 0.0,                 # session cumulative delta (closed bars) - raw contracts
        "cvd_usd": 0.0,             # session cumulative delta in USD notional (delta * price)
        "bars": 0,                  # closed bars counted this segment
        "high_cvd": 0.0,
        "low_cvd": 0.0,
        "high_cvd_usd": 0.0,
        "low_cvd_usd": 0.0,
        "prev_high_cvd": 0.0,       # extremes before the last closed bar
        "prev_low_cvd": 0.0,
        "prev_cvd": 0.0,            # CVD before the last closed bar
        "prev_cvd_usd": 0.0,
        "last_delta": 0.0,          # last closed bar's delta
        "last_delta_usd": 0.0,
        "bar_deltas": [],           # recent closed-bar deltas, oldest -> newest
        "bar_deltas_usd": [],       # same in USD notional
        "last_bar_index": None,
        "current_bar_delta": 0.0,   # live delta of the in-progress bar
        "current_bar_delta_usd": 0.0,
        "current_bar_volume": 0.0,  # live volume of the in-progress bar
        "last_bar_change_ts": 0.0,  # wall-clock of last accepted bar transition
        "last_price": 0.0,
        "segments": 0,              # reloads/timeframe switches seen
        "gaps": 0,                  # bar gaps seen (session kept, not wiped)
        "missing_bars": 0,          # total bars skipped inside gaps
        "bars_since_gap": None,     # clean closes since last gap (None = never gapped)
        "fast_skips": 0,            # untrustworthy fast closes skipped
        "race_ticks": 0,            # consecutive backfill-cadence advances
        "updated_ts": 0.0,
        "_src_ts": 0,               # last LiveExporter timestamp processed
    }


def _cvd_load() -> None:
    """Restore persisted per-symbol CVD state from disk."""
    try:
        if not os.path.exists(_CVD_STATE_PATH):
            return
        with open(_CVD_STATE_PATH, "r") as f:
            raw = _json.load(f)
        now = time.time()
        for sym, st in (raw or {}).items():
            if not isinstance(st, dict):
                continue
            if now - st.get("updated_ts", 0) > _CVD_STATE_MAX_AGE_H * 3600:
                continue
            merged = _cvd_new_state()
            merged.update(st)
            merged["last_bar_index"] = None  # bar continuity unknown after restart
            _cvd_state[sym.upper()] = merged
        log.info("CVD state loaded: %s", ", ".join(_cvd_state) or "empty")
    except Exception as e:
        log.warning("CVD state load failed: %s", e)


def _cvd_save_sync() -> None:
    """Persist CVD state to disk (atomic write). Resets the dirty flag."""
    global _cvd_dirty
    try:
        with _CVD_LOCK:
            payload = _json.dumps(_cvd_state)
            _cvd_dirty = False
        tmp = _CVD_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(payload)
        os.replace(tmp, _CVD_STATE_PATH)
    except Exception as e:
        log.warning("CVD state save failed: %s", e)


def _cvd_process_live(data: dict) -> None:
    """Update CVD state from one LiveExporter snapshot (pure, in-memory).

    Corruption guards (validated against the July 17-18 segment-2/3 poison):
      1. RACE GATE — bar_index advancing at file-write cadence (<2s) is
         history backfill/replay streaming after a chart reload. Ticks are
         skipped; a sustained race (>=3 consecutive) resets the segment.
      2. FAST-CLOSE GATE — a single bar advance in <20s (quick symbol
         flip-back or sub-minute chart) has unreliable delta attribution:
         the bar is skipped, session state is KEPT.
      3. GAP GATE — bar_index skipping ahead means bars closed unobserved
         (symbol switch, ATAS paused). Already-accumulated data stays valid;
         the boundary bar is skipped and the gap recorded. No reset.
      4. RELOAD GATE — bar_index moving backward = chart reload or timeframe
         switch: bar semantics changed, segment resets.
      5. PHYSICAL GATE — |closed_delta| can never exceed closed volume
         (delta = ask - bid, volume = ask + bid). Reject impossible values.
    """
    global _cvd_dirty
    ticker = (data.get("ticker") or "?").upper()
    if ticker == "?":
        return
    delta = data.get("delta")
    if delta is None:
        return
    src_ts = data.get("timestamp") or 0
    bar_index = data.get("bar_index")
    price = (data.get("price") or {}).get("last", 0.0)
    try:
        bar_volume = float((data.get("volume") or {}).get("total") or 0.0)
    except (TypeError, ValueError):
        bar_volume = 0.0
    now = time.time()

    with _CVD_LOCK:
        st = _cvd_state.setdefault(ticker, _cvd_new_state())
        if src_ts and src_ts == st.get("_src_ts"):
            return  # stale file (ATAS paused/closed) — nothing new
        st["_src_ts"] = src_ts

        prev_bar_index = st["last_bar_index"]
        prev_live_delta = st["current_bar_delta"]
        prev_bar_volume = st["current_bar_volume"]
        st["current_bar_delta"] = float(delta)
        st["current_bar_delta_usd"] = float(delta) * float(price) if price else float(delta)
        st["current_bar_volume"] = bar_volume
        st["last_price"] = price
        st["updated_ts"] = now

        if bar_index is None:
            return
        bar_index = int(bar_index)

        if prev_bar_index is None:
            st["last_bar_index"] = bar_index  # first observation — seed only
            st["last_bar_change_ts"] = now
            return

        def _reset(msg: str, b_index: int, count_segment: bool = True) -> None:
            global _cvd_segment_reason, _cvd_segment_ts
            fresh = _cvd_new_state()
            fresh.update({
                "segments": st["segments"] + (1 if count_segment else 0),
                "last_bar_index": b_index,
                "last_bar_change_ts": now,
                "last_price": price,
                "updated_ts": now,
                "_src_ts": src_ts,
            })
            fresh["segment_started_ts"] = now
            fresh["segment_reason"] = msg
            _cvd_state[ticker] = fresh
            # Publish the reason so /cvd and /health can say WHY the rules
            # cannot fire, instead of a consumer inferring it from zeros.
            _cvd_segment_reason = msg
            _cvd_segment_ts = now
            log.warning("CVD %s: %s", ticker, msg)
            _cvd_dirty = True

        if bar_index < prev_bar_index:
            # Chart reload / timeframe switch — bars no longer comparable
            _reset(f"chart reload, bar index {prev_bar_index}->{bar_index}, "
                   f"segment reseeded", bar_index)
            return

        if bar_index > prev_bar_index:
            elapsed = now - st["last_bar_change_ts"]
            gap = bar_index - prev_bar_index

            if elapsed < _CVD_RACE_SECONDS:
                # Backfill/replay streaming: bar_index advancing at file-write
                # cadence. Never accumulate; wipe only on a sustained race.
                st["race_ticks"] += 1
                if st["race_ticks"] >= _CVD_RACE_RESET_AFTER:
                    _reset(f"backfill race ({st['race_ticks']} advances at "
                           f"<{_CVD_RACE_SECONDS:.0f}s cadence), segment "
                           f"reseeded", bar_index)
                    return
                st["last_bar_index"] = bar_index
                st["last_bar_change_ts"] = now
                _cvd_dirty = True
                return
            st["race_ticks"] = 0

            if elapsed < _CVD_MIN_BAR_SECONDS:
                # Single fast close (quick symbol flip-back or sub-minute
                # chart): the closed bar's delta attribution is unreliable.
                # Skip it — do NOT wipe valid session history.
                st["fast_skips"] += 1
                st["bars_since_gap"] = 0
                st["last_bar_index"] = bar_index
                st["last_bar_change_ts"] = now
                log.warning(
                    "CVD %s: fast close after %.1fs — bar %d skipped "
                    "(no reset)", ticker, elapsed, prev_bar_index,
                )
                _cvd_dirty = True
                return

            if gap > 1:
                # Bars closed unobserved (symbol/timeframe looked away). The
                # accumulated session data is still valid — record the gap,
                # skip the unverifiable boundary bar, keep accumulating.
                st["gaps"] += 1
                st["missing_bars"] += gap - 1
                st["bars_since_gap"] = 0
                st["last_bar_index"] = bar_index
                st["last_bar_change_ts"] = now
                log.warning(
                    "CVD %s: bar gap %d->%d (%d missed) — boundary skipped, "
                    "session kept", ticker, prev_bar_index, bar_index, gap - 1,
                )
                _cvd_dirty = True
                return

            # Clean close: the closed bar's final delta = last observed live
            # delta (LiveExporter delta is the bar's true delta-so-far).
            closed_delta = prev_live_delta
            closed_vol = prev_bar_volume
            ok = True
            if closed_vol > 0 and abs(closed_delta) > closed_vol * 1.1 + 1.0:
                log.warning(
                    "CVD %s: closed delta %.1f > volume %.1f — impossible, "
                    "bar %d rejected",
                    ticker, closed_delta, closed_vol, prev_bar_index,
                )
                ok = False
            elif closed_vol <= 0:
                # No volume info: sanity-check against recent closed deltas
                recent = [abs(d) for d in st["bar_deltas"][-8:]]
                if recent and abs(closed_delta) > 3.0 * max(recent):
                    log.warning(
                        "CVD %s: closed delta %.1f is %.0fx recent max — "
                        "bar %d rejected",
                        ticker, closed_delta, abs(closed_delta) / max(recent),
                        prev_bar_index,
                    )
                    ok = False
            if ok:
                closed_delta_usd = closed_delta * price if price else closed_delta
                st["prev_cvd"] = st["cvd"]
                st["prev_cvd_usd"] = st.get("cvd_usd", 0.0)
                st["prev_high_cvd"] = st["high_cvd"]
                st["prev_low_cvd"] = st["low_cvd"]
                st["cvd"] += closed_delta
                st["cvd_usd"] = st.get("cvd_usd", 0.0) + closed_delta_usd
                st["bars"] += 1
                st["last_delta"] = closed_delta
                st["last_delta_usd"] = closed_delta_usd
                st["high_cvd"] = max(st["high_cvd"], st["cvd"])
                st["low_cvd"] = min(st["low_cvd"], st["cvd"])
                st["high_cvd_usd"] = max(st.get("high_cvd_usd", 0.0), st["cvd_usd"])
                st["low_cvd_usd"] = min(st.get("low_cvd_usd", 0.0), st["cvd_usd"])
                st["bar_deltas"].append(round(closed_delta, 4))
                st["bar_deltas_usd"] = st.get("bar_deltas_usd", [])
                st["bar_deltas_usd"].append(round(closed_delta_usd, 2))
                if len(st["bar_deltas"]) > _CVD_MAX_BAR_DELTAS:
                    st["bar_deltas"] = st["bar_deltas"][-_CVD_MAX_BAR_DELTAS:]
                    st["bar_deltas_usd"] = st["bar_deltas_usd"][-_CVD_MAX_BAR_DELTAS:]
                if st["bars_since_gap"] is not None:
                    st["bars_since_gap"] += 1
            st["last_bar_index"] = bar_index
            st["last_bar_change_ts"] = now
            _cvd_dirty = True


def _cvd_get(symbol: str) -> dict | None:
    """Thread-safe read of one symbol's CVD state."""
    with _CVD_LOCK:
        st = _cvd_state.get(symbol.upper())
        return dict(st) if st else None


async def _cvd_tracker():
    """Background loop: poll LiveExporter file and track CVD per symbol."""
    _cvd_load()
    log.info("CVD tracker started (poll %.2fs)", _CVD_POLL_SECONDS)
    while True:
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _read_live_data_sync)
            if data:
                _cvd_process_live(data)
                if _cvd_dirty:
                    await loop.run_in_executor(None, _cvd_save_sync)
        except Exception as e:
            log.warning("CVD tracker error: %s", e)
        await asyncio.sleep(_CVD_POLL_SECONDS)


def _cvd_public(sym: str, st: dict) -> dict:
    """Public view of a symbol's CVD state."""
    cur_usd = st.get("current_bar_delta", 0) * st.get("last_price", 0) if st.get("last_price") else 0
    now = time.time()
    return {
        "symbol": sym,
        "cvd": round(st["cvd"], 4),
        "cvd_usd": round(st.get("cvd_usd", 0.0), 2),
        "bars": st["bars"],
        # Rules 6/7 condition (a) needs 2+ consecutive closed bars. A segment
        # younger than that CANNOT satisfy it, and a reset (DLL redeploy,
        # timeframe switch, chart reload) is the usual cause. Publish both so
        # "rules cannot fire" is distinguishable from "market is flat".
        "warmup": bool(st["bars"] < _CVD_WARMUP_BARS),
        "warmup_bars_needed": _CVD_WARMUP_BARS,
        "segment_started_ts": round(st.get("segment_started_ts", 0.0), 1),
        "segment_age_s": round(now - st.get("segment_started_ts", now), 1),
        "segment_reason": st.get("segment_reason", ""),
        "high_cvd": round(st["high_cvd"], 4),
        "low_cvd": round(st["low_cvd"], 4),
        "high_cvd_usd": round(st.get("high_cvd_usd", 0.0), 2),
        "low_cvd_usd": round(st.get("low_cvd_usd", 0.0), 2),
        "prev_cvd": round(st["prev_cvd"], 4),
        "prev_cvd_usd": round(st.get("prev_cvd_usd", 0.0), 2),
        "last_delta": round(st["last_delta"], 4),
        "last_delta_usd": round(st.get("last_delta_usd", 0.0), 2),
        "current_bar_delta": round(st["current_bar_delta"], 4),
        "current_bar_delta_usd": round(cur_usd, 2),
        "bar_deltas": st.get("bar_deltas", [])[-12:],
        "bar_deltas_usd": st.get("bar_deltas_usd", [])[-12:],
        "last_price": st["last_price"],
        "segments": st["segments"],
        "gaps": st.get("gaps", 0),
        "missing_bars": st.get("missing_bars", 0),
        "bars_since_gap": st.get("bars_since_gap"),
        "continuity": (
            "clean"
            if st.get("bars_since_gap") is None or st.get("bars_since_gap", 0) >= 3
            else "post-gap"
        ),
        "updated_ts": st["updated_ts"],
    }


# ---------------------------------------------------------------------------
# Taker CVD per-symbol (trade-flow based, works for ALL symbols)
# ---------------------------------------------------------------------------
_TAKER_CVD_STATE_PATH = os.path.join(os.path.dirname(__file__), "_taker_cvd_state.json")
_taker_cvd_state: dict[str, dict] = {}
_TAKER_CVD_LOCK = threading.Lock()
_taker_cvd_dirty = False
# Rolling-window length for the restart-proof flow metric published as cvd_usd_1m/5m.
_TAKER_FLOW_WINDOW_S = float(os.getenv("TAKER_FLOW_WINDOW_S", "300.0"))

def _taker_cvd_new_state() -> dict:
    return {
        "cvd": 0.0,
        "cvd_usd": 0.0,
        "high_cvd": 0.0,
        "low_cvd": 0.0,
        "high_cvd_usd": 0.0,
        "low_cvd_usd": 0.0,
        "bars": 0,              # trade count
        "started_ts": 0.0,      # first-trade ts of THIS accumulator run
        "flow_win": [],         # [[ts, delta_usd], ...] trailing window for cvd_usd_1m/5m
        "updated_ts": 0.0,
    }

def _taker_cvd_load() -> None:
    try:
        if not os.path.exists(_TAKER_CVD_STATE_PATH):
            return
        with open(_TAKER_CVD_STATE_PATH, "r") as f:
            raw = _json.load(f)
        for sym, st in (raw or {}).items():
            if not isinstance(st, dict):
                continue
            merged = _taker_cvd_new_state()
            merged.update(st)
            if not merged.get("started_ts"):
                # Legacy/older state file: cvd_usd carries over from a previous run but the
                # accumulator's true start is unknown, so its age (and any per-minute rate
                # derived from it) would be wrong. Flag it instead of publishing a fake rate.
                merged["started_ts_unknown"] = True
            _taker_cvd_state[sym.upper()] = merged
        log.info("Taker CVD state loaded: %s", ", ".join(_taker_cvd_state) or "empty")
    except Exception as e:
        log.warning("Taker CVD load failed: %s", e)

def _taker_cvd_save_sync() -> None:
    global _taker_cvd_dirty
    try:
        with _TAKER_CVD_LOCK:
            payload = _json.dumps(_taker_cvd_state)
            _taker_cvd_dirty = False
        tmp = _TAKER_CVD_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(payload)
        os.replace(tmp, _TAKER_CVD_STATE_PATH)
    except Exception as e:
        log.warning("Taker CVD save failed: %s", e)

def _taker_cvd_get(symbol: str) -> dict | None:
    with _TAKER_CVD_LOCK:
        st = _taker_cvd_state.get(symbol.upper())
        return dict(st) if st else None

def _taker_cvd_public(sym: str, st: dict) -> dict:
    """Public view. `cvd_usd` is a running sum whose value depends on how long the
    symbol's WS stream has been alive (measured spread across symbols: 18.6x in tick
    count), so it is NOT comparable across symbols. The scale-free fields below are:
    `cvd_usd_per_min` (flow rate since start), `range_pos_usd` (position inside the
    session high/low range) and `age_s`."""
    now = time.time()
    started = st.get("started_ts") or 0.0
    legacy = bool(st.get("started_ts_unknown"))
    # age_s is only meaningful when this run started the accumulator from zero
    age_s = max(now - started, 0.0) if (started and not legacy) else None
    cvd_usd = st.get("cvd_usd", 0.0)
    hi = st.get("high_cvd_usd", 0.0)
    lo = st.get("low_cvd_usd", 0.0)
    win = st.get("flow_win") or []
    flow_1m = sum(e[1] for e in win if e[0] >= now - 60.0)
    flow_5m = sum(e[1] for e in win if e[0] >= now - 300.0)
    out = {
        "symbol": sym,
        "cvd": round(st.get("cvd", 0.0), 4),
        "cvd_usd": round(cvd_usd, 2),
        "high_cvd": round(st.get("high_cvd", 0.0), 4),
        "low_cvd": round(st.get("low_cvd", 0.0), 4),
        "high_cvd_usd": round(hi, 2),
        "low_cvd_usd": round(lo, 2),
        "bars": int(st.get("bars", 0)),
        "started_ts": started or None,
        "age_s": None if age_s is None else round(age_s, 1),
        "cvd_usd_1m": round(flow_1m, 2),
        "cvd_usd_5m": round(flow_5m, 2),
        "range_pos_usd": (None if (hi - lo) <= 0 else round((cvd_usd - lo) / (hi - lo), 4)),
        "updated_ts": st.get("updated_ts", 0.0),
        "_note": ("cvd_usd is a cumulative sum whose size depends on how long the stream has "
                  "been alive (ages differ ~19x across symbols): compare cvd_usd_1m, "
                  "cvd_usd_5m or range_pos_usd across symbols, never cvd_usd"),
    }
    if legacy:
        out["_note"] += (" | accumulator carried over from a previous run: cvd_usd includes "
                         "earlier flow, the rolling windows do not")
    return out

def _taker_cvd_update(symbol: str, delta_qty: float, delta_usd: float) -> None:
    global _taker_cvd_dirty
    sym = symbol.upper()
    now = time.time()
    with _TAKER_CVD_LOCK:
        st = _taker_cvd_state.setdefault(sym, _taker_cvd_new_state())
        if not st.get("started_ts"):
            st["started_ts"] = now
        st["cvd"] += delta_qty
        st["cvd_usd"] += delta_usd
        st["bars"] += 1
        st["high_cvd"] = max(st["high_cvd"], st["cvd"])
        st["low_cvd"] = min(st["low_cvd"], st["cvd"])
        st["high_cvd_usd"] = max(st["high_cvd_usd"], st["cvd_usd"])
        st["low_cvd_usd"] = min(st["low_cvd_usd"], st["cvd_usd"])
        # Rolling window: a cumulative sum is not comparable across symbols (stream ages
        # differ by ~19x), so also keep the trailing net flow per symbol.
        win = st.setdefault("flow_win", [])
        win.append([now, delta_usd])
        cutoff = now - _TAKER_FLOW_WINDOW_S
        if len(win) > 4096 or (win and win[0][0] < cutoff):
            st["flow_win"] = [e for e in win if e[0] >= cutoff]
        st["updated_ts"] = now
        _taker_cvd_dirty = True

async def _taker_cvd_saver():
    while True:
        await asyncio.sleep(5)
        if _taker_cvd_dirty:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _taker_cvd_save_sync)
            except Exception as e:
                log.warning("Taker CVD saver error: %s", e)


# ---------------------------------------------------------------------------
# Derivatives feed (OI / funding / LS-ratio)
# ---------------------------------------------------------------------------
_derivatives_state: dict[str, dict] = {}
_DERIVATIVES_LOCK = threading.Lock()
_DERIVATIVES_POLL_SECONDS = 30

def _derivatives_fetch_sync(symbol: str) -> dict | None:
    sym = symbol.upper()
    oi = None
    funding = None
    ls_ratio = None
    # OI
    try:
        url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}"
        data = _json.loads(urlopen(url, timeout=5).read())
        oi = float(data.get("openInterest", 0) or 0)
    except Exception:
        pass
    # fundingRate via premiumIndex lastFundingRate (real-time) fallback to fundingRate endpoint
    try:
        url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
        data = _json.loads(urlopen(url, timeout=5).read())
        funding = float(data.get("lastFundingRate", 0) or 0)
    except Exception:
        try:
            url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&limit=1"
            arr = _json.loads(urlopen(url, timeout=5).read())
            if isinstance(arr, list) and arr:
                funding = float(arr[0].get("fundingRate", 0) or 0)
        except Exception:
            pass
    # longShortRatio
    try:
        url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={sym}&period=5m&limit=1"
        arr = _json.loads(urlopen(url, timeout=5).read())
        if isinstance(arr, list) and arr:
            ls_ratio = float(arr[0].get("longShortRatio", 0) or 0)
        elif isinstance(arr, dict) and "longShortRatio" in arr:
            ls_ratio = float(arr["longShortRatio"] or 0)
    except Exception:
        pass
    return {"symbol": sym, "openInterest": oi, "fundingRate": funding, "longShortRatio": ls_ratio, "updated_ts": time.time()}

async def _derivatives_feed():
    symbols = ["SOLUSDT", "SUIUSDT", "HYPEUSDT", "XRPUSDT", "BTCUSDT", "ETHUSDT", "SPCXUSDT", "NVDAUSDT", "XAUUSDT"]
    log.info("Derivatives feed started (poll %ss)", _DERIVATIVES_POLL_SECONDS)
    while True:
        try:
            loop = asyncio.get_running_loop()
            for sym in symbols:
                try:
                    data = await loop.run_in_executor(None, _derivatives_fetch_sync, sym)
                    if data:
                        with _DERIVATIVES_LOCK:
                            _derivatives_state[sym] = data
                except Exception as e:
                    log.warning("Derivatives fetch error for %s: %s", sym, e)
        except Exception as e:
            log.warning("Derivatives feed error: %s", e)
        await asyncio.sleep(_DERIVATIVES_POLL_SECONDS)

def _derivatives_get(symbol: str) -> dict | None:
    with _DERIVATIVES_LOCK:
        d = _derivatives_state.get(symbol.upper())
        return dict(d) if d else None

def _derivatives_public(sym: str, st: dict) -> dict:
    return {
        "symbol": sym,
        "openInterest": st.get("openInterest"),
        "fundingRate": st.get("fundingRate"),
        "longShortRatio": st.get("longShortRatio"),
        "updated_ts": st.get("updated_ts", 0.0),
    }


# ---------------------------------------------------------------------------
# Liquidation feed (fstream !forceOrder@arr)
# ---------------------------------------------------------------------------
_liquidations: deque = deque(maxlen=50)
_LIQUIDATIONS_LOCK = threading.Lock()
_LIQUIDATION_WS_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"

def _liquidation_public(entry: dict) -> dict:
    return dict(entry)

async def _liquidation_feed():
    if not HAS_WEBSOCKETS:
        log.warning("Liquidation feed disabled — websockets not installed")
        return
    log.info("Liquidation feed started: %s", _LIQUIDATION_WS_URL)
    while True:
        try:
            async with websockets.connect(_LIQUIDATION_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                log.info("Liquidation feed connected")
                async for raw in ws:
                    try:
                        data = _json.loads(raw)
                        # data may be {"e":"forceOrder","o":{...}} or list-wrapped
                        orders = []
                        if isinstance(data, dict) and data.get("e") == "forceOrder" and "o" in data:
                            orders = [data["o"]]
                        elif isinstance(data, dict) and "o" in data and isinstance(data["o"], list):
                            orders = data["o"]
                        elif isinstance(data, list):
                            for item in data:
                                if isinstance(item, dict) and "o" in item:
                                    orders.append(item["o"])
                                elif isinstance(item, dict) and "s" in item:
                                    orders.append(item)
                        else:
                            continue
                        for o in orders:
                            try:
                                sym = o.get("s", "")
                                side = o.get("S", "")
                                qty = float(o.get("q", 0) or o.get("l", 0) or 0)
                                price = float(o.get("p", 0) or o.get("ap", 0) or 0)
                                usd = round(qty * price, 2) if qty and price else 0
                                entry = {
                                    "symbol": sym,
                                    "side": side,
                                    "qty": qty,
                                    "price": price,
                                    "usd": usd,
                                    "order_type": o.get("o", ""),
                                    "timeInForce": o.get("f", ""),
                                    "ts": time.time(),
                                    "orig": o,
                                }
                                with _LIQUIDATIONS_LOCK:
                                    _liquidations.append(entry)
                                log.info("LIQ %s %s qty=%.4f price=%.2f usd=%.2f", sym, side, qty, price, usd)
                            except Exception:
                                continue
                        # burst detection: >3 liquidations in last 10s
                        try:
                            now = time.time()
                            with _LIQUIDATIONS_LOCK:
                                recent = [e for e in _liquidations if now - e["ts"] <= 10]
                            if len(recent) >= 4:
                                # count per symbol
                                by_sym = {}
                                for e in recent:
                                    by_sym[e["symbol"]] = by_sym.get(e["symbol"], 0) + 1
                                log.warning("LIQ BURST: %d liquidations in 10s %s", len(recent), by_sym)
                        except Exception:
                            pass
                    except Exception:
                        continue
        except Exception as e:
            log.warning("Liquidation feed error: %s — reconnecting in 5s", e)
            await asyncio.sleep(5)


@app.get("/context/{symbol}")
async def context_symbol(symbol: str):
    """Quant context: value area, volatility regime, expected move (engine)."""
    sym = symbol.upper().strip("/")
    loop = asyncio.get_running_loop()
    data = await _de.get_context_async(sym, loop=loop)
    if "error" in data:
        raise HTTPException(status_code=503, detail=data["error"])
    # enrich with taker CVD + derivatives + recent liquidations
    try:
        tcvd = _taker_cvd_get(sym)
        if tcvd is None and not sym.endswith("USDT"):
            tcvd = _taker_cvd_get(sym + "USDT")
        data["taker_cvd"] = _taker_cvd_public(sym, tcvd) if tcvd else None
    except Exception:
        data["taker_cvd"] = None
    try:
        deriv = _derivatives_get(sym)
        if deriv is None and not sym.endswith("USDT"):
            deriv = _derivatives_get(sym + "USDT")
        data["derivatives"] = _derivatives_public(sym, deriv) if deriv else None
    except Exception:
        data["derivatives"] = None
    try:
        with _LIQUIDATIONS_LOCK:
            recent = [e for e in list(_liquidations) if e.get("symbol","").upper() == sym or e.get("symbol","").upper() == sym.replace("/","")]
            # also include last 5 global liquidations for cascade context
            data["recent_liquidations"] = recent[-10:]
            data["liquidations_global"] = list(_liquidations)[-5:]
    except Exception:
        data["recent_liquidations"] = []
    return data


# ---------------------------------------------------------------------------
# Trade call tracker (outcome logging)
# ---------------------------------------------------------------------------
# Every trade plan delivered in analysis is logged via POST /calls. A
# background checker polls Binance last-price and resolves each call:
# activation (observed print inside the entry zone) -> first touch of T1
# (win) or SL (loss; adverse side wins ties). MFE/MAE tracked in R while
# active. outcome_r uses planned levels (fill at zone mid assumed).
# Events are appended to _calls_log.jsonl; the open registry is rebuilt by
# replaying the log on startup. All mutations happen on the event-loop
# thread; RLock is defensive only.
_CALLS_LOG_PATH = os.path.join(os.path.dirname(__file__), "_calls_log.jsonl")
# Audit M-2 action 7 (2026-09-17): Rule 8's loss trail. The resolver appends one
# JSON line per resolved call (all outcomes, not just losses) with a deterministic
# root_cause_hint derived from data it already holds (MFE/MAE/hold/activation).
_AUDIT_LOG_PATH = os.path.join(os.path.dirname(__file__), "audit.log.json")
_CALLS_POLL_SECONDS = float(os.getenv("CALLS_POLL_SECONDS", "2.5"))
_CALLS_ACTIVATE_TTL_H = float(os.getenv("CALLS_ACTIVATE_TTL_H", "12.0"))
_CALLS_ACTIVE_TTL_H = float(os.getenv("CALLS_ACTIVE_TTL_H", "48.0"))
_CALLS_PRICE_URL = "https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}"
# Audit H2: cost model — round-trip taker fee + slippage haircut (bps of notional).
# Applied to every resolved outcome as cost_r = (bps/1e4) * mid / risk, converting
# the price-space cost into R units. Override via CALLS_COST_BPS.
CALLS_COST_BPS = float(os.getenv("CALLS_COST_BPS", "8.0"))  # 4bp taker x2 + 0 slippage default

# Excursion sanity. MFE/MAE are updated from the SAME single price poll that can resolve a
# call (one poll apart), so a resolved winner's MFE should be ~its exit R plus at most one
# poll of overshoot, and a stopped call's MAE should be ~1.0R. Measured on the 15-call track
# record, 9/16 carried an excursion implying a >1% move inside one poll (20.1R on a 0.287%
# stop = 5.8%) and stopped calls showed a median MAE of 2.1R instead of 1.0R — i.e.
# /calls/stats avg_mfe_r was measuring bad prints. Readings above this threshold are
# excluded from MFE/MAE and appended to the log as `excursion_suspect` events instead.
_CALLS_POLL_MOVE_SANITY_PCT = float(os.getenv("CALLS_POLL_MOVE_SANITY_PCT", "1.0"))

# Time constant (seconds) of the exponentially-weighted book imbalance published as
# `imbalance_pct_ema`. A single top-15 snapshot is dominated by sampling noise: measured on
# the live feed, the mean absolute change between 15s polls is 1.44x the entire cross-symbol
# spread of per-symbol means, and the cross-symbol ranking does not survive 2 minutes
# (Spearman -0.62). Consumers should prefer the EMA; `imbalance_pct` is kept unchanged.
_DOM_IMB_EMA_TAU_S = float(os.getenv("DOM_IMB_EMA_TAU_S", "45.0"))

# Exit mode for newly created calls, recorded on every call so cohorts stay separable.
#   "logged"     - the fixed-T1 behaviour every call in the track record so far used (default)
#   "half_at_1r" - half off at +1R, remainder trailed CALLS_TRAIL_R behind the best excursion
# Measured on the 16-call track record by replaying 1m klines (exit_replay.py): the trailing
# variant improved the median from -0.55R to +0.24R and net-positive calls from 8 to 9, but
# its mean advantage (~+0.19R) is inside the noise band for n=16 AND the ground-truth outcome
# log itself disagrees with exchange prints on long-held calls — so this is OFF by default and
# meant to be A/B'd on live cohorts, not switched on faith.
_CALLS_EXIT_MODE = os.getenv("CALLS_EXIT_MODE", "half_at_1r").strip().lower()
_CALLS_HALF_AT_R = float(os.getenv("CALLS_HALF_AT_R", "1.0"))
_CALLS_HALF_FRACTION = float(os.getenv("CALLS_HALF_FRACTION", "0.5"))
_CALLS_TRAIL_R = float(os.getenv("CALLS_TRAIL_R", "0.5"))

_calls: dict[str, dict] = {}               # open/active calls by call_id
_calls_resolved: deque = deque(maxlen=50)  # recent resolved, oldest -> newest
_CALLS_LOCK = threading.RLock()


class TradeCall(BaseModel):
    symbol: str
    direction: str                         # "long" | "short"
    entry_low: float
    entry_high: float
    sl: float
    t1: float
    t2: float | None = None
    bias_score: float | None = None
    note: str | None = None
    snapshot: dict | None = None           # full order-flow snapshot for validation


def _calls_append(event: dict) -> None:
    """Append one event to the JSONL log (best-effort)."""
    try:
        with _CALLS_LOCK:
            with open(_CALLS_LOG_PATH, "a") as f:
                f.write(_json.dumps(event) + "\n")
    except Exception as e:
        log.warning("Calls log append failed: %s", e)


def _audit_root_cause(c: dict, outcome: str, outcome_r, now: float) -> str:
    """Deterministic root-cause hint from resolver-held data (audit M-2)."""
    mfe = c.get("mfe_r", 0.0) or 0.0
    mae = c.get("mae_r", 0.0) or 0.0
    if outcome == "t1_hit":
        return "target_hit"
    if outcome == "expired_unactivated":
        return "never_activated"
    if outcome == "cancelled":
        return "cancelled"
    if outcome == "sl_hit":
        if mfe >= 1.0:
            return "gave_back_gains_exit_management"
        return "stopped_no_heat"
    r = outcome_r if outcome_r is not None else 0.0
    if r > 0:
        return "profitable_nonstandard_exit"
    if r < 0:
        return "unprofitable_nonstandard_exit"
    return "flat"


_VERIFY_VENUE = "binancefapi-1m"


def _verify_path_coverage(symbol: str, direction: str, sl: float, t1: float,
                          opened_ts: float, resolved_ts: float) -> dict:
    """Exchange-truth first-touch check (audit B7).

    The resolver polls discretely, so a wick can touch SL and recover to T1
    between two polls and get logged t1_hit. This walks Binance USDT-M 1m
    klines over the call's life and reports which level printed first.
    Returns a path_coverage dict; exchange_verified=False when klines are
    unreachable (fail-open: never overturns the logged outcome — history is
    append-only, verification only FLAGS contradictions).
    """
    pc: dict = {"venue": _VERIFY_VENUE, "exchange_verified": False,
                "first_touch": "unverified", "first_touch_ts": None,
                "contradicts": False, "klines": 0}
    try:
        klines = []
        cur = int((opened_ts - 120) * 1000)
        end = int((resolved_ts + 120) * 1000)
        while cur < end:
            url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}"
                   f"&interval=1m&startTime={cur}&endTime={end}&limit=1000")
            with urlopen(url, timeout=20) as r:
                batch = _json.loads(r.read().decode())
            if not batch:
                break
            klines.extend(batch)
            cur = batch[-1][0] + 60000
            if len(batch) < 1000:
                break
        pc["klines"] = len(klines)
        if not klines:
            return pc
        long = direction == "long"
        first_sl = first_t1 = None
        for k in klines:
            h, lo = float(k[2]), float(k[3])
            hit_sl = (lo <= sl) if long else (h >= sl)
            hit_t1 = (h >= t1) if long else (lo <= t1)
            if hit_sl and hit_t1:
                first_sl = first_t1 = k[0]
                pc["first_touch"] = "ambiguous"
                break
            if hit_sl and first_sl is None:
                first_sl = k[0]
            if hit_t1 and first_t1 is None:
                first_t1 = k[0]
            if first_sl is not None and first_t1 is not None:
                break
        if pc["first_touch"] != "ambiguous":
            if first_sl is not None and (first_t1 is None or first_sl < first_t1):
                pc["first_touch"] = "sl"
                pc["first_touch_ts"] = first_sl / 1000.0
            elif first_t1 is not None:
                pc["first_touch"] = "t1"
                pc["first_touch_ts"] = first_t1 / 1000.0
            else:
                pc["first_touch"] = "neither"
        pc["exchange_verified"] = True
    except Exception as e:  # noqa: BLE001
        pc["error"] = str(e)[:120]
    return pc


def _verify_call_path_bg(call_id: str, symbol: str, direction: str,
                         sl: float, t1: float, opened_ts: float,
                         resolved_ts: float, outcome: str) -> None:
    """Background worker: exchange-verify one resolution, append `verified`
    event + audit line. Fire-and-forget (daemon thread)."""
    try:
        pc = _verify_path_coverage(symbol, direction, sl, t1,
                                   opened_ts, resolved_ts)
        logged_side = {"t1_hit": "t1", "sl_hit": "sl",
                       "half_sl": "sl"}.get(outcome)
        if logged_side and pc.get("first_touch") in ("sl", "t1"):
            pc["contradicts"] = (logged_side != pc["first_touch"])
        now = time.time()
        _calls_append({"ev": "verified", "call_id": call_id, "ts": now,
                       "path_coverage": pc})
        try:
            with _CALLS_LOCK:
                with open(_AUDIT_LOG_PATH, "a") as f:
                    f.write(_json.dumps({"ts": now, "call_id": call_id,
                                         "symbol": symbol,
                                         "verification": pc}) + "\n")
        except Exception as e:
            log.warning("Audit verify append failed: %s", e)
        if pc.get("contradicts"):
            log.warning("Call %s: logged %s contradicts exchange first-touch %s",
                        call_id, outcome, pc.get("first_touch"))
            _telegram_send_sync(
                f"⚠️ <b>{symbol} {outcome}</b> — exchange first touch was "
                f"<b>{pc.get('first_touch').upper()}</b> ({pc.get('klines')} 1m bars checked)\n"
                f"call {call_id} is mislabelled by the poll loop; it stays in the record as "
                f"logged but is <b>excluded from the verified cut</b> of the stats.")
    except Exception as e:  # noqa: BLE001
        log.warning("Path verification failed for %s: %s", call_id, e)


def _audit_append(c: dict, outcome: str, outcome_r, now: float) -> None:
    """Append one Rule-8 audit line per resolved call (best-effort)."""
    try:
        snap = c.get("snapshot") or {}
        with _CALLS_LOCK:
            with open(_AUDIT_LOG_PATH, "a") as f:
                f.write(_json.dumps({
                    "ts": now,
                    "call_id": c["call_id"],
                    "symbol": c["symbol"],
                    "direction": c.get("direction"),
                    "entry": [_calls_entry_mid(c), c.get("sl"), c.get("t1")],
                    "outcome": outcome,
                    "outcome_r": outcome_r,
                    "mfe_r": c.get("mfe_r", 0.0),
                    "mae_r": c.get("mae_r", 0.0),
                    "hold_s": round(now - c.get("opened_ts", now), 1),
                    "activated": c.get("activated_ts") is not None,
                    "signal_validated": bool(snap),
                    "root_cause_hint": _audit_root_cause(c, outcome, outcome_r, now),
                }) + "\n")
    except Exception as e:
        log.warning("Audit log append failed: %s", e)


def _calls_iter_events():
    """Yield log events, tolerating torn/corrupt lines."""
    if not os.path.exists(_CALLS_LOG_PATH):
        return
    try:
        with open(_CALLS_LOG_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield _json.loads(line)
                except Exception:
                    continue
    except Exception as e:
        log.warning("Calls log read failed: %s", e)


def _calls_replay() -> None:
    """Rebuild the open-call registry from the event log."""
    with _CALLS_LOCK:
        _calls.clear()
        _calls_resolved.clear()
        for ev in _calls_iter_events():
            kind = ev.get("ev")
            if kind == "open":
                c = dict(ev["call"])
                _calls[c["call_id"]] = c
            elif kind == "activated":
                c = _calls.get(ev["call_id"])
                if c:
                    c["status"] = "active"
                    c["activated_ts"] = ev["ts"]
            elif kind == "resolved":
                c = _calls.pop(ev["call_id"], None)
                if c:
                    c["status"] = ev["outcome"]
                    c["outcome"] = ev["outcome"]
                    c["resolved_ts"] = ev["ts"]
                    c["outcome_r"] = ev.get("outcome_r")
                    c["mfe_r"] = ev.get("mfe_r", c.get("mfe_r", 0.0))
                    c["mae_r"] = ev.get("mae_r", c.get("mae_r", 0.0))
                    c["last_price"] = ev.get("price")
                    _calls_resolved.append(c)
        log.info("Calls replayed: %d open, %d recent resolved",
                 len(_calls), len(_calls_resolved))


def _calls_entry_mid(c: dict) -> float:
    return (c["entry_low"] + c["entry_high"]) / 2.0


def _calls_risk(c: dict) -> float:
    return abs(_calls_entry_mid(c) - c["sl"]) or 1e-9


def _calls_resolve_locked(c: dict, now: float, outcome: str,
                          outcome_r, price: float, notify) -> None:
    """Finalize a call: registry -> resolved, append event, notify.
    Caller must hold _CALLS_LOCK."""
    c["status"] = outcome
    c["outcome"] = outcome
    c["resolved_ts"] = now
    c["outcome_r"] = outcome_r
    c["last_price"] = price
    _calls.pop(c["call_id"], None)
    _calls_resolved.append(dict(c))
    _calls_append({
        "ev": "resolved", "call_id": c["call_id"], "ts": now,
        "outcome": outcome, "outcome_r": outcome_r, "price": price,
        "mfe_r": c.get("mfe_r", 0.0), "mae_r": c.get("mae_r", 0.0),
    })
    _audit_append(c, outcome, outcome_r, now)
    # B7: exchange-truth verification runs in background (network I/O must
    # never block the resolver). Skipped for cancelled (no market exposure).
    # Kill-switch: CALLS_VERIFY_PATH=0.
    if outcome != "cancelled" and os.getenv("CALLS_VERIFY_PATH", "1") == "1":
        try:
            _args = (c["call_id"], c["symbol"], c.get("direction"),
                     float(c["sl"]), float(c["t1"]),
                     float(c.get("opened_ts") or now), now, outcome)
            threading.Thread(target=_verify_call_path_bg, args=_args,
                             daemon=True).start()
        except Exception as e:  # noqa: BLE001
            log.warning("Verify scheduling failed for %s: %s", c["call_id"], e)
    if notify:
        emoji = {"t1_hit": "✅", "sl_hit": "❌", "trail": "🟡", "half_sl": "🟠",
                 "expired_open": "⏱"}.get(outcome, "⏱")
        r_txt = f"{outcome_r:+.2f}R" if outcome_r is not None else "no trade"
        g = c.get("geometry") or {}
        cost = g.get("cost_r")
        net_txt = ""
        if cost is not None and outcome_r is not None:
            net_txt = f" → <b>net {outcome_r - cost:+.2f}R</b> (taker cost {cost:.2f}R)"
        held = c.get("activated_ts")
        hold_txt = (f" · held {int((now - held) / 60)}m" if held else " · never activated")
        lines = [f"{emo} <b>CALL {c['symbol']} {c['direction'].upper()}</b> — {outcome}",
                 f"entry {c['entry_low']:.6g}–{c['entry_high']:.6g} | SL {c['sl']:.6g} | "
                 f"T1 {c['t1']:.6g}",
                 f"result {r_txt}{net_txt}{hold_txt}",
                 f"MFE +{c.get('mfe_r', 0.0):.2f}R / MAE {c.get('mae_r', 0.0):.2f}R · "
                 f"exit {c.get('exit_mode', '?')}"]
        if c.get("excursion_suspect_count"):
            lines.append(f"⚠️ {c['excursion_suspect_count']} suspect print(s) excluded from MFE/MAE")
        lines.append("exchange verification running (first-touch check)")
        notify("\n".join(lines))


def _calls_step(now: float, prices: dict, notify=None) -> None:
    """One resolution pass over open calls (sync, unit-testable).

    prices: symbol -> last price (missing/<=0 symbols are skipped).
    Activation requires an observed print inside the entry zone; once
    active, SL is checked before T1 (adverse-first tie-break).
    """
    with _CALLS_LOCK:
        for cid, c in list(_calls.items()):
            price = prices.get(c["symbol"])
            if price is None or price <= 0:
                continue
            _prev_price = c.get("last_price")
            c["last_price"] = price
            long = c["direction"] == "long"
            mid = _calls_entry_mid(c)
            risk = _calls_risk(c)

            if c["status"] == "open":
                if now - c["opened_ts"] > _CALLS_ACTIVATE_TTL_H * 3600:
                    _calls_resolve_locked(c, now, "expired_unactivated",
                                          None, price, notify)
                    continue
                if not (c["entry_low"] <= price <= c["entry_high"]):
                    continue
                c["status"] = "active"
                c["activated_ts"] = now
                _calls_append({"ev": "activated", "call_id": cid,
                               "ts": now, "price": price})

            # active: update excursions (R vs zone mid), then resolve
            r = ((price - mid) if long else (mid - price)) / risk

            # Excursion sanity gate. This pass is the same one that can resolve the
            # call, so a legitimate reading can only exceed the resolve boundary by a
            # fraction of one poll. A bigger single-poll move is a bad/stale print:
            # record it as an event, exclude it from MFE/MAE, but still resolve on it
            # (dropping the resolve would leave the call open on a genuine spike).
            _poll_move_pct = None
            if _prev_price and _prev_price > 0:
                _poll_move_pct = abs(price - _prev_price) / _prev_price * 100.0
            _suspect = (_poll_move_pct is not None
                        and _poll_move_pct > _CALLS_POLL_MOVE_SANITY_PCT)
            if _suspect:
                c["excursion_suspect_count"] = int(c.get("excursion_suspect_count", 0)) + 1
                c["excursion_suspect_last"] = {"ts": now, "price": price,
                                               "prev_price": _prev_price,
                                               "move_pct": round(_poll_move_pct, 3)}
                _calls_append({"ev": "excursion_suspect", "call_id": cid, "ts": now,
                               "price": price, "prev_price": _prev_price,
                               "move_pct": round(_poll_move_pct, 3),
                               "implied_r": round(r, 4)})
                log.warning("Call %s: suspect print %.3f%% in one poll (%.6g -> %.6g) — "
                            "excluded from MFE/MAE", cid, _poll_move_pct, _prev_price, price)
            else:
                c["mfe_r"] = max(c.get("mfe_r", 0.0), r)
                c["mae_r"] = max(c.get("mae_r", 0.0), -r)

            sl_hit = price <= c["sl"] if long else price >= c["sl"]
            t1_hit = price >= c["t1"] if long else price <= c["t1"]
            _mode = (c.get("exit_mode") or _CALLS_EXIT_MODE)
            if _mode == "half_at_1r":
                # Half off at +1R, remainder trailed. Default since 2026-09-17
                # (audit M-5): fixed-T1 exits never managed the advertised T2
                # runner (max realised 1.165R vs 6.41x median T2/stop).
                if not c.get("half_taken") and r >= _CALLS_HALF_AT_R:
                    c["half_taken"] = True
                    c["half_r"] = round(_CALLS_HALF_AT_R * _CALLS_HALF_FRACTION, 4)
                    c["half_ts"] = now
                    c["best_r"] = max(r, _CALLS_HALF_AT_R)
                    _calls_append({"ev": "partial", "call_id": cid, "ts": now, "price": price,
                                   "r": _CALLS_HALF_AT_R, "fraction": _CALLS_HALF_FRACTION})
                elif c.get("half_taken"):
                    c["best_r"] = max(c.get("best_r") or 0.0, r)
                _exit = None
                if sl_hit:
                    _exit = (("half_sl", c["half_r"] - _CALLS_HALF_FRACTION)
                             if c.get("half_taken") else ("sl_hit", -1.0))
                elif c.get("half_taken"):
                    _lvl = c["best_r"] - _CALLS_TRAIL_R
                    if r <= _lvl:
                        _exit = ("trail", c["half_r"] + _CALLS_HALF_FRACTION * _lvl)
                if _exit:
                    _calls_resolve_locked(c, now, _exit[0], round(_exit[1], 4), price, notify)
                elif now - c["activated_ts"] > _CALLS_ACTIVE_TTL_H * 3600:
                    _calls_resolve_locked(c, now, "expired_open", round(r, 4), price, notify)
            elif sl_hit:
                _calls_resolve_locked(c, now, "sl_hit", -1.0, price, notify)
            elif t1_hit:
                t1_r = ((c["t1"] - mid) if long else (mid - c["t1"])) / risk
                _calls_resolve_locked(c, now, "t1_hit", round(t1_r, 4),
                                      price, notify)
            elif now - c["activated_ts"] > _CALLS_ACTIVE_TTL_H * 3600:
                _calls_resolve_locked(c, now, "expired_open", round(r, 4),
                                      price, notify)


def _calls_fetch_price_sync(symbol: str):
    """Binance Futures last price (blocking; run in executor)."""
    try:
        url = _CALLS_PRICE_URL.format(symbol=symbol)
        data = _json.loads(urlopen(url, timeout=5).read())
        return float(data["price"])
    except Exception:
        return None


async def _calls_checker():
    """Background loop: resolve open trade calls against Binance last price."""
    _calls_replay()
    log.info("Calls checker started (poll %.0fs)", _CALLS_POLL_SECONDS)
    loop = asyncio.get_running_loop()
    while True:
        try:
            with _CALLS_LOCK:
                symbols = {c["symbol"] for c in _calls.values()}
            if symbols:
                prices = {}
                for sym in symbols:
                    prices[sym] = await loop.run_in_executor(
                        None, _calls_fetch_price_sync, sym)

                def _notify(text: str) -> None:
                    asyncio.ensure_future(send_telegram(text))

                _calls_step(time.time(), prices, _notify)
        except Exception as e:
            log.warning("Calls checker error: %s", e)
        await asyncio.sleep(_CALLS_POLL_SECONDS)


def _calls_stats() -> dict:
    """Aggregate resolved outcomes from the full event log.

    Audit H2: every outcome_r is now reported gross AND net of a round-trip
    cost haircut (CALLS_COST_BPS taker+fees+slippage, in bps of notional),
    converted to R units per call. Audit H4: r == 0 outcomes are counted
    explicitly (zeros) so wins + losses + zeros == n_traded. Audit H1: the
    snapshot-validated subset (phase-2 calls carrying order-flow snapshots)
    is reported separately — legacy calls without snapshots are NOT
    signal-validated and should not be pooled with them.
    """
    calls: dict[str, dict] = {}
    order: list[str] = []
    for ev in _calls_iter_events():
        if ev.get("ev") == "open":
            cid = ev["call"]["call_id"]
            calls[cid] = dict(ev["call"])
            order.append(cid)
        elif ev.get("ev") == "resolved" and ev["call_id"] in calls:
            calls[ev["call_id"]].update({
                "outcome": ev["outcome"], "outcome_r": ev.get("outcome_r"),
                "mfe_r": ev.get("mfe_r", 0.0), "mae_r": ev.get("mae_r", 0.0),
                "resolved_ts": ev["ts"],
            })
        elif ev.get("ev") == "verified" and ev["call_id"] in calls:
            # B7: exchange-truth first-touch marker joins the call record so
            # stats can split on it below.
            calls[ev["call_id"]]["path_coverage"] = ev.get("path_coverage")
    resolved = [calls[cid] for cid in order if calls[cid].get("outcome")]
    traded = [c for c in resolved
              if c["outcome"] in ("t1_hit", "sl_hit", "expired_open")]

    def _cost_r(c: dict) -> float:
        """Round-trip cost in R units for one call: taker fees + funding.

        Audit M-4 action 8 (2026-09-17): funding is systematic in this regime
        (live +10% ann on majors) and the book was always on the paying side
        (10 long / 5 short). Funding accrues over the ACTUAL hold
        (resolved_ts - opened_ts) at the snapshot fundingRate (per-8h): longs
        pay positive funding, shorts receive it. Kill-switch:
        CALLS_FUNDING_INCLUDE=0. Legacy calls without snapshot funding fall
        back to taker-only (funding 0, honest)."""
        mid = _calls_entry_mid(c)
        risk = _calls_risk(c)
        if mid <= 0 or risk <= 0:
            return 0.0
        base = (CALLS_COST_BPS / 10000.0) * mid / risk
        fund = 0.0
        if os.getenv("CALLS_FUNDING_INCLUDE", "1") == "1":
            try:
                fr = float((c.get("snapshot") or {}).get("funding_rate") or 0.0)
                hold_s = (c.get("resolved_ts") or 0) - (c.get("opened_ts") or 0)
                if fr and hold_s > 0:
                    direction = 1.0 if c.get("direction") == "long" else -1.0
                    fund = direction * fr * (hold_s / 28800.0) * mid / risk
            except Exception:  # noqa: BLE001
                fund = 0.0
        return base + fund

    def _subset_metrics(subset: list[dict]) -> dict:
        gross = [(c.get("outcome_r") or 0.0) for c in subset]
        # honest net: raw r minus cost (can go below -1R legitimately)
        net = [(c.get("outcome_r") or 0.0) - _cost_r(c) for c in subset]
        n = len(net)
        if n == 0:
            # EVERY key must exist even for an empty subset. The endpoint built
            # its headline from all_m["wins"] etc., so an empty log raised
            # KeyError: 'wins' and /calls/stats returned 500 — it was broken
            # exactly when there was nothing to report yet (0 calls).
            return {"n": 0, "wins": 0, "losses": 0, "zeros": 0,
                    "winrate": None, "expectancy_r_net": None,
                    "expectancy_r_gross": None, "profit_factor_net": None,
                    "avg_mfe_r": None, "avg_mae_r": None,
                    "cost_bps": CALLS_COST_BPS}
        wins = sum(1 for r in net if r > 0)
        losses = sum(1 for r in net if r < 0)
        zeros = n - wins - losses
        gross_w = sum(r for r in net if r > 0)
        gross_l = abs(sum(r for r in net if r < 0))
        mfes = [c["mfe_r"] for c in subset if c.get("mfe_r") is not None]
        maes = [c["mae_r"] for c in subset if c.get("mae_r") is not None]
        return {
            "n": n,
            "wins": wins,
            "losses": losses,
            "zeros": zeros,
            "winrate": round(wins / n, 4) if n else None,
            "expectancy_r_net": round(sum(net) / n, 4) if n else None,
            "expectancy_r_gross": round(sum(gross) / n, 4) if n else None,
            "profit_factor_net": round(gross_w / gross_l, 3) if gross_l > 0 else None,
            "avg_mfe_r": round(sum(mfes) / len(mfes), 4) if mfes else None,
            "avg_mae_r": round(sum(maes) / len(maes), 4) if maes else None,
            "cost_bps": CALLS_COST_BPS,
        }

    # H1: split the traded set into snapshot-validated vs legacy
    traded_valid = [c for c in traded if c.get("snapshot")]
    traded_legacy = [c for c in traded if not c.get("snapshot")]

    # B7: split the traded set by exchange-truth path verification.
    # verified_clean = kline first-touch agrees with the logged outcome;
    # contradicted = exchange printed the other level first (polling artifact);
    # unverified = no verified event yet (resolutions pre-date the marker).
    def _pc(c: dict) -> dict:
        return c.get("path_coverage") or {}

    traded_verified_clean = [
        c for c in traded
        if _pc(c).get("exchange_verified") and not _pc(c).get("contradicts")]
    traded_contradicted = [
        c for c in traded if _pc(c).get("contradicts")]
    traded_unverified = [
        c for c in traded if not _pc(c).get("exchange_verified")]

    def _bucket(subset: list[dict], key: str) -> dict:
        out: dict[str, dict] = {}
        for c in subset:
            b = out.setdefault(c[key], {"n": 0, "wins": 0, "r_sum_net": 0.0})
            b["n"] += 1
            r = (c.get("outcome_r") or 0.0) - _cost_r(c)
            b["wins"] += 1 if r > 0 else 0
            b["r_sum_net"] += r
        for b in out.values():
            b["winrate"] = round(b["wins"] / b["n"], 4)
            b["avg_r_net"] = round(b["r_sum_net"] / b["n"], 4)
            del b["r_sum_net"]
        return out

    all_m = _subset_metrics(traded)
    valid_m = _subset_metrics(traded_valid)
    legacy_m = _subset_metrics(traded_legacy)
    verified_clean_m = _subset_metrics(traded_verified_clean)
    contradicted_m = _subset_metrics(traded_contradicted)

    # ---- Research-grade extensions (Wilson, bootstrap, edge-decay, splits) ----
    def _wilson(successes: int, n: int, z: float = 1.96) -> list[float]:
        # Prefer quant_engine wilson_interval if available
        try:
            from quant_engine.engine.core.types import wilson_interval as _wi  # type: ignore
            lo, hi = _wi(successes, n, z)
            return [round(lo, 4), round(hi, 4)]
        except Exception:
            pass
        if n == 0:
            return [0.0, 1.0]
        phat = successes / n
        denom = 1 + z ** 2 / n
        centre = phat + z ** 2 / (2 * n)
        adj = z * math.sqrt((phat * (1 - phat) + z ** 2 / (4 * n)) / n)
        lo = max(0.0, (centre - adj) / denom)
        hi = min(1.0, (centre + adj) / denom)
        return [round(lo, 4), round(hi, 4)]

    def _bootstrap_ci(values: list[float], n_resamples: int = 1000, alpha: float = 0.05) -> list[float] | None:
        if len(values) < 5:
            return None
        rng = random.Random(42)
        means: list[float] = []
        n = len(values)
        for _ in range(n_resamples):
            samp = [rng.choice(values) for _ in range(n)]
            means.append(sum(samp) / n)
        means.sort()
        lo_i = int((alpha / 2) * len(means))
        hi_i = int((1 - alpha / 2) * len(means)) - 1
        lo_i = max(0, lo_i)
        hi_i = min(len(means) - 1, hi_i)
        return [round(means[lo_i], 4), round(means[hi_i], 4)]

    # Wilson CI for winrate (overall)
    _n = all_m.get("n") or 0
    _wins = all_m.get("wins") or 0
    wilson_ci = _wilson(_wins, _n) if _n else [0.0, 1.0]

    # Bootstrap CI for expectancy (net)
    net_rs = [(c.get("outcome_r") or 0.0) - _cost_r(c) for c in traded]
    bootstrap_ci = _bootstrap_ci(net_rs, 1000) if net_rs else None

    # Edge-decay rolling 50 (expectancy over sliding window, net)
    edge_decay: list[dict] = []
    if traded:
        # traded is in log order (chronological by open). Use that order.
        window = 50
        for i in range(len(traded)):
            start = max(0, i - window + 1)
            win_vals = [(traded[j].get("outcome_r") or 0.0) - _cost_r(traded[j]) for j in range(start, i + 1)]
            win_n = len(win_vals)
            exp = sum(win_vals) / win_n if win_n else 0.0
            wins_w = sum(1 for v in win_vals if v > 0)
            edge_decay.append({
                "i": i + 1,
                "n": win_n,
                "expectancy_r_net": round(exp, 4),
                "winrate": round(wins_w / win_n, 4) if win_n else None,
            })
        # Keep only last 200 points to bound payload; or last window*4
        if len(edge_decay) > 200:
            edge_decay = edge_decay[-200:]
        # Also compute simple decay metric: last 50 vs prior 50 delta
        if len(traded) >= 50:
            last50 = net_rs[-50:]
            prev50 = net_rs[-100:-50] if len(net_rs) >= 100 else net_rs[:50]
            last_exp = sum(last50) / len(last50)
            prev_exp = sum(prev50) / len(prev50) if prev50 else 0.0
            edge_decay_summary = {
                "last_50_expectancy": round(last_exp, 4),
                "prev_50_expectancy": round(prev_exp, 4),
                "delta": round(last_exp - prev_exp, 4),
                "decaying": (last_exp < prev_exp - 0.05),
            }
        else:
            edge_decay_summary = {
                "last_50_expectancy": round(sum(net_rs) / len(net_rs), 4) if net_rs else None,
                "prev_50_expectancy": None,
                "delta": None,
                "decaying": False,
            }
    else:
        edge_decay_summary = {"last_50_expectancy": None, "prev_50_expectancy": None, "delta": None, "decaying": False}

    # Splits per timeframe / symbol / hour
    def _timeframe_of(c: dict) -> str:
        snap = c.get("snapshot") or {}
        # snapshot may be dict with timeframe key, or nested
        tf = snap.get("timeframe") or snap.get("tf") or c.get("timeframe") or "unknown"
        if not tf or tf == "unknown":
            # try to infer from note
            note = (c.get("note") or "")
            import re as _re
            m = _re.search(r"(\d+\s*m|1h|4h|15m|5m)", note)
            if m:
                tf = m.group(1)
            else:
                tf = "unknown"
        return str(tf)

    def _hour_of(c: dict) -> str:
        ts = c.get("opened_ts") or c.get("resolved_ts") or 0
        try:
            h = time.gmtime(ts).tm_hour if ts else -1
            return f"{h:02d}:00" if h >= 0 else "unknown"
        except Exception:
            return "unknown"

    by_timeframe: dict[str, dict] = {}
    by_hour: dict[str, dict] = {}
    for c in traded:
        tf = _timeframe_of(c)
        hr = _hour_of(c)
        for bucket, key in [(by_timeframe, tf), (by_hour, hr)]:
            b = bucket.setdefault(key, {"n": 0, "wins": 0, "r_sum_net": 0.0, "r_sum_gross": 0.0})
            b["n"] += 1
            rn = (c.get("outcome_r") or 0.0) - _cost_r(c)
            rg = (c.get("outcome_r") or 0.0)
            b["wins"] += 1 if rn > 0 else 0
            b["r_sum_net"] += rn
            b["r_sum_gross"] += rg
    for bucket in (by_timeframe, by_hour):
        for b in bucket.values():
            b["winrate"] = round(b["wins"] / b["n"], 4) if b["n"] else None
            b["expectancy_r_net"] = round(b["r_sum_net"] / b["n"], 4) if b["n"] else None
            b["expectancy_r_gross"] = round(b["r_sum_gross"] / b["n"], 4) if b["n"] else None
            # Wilson per bucket
            b["wilson_ci95"] = _wilson(b["wins"], b["n"])
            # Bootstrap per bucket if enough samples
            vals = []  # re-collect not needed; keep simple: CI already via wilson covers winrate
            del b["r_sum_net"]
            del b["r_sum_gross"]

    return {
        # backwards-compatible headline keys
        "n_traded": all_m["n"],
        "wins": all_m["wins"],
        "losses": all_m["losses"],
        "zeros": all_m["zeros"],
        "winrate": all_m["winrate"],
        "expectancy_r": all_m["expectancy_r_gross"],     # legacy: gross
        "profit_factor": all_m["profit_factor_net"],      # legacy: gross PF approx
        "avg_mfe_r": all_m["avg_mfe_r"],
        "avg_mae_r": all_m["avg_mae_r"],
        "expired_unactivated": sum(
            1 for c in resolved if c["outcome"] == "expired_unactivated"),
        "cancelled": sum(1 for c in resolved if c["outcome"] == "cancelled"),
        "open": len(_calls),
        # audit H2: cost-aware view
        "cost_bps": CALLS_COST_BPS,
        "expectancy_r_net": all_m["expectancy_r_net"],
        "expectancy_r_gross": all_m["expectancy_r_gross"],
        # round() on a None crashed the whole endpoint (TypeError: type NoneType
        # doesn't define __round__) whenever no trade had closed yet — the second
        # landmine from the same never-exercised empty-log path.
        "profit_factor_gross": (lambda gw, gl: round(gw / gl, 3) if gl > 0 else None)(
            sum(max((c.get("outcome_r") or 0.0), 0) for c in traded),
            abs(sum(min((c.get("outcome_r") or 0.0), 0) for c in traded))),
        # audit H1: validated (phase-2, snapshot-attached) vs legacy split
        "validated": {
            **valid_m,
            "by_symbol": _bucket(traded_valid, "symbol"),
            "by_direction": _bucket(traded_valid, "direction"),
            "confidence": ("high" if valid_m["n"] >= 100 else "medium"
                           if valid_m["n"] >= 30 else "low"
                           if valid_m["n"] >= 10 else "insufficient"),
        },
        "legacy": {
            **legacy_m,
            "note": "calls without order-flow snapshots — NOT signal-validated",
        },
        # B7: exchange-truth split. contradicted outcomes keep their as-logged
        # figures everywhere above; this cut shows what the record looks like
        # when polling artifacts are set aside. History is never rewritten.
        "exchange_verified": {
            **verified_clean_m,
            "by_symbol": _bucket(traded_verified_clean, "symbol"),
            "note": "kline first-touch agrees with logged outcome",
        },
        "contradicted": {
            **contradicted_m,
            "call_ids": [c.get("call_id") for c in traded_contradicted],
            "note": "exchange printed the other level first — polling artifact, excluded from verified cut",
        },
        "unverified_path_n": len(traded_unverified),
        "by_symbol": _bucket(traded, "symbol"),
        "by_direction": _bucket(traded, "direction"),
        "confidence": ("high" if all_m["n"] >= 100 else "medium" if all_m["n"] >= 30
                       else "low" if all_m["n"] >= 10 else "insufficient"),
        # ---- Research-grade extensions ----
        "wilson_ci95": wilson_ci,
        "bootstrap_ci95_expectancy_net": bootstrap_ci,
        "bootstrap_resamples": 1000 if bootstrap_ci else 0,
        "edge_decay_rolling_50": edge_decay,
        "edge_decay_summary": edge_decay_summary,
        "by_timeframe": by_timeframe,
        "by_hour_utc": by_hour,
    }


# Auth guard for POST/PUT endpoints (Part 2)
async def _require_auth(request: Request):
    """Check X-Bridge-Token header against BRIDGE_TOKEN. Skip if token not configured."""
    if not BRIDGE_TOKEN:
        return  # no token configured, allow all
    token = request.headers.get("X-Bridge-Token", "")
    if token != BRIDGE_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Bridge-Token")

def _read_live_data() -> dict | None:
    """Read latest LiveExporter indicator data. Supports per-symbol files.

    Resolution order (newest file wins, stale files rejected):
      1. ATAS_BRIDGE_DIR (env) — or the directory of _LIVE_INDICATORS_FILE.
      2. This bridge's own directory.
    The compiled LiveExporter.dll writes per-symbol files
    (_live_indicators_<SYMBOL>.json) into its configured dir (historically
    C:\\atas-bridge); the legacy single _live_indicators.json is frozen and
    must never shadow a fresher per-symbol file nor be served when stale.
    """
    import glob as _glob
    try:
        # LiveExporter's real output directory (env override or the legacy
        # C:\atas-bridge dir). This is where ATAS actually writes.
        live_dir = _LIVEEXPORTER_OUTPUT_DIR
        search_dirs = []
        for d in (live_dir, os.path.dirname(os.path.abspath(__file__))):
            if d and d not in search_dirs and os.path.isdir(d):
                search_dirs.append(d)

        candidates = []
        for d in search_dirs:
            for p in _glob.glob(os.path.join(d, "_live_indicators_*.json")):
                if p not in candidates:
                    candidates.append(p)

        if not candidates:
            return None

        # Newest mtime first, but drop files older than STALE_LIMIT so a frozen
        # legacy file can never shadow live data or be served as a snapshot.
        now = time.time()
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for path in candidates:
            try:
                age = now - os.path.getmtime(path)
                if age > _LIVE_STALE_LIMIT_SECONDS:
                    continue
                content = _shared_read.read_text(path)
                if content.strip():
                    data = _json.loads(content)
                    # Legacy frozen files carry an old internal timestamp too —
                    # double-guard on the payload's timestamp when present.
                    ts = data.get("timestamp")
                    if ts is not None:
                        try:
                            if (now * 1000 - float(ts)) > _LIVE_STALE_LIMIT_SECONDS * 1000:
                                continue
                        except (TypeError, ValueError):
                            pass
                    return data
            except Exception:
                continue
        return None
    except Exception as e:
        log.warning("Could not read live indicators: %s", e)
        return None


def _newest_live_indicator_path() -> str | None:
    """Newest per-symbol LiveExporter file — the one /indicators actually serves.

    The DLL writes `_live_indicators_<SYMBOL>.json` (LiveExporter.cs); the legacy
    single `_live_indicators.json` is frozen and no longer updated. Health/watchdog
    used to stat only the legacy path, so a healthy exporter was reported as
    `stale 9999s` / `degraded` and a dead one looked identical. Falls back to the
    legacy path only when no per-symbol file exists at all.
    """
    import glob as _glob
    best, best_m = None, 0.0
    for d in (_LIVEEXPORTER_OUTPUT_DIR, os.path.dirname(os.path.abspath(__file__))):
        if not d or not os.path.isdir(d):
            continue
        for p in _glob.glob(os.path.join(d, "_live_indicators_*.json")):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > best_m:
                best, best_m = p, m
    if best:
        return best
    return _LIVE_INDICATORS_FILE if os.path.exists(_LIVE_INDICATORS_FILE) else None


def _attach_dom_context(data: dict) -> dict:
    """Merge the bridge-computed DOM context into an ATAS exporter payload.

    Rules 6 and 7 both gate on `imbalance_pct_ema`, not the instantaneous
    `imbalance_pct`: a single top-15 snapshot is dominated by sampling noise, so
    the raw value cannot decide a directional call. The bridge ALREADY computes
    that EMA (and the wall-persistence counters the rules also require) for its
    own /dom feed -- but /indicators returns the exporter payload verbatim, so
    every consumer of the ATAS leg saw `imbalance_pct_ema = None` and both hard
    rules were permanently unevaluable on the one symbol that actually has order
    flow. That made the rules look enforced while being inert.

    This copies the already-computed context onto the payload. No new smoothing
    happens here: the values come from the same dom_state/dom_persist the /dom
    endpoint serves, so /dom and /indicators can never disagree.

    Never overwrites a real field the exporter might start sending itself.
    """
    try:
        ticker = str(data.get("ticker") or "").strip()
        if not ticker:
            return data
        key = ticker.lower()
        dom = data.get("dom")
        if not isinstance(dom, dict):
            dom = {}
            data["dom"] = dom

        # dom_lock is an asyncio.Lock guarding plain dicts. Take the snapshot
        # WITHOUT awaiting it: run_until_complete() would raise inside this
        # already-running loop, and acquiring the lock here is not worth an await
        # on the exporter's hot path. Dict item assignment is atomic under the
        # GIL, so a consistent-enough read is fine for enrichment.
        # NB: `dom_persist` is a LOCAL inside _dom_feeds(), not a module global.
        # Referencing it here raised NameError, which the try/except swallowed,
        # so the enrichment silently did nothing. Everything needed is already
        # projected into dom_state by _dom_feeds().
        snap = dict(dom_state.get(key) or {})
        if not snap:
            return data

        # EMA: the rules' actual gate value.
        ema = snap.get("imbalance_pct_ema")
        if ema is not None and dom.get("imbalance_pct_ema") is None:
            dom["imbalance_pct_ema"] = round(float(ema), 1)

        # Persistence: rules require the reading to hold 3+ polls.
        if dom.get("imbalance_pct_ema_n") is None:
            n = snap.get("imbalance_ema_samples")
            dom["imbalance_pct_ema_n"] = int(n) if n else 0
        for side in ("bid", "ask"):
            for suffix, src_key in (
                ("wall_price", f"{side}_wall_price"),
                ("wall_vol", f"{side}_wall_vol"),
                ("wall_persistent", f"{side}_wall_persistent"),
            ):
                if dom.get(src_key) is None and snap.get(src_key) is not None:
                    dom[src_key] = snap[src_key]

        # Rule 9 context: identical direction on BOTH highly correlated members is
        # ONE bet, not two confirmations. Surfaced so consumers cannot double-count.
        corr = data.get("correlation") if isinstance(data.get("correlation"), dict) else {}
        if not corr:
            corr = {"note": "SUI-SOL correlate 0.811 (15m) / 0.679 (1h) per rules.json "
                            "rule 9 — treat same-direction SUI+SOL as ONE bet"}
            data["correlation"] = corr
    except Exception as e:  # noqa: BLE001 - enrichment must never break the feed
        log.debug("dom context attach skipped: %s", str(e)[:80])
    return data


@app.get("/indicators")
async def live_indicators():
    """Return latest values from ATAS LiveExporter indicator.

    Enriched with the bridge-computed `imbalance_pct_ema` so rules.json rules 6
    and 7 can actually be evaluated on this leg (see _attach_dom_context).
    """
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _read_live_data_sync)
        if data:
            data = _attach_dom_context(data)
            # P2: Append tick history
            ticker = data.get("ticker", "?").upper()
            price = data.get("price", {}).get("last")
            delta = data.get("delta")
            if ticker != "?" and price is not None and delta is not None:
                bar_id = data.get("bar_index")
                bar_delta = data.get("bar_delta")
                cvd_st = _cvd_get(ticker) or {}
                asyncio.ensure_future(_append_tick_history({
                    "ts": time.time(),
                    "ticker": ticker,
                    "price": price,
                    "delta": delta,
                    "bar_id": bar_id,
                    "bar_delta": bar_delta,
                    "cvd": cvd_st.get("cvd"),
                    "cvd_bars": cvd_st.get("bars", 0),
                    "dom_imbalance": data.get("dom", {}).get("imbalance_pct"),
                    "volume": data.get("volume", {}),
                    "tape_buy_pct": data.get("tape", {}).get("buy_count_pct"),
                }))
            return data
        return {"status": "no_data", "message": "LiveExporter indicator not active on chart"}
    except Exception as e:
        log.warning("Could not read live indicators: %s", e)
        return {"status": "error", "message": str(e)}


@app.get("/cvd")
async def cvd_all():
    """CVD state for all symbols — LIVE SOURCES ONLY.

    A symbol is included only while its ATAS exporter file is being written.
    When a chart stops calculating, its symbol is dropped from this response
    rather than served at a frozen cumulative value, which on a chart is
    indistinguishable from a genuinely flat CVD.
    """
    live = _live_indicator_symbols()
    with _CVD_LOCK:
        out = {}
        for sym, st in _cvd_state.items():
            if sym not in live:
                continue
            pub = _cvd_public(sym, st)
            pub["source_age_s"] = round(live[sym], 1)
            out[sym] = pub
        return out


@app.get("/cvd/{symbol}")
async def cvd_symbol(symbol: str):
    """CVD state for one symbol. Accepts SOL or SOLUSDT."""
    sym = symbol.upper()
    with _CVD_LOCK:
        st = _cvd_state.get(sym)
        if st is None and not sym.endswith("USDT"):
            sym = sym + "USDT"
            st = _cvd_state.get(sym)
        if st is None:
            raise HTTPException(status_code=404, detail=f"No CVD data for {symbol}")
        return _cvd_public(sym, st)


@app.get("/taker-cvd")
async def taker_cvd_all():
    """Taker CVD for all symbols (trade-flow based)."""
    with _TAKER_CVD_LOCK:
        return {sym: _taker_cvd_public(sym, st) for sym, st in _taker_cvd_state.items()}


@app.get("/taker-cvd/{symbol}")
async def taker_cvd_symbol(symbol: str):
    """Taker CVD for one symbol. Accepts SOL or SOLUSDT."""
    sym = symbol.upper()
    with _TAKER_CVD_LOCK:
        st = _taker_cvd_state.get(sym)
        if st is None and not sym.endswith("USDT"):
            sym = sym + "USDT"
            st = _taker_cvd_state.get(sym)
        if st is None:
            raise HTTPException(status_code=404, detail=f"No taker CVD data for {symbol}")
        return _taker_cvd_public(sym, st)


@app.get("/derivatives")
async def derivatives_all():
    """Derivatives (OI/funding/LS-ratio) for all symbols."""
    with _DERIVATIVES_LOCK:
        return {sym: _derivatives_public(sym, st) for sym, st in _derivatives_state.items()}


@app.get("/derivatives/{symbol}")
async def derivatives_symbol(symbol: str):
    """Derivatives for one symbol. Accepts SOL or SOLUSDT."""
    sym = symbol.upper()
    with _DERIVATIVES_LOCK:
        st = _derivatives_state.get(sym)
        if st is None and not sym.endswith("USDT"):
            sym = sym + "USDT"
            st = _derivatives_state.get(sym)
        if st is None:
            raise HTTPException(status_code=404, detail=f"No derivatives data for {symbol}")
        return _derivatives_public(sym, st)


@app.get("/liquidations")
async def liquidations_list():
    """Recent liquidations (last 50, newest last). Query ?limit=50."""
    with _LIQUIDATIONS_LOCK:
        return list(_liquidations)


def _read_live_data_sync() -> dict | None:
    """Synchronous read — executor-safe wrapper around _read_live_data."""
    return _read_live_data()


# ---------------------------------------------------------------------------
# Debug: catch-all to log any request ATAS X might send
# ---------------------------------------------------------------------------
@app.middleware("http")
async def log_all_requests(request: Request, call_next):
    log.info(">>> %s %s — headers: %s", request.method, request.url.path, dict(request.headers))
    response = await call_next(request)
    return response


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """Health check — returns bridge status and current tickers in memory.

    Aggregates alone can hide a partial failure: the exporter writes one file
    per symbol, so a frozen chart stays invisible while `status` reads "ok".
    `live_symbols` (fresh, with age) and `frozen_exporter_files` (present but
    stale, with age) are both reported so a dead leg is named, not inferred.
    """
    live = _live_indicator_symbols()
    frozen = {}
    import glob as _glob
    seen = set()
    for d in (_LIVEEXPORTER_OUTPUT_DIR, _BRIDGE_DIR):
        if not d or not os.path.isdir(d):
            continue
        for path in _glob.glob(os.path.join(d, "_live_indicators_*.json")):
            if path in seen:
                continue
            seen.add(path)
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                continue
            base = os.path.basename(path)
            sym = base[len("_live_indicators_"):-len(".json")].upper()
            if sym not in live and age <= 86400:
                frozen[sym] = round(age)

    status = "ok" if live else "degraded"
    return {
        "status": status,
        "tickers_in_buffer": list(ticker_buffers.keys()),
        "buffer_maxlen": BUFFER_MAXLEN,
        "dom_symbols": list(dom_state.keys()),
        "live_symbols": {k: round(v, 1) for k, v in sorted(live.items())},
        "frozen_exporter_files": frozen,
        "live_max_age_s": _LIVE_MAX_AGE_SECONDS,
        # Rules 6/7 condition (a) needs 2+ consecutive CLOSED bars. A segment
        # reset (LiveExporter DLL redeploy, chart reload, timeframe switch) starts
        # a new segment with 0 closed bars, which makes both hard directional
        # rules un-evaluable until it warms up. Report it here so a consumer can
        # tell "rules cannot fire yet" from "market is flat".
        "cvd_warmup": _cvd_warmup_summary(),
        # ATAS carries real L2 for ONE symbol (a hard platform limit). Which
        # symbol is therefore an architecture decision, not an accident -- the
        # chart that happened to be open. Publish the declared target and whether
        # the live chart matches, so "I'm reading the wrong asset" is visible in
        # health instead of inferred from a frozen file.
        "atas_target": _atas_target_status(),
    }


def _atas_target_status() -> dict:
    """Declared ATAS target symbol vs the symbol actually charted.

    Reads atas_target.json written by atas-bridge/atas_target.py. Never raises:
    an unreadable or missing target must not break /health.
    """
    out = {"target": None, "reason": None, "set_at": None,
           "chart": None, "match": None}
    try:
        import json as _j
        tpath = os.path.join(os.path.dirname(__file__), "atas_target.json")
        if os.path.exists(tpath):
            with open(tpath, "r", encoding="utf-8") as f:
                d = _j.load(f)
            out["target"] = d.get("symbol")
            out["reason"] = d.get("reason")
            out["set_at"] = d.get("set_at")
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:80]
    try:
        live = _live_indicator_symbols()
        out["chart"] = sorted(live.keys())[0] if live else None
        out["match"] = (out["target"] == out["chart"]
                        if out["target"] and out["chart"] else None)
    except Exception:
        pass
    return out


def _cvd_warmup_summary() -> dict:
    """Per-symbol CVD warmup state for the rules.json gate check."""
    out = {}
    with _CVD_LOCK:
        for sym, st in _cvd_state.items():
            bars = int(st.get("bars", 0) or 0)
            started = float(st.get("segment_started_ts", 0.0) or 0.0)
            out[sym] = {
                "bars": bars,
                "warmup": bars < _CVD_WARMUP_BARS,
                "segment_age_s": round(time.time() - started, 1) if started else None,
                "segment_reason": st.get("segment_reason", ""),
            }
    return out


@app.get("/dom")
async def dom_all():
    """Return live DOM snapshots for all symbols."""
    async with dom_lock:
        return dict(dom_state)


@app.get("/dom/{symbol}")
async def dom_symbol(symbol: str):
    """Return live DOM snapshot for a specific symbol."""
    sym = symbol.lower().strip()
    async with dom_lock:
        if sym not in dom_state:
            raise HTTPException(status_code=404, detail=f"DOM for '{sym}' not available")
        snap = dict(dom_state[sym])
    # enrich with taker CVD (outside lock)
    try:
        tcvd = _taker_cvd_get(sym.upper())
        if tcvd is None and not sym.upper().endswith("USDT"):
            tcvd = _taker_cvd_get(sym.upper() + "USDT")
        snap["taker_cvd"] = _taker_cvd_public(sym.upper(), tcvd) if tcvd else None
    except Exception:
        snap["taker_cvd"] = None
    try:
        deriv = _derivatives_get(sym.upper())
        if deriv is None and not sym.upper().endswith("USDT"):
            deriv = _derivatives_get(sym.upper() + "USDT")
        snap["derivatives"] = _derivatives_public(sym.upper(), deriv) if deriv else None
    except Exception:
        snap["derivatives"] = None
    return snap


@app.get("/state")
async def state_all():
    """Return current buffer state for all tickers."""
    live = _live_indicator_symbols()
    result = {}
    for ticker, buf in ticker_buffers.items():
        if ticker.upper() not in live:
            continue
        async with ticker_locks[ticker]:
            snapshot = list(buf)
        if not snapshot:
            continue
        # Only alerts inside the live window may be called "current state".
        newest = max((a.get("ts", 0) for a in snapshot), default=0)
        if newest and (time.time() - newest) > _LIVE_MAX_AGE_SECONDS:
            continue
        result[ticker] = {
            "window_size": len(snapshot),
            "alerts": snapshot,
        }
    return result


@app.get("/state/{ticker}")
async def state_ticker(ticker: str):
    """Return current buffer state for a specific ticker."""
    # Case-insensitive lookup; return empty window instead of 404 so
    # polling clients don't treat an idle buffer as an error (mirrors
    # /indicators no_data behaviour). The 404 is reserved for truly
    # malformed routes.
    key = ticker.upper().strip()
    # Find matching buffer key case-insensitively
    matched = None
    for k in list(ticker_buffers.keys()):
        if k.upper() == key:
            matched = k
            break
    if matched is None:
        return {"ticker": key, "window_size": 0, "alerts": [], "note": "buffer empty — no ATAS alerts yet"}
    async with ticker_locks[matched]:
        snapshot = list(ticker_buffers[matched])
    return {
        "ticker": matched,
        "window_size": len(snapshot),
        "alerts": snapshot,
    }


def _parse_atas_message(msg: str) -> dict:
    """Parse a single ATAS message string like:
    '[date] [TICKER]: BigTrade volume between X and Y at price (Direction)'
    Or DOM alerts: '[date] [TICKER]: Best Bid X lots > 5000'
    Returns {ticker, text, price, volume}
    """
    ticker_match = re.search(r"\[([^\]]+)\]:\s", msg)
    ticker = ticker_match.group(1) if ticker_match else "UNKNOWN"

    text = msg.split("]: ", 1)[-1] if "]: " in msg else msg

    # Extract side from both formats: "(Buy)" / "(Sell)" and "Buy at" / "Sell at"
    side = ""
    if "(Buy)" in text or text.startswith("Buy") or ". Buy " in text:
        side = "Ask"
    elif "(Sell)" in text or text.startswith("Sell") or ". Sell " in text:
        side = "Bid"

    # Price: look for "at X" or "at X,XXX" after Buy/Sell
    price_match = re.search(r"(?:at\s+)([\d.,]+)", text)
    if not price_match:
        price_match = re.search(r"(?:Buy|Sell)\s+at\s+([\d.,]+)", text)
    if not price_match:
        price_match = re.search(r"([\d.,]+)\s*\((Buy|Sell)\)", text)

    price_raw = price_match.group(1) if price_match else "?"
    price = _normalize_number(price_raw) if price_raw != "?" else "?"

    # Volume: "volume between X and Y", "volume greater than X", "volume is greater than X"
    vol_match = re.search(r"volume\s+(?:is\s+)?greater\s+than\s+([\d.,]+)", text)
    if vol_match:
        v = _normalize_number(vol_match.group(1))
        volume = f">{v}"
    else:
        vol_match = re.search(r"volume\s+between\s+([\d.,]+)\s+and\s+([\d.,]+)", text)
        if vol_match:
            v1 = _normalize_number(vol_match.group(1))
            v2 = _normalize_number(vol_match.group(2))
            volume = f"{v1}-{v2}"
        else:
            volume = "?"

    # DOM format: "Best Bid/Ask 5642,58 lots > 5000 ... at 0.7953"
    dom_match = re.search(r"Best\s+(Bid|Ask)\s+([\d.,]+)\s+lots\s*>\s*([\d.,]+)", text)
    if dom_match:
        side = dom_match.group(1)  # "Bid" or "Ask"
        actual_lots = _normalize_number(dom_match.group(2))
        threshold = _normalize_number(dom_match.group(3))
        volume = f">{threshold} ({actual_lots} lots)"
        # Keep the ATAS wall price ("at X") as-is so Telegram matches the
        # chart. Only fall back to Binance best bid/ask if ATAS sent no price.
        if price == "?":
            sym = ticker.lower().strip()
            if sym in dom_state:
                ds = dom_state[sym]
                if side == "Bid" and ds.get("best_bid"):
                    price = str(ds["best_bid"])
                elif side == "Ask" and ds.get("best_ask"):
                    price = str(ds["best_ask"])

    return {"ticker": ticker, "text": text, "precio": price, "volumen": volume, "side": side}


def _normalize_number(raw: str) -> str:
    """Normalize a number string handling both European (1.234,56) and US (1234.56) formats.
    Detects format: if the string has both ',' and '.', the last separator is the decimal.
    P2 FIX: More robust detection for edge cases like 1,2345, 12,345, 1,234,567.
    """
    if not raw:
        return raw
    
    # If has both comma and dot
    if "," in raw and "." in raw:
        # Last occurrence determines decimal separator
        last_comma = raw.rfind(",")
        last_dot = raw.rfind(".")
        if last_comma > last_dot:
            # European: 1.234,56 → 1234.56
            return raw.replace(".", "").replace(",", ".")
        else:
            # US: 1,234.56 → 1234.56
            return raw.replace(",", "")
    
    # If only comma → could be European decimal or US thousand separator
    if "," in raw:
        parts = raw.split(",")
        # If last part has 1-3 digits, could be either format
        # Use position: if comma is in thousands position (from right), it's US
        if len(parts) == 2:
            # Single comma: check if it's decimal or thousands
            if len(parts[1]) <= 2:
                # European decimal: 123,45 → 123.45
                return raw.replace(",", ".")
            elif len(parts[1]) == 3 and len(parts[0]) <= 3:
                # US thousands: 1,234 → 1234
                return raw.replace(",", "")
            else:
                # Ambiguous: 12,345 could be 12.345 (EU) or 12345 (US)
                # Default to European for crypto (typical decimal precision)
                return raw.replace(",", ".")
        else:
            # Multiple commas: 1,234,567 → US thousands
            return raw.replace(",", "")
    
    # If only dot → already US format
    return raw


@app.api_route("/alerta-atas", methods=["GET", "POST", "PUT", "OPTIONS"])
@app.api_route("/alerta-atas/", methods=["GET", "POST", "PUT", "OPTIONS"])
async def process_atas_alert(request: Request):
    raw_text = "???"
    try:
        raw_body = await request.body()
        raw_text = raw_body.decode("utf-8", errors="replace")
        method = request.method
        headers = dict(request.headers)

        if method in ("POST", "PUT"):
            await _require_auth(request)

        log.info("=== ATAS WEBHOOK === method=%s body=%s", method, raw_text[:2000])
        with open(os.path.join(_BRIDGE_DIR, "_last_raw_alert.json"), "w") as f:
            f.write(raw_text)

        if method == "POST":
            body = None
            ct = headers.get("content-type", "")
            if "json" in ct:
                try:
                    body = await request.json()
                except Exception:
                    pass
            if body is None:
                import json as _json
                try:
                    body = _json.loads(raw_text)
                except Exception:
                    body = None
            if body is None:
                # form-encoded: "key1=val1&key2=val2"
                from urllib.parse import parse_qs
                try:
                    parsed = parse_qs(raw_text)
                    body = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
                except Exception:
                    body = {"_raw": raw_text}

            log.info("Parsed body: %s", body)
            now = time.time()

            atas_messages = body.get("messages") if isinstance(body, dict) else None
            if atas_messages and isinstance(atas_messages, list):
                # Batch all trades into one Telegram message (deduped)
                tg_lines = []
                stored_count = 0
                deduped = 0
                seen_in_batch: set[str] = set()
                for msg in atas_messages:
                    raw_str = str(msg)
                    # Gate 0: identical raw string twice in one webhook
                    if raw_str in seen_in_batch:
                        deduped += 1
                        log.info("Dedup: in-batch duplicate skipped: %s", raw_str[:80])
                        continue
                    seen_in_batch.add(raw_str)
                    # Gate 1: exact-msg dedup across webhooks (ATAS resends)
                    last_exact = _ATG_LAST_EXACT.get(raw_str, 0)
                    if now - last_exact < ATAS_TG_EXACT_DEDUP_SEC:
                        deduped += 1
                        log.info("Dedup: exact repeat within %.0fs skipped: %s",
                                 ATAS_TG_EXACT_DEDUP_SEC, raw_str[:80])
                        continue
                    parsed = _parse_atas_message(raw_str)
                    text = parsed["text"]
                    # Skip generic "Delta changed!" — no actionable data
                    has_data = (
                        parsed["precio"] != "?"
                        or parsed["volumen"] != "?"
                        or "Buy" in text
                        or "Sell" in text
                        or "Best" in text
                        or "BigTrade" in text
                        or "Aggressive" in text
                    )
                    if not has_data:
                        log.info("Skipping non-actionable ATAS message: %s", text[:80])
                        continue
                    # Gate 2: DOM wall cooldown — same (ticker|side|price)
                    # re-fired while the wall persists. Suppress TG + buffer.
                    if text.startswith("Best"):
                        try:
                            _px = float(parsed["precio"]) if parsed["precio"] != "?" else 0.0
                        except (TypeError, ValueError):
                            _px = 0.0
                        dom_key = f"{parsed['ticker']}|{parsed.get('side', '')}|{round(_px, 4)}"
                        last_dom = _ATG_LAST_DOM.get(dom_key, 0)
                        if now - last_dom < ATAS_TG_DOM_COOLDOWN_SEC:
                            deduped += 1
                            log.info("Dedup: DOM wall %s re-fire within %.0fs suppressed",
                                     dom_key, ATAS_TG_DOM_COOLDOWN_SEC)
                            continue
                        _ATG_LAST_DOM[dom_key] = now
                    _ATG_LAST_EXACT[raw_str] = now
                    ticker = parsed["ticker"]
                    entry = {
                        "volumen": parsed["volumen"],
                        "delta": parsed.get("delta", "?"),
                        "precio": parsed["precio"],
                        "raw": text,
                        "ts": now,
                    }
                    async with ticker_locks[ticker]:
                        buf = ticker_buffers[ticker]
                        buf.append(entry)
                        enrich_buffer_ages(buf, now)
                    stored_count += 1
                    direction = "BUY" if "Buy" in text else ("SELL" if "Sell" in text else "")
                    side_icon = "🟢" if direction == "BUY" else ("🔴" if direction == "SELL" else "⚪")
                    if not direction and parsed["side"]:
                        if text.startswith("Best"):
                            # DOM wall: Bid = support (bullish), Ask = resistance (bearish)
                            side_icon = "🟢" if parsed["side"] == "Bid" else "🔴"
                        else:
                            # BigTrade passive side: Ask = aggressive buy, Bid = aggressive sell
                            side_icon = "🟢" if parsed["side"] == "Ask" else "🔴"
                    _pp = parsed["precio"]; price_str = f"${_pp}" if _pp and _pp != "?" else ""
                    _vv = parsed["volumen"]; vol_str = f"Vol: {_vv}" if _vv and _vv != "?" else ""
                    parts = [f"<b>{ticker}</b>", side_icon, price_str, vol_str]
                    tg_lines.append((" | ".join(p for p in parts if p), text))
                if tg_lines:
                    first = _parse_atas_message(str(atas_messages[0]))
                    ticker = first["ticker"]
                    n_dom = sum(1 for _, t in tg_lines if t.startswith("Best"))
                    n_bt = len(tg_lines) - n_dom
                    _bits = []
                    if n_bt:
                        _bits.append(f"{n_bt} BigTrade")
                    if n_dom:
                        _bits.append(f"{n_dom} DOM")
                    alert_type = " + ".join(_bits) if _bits else "alerts"
                    batch_msg = f"<b>{ticker}</b> — {stored_count} {alert_type}\n" + "\n".join(
                        f"{line}\n└ <i>{orig[:120]}</i>" for line, orig in tg_lines)
                    log.info("TG send (%d lines): %s", len(tg_lines), batch_msg[:500])
                    asyncio.ensure_future(send_telegram(batch_msg))
                else:
                    log.info("No actionable alerts to send (%d received, %d deduped)",
                             len(atas_messages), deduped)
            elif isinstance(body, dict):
                alert = AtasAlert(**body)
                ticker = alert.ticker
                # Skip if no actionable data (all defaults/empty)
                has_data = (
                    alert.precio not in ("0", "", "?")
                    or alert.volumen not in ("0", "", "?")
                )
                if not has_data:
                    log.info("Skipping empty AtasAlert for %s", ticker)
                else:
                    entry = {
                        "volumen": alert.volumen,
                        "delta": alert.delta,
                        "precio": alert.precio,
                        "raw": f"Price: {alert.precio}, Vol: {alert.volumen}",
                        "ts": now,
                    }
                    async with ticker_locks[ticker]:
                        buf = ticker_buffers[ticker]
                        buf.append(entry)
                        enrich_buffer_ages(buf, now)
                    price_str = f"${alert.precio}" if alert.precio != "0" else ""
                    vol_str = f"Vol: {alert.volumen}" if alert.volumen != "0" else ""
                    parts = [f"<b>{ticker}</b> ⚪", price_str, vol_str]
                    tg_line = " | ".join(p for p in parts if p)
                    asyncio.ensure_future(send_telegram(tg_line))
            else:
                ticker = "UNKNOWN"

        log.info("=== RESPONSE ===")
        return Response(content="OK", status_code=200, media_type="text/plain")

    except Exception as e:
        log.exception("Unhandled error processing alert")
        return Response(content="ERROR: " + str(e), status_code=200, media_type="text/plain")


# ---------------------------------------------------------------------------
# Analysis endpoint — Smart Tape Reading methodology
# ---------------------------------------------------------------------------
def _classify_alert(a: dict) -> dict:
    """Classify an ATAS alert as BigTrade or DOM, extract tape reading."""
    raw = a.get("raw", "")
    precio = a.get("precio", "?")
    volumen = a.get("volumen", "?")

    # DOM alert: "Best Bid/Ask X lots > Y"
    dom_match = re.match(r"Best\s+(Bid|Ask)\s+", raw)
    if dom_match:
        side = dom_match.group(1)
        return {
            "type": "DOM",
            "side": side,
            "aggressor": "",
            "label": f"Muralla {'compradora' if side == 'Bid' else 'vendedora'} {volumen}",
            "icon": "🟢" if side == "Bid" else "🔴",
            "price": precio,
            "volumen": volumen,
        }

    # BigTrade: "Ask/Bid BigTrade volume ... (Buy/Sell)" or "Trade volume ... Buy/Sell at"
    is_bid = "Bid" in raw and "BigTrade" in raw
    is_ask = "Ask" in raw and "BigTrade" in raw
    
    # P2 FIX: Make side detection mutually exclusive — check for explicit patterns first
    is_buy = False
    is_sell = False
    if "(Buy)" in raw:
        is_buy = True
    elif "(Sell)" in raw:
        is_sell = True
    elif ". Buy " in raw:
        is_buy = True
    elif ". Sell " in raw:
        is_sell = True
    elif raw.startswith("Buy"):
        is_buy = True
    elif raw.startswith("Sell"):
        is_sell = True

    # Determine passive side (where the trade happened)
    passive = ""
    if is_ask:
        passive = "Ask"
    elif is_bid:
        passive = "Bid"
    elif "(Buy)" in raw or "(Sell)" in raw:
        # Format: "BigTrade volume > X at price (Buy/Sell)"
        passive = "Ask" if "(Buy)" in raw else "Bid"

    # Aggressor is the opposite of passive
    aggressor = ""
    if passive == "Ask":
        aggressor = "comprador"  # took the Ask = aggressive buy
    elif passive == "Bid":
        aggressor = "vendedor"  # took the Bid = aggressive sell

    # Fallback: use "(Buy)/(Sell)"
    if not aggressor:
        if is_buy:
            aggressor = "comprador"
            passive = "Ask"
        elif is_sell:
            aggressor = "vendedor"
            passive = "Bid"

    icon = "🟢" if aggressor == "comprador" else "🔴"
    label = f"Agg {aggressor} @ {precio} | {volumen}"

    return {
        "type": "BigTrade",
        "side": passive,
        "aggressor": aggressor,
        "label": label,
        "icon": icon,
        "price": precio,
        "volumen": volumen,
    }


def _build_fast_analysis(ticker: str) -> str:
    """Build a full analysis from LiveExporter data using deterministic signal engine."""
    live = _read_live_data()
    if not live or live.get("ticker", "").upper() != ticker.upper():
        return f"<b>{ticker}</b>\nNo live data available."

    p = live.get("price", {})
    last_price = p.get("last", 0)
    delta = live.get("delta", 0)
    vol = live.get("volume", {})
    total_vol = vol.get("total", 0)
    fp = live.get("footprint", {})
    dom = live.get("dom", {})
    tape = live.get("tape", {})

    # Footprint
    levels = fp.get("top_levels", [])
    buy_levels = sum(1 for lv in levels if lv.get("delta", 0) > 0)
    sell_levels = sum(1 for lv in levels if lv.get("delta", 0) < 0)

    # Tape
    net_vol = tape.get("net_vol", 0)
    buy_pct = tape.get("buy_count_pct", 50)
    largest = tape.get("largest_trade", 0)
    largest_side = tape.get("largest_trade_side", "?")

    # DOM
    imb = dom.get("imbalance_pct", 0)
    bid_cum = dom.get("cumulative_bid_vol", 0)
    ask_cum = dom.get("cumulative_ask_vol", 0)

    # Deterministic signal engine
    from signal_engine import build_snapshot_from_live, compute_signal
    cvd_st = _cvd_get(ticker)
    snap = build_snapshot_from_live(live, cvd_st)
    sig = compute_signal(snap) if snap else None

    if sig:
        bias_icon = "🟢" if sig.bias_label == "bullish" else ("🔴" if sig.bias_label == "bearish" else "🟡")
        score = sig.bias_score
        bias_str = f"{bias_icon} {sig.bias_label.upper()}"
        extra = []
        if sig.rule_2_exhaustion:
            extra.append("⚠️ Rule 2: CVD exhaustion — no continuation call")
        if sig.rule_6_active:
            extra.append("📉 Rule 6: Distribution active")
        if sig.rule_7_active:
            extra.append("📈 Rule 7: Accumulation active")
        rule_lines = "\n".join(f"<i>{r}</i>" for r in extra) if extra else ""
    else:
        score = 50
        bias_str = "🟡 NEUTRAL"
        rule_lines = ""

    # CVD display
    if cvd_st and cvd_st.get("bars", 0) > 0:
        cvd_val = cvd_st["cvd"]
        cvd_icon = "🟢" if cvd_val >= 0 else "🔴"
        deltas = cvd_st.get("bar_deltas", [])
        if len(deltas) >= 2 and deltas[-1] > 0 and deltas[-2] > 0:
            cvd_trend = "🟢 rising 2+ bars"
        elif len(deltas) >= 2 and deltas[-1] < 0 and deltas[-2] < 0:
            cvd_trend = "🔴 falling 2+ bars"
        else:
            cvd_trend = "🟡 mixed"
        cvd_line = (f"{cvd_icon} {cvd_val:+,.0f} ({cvd_st['bars']} bars) | "
                    f"Last: {cvd_st['last_delta']:+,.0f} | {cvd_trend}")
    else:
        cvd_line = "⚪ no closed bars yet"

    fp_icon = "🟢" if buy_levels > sell_levels else ("🔴" if sell_levels > buy_levels else "⚪")
    lines = [
        f"<b>📊 {ticker} — QUANT ANALYSIS</b>",
        f"💵 <b>${last_price}</b> | Delta: {'🟢' if delta >=0 else '🔴'}{delta:+.1f} | Vol: {total_vol:,.0f}",
        f"",
        f"<b>🏛 FOOTPRINT</b>",
        f"POC: ${fp.get('poc', 0):.2f} | VA {fp.get('value_area_low',0):.2f}-{fp.get('value_area_high',0):.2f}",
        f"Levels: {len(levels)} ({buy_levels}🟢/{sell_levels}🔴) | Direction: {fp_icon}",
        f"",
        f"<b>📊 DOM</b>",
        f"Imbalance: {'+' if imb >= 0 else ''}{imb:.1f}% | Bids: {bid_cum:,.0f} / Asks: {ask_cum:,.0f}",
        f"",
        f"<b>📼 TAPE</b>",
        f"Buy {buy_pct:.0f}% / Sell {100-buy_pct:.0f}% | Net: {net_vol:+.1f} | Lg: {largest:.0f} {largest_side.upper()}",
        f"",
        f"<b>📈 CVD</b>",
        cvd_line,
        f"",
        f"<b>⚖️ SIGNAL: {score:.0f}/100 — {bias_str}</b>",
    ]
    if rule_lines:
        lines.append("")
        lines.append(rule_lines)
    return "\n".join(lines)


def _build_analysis(ticker: str, alerts: list[dict]) -> str:
    """Generate a structured analysis incorporating ATAS alerts + LiveExporter data."""
    n = len(alerts)
    if n < 2:
        return _build_fast_analysis(ticker)

    prices = [float(a["precio"]) for a in alerts if a.get("precio", "?").replace(".", "", 1).isdigit()]
    if not prices:
        return f"<b>{ticker}</b>\nNo valid price data."

    low = min(prices)
    high = max(prices)
    rng = high - low

    classified = [_classify_alert(a) for a in alerts]
    bigtrades = [c for c in classified if c["type"] == "BigTrade"]
    dom_alerts = [c for c in classified if c["type"] == "DOM"]

    bt_n = len(bigtrades)
    dom_n = len(dom_alerts)

    agg_buys = sum(1 for c in bigtrades if c["aggressor"] == "comprador")
    agg_sells = sum(1 for c in bigtrades if c["aggressor"] == "vendedor")
    bt_total = agg_buys + agg_sells
    aggressive_delta = agg_buys - agg_sells

    if bt_total > 0:
        agg_buy_pct = agg_buys / bt_total * 100
        if agg_buy_pct >= 70:
            bias = "🟢🟢 Acumulación — compradores agresivos dominan"
        elif agg_buy_pct >= 55:
            bias = "🟢 Presión compradora — buyers ligeramente activos"
        elif agg_buy_pct <= 30:
            bias = "🔴🔴 Distribución — vendedores agresivos dominan"
        elif agg_buy_pct <= 45:
            bias = "🔴 Presión vendedora — sellers ligeramente activos"
        else:
            bias = "⚪ Equilibrio — buyers y sellers balanceados"
    else:
        bias = "⚪ Sin BigTrades — solo actividad DOM"

    bid_alerts = sum(1 for c in dom_alerts if c["side"] == "Bid")
    ask_alerts = sum(1 for c in dom_alerts if c["side"] == "Ask")

    if bid_alerts > ask_alerts * 1.5:
        dom_reading = "🟢 Bid stacking — muralla compradora dominante"
    elif ask_alerts > bid_alerts * 1.5:
        dom_reading = "🔴 Ask stacking — muralla vendedora dominante"
    elif bid_alerts > 0 and ask_alerts > 0:
        dom_reading = "⚪ Murallas equilibradas — lucha Bid vs Ask"
    else:
        dom_reading = ""

    # Absorption detection — P3 FIX: normalize to percentage of price
    absorption = ""
    if bt_n >= 2:
        recent_price = float(alerts[-1].get("precio", 0) or 0)
        old_price = float(alerts[0].get("precio", 0) or 0)
        # Use 1% of price as threshold instead of absolute 0.02
        price_threshold = old_price * 0.01 if old_price > 0 else 0.01
        if abs(recent_price - old_price) < price_threshold and bt_n >= 3:
            if aggressive_delta > 0:
                absorption = "⚠️ Absorción: compras agresivas pero precio no sube — vendedores ilimitados (icebergs) absorbiendo demanda"
            elif aggressive_delta < 0:
                absorption = "⚠️ Absorción: ventas agresivas pero precio no baja — compradores ilimitados (icebergs) absorbiendo oferta"
            else:
                absorption = "⚠️ Absorción: actividad bilateral sin movimiento — mercado atascado"

    last_c = classified[-1]
    last_line = f"{last_c['icon']} {last_c['label']}"

    # ---- LiveExporter data ----
    live = _read_live_data()
    live_delta = None
    live_total_vol = None
    live_ask_vol = None
    live_bid_vol = None
    live_current_price = None
    if live and live.get("ticker", "").upper() == ticker.upper():
        live_delta = live["delta"]
        live_total_vol = live["volume"]["total"]
        live_ask_vol = live["volume"]["ask"]
        live_bid_vol = live["volume"]["bid"]
        live_current_price = live["price"]["last"]

    # ---- Build output ----
    lines = [f"<b>📊 {ticker} — Análisis Integrado</b>"]

    parts = []
    if bt_n > 0:
        parts.append(f"{bt_n} BigTrades ({agg_buys} Agg Buy / {agg_sells} Agg Sell)")
    if dom_n > 0:
        parts.append(f"{dom_n} DOM alerts ({bid_alerts} Bid / {ask_alerts} Ask)")
    # Live price always takes precedence over buffer range (fixes stale plan)
    live_price_for_header = None
    live_ts_for_header = None
    try:
        _live = _read_live_data()
        if _live and _live.get("price", {}).get("last"):
            live_price_for_header = float(_live["price"]["last"])
            live_ts_for_header = _live.get("timestamp")
    except Exception:
        pass
    if live_price_for_header:
        lines.append(f"📌 {' | '.join(parts)}")
        lines.append(f"💰 Precio LIVE: ${live_price_for_header:.2f} | Rango buffer: ${low:.2f} – ${high:.2f} (rango ${rng:.2f})")
        if abs(live_price_for_header - high) > high * 0.05 or abs(live_price_for_header - low) > low * 0.05:
            lines.append(f"⚠️ Buffer desactualizado — plan anclado a LIVE ${live_price_for_header:.2f}, no a buffer")
    else:
        lines.append(f"📌 {' | '.join(parts)}")
        lines.append(f"💰 Precio: ${low} – ${high} (rango ${rng:.2f})")

    try:
        sym = ticker.lower()
        if sym in dom_state and dom_state[sym].get("best_bid"):
            d = dom_state[sym]
            bid = d["best_bid"]
            ask = d["best_ask"]
            spr = ask - bid
            imb = d.get("imbalance_pct", 0)
            lines.append(f"💧 Spread ${spr:.2f} | Imbalance DOM {imb:+.1f}%")
    except Exception:
        pass

    lines.append("")

    if bigtrades:
        lines.append(f"<b>📋 BigTrades — Flujo Agresivo</b>")
        for c in bigtrades:
            lines.append(f"  {c['icon']} Agg {c['aggressor']} @ ${c['price']} | {c['volumen']}")

    if dom_alerts:
        lines.append(f"<b>📋 DOM — Murallas</b>")
        for c in dom_alerts:
            lines.append(f"  {c['icon']} {c['label']}")

    lines.append("")

    # ── LiveExporter section ──
    if live_delta is not None and live_total_vol is not None:
        bid_pct = round(live_bid_vol / live_total_vol * 100, 1) if live_total_vol > 0 else 0
        ask_pct = round(live_ask_vol / live_total_vol * 100, 1) if live_total_vol > 0 else 0
        delta_icon = "🟢" if live_delta >= 0 else "🔴"
        lines.append(f"<b>🔶 ATAS DELTA EN VIVO</b>")
        lines.append(f"  Delta: {delta_icon} {live_delta:+.1f}")
        lines.append(f"  Volumen: {live_total_vol:,.1f} SOL (Bid {bid_pct:.0f}% / Ask {ask_pct:.0f}%)")

        # Tape Pressure (audit M1: icons were inverted vs aggressor semantics)
        # LiveExporter bid volume = trades at the bid = SELL aggressor; ask
        # volume = trades at the ask = BUY aggressor. High bid share = selling
        # pressure (bearish, red); high ask share = buying pressure (bullish, green).
        if bid_pct > 60:
            tape_pressure = f"🔴 {bid_pct:.0f}% Bid — oferta dominando demanda"
        elif ask_pct > 60:
            tape_pressure = f"🟢 {ask_pct:.0f}% Ask — demanda dominando oferta"
        elif bid_pct > 55:
            tape_pressure = f"🟡 {bid_pct:.0f}% Bid — leve presión vendedora"
        elif ask_pct > 55:
            tape_pressure = f"🟡 {ask_pct:.0f}% Ask — leve presión compradora"
        else:
            tape_pressure = f"⚪ Bid {bid_pct:.0f}% / Ask {ask_pct:.0f}% — balanceado"
        lines.append(f"  Tape Pressure: {tape_pressure}")

        # Delta per bar (if Open available)
        if live.get("price", {}).get("open") and live_current_price:
            o = live["price"]["open"]
            c = live_current_price
            bar_dir = "🟢" if c >= o else "🔴"
            lines.append(f"  Barra actual: {bar_dir} Open ${o:.2f} → Close ${c:.2f} | Delta {delta_icon} {live_delta:+.1f}")
            # Delta confirming or rejecting bar
            if (c >= o and live_delta > 0) or (c < o and live_delta < 0):
                lines.append(f"  ✅ Delta CONFIRMA dirección del precio")
            else:
                lines.append(f"  ⚠️ Delta RECHAZA dirección — divergencia intra-bar")
    else:
        lines.append(f"<b>🔶 ATAS DELTA EN VIVO</b>")
        lines.append(f"  ⚪ LiveExporter no disponible — agregar indicador al chart")

    lines.append("")

    # ── Absorption Score mejorado con LiveExporter ──
    enhanced_absorption = ""
    if absorption:
        enhanced_absorption = absorption
        if live_delta is not None and live_total_vol is not None:
            # Cross-check with real ATAS delta
            if aggressive_delta > 0 and live_delta < 0:
                enhanced_absorption += "\n  🔴 Confirmado por Delta ATAS: la barra cierra con delta negativo — distribución real"
            elif aggressive_delta < 0 and live_delta > 0:
                enhanced_absorption += "\n  🟢 Confirmado por Delta ATAS: la barra cierra con delta positivo — acumulación real"
            elif abs(live_delta) < abs(live_total_vol) * 0.05:
                enhanced_absorption += "\n  ⚪ Delta ATAS casi neutro — absorción pura confirmada"
    elif live_delta is not None and live_total_vol is not None and bt_n >= 1:
        # Check using LiveExporter delta alone
        if abs(live_delta) < live_total_vol * 0.03 and live_total_vol > 100:
            direction = "compras" if aggressive_delta > 0 else "ventas"
            enhanced_absorption = f"⚠️ Absorción: {direction} agresivas pero delta ATAS casi neutro ({live_delta:+.1f}) — icebergs absorbiendo"

    lines.append(f"<b>🧠 Lectura</b>")
    lines.append(f"• {bias}")
    if dom_reading:
        lines.append(f"• {dom_reading}")
    if enhanced_absorption:
        lines.append(f"• {enhanced_absorption}")

    lines.append("")
    lines.append(f"<b>⏺ Último:</b> {last_line}")

    if aggressive_delta > 2:
        lines.append(f"📈 Delta agresivo: +{aggressive_delta} 🟢 — compradores controlando el tape")
    elif aggressive_delta < -2:
        lines.append(f"📉 Delta agresivo: {aggressive_delta} 🔴 — vendedores controlando el tape")
    else:
        lines.append(f"📊 Delta agresivo: {aggressive_delta:+d} ⚪ — tape equilibrado")

    return "\n".join(lines)


@app.post("/analizar/{ticker}")
async def analyze_ticker(ticker: str, request: Request):
    """Generate analysis for a ticker and send to Telegram."""
    await _require_auth(request)
    ticker = ticker.upper().strip("/")
    # Allow analysis even with empty buffer — _build_analysis falls back to _build_fast_analysis (live-anchored)
    async with ticker_locks[ticker]:
        snapshot = list(ticker_buffers[ticker]) if ticker in ticker_buffers else []
    analysis = _build_analysis(ticker, snapshot)
    asyncio.ensure_future(send_telegram(analysis))
    return {"status": "sent", "ticker": ticker, "alerts_analyzed": len(snapshot)}


@app.post("/analizar")
async def analyze_all(request: Request):
    """Generate analysis for all tickers and send each to Telegram."""
    await _require_auth(request)
    sent = {}
    for ticker, buf in ticker_buffers.items():
        async with ticker_locks[ticker]:
            snapshot = list(buf)
        analysis = _build_analysis(ticker, snapshot)
        asyncio.ensure_future(send_telegram(analysis))
        sent[ticker] = len(snapshot)
    return {"status": "sent", "tickers": sent}


@app.post("/reset/{ticker}")
async def reset_ticker(ticker: str, request: Request):
    """Clear alert buffer for a specific ticker."""
    await _require_auth(request)
    ticker = ticker.upper().strip("/")
    if ticker in ticker_buffers:
        async with ticker_locks[ticker]:
            ticker_buffers[ticker].clear()
        return {"status": "ok", "ticker": ticker, "message": f"Buffer cleared for {ticker}"}
    return {"status": "ok", "ticker": ticker, "message": f"No buffer for {ticker}"}


@app.post("/reset")
async def reset_all(request: Request):
    """Reset all buffers for new session."""
    await _require_auth(request)
    for t, lock in ticker_locks.items():
        async with lock:
            ticker_buffers[t].clear()
    return {"status": "ok", "message": "All buffers cleared"}


# ---------------------------------------------------------------------------
# Quant Alert Engine buffer
# ---------------------------------------------------------------------------
quant_buffers: dict[str, deque] = defaultdict(
    lambda: deque(maxlen=BUFFER_MAXLEN)
)
quant_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Active filter: only store/send alerts matching this symbol + timeframe
quant_filter: dict = {"symbol": "", "timeframe": ""}


@app.post("/quant/filter")
async def quant_set_filter(request: Request):
    """Set active filter for quant alerts."""
    await _require_auth(request)
    try:
        raw = await request.body()
        body = _json.loads(raw) if raw else {}
        quant_filter["symbol"] = body.get("symbol", "").upper()
        quant_filter["timeframe"] = body.get("timeframe", "")
        log.info("QUANT filter set: symbol='%s' timeframe='%s'", quant_filter["symbol"], quant_filter["timeframe"])
        return {"status": "ok", "filter": dict(quant_filter)}
    except Exception as e:
        log.warning("Quant filter error: %s", e)
        return {"status": "error", "message": str(e)}


@app.get("/quant/filter")
async def quant_get_filter():
    """Return current quant alert filter."""
    return dict(quant_filter)


@app.post("/quant-alert")
async def quant_alert_endpoint(request: Request):
    """Receive alerts from Quant Alert Engine and store in buffer."""
    await _require_auth(request)
    try:
        raw = await request.body()
        body = _json.loads(raw) if raw else {}
        symbol = body.get("symbol", "UNKNOWN").split("/")[0].split(":")[0]
        signal_type = body.get("signal_type", "SIGNAL")
        signal_name = body.get("signal_name", "")
        price = body.get("price", 0)
        timeframe = body.get("timeframe", "")
        color = body.get("color", "info")
        now = time.time()

        # Check filter
        sf = quant_filter.get("symbol", "")
        tf = quant_filter.get("timeframe", "")
        if sf and symbol.upper() != sf:
            return Response(content="FILTERED", status_code=200)
        if tf and timeframe != tf:
            return Response(content="FILTERED", status_code=200)

        entry = {
            "source": "quant_engine",
            "signal_type": signal_type,
            "signal_name": signal_name,
            "precio": str(price) if price else "?",
            "timeframe": timeframe,
            "color": color,
            "raw": f"[{signal_type}] {signal_name} @ {price}",
            "ts": now,
        }

        async with quant_locks[symbol]:
            quant_buffers[symbol].append(entry)
            enrich_buffer_ages(quant_buffers[symbol], now)

        log.info("QUANT alert: %s %s %s @ %.2f", symbol, signal_type, signal_name, price)

        return Response(content="OK", status_code=200)

    except Exception as e:
        log.exception("Quant alert error")
        return Response(content="ERROR: " + str(e), status_code=200)


@app.get("/quant/{symbol}")
async def quant_state(symbol: str):
    """Return quant alerts for a symbol."""
    sym = symbol.upper().strip()
    # Return empty window instead of 404 for idle symbols — consistent
    # with /state/{ticker} fix. Clients polling empty buffers get 200
    # with window_size 0, not an error.
    if sym not in quant_buffers:
        # Also check stripped suffix (SOL vs SOLUSDT alias)
        base = sym[:-4] if sym.endswith("USDT") else sym
        alt = base if base in quant_buffers else (base + "USDT" if (base + "USDT") in quant_buffers else None)
        if alt is None:
            return {"symbol": sym, "window_size": 0, "alerts": [], "note": "buffer empty — no quant alerts yet"}
        sym = alt
    async with quant_locks[sym]:
        snapshot = list(quant_buffers[sym])
    return {"symbol": sym, "window_size": len(snapshot), "alerts": snapshot}


@app.get("/quant")
async def quant_state_all():
    """Return all quant alerts."""
    live = _live_indicator_symbols()
    result = {}
    now = time.time()
    for sym, buf in quant_buffers.items():
        if sym.upper() not in live:
            continue
        async with quant_locks[sym]:
            alerts = [a for a in buf
                      if now - (a.get("ts", 0) or 0) <= _LIVE_MAX_AGE_SECONDS]
        if not alerts:
            continue
        result[sym] = {"window_size": len(alerts), "alerts": alerts}
    return result


# ---------------------------------------------------------------------------
# Trade call endpoints
# ---------------------------------------------------------------------------
def _telegram_send_sync(text: str) -> bool:
    """Blocking Telegram send for background threads (verification, etc.)."""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "none":
        return False
    try:
        payload = f"chat_id={TELEGRAM_CHAT_ID}&text={quote(text)}&parse_mode=HTML"
        req = URLRequest(TELEGRAM_API, data=payload.encode(), method="POST")
        with urlopen(req, timeout=8):
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram sync send failed: %s", e)
        return False


def _plan_alert_text(c: dict) -> str:
    """Opening alert for a logged call: what, why, the geometry it will be judged by,
    and where the order-flow numbers came from (audit trail — a plan without provenance
    cannot be reviewed later)."""
    import re as _re
    g = c.get("geometry") or {}
    s = c.get("snapshot") or {}
    long_ = c.get("direction") == "long"
    emo = "🟢" if long_ else "🔴"
    note = c.get("note") or ""
    m = _re.search(r"score\s*([+-]?\d+(?:\.\d+)?)", note)
    score = float(m.group(1)) if m else None
    prov = s.get("source") or ("binance_only" if s else "no snapshot")
    lines = [f"{emo} <b>NEW {c['symbol']} {c.get('direction','').upper()}</b> @ "
             f"{g.get('entry_mid', (c['entry_low'] + c['entry_high']) / 2):.6g}"]
    if score is not None:
        lines.append(f"trigger {score:+.1f} (|score| ≥ 20 fires) · exit mode {c.get('exit_mode', '?')}")
    lines.append(f"entry {c['entry_low']:.6g}–{c['entry_high']:.6g} | SL {c['sl']:.6g} | "
                 f"T1 {c['t1']:.6g}" + (f" | T2 {c['t2']:.6g}" if c.get("t2") else ""))
    if g:
        lines.append(f"stop {g.get('stop_pct')}% · T1/stop {g.get('t1_over_sl')} · "
                     f"cost {g.get('cost_r')}R → breakeven {g.get('breakeven_winrate')}")
    if g.get("warnings"):
        lines.append("⚠️ " + "; ".join(g["warnings"]))
    f_lines = []
    if s.get("dom_imbalance_pct") is not None:
        f_lines.append(f"DOM {s['dom_imbalance_pct']:+.1f}%")
    if s.get("cvd_usd_delta_win") is not None:
        f_lines.append(f"flow {s['cvd_usd_delta_win']:+,.0f}$/{s.get('hist_seconds', 0):.0f}s")
    if s.get("oi_delta_win") is not None:
        f_lines.append(f"OIΔ {s['oi_delta_win']:+,.0f}")
    if s.get("funding_rate") is not None:
        f_lines.append(f"fund {s['funding_rate'] * 3 * 365 * 100:+.1f}%/yr")
    if s.get("ls_ratio") is not None:
        f_lines.append(f"L/S {s['ls_ratio']:.2f}")
    if s.get("va_location"):
        f_lines.append(f"{s['va_location']}")
    if s.get("atr_percentile_rank") is not None:
        f_lines.append(f"ATR p{float(s['atr_percentile_rank']):.0f}")
    if f_lines:
        lines.append(" · ".join(f_lines))
    lines.append(f"source: {prov}" + (f" · atas {s.get('atas_status')}" if s.get("atas_status") else ""))
    return "\n".join(lines)


@app.post("/calls")
async def calls_open(call: TradeCall, request: Request):
    """Log a new trade call (entry zone, SL, T1) for outcome tracking."""
    await _require_auth(request)
    sym = call.symbol.upper().strip("/")
    direction = call.direction.lower().strip()
    if direction not in ("long", "short"):
        raise HTTPException(status_code=400,
                            detail="direction must be 'long' or 'short'")
    if not call.entry_low < call.entry_high:
        raise HTTPException(status_code=400,
                            detail="entry_low must be < entry_high")
    if direction == "long":
        if not (call.sl < call.entry_low and call.t1 > call.entry_high):
            raise HTTPException(
                status_code=400,
                detail="long: sl must be below entry zone, t1 above it")
    else:
        if not (call.sl > call.entry_high and call.t1 < call.entry_low):
            raise HTTPException(
                status_code=400,
                detail="short: sl must be above entry zone, t1 below it")
    # Symbol/price coherence (2026-09-17: SOLUSDT-20260917-7a5f52 was logged
    # with SUI's $0.73 price — wrong-symbol contamination from a mixed feed).
    # Rejects entries >MAX_ENTRY_DEVIATION_PCT from the bridge's live price.
    # Fail-open when no live price is available for the symbol.
    try:
        _ds = dom_state.get(sym) or dom_state.get(sym.lower()) or {}
        _live_px = _ds.get("best_bid") or 0
        _max_dev = float(os.getenv("CALLS_MAX_ENTRY_DEVIATION_PCT", "10.0"))
        _emid = (call.entry_low + call.entry_high) / 2.0
        if _live_px > 0 and _emid > 0:
            _dev = abs(_emid - _live_px) / _live_px * 100.0
            if _dev > _max_dev:
                raise HTTPException(
                    status_code=400,
                    detail=f"entry_mid {_emid} deviates {_dev:.1f}% from live "
                           f"{sym} price {_live_px} (max {_max_dev}%) — "
                           f"wrong-symbol feed?")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        pass
    now = time.time()
    cid = f"{sym}-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"

    # Geometry guard. cost_r = CALLS_COST_BPS applied over the entry-to-SL distance, so a
    # tight stop is not "lower risk" — it is a larger share of the trade paid to the
    # exchange — and a T1 closer than the stop cannot pay for that cost at any win rate.
    # Measured on the 15-call track record: mean stop 0.538% -> mean cost_r 0.21R,
    # T1/stop 1.02 -> breakeven win rate 59.9% against a realised 53.3%. Recorded on the
    # call (and warned about) rather than rejected, so the generator's output stays
    # comparable over time instead of silently changing shape.
    entry_mid = (call.entry_low + call.entry_high) / 2.0
    risk_abs = abs(entry_mid - call.sl)
    t1_abs = abs(call.t1 - entry_mid)
    geom: dict = {}
    if entry_mid > 0 and risk_abs > 0:
        geom["entry_mid"] = round(entry_mid, 8)
        geom["stop_pct"] = round(100 * risk_abs / entry_mid, 4)
        geom["t1_over_sl"] = round(t1_abs / risk_abs, 4)
        geom["cost_r"] = round((CALLS_COST_BPS / 1e4) * entry_mid / risk_abs, 4)
        geom["breakeven_winrate"] = round(
            (1 + geom["cost_r"]) / (1 + geom["t1_over_sl"]), 4)
        if call.t2:
            geom["t2_over_sl"] = round(abs(call.t2 - entry_mid) / risk_abs, 4)
        _warns = []
        if geom["cost_r"] > 0.25:
            _warns.append(f"cost_r {geom['cost_r']:.3f}R on a {geom['stop_pct']:.3f}% stop")
        if geom["t1_over_sl"] < 0.5:
            _warns.append(f"t1 only {geom['t1_over_sl']:.3f}x the stop")
        if geom["breakeven_winrate"] > 0.55:
            _warns.append(f"breakeven winrate {geom['breakeven_winrate']:.3f}")
        if _warns:
            geom["warnings"] = _warns
            log.warning("Call %s geometry: %s", cid, "; ".join(_warns))

    c = {
        "call_id": cid, "symbol": sym, "direction": direction,
        "entry_low": call.entry_low, "entry_high": call.entry_high,
        "sl": call.sl, "t1": call.t1, "t2": call.t2,
        "bias_score": call.bias_score, "note": call.note,
        "snapshot": call.snapshot,        # order-flow snapshot (validation)
        "geometry": geom,                 # cost / reward geometry at creation time
        "exit_mode": _CALLS_EXIT_MODE,    # which exit rule this call ran under (cohorts)
        "status": "open", "opened_ts": now, "activated_ts": None,
        "mfe_r": 0.0, "mae_r": 0.0, "last_price": None,
        "resolved_ts": None, "outcome_r": None,
    }
    with _CALLS_LOCK:
        _calls[cid] = c
    _calls_append({"ev": "open", "ts": now, "call": c})
    try:
        asyncio.ensure_future(send_telegram(_plan_alert_text(c)))
    except Exception as e:  # noqa: BLE001
        log.warning("Plan alert send failed: %s", e)
    return {"status": "logged", "call_id": cid}


@app.get("/calls")
async def calls_list():
    """Open/active calls + 20 most recent resolved."""
    with _CALLS_LOCK:
        open_calls = list(_calls.values())
        resolved = list(_calls_resolved)
    return {"open": open_calls, "resolved_recent": resolved[-20:]}


@app.get("/calls/stats")
async def calls_stats():
    """Win rate / expectancy over all resolved calls (full log)."""
    return _calls_stats()


@app.get("/calls/{call_id}")
async def calls_get(call_id: str):
    """Return a single call including its order-flow snapshot (validation)."""
    with _CALLS_LOCK:
        c = _calls.get(call_id)
        if not c:
            for r in _calls_resolved:
                if r.get("call_id") == call_id:
                    c = r
                    break
    if not c:
        raise HTTPException(status_code=404, detail=f"call '{call_id}' not found")
    return c


@app.post("/calls/{call_id}/close")
async def calls_close(call_id: str, request: Request):
    """Manually cancel an open call (excluded from stats)."""
    await _require_auth(request)
    with _CALLS_LOCK:
        c = _calls.get(call_id)
        if not c:
            raise HTTPException(status_code=404,
                                detail=f"call '{call_id}' not open")
        _calls_resolve_locked(c, time.time(), "cancelled", None,
                              c.get("last_price") or 0.0, None)
    return {"status": "cancelled", "call_id": call_id}


# ---------------------------------------------------------------------------
# Event blackout — GET /blackout/status
# ---------------------------------------------------------------------------
@app.get("/blackout/status")
async def blackout_status():
    """Return blackout window status (FOMC/CPI/NFP + crypto events)."""
    try:
        import event_blackout  # type: ignore
        return event_blackout.status()
    except Exception as e:
        # Fallback inline if module not available
        return {"blackout": False, "suppress_plans": False, "reason": f"event_blackout unavailable: {e}", "upcoming": []}


# ---------------------------------------------------------------------------
# Watchdog / heartbeat — GET /health/detailed + background _heartbeat
# ---------------------------------------------------------------------------
_HEARTBEAT_INTERVAL_S = float(os.getenv("HEARTBEAT_INTERVAL_S", "10"))
_heartbeat_state: dict = {"last_tick": time.time(), "checks": 0}


async def _heartbeat():
    """Background watchdog: monitors stale indicators, CVD gaps, WS liveness."""
    log.info("Watchdog heartbeat started (interval %.0fs)", _HEARTBEAT_INTERVAL_S)
    while True:
        try:
            _heartbeat_state["last_tick"] = time.time()
            _heartbeat_state["checks"] += 1
            # Update watchdog module if available
            try:
                import watchdog as _wd  # type: ignore
                # Feed live file mtime
                _live_path = _newest_live_indicator_path()
                if _live_path and os.path.exists(_live_path):
                    _wd.STATE.last_indicator_file_mtime = os.path.getmtime(_live_path)
                    try:
                        data = _json.loads(_shared_read.read_text(_live_path) or "{}")
                        ts = data.get("timestamp") or 0
                        if ts:
                            _wd.STATE.last_indicator_ts = float(ts)
                    except Exception:
                        pass
                # CVD save freshness
                if os.path.exists(_CVD_STATE_PATH):
                    try:
                        _wd.STATE.last_cvd_save_ts = os.path.getmtime(_CVD_STATE_PATH)
                    except Exception:
                        pass
                # Calls checker liveness
                _wd.STATE.last_calls_check_ts = time.time() if _calls else _wd.STATE.last_calls_check_ts
                # DOM last msg ages are updated via dom_state ts directly in collect()
            except Exception:
                pass
            # Also log warnings on critical staleness
            try:
                _live_path = _newest_live_indicator_path()
                if _live_path and os.path.exists(_live_path):
                    age = time.time() - os.path.getmtime(_live_path)
                    if age > 30:
                        log.warning("Watchdog: live indicators stale %.0fs (%s)", age, _live_path)
            except Exception:
                pass
        except Exception as e:
            log.warning("Heartbeat error: %s", e)
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)


@app.get("/health/detailed")
async def health_detailed():
    """Data-quality metrics: stale indicators, CVD gaps, WS reconnects."""
    try:
        import watchdog as _wd  # type: ignore
        with _CVD_LOCK:
            cvd_snap = dict(_cvd_state)
        async with dom_lock:
            dom_snap = dict(dom_state)
        with _CALLS_LOCK:
            open_n = len(_calls)
        return _wd.collect(
            live_file=_newest_live_indicator_path() or _LIVE_INDICATORS_FILE,
            dom_state=dom_snap,
            cvd_state=cvd_snap,
            calls_open=open_n,
            tick_history_path=_TICK_HISTORY_PATH,
        )
    except ImportError as e:
        return {"status": "error", "error": f"watchdog module missing: {e}"}
    except Exception as e:
        log.warning("health/detailed error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health/heartbeat")
async def health_heartbeat():
    """Lightweight heartbeat tick for supervisor."""
    return {"ok": True, "last_tick": _heartbeat_state["last_tick"], "checks": _heartbeat_state["checks"], "now": time.time()}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info(
        "Bridge live on http://%s:%d/alerta-atas — awaiting ATAS X alerts.",
        HOST,
        PORT,
    )
    uvicorn.run(app, host=HOST, port=PORT)
