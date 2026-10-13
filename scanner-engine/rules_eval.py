"""Rule evaluator — turns cross-venue features into a verdict.

THE THREE-STATE CONTRACT
========================
Every rule and every subcondition reports exactly one of:

    MET           the condition was evaluated and is true
    UNMET         the condition was evaluated and is false
    UNEVALUABLE   there was not enough data to ask the question

`UNEVALUABLE` is the state that matters. Collapsing it into `UNMET` is the
single most dangerous thing this module could do, because "the venues agree"
and "we could not check whether the venues agree" are different claims and only
one of them supports a verdict.

THE REGRESSION THAT FORCED THIS
==============================
Rules 3 and 4 require 3 venues. SPCX/USD and NVDA/USD list on 3 venues. An
earlier design evaluated a 3-venue condition on whatever venues happened to be
live, so a pair that lost Kraken would quietly be judged by 2 venues against a
3-venue rule and report a confident number. It now reports UNEVALUABLE with the
reason attached.

DIRECTION IS NOT GUESSED
========================
`direction` stays "neutral" unless a rule explicitly names a side. A basis
dislocation says a VENUE is apart from the others; it does not say the pair is
going up. Only an audited rule may contribute a side, and none has been, so
every verdict today is neutral by construction. `unknowns` records that.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from statistics import median, stdev

MET = "MET"
UNMET = "UNMET"
UNEVALUABLE = "UNEVALUABLE"


def load_rules(path: pathlib.Path | str | None = None) -> dict:
    p = pathlib.Path(path) if path else pathlib.Path(__file__).with_name("rules.json")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


# Floor used when no rule declares one. Both defaults are measured against what
# the venue APIs can actually deliver -- see `features.compute` for the table
# that motivated gating on trade count as well as span.
DEFAULT_MIN_COVERAGE_S = 5.0
DEFAULT_MIN_TRADES = 50


def coverage_floor(rules: dict) -> float:
    """The tape-coverage floor implied by rules.json, applied GLOBALLY.

    Rule 4 declares `min_coverage_s`. `features.compute` hardcoded 20.0 and
    `_rule4` never read its own declared value, so the config entry was
    decorative -- the exact failure this repo documents for thresholds that do
    not change behaviour.

    Taking the MAXIMUM declared value and applying it once in `compute` is the
    conservative reading: no rule can ever be handed a venue thinner than its
    own spec permits. Taking the minimum instead would silently relax rule 4;
    taking each rule's own value would require computing flow four times.
    """
    declared = []
    for spec in (rules.get("rules") or {}).values():
        v = (spec.get("thresholds") or {}).get("min_coverage_s")
        if isinstance(v, (int, float)):
            declared.append(float(v))
    return max(declared) if declared else DEFAULT_MIN_COVERAGE_S


def trade_floor(rules: dict) -> int:
    """The minimum trade count a venue's tape must carry, from rules.json.

    This is the gate that actually protects the delta_ratio's precision. A
    coverage floor alone cannot: Kraken returns four trades spanning 25s and
    would pass it, while a ratio from four trades is a coin-flip.
    """
    declared = []
    for spec in (rules.get("rules") or {}).values():
        v = (spec.get("thresholds") or {}).get("min_trades")
        if isinstance(v, int) and v > 0:
            declared.append(v)
    return max(declared) if declared else DEFAULT_MIN_TRADES


@dataclass
class Sub:
    id: str
    state: str
    detail: str = ""


@dataclass
class RuleResult:
    id: str
    name: str
    state: str
    requires_venues: int
    subconditions: dict[str, Sub] = field(default_factory=dict)
    reason: str = ""
    side: str | None = None          # only set by an audited rule; currently None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "requires_venues": self.requires_venues,
            "reason": self.reason,
            "side": self.side,
            "subconditions": {k: {"state": v.state, "detail": v.detail}
                              for k, v in self.subconditions.items()},
        }


@dataclass
class Verdict:
    pair: str
    ts: float
    n_venues: int
    min_venues: int
    evidence_ok: bool
    direction: str
    rules: dict[str, RuleResult]
    reduced_evidence: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    score: int = 0

    @property
    def evaluable_states(self) -> set[str]:
        return {r.state for r in self.rules.values()}

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "ts": self.ts,
            "n_venues": self.n_venues,
            "min_venues": self.min_venues,
            "evidence_ok": self.evidence_ok,
            "direction": self.direction,
            "score": self.score,
            "reduced_evidence": sorted(set(self.reduced_evidence)),
            "unknowns": self.unknowns,
            "rules": {k: v.to_dict() for k, v in sorted(self.rules.items())},
        }


# ── individual rules ───────────────────────────────────────────────────────

def _rule_state(sub_a: Sub, sub_b: Sub) -> str:
    """Fold two subconditions into one rule state, correctly.

    The ordering carries the meaning and is not interchangeable:

      * a UNMET primary means the rule is false. There is nothing left to ask,
        so the rule is UNMET even if the secondary could not be evaluated.
      * a UNMET secondary over a MET primary is a genuine UNMET — we asked and
        the answer was no.
      * an UNEVALUABLE subcondition makes the whole rule UNEVALUABLE. "We could
        not check" must never be reported as "no".

    Getting this wrong in the permissive direction is how a rule that was never
    asked gets graded; getting it wrong in the strict direction is how a real
    signal is suppressed by a missing feed.
    """
    if sub_a.state == UNMET:
        return UNMET
    if sub_b.state == UNEVALUABLE:
        return UNEVALUABLE
    if sub_a.state == MET and sub_b.state == MET:
        return MET
    return UNMET


def _rule1(spec, f, history) -> RuleResult:
    """Cross-venue basis dislocation.

    (a) some venue's z exceeds the threshold
    (b) it has held for persist_cycles consecutive cycles
    """
    thr = spec["thresholds"]["z_threshold"]
    need = spec["thresholds"]["persist_cycles"]

    if not f.basis_z:
        return RuleResult("1", spec["name"], UNEVALUABLE, spec["requires_venues"],
                          reason="no venue basis computed")

    z = f.z_max()
    sub_a = Sub("a", MET if z is not None and abs(z) >= thr else UNMET,
                f"z_max={z:.2f} threshold={thr}" if z is not None else "no z")

    h = (history or {}).get("1", {})
    cycles = h.get("cycles", 0)
    was_big = abs(h.get("z_max") or 0.0) >= thr
    sub_b = (Sub("b", MET, f"{cycles} consecutive cycles >= threshold")
             if was_big and cycles >= need else
             Sub("b", UNMET, f"{cycles} cycles, need {need}; prev z={h.get('z_max') or 0.0:.2f}"))

    state = MET if (sub_a.state == MET and sub_b.state == MET) else UNMET
    return RuleResult("1", spec["name"], state, spec["requires_venues"],
                      {"a": sub_a, "b": sub_b})


def _rule2(spec, f, history) -> RuleResult:
    """Single-venue flow dominance.

    (a) one venue's flow ratio deviates from the cross-venue median
    (b) price did NOT respond — absorption

    Condition (b) was previously a hard-coded `UNEVALUABLE` with the note
    "price-response window not recorded yet", and the rule then returned
    `state = UNEVALUABLE` unconditionally. That made rule 2 STRUCTURALLY
    impossible to fire: the engine shipped four rules and could measure one.
    The response is now measured — `features.price_move_bps` carries the move
    over each venue's own flow window — so the condition can be asked.
    """
    thr = spec["thresholds"]["flow_gap"]
    # How far the deviating venue's price may move and still count as "not
    # responding". Read from the spec so the rule is tunable from rules.json;
    # it used to be hard-coded, which is how a config value ends up decorative.
    resp = spec["thresholds"].get("max_price_response_bps", 5.0)

    if f.n_venues_with_tape < 2:
        return RuleResult("2", spec["name"], UNEVALUABLE, spec["requires_venues"],
                          reason=f"only {f.n_venues_with_tape} venue(s) with usable tape")
    if f.flow_median is None:
        return RuleResult("2", spec["name"], UNEVALUABLE, spec["requires_venues"],
                          reason="no flow median")

    # `features.compute` precomputes the deviating venue, but the rule derives
    # it if absent rather than trusting an optional convenience field. A rule
    # that silently reads None as 0.0 would report "no venue deviated" for a
    # snapshot that plainly shows one at 0.95 vs a 0.865 median.
    worst_v = f.flow_deviator
    worst_dev = f.flow_deviation
    if worst_dev is None or worst_v is None:
        worst_v, worst_dev = None, 0.0
        for v, r in (f.flow_by_venue or {}).items():
            dev = abs(r - f.flow_median)
            if dev > worst_dev:
                worst_v, worst_dev = v, dev

    sub_a = Sub("a", MET if worst_dev >= thr else UNMET,
                f"{worst_v} deviates {worst_dev:.3f} from median {f.flow_median:.3f} "
                f"(threshold {thr})")

    moved = f.price_move_bps.get(worst_v) if worst_v else None
    if moved is None:
        # No price alongside this venue's flow. That is "we could not check",
        # which is UNEVALUABLE and must not be folded into UNMET.
        sub_b = Sub("b", UNEVALUABLE,
                    f"no price response recorded for {worst_v!r}")
    else:
        sub_b = Sub("b", MET if abs(moved) < resp else UNMET,
                    f"{worst_v} price moved {moved:+.2f}bps over the window; "
                    f"abs must stay under {resp}bps for absorption")

    state = _rule_state(sub_a, sub_b)
    return RuleResult("2", spec["name"], state, spec["requires_venues"],
                      {"a": sub_a, "b": sub_b})


def _rule3(spec, f, history) -> RuleResult:
    """Cross-venue liquidity vacuum.

    (a) touch imbalance beyond the band on >= vac_venues venues
    (b) top-N depth below its own trailing median for the pair
    """
    band = spec["thresholds"]["vac_band"]
    need_n = spec["thresholds"]["vac_venues"]

    usable = {v: x for v, x in f.touch_imbalance.items() if x is not None}
    if not usable:
        return RuleResult("3", spec["name"], UNEVALUABLE, spec["requires_venues"],
                          reason="no touch imbalance available (books carry no size)")

    side_buy = sum(1 for x in usable.values() if x > 0.5 + band)
    side_sell = sum(1 for x in usable.values() if x < 0.5 - band)
    hit = max(side_buy, side_sell)
    sub_a = Sub("a", MET if hit >= need_n else UNMET,
                f"{hit} venue(s) beyond +/-{band} (need {need_n}); "
                f"buy={side_buy} sell={side_sell} of {len(usable)}")

    h = (history or {}).get("3", {})
    med = h.get("depth_median")
    hist = h.get("depth_history") or []

    # The comparison is TODAY's depth against the trailing baseline of PRIOR
    # cycles. Reading the current value out of `history` instead would compare
    # the last observation against a median that already contains it, which
    # always leans positive and can never detect a departure.
    now = sum(f.depth_total.values()) if f.depth_total else None

    if not med:
        sub_b = Sub("b", UNEVALUABLE, "no trailing depth median recorded for this pair")
    elif now is None:
        sub_b = Sub("b", UNEVALUABLE, "current cycle carries no depth to compare")
    elif len(hist) < MIN_DEPTH_POINTS:
        sub_b = Sub("b", UNEVALUABLE,
                    f"only {len(hist)} of {MIN_DEPTH_POINTS} cycles of depth "
                    f"history — a trailing median needs a usable dispersion")
    else:
        sd = stdev(hist)
        z = (now - med) / sd if sd > 0 else None
        need_z = spec["thresholds"].get("depth_z", -1.5)
        if z is None:
            sub_b = Sub("b", UNEVALUABLE, "depth distribution is degenerate")
        else:
            sub_b = Sub("b", MET if z <= need_z else UNMET,
                        f"depth z={z:.2f} vs this pair's own trailing median "
                        f"(needs <= {need_z})")

    state = _rule_state(sub_a, sub_b)
    return RuleResult("3", spec["name"], state, spec["requires_venues"],
                      {"a": sub_a, "b": sub_b})


def _rule4(spec, f, history) -> RuleResult:
    """Aggressor flow agreement across venues."""
    need_n = spec["thresholds"]["agree_venues"]
    strength = spec["thresholds"]["flow_strength"]

    if f.n_venues_with_tape < need_n:
        return RuleResult("4", spec["name"], UNEVALUABLE, spec["requires_venues"],
                          reason=f"only {f.n_venues_with_tape} venue(s) with usable tape, "
                                 f"need {need_n}")

    vals = [r for r in f.flow_by_venue.values() if r is not None]
    if not vals:
        return RuleResult("4", spec["name"], UNEVALUABLE, spec["requires_venues"],
                          reason="no usable flow ratios")

    buys = [r for r in vals if r >= 0.5]
    sells = [r for r in vals if r < 0.5]
    major = buys if len(buys) >= len(sells) else sells

    if not major:
        return RuleResult("4", spec["name"], UNMET, spec["requires_venues"],
                          reason="no venue reported a usable aggressor ratio")
    if len(major) < need_n:
        sub_a = Sub("a", UNMET, f"largest agreeing side has {len(major)}, need {need_n}")
    else:
        sub_a = Sub("a", MET, f"{len(major)} venue(s) agree")

    # Upper-middle element, not an interpolated median — the SAME convention
    # features.compute uses for its median centre (see the CONVENTION note
    # there). Consistency matters: flow_strength in rules.json is calibrated
    # against this reading, and scanner._flow_by_rule derives the provisional
    # side from the same shape of statistic.
    med = sorted(major)[len(major) // 2]
    sub_b = Sub("b", MET if abs(med - 0.5) >= strength else UNMET,
                f"median ratio {med:.3f}, needs |x-0.5| >= {strength}")

    state = MET if (sub_a.state == MET and sub_b.state == MET) else UNMET
    return RuleResult("4", spec["name"], state, spec["requires_venues"],
                      {"a": sub_a, "b": sub_b})


_DISPATCH = {"1": _rule1, "2": _rule2, "3": _rule3, "4": _rule4}


# ── cross-cycle memory ──────────────────────────────────────────────────────

# A trailing median over fewer points than this is not a baseline: the spread
# is too small to be meaningful, and a z-score against it would be dominated
# by rounding noise.
MIN_DEPTH_POINTS = 20


class RuleHistory:
    """Per-pair memory the rule evaluators need but a snapshot cannot carry.

    THREE OF THE FOUR RULES WERE PERMANENTLY UNFIRABLE WITHOUT THIS, and the
    engine did not say so: it reported "insufficient data" while actually
    having no code path to MET.

      rule 1  condition (b) asks whether a dislocation PERSISTED for N cycles.
      rule 3  condition (b) asks whether depth FELL BELOW ITS OWN trailing
              median for this pair.
      rule 2  condition (b) asks whether price did NOT RESPOND to the flow.

    `evaluate()` stays a pure function of (features, history) so it remains
    testable; this object owns the state and the loop advances it.

    THE ORDER MATTERS and is enforced by the call sequence in the scanner:

        snap = history.snapshot(pair)   # state as of the END of last cycle
        verdict = evaluate(feat, pair, min_venues, rules, history=snap)
        history.observe(pair, feat)     # advance memory with THIS cycle

    `snapshot()` must be taken BEFORE `observe()`, because condition (b) of
    rule 1 is about what happened on the PREVIOUS cycle. Observing first would
    make every rule fire one cycle early.
    """

    def __init__(self, depth_window: int = 60):
        self.depth_window = depth_window
        # pair -> list[float] of total top-N depth, most recent last
        self._depths: dict[str, list[float]] = {}
        # (pair, z_threshold) -> consecutive cycles at or over the threshold
        self._z_streak: dict[tuple, int] = {}
        # pair -> last observed z_max, used by rule 1's persistence test
        self._last_z: dict[str, float] = {}
        # pair -> {"z_max": float|None, "cycles": int}
        self._state: dict[str, dict] = {}

    def snapshot(self, pair: str) -> dict:
        """The `history` argument for `evaluate()`, as of the last cycle."""
        return self._state.get(pair, {})

    def observe(self, pair: str, feat, z_threshold: float = 6.0) -> None:
        """Fold this cycle's features into the running state."""
        z = feat.z_max()
        key = (pair, z_threshold)
        if z is not None and abs(z) >= z_threshold:
            self._z_streak[key] = self._z_streak.get(key, 0) + 1
        else:
            self._z_streak[key] = 0

        depths = self._depths.setdefault(pair, [])
        if feat.depth_total:
            # Sum across venues: rule 3 asks whether the PAIR's liquidity left,
            # not whether one venue's book shrank while another's grew.
            depths.append(sum(feat.depth_total.values()))
            if len(depths) > self.depth_window:
                del depths[:-self.depth_window]

        self._last_z[pair] = z
        self._state[pair] = {
            "1": {"z_max": z, "cycles": self._z_streak.get(key, 0)},
            "3": {
                "depth_median": median(depths) if depths else None,
                "depth_now": depths[-1] if depths else None,
                "depth_history": list(depths),
                "depth_n": len(depths),
            },
        }

    def depth_median(self, pair: str) -> float | None:
        """This pair's trailing median top-N depth, or None before any cycle."""
        st = self._state.get(pair, {}).get("3")
        return st.get("depth_median") if st else None

    def depth_z(self, pair: str, current: float | None = None) -> float | None:
        """`current` depth vs this pair's own trailing distribution.

        `current` is supplied by the caller because the comparison must be
        against a baseline of PRIOR cycles. Reading it back out of the stored
        state would put the current observation inside its own median.

        None until there is enough history for the spread to mean anything.
        Fewer than `MIN_DEPTH_POINTS` observations has no usable dispersion, and
        dividing by a near-zero stdev would manufacture an enormous z from
        rounding noise.
        """
        st = self._state.get(pair, {}).get("3")
        if not st or not st.get("depth_history"):
            return None
        hist = st["depth_history"]
        if len(hist) < MIN_DEPTH_POINTS:
            return None
        if current is None:
            current = st.get("depth_now")
        if current is None:
            return None
        med = st.get("depth_median")
        sd = stdev(hist)
        if not med or sd <= 0:
            return None
        return (current - med) / sd


