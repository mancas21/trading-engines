"""Promotion — the step that closes the loop.

A rule becomes AUDITED only when the auditor has measured it on the CANDIDATE
ledger with its own significance floor. Nothing here can promote a rule on a
favourable read: `min_n` and `min_t` are the same values the auditor uses, and
`promote_if_earned` refuses anything that has not cleared them.

THE LOOP THIS CLOSES
====================
  rule fires MET -> provisional side from direction_hint -> candidate row enters
  expectancy math -> auditor measures it -> if it clears n>=50 and t>=-1.65 and
  the mean is positive, the rule is PROMOTED to AUDITED -> only then may it
  contribute a side to the verdict ledger, where alerts and gating live.

Without promotion, every row stayed direction=0 forever: measured on the running
engine as 336 rows, `forward_return NOT NULL` = 0, `direction != 0` = 0.

WHAT PROMOTION WILL NOT DO
==========================
* Promote on n below the floor. Insufficient data is a valid answer and the
  routine says so.
* Promote a rule whose direction_hint is null. Rule 1 has no directional claim,
  so there is nothing to promote even if its numbers are good — and reporting
  that honestly is the point.
* Infer a side from a rule's name.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

DEFAULT_MIN_N = 50
DEFAULT_MIN_T = -1.65

AUDITED_PREFIX = "AUDITED"
UNVALIDATED_PREFIX = "UNVALIDATED"


def _spec(rules: dict, rid: str) -> dict:
    """Nested (rules.json) or flat ({id: spec}) rule mapping — accept both."""
    if not rules:
        return {}
    nested = rules.get("rules")
    if isinstance(nested, dict) and rid in nested:
        return nested[rid] or {}
    return rules.get(rid) or {}


def is_audited(spec: dict) -> bool:
    """A rule is audited only when its status SAYS so.

    Compared on prefix, not equality: `audit_status` is a human-readable string
    ("AUDITED keep n=412 t=+2.14 mean=+0.00031"), and an exact `== "AUDITED"`
    test would report every promoted rule as still unvalidated — which is how a
    rule stays useless forever after being proven.
    """
    status = str((spec or {}).get("audit_status", UNVALIDATED_PREFIX)).upper()
    return status.startswith(AUDITED_PREFIX)


def has_directional_claim(spec: dict) -> bool:
    """True when the rule makes a testable directional claim. A null hint is
    an honest 'this rule has no directional edge', not a gap to fill in."""
    hint = (spec or {}).get("direction_hint")
    return bool(hint) and hint.get("side") in ("follow", "fade")


# ── what the auditor measured ──────────────────────────────────────────────

def promote_if_earned(audit_row: dict, spec: dict,
                      min_n: int = DEFAULT_MIN_N,
                      min_t: float = DEFAULT_MIN_T) -> tuple[bool, str]:
    """Decide whether `spec` may be promoted, given its measured `audit_row`.

    Returns (promote, reason). `audit_row` is one entry from
    `expectancy.audit()["rules"]`.
    """
    if is_audited(spec):
        return False, "already audited"

    if not has_directional_claim(spec):
        return False, ("no directional claim (direction_hint is null) — this "
                       "rule is observation-only by design")

    n = audit_row.get("n", 0)
    mean = audit_row.get("mean", 0.0)
    t = audit_row.get("t")

    if n < min_n:
        return False, f"n={n} < min_n={min_n}: insufficient data"

    if t is None:
        return False, "t-statistic undefined (degenerate distribution)"

    if mean > 0 and t >= -min_t:
        return True, f"EARNED: n={n} mean={mean:+.6f} t={t:+.2f}"

    if mean < 0 and t <= min_t:
        return False, f"REFUTED: mean={mean:+.6f} t={t:+.2f} — a significant loser"

    return False, f"INCONCLUSIVE: mean={mean:+.6f} t={t:+.2f}"


def apply_promotion(rules_path: pathlib.Path, rule_id: str,
                    audit_row: dict, min_n: int = DEFAULT_MIN_N,
                    min_t: float = DEFAULT_MIN_T) -> tuple[bool, str]:
    """Measure-then-write. Never writes unless promote_if_earned agrees."""
    with open(rules_path, encoding="utf-8") as fh:
        rules = json.load(fh)

    spec = rules["rules"].get(rule_id)
    if spec is None:
        return False, f"no such rule {rule_id}"

    ok, reason = promote_if_earned(audit_row, spec, min_n, min_t)
    if not ok:
        return False, reason

    spec["audit_status"] = (
        f"AUDITED keep n={audit_row['n']} t={audit_row['t']:+.2f} "
        f"mean={audit_row['mean']:+.6f}"
    )
    spec["measured"] = {
        "n": audit_row["n"],
        "t": audit_row["t"],
        "mean": audit_row["mean"],
        "hit_rate": audit_row.get("hit_rate"),
    }
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return True, reason


def promotion_report(db: sqlite3.Connection, rules: dict,
                     audit: dict) -> list[dict]:
    """What the next promotion pass WOULD do, without writing anything.

    Read-only by construction: this is the command you run to see whether a
    rule has earned its side before letting the engine act on it.
    """
    out = []
    for rid, spec in sorted(rules["rules"].items()):
        row = (audit.get("rules") or {}).get(rid)
        if row is None:
            out.append({"rule": rid, "state": "no data",
                        "detail": "this rule has never fired MET"})
            continue
        ok, reason = promote_if_earned(row, spec,
                                       audit.get("min_n", DEFAULT_MIN_N),
                                       audit.get("min_t", DEFAULT_MIN_T))
        out.append({
            "rule": rid,
            "name": spec.get("name"),
            "state": "PROMOTE" if ok else "hold",
            "n": row.get("n"),
            "t": row.get("t"),
            "mean": row.get("mean"),
            "detail": reason,
        })
    return out


if __name__ == "__main__":
    import sys
    HERE = pathlib.Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE))
    import recorder as RC
    import expectancy as E
    import rules_eval as RE

    db_path = HERE / "data" / "scanner.db"
    if not db_path.exists():
        print(f"no database at {db_path} — nothing to promote")
        raise SystemExit(0)

    rules = RE.load_rules(HERE / "rules.json")
    db = RC.open_db(db_path)
    a = E.audit(db)

    print("=" * 74)
    print("PROMOTION REPORT (read-only — nothing written)")
    print("=" * 74)
    for row in promotion_report(db, rules, a):
        t = f"{row['t']:+.2f}" if row.get("t") is not None else "n/a"
        m = f"{row['mean']:+.6f}" if row.get("mean") is not None else "n/a"
        print(f"\nrule {row['rule']}  {row.get('name')}")
        print(f"   state  {row['state']}")
        print(f"   n={row.get('n')}  t={t}  mean={m}")
        print(f"   {row['detail']}")

    print("\nTo apply a promotion, run:")
    print("  python promotion.py --apply --rule <id>")