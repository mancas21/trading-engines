# STATUS — quant-alert-engine (live alert system)

Verdicts below are measured, not aspirational. Numbers come from the engine's own
SQLite corpus (`quant-alert-engine/data/alerts.db`) unless stated otherwise.

---

## 2026-10-06 (later) — DEEP AUDIT + CUT: every signal family disabled, every call path verified losing

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