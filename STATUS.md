# STATUS — quant-alert-engine (live alert system)

Verdicts below are measured, not aspirational. Numbers come from the engine's own
SQLite corpus (`quant-alert-engine/data/alerts.db`) unless stated otherwise.

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