"""Auditor tick for the cron: emit ONLY when something actually changed.

The auditor runs every 30 minutes. At 12s per cycle the database gains ~150
rows per tick, so the sample size moves constantly — but "n grew" is not news.
The point is to be told when:

  * a rule crosses the significance floor (n >= 50 and |t| >= 1.65), or
  * a rule changes bucket (insufficient -> inconclusive -> keep -> drop), or
  * a rule is ELIGIBLE for promotion, or
  * the pipeline itself breaks (no data, resolver failing, venues frozen).

Everything else exits 0 with no output, and the cron delivers nothing.

STATE FILE: data/audit_state.json holds the last emitted summary. It is what
makes "changed" mean something across ticks.

Run:  python scanner-engine/audit_tick.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import expectancy as E          # noqa: E402
import promotion as PROM        # noqa: E402
import recorder as RC          # noqa: E402
from recorder import _guard     # noqa: E402
import rules_eval as RE         # noqa: E402

DB = HERE / "data" / "scanner.db"
RULES = HERE / "rules.json"
STATE = HERE / "data" / "audit_state.json"

MIN_N = E.DEFAULT_MIN_N
MIN_T = E.DEFAULT_MIN_T


def fingerprint(audit: dict) -> dict:
    """The parts worth waking a human for."""
    out = {}
    for rid, row in (audit.get("rules") or {}).items():
        t = row.get("t")
        out[rid] = {
            "bucket": row.get("bucket"),
            # bucket changes only; n alone is not news
            "n": row.get("n"),
            "t": round(t, 2) if t is not None else None,
            "mean": row.get("mean"),
            "hit": row.get("hit_rate"),
            "significant": bool(row.get("n", 0) >= MIN_N and t is not None
                                and abs(t) >= abs(MIN_T)),
        }
    return out


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def main() -> int:
    if not DB.exists():
        print(f"AUDIT: database {DB} does not exist — the scanner has never run.")
        return 0

    db = RC.open_db(DB)
    audit = E.audit(db)
    rules = RE.load_rules(RULES)

    c = audit.get("counts", {})
    health = {
        "rows_total": c.get("rows_total", 0),
        "rows_resolved": c.get("rows_resolved", 0),
        "candidate_resolved": c.get("rows_candidate_resolved", 0),
        "directed_resolved": c.get("rows_directed_resolved", 0),
        "awaiting": (c.get("rows_total", 0) - c.get("rows_resolved", 0)),
        "unscorable": c.get("rows_unscorable", 0),
        "pending": None,          # filled from SQL below
    }

    # ── pipeline broken? that is always worth saying ───────────────────────
    broken = []
    if audit.get("verdict") == "INSUFFICIENT DATA" and health["rows_total"] > 20:
        broken.append("no directed rows despite data being recorded")
    # The legacy backlog predates the resolver and cannot be scored honestly:
    # a 5-minute forward move is not recoverable from a 16-hour-old row. It is
    # expected to stay pending forever and is NOT a fault. Only rows recorded
    # recently are a real signal about resolver health.
    with _guard(db):
        # resolved=0 AND unscorable=0: the only rows that still OWE an outcome.
        pending_now = db.execute(
            "SELECT COUNT(*) FROM verdicts WHERE resolved = 0 AND unscorable = 0"
        ).fetchone()[0]
    if health["rows_resolved"] == 0 and health["rows_total"] > 50:
        broken.append("zero rows resolved — outcome resolver is not running")
    elif pending_now > 40:
        broken.append(f"{pending_now} rows still pending an outcome "
                      f"(resolved {health['rows_resolved']} and climbing, "
                      f"so this is backlog, not a stall)")

    prev = load_state()
    fp = fingerprint(audit)
    prev_fp = prev.get("fingerprint", {})

    changes = []
    for rid, cur in fp.items():
        old = prev_fp.get(rid)
        if old is None:
            changes.append(f"rule {rid}: first measurement — {cur['bucket']} "
                           f"n={cur['n']} t={cur['t']} mean={cur['mean']:+.6f}")
            continue
        if cur["bucket"] != old["bucket"]:
            changes.append(f"rule {rid}: bucket {old['bucket']} -> {cur['bucket']} "
                           f"(n={cur['n']} t={cur['t']})")
        elif not old.get("significant") and cur["significant"]:
            changes.append(f"rule {rid}: now SIGNIFICANT (n={cur['n']} t={cur['t']} "
                           f"mean={cur['mean']:+.6f} hit={cur['hit']})")

    # promotion eligibility
    promo = PROM.promotion_report(db, rules, audit)
    eligible = [p for p in promo if p["state"] == "PROMOTE"]
    prev_elig = prev.get("eligible", [])
    now_elig = sorted(p["rule"] for p in eligible)
    for rid in now_elig:
        if rid not in prev_elig:
            changes.append(f"rule {rid}: ELIGIBLE for promotion — "
                           f"run `python promotion.py` to review")

    # row-count sanity: not news, but a big drop IS
    if prev.get("rows_total"):
        delta = health["rows_total"] - prev["rows_total"]
        if delta < 0:
            changes.append(f"row count DROPPED by {abs(delta)} "
                           f"({prev['rows_total']} -> {health['rows_total']})")

    save_state({"fingerprint": fp, "eligible": now_elig,
                "rows_total": health["rows_total"], "ts": time.time()})

    db.close()

    # A CONTINUING pipeline problem is not news every 30 minutes. Report each
    # distinct problem once; re-report only if it persists AND the numbers
    # moved, or if a NEW problem appears. Measured: the "347 rows never
    # resolve" warning was identical on every tick because the row count had
    # not changed, so the change-detection never fired and the job repeated
    # itself forever.
    prev_problems = set(prev.get("problems", []))
    now_problems = set(broken)
    fresh = now_problems - prev_problems
    resolved_now = prev_problems - now_problems

    to_report = list(fresh)
    for b in resolved_now:
        to_report.append(f"RESOLVED: {b}")

    # Re-report a still-open problem only when the numbers behind it moved, so
    # a stuck backlog does not repeat verbatim forever. Keyed on resolved rows,
    # not total rows: the legacy pre-resolver backlog keeps total constant
    # while resolved climbs, and "resolved" is the number that proves the
    # resolver is making progress.
    still_open = now_problems & prev_problems
    if still_open and health["rows_resolved"] != prev.get("rows_resolved"):
        to_report.append("still open (progressing): " + "; ".join(sorted(still_open)))

    save_state({"fingerprint": fp, "eligible": now_elig,
                "rows_total": health["rows_total"],
                "rows_resolved": health["rows_resolved"],
                "problems": sorted(now_problems),
                "ts": time.time()})
    db.close()

    if not changes and not to_report:
        return 0                       # silence: nothing changed

    lines = ["scanner-engine: audit update", ""]
    lines.append(f"  rows {health['rows_total']}  resolved {health['rows_resolved']}  "
                 f"candidate {health['candidate_resolved']}  "
                 f"directed {health['directed_resolved']}  "
                 f"pending {pending_now}  retired {health['unscorable']}")
    lines.append("")
    if to_report:
        lines.append("STATUS:")
        lines += [f"  - {b}" for b in to_report]
        lines.append("")
    if changes:
        lines.append("CHANGED:")
        lines += [f"  * {c}" for c in changes]
    else:
        lines.append("no rule changed state; sample still growing")

    for p in promo:
        if p["state"] != "no data":
            t = f"{p['t']:+.2f}" if p.get("t") is not None else "n/a"
            lines.append(f"    rule {p['rule']:2} {p['state']:8} n={p.get('n')} "
                         f"t={t}  {p['detail']}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())