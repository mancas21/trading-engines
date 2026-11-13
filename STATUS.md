# STATUS — quant-alert-engine (live alert system)

Verdicts below are measured, not aspirational. Numbers come from the engine's own
SQLite corpus (`quant-alert-engine/data/alerts.db`) unless stated otherwise.

---

## 2026-11-06 — Criba prompts100 acelerada x8 (señal una vez, caché feather, pool) — resultados idénticos bit a bit

`backtest_prompts100/` fast pass (decisión conjunta con backtest-system:
*idea → veredicto validado* en unos treinta minutos, motores y convenciones
intactos):

- `run_all.py`: `run_one` construye la señal UNA vez y la puntúa a 11 y 22 bps
  (antes re-ejecutaba todo el stack de indicadores por coste); `--workers N`
  lanza las estrategias en pool reordenando a orden STRATS (CSV idéntico);
  `--eq oos` (por defecto) guarda solo el tramo OOS de cada curva en
  `eqs.npy` — `make_charts10.py` necesita curvas completas: regenerar con
  `--eq full --solo <nums>`.
- `btlib.py`: caché feather en `data/_csv_cache` (clave nombre+mtime_ns+tamaño
  de cada CSV fuente; el frame cacheado es el parse crudo, el post-proceso
  dtype/astype/dedupe/sort corre en ambos caminos → DataFrame bit-idéntico).
  backtest-system/data sigue read-only.
- Indicadores vectorizados con verificación de igualdad EXACTA contra las
  versiones de bucle en BTCUSDT 1h y SOLUSDT 5m: aroon (argmax de ventana
  invertida), percentrank y el streak/percentrank de connors_rsi — incluida
  la regla NaN de pandas (ventana con NaN → NaN; descubierta porque la
  primera versión falló la igualdad y la prueba la cazó). wma/hma/kama/
  supertrend/psar quedan en bucle: una estrategia cada uno y el orden de
  suma flotante no es demostrablemente idéntico.
- Igualdad old-vs-new por cada camino de código (signal, tuple, flip, xs,
  funding): cero diferencias. Pantalla completa (doscientas corridas): 39 s
  frente a 5 min 08 s; el DataFrame de 200 filas es idéntico entre la
  corrida pre y post vectorización del mismo día.
- El enlace con backtest-system no cambia (solo lectura); su capa de
  validación ganó cortes de datos y `bt.py` — ver
  `docs/BACKTEST-SYSTEM-LINK.md` sección ocho.

---

## 2026-10-29 — Segunda pata de ejecución: Alpaca (acciones US) vía CLI

El `execution/` deja de ser solo Binance: hay un cliente de Alpaca orquestando
su CLI oficial (`alpaca.exe`, v0.0.15, instalado y autenticado ese día). La
cuenta es **paper** (PA3KTTITR0AF, $100k, opciones nivel 3, crypto activo).
Las claves NO van en el `.env`: la autenticación vive en el perfil del CLI
(`alpaca profile login`).

| pieza | estado |
|---|---|
| `execution/alpaca_broker.py` | cliente: localiza el binario, envuelve el CLI en JSON, lista blanca `ALLOW_ALPACA_SYMBOLS`, tope `MAX_ALPACA_NOTIONAL_USD`, dry-run por defecto + gate `ALLOW_ALPACA_ORDERS`, log `logs/alpaca_orders.jsonl` |
| `execution/alpaca_check.py` | chequeo solo lectura con los códigos 0/2/3/4 de `check_testnet.py` |
| `execution/alpaca_router.py` | señal → bracket protegido: mismas decisiones del operador (una posición por símbolo, sizing por riesgo, stop en el exchange) |
| `execution/tests/test_alpaca_broker.py` | 22 tests offline de la política (dry-run gate, whitelist, sizing, lados del stop, envelope de error del CLI) |

Evidencia: 22/22 tests (`python -m pytest execution/tests/test_alpaca_broker.py -q`)
y chequeo vivo solo-lectura de `alpaca_check.py` (exit 0: clock, asset, cuenta,
posiciones, bracket en seco como `not_sent`). El router en modo plan contra la
cuenta real produce el PLAN con sizing correcto (0.5% riesgo → tope de nocional
manda con shares enteras → AVISO de riesgo por debajo del objetivo).

