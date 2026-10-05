# AUDIT — rule files and plan files

**Date:** 2026-09-17 · **Auditor stance:** independent, evidence-based. Every finding carries the
measurement behind it and the command that reproduces it. No finding is based on reading intent.
**Scope:** the artifacts that define *what may be traded* (rule files) and *how a trade is shaped*
(plan files) in `trading-engines`.

## 0. Artifacts audited

| artifact | size / mtime | role | consumed by |
|---|---|---|---|
| `atas-bridge/rules.json` (v3.2) | 3,503 B · 2026-08-02 | 10 hard rules + `scoring_weights` 25/25/20/15/15 | `atas-bridge/signal_engine.py` (line 74), `telegram-bot.mjs` (weights hardcoded, not read) |
| `atas-bridge/signal_engine.py` | 13,710 B | implements rules 2, 6, 7, 10 + data-coverage gate | `quant-alert-engine/orderflow_engine.py`, `_validate_rules.py`, tv bot |
| `quant-alert-engine/config.json` | 4,709 B (gitignored) | the gates that actually run: confluence 2, blackout 300s, cooldown 600s, time-gating 08–15h local, level2 gate, `ml_filter` 0.55 shadow | `quant_alert_engine.py`, `quant_alert_ui.py` |
| `atas-bridge/orderflow_calls.py` (`WEIGHTS`, `build_plan`) | 33,560 B · today | **the live plan generator**: own composite + own geometry | run as `monitor` |
| `quant-alert-engine/orderflow_engine.py` (`build_plan`) | 18,953 B | second plan builder, same signals | QAE |
| `tradingview-bridge/rules.json` + `.example.json` | 1,997 / 943 B · 2026-06-15 | **a different strategy**: "10-Second Momentum Scalper", no stop loss, max $3/trade, "demo for a YouTube video" | `src/core/morning.js` only |
| `quant-alert-engine/audit.log.json` | **858 B · 2026-09-14** | Rule 8's mandated loss root-cause log | `_validate_rules.py` |
| `atas-bridge/audit.log.json` | **absent** (documented in `atas-bridge/AGENTS.md`) | call track record | — |

Out of scope: strategy alpha, execution venue, UI, the TradingView bridge's chart tooling.

## 1. Method

1. **Spec read** — every rule file read in full, including enforcement flags and thresholds.
2. **Consumer read** — traced what code actually reads each rule file, and what it implements:
   `signal_engine.compute_signal()` implements rules 2/6/7/10 and the coverage gate; **nothing
   implements rules 1, 3, 5, 8, 9** (no code reads them at all).
3. **Harness read** — `_validate_rules.py` backtests rules against the audit log; checked whether it
   has data and what it excludes.
4. **Re-derivation** — re-computed the plans from the artifacts and compared them against
   (a) the 16 logged plans in `_calls_log.jsonl`, (b) live feed values, (c) the 1m-kline replay of
   the 16 calls, (d) 1,000-bar klines for correlation/volatility. This is where the material
   findings come from.

## 2. Findings

Severity: **C**ritical = the artifact does not do what it says and the effect is measurable in
results; **H**igh = unenforceable / internally contradictory; **M**edium = wrong or dead logic;
**L**ow = hygiene.

### C-1 — The plan generator logs trades its own rule set forbids (16 of 16)

`rules.json` Rule 10: *"No trade plan if bias score is 40–59"*; Rule 4: *"≥60 bullish, ≤40 bearish"*.
Measured over the 16 logged plans (`_calls_log.jsonl`, probes excluded):

* **2** in the forbidden 40–59 band (HYPE 45.2, XRP 48.8);
* **14** with bias < 40 (bearish zone) — and **10 of those were LONG plans** (e.g. XRP long @32.3,
  SUI long @23.6, SOL long @21.2, SOL long @0.0);
* **0** with bias ≥ 60.

`orderflow_calls.py` attaches the composite to the call as `bias_score` and never gates on it — the
score is a label on a plan that the score's own thresholds prohibit. Impact: the published track
record (`n=15`, 53.3% win rate, −0.1405R net) is a record of rule-violating plans, so it cannot be
used to validate the rules it violates. Fix: either gate on the score (Rule 10 enforced) or delete
Rule 10 and the score field — the current state is the worst of both.

