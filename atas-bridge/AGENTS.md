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
- **Rule 4:** Must output signal scorecard (0-100). Bias >= 60 bullish, <= 40 bearish, else neutral
- **Rule 6:** Distribution requires ALL: CVD drops 2+ consecutive bars + 4 levels 70%+ selling + DOM < +5%
- **Rule 7:** Accumulation requires ALL: CVD rises 2+ consecutive bars + 3 levels 70%+ buying + DOM > +10%
- **Rule 10:** DELETED 2026-09-17 (audit C-1 action 1) — score ranks only, never gates plans.

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
- `rules.json` — 10 hard rules for quant analysis (must-follow)
- `audit.log.json` — Track record of every call with outcome

---

## Order-flow call generation (Binance-only) — `orderflow_calls.py`

`/calls` sat at **0 entries** and `/calls/stats` returned **HTTP 500**, so the whole
validation loop was dead. Two independent causes:

1. `quant-alert-engine/orderflow_engine.py` scores through `signal_engine.compute_signal`,
   which needs ATAS LiveExporter footprint/tape. **ATAS is a GUI app and is usually
   closed**, so `_data_coverage` is 25/100 < 75 and `trade_plan_allowed` is always
   False — it can never log a call without the charted symbol's footprint.
2. Nothing was running a generator anyway.

But the bridge already exposes a rich **Binance-only** order-flow surface that does
not need ATAS at all: `/taker-cvd/{sym}` (session taker CVD in qty + USD with
extremes), `/dom/{sym}` (20 levels, imbalance, bid/ask walls + persistence),
`/derivatives/{sym}` (OI, funding, long/short ratio), `/context/{sym}` (value area,
vol regime, expected move), `/blackout/status` (macro events).

### Commands

```
python orderflow_calls.py scan       # one pass, prints components, POSTs nothing
python orderflow_calls.py monitor    # loop: score -> POST /calls (with snapshot)
.\orderflow-calls.ps1 start|stop|status     # detached run, PID file, log file
python orderflow_calls.py monitor --dry-run # loop, never POST
```

### Design rule

Every **raw** component is logged in the call's `snapshot` (36 keys) together with the
weighted `components` dict, and the composite is a fixed documented weighting
(`WEIGHTS` at the top of the file). The composite only decides **when** to log; any
later thesis ("weight taker flow higher", "funding is contrarian") is tested OFFLINE
on the same logged data instead of re-fitting and re-running the generator.

Two calibrations that were measured, not assumed:

* **Flow components must be normalised by their OWN recent variability.** The first
  version divided the trailing taker delta by the session's cumulative USD range
  (~113M on SOL), so a real −44k USD 12s delta scored −0.004: the highest-weight
  component was dead. Now `taker_flow = delta / (3 × mean|recent delta|)` and
  `oi_delta` uses %-of-OI against 0.05%. Both are logged so they can be re-derived.
* **Market-wide levels are z-scored ACROSS symbols.** Funding, long/short ratio and
  value location measured nearly identical on all 7 symbols (ls_ratio −0.88..−1.00,
  value_loc pinned), i.e. zero cross-sectional information that would just tilt every
  call the same way. They are z-scored across the symbols sampled at the same instant,
  so only the relative outlier votes. Do NOT clamp them before the z-score — that
  flattened the ordering and destroyed the spread the transform needs.

### Bugs fixed in `/calls/stats` (both were the "empty log" path, never exercised)

* `_subset_metrics` returned `{"n": 0}` for an empty subset while the caller read
  `all_m["wins"]` → `KeyError: 'wins'` → 500.
* `round(None, 3)` in `profit_factor_gross` → `TypeError: type NoneType doesn't
  define __round__` → 500.

### How to read the output (do not over-read)

* `cost_bps = 8.0` (4bp taker ×2); `expectancy_r_net` is already cost-adjusted, and
  `expectancy_r_gross` is not. Use the NET figure for decisions.
* `/calls/stats` splits **`validated`** (calls carrying a snapshot, i.e. this
  generator) from **`legacy`** (no snapshot). Only `validated` is signal-validated;
  a synthetic/legacy call cannot contaminate it.