# ── the evaluator ──────────────────────────────────────────────────────────

def evaluate(feat, pair: str, min_venues: int, rules: dict,
             history: dict | None = None, ts: float | None = None) -> Verdict:
    import time
    ts = ts if ts is not None else time.time()

    reduced: list[str] = list(getattr(feat, "missing_venue_data", []))
    reduced += [f"{v}:thin_tape_coverage" for v in getattr(feat, "thin_coverage", [])]

    evidence_ok = feat.n_venues >= min_venues
    if not evidence_ok:
        reduced.append(f"venues_live={feat.n_venues}<{min_venues}")

    results: dict[str, RuleResult] = {}
    for rid, spec in rules["rules"].items():
        need = spec.get("requires_venues", 2)
        fn = _DISPATCH.get(rid)
        if fn is None:
            results[rid] = RuleResult(rid, spec.get("name", rid), UNEVALUABLE, need,
                                      reason="no evaluator implemented")
            continue
        if feat.n_venues < need:
            results[rid] = RuleResult(
                rid, spec.get("name", rid), UNEVALUABLE, need,
                reason=f"requires {need} live venue(s), {feat.n_venues} available")
            continue
        results[rid] = fn(spec, feat, history)

    met = [r for r in results.values() if r.state == MET]

    unknowns: list[str] = []
    if not met:
        unknowns.append("side")
    for r in met:
        if r.side is None:
            unknowns.append(f"rule{r.id}_side")

    # Direction stays neutral until an audited rule names a side. No rule has
    # been audited (all are UNVALIDATED in rules.json), so this is neutral by
    # construction today, not by accident.
    direction = "neutral"
    sides = {r.side for r in met if r.side}
    if len(sides) == 1 and None not in sides:
        direction = next(iter(sides))

    return Verdict(
        pair=pair, ts=ts, n_venues=feat.n_venues, min_venues=min_venues,
        evidence_ok=evidence_ok, direction=direction, rules=results,
        reduced_evidence=reduced, unknowns=sorted(set(unknowns)), score=len(met) * 10,
    )


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from features import CrossVenueFeatures
    r = load_rules()
    print(json.dumps(evaluate(CrossVenueFeatures(), "DEMO/USD", 3, r).to_dict(),
                     indent=2))