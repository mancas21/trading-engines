"""
engine.core — shared data structures used across every layer of the engine.

This module is the foundation the spec's Section 12 (Data Quality and
Confidence) depends on. Every statistical result produced anywhere in the
engine should be wrapped in a StatResult so N, lookback, and confidence
travel with the number instead of being reported separately (or forgotten).

Design principle: a probability or score without its sample size is not
a usable output. Downstream layers (confluence, expectancy, scenario)
should refuse to escalate a StatResult's confidence level above what its
N actually supports — they can only inherit or downgrade confidence, never
upgrade it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any


class Confidence(str, Enum):
    """
    Confidence bands driven by sample size, not by how "clean" a signal
    looks. Thresholds are deliberately conservative for intraday crypto
    data where regime shifts mean older samples decay in relevance.

    LOW:    N < 30   — do not act on this alone; context/color only
    MEDIUM: 30-99     — directionally useful, size down
    HIGH:   100+      — usable for sizing decisions, still not certainty
    """
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @staticmethod
    def from_n(n: int) -> "Confidence":
        if n < 30:
            return Confidence.LOW
        if n < 100:
            return Confidence.MEDIUM
        return Confidence.HIGH


@dataclass
class StatResult:
    """
    Universal wrapper for any statistic (probability, mean, ratio, score)
    produced anywhere in the engine.

    value             — the statistic itself
    n                 — sample size backing this specific statistic
    lookback          — human-readable description of the data window used
                         (e.g. "30 days, 15m bars" or "since 2026-07-11")
    confidence        — derived from n via Confidence.from_n, unless
                         explicitly overridden with a documented reason
    confidence_interval — optional (lo, hi) tuple, e.g. Wilson score
                         interval for a proportion
    method            — short string naming how it was computed, for
                         auditability (e.g. "wilson_95", "bootstrap_1000")
    warnings          — list of caveats to surface to the user, e.g.
                         "N below recommended minimum for tail estimates"
    """
    value: float
    n: int
    lookback: str
    confidence: Confidence = field(init=False)
    confidence_interval: Optional[tuple] = None
    p90: Optional[float] = None        # audit M4: explicit tail percentile (e.g. expected-move p90)
    method: Optional[str] = None
    warnings: list = field(default_factory=list)

    def __post_init__(self):
        self.confidence = Confidence.from_n(self.n)
        if self.n < 30:
            self.warnings.append(
                f"N={self.n} is below the reliable threshold (30) — "
                "treat as directional color, not a tradeable edge."
            )

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "n": self.n,
            "lookback": self.lookback,
            "confidence": self.confidence.value,
            "confidence_interval": self.confidence_interval,
            "p90": self.p90,
            "method": self.method,
            "warnings": self.warnings,
        }


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple:
    """
    Wilson score interval for a binomial proportion — better behaved than
    the normal approximation at small N and near 0/1, which matters here
    since setup-conditional samples are often small.

    Returns (lo, hi) as proportions in [0, 1].
    """
    if n == 0:
        return (0.0, 1.0)
    phat = successes / n
    denom = 1 + z ** 2 / n
    centre = phat + z ** 2 / (2 * n)
    adj = z * math.sqrt((phat * (1 - phat) + z ** 2 / (4 * n)) / n)
    lo = (centre - adj) / denom
    hi = (centre + adj) / denom
    return (max(0.0, lo), min(1.0, hi))


def conditional_probability(
    condition_mask,
    outcome_mask,
    lookback_desc: str,
) -> StatResult:
    """
    Compute P(outcome | condition) with a Wilson confidence interval.

    condition_mask, outcome_mask — boolean arrays/Series of equal length,
    aligned to the same index (e.g. same bar timestamps).

    This is the base primitive Section 7 (Conditional Probability Engine)
    is built on. Every conditional stat in the engine — "P(+2% | bullish
    regime + NY + above POC + positive CVD)" — should route through this
    function rather than being computed ad hoc, so N and CI reporting are
    consistent everywhere.
    """
    import numpy as np

    condition_mask = np.asarray(condition_mask, dtype=bool)
    outcome_mask = np.asarray(outcome_mask, dtype=bool)
    n = int(condition_mask.sum())
    if n == 0:
        return StatResult(value=float("nan"), n=0, lookback=lookback_desc,
                           method="wilson_95",
                           warnings=["Condition never occurred in the sample — no basis for a probability."])
    successes = int((condition_mask & outcome_mask).sum())
    p = successes / n
    ci = wilson_interval(successes, n)
    return StatResult(value=p, n=n, lookback=lookback_desc,
                       confidence_interval=ci, method="wilson_95")


@dataclass
class TimeframeSnapshot:
    """
    A single timeframe's read on trend/regime, as produced by
    engine.regime for one timeframe. Used by the multi-timeframe
    alignment logic (Section 1).
    """
    timeframe: str  # e.g. "15m", "1h", "4h"
    trend: str      # "bullish" | "bearish" | "neutral"
    trend_strength: float  # 0-1, e.g. normalized slope or structure score
    as_of: str      # timestamp of the last bar used


@dataclass
class MTFContext:
    """
    Aggregated multi-timeframe read, output of engine.regime.mtf_alignment.
    """
    snapshots: list  # list[TimeframeSnapshot]
    alignment_score: float  # -1 (fully bearish aligned) to +1 (fully bullish aligned)
    aligned: bool
    conflicting: list  # list of timeframe pairs that disagree, e.g. [("4h", "15m")]

    def to_dict(self) -> dict:
        return {
            "snapshots": [vars(s) for s in self.snapshots],
            "alignment_score": self.alignment_score,
            "aligned": self.aligned,
            "conflicting": self.conflicting,
        }
