# scanner-engine

A headless multi-exchange scanner. Reads public market data from **Binance,
OKX, Kraken and Bybit**, normalizes it into one shape, evaluates cross-venue
rules, and records every verdict with a forward outcome so the rules can be
audited instead of trusted.

This is a second engine, independent of `quant-alert-engine`, which needs ATAS X
and TradingView Desktop open to function at all. This one needs neither.

## Why a second engine

| | `quant-alert-engine` | `scanner-engine` |
|---|---|---|
| Sources | ATAS L2 footprint, TradingView via CDP, Binance | Binance, OKX, Kraken, Bybit (public REST) |
| Desktop apps required | **Yes** | **No** — proven on bare Python |
| Order flow | Real L2 footprint | Top-of-book only, no footprint |
| Third-party dependencies | pandas, numpy, ccxt, PyQt6, sklearn | **none** — stdlib only |
| Cross-venue signal | Impossible (single venue) | The entire point |

**What it cannot do:** no public venue exposes footprint aggregation, so the
ATAS engine's Rules 6/7 condition (b) cannot be evaluated here. This is declared
in `rules.json` under `capabilities.l2_footprint: false` rather than discovered
later.

**What it can do that the other cannot:** see venues disagree. XAU/USD measured
40.9 bps of cross-venue dispersion — 171 median venue spreads — on 2026-10-10,
which is either a real gold-perp basis or stale books. Every other pair sat
between 2 and 7 median spreads. Distinguishing the two is what the outcome
recorder is for. Re-measure with `python tests/live_features.py`; the figure is
live market data and moves.

## Quick start

```bash
# no dependencies to install
python scanner.py --interval 15 --port 8090

# one cycle and exit, printing health
python scanner.py --once

# audit what has been measured so far
python expectancy.py
```

Endpoints on `:8090`:

| Path | What it returns |
|---|---|
| `/health` | cycles, per-pair live venue counts, frozen/throttled venues, failures, recorder counts |
| `/rank` | conviction ranking; pairs without adequate evidence are excluded, not ranked last |
| `/verdict?pair=XAU/USD` | full verdict with per-subcondition `MET` / `UNMET` / `UNEVALUABLE` |

## The rules of this engine

These are not style preferences. Each one is a defect that was found, measured
and fixed — several in this repo's sibling engine.

1. **Live-only.** A venue whose last successful poll is older than
   `max_age_s` (300s) is dropped from the verdict and *named* in the output with
   its exact age. A venue that has never returned is absent from both — "never
   worked" and "stopped working" are different claims.

2. **Absent is not zero.** No data yields `None` or is excluded, never `0.0`.
   A missing tape is not "all selling"; a book with no size at the touch is not
   a vacuum. Only one of those claims is bearish.

3. **`direction = 0` means no direction.** Not neutral, not bearish. Those rows
   are retained (they record which rules were `UNEVALUABLE`) but excluded from
   expectancy math in SQL. In the sibling engine, 63% of rows carried
   `direction=0` and were averaged in as if short, which inverted its entire
   outcome gate.

4. **`forward_return` is stored raw.** Direction adjustment is derived in SQL
   at audit time. A pre-adjusted column cannot be re-derived once you discover
   the adjustment was wrong.

5. **An unclosed bar cannot produce an outcome.** `record_verdict` raises. The
   last row of any kline series is partially formed; scoring it invents both a
   signal and a forward return.

6. **No conclusion without significance.** A rule is only recommended for
   suppression when `mean < 0` **and** `n >= 50` **and** `t <= -1.65`. A negative
   mean alone is noise at small n.

7. **`UNEVALUABLE` is not `UNMET`.** "The venues agree" and "we could not check
   whether the venues agree" are different claims. Rules 3 and 4 require three
   venues and declare `UNEVALUABLE` for the two pairs that list on three, rather
   than quietly evaluating a 3-venue condition on 2 venues.

8. **Per-venue normalization.** Venue spreads differ by up to 100× (measured
   XAU: Binance 0.024 bps, Kraken 0.239 bps). A raw basis threshold measures
   tick size, not signal. The basis is normalized by each venue's own spread,
   and ranked from a **median** centre — a depth-weighted consensus ranked
   backwards, flagging the liquid venues and exonerating the thin one.

