"""
_validate_rules.py — Backtest rules.json against audit log.

Uses compute_signal() from signal_engine.py instead of string-matching notes.
Reports binomial confidence interval on win rate.

IMPORTANT (audit fix 2026-08-05): Entries that predate full-snapshot capture
(no `snapshot` field) are EXCLUDED from rule simulation. A zero-filled
snapshot always scores ~50 (neutral), which trivially "blocks" every loss —
that phantom-data result proved nothing. Only entries with a captured
`snapshot` dict are signal-simulated.
"""

import json
import math
from signal_engine import Snapshot, compute_signal


def _binom_ci(wins: int, total: int, z: float = 1.96) -> tuple:
    """Wilson score interval for binomial proportion."""
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denominator = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) / total + z**2 / (4 * total**2))) / denominator
    return (max(0, centre - margin), min(1, centre + margin))


_SNAPSHOT_FIELDS = (
    "delta", "volume_total", "volume_bid", "volume_ask",
    "poc", "value_area_high", "value_area_low",
    "dom_imbalance_pct", "cumulative_bid_vol", "cumulative_ask_vol",
    "tape_buy_vol", "tape_sell_vol", "tape_buy_count", "tape_sell_count",
    "tape_buy_count_pct", "tape_largest_trade",
)


def _build_snapshot_from_entry(entry: dict) -> tuple[Snapshot | None, bool]:
    """Build a Snapshot from an audit log entry.

    Returns (snapshot, has_full_data). Entries without the `snapshot` field
    return (None, False) — they cannot be simulated without phantom-data bias.
    """
    snap = entry.get("snapshot")
    if not isinstance(snap, dict):
        return None, False
    fields = {f: snap.get(f, 0) for f in _SNAPSHOT_FIELDS}
    return Snapshot(
        ticker=entry.get("asset", "?"),
        price=entry.get("price_at_call", 0),
        footprint_levels=list(snap.get("footprint_levels", [])),
        tape_largest_trade_side=snap.get("tape_largest_trade_side", "?"),
        cvd=snap.get("cvd", 0.0),
        cvd_bars=snap.get("cvd_bars", 0),
        cvd_bar_deltas=list(snap.get("cvd_bar_deltas", [])),
        **fields,
    ), True


def run_validation():
    with open("audit.log.json", "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    audit_log = data.get("audit_log", [])
    print("=== QUANT RULES v3.2 BACKTEST VALIDATION ===")
    print(f"Total historical calls: {len(audit_log)}")

    wins = 0
    losses = 0
    simulated = 0
    skipped = 0

    for i, entry in enumerate(audit_log):
        outcome = entry.get("outcome", "?")
        snap, has_snapshot = _build_snapshot_from_entry(entry)
        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1

        if not has_snapshot:
            skipped += 1
            print(f"Call {i+1} ({entry['call'][:40]:40s}) -> {outcome.upper()}. "
                  f"NO SNAPSHOT (pre-fix) — excluded from simulation")
            continue

        simulated += 1
        sig = compute_signal(snap)

        if outcome == "win":
            rule_result = "PASS" if sig.trade_plan_allowed else "FILTERED (wrongly)"
            print(f"Call {i+1} ({entry['call'][:40]:40s}) -> WIN. Rules: {rule_result}. Score={sig.bias_score:.0f}")
        else:
            blocked = not sig.trade_plan_allowed
            rule_result = "BLOCKED" if blocked else "NOT BLOCKED"

            reasons = []
            if not sig.trade_plan_allowed:
                if sig.bias_score < 60 and sig.bias_score > 40:
                    reasons.append("Rule 10 (neutral zone)")
            if not sig.rule_6_active:
                reasons.append("Rule 6 (distribution incomplete)")
            if not sig.rule_7_active:
                reasons.append("Rule 7 (accumulation incomplete)")

            reason_str = "; ".join(reasons) if reasons else "none — rule gap"
            print(f"Call {i+1} ({entry['call'][:40]:40s}) -> LOSS. Rules: {rule_result}. Score={sig.bias_score:.0f} ({reason_str})")

    total = wins + losses
    win_rate = wins / total if total > 0 else 0
    ci_low, ci_high = _binom_ci(wins, total)

    print("\n=== SUMMARY ===")
    print(f"Win count: {wins}/{total}")
    print(f"Raw win rate: {win_rate:.1%}")
    print(f"95% binomial CI: [{ci_low:.1%}, {ci_high:.1%}]")
    print(f"Note: At n={total}, confidence interval is wide. ")
    print(f"Need ~200+ calls for a stable estimate.")
    print(f"Rule simulation coverage: {simulated}/{total} entries "
          f"({skipped} pre-fix entries without snapshot — not simulated)")

    if simulated == 0:
        print("\nWARNING: No entry has a full snapshot. Rule simulation is "
              "meaningless until audit entries capture `snapshot` at call time "
              "(skill Rule 8). Raw win rate above is outcome-only, not signal-validated.")

    # Rule coverage (only over entries with real snapshots)
    rule_hits = {
        "Rule 6 (distribution)": sum(1 for e in audit_log if _build_snapshot_from_entry(e)[1] and compute_signal(_build_snapshot_from_entry(e)[0]).rule_6_active),
        "Rule 7 (accumulation)": sum(1 for e in audit_log if _build_snapshot_from_entry(e)[1] and compute_signal(_build_snapshot_from_entry(e)[0]).rule_7_active),
    }
    print("\n=== RULE COVERAGE (simulated, snapshot-only) ===")
    for rule, count in rule_hits.items():
        print(f"  {rule}: {count}/{simulated} calls would have triggered")


if __name__ == "__main__":
    run_validation()
