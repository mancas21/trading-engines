"""
_validate_rules.py — Backtest rules.json against the calls track record.

Reads _calls_log.jsonl (open + resolved joined on call_id) per audit M-2
action 7 (2026-09-17). Uses compute_signal() from signal_engine.py instead of
string-matching notes. Reports binomial confidence interval on win rate.

IMPORTANT (audit fix 2026-08-05): Entries that predate full-snapshot capture
(no `snapshot` field) are EXCLUDED from rule simulation. A zero-filled
snapshot always scores ~50 (neutral), which trivially "blocks" every loss —
that phantom-data result proved nothing. Only entries with a captured
`snapshot` dict are signal-simulated.
"""

import json
import math
import os
from signal_engine import Snapshot, compute_signal

_HERE = os.path.dirname(os.path.abspath(__file__))


def _rules_version():
    try:
        with open(os.path.join(_HERE, "rules.json"), encoding="utf-8") as f:
            return json.load(f).get("version", "?")
    except Exception:  # noqa: BLE001
        return "?"


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
    """Build a Snapshot from a joined _calls_log.jsonl record.

    Tolerates all three snapshot shapes in the wild:
      - orderflow_calls (binance-only): mid, cvd_usd, dom_imbalance_pct, ...
      - orderflow_engine compact: price, top_levels, dom_imbalance, ...
      - legacy audit format: footprint_levels, tape_buy_vol, ...
    Returns (snapshot, has_full_data). Records without a `snapshot` dict
    return (None, False) — they cannot be simulated without phantom-data bias.
    """
    snap = entry.get("snapshot")
    if not isinstance(snap, dict):
        return None, False
    fp = snap.get("footprint_levels", snap.get("top_levels", []))
    dom = snap.get("dom_imbalance_pct", snap.get("dom_imbalance", 0))
    dom_ema = snap.get("dom_imbalance_ema_pct",
                       snap.get("dom_imbalance_ema", 0))
    price = snap.get("price", snap.get("mid", 0))
    vol = snap.get("volume_total", 0)
    return Snapshot(
        ticker=entry.get("symbol", "?"),
        price=price or 0,
        delta=snap.get("delta", 0) or 0,
        volume_total=vol or 0,
        volume_bid=snap.get("volume_bid", 0) or 0,
        volume_ask=snap.get("volume_ask", 0) or 0,
        footprint_levels=list(fp or []),
        poc=snap.get("poc", 0) or 0,
        value_area_high=snap.get("value_area_high", snap.get("va_high", 0)) or 0,
        value_area_low=snap.get("value_area_low", snap.get("va_low", 0)) or 0,
        dom_imbalance_pct=dom or 0,
        dom_imbalance_ema=dom_ema or 0,
        cumulative_bid_vol=snap.get("cumulative_bid_vol", 0) or 0,
        cumulative_ask_vol=snap.get("cumulative_ask_vol", 0) or 0,
        tape_buy_vol=snap.get("tape_buy_vol", 0) or 0,
        tape_sell_vol=snap.get("tape_sell_vol", 0) or 0,
        tape_buy_count=snap.get("tape_buy_count", 0) or 0,
        tape_sell_count=snap.get("tape_sell_count", 0) or 0,
        tape_buy_count_pct=snap.get("tape_buy_count_pct", 50) or 50,
        tape_largest_trade=snap.get("tape_largest_trade",
                                    snap.get("tape_largest", 0)) or 0,
        tape_largest_trade_side=snap.get("tape_largest_trade_side",
                                         snap.get("tape_largest_side", "?")),
        cvd=snap.get("cvd", snap.get("cvd_usd", 0.0)) or 0.0,
        cvd_bars=snap.get("cvd_bars", 0) or 0,
        cvd_bar_deltas=list(snap.get("cvd_bar_deltas", [])),
        cvd_continuity=snap.get("cvd_continuity", "clean"),
    ), True


