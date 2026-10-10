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

10. **One venue's failure cannot starve another.** A permanent rejection
    (`EQuery:Unknown asset pair`) does not open a venue-wide cooldown. This was a
    real bug: SPCX's permanent Kraken rejection silently removed Kraken evidence
    for HYPE and ETH in the same run, and both reported 3 venues with no failure
    recorded.

## Current state

All four rules in `rules.json` are marked `UNVALIDATED` and `gate_signals` is
`false`. **No rule may suppress a signal until the auditor has measured it.**

Every recorded verdict therefore carries `direction = 0` and is excluded from the
expectancy math. That is the designed behaviour, not a defect:

```
EXPECTANCY AUDIT — INSUFFICIENT DATA
  directed + resolved 0   <- enters math
  non-directional     60   <- excluded from math
```

The bottleneck is not plumbing. It is that the rules are hypotheses, and until
enough directed outcomes accumulate there is nothing to decide with.

## Tests

```bash
python -m pytest tests/ -q          # 143 unit tests
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
  rules_eval.py     MET / UNMET / UNEVALUABLE per subcondition
  rules.json        4 cross-venue rules, all UNVALIDATED, no-footprint declared
  recorder.py       raw forward returns, direction=0 excluded, unclosed barred
  expectancy.py     the auditor; day-one answer is INSUFFICIENT DATA
  supervisor.py     instance lock, restart only on crash, bounded attempts
  venues/           binance, okx, kraken, bybit
  docs/             VENUE_FACTS.md (measured), VPS-CHECKLIST.md
```

See `docs/VENUE_FACTS.md` for every endpoint gotcha measured on 2026-10-08 —
the Binance `TRADIFI_PERPETUAL` trap, OKX's missing taker volume, Kraken's
inconsistent pair-key mangling, and the rest.