* `snapshot.atas_present = false` and `source = "binance_only"`: these calls carry
  **no footprint and no ATAS tape**. Say so before attributing an outcome to "order
  flow" — the snapshot has taker CVD, book walls, funding/OI and value area, not
  per-level bid/ask footprint.
* Concurrency is capped (`--max-open`, default 12) and calls live up to 48h
  (`CALLS_ACTIVE_TTL_H`). Calls open simultaneously on 7 correlated symbols are NOT
  independent observations; effective n is smaller than the raw count. The stats
  `confidence` field only reaches "low" at n≥10, "medium" at n≥30.
* The resolver polls Binance last price every 2.5s, so a spike between polls can be
  missed — T1/SL touches are approximate, not tick-exact.

---

## ATAS enrichment — `binance_only` vs `binance+atas` in the same calls

`orderflow_calls.py` attaches ATAS LiveExporter footprint/Smart-Tape/Smart-DOM to a
call **automatically when ATAS is showing that same instrument**. Nothing to wire:
open ATAS with the LiveExporter and the next call for that symbol carries it.

```
python orderflow_calls.py scan              # shows the atas column per symbol
python orderflow_calls.py monitor --atas off   # never attach (pure binance)
python orderflow_calls.py monitor --atas-max-age 30
.\orderflow-calls.ps1 start                 # the normal path: --atas auto
```

### The rule that matters: never attach the wrong symbol's footprint

The LiveExporter reflects **whichever chart ATAS is on**. One chart = one instrument,
so at any instant at most ONE of the tracked symbols can legitimately carry footprint.
`atas_features()` returns no attach — with the reason recorded — when:

* `ticker != symbol` → `ticker_mismatch(<ticker>)`. ATAS is simply on another
  instrument; this is the common case, not an error.
* sample older than `--atas-max-age` (default 30s) → `stale(<age>s)`. The bridge
  itself only rejects files older than 300s, which is far too old to hang on a live
  call — and worse, it *looks* valid.
* ATAS not exporting → `atas_inactive(no_data)`.

The reason is written to `snapshot.atas_status` on **every** call, so a green
`atas_present: true` is never inferred from silence:

```
atas_present: true|false
atas_status:  "ok" | "ticker_mismatch(SUIUSDT)" | "stale(412s>30s)" |
              "atas_inactive(no_data)" | "no_payload"
source:       "binance+atas" | "binance_only"
```

A call with ATAS carries **70 snapshot keys vs 36 without** (footprint levels,
per-level bid/ask/delta/ticks, POC/value-area, tape net_vol/buy_count_pct/largest
trade + its distance, DOM stacks). Raw `top_levels` are kept so features can be
re-derived offline.

### Why the ATAS components carry weight 0 in the live composite

`atas_footprint`, `atas_tape`, `atas_dom_stack` are logged but do NOT enter `WEIGHTS`.
Deliberate: if they changed the score, the trigger would differ depending on whether
ATAS happens to be open, and `binance_only` vs `binance+atas` would be **two different
decision rules** instead of one rule with more data. Keeping the trigger fixed makes
the A/B readable; the features are there to be weighted offline on the same sample.

### Reading the comparison honestly

`python orderflow_calls_report.py` buckets the track record by provenance. Two traps:

* **It is not a controlled experiment.** The ATAS subset is whichever symbol happened
  to be on the chart, so it is selection-biased by construction. Report it as
  "the enriched subset did X", never "ATAS adds X".
* **Feature AUC is in-sample by design.** The composite chose these calls using these
  components. With under ~30 resolved calls, several features reach AUC 1.000 purely
  by chance — the report prints an explicit NOISE warning and refuses to rank below 10.

`orderflow_calls_report.py` joins the event log, so it needs all three event shapes
(`open` carries the snapshot; `activated`/`resolved` are thin and keyed by `call_id`).
It cross-checks `n` and net expectancy against `/calls/stats` and prints a MISMATCH if
they disagree — a broken reader used to report "0 traded" while the bridge had several.