### C-2 — Plan geometry is a fixed fraction of a 4-hour expected move, so the stop sits inside normal noise

`orderflow_calls.build_plan()`: `sl_d = mid·(exp_move_med/100)·0.85`, `t1_d = mid·(exp_move_med/100)·1.00`,
`t2_d = mid·(exp_move_med/100)·1.8·(p90/med)`. Therefore **T1/stop = 1.0/0.85 = 1.1765 by
construction**, and the stop is 0.85 × the engine's *median 4h expected move* for the symbol.

Measured today (bridge `/context`, per symbol): stop% = 0.85·median → BTC 0.268, ETH 0.442, SOL 0.485,
XRP 0.549, SUI 0.697, HYPE 0.731, SPCX 0.285, NVDA 0.209, XAU 0.203. The stops in the logged calls
match this to a few bp (XAU 0.193, NVDA 0.197, SUI 0.757–0.758, SOL 0.556–0.558, HYPE 0.786) — the
construction is confirmed, not inferred.

Two consequences, both measured:

* **Cost:** at 8bp round trip (`CALLS_COST_BPS`, the repo's own assumption) `cost_r = 0.08/stop%`:
  XAU **0.393R**, NVDA **0.384R**, BTC 0.299R, SPCX 0.281R, ETH 0.181R, SOL 0.165R, XRP 0.146R,
  SUI 0.115R, HYPE 0.109R. On the realised 16 calls: mean 0.231R, max 0.415R.
* **Noise:** the same `/context` reports trailing 4h range ÷ median expected move = **2.7×–7.9×**.
  The stop is 2.3–9.3× *smaller* than the range the symbol routinely covers over the horizon the
  model itself uses.

Breakeven win rate with T1/stop = 1.1765 is `(1+cost_r)/2.1765` = **52.6% at 0.144R, 55.6% at
0.21R, 65.0% at 0.415R**; realised is 50% on the validated subset. The geometry cannot pay for
itself with the stops it emits — this is arithmetic, not a market call.

### C-3 — The 60/40 gate is decided by measurement noise, not by the market

The gate that permits or forbids a plan is the composite score, and the composite's top-weighted
field (`rules.json` `scoring_weights.dom_imbalance = 25/100`) is a single top-15 Binance snapshot.
Measured:

* 12 polls × 9 symbols, 8s apart: the raw field moves the composite **5.37 points per 8s round**
  (45s EMA: 2.50), flips the zone label **29 times vs 12**, and 8/9 symbols touch more than one zone
  inside 2 minutes.
* Two full pulls 3m44s apart moved the composite by a mean of **16.6 points** and changed the zone
  on 6/9 symbols; three further pulls in this session produced three further re-orderings (top of the
  board: BTC/ETH → NVDA/SPCX → XAU/SOL → SPCX/ETH/BTC → BTC/SPCX/ETH → SOL), each with prices moving
  <1%.

So "is a plan allowed?" is, in practice, a coin flip on a sampling artefact — and leg (c) of rules 6
and 7 (`DOM < +5%` / `> +10%`) inherits exactly that field. Fix: feed the gate the 45s-averaged
field (the bridge now publishes `imbalance_pct_ema`), and require persistence before either 6/7 or
Rule 10 may fire.

### H-1 — Rules 6 and 7 are unverifiable for 8 of the 9 tracked symbols

Both require ATAS LiveExporter footprint levels (`footprint_levels ≥ 4` / `≥ 3` with 70% bid/ask
dominance). ATAS exports one chart at a time, so `footprint_levels` is non-empty for exactly the
symbol on the chart (today: SUIUSDT). For the other eight, leg (b) cannot be evaluated at all, and
`signal_engine` silently scores the missing footprint as neutral 5/10 — it does not report
"unverifiable". A rule marked `"enforcement": "hard"` that cannot be evaluated for 89% of the
instrument universe is not a rule, it is a commentary.

### H-2 — The data-coverage guard does not catch the case it was written for

`signal_engine._data_coverage()` weights: footprint_depth 25 + footprint_confirmation 15 +
dom 25 + tape 20 + volume 15. With **no footprint at all** — the normal case — coverage is
(25+20+15)/100 = **0.60**, which passes the `< 0.5` plan block at line 323. The docstring says the
guard prevents "single DOM reading manufactures a plan"; at 0.5 it only fires when DOM *and* tape
*and* volume are all absent. Fix: threshold ≥ 0.75, and make footprint mandatory (with an explicit
"unverifiable" status) for rules 6/7 rather than a neutral substitute.

### H-3 — Rule 9 rests on a false premise

Rule 9: *"SUI and SOL analyzed independently. Decoupling means correlation = 0."* Measured over
1,000 15m bars: **SUI–SOL correlation = 0.889** (1h: 0.803); basket-average pairwise 0.499, PC1
60.5% of variance, effective breadth 1.65. The rule instructs the analyst to treat as independent
two instruments that move together almost one-for-one, which systematically over-counts evidence
(two "independent" confirmations are one bet).

### H-4 — Two plan builders, two geometries, no single source of truth

| | `orderflow_calls.build_plan` (live generator) | `orderflow_engine.build_plan` (QAE) |
|---|---|---|
| entry zone | ±0.05% of mid | ±0.15% of mid |
| stop | 0.85 × median expected move | 0.85 × median expected move |
| T1 | 1.00 × median | 1.00 × median |
| T2 | 1.80 × median × (p90/median) | 1.80 × median |
| rounding | 8 decimals | 4 decimals |

Same signals, different plans, and neither is referenced by the other. Two of the 16 logged calls
match **neither** construction (T1/stop = 0.087) — an undocumented third path. Measured T1/stop
across the log: mean 0.944–1.018 (vs 1.1765 by construction), min 0.087, max 1.165. Fix: one
builder, one schema, and a CI test that asserts every generated plan satisfies it.

### H-5 — The entry zone is a fixed % of price while the stop is volatility-scaled

`entry_pct` is a constant fraction (0.0005 / 0.0015) but `sl_d` scales with the symbol's expected
move. Live ATR(15m)% ranges 0.31 (NVDA) to 1.22 (HYPE) — a 4× spread — so a fixed ±5bp zone is
~1/6 ATR on NVDA and ~1/24 ATR on HYPE. Activation requires a print inside that zone, sampled every
2.5s, so the probability of ever activating, and the difference between the planned mid and the
actual fill, are symbol-dependent and unmodelled (no slippage, no partial fill, no maker/taker
entry distinction). All R accounting assumes a fill at the zone mid.

### H-6 — `tradingview-bridge/rules.json` describes a different product

It specifies a "10-Second Momentum Scalper": enter on the previous 1-minute candle direction,
exit after 10 seconds, **no stop loss**, fixed 1% of portfolio capped at **$3.00** per trade, max 6
trades/minute, noted as *"Demo strategy for YouTube video."* Nothing in the order-flow stack reads
it, yet it shares the filename `rules.json` with the hard-rule set and the tv bot's own commentary
claims to use "rules.json weights 25/25/20/15/15" — weights that exist only in `atas-bridge/rules.json`
and are hardcoded in `telegram-bot.mjs`. Three artifacts, two schemas, one name.

### H-7 — The sample this audit measures against is not ground truth

Every win-rate / expectancy figure quoted here (`n=15`, 53.3% win rate, −0.1405R net) comes from
`/calls/stats`, which is computed from recorded `t1_hit` / `sl_hit` labels. Those labels are not
exchange truth: replaying the three long-held calls against the actual print stream (Binance
aggTrades, no intrabar ordering ambiguity) contradicts the log on **3 of 3** cases — HYPEUSDT-c8ce08
and XAUUSDT-dcc1b8 are labelled `t1_hit` after 15h while the first level the prints actually touched
was the **stop** (2.6h and 1.6h after activation), and NVDAUSDT-3e05ef is labelled `sl_hit` while the
first touch was **T1** (2.2h). Mechanism: the resolver observes price only while the bridge process
runs, so any restart or outage blinds the call's path and the call is later resolved on whatever
level it happens to see afterwards.

Consequence for this audit: C-1, C-2 and M-5 are unaffected (they derive from plan construction and
the rules text, not from outcomes), but **every performance claim built on the log is provisional**
until the path is recorded per call. Fix: persist each call's observed path (levels touched, last
price, a monotonic coverage clock) and tag any resolution spanning an unseen window
`path_coverage_suspect`; exclude tagged calls from expectancy, or report them as a separate cohort.
Until then, `/calls/stats` should carry a coverage caveat rather than a `confidence` label.

