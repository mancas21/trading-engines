# ATAS Bridge — Agent Instructions

## Commands

- Bridge (recommended): `powershell -ExecutionPolicy Bypass -File start-bridge.ps1` — launches a detached auto-restart supervisor (survives shell exit, restarts on crash in 3s)
- Bridge (direct, dies on shell exit): `python bridge_server.py` (runs uvicorn on http://127.0.0.1:8000)
- Integrated Analysis: `python _integrated_analysis.py`
- DLL Build: `dotnet build` in `atas-exporter/` (targets net10.0, outputs to `%AppData%\ATAS X\Indicators\`)
- Lint: `python -m py_compile bridge_server.py`

## Skills

- **quant-crypto-analysis** — When the user asks to "analiza" / "analyze the chart" or requests market analysis, ALWAYS use this skill. It produces structured quantitative trader analysis with data from:
  - LiveExporter (OHLC, delta, footprint, Smart DOM, Smart Tape)
  - Binance DOM + Trade Flow WebSocket
  - Quant Alert Engine (multi-TF signals)
  - ATAS alerts buffer (DOM murallas / BigTrades)

## Endpoints

- `GET /indicators` — LiveExporter data
- `GET /cvd` — CVD state for all tracked symbols (session cumulative delta)
- `GET /cvd/SOLUSDT` — CVD for one symbol (short alias `SOL` also works)
- `GET /dom/solusdt` — Binance DOM snapshot
- `GET /quant/SOL` — Quant engine alerts
- `GET /state/SOLUSDT` — ATAS alerts buffer
- `GET /context/{symbol}` — Quant context: value area (POC/VAH/VAL), volatility regime (ATR rank / expansion / clustering), expected move (median, p90)
- `POST /analizar/SOLUSDT` — Trigger bridge-side analysis to Telegram
- `POST /reset/SOLUSDT` — Clear alert buffer
- `POST /calls` — Log a trade call {symbol, direction, entry_low, entry_high, sl, t1, t2?, bias_score?, note?}
- `GET /calls` — Open/active calls + recent resolved
- `GET /calls/stats` — Win rate / expectancy / profit factor over all resolved calls
- `GET /calls/{call_id}` — Return one call INCLUDING its order-flow snapshot (for validation)
- `POST /calls/{call_id}/close` — Cancel an open call (excluded from stats)

## Data Flow

```
ATAS X (LiveExporter C# indicator)
    ↓ writes to _live_indicators.json every 300ms
ATAS X (DOM/BigTrade alerts)
    ↓ POST /alerta-atas
Binance (WebSocket depth+trades)
    ↓ background task in bridge
Quant Alert Engine
    ↓ POST /quant-alert
Bridge (FastAPI on :8000)
    ↓ background task: CVD tracker (polls _live_indicators.json every 0.5s,
      detects bar rollover via bar_index, persists _cvd_state.json.
      Gap-tolerant: bar gaps/fast closes are skipped + recorded (gaps,
      missing_bars, bars_since_gap in /cvd), NOT wiped. Only chart reloads
      (bar_index backward) and sustained backfill races reset the segment.
      Rule 6/7 calls require bars_since_gap >= 2 or continuity == "clean")
    ↓ background task: Calls checker (polls Binance last price every 10s,
      resolves open trade calls: activation on print inside entry zone,
      then first touch of T1/SL; MFE/MAE tracked in R; events appended to
      _calls_log.jsonl, registry rebuilt by replay on startup)
```

## Quant Analysis Rules (audited after 10-call track record — 50% raw, NOT signal-validated; pre-fix entries lack snapshots)

ALL quant analyses MUST follow the 9 hard rules in `rules.json`. Key rules:
- **Rule 1:** No directional call from a single bar — wait 2+ bars of confirmation
- **Rule 2:** CVD spike >2x prior ATH in one bar = exhaustion — flag, never call continuation
- **Rule 4:** **RETIRADA 2026-10-13 (commit a93147f).** Exigía un scorecard 0-100 que se
  midió sin poder predictivo: pearson +0,0175 con `forward_return`, y el tercil alto
  ("mejor score") ganaba 32,8% frente al 37,9% del bajo. El sesgo sale únicamente de
  las reglas 6/7. Si alguien la reactiva, **no** reutilice el spread de +27,3pp que se
  midió: con autocorrelación rho=+0.995 equivale a ~1 observación independiente (z=0.35).
- **Rule 6:** Distribution requiere las 3: CVD cae 2+ barras + footprint + **consenso
  multi-venue del scanner** (`:8090`, túnel al VPS). Con el túnel caído es UNEVALUABLE
  y, al ser AND estricto, ninguna regla puede dispararse.
- **Rule 7:** Accumulation, igual que la 6 con el lado comprador. Hoy `enforcement:
  hard`.
- **Rule 6 (estado):** `enforcement: disabled` — evidencia insuficiente (n_eff=2,7), no
  anti-edge probado. Se sigue evaluando y publicando; no cuenta para el sesgo.

Every trade plan delivered in analysis MUST be logged via `POST /calls` (outcome tracking). This builds the validated track record.

## Phase 2 — snapshot-validated calls

`POST /calls` now accepts an optional `snapshot` object (full order-flow snapshot: price, delta, footprint top_levels, DOM imbalance, tape, CVD bar deltas, value area, expected move). It is stored and retrievable via `GET /calls/{call_id}`. **Every call that carries a snapshot is signal-validated; calls without one are NOT** (audit: pre-fix entries lack snapshots). The `orderflow_engine.py` monitor attaches a full snapshot to every call it logs.

## Key Files

- `bridge_server.py` — FastAPI server, all endpoints
- `start-bridge.ps1` — One-command bridge launcher (kills stale procs, spawns detached supervisor)
- `bridge_supervisor.bat` — Auto-restart loop (runs inside detached cmd console)
- `launch_bridge.vbs` — WScript launcher that fully detaches the supervisor from any parent shell
- `_integrated_analysis.py` — Multi-source analysis script
- `_live_indicators.json` — LiveExporter output (consumed by bridge)
- `_cvd_state.json` — CVD tracker persistence (written by bridge on bar close)
- `_calls_log.jsonl` — Trade call event log (open/activated/resolved; replayed on startup)
- `decision_engine.py` — Quant context engine (value area / vol regime / expected move from Binance klines)
- `quant_engine/` — Vendored engine subpackage (volume + volatility + core modules)
- `atas-exporter/LiveExporter.cs` — C# indicator source
- `atas-exporter/AtasExporter.csproj` — .NET project file
- `rules.json` — 9 hard rules for quant analysis (must-follow)
- `audit.log.json` — Track record of every call with outcome
