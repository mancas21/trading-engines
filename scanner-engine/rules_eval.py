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

MET = "MET"
UNMET = "UNMET"
UNEVALUABLE = "UNEVALUABLE"


def load_rules(path: pathlib.Path | str | None = None) -> dict:
    p = pathlib.Path(path) if path else pathlib.Path(__file__).with_name("rules.json")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


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
    was_big = abs(h.get("z_max", 0.0)) >= thr
    sub_b = (Sub("b", MET, f"{cycles} consecutive cycles >= threshold")
             if was_big and cycles >= need else
             Sub("b", UNMET, f"{cycles} cycles, need {need}; prev z={h.get('z_max', 0.0):.2f}"))

    state = MET if (sub_a.state == MET and sub_b.state == MET) else UNMET
    return RuleResult("1", spec["name"], state, spec["requires_venues"],
                      {"a": sub_a, "b": sub_b})


def _rule2(spec, f, history) -> RuleResult:
    """Single-venue flow dominance.

    (a) one venue's flow ratio deviates from the cross-venue median
    (b) price did NOT respond — absorption
    """
    thr = spec["thresholds"]["flow_gap"]

    if f.n_venues_with_tape < 2:
        return RuleResult("2", spec["name"], UNEVALUABLE, spec["requires_venues"],
                          reason=f"only {f.n_venues_with_tape} venue(s) with usable tape")
    if f.flow_median is None:
        return RuleResult("2", spec["name"], UNEVALUABLE, spec["requires_venues"],
                          reason="no flow median")

    worst_v, worst_dev = None, 0.0
    for v, r in f.flow_by_venue.items():
        dev = abs(r - f.flow_median)
        if dev > worst_dev:
            worst_v, worst_dev = v, dev

    sub_a = Sub("a", MET if worst_dev >= thr else UNMET,
                f"{worst_v} deviates {worst_dev:.3f} from median {f.flow_median:.3f} "
                f"(threshold {thr})")

    # (b) needs the deviation venue's own price move over the window, which the
    # current snapshot does not carry. Reported UNEVALUABLE, never assumed.
    sub_b = Sub("b", UNEVALUABLE, "price-response window not recorded yet")

    state = UNEVALUABLE      # a required subcondition cannot be asked
    return RuleResult("2", spec["name"], state, spec["requires_venues"],
                      {"a": sub_a, "b": sub_b},
                      reason="condition (b) requires a recorded price-response window")


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
    if not h.get("depth_median"):
        sub_b = Sub("b", UNEVALUABLE, "no trailing depth median recorded for this pair")
    else:
        sub_b = Sub("b", UNEVALUABLE, "depth z-score needs multiple cycles of history")

    state = MET if (sub_a.state == MET and sub_b.state == MET) else (
        UNMET if sub_a.state == UNMET else UNEVALUABLE)
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

    med = sorted(major)[len(major) // 2]
    sub_b = Sub("b", MET if abs(med - 0.5) >= strength else UNMET,
                f"median ratio {med:.3f}, needs |x-0.5| >= {strength}")

    state = MET if (sub_a.state == MET and sub_b.state == MET) else UNMET
    return RuleResult("4", spec["name"], state, spec["requires_venues"],
                      {"a": sub_a, "b": sub_b})


_DISPATCH = {"1": _rule1, "2": _rule2, "3": _rule3, "4": _rule4}


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