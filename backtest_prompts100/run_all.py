"""Corre las 100 estrategias y saca CSV + informe.

Uso: python run_all.py [--solo N1,N2...] [--coste 11|22|ambos] [--workers N] [--eq oos|full]

2026-11-06 (pipeline fast pass):
- run_one builds each signal ONCE and scores it at every cost (before, the
  whole indicator stack re-ran per cost -- two full builds per strategy).
- --workers N fans the strategies out over a process pool; results are
  re-ordered into STRATS order so the CSV is identical to the sequential run.
- --eq oos (default) stores only the OOS slice of each equity curve in
  eqs.npy (the pool would otherwise ship every full curve to the parent);
  --eq full keeps the old behavior (make_charts10.py needs full curves:
  regenerate charts with `python run_all.py --solo <top10 nums> --eq full`).
The numbers are unchanged: same engine, same conventions, same gates.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import btlib as B
import strategies as S

WORK = Path(__file__).parent
BARS_PYR = {"1h": 8760, "15m": 35040, "5m": 105120}


def finish(name, sym, tf, group, eq, trades, close, notes, cost_bps, split_frac=0.7):
    eq = np.asarray(eq, dtype=float)
    n = len(eq)
    split = int(n * split_frac)
    bpy = BARS_PYR[tf]
    full = B.segment_stats(eq, trades, 0, n, bpy)
    oos = B.segment_stats(eq, trades, split, n, bpy)
    bh_full = float(close[-1] / close[0] - 1)
    bh_oos = float(close[-1] / close[split] - 1)
    full["excess"] = full["net"] - bh_full
    oos["excess"] = oos["net"] - bh_oos
    gate = bool(full["pf"] > 1.2 and full["n"] >= 30 and full["net"] > 0)
    oos_ok = bool(oos["pf"] > 1.2 and oos["net"] > 0 and oos["n"] >= 30)
    return dict(name=name, sym=sym, tf=tf, group=group, cost_bps=cost_bps,
                net=full["net"], mdd=full["mdd"], pf=full["pf"], n=full["n"],
                wr=full["wr"], sharpe=full["sharpe"], bh_full=bh_full,
                excess=full["excess"],
                oos_net=oos["net"], oos_pf=oos["pf"], oos_n=oos["n"],
                oos_mdd=oos["mdd"], oos_sharpe=oos["sharpe"], oos_excess=oos["excess"],
                gate=gate, oos_ok=oos_ok, eq=eq, split=split, notes=notes or "")


def run_one(st, costs):
    """Build the signal ONCE, score it at every cost.

    Before 2026-11-06 this was called once per (strategy, cost) and re-ran the
    whole indicator stack per cost -- double work for identical signals. The
    evaluation per cost is unchanged (B.evaluate / S.run_xs / S.run_flip with
    the same conventions), so the rows are identical to the old per-cost path.
    """
    import inspect
    no, name, sym, tf, group = st["no"], st["name"], st["sym"], st["tf"], st["group"]
    needs_df = "df" in inspect.signature(st["build"]).parameters
    built = st["build"](B.load(sym, tf)) if needs_df else st["build"]()
    rows = []
    for cost_rt in costs:
        if isinstance(built, dict) and built.get("kind") == "xs":
            eq, tr = S.run_xs(built["syms"], built["tf"], built["ret_n"], built["rebal_n"],
                              lowvol=built.get("lowvol", False),
                              vol_scaled=built.get("vol_scaled", False),
                              cost_side=cost_rt / 2)
            closes = pd.concat([B.load(s, built["tf"])["close"].pct_change().fillna(0) + 1
                                for s in built["syms"]], axis=1).dropna()
            bh = (closes.prod(axis=1) ** (1 / closes.shape[1])).values
            rows.append(finish(name, sym, tf, group, eq, tr, bh, st["notes"], cost_rt))
            continue
        if isinstance(built, dict) and built.get("kind") == "flip":
            df = B.load(sym, tf)
            eq, tr = S.run_flip(df, built["pos"], built["weight"], cost_side=cost_rt / 2)
            rows.append(finish(name, sym, tf, group, eq, tr, df["close"].values,
                               st["notes"], cost_rt))
            continue
        if isinstance(built, tuple):  # (df, sig) pares
            df, sig = built
            res = B.evaluate(df, sig, sym, tf, name, group, cost_rt=cost_rt)
            rows.append(pack(res, st, cost_rt))
            continue
        df = B.load(sym, tf)
        sig = built
        funding = B.funding_series(sym) if group == "funding" else None
        res = B.evaluate(df, sig, sym, tf, name, group, cost_rt=cost_rt, funding=funding)
        rows.append(pack(res, st, cost_rt))
    return rows


def pack(res, st, cost_rt):
    f, o = res["full"], res["oos"]
    gate = bool(f["pf"] > 1.2 and f["n"] >= 30 and f["net"] > 0)
    oos_ok = bool(o["pf"] > 1.2 and o["net"] > 0 and o["n"] >= 30)
    return dict(name=st["name"], sym=st["sym"], tf=st["tf"], group=st["group"],
                cost_bps=cost_rt,
                net=f["net"], mdd=f["mdd"], pf=f["pf"], n=f["n"], wr=f["wr"],
                sharpe=f["sharpe"], bh_full=res["bh_full"], excess=f["excess"],
                oos_net=o["net"], oos_pf=o["pf"], oos_n=o["n"], oos_mdd=o["mdd"],
                oos_sharpe=o["sharpe"], oos_excess=o["excess"],
                gate=gate, oos_ok=oos_ok, eq=res["eq"], split=res["split"],
                notes=st["notes"] or "")


def _worker(job):
    """One strategy in a worker process: run at every cost, trim the equity
    curve per --eq, return rows picklable."""
    no, costs, eq_mode = job
    st = next(s for s in S.STRATS if s["no"] == no)
    try:
        rows = run_one(st, costs)
    except Exception as e:
        return no, [{"no": no, "error": f"{type(e).__name__}: {e}"}]
    for r in rows:
        eq = r.pop("eq", None)
        split = r.pop("split", None)
        if eq is not None and eq_mode == "oos":
            eq = eq[split:]
        r["eq"] = eq
    return no, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo", default="")
    ap.add_argument("--coste", default="ambos")
    ap.add_argument("--workers", type=int, default=1,
                    help=">1: strategy-level process pool (results in STRATS order)")
    ap.add_argument("--eq", default="oos", choices=["oos", "full"],
                    help="equity curves kept in eqs.npy: OOS slice (default, "
                         "pool-friendly) or full (make_charts10.py needs full)")
    args = ap.parse_args()
    only = set(int(x) for x in args.solo.split(",") if x.strip()) if args.solo else None
    costs = [0.0011, 0.0022] if args.coste == "ambos" else \
        ([0.0011] if args.coste == "11" else [0.0022])

    jobs = [(st["no"], costs, args.eq) for st in S.STRATS
            if not only or st["no"] in only]
    by_no = {st["no"]: st for st in S.STRATS}
    if args.workers > 1 and len(jobs) > 1:
        from concurrent.futures import ProcessPoolExecutor
        print(f"pool: {len(jobs)} estrategias x {len(costs)} costes, "
              f"workers={min(args.workers, len(jobs))}", flush=True)
        collected = {}
        with ProcessPoolExecutor(max_workers=min(args.workers, len(jobs))) as ex:
            for no, rows in ex.map(_worker, jobs):
                collected[no] = rows
        ordered = [(no, collected.get(no, [])) for no, _costs, _eq in jobs]
    else:
        ordered = [_worker(j) for j in jobs]

    rows_all, eqs = [], {}
    for no, rows in ordered:
        st = by_no[no]
        for r in rows:
            if r.get("error"):
                print(f"[{no:3d}] {st['name']:34s} ERROR: {r['error']}")
                continue
            r["no"] = no
            rows_all.append(r)
            eqs[(no, r["cost_bps"])] = r.pop("eq")
            f = r
            print(f"[{no:3d}] {st['name']:34s} {f['sym']:9s} {f['tf']:3s} "
                  f"cost={int(f['cost_bps']*1e4):2d}bp net={f['net']*100:8.1f}% "
                  f"dd={f['mdd']*100:6.1f}% pf={f['pf']:5.2f} n={f['n']:4d} | "
                  f"OOS pf={f['oos_pf']:5.2f} net={f['oos_net']*100:8.1f}% "
                  f"n={f['oos_n']:4d} "
                  f"{'GATE' if f['gate'] else ''}{' OOS_OK' if f['oos_ok'] else ''}")

    df = pd.DataFrame(rows_all)
    cols = ["no", "name", "sym", "tf", "group", "cost_bps", "net", "mdd", "pf", "n",
            "wr", "sharpe", "bh_full", "excess", "oos_net", "oos_pf", "oos_n",
            "oos_mdd", "oos_sharpe", "oos_excess", "gate", "oos_ok", "notes"]
    df = df[cols]
    df.to_csv(WORK / "results_100.csv", index=False)
    n11 = df[np.isclose(df.cost_bps, 0.0011)]
    print("\n=== resumen coste 11 bps ===")
    print(f"estrategias: {len(n11)} | GATE (PF>1.2, n>=30, net>0): {int(n11.gate.sum())} "
          f"| ademas OOS PF>1.2 y net>0: {int((n11.gate & n11.oos_ok).sum())}")
    df.to_pickle(WORK / "results_100.pkl")
    np.save(WORK / "eqs.npy", np.array(list(eqs.items()), dtype=object), allow_pickle=True)
    print("guardado results_100.csv / results_100.pkl")


if __name__ == "__main__":
    main()