Diferencias declaradas con la pata crypto: bracket **atómico** de Alpaca
(entrada+stop+TP juntos, verificación pos-fill igual), **shares enteras**
(brackets fraccionales fuera), caja sin apalancamiento (`cash`, no
`buying_power`), **mercado cerrado no entra** (órdenes `day` en cola llenarían
con el stop de hoy), sin `--require-candidate` (no hay gate de backtest para
acciones US: quien ejecuta responde de la señal) y sin `state/positions.json`
(el reconciliador crypto no debe ver AAPL; trazabilidad en
`logs/alpaca_router.jsonl`).

Límite que no se disimula: **paper no prueba costes ni fills reales**, y no hay
señal con evidencia para acciones US en este repo — esto es plumbing, igual que
el testnet. En reposo, `ALLOW_ALPACA_ORDERS=0`.

Mismo día, capa de agente: el **MCP server oficial de Alpaca**
(`alpaca-mcp-server` vía uvx, 65 herramientas) queda registrado en
`~/.zcode/cli/config.json` → `mcp.servers.alpaca`, leyendo las claves del
`execution/.env` por `--env-file` (la config de ZCode no guarda secretos) con
`ALPACA_PAPER_TRADE=true` y toolsets recortados a
`account,trading,watchlists,stock-data,news`. Queda `enabled: false` hasta que
el operador genere claves API en el dashboard (el login OAuth del CLI no sirve
para la REST API) y corra `execution/set_alpaca_mcp.py`. Es consola interactiva
del agente, no parte del bucle del engine: no pasa por los topes de
`alpaca_broker.py`.

---

Esto **no reabre** el veredicto de abajo. El motor de alertas del QAE sigue sin
edge demostrado y no se enruta. Lo que se conectó es otra cosa: los candidatos
`cbpackf` del backtest-system que sí pasan el gate del repo (PF>1.2 ex-costes,
n≥30, 2/3 tercios verdes, vecindad estable, WF/OOS, vs buy&hold) y que se
re-corrieron a **11 pb** roundtrip (5 pb fee + 0,5 pb slip por lado, medidos).

| pieza | estado |
|---|---|
| `execution/live_signal.py` | señal viva con el **mismo** engine auditado (paridad exacta con los artefactos: trades/PF/net) |
| `execution/router.py` | tamaño por riesgo 0,5% (haircut a 0,25% por concentración) + bracket `STOP_MARKET closePosition=true` |
| `execution/auto_router.py` | señal → abrir/cerrar/revertir/mantener, una posición por símbolo, sin perseguir entradas >24 barras |
| `execution/reconcile.py` | recrea el stop si desaparece, detecta cierres vía `userTrades`, avisa de huérfanas |
| claves + cliente | testnet cargado, `check_testnet.py` OK, Algo Order API (`-4120`) resuelta |

Evidencia: `execution/evidence_testnet_2026-10-25.json` (firma, filtros, fills,
fees 4 pb del testnet) y `execution/evidence_automation_2026-10-25.json` (24/24
comprobaciones end-to-end: bracket, stop recreado, cierre detectado, huérfana,
`entering` en vuelo). Tests: 17 offline de decisión/reconciliación + 10 del gate.

Límite que no se disimula: el testnet cobra 4 pb (no 5) y su libro es otro, así
que **ningún PnL de testnet es evidencia de edge**. Solo prueba el plumbing. La
cuenta quedó plana y `ALLOW_ORDERS=0` en reposo.

---

## 2026-10-06 (Fase 6) — REDISEÑO COMPLETADO: el motor es ahora un LECTOR

Las seis fases de `PLAN_REDESIGN.md` están ejecutadas. Resumen del estado final.

### Lo que existe ahora (`reader/`)

