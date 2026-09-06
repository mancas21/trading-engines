"""
signal_engine.py — Deterministic signal computation from rules.json.

Pure function (no I/O, no LLM, no randomness). Same input → same output.
Every caller routes through compute_signal() — the single source of truth.
"""

from dataclasses import dataclass, field
from typing import Any
import json
import math
import os


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    """Typed input snapshot for signal computation. All fields required."""
    ticker: str
    price: float
    delta: float
    volume_total: float
    volume_bid: float
    volume_ask: float

    # Footprint
    footprint_levels: list[dict] = field(default_factory=list)
    poc: float = 0.0
    value_area_high: float = 0.0
    value_area_low: float = 0.0

    # DOM
    dom_imbalance_pct: float = 0.0
    cumulative_bid_vol: float = 0.0
    cumulative_ask_vol: float = 0.0

    # Tape
    tape_buy_vol: float = 0.0
    tape_sell_vol: float = 0.0
    tape_buy_count: int = 0
    tape_sell_count: int = 0
    tape_buy_count_pct: float = 50.0
    tape_largest_trade: float = 0.0
    tape_largest_trade_side: str = "?"

    # CVD (session cumulative delta from bridge tracker)
    cvd: float = 0.0
    cvd_bars: int = 0
    cvd_bar_deltas: list[float] = field(default_factory=list)
    cvd_continuity: str = "clean"   # "clean" | "post-gap" | "unknown"


@dataclass
class SignalResult:
    """Deterministic output from compute_signal()."""
    bias_score: float
    bias_label: str  # "bullish" / "bearish" / "neutral"
    sub_scores: dict[str, float]
    trade_plan_allowed: bool
    rule_2_exhaustion: bool
    rule_6_active: bool
    rule_7_active: bool
    data_coverage: float = 1.0     # 0-1: fraction of scoring weight backed by real data
    plan_blocked_reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Load rules.json weights
# ---------------------------------------------------------------------------

_RULES_PATH = os.path.join(os.path.dirname(__file__), "rules.json")


def _load_weights() -> dict:
    try:
        with open(_RULES_PATH, "r") as f:
            rules = json.load(f)
        return rules.get("scoring_weights", {
            "footprint_depth": 25, "dom_imbalance": 25,
            "tape_ratio": 20, "volume_climax": 15, "footprint_confirmation": 15
        })
    except Exception:
        return {"footprint_depth": 25, "dom_imbalance": 25,
                "tape_ratio": 20, "volume_climax": 15, "footprint_confirmation": 15}


# ---------------------------------------------------------------------------
# Sub-scores (each 0-10)
# ---------------------------------------------------------------------------

def _score_footprint_depth(s: Snapshot) -> float:
    """Score footprint: ratio of buying vs selling levels."""
    if not s.footprint_levels:
        return 5.0
    buy_levels = sum(1 for lv in s.footprint_levels if lv.get("delta", 0) > 0)
    sell_levels = sum(1 for lv in s.footprint_levels if lv.get("delta", 0) < 0)
    total = buy_levels + sell_levels
    if total == 0:
        return 5.0
    ratio = buy_levels / total
    # 0.0 → score 0, 0.5 → 5, 1.0 → 10
    return ratio * 10.0


def _score_dom_imbalance(s: Snapshot) -> float:
    """Score DOM imbalance: centered at 5, soft-saturated via sigmoid so the
    full observed crypto range (±40%..±75%) keeps discriminating instead of
    clipping at ±40 (audit C3). imb=0 -> 5, asymptotes 0/10 at extreme tails."""
    imb = s.dom_imbalance_pct
    # scale: 1.0 at imb≈±75 maps to ~8.8/1.2; 10/0 only in pathological tails
    try:
        x = imb / 40.0
        score = 5.0 + 5.0 * (2.0 / (1.0 + math.exp(-x)) - 1.0)
    except Exception:  # noqa: BLE001
        return 5.0
    return max(0.0, min(10.0, round(score, 2)))


def _score_tape_ratio(s: Snapshot) -> float:
    """Volume-weighted tape ratio: 70% USD volume + 30% trade count."""
    total_vol = s.tape_buy_vol + s.tape_sell_vol
    total_cnt = s.tape_buy_count + s.tape_sell_count
    vol_ratio = (s.tape_buy_vol / total_vol) if total_vol > 0 else 0.5
    cnt_ratio = (s.tape_buy_count / total_cnt) if total_cnt > 0 else 0.5
    combined = (0.70 * vol_ratio) + (0.30 * cnt_ratio)
    return round(combined * 10.0, 2)


