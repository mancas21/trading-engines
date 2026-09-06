# trading-engines — monorepo

Single home for all trading engines. Each engine runs from its own folder;
nothing here is duplicated — originals remain where they were until cutover.

| Folder | Engine | Language | Ports / entry |
|---|---|---|---|
| `quant-alert-engine/` | Quant Alert Engine — multi-TF signal generator (RSI/MACD/CVD/Stoch/BB/ATR/OBV), blackout + confluence + regime + Level-2 gates | Python | `:8081/health` · `run_ui.bat` / `quant_alert_ui.py` |
| `atas-bridge/` | ATAS Bridge Engine — LiveExporter order flow, Binance DOM+CVD WS feeds, quant multi-TF state, event blackout | Python (FastAPI) | `:8000` · `bridge_server.py` |
| `tradingview-bridge/` | TradingView Desktop bridge via CDP — chart read/control, Pine toolchain, morning brief | Node (MCP stdio + CLI) | CDP `:9222` · `tv` CLI / `src/server.js` |
| `docs/` | Consolidated documentation + audits | — | — |
| `scripts/` | Shared launchers (TV with CDP, path resolver) | PowerShell | — |

## Launch order

1. TradingView Desktop with CDP: `scripts\launch_tv_debug.ps1 -Port 9222`
   (or the copy in `tradingview-bridge/scripts/`)
2. ATAS bridge: `cd atas-bridge` → `python bridge_server.py` (serves `:8000`)
3. Quant Alert Engine UI: `cd quant-alert-engine` → `run_ui.bat`
   (health `:8081/health` — checks: exchange, database, symbols,
   macro_regime, outcome_gating, time_gating, blackout, level2)

## Data flow

```
TradingView chart ──CDP──▶ tradingview-bridge (tv CLI / MCP)
ATAS X ──LiveExporter──▶ atas-bridge :8000 (/dom /cvd /quant /indicators /state)
quant-alert-engine ──reads──▶ atas-bridge Level-2 validator (DOM+CVD+quant score)
quant-alert-engine ──POSTs──▶ atas-bridge /quant-alert (one-way forward)
```

## First-time setup per engine

- `quant-alert-engine/`: `setup_windows.bat` (creates `venv`), copy config template,
  create `.env` from `.env.example`, `pip install -r requirements.txt`
- `atas-bridge/`: `pip install -r requirements.txt`, create `.env` from `.env.example`
- `tradingview-bridge/`: `npm install`, copy `rules.example.json` → `rules.json`

## Secret hygiene

Live `.env` files are git-ignored (see `.gitignore`). Only `.env.example`
templates are committed. Never `git add -f` a real `.env`.
