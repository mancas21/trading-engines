"""Read the /calls track record and answer the only question that matters:

    does a scored order-flow call have POSITIVE expectancy net of costs, and
    which components (Binance-only vs ATAS footprint/tape) carry the information?

Usage:
    python orderflow_calls_report.py                # summary: overall + per bucket
    python orderflow_calls_report.py components     # attribution: which features work
    python orderflow_calls_report.py open           # what is still pending
    python orderflow_calls_report.py --json out.json

Source of truth = _calls_log.jsonl (durable, one event per state change; the call
record is nested under "call"). The in-memory registry is NOT readable from outside
the bridge, and /calls/resolved_recent is capped, so the log is what we analyse.

Cost model MIRRORS the bridge exactly (bridge_server.py:1432):
    mid    = (entry_low + entry_high) / 2
    risk   = |mid - sl|
    cost_r = (cost_bps / 10000) * mid / risk
If this ever diverges, the cross-check against /calls/stats fails loudly.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "_calls_log.jsonl")
BRIDGE = os.environ.get("ATAS_BRIDGE_BASE", "http://127.0.0.1:8000")
COST_BPS = float(os.environ.get("CALLS_COST_BPS", "8.0"))

# The bridge only counts these three as traded (bridge_server.py:1430). Cancelled
# calls and never-activated ones are excluded, so they cannot flatter the record.
TRADED = ("t1_hit", "sl_hit", "expired_open")


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #

def load_calls() -> dict:
    """Merge the event stream into one record per call_id.

    The log is an event stream with THREE different shapes — do not assume a
    uniform schema:
        {"ev":"open",      "call": {<full geometry + snapshot>, "call_id": ...}}
        {"ev":"activated", "call_id":..., "price":..., "ts":...}
        {"ev":"resolved",  "call_id":..., "outcome":"t1_hit", "outcome_r":...,
                           "price":..., "mfe_r":..., "mae_r":..., "ts":...}
    Only `open` carries the snapshot; the outcome lives on the thin `resolved`
    event, so the two must be JOINED on call_id. Reading only `rec["call"]` finds
    zero traded calls even when the bridge reports several.
    """
    if not os.path.exists(LOG):
        return {}
    calls: dict = {}
    with open(LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue                       # torn final line (killed mid-write)
            ev = rec.get("ev")
            body = rec.get("call") or {}
            cid = rec.get("call_id") or body.get("call_id")
            if not cid:
                continue
            if ev == "open" and body:
                calls[cid] = dict(body)
                calls[cid]["call_id"] = cid
                continue
            c = calls.setdefault(cid, {"call_id": cid})
            ts = rec.get("ts")
            if rec.get("price") is not None:
                c["resolved_price" if ev == "resolved" else "last_price"] = rec["price"]
            for k in ("outcome", "outcome_r", "mfe_r", "mae_r"):
                if rec.get(k) is not None:
                    c[k] = rec[k]
            if ev == "activated":
                c["activated_ts"] = ts
                c.setdefault("status", "active")
            elif ev == "resolved":
                c["resolved_ts"] = ts
                c["status"] = rec.get("outcome") or c.get("status")
            elif ev == "verified" and rec.get("path_coverage"):
                # B7: exchange-truth first-touch marker joins the record.
                c["path_coverage"] = rec["path_coverage"]
    return calls


def entry_mid(c: dict) -> float:
    return (c["entry_low"] + c["entry_high"]) / 2.0


def risk(c: dict) -> float:
    return abs(entry_mid(c) - c["sl"]) or 1e-9


def cost_r(c: dict) -> float:
    """Taker cost + funding in R (mirrors bridge _cost_r incl. audit M-4).

    Funding uses the snapshot fundingRate over the actual hold
    (resolved_ts - opened_ts); longs pay positive funding. Env kill-switch:
    CALLS_FUNDING_INCLUDE=0."""
    m = entry_mid(c)
    r = risk(c)
    if m <= 0 or r <= 0:
        return 0.0
    base = (COST_BPS / 10000.0) * m / r
    fund = 0.0
    if os.environ.get("CALLS_FUNDING_INCLUDE", "1") == "1":
        try:
            fr = float((c.get("snapshot") or {}).get("funding_rate") or 0.0)
            hold_s = (c.get("resolved_ts") or 0) - (c.get("opened_ts") or 0)
            if fr and hold_s > 0:
                direction = 1.0 if c.get("direction") == "long" else -1.0
                fund = direction * fr * (hold_s / 28800.0) * m / r
        except Exception:  # noqa: BLE001
            fund = 0.0
    return base + fund


def gross(c: dict) -> float:
    return float(c.get("outcome_r") or 0.0)


def net(c: dict) -> float:
    return gross(c) - cost_r(c)


# --------------------------------------------------------------------------- #
# stats helpers (no scipy in this venv — keep them explicit and testable)
# --------------------------------------------------------------------------- #

def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def boot_ci(vals, n_boot: int = 2000, seed: int = 7):
    """Percentile bootstrap CI for the mean. Fixed seed: reproducible numbers."""
    if len(vals) < 2:
        return (None, None)
    rnd = random.Random(seed)
    n = len(vals)
    means = []
    for _ in range(n_boot):
        means.append(sum(vals[rnd.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return (means[int(0.05 * n_boot)], means[int(0.95 * n_boot)])


def auc(scores, labels):
    """Rank-based AUC (Mann-Whitney), ties handled. labels: 1=win, 0=loss."""
    pos = [s for s, l in zip(scores, labels) if l == 1]
    neg = [s for s, l in zip(scores, labels) if l == 0]
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    rsum = sum(r for r, l in zip(ranks, labels) if l == 1)
    n1, n0 = len(pos), len(neg)
    return (rsum - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def bucket_metrics(calls):
    n = len(calls)
    if n == 0:
        return {"n": 0}
    nets = [net(c) for c in calls]
    grosses = [gross(c) for c in calls]
    wins = [v for v in nets if v > 0]
    losses = [v for v in nets if v <= 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    k = len(wins)
    lo, hi = wilson(k, n)
    blo, bhi = boot_ci(nets)
    return {
        "n": n,
        "win_rate": k / n,
        "win_ci95": [lo, hi],
        "exp_r_net": sum(nets) / n,
        "exp_r_net_ci90": [blo, bhi],
        "exp_r_gross": sum(grosses) / n,
        "median_r_net": sorted(nets)[n // 2],
        "total_r_net": sum(nets),
        "avg_cost_r": sum(cost_r(c) for c in calls) / n,
        "avg_win_r": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss_r": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor_net": (gp / gl) if gl > 0 else None,
        "avg_mae_r": sum(float(c.get("mae_r") or 0.0) for c in calls) / n,
        "avg_mfe_r": sum(float(c.get("mfe_r") or 0.0) for c in calls) / n,
    }


def confidence(n: int) -> str:
    if n >= 100:
        return "high"
    if n >= 30:
        return "medium"
    if n >= 10:
        return "low"
    return "anecdote"


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #

def _fmt_row(label, m, width=34):
    if m.get("n", 0) == 0:
        return f"  {label:<{width}} n=0"
    ci = m["exp_r_net_ci90"]
    cis = (f"[{ci[0]:+.2f},{ci[1]:+.2f}]" if ci[0] is not None else "[--]")
    pf = m["profit_factor_net"]
    return (f"  {label:<{width}} n={m['n']:<4} win={m['win_rate']*100:5.1f}%  "
            f"expNET={m['exp_r_net']:+.3f}R {cis:<16} "
            f"expGROSS={m['exp_r_gross']:+.3f}R  PF={('%5.2f' % pf) if pf is not None else '  -- '}  "
            f"tot={m['total_r_net']:+.1f}R  [{confidence(m['n'])}]")


def view_summary(calls, min_n=1):
    traded = [c for c in calls.values() if c.get("status") in TRADED]
    op = [c for c in calls.values() if c.get("status") in ("open", "active")]
    cancelled = [c for c in calls.values() if c.get("status") == "cancelled"]

    print("=" * 132)
    print("ORDER-FLOW CALLS — TRACK RECORD  (source: _calls_log.jsonl)")
    print("=" * 132)
    print(f"  calls in log: {len(calls)}   resolved/traded: {len(traded)}   "
          f"open+active: {len(op)}   cancelled(excluded): {len(cancelled)}")
    print(f"  cost model: {COST_BPS} bps round trip -> cost_r = (bps/1e4)*mid/risk\n")

    m_all = bucket_metrics(traded)
    print(_fmt_row("ALL (traded)", m_all))

    # --- the comparison this whole exercise exists for
    print("\n  -- by snapshot provenance --")
    with_atas = [c for c in traded if (c.get("snapshot") or {}).get("atas_present")]
    no_snap = [c for c in traded if not (c.get("snapshot") or {})]
    binance_only = [c for c in traded
                    if (c.get("snapshot") or {}) and not c["snapshot"].get("atas_present")]
    print(_fmt_row("binance_only", bucket_metrics(binance_only)))
    print(_fmt_row("binance+atas (footprint/tape)", bucket_metrics(with_atas)))
    # Kept separate on purpose: a call with no snapshot at all has none of the raw
    # features, so lumping it into binance_only would pollute the A/B.
    print(_fmt_row("no snapshot (legacy/test, not in A/B)", bucket_metrics(no_snap)))
    if len(with_atas) and len(binance_only):
        a, b = bucket_metrics(with_atas), bucket_metrics(binance_only)
        print(f"    delta expNET: {a['exp_r_net'] - b['exp_r_net']:+.3f}R   "
              f"(NOT a controlled experiment: the ATAS subset is whichever symbol "
              f"happened to be on the chart)")

    # --- B7: exchange-truth split (polling artifacts set aside, never rewritten)
    def _pc(c):
        return c.get("path_coverage") or {}

    verified_clean = [c for c in traded
                      if _pc(c).get("exchange_verified") and not _pc(c).get("contradicts")]
    contradicted = [c for c in traded if _pc(c).get("contradicts")]
    print("\n  -- by exchange verification (B7) --")
    print(_fmt_row("verified clean", bucket_metrics(verified_clean)))
    print(_fmt_row("CONTRADICTED (kline first-touch disagrees)",
                   bucket_metrics(contradicted)))
    for c in contradicted:
        print(f"    ! {c.get('call_id')} {c.get('symbol')} {c.get('direction')} "
              f"logged={c.get('status')} first_touch={_pc(c).get('first_touch')}")

    # --- why a call has no footprint
    no_atas_reason = defaultdict(list)
    for c in traded:
        s = c.get("snapshot") or {}
        if not s.get("atas_present"):
            r = s.get("atas_status")
            no_atas_reason["(no snapshot at all)" if not s else
                           (r if r is not None else
                            "(field absent: call predates atas_status)")].append(c)
    if no_atas_reason:
        print("\n  -- why binance_only (atas_status) --")
        for k, v in sorted(no_atas_reason.items(), key=lambda kv: -len(kv[1])):
            print(f"    {k:<34} n={len(v)}")

    print("\n  -- by symbol --")
    by_sym = defaultdict(list)
    for c in traded:
        by_sym[c.get("symbol")].append(c)
    for s, v in sorted(by_sym.items(), key=lambda kv: -len(kv[1])):
        if len(v) >= min_n:
            print(_fmt_row(s, bucket_metrics(v)))

    print("\n  -- by outcome --")
    by_out = defaultdict(list)
    for c in traded:
        by_out[c.get("status")].append(c)
    for s, v in sorted(by_out.items(), key=lambda kv: -len(kv[1])):
        r = [net(c) for c in v]
        print(f"    {s:<16} n={len(v):<4} expNET={sum(r)/len(r):+.3f}R  "
              f"avgMFE={sum(float(c.get('mfe_r') or 0)/1 for c in v)/len(v):+.2f}R  "
              f"avgMAE={sum(float(c.get('mae_r') or 0)/1 for c in v)/len(v):+.2f}R")

    # --- time to resolution (operational, not edge)
    res = [float(c.get("resolved_ts") or 0) - float(c.get("opened_ts") or 0) for c in traded]
    res = [r for r in res if r > 0]
    if res:
        res.sort()
        print(f"\n  time to resolution: median {res[len(res)//2]/60:.0f} min, "
              f"p90 {res[int(0.9*(len(res)-1))]/60:.0f} min")

    print(f"\n  VERDICT SIGNAL: {len(traded)} traded. Need n>=30 for 'medium' confidence "
          f"and a CI that excludes 0.")
    if m_all.get("n", 0) >= 2:
        ci = m_all["exp_r_net_ci90"]
        if ci[0] is not None:
            if ci[0] > 0:
                print("    CI90 of net expectancy is ENTIRELY ABOVE 0 -> candidate edge, "
                      "keep collecting before acting.")
            elif ci[1] < 0:
                print("    CI90 of net expectancy is ENTIRELY BELOW 0 -> the rule LOSES "
                      "after costs on this sample.")
            else:
                print("    CI90 straddles 0 -> cannot distinguish from no edge yet.")
    return m_all


def view_components(calls):
    """Which logged features separate winners from losers? This is the analysis the
    snapshots exist for: the trigger is fixed, so we test the FEATURES offline."""
    traded = [c for c in calls.values() if c.get("status") in TRADED]
    print("=" * 118)
    print("COMPONENT ATTRIBUTION — does each logged feature separate winners from losers?")
    print("=" * 118)
    print("  AUC 0.50 = no information. >0.60 or <0.40 on n>=30 is worth a look.")
    print("  A component is only actionable if it beats 0.50 OUT of the sample that")
    print("  chose it — the trigger used these weights, so in-sample AUC is biased.\n")

    def analyse(subset, title):
        if len(subset) < 4:
            print(f"\n  {title}: n={len(subset)} — too few to rank (need >=10 to read, >=30 to trust)")
            return
        nets = [net(c) for c in subset]
        labels = [1 if v > 0 else 0 for v in nets]
        # collect every numeric feature appearing in the snapshots
        keys = set()
        for c in subset:
            s = c.get("snapshot") or {}
            for k, v in s.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    keys.add(k)
            for k, v in (s.get("components") or {}).items():
                if isinstance(v, (int, float)):
                    keys.add("comp." + k)
        rows = []
        for k in sorted(keys):
            vals, labs = [], []
            for c, lab in zip(subset, labels):
                s = c.get("snapshot") or {}
                v = (s.get("components") or {}).get(k[5:]) if k.startswith("comp.") else s.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals.append(float(v))
                    labs.append(lab)
            if len(vals) < len(subset) * 0.8 or len(set(vals)) < 2:
                continue                       # too sparse or constant to learn from
            a = auc(vals, labs)
            if a is None:
                continue
            w = [v for v, l in zip(vals, labs) if l == 1]
            l_ = [v for v, l in zip(vals, labs) if l == 0]
            rows.append((abs(a - 0.5), k, a, len(vals),
                         sum(w) / len(w) if w else 0.0,
                         sum(l_) / len(l_) if l_ else 0.0))
        rows.sort(reverse=True)
        print(f"\n  {title}  (n={len(subset)}, wins={sum(labels)})")
        print(f"    {'feature':<28} {'AUC':>6} {'n':>4}  {'avg if WIN':>11} {'avg if LOSS':>11}")
        for _, k, a, nn, mw, ml in rows[:18]:
            if nn < 30:
                flag = "  (n<30: noise)"
            elif a >= 0.60 or a <= 0.40:
                flag = "  <-- worth a look"
            else:
                flag = ""
            print(f"    {k:<28} {a:>6.3f} {nn:>4}  {mw:>11.4g} {ml:>11.4g}{flag}")

    traded = [c for c in calls.values() if c.get("status") in TRADED]
    if len(traded) < 2:
        print(f"\n  Only {len(traded)} traded call(s) — nothing to attribute yet.")
        return
    if len(traded) < 30:
        print(f"\n  !! n={len(traded)} traded. Every AUC below is NOISE: with this few")
        print(f"     resolved calls, several features separate perfectly BY CHANCE")
        print(f"     (that is what AUC 1.000 on n=7 means — not a discovery).")
    # ONLY resolved calls: an open call has outcome_r=None, which would score as a
    # loss and make every feature look predictive. That bug inflated the first
    # version of this table with meaningless AUC 1.000 rows.
    analyse(traded, "ALL traded calls (in-sample: the trigger used these weights)")
    with_atas = [c for c in traded if (c.get("snapshot") or {}).get("atas_present")]
    if with_atas:
        analyse(with_atas, "ATAS-enriched subset (footprint/tape features present)")
    else:
        print("\n  ATAS-enriched subset: EMPTY. No call has ever carried footprint.")
        print("  -> Open ATAS with the LiveExporter on the symbol you want enriched;")
        print("     the generator attaches automatically (--atas auto, default).")


def view_open(calls):
    # MFE/MAE for OPEN calls live only in the bridge's in-memory registry — the log
    # captured the open event, when both were still 0. Printing the log values would
    # show a permanent +0.00 column, so hydrate the open calls from /calls.
    live = {}
    try:
        with urllib.request.urlopen(f"{BRIDGE}/calls", timeout=8) as r:
            data = json.load(r)
        for c in (data.get("open") or []) + (data.get("resolved_recent") or []):
            if c.get("call_id"):
                live[c["call_id"]] = c
    except Exception as e:                     # noqa: BLE001
        print(f"  (bridge unreachable, showing log values: {e})")
    op = [c for c in calls.values() if c.get("status") in ("open", "active")]
    print("=" * 118)
    print(f"OPEN CALLS — {len(op)}   (MFE/MAE read live from the bridge)")
    print("=" * 118)
    print(f"  {'call_id':<26} {'sym':<10} {'dir':<6} {'status':<8} {'last':>12} "
          f"{'MFE':>7} {'MAE':>7}  {'atas':<24} age")
    for c in sorted(op, key=lambda x: x.get("opened_ts") or 0):
        lv = live.get(c.get("call_id"), {})
        s = (lv.get("snapshot") or c.get("snapshot") or {})
        tag = "+atas" if s.get("atas_present") else str(s.get("atas_status", ""))[:24]
        age = (time.time() - float(c.get("opened_ts") or 0)) / 60.0
        print(f"  {str(c.get('call_id'))[:26]:<26} {str(c.get('symbol'))[:10]:<10} "
              f"{str(c.get('direction'))[:6]:<6} {str(c.get('status'))[:8]:<8} "
              f"{float(lv.get('last_price') or c.get('last_price') or 0):>12.5f} "
              f"{float(lv.get('mfe_r') or 0):>+7.2f} {float(lv.get('mae_r') or 0):>+7.2f}  "
              f"{tag:<24} {age:.0f}m")
    print(f"\n  Note: calls only count toward the track record once they hit T1/SL or")
    print(f"  expire. Nothing is 'won' while open.")


def crosscheck(calls):
    """Assert our cost/bookkeeping matches the bridge's own aggregate."""
    try:
        with urllib.request.urlopen(f"{BRIDGE}/calls/stats", timeout=8) as r:
            st = json.load(r)
    except Exception as e:                     # noqa: BLE001
        print(f"\n  [cross-check] bridge unreachable: {e}")
        return
    traded = [c for c in calls.values() if c.get("status") in TRADED]
    mine = sum(net(c) for c in traded) / len(traded) if traded else None
    theirs = st.get("expectancy_r_net")
    n_theirs = st.get("n_traded")
    print(f"\n  [cross-check vs /calls/stats] bridge n_traded={n_theirs} "
          f"expNET={theirs}  |  report n={len(traded)} expNET="
          f"{('%.4f' % mine) if mine is not None else None}")
    # Compare n as well as expectancy: an empty report used to "pass" this check
    # silently while the bridge had trades, which is the worst kind of false OK.
    bad = []
    if n_theirs is not None and len(traded) != n_theirs:
        bad.append(f"n {len(traded)} != {n_theirs}")
    if mine is not None and theirs is not None and abs(mine - theirs) > 1e-6:
        # Compare at the precision the bridge PUBLISHES (4 dp): a difference of
        # 2e-5 is our unrounded float vs their round(...,4), not a cost-model
        # divergence. A real divergence is orders of magnitude larger.
        if round(mine, 4) != round(theirs, 4):
            bad.append(f"expNET {mine:.6f} != {theirs:.6f} (beyond published 4dp)")
    if bad:
        print(f"  !! MISMATCH ({'; '.join(bad)}) — the report and the bridge disagree. "
              f"Fix before trusting either.")
    else:
        print("  OK — report reconciles with the bridge (n and net expectancy match).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("view", nargs="?", default="summary",
                    choices=["summary", "components", "open", "all"])
    ap.add_argument("--json", default=None, help="write the summary metrics as JSON")
    a = ap.parse_args()

    calls = load_calls()
    if not calls:
        print(f"No calls in {LOG}. Is the generator running?  "
              f"(.\\orderflow-calls.ps1 start)")
        return 1

    out = {}
    if a.view in ("summary", "all"):
        out["summary"] = view_summary(calls)
        crosscheck(calls)
    if a.view in ("components", "all"):
        print()
        view_components(calls)
    if a.view in ("open", "all"):
        print()
        view_open(calls)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
