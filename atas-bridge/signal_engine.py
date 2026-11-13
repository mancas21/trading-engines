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
# rules.json — the ONLY source of rule thresholds
# ---------------------------------------------------------------------------
# 2026-10-11 audit finding, line-by-line pass: every threshold below used to be
# hardcoded (spike 2.0 vs declared 1.5, footprint 70% vs declared 65, DOM
# +5/+10 vs declared +3/+8), dom_persistence_polls_min was not checked at all,
# and rule 6's "disabled" enforcement was ignored — the quarantined rule kept
# vetoing plans. That is the exact transcription-bug class verdict.py fixed on
# 2026-10-07 ("los umbrales estaban transcritos a mano"). Everything is read
# from atas-bridge/rules.json now; the old literals survive only as fallbacks
# for the no-file case, and a missing file warns instead of guessing silently.
_RULES_PATH = os.path.join(os.path.dirname(__file__), "rules.json")


def _load_rules_raw() -> dict:
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"[signal_engine] AVISO: rules.json no legible ({e}); "
              f"se usan umbrales por defecto que pueden NO cumplir rules.json.")
        return {}


_RULES_RAW = _load_rules_raw()


def _load_rule_conditions(rule_id: int) -> dict:
    """Flat conditions dict for one rule id, or _present=False when missing.

    Mirrors reader/verdict.py's loader: rules.json nests conditions under
    rules["rules"][i]["conditions"] with the rules as a LIST here. A flat dict
    means a consumer either finds the key or it doesn't — there is no wrong
    level to read from.
    """
    for r in (_RULES_RAW.get("rules") or []):
        try:
            if int(r.get("id", -1)) == rule_id:
                out = dict(r.get("conditions") or {})
                out["_enforcement"] = r.get("enforcement", "hard")
                out["_present"] = True
                return out
        except (TypeError, ValueError):
            continue
    return {"_enforcement": "hard", "_present": False}


_R2 = _load_rule_conditions(2)

RULE2 = {"spike_multiple": float(_R2.get("spike_multiple", 2.0)),
         "min_prior_bars": int(_R2.get("min_prior_bars", 3))}
if not _R2.get("_present"):
    print("[signal_engine] AVISO: regla 2 incompleta en rules.json; "
          "los defaults sustituyen lo que falte.")
# AT-6/AT-7 NO se evaluan aqui (2026-10-11, Fix 2-A): su condicion (c) es
# consenso multi-venue del scanner con persistencia entre lecturas, y este
# modulo es puro (sin I/O, sin memoria entre llamadas). Su UNICO evaluador es
# la puerta del reader: reader/verdict.py dom_vac_eval. Duplicarlos aqui con
# otra fuente de datos es el fallo de contrato que este repo ya pago dos veces.


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

    # DOM (raw snapshot + 45s EMA; scoring and rules 6/7 MUST use the EMA —
    # audit C-3 action 3, 2026-09-17 — with raw fallback when EMA unavailable)
    dom_imbalance_pct: float = 0.0
    dom_imbalance_ema: float = 0.0
    # Sample count behind the EMA. rules.json's dom_persistence_polls_min makes
    # it part of conditions (c): a hard rule evaluated on a single sample is
    # not a hard rule. The bridge attaches imbalance_pct_ema_n in
    # _attach_dom_context; backtester snapshots predate the field and send 0.
    dom_imbalance_ema_n: int = 0
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

    # ── LiveExporter schema 2 (2026-11-12) ────────────────────────────────
    # TRUE per-bar tape totals. The `tape_buy_vol`/`tape_sell_vol` above are
    # the 30-PRINT WINDOW and keep that meaning on purpose: old track-record
    # rows must stay comparable. These are every print of the bar, so a 30-print
    # window can no longer be mistaken for the bar (measured on 1000PEPEUSDT:
    # window said net +1.1M BUY while the bar was net -6.6M SELL over 457
    # prints). Always read these for direction, not the window fields.
    tape_bar_buy_vol: float = 0.0
    tape_bar_sell_vol: float = 0.0
    tape_bar_trade_count: int = 0
    tape_prints_sampled: int = 0

    # Per-bar order flow
    open_interest: float = 0.0
    vwap: float = 0.0
    max_delta: float = 0.0
    min_delta: float = 0.0

    # Session value area, computed inside ATAS from the closed-bar footprint
    # union. Before schema 2 the bridge only ever saw the FORMING bar's value
    # area, so there was no session context at all.
    session_poc: float = 0.0
    session_value_area_high: float = 0.0
    session_value_area_low: float = 0.0
    session_value_area_bars: int = 0

    # Closed-bar footprint summary, newest first. Each entry carries the bar's
    # POC / value area / delta / imbalance plus stacked-imbalance counts.
    # The per-level detail is only present when the exporter's
    # "Publish closed-bar levels" setting is on.
    closed_bars: list[dict] = field(default_factory=list)


