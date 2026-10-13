"""Expectancy auditor — the tool that decides which rules earn their place.

Every rule in `rules.json` is a HYPOTHESIS until this has measured it. Day one
the honest answer is "insufficient data", and this module is built so that
answer is hard to argue with and easy to produce.

THE SIX THINGS THIS WILL NOT DO
===============================
1. Treat `direction = 0` as bearish. Those rows are excluded via
   `recorder.DIRECTED_RETURN_SQL`. In the sibling engine this exact mistake
   (63% of rows) inverted the entire outcome gate.

2. Conclude anything from a negative mean alone. At n in the dozens, a negative
   mean is common by chance. A rule is only recommended for suppression when
   `mean < 0` AND `n_eff >= min_n` AND `t <= -threshold`.

3. Report a verdict for a rule whose subconditions were UNEVALUABLE. A rule
   that was never asked is not a rule that failed.

4. Lower its own thresholds to produce a result. `min_n` and the significance
   level are arguments with defaults; the defaults are the ones this repo's
   audit established.

5. Count overlapping rows as independent evidence. See OVERLAP below.

6. Quote gross returns. Every expectancy reported here is NET of a cost.

OVERLAP - WHY n IS NOT THE SAMPLE SIZE
=====================================
The loop polls every ~12s against a 5-minute horizon, so roughly 25
consecutive rows share one underlying price move. Treating them as 25
independent observations understates the standard error by about sqrt(25) and
inflates |t| by the same factor.

Measured on the running corpus before this was fixed:

    pair       rows  distinct_windows   t_naive   t_effective
    SOL/USD      167               57     -1.59         -1.24
    XAU/USD       54               30     -2.80         -1.65
    TOTAL        721              305     -2.13

XAU's -2.80, the single headline "significant loser" in the book, is -1.65 at
best once overlap is accounted for — and that was on top of a fill/exit venue
mismatch that made the return itself an artifact (see `recorder.py` defect 4).

The correction is the standard lag-1 effective sample size:

    n_eff = n * (1 - r1) / (1 + r1),   r1 = lag-1 autocorrelation

floored at 1, capped at n. `min_n` is applied to `n_eff`, NOT to `n`. A series
with r1 = 0.6 — genuinely overlapping — gets about 40% of the nominal sample,
which is the honest reading.

COSTS
=====
Every number reported is NET. The gross figure is reported alongside, because
"the signal has no edge" and "the signal has an edge smaller than the cost of
trading it" are different findings, and the difference is the entire point of
a scanner.

Measured on the corpus as previously audited: pooled gross mean was -0.90bps
against a round trip of ~11bps. Every cell in that report sat an order of
magnitude below the cost of acting on it, which no amount of significance
testing makes tradable.

MULTIPLE COMPARISONS
====================
This module grades a rule and eight pairs in one pass (the PER LOCAL HOUR
cells were retired 2026-10-11: hour-of-day buckets on a sample spanning
days measure the session, not the rule, and they diluted the Bonferroni
threshold for every real cell. Re-enable only once the ledger spans
weeks). At a
nominal one-sided 5% that is ~20 tests, and roughly one false positive is the
expected yield at random. The threshold is Bonferroni-corrected across the
cells actually tested, and `n_tests` is reported so the haircut is visible.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sqlite3
import sys
from statistics import NormalDist, median

import recorder as RC

# ── cost model ──────────────────────────────────────────────────────────────
# Provenance matters more than the number here: a cost assumption with no
# stated source is how a negative-expectancy book stays in the book.
#
#   Binance USDT-M taker fee     5.0 bps/side   (0.05%, the published base rate)
#   slippage allowance           0.5 bps/side
#   spread cross                 the row's OWN recorded fill_spread_bps
#
# The fee+slip component is 11.0 bps round trip, which is the figure this
# repo's sibling-engine audit already used (STATUS.md, 2026-10-06, "0.11%
# round trip"), so the two engines stay comparable.
#
# The spread term is ONE full spread, not two. Returns here are measured
# mid-to-mid; a real fill crosses half the spread on entry and half on exit,
# which is one spread in total. So a 1bps venue gives a 12bps round trip.
#
# Both are retail TAKER rates. A strategy whose edge is smaller than this is
# not a strategy, it is a curiosity.
FEE_BPS_PER_SIDE = 5.0
SLIPPAGE_BPS_PER_SIDE = 0.5
DEFAULT_SPREAD_BPS = 1.0        # used only when a row predates the column

# Significance level for the whole pass, before the Bonferroni haircut.
ALPHA = 0.05

# Retained for callers that read these. `min_t` is now DERIVED from the number
# of cells tested rather than fixed, so the constant is the uncorrected
# one-cell threshold and is only what the derivation starts from.
DEFAULT_MIN_N = 50
DEFAULT_MIN_T = -1.65


def round_trip_bps(fill_spread_bps: float | None = None) -> float:
    """Round-trip cost in bps for one row, spread measured where possible."""
    spread = DEFAULT_SPREAD_BPS if fill_spread_bps is None else float(fill_spread_bps)
    return 2.0 * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) + spread


def _bonferroni_t(n_tests: int, alpha: float = ALPHA) -> float:
    """One-sided t threshold corrected for `n_tests` simultaneous comparisons.

    With 20 cells graded at a nominal 5%, the expected number of false
    positives at random is ~1. Grading them at an uncorrected 1.65 therefore
    produces findings that are indistinguishable from luck. This widens the
    bar to the value that holds the FAMILY-wise error rate at `alpha`.
    """
    m = max(1, int(n_tests))
    if m == 1:
        return NormalDist().inv_cdf(1.0 - alpha)
    return NormalDist().inv_cdf(1.0 - alpha / m)


# ── population ──────────────────────────────────────────────────────────────

_ROWS_SQL = f"""
    SELECT v.id, v.pair, v.ts, v.rules_met, v.rules_unevaluable, v.venues_live,
           v.horizon_bars, v.candidate, v.fill_spread_bps, v.exit_close_ms,
           CASE WHEN v.direction > 0 THEN  v.forward_return
                WHEN v.direction < 0 THEN -v.forward_return END AS dr
      FROM verdicts v
     WHERE v.resolved = 1
       AND v.forward_return IS NOT NULL
       AND v.direction != 0
       AND {RC.PRICE_BASIS_SQL}
     ORDER BY v.id
