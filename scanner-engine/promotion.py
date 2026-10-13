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

    `min_t` is the SIGNIFICANCE MAGNITUDE and is normalised with abs(), so a
    caller passing either 1.65 or -1.65 gets the same bar. The auditor now
    derives this from Bonferroni across the cells it grades rather than using
    the bare 1.65 constant, and the two must agree — a promotion gate looser
    than the auditor's own threshold is a gate that fires on noise.

    The sign handling here used to be wrong in a way that mattered: it tested
    `t >= -min_t` to grant and `t <= min_t` to refute, so with a POSITIVE
    threshold essentially any t granted promotion. Promotion is the one
    irreversible act in this engine, so it gets the strictest reading available.
    """
    if is_audited(spec):
        return False, "already audited"

    if not has_directional_claim(spec):
        return False, ("no directional claim (direction_hint is null) — this "
                       "rule is observation-only by design")

    thresh = abs(float(min_t))
    n = audit_row.get("n", 0)
    n_eff = audit_row.get("n_eff", n)
    mean = audit_row.get("mean", 0.0)
    t = audit_row.get("t")

    if n_eff < min_n:
        return False, (f"n_eff={n_eff} < min_n={min_n}: insufficient data "
                       f"(n={n}, overlap-corrected)")

    if t is None:
        return False, "t-statistic undefined (degenerate distribution)"

    if mean > 0 and t >= thresh:
        return True, (f"EARNED: n_eff={n_eff} net mean={mean:+.6f} "
                      f"t={t:+.2f} >= {thresh:.2f}")

    if mean < 0 and t <= -thresh:
        return False, (f"REFUTED: net mean={mean:+.6f} t={t:+.2f} <= "
                       f"{-thresh:.2f} — a significant loser")

    return False, f"INCONCLUSIVE: net mean={mean:+.6f} t={t:+.2f} vs {thresh:.2f}"


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


DROP_PREFIX = "DROP"


def is_retired(spec: dict) -> bool:
    """True when the auditor measured the rule into the DROP bucket.

    Retired rules stay in rules.json with their evidence attached — deleting
    them would erase the measurement. They stop producing candidate rows (see
    scanner._retired_rules), which is the loop-closing half of promotion: the
    auditor can now also take hypotheses OUT, not only grant sides.
    """
    status = str((spec or {}).get("audit_status", "")).upper()
    return status.startswith(DROP_PREFIX)


def apply_retirement(rules_path: pathlib.Path, rule_id: str,
                     audit_row: dict) -> tuple[bool, str]:
    """Write a measured DROP into rules.json. Refuses without evidence.

    Retirement is the inverse of promotion and gets the same strictness: it
    only writes when the audit row actually shows a significant net loser
    (mean < 0 and t <= -threshold on the EFFECTIVE sample). A rule retired on
    an eyeball read would be exactly the "conclusión sin medida" this repo
    exists to prevent.
    """
    with open(rules_path, encoding="utf-8") as fh:
        rules = json.load(fh)

    spec = rules["rules"].get(rule_id)
    if spec is None:
        return False, f"no such rule {rule_id}"

    n = audit_row.get("n", 0)
    n_eff = audit_row.get("n_eff", n)
    mean = audit_row.get("mean", 0.0)
    t = audit_row.get("t")

    if mean is None or t is None:
        return False, (f"sin medida suficiente (mean={mean}, t={t}) — "
                       f"una regla sin medida no se retira")
    if not (mean < 0 and t < 0):
        return False, (f"la medida no refuta (net {mean:+.6f}, t={t:+.2f}) — "
                       f"REFUSED")
    if n_eff < DEFAULT_MIN_N:
        return False, (f"n_eff={n_eff} < {DEFAULT_MIN_N}: la medida no alcanza "
                       f"el suelo de significancia — REFUSED")

    spec["audit_status"] = (
        f"DROP refuted n={n} n_eff={n_eff:.1f} t={t:+.2f} mean={mean:+.6f}"
    )
    spec["measured"] = {
        "n": n,
        "n_eff": n_eff,
        "t": t,
        "mean": mean,
        "hit_rate": audit_row.get("hit_rate"),
        "hit_gross": audit_row.get("hit_gross"),
        "hz_med_s": audit_row.get("hz_med_s"),
    }
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return True, (f"RETIRADA: net {mean:+.6f}, t={t:+.2f}, "
                  f"n_eff={n_eff:.1f} — deja de producir candidatos")


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
            "n_eff": row.get("n_eff"),
            "t": row.get("t"),
            "t_naive": row.get("t_naive"),
            "mean": row.get("mean"),
            "detail": reason,
        })
    return out


if __name__ == "__main__":
    import argparse
    import sys

    HERE = pathlib.Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE))
    import recorder as RC
    import expectancy as E
    import rules_eval as RE

    ap = argparse.ArgumentParser(description="scanner-engine rule promotion")
    ap.add_argument("--db", default=str(HERE / "data" / "scanner.db"))
    ap.add_argument("--rules", default=str(HERE / "rules.json"))
    ap.add_argument("--apply", action="store_true",
                    help="write the promotion to rules.json (default: report only)")
    ap.add_argument("--retire", action="store_true",
                    help="write a measured DROP for --rule instead of a promotion. "
                         "Refuses unless the audit row shows a significant net loser.")
    ap.add_argument("--rule", default=None, help="rule id to promote/retire, e.g. 4")
    ap.add_argument("--min-n", type=int, default=DEFAULT_MIN_N)
    ap.add_argument("--min-t", type=float, default=None,
                    help="override the Bonferroni-derived threshold")
    args = ap.parse_args()

    db_path = pathlib.Path(args.db)
    if not db_path.exists():
        print(f"no database at {db_path} — nothing to promote")
        raise SystemExit(0)

    rules = RE.load_rules(args.rules)
    db = RC.open_db(db_path)
    a = E.audit(db, args.min_n, args.min_t)

    print("=" * 74)
    print("PROMOTION REPORT (read-only — nothing written)")
    print("reglas de scanner-engine/rules.json (namespace: scanner)")
    report = promotion_report(db, rules, a)
    for row in report:
        t = f"{row['t']:+.2f}" if row.get("t") is not None else "n/a"
        m = f"{row['mean']:+.6f}" if row.get("mean") is not None else "n/a"
        print(f"\nrule {row['rule']}  {row.get('name')}")
        print(f"   state  {row['state']}")
        print(f"   n={row.get('n')}  n_eff={row.get('n_eff')}  t={t}  net mean={m}")
        print(f"   {row['detail']}")

    if not args.apply and not args.retire:
        print("\nNothing written. To apply a promotion:")
        print("  python promotion.py --apply --rule <id>")
        print("To retire a measured loser:")
        print("  python promotion.py --retire --rule <id>")
        raise SystemExit(0)

    # ── apply ────────────────────────────────────────────────────────────
    if not args.rule:
        print("\n--apply/--retire need --rule <id>. Nothing written.")
        raise SystemExit(2)

    audit_row = (a.get("rules") or {}).get(str(args.rule))
    if args.retire:
        ok, reason = apply_retirement(pathlib.Path(args.rules), str(args.rule),
                                      audit_row or {})
        print(f"\n{'RETIRADA' if ok else 'REFUSED'}: rule {args.rule} — {reason}")
        if ok:
            print("  RESTART THE SCANNER — la regla retirada deja de producir "
                  "candidatos solo cuando el proceso recarga rules.json.")
        raise SystemExit(0 if ok else 1)

    target = next((r for r in report if str(r["rule"]) == str(args.rule)), None)
    if target is None:
        print(f"\nno such rule {args.rule!r}. Nothing written.")
        raise SystemExit(2)
    if target["state"] != "PROMOTE":
        print(f"\nrule {args.rule} is {target['state']}, not PROMOTE — "
              f"promotion refused. Nothing written.")
        print(f"  {target['detail']}")
        raise SystemExit(1)

    audit_row = (a.get("rules") or {}).get(str(args.rule))
    if args.retire:
        ok, reason = apply_retirement(pathlib.Path(args.rules), str(args.rule),
                                      audit_row or {})
        print(f"\n{'RETIRADA' if ok else 'REFUSED'}: rule {args.rule} — {reason}")
        if ok:
            print("  RESTART THE SCANNER — la regla retirada deja de producir "
                  "candidatos solo cuando el proceso recarga rules.json.")
        raise SystemExit(0 if ok else 1)

    ok, reason = apply_promotion(pathlib.Path(args.rules), str(args.rule),
                                 audit_row, args.min_n, a.get("min_t"))
    print(f"\n{'PROMOTED' if ok else 'REFUSED'}: rule {args.rule} — {reason}")
    if ok:
        # rules.json is loaded ONCE at scanner startup. A promotion written to
        # disk is invisible to the running process until it restarts, and a
        # rule that "went audited" without the engine knowing is exactly the
        # kind of silent state drift this engine exists to prevent.
        print("  RESTART THE SCANNER for audit_status to take effect — the "
              "running process keeps its old rules until then.")
    raise SystemExit(0 if ok else 1)