@dataclass
class SignalResult:
    """Deterministic output from compute_signal()."""
    trade_plan_allowed: bool
    rule_2_exhaustion: bool
    data_coverage: float = 1.0     # 0-1: fraction of scoring weight backed by real data
    plan_blocked_reasons: list[str] = field(default_factory=list)
    # Audit H-1/H-2 action 4 (2026-09-17): explicit footprint verifiability for
    # the footprint legs — "ok" (4+ levels: evaluable), "partial" (1-3),
    # "absent" (0: NOT neutral — unknowable).
    footprint_status: str = "absent"


# ---------------------------------------------------------------------------
# Rule checks
# ---------------------------------------------------------------------------

def _dom_ema(s: Snapshot) -> float:
    """45s-averaged DOM imbalance; raw fallback when EMA unavailable
    (backtester/validator snapshots predate the EMA field)."""
    return s.dom_imbalance_ema if s.dom_imbalance_ema != 0.0 else s.dom_imbalance_pct


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
    """Rule 2: one-bar CVD spike > spike_multiple x prior max 1-bar spike =
    exhaustion, not continuation. Thresholds from rules.json (v3.4.1 declares
    spike_multiple 1.5, min_prior_bars 3 — the code used to hardcode 2.0, so a
    1.5x-2.0x spike that rules.json calls exhaustion was read as continuation).
    Only evaluated on a clean CVD series (post-gap deltas are unreliable)."""
    need = RULE2["min_prior_bars"]
    if len(s.cvd_bar_deltas) < need + 1:
        return False
    if s.cvd_continuity == "post-gap":
        return False
    last = abs(s.cvd_bar_deltas[-1])
    prior_max = max(abs(d) for d in s.cvd_bar_deltas[:-1])
    return prior_max > 0 and last > RULE2["spike_multiple"] * prior_max


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def _data_coverage(s: Snapshot) -> float:
    """Fraction of total scoring weight backed by REAL data (audit C4).

    Missing footprint/tape/volume currently fall back to neutral 5.0, which
    makes the weighted score treat "no data" as "balanced data" — that lets a
    single DOM reading manufacture a plan. Coverage < 0.5 blocks plans and is
    reported on every SignalResult."""
    real = 0.0
    total = 0.0
    WEIGHTS = {"footprint_depth": 25, "footprint_confirmation": 15,
                    "dom_imbalance": 25, "tape_ratio": 20, "volume_climax": 15}
    for k, w in WEIGHTS.items():
        total += w
        if k == "footprint_depth" and s.footprint_levels:
            real += w
        elif k == "footprint_confirmation" and len(s.footprint_levels) >= 2:
            real += w
        elif k == "dom_imbalance" and (_dom_ema(s) != 0.0
                                        or s.cumulative_bid_vol > 0 or s.cumulative_ask_vol > 0
                                        or s.dom_imbalance_pct != 0.0):
            real += w
        elif k == "tape_ratio" and (s.tape_buy_vol > 0 or s.tape_sell_vol > 0
                                    or s.tape_buy_count > 0 or s.tape_sell_count > 0):
            real += w
        elif k == "volume_climax" and s.volume_total > 0:
            real += w
    return (real / total) if total > 0 else 0.0


