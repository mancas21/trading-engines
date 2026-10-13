# The blocker: no rule can ever acquire a direction

**Status: RESOLVED 2026-10-11.** All six steps below are implemented. What
followed was a measurement-integrity audit that invalidated the corpus the
blocker fix had been collecting into — see `README.md`, "Measurement integrity".

The circular dependency in Gap 2 is gone: `outcomes.hint_direction` gives a
candidate row a provisional side, `expectancy.by_rule` measures the candidate
ledger, and `promotion.promotion_report` decides what is earned.

What replaced the blocker is quieter and arguably worse: the engine was
measuring a rule it could not honestly measure. A direction the auditor could
not earn was not the binding constraint; a fill venue that was not the exit
venue was.

## What the data says

```
scanner-engine/data/scanner.db   336 rows over 22.3 minutes, 8 pairs

  rows                       336
  forward_return NOT NULL      0    <- nothing is ever scored
  resolved                     0
  direction != 0               0    <- nothing enters expectancy math
  distinct verdict states      2-6 per pair (out of 42 rows)
```

The loop is running and healthy. It is recording rows that can never be used.

## The two independent gaps

### Gap 1 — the outcome resolver was never wired

`recorder.resolve()` exists and is tested. **Nothing calls it.** The scanner
writes `forward_return=None` on every row and moves on:

```python
# scanner.py:184
forward_return=None,
```

So there is no code path that ever looks at a price *after* the horizon and
records what happened. Every row stays `resolved=0` forever.

This is the plain half of the problem and it is a missing feature, not a design
tension.

### Gap 2 — the circular dependency (this is the real one)

Even if the resolver were wired, nothing would change:

```
1. rule fires, evaluator returns a verdict
2. verdict.direction is "neutral"  (no rule may name a side until AUDITED)
3. so _direction() returns 0
4. so the recorded row has direction=0
5. so the auditor EXCLUDES it from expectancy math
6. so the auditor can never measure whether the rule has an edge
7. so the rule stays UNVALIDATED
8. so no rule may name a side      <- back to step 2
```

The gate is sound and I built it deliberately: a rule must not claim a
direction it has not earned. But the implementation makes earning one
**impossible**, because the only data that could earn it is excluded by
construction.

The sibling engine never had this problem — and that is why it shipped rules
that were silently inverted. It assigned directions from signal names and
*then* audited, which let it conclude things from bad labels.

## Why I did not just "assign a side and audit it"

That is the sibling engine's mistake, and it produced three fabricated
"significant losers" (BB Squeeze t=-4.05, Volume Dryup t=-3.94, Volume Climax
t=-2.15) that were entirely an artifact of treating `direction=0` as bearish.
Assigning a side first would rebuild the same trap with better plumbing.

## The fix: two ledgers, and the candidate ledger is never gated

Split the recording into two populations with different rules:

**Candidate ledger** (new) — every row where ANY rule fired `MET`.
- Gets a **provisional** side from the rule's own `direction_hint`.
- Enters expectancy math.
- Cannot suppress a signal, cannot produce an alert, cannot gate.
- Its only purpose is to answer "does rule 4 (flow agreement) have an edge?"

**Verdict ledger** (existing) — rows where a rule is `AUDITED`.
- Keeps `direction=0` until the auditor promotes that rule.
- Everything downstream (alerts, ranking, gating) reads only this.

The provisional side is a **hypothesis under test**, not a prediction. The
distinction is what makes it safe: a candidate row can be wrong and that is the
entire point, but it cannot act on the world.

### Direction hints are claims to be tested, not conclusions

`rules.json` gains an explicit `direction_hint` per rule, phrased as a testable
claim:

```json
"4": {
  "name": "Aggressor flow agreement",
  "direction_hint": {
    "side": "follow",
    "claim": "when >=3 venues agree on taker direction, the move follows the agreed flow",
    "testable": true
  }
}
```

A hint may be `"follow"`, `"fade"`, or `null`. **`null` is a legitimate and
honest answer** — a basis dislocation may simply have no directional edge, and
the auditor should be able to conclude exactly that.

Note rule 1's current hint is `null`: "one venue is dislocated" does not imply
the pair moves either way. That is a real limitation, not a gap to paper over.

## What the auditor then has to report

```
rule 4  n=412  mean=+0.00031  t=+2.14  n=412 >= 50, t >= 1.65
         -> keep, promote to AUDITED, side=follow
rule 1  n=380  mean=+0.00002  t=+0.11  hint was null; no directional claim
         -> directional claim NOT SUPPORTED; stays observation-only
```

Both outcomes are legitimate. The engine reports which one it found; it does not
decide in advance.

## Work required, in order

| # | Task | Why first |
|---|------|-----------|
| 1 | Outcome resolver: fetch the price N bars after the horizon, call `resolve()` | Nothing is measurable until rows resolve |
| 2 | `rules.json`: `direction_hint` per rule, incl. `null` where honest | Makes the hypothesis explicit |
| 3 | Recorder: `candidate` flag, separate from `direction` | Lets a hypothesis be tested without acting |
| 4 | Auditor: candidate rows enter math, verdict rows do not; report per-rule promotion | Decides what is actually earned |
| 5 | Promotion: on `keep`, flip that rule to `AUDITED` in `rules.json`, write the measured n/t/mean | Closes the loop |
| 6 | Gate reads only `AUDITED` rules | Preserves the sound gate |

Nothing in steps 3-6 can be evaluated honestly until step 1 exists. **Step 1 is
the whole immediate blocker**, and it is the smallest of the six.

## What must not happen

- Do not lower `min_n` or `min_t` to force a conclusion. `n >= 50`, `t <= -1.65`
  stay. Insufficient data is a valid result.
- Do not let a candidate row suppress, alert, or rank. It is measured, not acted
  upon.
- Do not assign a side from a signal's name. That is how the sibling engine's
  gate inverted.
- Do not promote on the first favourable read. Promotion requires the auditor's
  significance test on the candidate ledger.