### M-1 — Rule 2's veto is unreachable in practice

Implemented (`signal_engine._check_rule_2_exhaustion`): `|last delta| > 2 · max(|prior deltas|)`,
evaluated only on a clean CVD series. Measured live multiples across this session: 0.03×, 0.10×,
0.13×, 0.89× — never near the 2× flag. Under any reading it is dead code as a veto, and the spec
text ("CVD spike >2x prior **ATH**") differs from the implementation (prior *window max*), so the
threshold has no stated empirical basis.

### M-2 — Rule 8's trail is not maintained; the validator has nothing to validate

Rule 8 mandates root-cause entries in `audit.log.json` "at START of next analysis".
`quant-alert-engine/audit.log.json` is **858 bytes, 3 days old**; `atas-bridge/audit.log.json` —
documented in `atas-bridge/AGENTS.md` as the call track record — **does not exist**. `_validate_rules.py`
(which correctly excludes non-snapshot entries to avoid phantom results) therefore tests the rules
against a near-empty sample.

### M-3 — Rule 3 vs the shadow model's published probability threshold

`ml_filter.min_prob = 0.55` with the model's own reported **AUC 0.552** (49 features, 1,640 resolved
signals). Rule 3 forbids probabilities not backed by a validated hit-rate. Currently harmless only
because `gate_signals = false` (`scored: 37, gated: 0`); flipping that switch would put the repo in
violation of its own Rule 3 while publishing a 0.55 threshold on a coin-flip classifier.