def _score_volume_climax(s: Snapshot) -> float:
    """SIGNED climax score (audit C1): strong one-sided flow is directional —
    heavy buying raises the score, heavy selling lowers it. Previously the
    score used |delta|/volume so a huge sell bar read as bullish flow.

    ratio = delta/volume in [-1, 1]:
      +1.0 (all buying)  -> 10.0
       0.0 (absorbed)    ->  5.0  (no directional edge — exhaustion/absorption)
      -1.0 (all selling) ->  0.0
    """
    if s.volume_total <= 0 or s.delta == 0:
        return 5.0
    ratio = s.delta / s.volume_total
    ratio = max(-1.0, min(1.0, ratio))
    return round(5.0 + 5.0 * math.tanh(ratio / 0.25), 2)


def _score_footprint_confirmation(s: Snapshot) -> float:
    """Score footprint confirmation: 2+ levels with volume = pattern confirmed."""
    if s.footprint_levels and len(s.footprint_levels) >= 2:
        return 10.0  # confirmed
    return 5.0  # insufficient data


# ---------------------------------------------------------------------------
# Rule checks
# ---------------------------------------------------------------------------

def _cvd_consecutive(deltas: list[float]) -> tuple[int, int]:
    """Count consecutive same-sign deltas from the end. Returns (consec_up, consec_down)."""
    up = down = 0
    for d in reversed(deltas):
        if d > 0:
            if down:
                break
            up += 1
        elif d < 0:
            if up:
                break
            down += 1
        else:
            break
    return up, down


def _check_rule_2_exhaustion(s: Snapshot) -> bool:
    """Rule 2: one-bar CVD spike >2x prior max 1-bar spike = exhaustion, not continuation.
    Only evaluated on a clean CVD series (post-gap deltas are unreliable)."""
    if len(s.cvd_bar_deltas) < 4:
        return False
    if s.cvd_continuity == "post-gap":
        return False
    last = abs(s.cvd_bar_deltas[-1])
    prior_max = max(abs(d) for d in s.cvd_bar_deltas[:-1])
    return prior_max > 0 and last > 2.0 * prior_max


def _check_rule_6_distribution(s: Snapshot) -> bool:
    """Rule 6: ALL of: (a) CVD drops 2+ consecutive bars AND (b) 4 levels 70%+ selling AND (c) DOM <+5%.
    Only on a clean CVD series (audit C2/H3: post-gap deltas must not fire hard rules)."""
    if s.cvd_continuity == "post-gap":
        return False
    _, consec_down = _cvd_consecutive(s.cvd_bar_deltas)
    if consec_down < 2:
        return False
    levels = s.footprint_levels
    if len(levels) < 4:
        return False
    sell_dominated = 0
    for lv in levels:
        bid_vol = lv.get("bid", 0)
        ask_vol = lv.get("ask", 0)
        total_level_vol = bid_vol + ask_vol
        if total_level_vol > 0 and bid_vol / total_level_vol >= 0.70:
            sell_dominated += 1
    if sell_dominated < 4:
        return False
    if s.dom_imbalance_pct >= 5:
        return False
    return True


def _check_rule_7_accumulation(s: Snapshot) -> bool:
    """Rule 7: ALL of: (a) CVD rises 2+ consecutive bars AND (b) 3 levels 70%+ buying AND (c) DOM >+10%.
    Only on a clean CVD series."""
    if s.cvd_continuity == "post-gap":
        return False
    consec_up, _ = _cvd_consecutive(s.cvd_bar_deltas)
    if consec_up < 2:
        return False
    levels = s.footprint_levels
    if len(levels) < 3:
        return False
    buy_dominated = 0
    for lv in levels:
        bid_vol = lv.get("bid", 0)
        ask_vol = lv.get("ask", 0)
        total_level_vol = bid_vol + ask_vol
        if total_level_vol > 0 and ask_vol / total_level_vol >= 0.70:
            buy_dominated += 1
    if buy_dominated < 3:
        return False
    if s.dom_imbalance_pct <= 10:
        return False
    return True


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def _data_coverage(s: Snapshot, weights: dict) -> float:
    """Fraction of total scoring weight backed by REAL data (audit C4).

    Missing footprint/tape/volume currently fall back to neutral 5.0, which
    makes the weighted score treat "no data" as "balanced data" — that lets a
    single DOM reading manufacture a plan. Coverage < 0.5 blocks plans and is
    reported on every SignalResult."""
    real = 0.0
    total = 0.0
    for k, w in weights.items():
        total += w
        if k == "footprint_depth" and s.footprint_levels:
            real += w
        elif k == "footprint_confirmation" and len(s.footprint_levels) >= 2:
            real += w
        elif k == "dom_imbalance" and (s.cumulative_bid_vol > 0 or s.cumulative_ask_vol > 0
                                       or s.dom_imbalance_pct != 0.0):
            real += w
        elif k == "tape_ratio" and (s.tape_buy_vol > 0 or s.tape_sell_vol > 0
                                    or s.tape_buy_count > 0 or s.tape_sell_count > 0):
            real += w
        elif k == "volume_climax" and s.volume_total > 0:
            real += w
    return (real / total) if total > 0 else 0.0


