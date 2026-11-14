#!/usr/bin/env python3
"""early_engine.py — live runtime for the early-movement detectors.

Wiring:
  Binance USDT-M WS  --@depth@100ms-->  LocalBook (book.py)
                     --@aggTrade----->  taker flow window + price history
  Binance USDT-M REST --1m klines---->  ATR, prior-high/low levels, 1m FVGs
                                              |
                                  MarketView -> detectors.run_all
                                              |
              +-------------------+-----------+-----------+
              |                   |                       |
     JSONL event log      POST bridge /quant-alert   control sampler
     (every event +       (same path as the bar-close  (unconditional
      features)            engine, signal_type=EARLY)   baseline rows)

Why a control sampler: an early alert is only "early" if its forward move
beats what happens at a random moment. Every 60 s a control row is logged with
no condition attached. The study compares event forward returns to control
forward returns. Without that comparison, a high hit rate proves nothing.

Shadow mode is the default. Set `"mode": "live"` in config only after the
shadow study shows lead time and forward return above the control baseline.
Post-to-bridge is still honored in shadow mode so the buffer can be watched,
but nothing here places orders.

Requires: websockets, httpx. Run:  python early_engine.py --config config.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path

import httpx

import detectors as D
from book import LocalBook

log = logging.getLogger("early_engine")

FAPI_REST = "https://fapi.binance.com"
FAPI_WS = "wss://fstream.binance.com/ws"
HORIZONS_S = (30, 60, 300)


# ---------------------------------------------------------------- per-symbol state
class SymbolState:
    def __init__(self, symbol: str, cfg: D.Config):
        self.symbol = symbol
        self.cfg = cfg
        self.book = LocalBook(symbol)
        self.trades: deque = deque(maxlen=20000)     # (ts, qty, is_sell)
        self.prices: deque = deque(maxlen=20000)     # (ts, price)
        self.flow_hist_buy: deque = deque(maxlen=120)   # 5 s samples over 10 min
        self.flow_hist_sell: deque = deque(maxlen=120)
        self.ask_hist: deque = deque(maxlen=120)
        self.bid_hist: deque = deque(maxlen=120)
        self.levels: list[D.Level] = []
        self.atr_1m = 0.0
        self.last_price = 0.0
        self.last_sample_ts = 0.0
        self.last_event_ts: dict[str, float] = {}    # cooldown per event key

    def on_trade(self, ts: float, price: float, qty: float, is_sell: bool) -> None:
        self.trades.append((ts, qty, is_sell))
        self.prices.append((ts, price))
        self.last_price = price

    def _window(self, now: float, secs: float):
        cutoff = now - secs
        buy = sell = 0.0
        for ts, q, is_sell in reversed(self.trades):
            if ts < cutoff:
                break
            if is_sell:
                sell += q
            else:
                buy += q
        return buy, sell

    def flow_z(self, now: float) -> tuple[dict, float, float]:
        buy, sell = self._window(now, self.cfg.flow_window_s)
        if now - self.last_sample_ts >= 5.0:
            self.last_sample_ts = now
            self.flow_hist_buy.append(buy)
            self.flow_hist_sell.append(sell)
            m = self.book.metrics(self.cfg.band_bps)
            if m:
                self.ask_hist.append(m.ask_depth)
                self.bid_hist.append(m.bid_depth)
        flow = {
            "buy_qty": buy, "sell_qty": sell,
            "buy_z": D.zscore(buy, list(self.flow_hist_buy)),
            "sell_z": D.zscore(sell, list(self.flow_hist_sell)),
        }
        return flow, buy, sell

    def price_change_bps(self, now: float, secs: float) -> float:
        cutoff = now - secs
        old = None
        for ts, p in self.prices:
            if ts >= cutoff:
                old = p
                break
        if old is None or self.last_price == 0:
            return 0.0
        return D._bps(self.last_price, old)

    def signed_move_atr(self, now: float, secs: float) -> tuple[float, float]:
        """Move over the window in ATRs, and extension = distance from the move's low."""
        if self.atr_1m <= 0:
            return 0.0, 0.0
        cutoff = now - secs
        window = [p for ts, p in self.prices if ts >= cutoff]
        if len(window) < 2:
            return 0.0, 0.0
        lo, hi = min(window), max(window)
        start, end = window[0], window[-1]
        move = (end - start) / self.atr_1m
        ext = (end - lo) / self.atr_1m if move > 0 else (hi - end) / self.atr_1m
        return move, ext

    def view(self, now: float) -> D.MarketView | None:
        m = self.book.metrics(self.cfg.band_bps)
        if m is None or self.last_price <= 0:
            return None
        # A level is spent once price has traded through it. Without this a
        # broken resistance keeps producing "pressure" events long after the break.
        for lvl in self.levels:
            if lvl.kind == "resistance" and self.last_price > lvl.price:
                lvl.mitigated = True
            elif lvl.kind == "support" and self.last_price < lvl.price:
                lvl.mitigated = True
            elif lvl.kind == "fvg_bull" and self.last_price < lvl.bottom:
                lvl.mitigated = True
        flow, _, _ = self.flow_z(now)
        move, ext = self.signed_move_atr(now, self.cfg.rally_window_s)
        return D.MarketView(
            symbol=self.symbol, ts=now, price=self.last_price, atr_1m=self.atr_1m,
            levels=self.levels, flow=flow, book=m,
            book_baseline_ask=statistics.median(self.ask_hist) if len(self.ask_hist) >= 10 else 0.0,
            book_baseline_bid=statistics.median(self.bid_hist) if len(self.bid_hist) >= 10 else 0.0,
            price_change_bps_window={int(self.cfg.flow_window_s):
                                     self.price_change_bps(now, self.cfg.flow_window_s)},
            move_atr_window=move, extension_atr=ext,
        )