| Fichero | Rol |
|---|---|
| `verdict.py` | Motor de veredictos. Reglas 6/7/2 como salida principal. El scorecard 0-100 se **retiró** el 2026-10-13 (medido sin poder predictivo); el sesgo sale solo de las reglas. Requiere el túnel al scanner del VPS (`:8090`) para el gate multi-venue. |
| `live_reader.py` | Las 4 fuentes en un punto de entrada. Una fuente ausente **se declara**, nunca se imputa. |
| `verdict_recorder.py` | Graba cada veredicto en SQLite, resuelve el retorno futuro con fill en el OPEN de la barra siguiente. |
| `verdict_notify.py` | Telegram **solo al cambiar de sesgo**. 12 lecturas simuladas → 6 mensajes, los 6 reales. |
| `reader_supervisor.py` | Servicio con política de reinicio de 3 estados. |
| `atas-bridge/atas_target.py` | Qué símbolo tiene el L2 real (cobertura = 1, elegirlo ES el diseño). |

### La limitación real, medida

`reader_supervisor.py --status` durante una sesión con ATAS cerrado:

```
recorder_rc   1
recorder_tail SKIP: ATAS no disponible: TimeoutError: timed out
```

**El grabador no inventa dato: no graba y lo dice.** ATAS y TradingView son
aplicaciones de escritorio de Windows; si el servidor se apaga, no hay análisis.
Para tener evidencia hace falta ATAS abierto. No es un bug del código.

### Ruta perdedora eliminada (Fase 6)

Borrado: `orderflow_calls.py`, `orderflow_calls_report.py`, `plan_gate.py`,
`monitor_supervisor.bat`, `orderflow-calls.ps1`,
`quant_engine/orderflow_backtester.py`. El último-commit del rediseño conserva
`signal_engine.py` porque `bridge_server.py` lo usa para el comando de chat
`/analizar` — que es a demanda, no automático.

### Estado

- `outcome_gating` muta 1 señal (Stoch Bearish Cross, t=−1.81) y el pool sigue
  negativo: **eso no se puede arreglar con gates**.
- Las 6 familias de señal y el ML están en `enabled: false` con su evidencia en
  `config.json.pre-edgecut-*`.
- Objetivo de evidencia: **5.000 veredictos** (~4–5 meses) antes de volver a
  preguntar por edge. El contador más reciente estaba en ~120.

---

The first audit established the pool has no edge. This pass found **two independent
losing subsystems** and cut both. Nothing in this engine currently generates a
trade recommendation.

### Finding 1 — the signal engine has no positive cell anywhere

Re-run with the corrected t-statistic on the full corpus (2,808 rows / 28.0 days,
1,016 directed). Per-family, direction-adjusted, net of a 0.11% round trip:

| family | n | dir-adj mean | t | net of cost | verdict |
|---|---|---|---|---|---|
| CVD Bearish Flip | 209 | +0.000370 | +0.61 | −0.000730 | CUT |
| Stoch Bearish Cross | 142 | −0.000742 | −1.81 | −0.001842 | CUT |
| OBV Bearish Break | 97 | −0.000351 | −0.96 | −0.001451 | CUT |
| CVD Bullish Flip | 94 | −0.000303 | −0.87 | −0.001403 | CUT |
| RSI Crossed Below 50 | 94 | −0.000355 | −0.94 | −0.001455 | CUT |
| RSI Crossed Above 50 | 56 | −0.000607 | −0.76 | −0.001707 | CUT |
| Stoch Bullish Cross | 56 | −0.000338 | −0.52 | −0.001438 | CUT |
| BB Breakout Down | 55 | +0.000434 | +0.82 | −0.000666 | CUT |
| MACD Bearish Cross | 32 | +0.000639 | +0.81 | −0.000461 | CUT |
| MACD Bullish Cross | 23 | −0.001261 | **−2.04** | −0.002361 | CUT (proven loser) |

**Zero families pass `t > +1.65 AND net > 0`.** The strongest positive is
CVD Bearish Flip at t=+0.61 — noise. A 40-cell sweep over
family x symbol x timeframe x direction at n≥8 also returned **zero** cells with
edge. Direction split: long n=337 t=−1.55, short n=694 t=+0.73 — neither side
works, so this is not an inverted-leg bug.

