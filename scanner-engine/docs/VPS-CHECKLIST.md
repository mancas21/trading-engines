# scanner-engine — headless / VPS deployment

**Proven, not assumed.** This engine was run on a bare `python` (the Hermes
3.14 toolchain, no venv, no site-packages) and completed a full cycle against
all four exchanges: `exit=0`, 8 rows recorded.

## Why this engine can run headless

Measured, not claimed:

- **Zero third-party imports.** `ast` walk over every module in
  `scanner-engine/` and `scanner-engine/venues/` returns only stdlib:
  `json, math, os, pathlib, sqlite3, subprocess, sys, threading, time,
  urllib.request, http.server, dataclasses, typing, ctypes, contextlib,
  argparse, tempfile, __future__`.
- **Zero imports from the desktop stack.** No `atas`, `tradingview`,
  `quant_alert`, or `reader` anywhere in the package.
- **No PyQt6, no ccxt, no numpy/pandas required** at runtime.

That is the whole point of building it: `quant-alert-engine` cannot run without
ATAS X and TradingView Desktop both open, because it reads footprint from ATAS
and chart state over CDP. This one reads only public REST.

## Verified in this environment

| Check | Result |
|---|---|
| Bare-python run, full cycle | `exit=0`, 8 rows recorded |
| `python` (Hermes 3.14, no deps) | passes all engine code |
| `quant-alert-engine/venv` (3.12.9) | 143 tests pass |
| Live books | 30/32 across 4 venues × 8 pairs |
| Supervisor duplicate guard | exit 0, 0 attempts, no child |
| Crash → restart → clean stop | 2 attempts, correct |

## Linux / VPS install

```bash
# Python 3.11+ is sufficient; nothing else is needed.
git clone <repo> trading-engines
cd trading-engines/scanner-engine

# no pip install step -- there are no dependencies.
python3 scanner.py --interval 15 --port 8090
```

### Health check

```bash
curl -s localhost:8090/health | python3 -m json.tool
```

`frozen_venues`, `throttled_venues` and `failures` must all be empty or
explained. A pair missing from `live_pairs` is a fault; a pair listed in
`coverage[...]["missing"]` is a fact (e.g. Kraken does not list SPCX or NVDA).

### systemd unit

```ini
# /etc/systemd/system/scanner-engine.service
[Unit]
Description=scanner-engine multi-exchange scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=scanner
WorkingDirectory=/opt/trading-engines/scanner-engine
ExecStart=/usr/bin/python3 scanner.py --interval 15 --port 8090
Restart=on-failure
RestartSec=10
# the supervisor already bounds the crash-loop; systemd's own Restart is a
# second line of defence, not the primary one.
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now scanner-engine
journalctl -u scanner-engine -f
```

### Do NOT wrap the scanner in the supervisor AND systemd

Both restart on failure, so a persistent crash becomes a double restart loop.
Pick one:

- **systemd** — preferred on a VPS. `Restart=on-failure` plus
  `RestartSec` plus systemd's own start-limit-burst.
- **supervisor.py** — for Windows, or when you want attempt numbering and the
  bounded `max_attempts` cap in one log file.

If you use systemd, do not also run `supervisor.py`.

## Network requirements

Outbound HTTPS/WSS to four hosts, nothing inbound, no API keys:

```
fapi.binance.com        (HTTPS, 32 polls/cycle)
www.okx.com
api.kraken.com
api.bybit.com
```

**Rate budget, measured:** Binance reports `REQUEST_WEIGHT MINUTE=2400`. A
cycle of 8 pairs × 1 kline = 8 weight. At `--interval 15` that is 4 cycles/min
= 32 weight/min — **1.3% of one venue's budget**. A 5s interval would be 3.9%.
There is no reason to poll faster; the book data is not that fresh anyway.

## What is NOT deployed yet

- **Telegram alerts** — not implemented. Deliberately separate from recording
  so alert spam can never suppress or distort the outcome recorder.
- **A live run with a non-zero `direction`** — every recorded row is
  `direction=0` because no rule has been audited. The auditor will keep
  reporting `INSUFFICIENT DATA`, correctly, until a rule is measured and earns
  a side. That is the real bottleneck, not deployment.

## Cross-venue dispersion, measured

`python tests/live_features.py` reports dispersion in units of each pair's
median venue spread, which makes pairs comparable:

```
XAU  171.6 spreads   (40.9 bps)   <- two orders of magnitude above the rest
SUI    ~6      SOL ~5      XRP ~6      HYPE ~6      SPCX ~4      NVDA ~2
```

XAU at 171 median spreads is a genuine gold-perp dislocation or stale books;
the engine cannot tell which from one sample, which is why persistence (rule 1
condition b) and the outcome recorder both exist. Re-measure rather than trusting
these numbers — they are live.

## Honest caveat on VPS latency

Cross-venue basis dispersion assumes the snapshots are near-simultaneous. On a
VPS in one region, all four venues are close enough that a 15s-old snapshot is
fine. If you deploy far from the exchange DCs, the `basis_dispersion_bps`
feature will partly measure network path rather than genuine dislocation. The
feature is normalized by each venue's own spread, but normalization cannot
recover a latency skew it does not know about. Watch `venue_ages_s` in
`/health`: if the spread of ages exceeds a few seconds, treat cross-venue
dispersion with suspicion until measured.