# ---------------------------------------------------------------- levels (REST)
async def refresh_levels(http: httpx.AsyncClient, st: SymbolState, cfg: D.Config) -> None:
    """Prior 60-bar high/low, 14-bar ATR, and 1m FVGs from closed 1m klines."""
    r = await http.get(f"{FAPI_REST}/fapi/v1/klines",
                       params={"symbol": st.symbol, "interval": "1m", "limit": 80})
    r.raise_for_status()
    rows = r.json()[:-1]  # drop the forming candle: closed bars only
    bars = [{"high": float(k[2]), "low": float(k[3]), "close": float(k[4])} for k in rows]
    if len(bars) < 20:
        return
    trs = []
    for prev, b in zip(bars, bars[1:]):
        trs.append(max(b["high"] - b["low"], abs(b["high"] - prev["close"]),
                       abs(b["low"] - prev["close"])))
    st.atr_1m = sum(trs[-14:]) / min(14, len(trs))
    window = bars[-61:-1]  # prior 60 closed bars, excluding the latest
    levels = [D.Level(price=max(b["high"] for b in window), kind="resistance"),
              D.Level(price=min(b["low"] for b in window), kind="support")]
    fvg = D.fvg_from_bars(bars, min_gap_atr=0.1, atr=st.atr_1m)
    if fvg is not None:
        levels.append(fvg)
    st.levels = levels