**Action taken:** `cvd`, `rsi`, `macd`, `stochastic`, `volatility`, `volume` all
set to `enabled: false` in `quant-alert-engine/config.json`, each carrying a
`_disabled_reason`. Backup at `config.json.pre-edgecut-<ts>`.

### Finding 2 — the rules/orderflow call path is a proven loser, and the engine agrees

`/calls/stats` on the bridge (its own audit surface, not my computation):

```
n_traded                24          winrate              37.5%
expectancy_r_gross      -0.2236     expectancy_r_net     -0.4196
profit_factor            0.423      cost_bps             8.0
bootstrap_ci95_net      [-0.8044, -0.0216]   <- excludes zero
confidence              low
```

Every slice loses except one n=3 slice:

| slice | n | expectancy net | PF net |
|---|---|---|---|
| validated | 20 | −0.343 | 0.518 |
| exchange_verified | 21 | **−0.5003** | 0.348 |
| legacy | 4 | −0.8028 | 0.002 |
| contradicted | 3 | +0.1453 | 1.327 |

By symbol: SPCX +0.556 and HYPE +0.252 (n=2 and n=5) are the only positives;
XRP −0.773 (n=6), SOL −0.757, SUI −0.546. Both directions lose (long −0.401,
short −0.451), so there is no side to keep.

**The bootstrap CI excludes zero** — this is a statistically resolved loser, not an
unproven one.

**Action taken:** none needed on the running system — the orderflow monitor is
**not currently running** (no matching process), so these 24 calls are historical.
`plan_gate.py` already enforces the right creation-time invariants
(breakeven win rate ≤0.55, cost_r ≤0.25). It was left in place. If that monitor
is ever restarted, it must not be treated as validated.

### What is NOT cut, and why

- **The outcome/time gates stay on.** They correctly mute Stoch Bearish Cross
  (t=−1.81). But with the pool negative there is nothing left to unmute, so the
  gate cannot make the engine profitable — a low alert count is not evidence the
  gate is "working well".
- **rules.json Rule 6/7 evaluation stays.** It is the only part of this codebase
  that produced a defensible verdict during live reads (it correctly refused the
  17:45 setup that resolved bearish). It produces a *reading*, not a trade.
- **ML stays `gate_signals: false`.** AUC 0.552 vs 0.510 permuted baseline.

### Honest state

The engine as it stands **cannot produce a trade recommendation with evidence
behind it**. What remains working is data plumbing: live-only freshness gating,
the live-only source discipline, exporter atomic publish, supervisor/lock
behaviour, and the rules-based *reader*. Re-enable a family only when
`audit_edge.py` shows `t > +1.65 AND net > 0` for it.

### Errors caught during this audit (both mine)

1. I wrote the t-statistic as `sd/(sd/sqrt(n))`, which **equals `sqrt(n)`** — so
   every cell printed a large positive t regardless of its mean. It surfaced
   because n=209 gave t=+14.46, and sqrt(209)=14.46 exactly.
2. I read `_calls_log.jsonl` directly and concluded **all 44 calls were stuck
   `open`** because `outcome_r` is null on `open` records. The live endpoint shows
   43 already resolved; resolution happens via the `ev: resolved` event and is
   applied in `_calls_replay()`, not on the `open` record. Reading the log without
   replaying it understated the engine's own record.

---

## 2026-10-06 — QUANT AUDIT: no tradeable edge found. Do not trade this.

**Verdict: the engine has no demonstrated edge, and the sample cannot yet prove
one either way. Keep it as an alerting/observation system. It is not a trading
bot.**

### The corpus this rests on

| Fact | Value |
|---|---|
| Span | 2026-09-08 → 2026-10-06 = **27.8 days** |
| Total rows | 2,775 (2,758 resolved) |
| Directed rows usable (`direction != 0`) | **1,014** (63% have no direction) |
| Timeframes | 1m 1,329 · 5m 1,022 · 15m 262 · 30m 112 · 1h 50 |
| `forward_bars` | **12 for every row** — one fixed horizon, not per-signal |

### The number that settles it

```
mean direction-adjusted return per signal : -0.000241  (-0.0241%)
Binance perp round trip (0.05%/side + slip): -0.001100  (-0.1100%)
                                       NET : -0.001341  (-0.1341%)
```