def compute_signal(snapshot: Snapshot) -> SignalResult:
    """Deterministic signal computation. Pure function, no side effects."""
    weights = _load_weights()

    sub_scores = {
        "footprint_depth": _score_footprint_depth(snapshot),
        "dom_imbalance": _score_dom_imbalance(snapshot),
        "tape_ratio": _score_tape_ratio(snapshot),
        "volume_climax": _score_volume_climax(snapshot),
        "footprint_confirmation": _score_footprint_confirmation(snapshot),
    }

    # Weighted sum — sub-scores are 0-10, weights sum to ~100.
    # Normalize to 0-100 so Rule 4 thresholds (>=60 bullish, <=40 bearish) apply.
    total_weight = sum(weights.values())
    if total_weight == 0:
        bias_score = 50.0
    else:
        bias_score = sum(
            sub_scores[k] * weights.get(k, 5) for k in sub_scores
        ) / total_weight * 10.0

    # Bias label (Rule 4)
    if bias_score >= 60:
        bias_label = "bullish"
    elif bias_score <= 40:
        bias_label = "bearish"
    else:
        bias_label = "neutral"

    # Rule checks
    rule_2_exhaustion = _check_rule_2_exhaustion(snapshot)
    rule_6_active = _check_rule_6_distribution(snapshot)
    rule_7_active = _check_rule_7_accumulation(snapshot)

    coverage = _data_coverage(snapshot, weights)
    reasons: list[str] = []

    # Rule 10: no trade plan in neutral zone
    trade_plan_allowed = bias_score >= 60 or bias_score <= 40
    if trade_plan_allowed:
        # Rule 2 (hard): exhaustion spike is NOT a continuation call (audit C2).
        if rule_2_exhaustion:
            trade_plan_allowed = False
            reasons.append("rule_2_exhaustion_veto")
        # Rule 6 (hard): confirmed distribution contradicts a bullish plan.
        if rule_6_active and bias_label == "bullish":
            trade_plan_allowed = False
            reasons.append("rule_6_distribution_contradicts_long")
        # Rule 7 (hard): confirmed accumulation contradicts a bearish plan.
        if rule_7_active and bias_label == "bearish":
            trade_plan_allowed = False
            reasons.append("rule_7_accumulation_contradicts_short")
        # C4: no phantom plans from single-source / missing-data scores.
        if coverage < 0.5:
            trade_plan_allowed = False
            reasons.append(f"low_data_coverage_{coverage:.2f}")

    return SignalResult(
        bias_score=round(bias_score, 1),
        bias_label=bias_label,
        sub_scores=sub_scores,
        trade_plan_allowed=trade_plan_allowed,
        rule_2_exhaustion=rule_2_exhaustion,
        rule_6_active=rule_6_active,
        rule_7_active=rule_7_active,
        data_coverage=round(coverage, 2),
        plan_blocked_reasons=reasons,
    )


def build_snapshot_from_live(live: dict, cvd: dict | None = None) -> Snapshot | None:
    """Build a Snapshot from LiveExporter dict + optional bridge CVD state."""
    if not live:
        return None
    price = live.get("price", {})
    vol = live.get("volume", {})
    fp = live.get("footprint", {})
    dom = live.get("dom", {})
    tape = live.get("tape", {})
    cvd = cvd or {}

    return Snapshot(
        ticker=live.get("ticker", "?"),
        price=price.get("last", 0),
        delta=live.get("delta", 0),
        volume_total=vol.get("total", 0),
        volume_bid=vol.get("bid", 0),
        volume_ask=vol.get("ask", 0),
        footprint_levels=fp.get("top_levels", []),
        poc=fp.get("poc", 0),
        value_area_high=fp.get("value_area_high", 0),
        value_area_low=fp.get("value_area_low", 0),
        dom_imbalance_pct=dom.get("imbalance_pct", 0),
        cumulative_bid_vol=dom.get("cumulative_bid_vol", 0),
        cumulative_ask_vol=dom.get("cumulative_ask_vol", 0),
        tape_buy_vol=tape.get("buy_vol", 0),
        tape_sell_vol=tape.get("sell_vol", 0),
        tape_buy_count=tape.get("buy_count", 0),
        tape_sell_count=tape.get("sell_count", 0),
        tape_buy_count_pct=tape.get("buy_count_pct", 50),
        tape_largest_trade=tape.get("largest_trade", 0),
        tape_largest_trade_side=tape.get("largest_trade_side", "?"),
        cvd=cvd.get("cvd", 0.0),
        cvd_bars=cvd.get("bars", 0),
        cvd_bar_deltas=list(cvd.get("bar_deltas", [])),
        cvd_continuity=cvd.get("continuity", "clean"),
    )