9. **Per-pair evidence floors and horizons.** `min_venues` is derived from each
   pair's own venue count (3 for SPCX/NVDA which Kraken does not list, 4 for the
   rest). The forward horizon is defined in **minutes** and converted per pair
   (1m pair → 5 bars, 5m pair → 1 bar), because a fixed bar count makes a 5m pair
   a 5× longer horizon and its expectancy non-comparable.

9b. **A venue may quote the same underlying under a different contract
   multiplier.** Binance and Bybit list `1000PEPEUSDT` (1000×); OKX lists
   `PEPE-USDT-SWAP` (1× price, size in contracts of 10,000,000 PEPE); Kraken
   lists `PEPEUSD` (1× spot, size in PEPE). The registry's `price_mult` /
   `size_mult` bring every venue onto the pair's reference scale before any
   cross-venue comparison. Their product preserves notional. Without it the
   cross-venue mid is a blend of two scales and every venue's basis is off by
   1000×. The venue symbols come from the registry too — deriving them from the
   pair name asks OKX for an instrument that does not exist. Measured detail:
   `docs/VENUE_FACTS.md`.

10. **One venue's failure cannot starve another.** A permanent rejection
    (`EQuery:Unknown asset pair`) does not open a venue-wide cooldown. This was a
    real bug: SPCX's permanent Kraken rejection silently removed Kraken evidence
    for HYPE and ETH in the same run, and both reported 3 venues with no failure
    recorded.

## Measurement integrity (2026-10-11)

An audit of the running engine found that its headline result was an artifact.
The engine had recorded 4,848 rows over 42 hours and reported:

\RULE 4   n=721  mean=-0.000090  t=-2.13   ->  "a significant loser"
\
It was wrong, for three independent reasons. All three are now fixed and pinned
by \	ests/test_measurement_integrity.py\.

### 1. XAU's "loss" was a venue spread, not a P&L

The fill was depth-weighted across venues. Measured live:

\XAU/USD   okx     depth5 286866  -> 99.99% of the fill weight
           bybit         16  ->  0.01%
           binance        7  ->  0.00%
\
OKX quotes gold ~30bps above Binance, so the fill was effectively OKX's price
and the exit was Binance's. Result:

\XAU/USD  n=599   raw mean -0.003029 (-30.3 bps)   sd 0.000165 (1.65 bps)
                  negative in 599 of 599 rows
\
599/599 the same sign with almost no variance is a constant offset, not a
distribution. XAU was the only pair the auditor graded as a loser (t=-2.80).
Now the fill is one venue's mid (\pairs.ref_venue\), the row records which venue,
and the resolver may not fall back to another. Measured after the fix, XAU's
5-minute return is **-0.01 bps** instead of -30.

### 2. Overlapping rows were counted as independent evidence

The loop polls every ~12s against a 5-minute horizon, so roughly 25 consecutive
rows share one underlying price move.

\pair       rows  distinct_windows   t_naive   t_effective
SOL/USD      167               57     -1.59         -1.24
XAU/USD       54               30     -2.80         -1.65
TOTAL        721              305     -2.13
\
_eff = n * (1 - r1) / (1 + r1)\, and \min_n\ applies to _eff\, not \.

### 3. Everything was quoted gross, with no multiple-comparison correction

Pooled gross mean was **-0.90 bps** against an **11 bps** round trip. Every cell
in that report sat an order of magnitude below the cost of acting on it. The
auditor also graded ~20 cells at an uncorrected 1.65 and flagged 6 of them,
which is the chance rate. The threshold is now Bonferroni-derived from the
number of cells actually graded, and _tests\ is printed.

### 4. Three of the four rules could never fire

\rule 1  MET?  NO  condition (b) needs history["1"]["cycles"], never populated
rule 2  MET?  NO  condition (b) was a hard-coded UNEVALUABLE; state forced to UNEVALUABLE
rule 3  MET?  NO  condition (b) needs history["3"]["depth_median"], never populated
rule 4  MET?  yes
\
\scanner.run_cycle\ called \evaluate()\ with no \history\ argument at all. The
engine shipped four rules and could measure one. \RuleHistory\ now carries the
cross-cycle memory, rule 2's price-response leg is measured rather than assumed,
and the scanner advances history every cycle. Live, rules 1 and 2 now fire.