**Every signal, before any gate, loses money net of costs.** Win rate 43.9%
against a ~50.1% break-even for a symmetric 1:1 TP/SL.

Per-cell analysis (29 cells with n ≥ 15):

- Cells with **positive** expectancy t > +1.65: **0**
- Cells with **negative** expectancy t < −1.65: **3** (STOCH HYPE t=−2.95,
  STOCH SUI t=−2.47, RSI XRP t=−1.74)
- At |t| > 1.65 across 29 cells, ~1.5 false positives are expected by chance.
  The Bonferroni threshold for 29 cells is t = 2.40, and the single strongest
  |t| is **2.95 — negative**. So the best "finding" is a *loser*, and even that
  is at the edge of what 28 days can distinguish from noise.

**There is no positive-expectancy cell in the corpus.** The signal families are
at best neutral and at worst measurably negative.

### Why the gates cannot fix this

The engine's outcome/time gates mute signals on negative adjusted expectancy.
That is correct and it is why only **1 signal is muted today** (Stoch Bearish
Cross). But the problem is not one bad signal — it is that the **pooled** edge is
negative, so the gate is already doing nearly all it can. There is nothing to
"optimize into" profitability without new information.

### Distributional warning

p05 −0.69% · p50 −0.00% · p95 +0.49% · min −3.06% · max **+10.83%**. 4.3% of
rows exceed |1%|. These are fat tails: a t-statistic on ~30 samples is dominated
by a handful of bars, so per-cell |t| near 2–3 is weaker evidence than it looks.
Heavy tails also mean a single favourable outlier can manufacture an apparently
significant positive cell — none was found here.

### Power: what it would take to settle it

At sd = 0.0056, detecting a 0.05% per-signal edge needs n ≈ 483 (have 1,014);
0.10% needs n ≈ 121. So the sample size is **adequate for a 0.1% edge** — the
problem is that no edge of that size is present, not that the data is too small
to see it. Reaching n ≈ 5,000 at the current rate takes ~137 days.

### What this does NOT mean

- It does not mean the signals are useless for **observation** — CVD and footprint
  read well as situational context.
- It does not mean the architecture is wrong. Live-only gating, freshness
  enforcement, and the supervisor/lock work are sound and verified.
- It does not mean no edge exists — it means **none is demonstrated in 28 days**,
  and the honest state is "unproven", not "proven worthless".

### Recommended posture

1. **Do not route this to order execution.** There is no order code in the repo
   and adding any would trade a negative-expectancy signal stream.
2. Keep collecting rows. Re-run this audit at n ≈ 5,000 before revisiting.
3. The ML layer stays `gate_signals: false`. Prior logreg AUC 0.552 against a
   0.510 permuted baseline is reordering noise on this corpus.
4. `forward_bars` should become per-signal-type. A 12-bar horizon on a 1m bar is
   12 minutes and on a 1h bar is 12 hours; expectancy is not comparable across them.

---

## 2026-10-06 — Rule-gate plumbing: all six conditions now evaluate

Before this work, rules.json Rules 6/7 could not fire: `imbalance_pct_ema` was
absent from `/indicators` (it existed only on `/dom`), and the eval script tested
for CVD fields (`history`, `cvd_series`, `cvd_up_streak`) that do not exist —
the real series is `bar_deltas`. Fixed in `7c86505`, `70ba0c8`, `67c86ad`.
`rule_gate_eval.py` now reports **zero UNEVALUABLE** conditions: both rules
evaluate all three sub-conditions on live data.

A DLL redeploy resets the exporter and invalidates CVD bar continuity, which
silently un-evaluates condition (a) again. `/health` now publishes `cvd_warmup`
and `/cvd` publishes `warmup` / `segment_reason` so this is visible.

## 2026-10-06 — VOL spam: fixed 2026-09-27 (VOL-1)

19.9 signals/symbol-day before → **1.1** after. `tradingkey_movers` disabled
(US-equities only, 0/7 crypto coverage). Polymarket fallback repointed to a live
endpoint; macro pipeline DNS failures fixed via ThreadedResolver.