### M-4 — Cost model omits funding, and funding is systematic in this regime

`CALLS_COST_BPS = 8.0` covers 4bp taker ×2 and nothing else — no funding, no borrow, no slippage
beyond the flat assumption. Live funding is **+10.9% ann on SUI, +15.4% on NVDA, +10.7% SOL, +10.2% BTC**
and the generator's calls were 10 long / 5 short, i.e. systematically on the side that *pays*. A 15h
hold at 10.9% costs ≈0.019% of notional ≈0.04R on a 0.5% stop — small per trade, but always against
the book and never stopped out.

### M-5 — `T2` is advertised but unmanaged

Median T2/stop across the 14 calls that carry a T2 is **6.41** (max 6.75×P90-derived). The resolver
(`bridge_server._calls_step`) never references `t2` — it checks only `sl` and `t1`. Measured: max
realised outcome **1.165R**; no call ever approached T2. The plan payload promises a 6R runner that
the execution layer cannot take. (The `half_at_1r` exit mode added today is the first mechanism
that can manage a runner; it is off by default.)

### L-1 — Version/ownership hygiene

`rules.json` carries version 3.2 and a 2026-08-02 mtime with no owner, changelog or date-of-effect;
code that consumes it changed since. All ten rules claim `"enforcement": "hard"`, but code implements
4 of them and the live plan generator implements none of the four — the flag overstates reality.

### L-2 — Dead/duplicated rule surface

Rules 1, 5 and 9 are read by no code and were not applied by any of the 16 logged plans (Rule 1 needs
2 closed bars; several plans were logged on the bar of the signal). Rule 5 ("no pattern anchoring")
is in tension with the persistence flags the same stack relies on (`bid_wall_persistent`).

## 3. What is genuinely good

* `signal_engine.py` is a **pure function** (no I/O, no randomness, same input → same output) with
  inline audit-fix annotations, and `_validate_rules.py` correctly refuses to simulate entries whose
  snapshots were zero-filled — a real methodological trap avoided.
* The right guards exist and are wired: Rule 2 veto, Rule 6/7 contradiction veto, data-coverage
  reporting on every result, `trade_plan_allowed` plus machine-readable `plan_blocked_reasons`.
* `CALLS_COST_BPS` is the only cost-adjusted expectancy I have seen in this repo's peer set, and the
  `/calls/stats` split between `validated` and `legacy` is exactly the right cut.
* `build_plan` refuses to emit malformed geometry (line 550-553), so the bridge never receives an
  impossible plan; the bridge rejects them independently too.
* Secret hygiene: `config.json` (live Telegram token + chat id) is gitignored and not tracked;
  only `.env.example` is committed.

