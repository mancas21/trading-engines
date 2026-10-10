"""Expectancy auditor — the tool that decides which rules earn their place.

Every rule in `rules.json` is a HYPOTHESIS until this has measured it. Day one
the honest answer is "insufficient data", and this module is built so that
answer is hard to argue with and easy to produce.

THE FOUR THINGS THIS WILL NOT DO
================================
1. Treat `direction = 0` as bearish. Those rows are excluded via
   `recorder.DIRECTED_RETURN_SQL`. In the sibling engine this exact mistake
   (63% of rows) inverted the entire outcome gate.

2. Conclude anything from a negative mean alone. At n in the dozens, a negative
   mean is common by chance. A rule is only recommended for suppression when
   `mean < 0` AND `n >= min_n` AND `t <= -min_t`.

3. Report a verdict for a rule whose subconditions were UNEVALUABLE. A rule
   that was never asked is not a rule that failed.

4. Lower its own thresholds to produce a result. `min_n` and `min_t` are
   arguments with defaults; the defaults are the ones this repo's audit
   established.

It reports three sections — keep, drop, insufficient — because the third is the
one that prevents repeating the original mistake of drawing conclusions from a
sample too small to support them.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sqlite3
import sys

import recorder as RC

# Significance floor, carried from this repo's sibling-engine audit: one-sided
# t < -1.65 is p < 0.05. A negative mean without it is noise.
DEFAULT_MIN_N = 50
DEFAULT_MIN_T = -1.65


def _stats(values: list[float]) -> tuple[float, float, int]:
    """(mean, population stdev, n)."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0
    mean = sum(values) / n
    if n == 1:
        return mean, 0.0, 1
    var = sum((v - mean) ** 2 for v in values) / n
    return mean, math.sqrt(var), n


def _tstat(mean: float, sd: float, n: int) -> float | None:
    """t = mean / (sd / sqrt(n)). Population sd, matching the sibling audit."""
    if n < 2 or sd <= 0:
        return None
    return mean / (sd / math.sqrt(n))


def directed_rows(db: sqlite3.Connection) -> list[tuple]:
    """Every resolved, DIRECTED row: (id, pair, rules_met, rules_unevaluable,
    venues_live, horizon_bars, directed_return)."""
    sql = f"""
        SELECT v.id, v.pair, v.rules_met, v.rules_unevaluable, v.venues_live,
               v.horizon_bars,
               CASE WHEN v.direction > 0 THEN  v.forward_return
                    WHEN v.direction < 0 THEN -v.forward_return END AS dr
          FROM verdicts v
         WHERE v.resolved = 1
           AND v.forward_return IS NOT NULL
           AND v.direction != 0
    """
    return db.execute(sql).fetchall()


def by_rule(db: sqlite3.Connection) -> dict[str, list[float]]:
    """Directed returns grouped by which rule MET, over the CANDIDATE ledger.

    A row contributes to every rule it lists as MET. Rows are not double
    counted across rules because each rule's expectancy is computed
    independently — a row firing two rules legitimately appears in both.

    CANDIDATE ONLY. A row with candidate=0 has direction=0 by construction and
    is already excluded by the `direction != 0` filter, but the flag is made
    explicit here so the population under audit is never ambiguous. This is the
    population that makes promotion possible: before the candidate split, every
    row was direction=0 and this function could only ever return {}.
    """
    out: dict[str, list[float]] = {}
    for rid, pair, met_json, dr in candidate_directed_rows(db):
        try:
            met = json.loads(met_json)
        except (TypeError, ValueError):
            continue
        for rule in met:
            out.setdefault(str(rule), []).append(dr)
    return out


def candidate_directed_rows(db: sqlite3.Connection) -> list[tuple]:
    """Resolved, directed, candidate rows: (id, pair, rules_met, directed_return)."""
    sql = """
        SELECT id, pair, rules_met,
               CASE WHEN direction > 0 THEN  forward_return
                    WHEN direction < 0 THEN -forward_return END AS dr
          FROM verdicts
         WHERE resolved = 1
           AND forward_return IS NOT NULL
           AND direction != 0
           AND candidate = 1
    """
    return db.execute(sql).fetchall()


def by_pair(db: sqlite3.Connection) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for _rid, pair, _m, _u, _v, _h, dr in directed_rows(db):
        out.setdefault(pair, []).append(dr)
    return out


def by_hour(db: sqlite3.Connection) -> dict[int, list[float]]:
    """Directed returns bucketed by LOCAL hour, matching how the engine's
    other gates bucket time.

    Directed rows only — the same exclusion rule as everything else here.
    """
    sql = """
        SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER),
               CASE WHEN direction > 0 THEN  forward_return
                    WHEN direction < 0 THEN -forward_return END
          FROM verdicts
         WHERE resolved = 1 AND forward_return IS NOT NULL AND direction != 0
    """
    out: dict[int, list[float]] = {}
    for hr, dr in db.execute(sql):
        out.setdefault(int(hr), []).append(dr)
    return out