"""


def _directed(db: sqlite3.Connection) -> list[dict]:
    """Every row that may enter expectancy math, as dicts.

    Three structural exclusions, all in SQL so none depends on remembering to
    skip something:

      `resolved = 1`            the horizon bar has closed
      `direction != 0`          a side was actually claimed
      `price_basis_ok = 1`      the fill venue is the exit venue

    The third is the newest. Rows recorded before the fill/exit fix measure a
    venue spread rather than a signal and are excluded on principle, not
    because their numbers look wrong.
    """
    # A cursor-local row_factory rather than mutating the shared connection:
    # `recorder.open_db` hands the same connection to the scan loop, the HTTP
    # surface and the tests, and several of those read rows positionally.
    cur = db.cursor()
    cur.row_factory = sqlite3.Row
    # The ORDER BY is not cosmetic: `autocorr1` assumes its input is in time
    # order, and without an explicit order the lag-1 autocorrelation (and
    # therefore every n_eff and t in this module) depends on whatever order
    # SQLite's planner happens to return rows in. `id` is AUTOINCREMENT, so id
    # order IS insertion order IS ts order.
    cur.execute(_ROWS_SQL)
    out = []
    for r in cur.fetchall():
        d = dict(r)
        d["cost"] = round_trip_bps(d.get("fill_spread_bps"))
        d["net"] = d["dr"] - d["cost"] / 10_000.0
        d["hz"] = (None if d.get("exit_close_ms") is None
                   else d["exit_close_ms"] / 1000.0 - d["ts"])
        out.append(d)
    return out


def directed_rows(db: sqlite3.Connection) -> list[tuple]:
    """Back-compatible tuple view: (id, pair, met, unevaluable, venues, horizon, dr)."""
    return [(r["id"], r["pair"], r["rules_met"], r["rules_unevaluable"],
             r["venues_live"], r["horizon_bars"], r["dr"])
            for r in _directed(db)]


def candidate_directed_rows(db: sqlite3.Connection) -> list[tuple]:
    """Resolved, directed, candidate rows: (id, pair, rules_met, directed_return)."""
    return [(r["id"], r["pair"], r["rules_met"], r["dr"])
            for r in _directed(db) if r["candidate"]]


# ── grouping ────────────────────────────────────────────────────────────────

def _by_rule(rows: list[dict], key: str = "net") -> dict[str, list[float]]:
    """Directed returns grouped by which rule MET, over the CANDIDATE ledger.

    A row contributes to every rule it lists as MET. Rows are not double
    counted across rules because each rule's expectancy is computed
    independently — a row firing two rules legitimately appears in both.
    """
    out: dict[str, list[float]] = {}
    for r in rows:
        if not r["candidate"]:
            continue
        try:
            met = json.loads(r["rules_met"] or "[]")
        except (TypeError, ValueError):
            continue
        for rule in met:
            out.setdefault(str(rule), []).append(r[key])
    return out


def _by_pair(rows: list[dict], key: str = "net") -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in rows:
        out.setdefault(r["pair"], []).append(r[key])
    return out


# The public accessors below report the GROSS directed return — the raw effect
# of the direction flip, which is what they exist to expose and what the tests
# pin. Cost enters in `audit()`, where the bucketing happens: a signal's gross
# mean and its net expectancy are different questions and conflating them hides
# "there is a tiny edge, and it is smaller than the fee".
def by_rule(db: sqlite3.Connection) -> dict[str, list[float]]:
    return _by_rule(_directed(db), "dr")


def by_pair(db: sqlite3.Connection) -> dict[str, list[float]]:
    return _by_pair(_directed(db), "dr")


# ── statistics ──────────────────────────────────────────────────────────────

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


def autocorr1(values: list[float]) -> float:
    """Lag-1 autocorrelation. 0.0 when it is undefined (n < 3 or no spread)."""
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    denom = sum((v - mean) ** 2 for v in values)
    if denom <= 0:
        return 0.0
    num = sum((values[i] - mean) * (values[i - 1] - mean) for i in range(1, n))
    r = num / denom
    return max(-0.99, min(0.99, r))


def effective_n(values: list[float]) -> tuple[float, float]:
    """(n_eff, r1) for a possibly-overlapping sample.

    The lag-1 correction is the standard first-order fix for overlapping
    observations: if consecutive samples share `r1` of their variance, the
    information content is `n * (1-r1)/(1+r1)` independent draws, not n.
    """
    n = len(values)
    r1 = autocorr1(values)
    if n == 0:
        return 0.0, 0.0
    n_eff = n * (1.0 - r1) / (1.0 + r1)
    return max(1.0, min(float(n), n_eff)), r1


def _tstat(mean: float, sd: float, n: float) -> float | None:
    """t = mean / (sd / sqrt(n)). Undefined when there is no spread."""
    if n < 2 or sd <= 0:
        return None
    return mean / (sd / math.sqrt(n))


def _tstat_effective(mean: float, sd: float, n_eff: float) -> float | None:
    return _tstat(mean, sd, n_eff)


# ── the audit ───────────────────────────────────────────────────────────────

def audit(db: sqlite3.Connection, min_n: int = DEFAULT_MIN_N,
          min_t: float | None = None) -> dict:
    """Produce the full audit: keep / drop / insufficient, per rule and pair.

    `min_t` may be passed to override the DERIVED threshold. The default is
    None because the correct threshold depends on how many cells this pass
    grades, and hard-coding it is how a Bonferroni correction gets silently
    dropped. Pass a number only when you are deliberately testing one cell.
    """
    c = RC.counts(db)
    rows = _directed(db)
    n_directed = c["rows_directed_resolved"]

    if n_directed == 0 or not rows:
        return {
            "verdict": "INSUFFICIENT DATA",
            "reason": "no resolved rows with a direction and a trustworthy price basis",
            "counts": c,
            "rules": {}, "pairs": {},
        }

    gross_rules = _by_rule(rows, "dr")
    rules = _by_rule(rows, "net")
    pairs = _by_pair(rows, "net")

    # The number of simultaneous comparisons actually being made. This is what
    # the haircut is computed against; reporting it keeps the correction
    # auditable rather than a hidden constant.
    n_tests = max(1, len(rules) + len(pairs))
    threshold = min_t if min_t is not None else _bonferroni_t(n_tests)

    def classify(gross: list[float], net: list[float],
                 hz: list[float | None] | None = None) -> dict:
        mean_g, sd, n = _stats(net)
        mean_raw, _, _ = _stats(gross)
        n_eff, r1 = effective_n(net)
        t_eff = _tstat_effective(mean_g, sd, n_eff)
        t_naive = _tstat(mean_g, sd, n)
        # Realized horizon: the median of (exit_close_ms - ts) over rows that
        # recorded an exit bar. This is what the cell actually measured — the
        # horizon label is the intent, this is the fact. Rows resolved before
        # the column existed report None and are skipped.
        known_hz = [h for h in (hz or []) if h is not None]
        hz_med = median(known_hz) if known_hz else None
        rec = {"n": n, "n_eff": round(n_eff, 1), "autocorr": round(r1, 3),
               "mean": mean_g, "mean_gross": mean_raw, "stdev": sd,
               "t": t_eff, "t_naive": t_naive,
               "hit_rate": (sum(1 for v in net if v > 0) / n) if n else None,
               "hit_gross": (sum(1 for v in gross if v > 0) / len(gross))
                            if gross else None,
               "hz_med_s": hz_med}
        if n_eff < min_n:
            rec["bucket"] = "insufficient"
            rec["why"] = (f"n_eff={n_eff:.1f} < min_n={min_n} "
                          f"(n={n}, autocorr={r1:+.2f})")
        elif t_eff is not None and t_eff <= -threshold and mean_g < 0:
            rec["bucket"] = "drop"
            rec["why"] = (f"net {mean_g:+.6f} < 0 and t={t_eff:.2f} <= "
                          f"{-threshold:.2f} (n_eff={n_eff:.1f}, "
                          f"{n_tests} cells Bonferroni)")
        elif t_eff is not None and t_eff >= threshold and mean_g > 0:
            rec["bucket"] = "keep"
            rec["why"] = (f"net {mean_g:+.6f} > 0 and t={t_eff:.2f} >= "
                          f"{-threshold:.2f} (n_eff={n_eff:.1f}, "
                          f"{n_tests} cells Bonferroni)")
        else:
            rec["bucket"] = "inconclusive"
            rec["why"] = (f"net {mean_g:+.6f}, t="
                          f"{t_eff if t_eff is None else round(t_eff, 2)} "
                          f"vs threshold {threshold:.2f} — not significant "
                          f"(n_eff={n_eff:.1f})")
        return rec

    def classify_all(gross_map: dict, net_map: dict, hz_map: dict | None = None):
        hz_map = hz_map or {}
        return {k: classify(gross_map.get(k, []), v, hz_map.get(k))
                for k, v in sorted(net_map.items())}

    hz_rules = _by_rule(rows, "hz")
    hz_pairs = _by_pair(rows, "hz")

    return {
        "verdict": "MEASURED",
        "counts": c,
        "min_n": min_n,
        "min_t": threshold,
        "n_tests": n_tests,
        "alpha": ALPHA,
        "cost_bps_rt": round_trip_bps(),
        "rules": classify_all(gross_rules, rules, hz_rules),
        "pairs": classify_all(_by_pair(rows, "dr"), pairs, hz_pairs),
    }


def render(a: dict) -> str:
    L: list[str] = []
    if a["verdict"] == "INSUFFICIENT DATA":
        L.append("=" * 74)
        L.append("EXPECTANCY AUDIT — INSUFFICIENT DATA")
        L.append("  reglas de scanner-engine/rules.json (namespace: scanner)")
        L.append("=" * 74)
        L.append(f"reason: {a['reason']}")
        c = a["counts"]
        L.append(f"  rows total            {c['rows_total']}")
        L.append(f"  rows resolved         {c['rows_resolved']}")
        L.append(f"  auditable             {c.get('rows_auditable', 0)}   <- enters math")
        L.append(f"  non-directional       {c['rows_non_directional']}   <- no direction at all")
        L.append(f"  bad price basis       {c.get('rows_bad_price_basis', 0)}   "
                 f"<- fill venue != exit venue")
        L.append(f"  awaiting resolution   {c['rows_total'] - c['rows_resolved']}   <- horizon not elapsed")
        L.append("")
        L.append("This is the correct day-one answer. Nothing here supports or")
        L.append("refutes any rule. Keep collecting; re-run at n_eff >= 50.")
        return "\n".join(L)

    L.append("=" * 74)
    L.append("EXPECTANCY AUDIT — MEASURED  (NET of cost, overlap-corrected)")
    L.append("  reglas citadas aqui son las del scanner-engine/rules.json "
             "(namespace: scanner) — NO confundir con atas-bridge/rules.json")
    L.append(f"  rows in math          {a['counts']['rows_auditable']}")
    L.append(f"  excluded direction=0  {a['counts']['rows_non_directional']}")
    L.append(f"  excluded bad basis    {a['counts'].get('rows_bad_price_basis', 0)}"
             "   <- fill venue != exit venue")
    L.append(f"  round-trip cost       {a['cost_bps_rt']:.2f} bps "
             f"(fee {FEE_BPS_PER_SIDE}+slip {SLIPPAGE_BPS_PER_SIDE} per side, x2, + spread)")
    L.append(f"  cells graded          {a['n_tests']}  -> Bonferroni "
             f"t = {a['min_t']:.2f} at alpha={a['alpha']}")
    L.append(f"  min_n on EFFECTIVE n  {a['min_n']}  (overlap-corrected)")
    L.append("  columns               HIT_N = hit rate on NET returns (post-cost);")
    L.append("                        HIT_G = on GROSS returns (the signal itself);")
    L.append("                        HZ(s) = median REALIZED horizon (exit_close_ms - ts),")
    L.append("                        what the cell actually measured, not the label")
    L.append("=" * 74)
    for label, key in (("PER RULE", "rules"), ("PER PAIR", "pairs")):
        data = a.get(key) or {}
        if not data:
            continue
        L.append(f"\n{label}")
        L.append(f"  {'KEY':10} {'BUCKET':13} {'N':>5} {'N_EFF':>6} {'R1':>6} "
                 f"{'NET':>11} {'GROSS':>11} {'T':>7} {'T_NAIVE':>8} "
                 f"{'HIT_N':>6} {'HIT_G':>6} {'HZ(s)':>7}  WHY")
        L.append("  " + "-" * 110)
        for k, r in sorted(data.items(), key=lambda kv: (kv[1]["bucket"], str(kv[0]))):
            t = f"{r['t']:.2f}" if r["t"] is not None else "n/a"
            tn = f"{r['t_naive']:.2f}" if r["t_naive"] is not None else "n/a"
            hit = f"{r['hit_rate']:.1%}" if r["hit_rate"] is not None else "n/a"
            hitg = f"{r['hit_gross']:.1%}" if r.get("hit_gross") is not None else "n/a"
            hz = f"{r['hz_med_s']:.0f}" if r.get("hz_med_s") is not None else "n/a"
            L.append(f"  {str(k):10} {r['bucket']:13} {r['n']:>5} {r['n_eff']:>6.1f} "
                     f"{r['autocorr']:>+6.2f} {r['mean']:>+11.6f} {r['mean_gross']:>+11.6f} "
                     f"{t:>7} {tn:>8} {hit:>6} {hitg:>6} {hz:>7}  {r['why']}")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Audit scanner-engine rule expectancy")
    # The database lives under data/. Pointing the default at the module's own
    # directory made `python expectancy.py` print "the recorder has not run yet"
    # on an engine that had been recording for 42 hours, which is how a working
    # audit gets mistaken for a broken one.
    ap.add_argument("--db", default=str(pathlib.Path(__file__).resolve().parent
                                        / "data" / "scanner.db"))
    ap.add_argument("--min-n", type=int, default=DEFAULT_MIN_N)
    ap.add_argument("--min-t", type=float, default=None,
                    help="override the Bonferroni-derived threshold")
    args = ap.parse_args()

    p = pathlib.Path(args.db)
    if not p.exists():
        print(f"no database at {p}\n"
              f"the recorder has not run yet — nothing to audit.")
        sys.exit(0)

    conn = RC.open_db(p)
    print(render(audit(conn, args.min_n, args.min_t)))