# ---------------------------------------------------------------- outputs
class Output:
    def __init__(self, cfg: dict):
        self.log_path = Path(cfg.get("event_log", "logs/early_events.jsonl"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.bridge_url = (cfg.get("bridge_url") or "").rstrip("/")
        self.bridge_token = os.environ.get("BRIDGE_TOKEN", "").strip()
        self.mode = cfg.get("mode", "shadow")
        self.pending: list[dict] = []  # rows awaiting forward returns

    def _write(self, row: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=float) + "\n")

    def record_event(self, ev: D.Event, now: float) -> None:
        row = {"kind": "event", **asdict(ev)}
        self._write(row)
        self.pending.append({"kind": "event", "key": ev.key(), "side": ev.side,
                             "symbol": ev.symbol, "t0": now, "p0": ev.price,
                             "due": {h: False for h in HORIZONS_S}})

    def record_control(self, symbol: str, price: float, now: float) -> None:
        self.pending.append({"kind": "control", "key": "control", "side": "long",
                             "symbol": symbol, "t0": now, "p0": price,
                             "due": {h: False for h in HORIZONS_S}})

    def settle(self, symbol: str, price: float, now: float) -> None:
        """Write forward returns for any row whose horizon has elapsed."""
        keep = []
        for row in self.pending:
            if row["symbol"] != symbol:
                keep.append(row)
                continue
            for h in HORIZONS_S:
                if not row["due"][h] and now - row["t0"] >= h:
                    row["due"][h] = True
                    self._write({"kind": "fwd", "row": row["kind"], "key": row["key"],
                                 "symbol": symbol, "horizon_s": h,
                                 "ret_bps": D._bps(price, row["p0"]) *
                                            (1 if row["side"] == "long" else -1)})
            if not all(row["due"].values()):
                keep.append(row)
        self.pending = keep

    async def post_bridge(self, ev: D.Event) -> None:
        if not self.bridge_url:
            return
        headers = {"Content-Type": "application/json"}
        if self.bridge_token:
            headers["X-Bridge-Token"] = self.bridge_token
        body = {"symbol": ev.symbol, "signal_type": "EARLY",
                "signal_name": f"{ev.name} {ev.side} @ {ev.level:g}",
                "price": ev.price, "timeframe": "tick", "color": "info"}
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(f"{self.bridge_url}/quant-alert", json=body, headers=headers)
        except httpx.HTTPError as e:
            log.warning("bridge post failed: %s", e)


# ---------------------------------------------------------------- streams
async def depth_loop(http, st: SymbolState, ws_base: str) -> None:
    import websockets  # runtime-only dependency
    stream = f"{ws_base}/{st.symbol.lower()}@depth@100ms"
    while True:
        try:
            async with websockets.connect(stream, ping_interval=60, max_queue=4096) as ws:
                buffered = []
                # buffer events while the snapshot is fetched
                snap_r = await http.get(f"{FAPI_REST}/fapi/v1/depth",
                                        params={"symbol": st.symbol, "limit": 1000})
                snap_r.raise_for_status()
                st.book.load_snapshot(snap_r.json())
                for _ in range(200):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        buffered.append(json.loads(raw))
                    except asyncio.TimeoutError:
                        break
                for ev in buffered:
                    if not st.book.apply(ev):
                        raise RuntimeError("bridging failed")
                log.info("%s book synced (gaps=%d)", st.symbol, st.book.gaps)
                async for raw in ws:
                    if not st.book.apply(json.loads(raw)):
                        raise RuntimeError("sequence gap, resync")
        except Exception as e:  # resync on any failure; never trust a stale book
            log.warning("%s depth stream: %s — reconnecting in 2s", st.symbol, e)
            st.book = LocalBook(st.symbol)
            await asyncio.sleep(2)


async def trade_loop(st: SymbolState, ws_base: str) -> None:
    import websockets
    stream = f"{ws_base}/{st.symbol.lower()}@aggTrade"
    while True:
        try:
            async with websockets.connect(stream, ping_interval=60) as ws:
                async for raw in ws:
                    d = json.loads(raw)
                    # m = True means the buyer is the maker, so the aggressor sold
                    st.on_trade(d["T"] / 1000.0, float(d["p"]), float(d["q"]), bool(d["m"]))
        except Exception as e:
            log.warning("%s trade stream: %s — reconnecting in 2s", st.symbol, e)
            await asyncio.sleep(2)


async def levels_loop(http, states: dict[str, SymbolState], cfg: D.Config) -> None:
    while True:
        for st in states.values():
            try:
                await refresh_levels(http, st, cfg)
            except Exception as e:
                log.warning("%s levels refresh failed: %s", st.symbol, e)
        await asyncio.sleep(30)


async def detect_loop(states: dict[str, SymbolState], cfg: D.Config, out: Output,
                      ctrl_every_s: float, shadow: bool) -> None:
    last_ctrl = 0.0
    while True:
        now = time.time()
        for st in states.values():
            v = st.view(now)
            if v is None:
                continue
            out.settle(st.symbol, st.last_price, now)
            for ev in D.run_all(v, cfg):
                last = st.last_event_ts.get(ev.key(), 0.0)
                if now - last < 60.0:  # per-level cooldown
                    continue
                st.last_event_ts[ev.key()] = now
                out.record_event(ev, now)
                log.info("EVENT %s %s %s @ %.6g level %.6g", ev.name, ev.side,
                         ev.symbol, ev.price, ev.level)
                # Shadow mode logs only. Nothing reaches the bridge (and therefore
                # Telegram) until the shadow study has validated the detector.
                if not shadow:
                    await out.post_bridge(ev)
        if now - last_ctrl >= ctrl_every_s:
            last_ctrl = now
            for st in states.values():
                if st.last_price > 0:
                    out.record_control(st.symbol, st.last_price, now)
        await asyncio.sleep(0.5)


# ---------------------------------------------------------------- main
async def main_async(cfg_path: str) -> None:
    raw = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    dcfg = D.Config(**raw.get("detectors", {}))
    symbols = [s.upper() for s in raw.get("symbols", ["BTCUSDT"])]
    ws_base = raw.get("ws_base", FAPI_WS)
    out = Output(raw)
    states = {s: SymbolState(s, dcfg) for s in symbols}
    async with httpx.AsyncClient(timeout=10) as http:
        tasks = [asyncio.create_task(levels_loop(http, states, dcfg)),
                 asyncio.create_task(detect_loop(states, dcfg, out,
                                                 raw.get("control_every_s", 60.0),
                                                 raw.get("mode", "shadow") == "shadow"))]
        for st in states.values():
            tasks.append(asyncio.create_task(depth_loop(http, st, ws_base)))
            tasks.append(asyncio.create_task(trade_loop(st, ws_base)))
        await asyncio.gather(*tasks)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main_async(args.config))


if __name__ == "__main__":
    main()