## 4. Priority actions

| # | action | fix for | effort |
|---|---|---|---|
| 1 | Decide the role of the score: **gate on it** (enforce Rule 10 in `orderflow_calls.py`) or **delete it and Rule 10**. | C-1 | small |
| 2 | Make the stop cost-aware: floor stop% so `cost_r ≤ 0.15R`, and set T1 ≥ 1.5×stop with a managed runner. Today that would suppress plans on XAU/NVDA/BTC/SPCX (cost_r 0.28–0.39) and keep them on HYPE/SUI/XRP. | C-2 | small |
| 3 | Feed Rule 10 and rules 6/7 leg (c) the 45s-averaged DOM (`imbalance_pct_ema`) and require persistence across ≥3 polls. | C-3 | small |
| 4 | Raise the coverage gate to ≥0.75 and mark rules 6/7 "unverifiable" (not neutral) when footprint is absent. | H-1, H-2 | small |
| 5 | Collapse to **one** plan builder + one schema; delete or rename `tradingview-bridge/rules.json` so `rules.json` means one thing. | H-4, H-6 | medium |
| 6 | Rewrite Rule 9 to the measured correlation structure (or delete it), and add the correlation/breadth figure to every basket-level output. | H-3 | small |
| 7 | Emit `audit.log.json` entries from the resolver (it already has the data); point `_validate_rules.py` at `_calls_log.jsonl`. | M-2 | medium |
| 8 | Add funding to the cost model using live `derivatives.fundingRate` per symbol and hold time. | M-4 | small |
| 9 | CI gate: replay each new plan on 1m klines (`exit_replay.py`) and reject plans whose breakeven win rate > 55% or `cost_r > 0.25R` at creation. | C-2, H-4, M-5 | medium |

## 5. Appendix — measurements and reproduction

All figures come from the pull at 10:18–14:35 local on 2026-09-17 and from `_calls_log.jsonl`
(18 `open`, 16 `activated`, 18 `resolved`; the 3 verification probes are excluded throughout).

| metric | value | source |
|---|---|---|
| logged plans / resolved | 16 / 18 | `_calls_log.jsonl` |
| plans with bias in 40–59 (forbidden by R10) | 2 | same |
| plans with bias < 40 | 14 (10 of them long) | same |
| plans with bias ≥ 60 | 0 | same |
| T1/stop, construction | 1.1765 | `build_plan` |
| T1/stop, realised | mean 0.944–1.018, min 0.087 | log |
| T2/stop, median | 6.41 | log |
| stop% realised | mean 0.501, min 0.193, max 0.975 | log |
| stop% implied by construction today | 0.203 (XAU) … 0.731 (HYPE) | `/context` |
| cost_r at 8bp | 0.109 (HYPE) … 0.393 (XAU), mean 0.231 | computed |
| breakeven win rate | 52.6% @0.144R · 55.6% @0.21R · 65.0% @0.415R | computed |
| realised win rate (validated) | 50% | `/calls/stats` |
| rng4h ÷ expected move | 2.69 (SUI) … 7.88 (SPCX) | `/context` |
| composite move per 8s poll, raw vs EMA | 5.37 vs 2.50 points | `dom_ab.py` |
| zone flips, raw vs EMA (12 polls × 9 sym) | 29 vs 12 | `dom_ab.py` |
| composite move over 3m44s | mean 16.6 points, 6/9 zones changed | `scorecard_history.jsonl` |
| SUI–SOL correlation (15m / 1h) | 0.889 / 0.803 | 1,000-bar klines |
| basket avg pairwise corr / PC1 / breadth | 0.499 / 60.5% / 1.65 | 1,000-bar klines |
| coverage with no footprint | 0.60 (gate is 0.5) | `_data_coverage` + weights |
| Rule 2 multiples observed | 0.03× – 0.89× (flag at 2×) | `/cvd`, `analyze2.py` |
| `audit.log.json` age / size | 3 days / 858 B (atas-bridge copy absent) | filesystem |

Reproduce: `python fetch_live.py && python analyze.py && python analyze2.py && python live_card.py`
(in `Downloads/trading-engines-master/live-analysis/`), plus
`python -c "import signal_engine"`-level calls documented per finding above.
