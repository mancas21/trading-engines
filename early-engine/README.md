# early-engine — intrabar movement detection on Binance L2

## Why this exists

The `quant-alert-engine` fires on indicators computed from **closed bars**
(EMA crosses, RSI, MACD, volume persistence). Every one of its triggers is
late by construction: it sees a breakout only after the bar that breaks has
printed, and a bounce only after the bar that bounces has closed. The BB
alerts were removed in September for the same reason.

Nothing in the repo reads order flow to detect a move before it prints:
breakout pressure, absorption at a level, FVG retests, or the first leg of a
rally. The bridge holds a depth snapshot for display only.

This package adds those detectors. It runs on its own Binance USDT-M streams.

## What it detects

| Event | Reads | Fires |
|---|---|---|
| `BREAKOUT_PRESSURE` | price within band of resistance, taker buy z-score, bid-heavy book, depleted asks | before the level breaks |
| `BOUNCE_ABSORPTION` | price at support, taker sell z-score, price not falling, bid-heavy book | while sellers are being absorbed |
| `FVG_RETEST` | unmitigated 1m bullish FVG, price trades back into the gap, bid-heavy book | intrabar, on re-entry |
| `RALLY_IGNITION` | 30 s move ≥ 1 ATR, buy z-score, book, **not extended** beyond 1.5 ATR | first leg only |

Every event logs its feature values. The shadow log is the audit trail.

## Honest limits — read before trusting any output

- **Nothing here is validated.** Thresholds (`flow_z_min = 2`, `imb_min = 0.25`,
  etc.) are starting points, not measured edges. Calibrate them on shadow data.
- **Early means more false positives.** Each detector trades lead time for
  precision. The study script measures that trade-off; intuition does not.
- **FVG formation is not early.** A 1m FVG is known when its third candle
  closes. Only the *retest* is intrabar.
- **Order-book signals are noisy on thin markets.** Depth changes that look
  like a wall are often spoofing or market-maker quoting. Imbalance is a
  weak feature alone; the detectors require several conditions for that reason.
- **Lead time is measured, not asserted.** The control sampler logs
  unconditional forward returns every minute. An event is only useful if its
  forward return beats the control at the same horizon.
- **Not tested against live feeds.** The book, detectors, and runtime path are
  tested offline (13 unit tests, one synthetic end-to-end run). The WebSocket
  and REST wiring has not been exercised against Binance from the environment
  it was written in.

## Modes

- `shadow` (default): logs events and forward returns. Does **not** post to the
  bridge, so nothing reaches Telegram or the UI. Places no orders.
- `live`: also posts events to the bridge `/quant-alert` endpoint as
  `signal_type=EARLY`. Switch only after the study shows a positive excess
  return over control on several independent days.

No order placement is implemented here. That is deliberate.

## Run

```bash
pip install httpx websockets
cd early-engine
python early_engine.py --config config.json      # shadow mode
python study.py logs/early_events.jsonl          # after a few hours or days
python -m pytest tests/ -q                       # offline unit tests
```

Set `BRIDGE_TOKEN` in the environment if the bridge enforces auth.

## Study output

`study.py` prints, per event type and horizon (30 s, 60 s, 5 min):
mean event return, mean control return, the **excess** (event − control),
a Welch t-stat, and the hit rate of each. Read the excess and the t-stat
together. A positive excess on one day with n < 50 is noise.

## Files

- `book.py` — local L2 book with the documented Binance resync procedure. A
  gap invalidates the book, and an invalid book is never read.
- `detectors.py` — pure detectors. No I/O. Each returns an event with its
  invalidation level and the features that fired it.
- `early_engine.py` — runtime: streams, flow state, level refresh from 1m
  klines, shadow logging, control sampler, optional bridge post.
- `study.py` — event vs control analysis.
- `tests/test_early.py` — 13 unit tests covering book sequencing and each
  detector's firing and silent conditions.