def compute_signal(snapshot: Snapshot) -> SignalResult:
    """Rule checks + data-coverage verdict. Deterministic, no scoring.

    The weighted bias_score was REMOVED (2026-10-11): its v3.4.1 weights were
    measured ANTI-PREDICTIVE (spread -4.0 on 1,205 resolved outcomes) and its
    percentile successor lives in reader/verdict.py, which is the single
    scoring authority. What survives here is the part that measured clean:
    the data-coverage and footprint-verifiability honesty. Rules AT-6/AT-7
    moved to the reader's door the same day (Fix 2-A, docs/FIXES-gate-DOM.md):
    their condition (c) is scanner multi-venue book consensus with
    cross-reading persistence, which a pure module cannot judge.
    """
    rule_2_exhaustion = _check_rule_2_exhaustion(snapshot)
    coverage = _data_coverage(snapshot)

    # Footprint verifiability for rules 6/7 (audit H-1: ATAS charts one symbol;
    # for the rest the legs cannot be evaluated at all — report it, don't
    # silently score missing footprint as neutral).
    _fp_n = len(snapshot.footprint_levels)
    footprint_status = "ok" if _fp_n >= 4 else ("partial" if _fp_n >= 1 else "absent")

    reasons: list[str] = []
    # C4/H-2: no phantom plans from single-source / missing-data scores.
    # Gate raised 0.5 -> 0.75 (audit H-2 action 4): with no footprint at all
    # coverage is 0.60, which must NOT pass.
    trade_plan_allowed = coverage >= 0.75
    if not trade_plan_allowed:
        reasons.append(f"low_data_coverage_{coverage:.2f}")
    if footprint_status != "ok":
        reasons.append(f"rules_67_unverifiable_footprint_{footprint_status}")

    return SignalResult(
        trade_plan_allowed=trade_plan_allowed,
        rule_2_exhaustion=rule_2_exhaustion,
        data_coverage=round(coverage, 2),
        plan_blocked_reasons=reasons,
        footprint_status=footprint_status,
    )


def build_snapshot_from_live(live: dict, cvd: dict | None = None) -> Snapshot | None:
    """Build a Snapshot from LiveExporter dict + optional bridge CVD state."""
    if not live:
        return None
    # `or {}` on every block, not just a default: a key that EXISTS with a
    # null value (a torn read, a hand-edited file) makes `live.get(k, {})`
    # return None and the next `.get` raises AttributeError. The reader must
    # survive a half-written snapshot, not crash on it.
    price = live.get("price") or {}
    vol = live.get("volume") or {}
    fp = live.get("footprint") or {}
    dom = live.get("dom") or {}
    tape = live.get("tape") or {}
    cvd = cvd or {}
    # Schema 2 blocks. Every one of them is optional: a v1 payload (an old
    # exporter file, or a backtester snapshot) must still build a Snapshot.
    sva = live.get("session_value_area") or {}
    closed_bars = live.get("closed_bars") or []

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
        dom_imbalance_ema=dom.get("imbalance_pct_ema", 0),
        dom_imbalance_ema_n=int(dom.get("imbalance_pct_ema_n", 0) or 0),
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
        # ── schema 2 ──
        tape_bar_buy_vol=tape.get("bar_buy_vol") or 0,
        tape_bar_sell_vol=tape.get("bar_sell_vol") or 0,
        tape_bar_trade_count=int(tape.get("bar_trade_count") or 0),
        tape_prints_sampled=int(tape.get("prints_sampled") or 0),
        open_interest=live.get("open_interest") or 0,
        vwap=live.get("vwap") or 0,
        max_delta=live.get("max_delta") or 0,
        min_delta=live.get("min_delta") or 0,
        session_poc=sva.get("poc") or 0,
        session_value_area_high=sva.get("value_area_high") or 0,
        session_value_area_low=sva.get("value_area_low") or 0,
        session_value_area_bars=int(sva.get("bars_used") or 0),
        closed_bars=list(closed_bars),
    )