def audit(db: sqlite3.Connection, min_n: int = DEFAULT_MIN_N,
          min_t: float = DEFAULT_MIN_T) -> dict:
    """Produce the full audit: keep / drop / insufficient, per rule and pair."""
    c = RC.counts(db)
    n_directed = c["rows_directed_resolved"]

    if n_directed == 0:
        return {
            "verdict": "INSUFFICIENT DATA",
            "reason": "no resolved rows with a direction (direction != 0)",
            "counts": c,
            "rules": {}, "pairs": {}, "hours": {},
        }

    def classify(vals: list[float]) -> dict:
        mean, sd, n = _stats(vals)
        t = _tstat(mean, sd, n)
        rec = {"n": n, "mean": mean, "stdev": sd, "t": t,
               "hit_rate": (sum(1 for v in vals if v > 0) / n) if n else None}
        if n < min_n:
            rec["bucket"] = "insufficient"
            rec["why"] = f"n={n} < min_n={min_n}"
        elif t is not None and t <= min_t and mean < 0:
            rec["bucket"] = "drop"
            rec["why"] = f"mean {mean:+.6f} < 0 and t={t:.2f} <= {min_t} (n={n})"
        elif t is not None and t >= -min_t and mean > 0:
            rec["bucket"] = "keep"
            rec["why"] = f"mean {mean:+.6f} > 0 and t={t:.2f} (n={n})"
        else:
            rec["bucket"] = "inconclusive"
            rec["why"] = f"mean {mean:+.6f}, t={t if t is None else round(t, 2)} — not significant"
        return rec

    return {
        "verdict": "MEASURED",
        "counts": c,
        "min_n": min_n,
        "min_t": min_t,
        "rules": {r: classify(v) for r, v in sorted(by_rule(db).items())},
        "pairs": {p: classify(v) for p, v in sorted(by_pair(db).items())},
        "hours": {h: classify(v) for h, v in sorted(by_hour(db).items())},
    }


def render(a: dict) -> str:
    L: list[str] = []
    if a["verdict"] == "INSUFFICIENT DATA":
        L.append("=" * 74)
        L.append("EXPECTANCY AUDIT — INSUFFICIENT DATA")
        L.append("=" * 74)
        L.append(f"reason: {a['reason']}")
        c = a["counts"]
        L.append(f"  rows total            {c['rows_total']}")
        L.append(f"  rows resolved         {c['rows_resolved']}")
        L.append(f"  candidate + resolved  {c.get('rows_candidate_resolved', 0)}   <- enters math")
        L.append(f"  directed + resolved   {c['rows_directed_resolved']}   <- directed rows")
        L.append(f"  non-directional       {c['rows_non_directional']}   <- no direction at all")
        L.append(f"  awaiting resolution   {c['rows_total'] - c['rows_resolved']}   <- horizon not elapsed")
        L.append("")
        L.append("This is the correct day-one answer. Nothing here supports or")
        L.append("refutes any rule. Keep collecting; re-run at n >= 50 directed rows.")
        return "\n".join(L)

    L.append("=" * 74)
    L.append("EXPECTANCY AUDIT — MEASURED")
    L.append(f"  directed rows in math : {a['counts']['rows_directed_resolved']}")
    L.append(f"  excluded direction=0  : {a['counts']['rows_non_directional']}")
    L.append(f"  significance floor    : t <= {a['min_t']} (one-sided p<0.05), n >= {a['min_n']}")
    L.append("=" * 74)
    for label, key in (("PER RULE", "rules"), ("PER PAIR", "pairs"), ("PER LOCAL HOUR", "hours")):
        data = a[key]
        if not data:
            continue
        L.append(f"\n{label}")
        L.append(f"  {'KEY':10} {'BUCKET':13} {'N':>5} {'MEAN':>11} {'T':>8} {'HIT':>6}  WHY")
        L.append("  " + "-" * 70)
        for k, r in sorted(data.items(), key=lambda kv: (kv[1]["bucket"], str(kv[0]))):
            t = f"{r['t']:.2f}" if r["t"] is not None else "n/a"
            hit = f"{r['hit_rate']:.1%}" if r["hit_rate"] is not None else "n/a"
            L.append(f"  {str(k):10} {r['bucket']:13} {r['n']:>5} {r['mean']:>+11.6f} "
                     f"{t:>8} {hit:>6}  {r['why']}")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Audit scanner-engine rule expectancy")
    ap.add_argument("--db", default=str(pathlib.Path(__file__).with_name("scanner.db")))
    ap.add_argument("--min-n", type=int, default=DEFAULT_MIN_N)
    ap.add_argument("--min-t", type=float, default=DEFAULT_MIN_T)
    args = ap.parse_args()

    p = pathlib.Path(args.db)
    if not p.exists():
        print(f"no database at {p}\n"
              f"the recorder has not run yet — nothing to audit.")
        sys.exit(0)

    conn = RC.open_db(p)
    print(render(audit(conn, args.min_n, args.min_t)))