def _load_calls(path: str) -> list[dict]:
    """Join _calls_log.jsonl open+resolved on call_id (audit M-2 action 7).

    Returns joined records: open payload + outcome/outcome_r/resolved_ts.
    Torn lines tolerated like the bridge reader."""
    calls: dict[str, dict] = {}
    order: list[str] = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = rec.get("ev")
            if ev == "open" and isinstance(rec.get("call"), dict):
                cid = rec["call"].get("call_id")
                if cid:
                    calls[cid] = dict(rec["call"])
                    order.append(cid)
            elif ev == "resolved":
                cid = rec.get("call_id")
                if cid in calls:
                    calls[cid].update({
                        "outcome": rec.get("outcome"),
                        "outcome_r": rec.get("outcome_r"),
                        "resolved_ts": rec.get("ts"),
                    })
    return [calls[cid] for cid in order if calls[cid].get("outcome")]


def run_validation():
    log_path = os.path.join(_HERE, "_calls_log.jsonl")
    records = _load_calls(log_path)

    print(f"=== QUANT RULES v{_rules_version()} BACKTEST VALIDATION ===")
    print(f"Resolved calls in log: {len(records)}")

    wins = 0
    losses = 0
    simulated = 0
    skipped = 0
    no_trade = 0

    for i, entry in enumerate(records):
        r = entry.get("outcome_r")
        if r is None or r == 0:
            no_trade += 1
            continue
        outcome = "win" if r > 0 else "loss"
        label = f"{entry.get('symbol', '?')} {entry.get('direction', '?')} r={r:+.2f}"
        snap, has_snapshot = _build_snapshot_from_entry(entry)
        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1

        if not has_snapshot:
            skipped += 1
            print(f"Call {i+1} ({label:40s}) -> {outcome.upper()}. "
                  f"NO SNAPSHOT (pre-fix) — excluded from simulation")
            continue

        simulated += 1
        sig = compute_signal(snap)

        if outcome == "win":
            rule_result = "PASS" if sig.trade_plan_allowed else "FILTERED (wrongly)"
            print(f"Call {i+1} ({label:40s}) -> WIN. Rules: {rule_result}. Score={sig.bias_score:.0f}")
        else:
            blocked = not sig.trade_plan_allowed
            rule_result = "BLOCKED" if blocked else "NOT BLOCKED"

            reasons = []
            if not sig.trade_plan_allowed:
                reasons.extend(sig.plan_blocked_reasons or [])
            if not sig.rule_6_active:
                reasons.append("Rule 6 (distribution incomplete)")
            if not sig.rule_7_active:
                reasons.append("Rule 7 (accumulation incomplete)")

            reason_str = "; ".join(reasons) if reasons else "none — rule gap"
            print(f"Call {i+1} ({label:40s}) -> LOSS. Rules: {rule_result}. Score={sig.bias_score:.0f} ({reason_str})")

    total = wins + losses
    win_rate = wins / total if total > 0 else 0
    ci_low, ci_high = _binom_ci(wins, total)

    print("\n=== SUMMARY ===")
    print(f"Win count: {wins}/{total} (+{no_trade} no-trade resolutions skipped)")
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
    sim_sigs = []
    for e in records:
        r = e.get("outcome_r")
        if r is None or r == 0:
            continue
        snap, ok = _build_snapshot_from_entry(e)
        if ok:
            sim_sigs.append(compute_signal(snap))
    rule_hits = {
        "Rule 6 (distribution)": sum(1 for s in sim_sigs if s.rule_6_active),
        "Rule 7 (accumulation)": sum(1 for s in sim_sigs if s.rule_7_active),
    }
    print("\n=== RULE COVERAGE (simulated, snapshot-only) ===")
    for rule, count in rule_hits.items():
        print(f"  {rule}: {count}/{len(sim_sigs)} calls would have triggered")


if __name__ == "__main__":
    run_validation()