### 5. A declared threshold no code read

Rule 4 declared \min_coverage_s: 30.0\. \_rule4\ never read it and
\eatures.compute\ hardcoded 20.0. Enforcing the declared value then exposed a
second problem: 30s is **unachievable**. Measured, OKX's trades endpoint caps at
500 trades (~5.8s of a 60s window) and Bybit reaches ~27s at 1000 trades.

A span-only gate is also the wrong gate. Measured on the same pair:

\binance  1000 trades / 41.0s      plenty of data
okx       500 trades /  5.8s      plenty of data, short span
kraken      4 trades / 25.1s      four trades, and it spans the window
\
Span favours thin venues and penalises busy ones, and a delta_ratio from four
trades can only be 0, 0.25, 0.5, 0.75 or 1. So the gate is now **both** a span
floor (5.0s, measured) **and** a trade-count floor (50, from ules.json\).

### What this did to the corpus

Every pre-fix row is marked \price_basis_ok = 0\ and excluded from expectancy.
The engine re-baselines to **0 auditable rows** and starts collecting clean data.
That is the correct answer, and it is deliberately the uncomfortable one: 42
hours of data bought a rule-level conclusion that was never there.

\data/scanner.db.pre-measurement-fix\ is the untouched backup.

---

## Current state

All four rules in `rules.json` are marked `UNVALIDATED` and `gate_signals` is
`false`. **No rule may suppress a signal until the auditor has measured it.**

A row where a rule fired MET carries a **provisional** side from that rule's
`direction_hint` and enters the expectancy math. It is a hypothesis under test,
not a prediction: it cannot suppress, alert, gate or rank. Only a rule the
auditor has promoted to `AUDITED` may contribute a side to the verdict ledger.

The corpus is currently at **zero auditable rows**, because the whole pre-fix
history is excluded on measurement grounds (see above). That is the designed
behaviour, not a defect:

```
EXPECTANCY AUDIT — INSUFFICIENT DATA
  directed + resolved 0   <- enters math
  non-directional     60   <- excluded from math
```

The bottleneck is not plumbing. It is that the rules are hypotheses, and until
enough directed outcomes accumulate on a trustworthy price basis there is
nothing to decide with.

**Do not lower `min_n`, `min_trades` or the significance level to force a
conclusion.** Insufficient data is a valid answer, and it is the answer this
engine gave for the first 42 hours of its life.

## Tests

```bash
python -m pytest tests/ -q          # 203 unit tests
python tests/live_features.py        # cross-venue features, real books
python tests/live_verdict.py         # features -> rules -> verdict, end to end
python tests/live_cvd_smoke.py       # CVD from all four venues
python tests/live_poller_smoke.py    # 4 venues x 8 pairs, failure routing
python tests/live_supervisor.py      # duplicate supervisor spawns nothing
python tests/live_restart.py         # crash->restart->clean, and lock release
```

## Layout

```
scanner-engine/
  scanner.py        the loop: poll -> features -> rules -> record -> serve
  poller.py         staleness drop, per-venue isolation, permanent/transient
  pairs.py          canonical registry; measured symbols, floors, bar intervals
  features.py       cross-venue basis, dispersion, flow divergence, liquidity
  flow.py           CVD normalization to a fixed time window
  rules_eval.py     MET / UNMET / UNEVALUABLE per subcondition + RuleHistory
  rules.json        4 cross-venue rules, all UNVALIDATED, no-footprint declared
  recorder.py       raw forward returns; direction=0 and bad basis excluded
  outcomes.py       the resolver; horizon bar on the SAME venue as the fill
  expectancy.py     the auditor: net of cost, overlap-corrected, Bonferroni
  promotion.py      auditor verdict -> rules.json, measured then written
  audit_tick.py     change-detected cron surface, versioned measurement basis
  supervisor.py     instance lock, restart only on crash, bounded attempts
  venues/           binance, okx, kraken, bybit
  docs/             VENUE_FACTS.md (measured), VPS-CHECKLIST.md, THE-BLOCKER.md
```

See `docs/VENUE_FACTS.md` for every endpoint gotcha measured on 2026-10-08 —
the Binance `TRADIFI_PERPETUAL` trap, OKX's missing taker volume, Kraken's
inconsistent pair-key mangling, and the rest.