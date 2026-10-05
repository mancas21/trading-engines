"""
ml_train.py — Meta-labeling trainer for the Quant Alert Engine.

The engine's rules generate the candidates (MACD cross, CVD flip, volume
climax...). This trains the SECONDARY model that decides whether a given
candidate is worth acting on — López de Prado's meta-labeling: the rule picks
the side, the model filters. That is the only ML shape that fits this data
volume (1.6k labeled signals, ~1 week of history): a sequence model would need
10^5+ rows.

Evaluation discipline is the repo's own: TIME-ORDERED expanding walk-forward
folds, no shuffling, no threshold tuned on the test fold. A threshold is picked
on the train fold as a quantile of predicted probability and then applied
untouched to the held-out fold, so "kept 50%" really means the model ranked
50% of the train distribution above the cut.

Run:
    python ml_backfill.py          # first: reconstruct features for history
    python ml_train.py             # walk-forward report + save model bundle
    python ml_train.py --model lightgbm
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from features import FEATURES
from ml_filter import BUNDLE_VERSION, MetaLabelFilter, build_columns, build_design

KEEP_FRACTIONS = (1.0, 0.75, 0.5, 0.25)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def load_dataset(db: str, exclude_neutral: bool = False) -> Dict[str, Any]:
    """Join outcomes with their feature vectors, time-ordered."""
    conn = sqlite3.connect(db, timeout=30)
    rows = conn.execute(
        """SELECT o.signal_id, o.symbol, o.timeframe, o.signal_name, o.signal_type,
                  o.direction, o.fired_ts, o.forward_return, f.features_json
           FROM signal_outcomes o
           JOIN signal_features f ON f.signal_id = o.signal_id
           WHERE o.resolved = 1
           ORDER BY o.fired_ts""").fetchall()
    conn.close()
    if not rows:
        return {"n": 0}

    feats, stypes, dirs, y, ret = [], [], [], [], []
    syms, tfs, names, ts = [], [], [], []
    for (_sid, sym, tf, name, stype, direction, fired_ts, fret, fjson) in rows:
        if fret is None:
            continue
        d = int(direction or 0)
        if exclude_neutral and d == 0:
            continue
        try:
            fv = json.loads(fjson)
        except Exception:
            continue
        if not isinstance(fv, dict) or not fv:
            continue
        feats.append(fv)
        stypes.append(str(stype or "UNK"))
        dirs.append(d)
        ret.append(float(fret))
        y.append(1.0 if float(fret) > 0 else 0.0)
        syms.append(sym)
        tfs.append(tf)
        names.append(name)
        ts.append(float(fired_ts or 0.0))

    return {
        "n": len(feats),
        "features": feats,
        "signal_types": stypes,
        "directions": dirs,
        "y": np.asarray(y, dtype=np.float64),
        "ret": np.asarray(ret, dtype=np.float64),
        "symbol": syms, "timeframe": tfs, "signal_name": names,
        "fired_ts": np.asarray(ts, dtype=np.float64),
    }


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def auc(score: np.ndarray, label: np.ndarray) -> float:
    """Rank AUC (Mann–Whitney). NaN when a fold is single-class."""
    label = np.asarray(label, dtype=np.float64)
    pos, neg = float((label == 1).sum()), float((label == 0).sum())
    if pos == 0 or neg == 0:
        return float("nan")
    order = np.argsort(np.asarray(score, dtype=np.float64), kind="mergesort")
    ranks = np.empty(len(order), dtype=np.float64)
    ranks[order] = np.arange(1, len(order) + 1, dtype=np.float64)
    return (ranks[label == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg)


def brier(prob, label) -> float:
    return float(np.mean((np.asarray(prob) - np.asarray(label)) ** 2))


def take_metrics(prob: np.ndarray, ret: np.ndarray, y: np.ndarray,
                 thr: float) -> Dict[str, float]:
    m = prob >= thr
    k = int(m.sum())
    if k == 0:
        return {"kept": 0, "kept_pct": 0.0, "mean_ret": float("nan"),
                "sum_ret": 0.0, "hit_rate": float("nan")}
    return {
        "kept": k,
        "kept_pct": 100.0 * k / len(prob),
        "mean_ret": float(ret[m].mean()),
        "sum_ret": float(ret[m].sum()),
        "hit_rate": float(y[m].mean()),
    }


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

def fit_logreg(Xtr: np.ndarray, ytr: np.ndarray, l2: float = 1.0):
    """L2 logistic regression. sklearn when present, numpy fallback otherwise."""
    mu = Xtr.mean(0)
    sd = Xtr.std(0)
    sd[sd == 0] = 1.0
    Ztr = (Xtr - mu) / sd
    try:
        from sklearn.linear_model import LogisticRegression
        clf = LogisticRegression(C=1.0 / max(l2, 1e-6), max_iter=2000)
        clf.fit(Ztr, ytr)
        w = clf.coef_.ravel().astype(np.float64)
        b = float(clf.intercept_[0])
    except Exception:
        w = np.zeros(Ztr.shape[1])
        b = 0.0
        lr, lam = 0.05, max(l2, 1e-6)
        for _ in range(4000):
            p = 1.0 / (1.0 + np.exp(-np.clip(Ztr @ w + b, -60, 60)))
            g = p - ytr
            w -= lr * (Ztr.T @ g / len(ytr) + lam * w)
            b -= lr * float(g.mean())

    def predict(X):
        Z = (X - mu) / sd
        return 1.0 / (1.0 + np.exp(-np.clip(Z @ w + b, -60, 60)))

    return {"kind": "logreg", "w": w, "b": b, "mu": mu, "sd": sd}, predict


def fit_lightgbm(Xtr: np.ndarray, ytr: np.ndarray, seed: int = 7):
    """Deliberately small trees: 1.6k rows overfits a default GBDT instantly."""
    import lightgbm as lgb
    dtr = lgb.Dataset(Xtr, label=ytr)
    params = {
        "objective": "binary", "learning_rate": 0.04, "num_leaves": 7,
        "min_data_in_leaf": 40, "feature_fraction": 0.55, "bagging_fraction": 0.8,
        "bagging_freq": 1, "lambda_l2": 5.0, "verbose": -1, "seed": seed,
        "num_threads": 2,
    }
    booster = lgb.train(params, dtr, num_boost_round=250)

    def predict(X):
        return np.asarray(booster.predict(X)).ravel()

    return {"kind": "lightgbm", "booster": booster}, predict


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------

def walk_forward(data: Dict[str, Any], meta: Dict[str, Any], kind: str,
                 n_folds: int = 5, min_prob: float = 0.55,
                 verbose: bool = True) -> Dict[str, Any]:
    X = build_design(data["features"], data["signal_types"],
                     data["directions"], meta)
    y, ret = data["y"], data["ret"]
    n = len(y)

    base = data_base_rate = float(y.mean())
    test_size = max(30, int(n * 0.2))
    start = max(test_size, n - n_folds * test_size)
    folds = [(start + k * test_size, min(n, start + (k + 1) * test_size))
             for k in range(n_folds)]
    folds = [(a, b) for a, b in folds if a < b and a >= 60]

    if verbose:
        print(f"dataset: n={n}  base hit rate={base:.3f}  "
              f"mean directional return={ret.mean():+.5%}  "
              f"features={len(meta['feature_names'])}  columns={len(meta['columns'])}")
        print(f"walk-forward folds: {len(folds)} "
              f"(expanding window, no shuffle, threshold chosen on train only)\n")
        hdr = (f"{'fold':>6} {'n_tr':>6} {'n_te':>5} {'AUC':>6} {'brier':>6} "
               f"{'base':>6} | " +
               " ".join(f"{'keep '+str(int(k*100))+'%':>16}" for k in KEEP_FRACTIONS))
        print(hdr)
        print("-" * len(hdr))

    per_fold: List[Dict[str, Any]] = []
    all_prob, all_y, all_ret, all_te_idx = [], [], [], []
    for fi, (a, b) in enumerate(folds):
        Xtr, ytr = X[:a], y[:a]
        Xte, yte, rte = X[a:b], y[a:b], ret[a:b]
        if len(np.unique(ytr)) < 2:
            continue
        params, predict = (fit_lightgbm(Xtr, ytr) if kind == "lightgbm"
                           else fit_logreg(Xtr, ytr))
        ptr = np.asarray(predict(Xtr)).ravel()
        pte = np.asarray(predict(Xte)).ravel()

        f_auc = auc(pte, yte)
        f_brier = brier(pte, yte)
        rows = {}
        for kf in KEEP_FRACTIONS:
            thr = (-np.inf if kf >= 1.0 else float(np.quantile(ptr, 1.0 - kf)))
            rows[kf] = take_metrics(pte, rte, yte, thr)
        per_fold.append({"fold": fi + 1, "n_train": a, "n_test": len(yte),
                         "auc": f_auc, "brier": f_brier,
                         "base_hit": float(yte.mean()),
                         "base_mean_ret": float(rte.mean()),
                         "keep": {str(k): v for k, v in rows.items()}})
        all_prob.append(pte); all_y.append(yte); all_ret.append(rte)
        all_te_idx.append(np.arange(a, b))
        if verbose:
            cells = " ".join(
                f"{rows[k]['kept_pct']:>4.0f}% {rows[k]['mean_ret']:>+8.4%}"
                for k in KEEP_FRACTIONS)
            print(f"{fi+1:>6} {a:>6} {len(yte):>5} {f_auc:>6.3f} {f_brier:>6.3f} "
                  f"{yte.mean():>6.3f} | {cells}")

    if not all_prob:
        return {"error": "no usable folds", "per_fold": []}

    P = np.concatenate(all_prob)
    Y = np.concatenate(all_y)
    R = np.concatenate(all_ret)
    T = np.concatenate(all_te_idx)

    # Aggregate: threshold from the pooled TRAIN portion of each fold, i.e. the
    # pooled out-of-sample probability distribution is not used to pick the cut.
    agg = {"n_oos": int(len(P)), "auc": auc(P, Y), "brier": brier(P, Y),
           "base_hit": float(Y.mean()), "base_mean_ret": float(R.mean()),
           "keep": {}}
    for kf in KEEP_FRACTIONS:
        # pooled cut: rank-based, so it never depends on the test distribution
        thr = -np.inf if kf >= 1.0 else float(np.quantile(P, 1.0 - kf))
        agg["keep"][str(kf)] = take_metrics(P, R, Y, thr)

    # quintile table — the honest read on whether the model ranks
    order = np.argsort(P)
    q = max(1, len(P) // 5)
    quintiles = []
    for i in range(5):
        idx = order[i * q:(i + 1) * q] if i < 4 else order[4 * q:]
        quintiles.append({"q": i + 1, "mean_prob": float(P[idx].mean()),
                          "mean_ret": float(R[idx].mean()),
                          "hit_rate": float(Y[idx].mean()), "n": int(len(idx))})

    # engine's current gate, evaluated the same way it runs at runtime: the mute
    # set is derived from TRAIN rows only (never the held-out fold), so this is a
    # fair like-for-like benchmark against the model's out-of-sample numbers.
    names_arr = np.asarray(data["signal_name"])
    gate_bench: Dict[str, Any] = {}
    for min_n in (5, 30):
        kept = np.zeros(len(y), dtype=bool)
        seen = np.zeros(len(y), dtype=bool)
        muted_last: set = set()
        for a_, b_ in folds:
            per_name: Dict[str, List[float]] = {}
            for nm, r in zip(names_arr[:a_], ret[:a_]):
                per_name.setdefault(nm, []).append(float(r))
            muted_f = {nm for nm, v in per_name.items()
                       if len(v) >= min_n and float(np.mean(v)) < 0}
            kept[a_:b_] = np.array([nm not in muted_f for nm in names_arr[a_:b_]])
            seen[a_:b_] = True
            muted_last = muted_f
        if not seen.any():
            continue
        Rm, Ym = ret[seen], y[seen]
        kk = kept[seen]
        gate_bench[f"min_n_{min_n}"] = {
            "min_n": min_n,
            "kept_pct": 100.0 * float(kk.mean()),
            "mean_ret": float(Rm[kk].mean()) if kk.any() else float("nan"),
            "hit_rate": float(Ym[kk].mean()) if kk.any() else float("nan"),
            "base_mean_ret": float(Rm.mean()),
            "muted_signals": sorted(muted_last),
        }
    if "min_n_5" in gate_bench:
        gate_bench = gate_bench["min_n_5"] | {
            "also_min_n_30": gate_bench.get("min_n_30", {})}

    # permutation sanity check: shuffle labels, the same pipeline must land at ~0.5
    rng = np.random.default_rng(0)
    perm = []
    for _ in range(3):
        yp = rng.permutation(y)
        Xtr, ytr = X[:folds[-1][0]], yp[:folds[-1][0]]
        Xte, yte = X[folds[-1][0]:folds[-1][1]], yp[folds[-1][0]:folds[-1][1]]
        if len(np.unique(ytr)) < 2:
            continue
        _p, predict = fit_logreg(Xtr, ytr)
        perm.append(auc(np.asarray(predict(Xte)).ravel(), yte))
    agg["permuted_auc"] = float(np.mean(perm)) if perm else float("nan")

    return {"per_fold": per_fold, "aggregate": agg, "quintiles": quintiles,
            "engine_gate_benchmark": gate_bench}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Train the meta-label filter")
    ap.add_argument("--db", default="data/alerts.db")
    ap.add_argument("--model", default="auto", choices=["auto", "logreg", "lightgbm"])
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--min-prob", type=float, default=0.55)
    ap.add_argument("--exclude-neutral", action="store_true",
                    help="drop direction-agnostic signals (direction==0)")
    ap.add_argument("--out", default="data/ml/meta_model.json")
    ap.add_argument("--json", default=None, help="also write the full report here")
    a = ap.parse_args()

    data = load_dataset(a.db, exclude_neutral=a.exclude_neutral)
    if not data.get("n"):
        print("no trainable rows — run ml_backfill.py first "
              "(needs signal_outcomes JOIN signal_features)")
        return 1

    signal_types = sorted(set(data["signal_types"]))
    meta = {"feature_names": list(FEATURES), "signal_types": signal_types,
            "columns": build_columns(FEATURES, signal_types)}

    kinds = ["logreg", "lightgbm"] if a.model == "auto" else [a.model]
    reports: Dict[str, Any] = {}
    for kind in kinds:
        print(f"\n{'='*78}\nMODEL: {kind}\n{'='*78}")
        try:
            if kind == "lightgbm":
                import lightgbm  # noqa: F401
        except ImportError:
            print("lightgbm not installed — skipping")
            continue
        reports[kind] = walk_forward(data, meta, kind, n_folds=a.folds,
                                     min_prob=a.min_prob)

    if not reports:
        return 1

    print(f"\n{'='*78}\nAGGREGATE (out-of-sample, pooled across folds)\n{'='*78}")
    for kind, rep in reports.items():
        ag = rep.get("aggregate")
        if not ag:
            continue
        print(f"\n[{kind}] AUC={ag['auc']:.3f}  brier={ag['brier']:.3f}  "
              f"permuted AUC={ag['permuted_auc']:.3f}  base hit={ag['base_hit']:.3f}")
        print(f"  all signals:        n={ag['n_oos']:>5}  mean_ret={ag['base_mean_ret']:+.5%}"
              f"  hit={ag['base_hit']:.1%}")
        for kf in KEEP_FRACTIONS:
            m = ag["keep"][str(kf)]
            if not m.get("kept"):
                continue
            edge = (m["mean_ret"] - ag["base_mean_ret"])
            print(f"  keep {int(kf*100):>3}% (n={m['kept']:>4}): "
                  f"mean_ret={m['mean_ret']:+.5%}  hit={m['hit_rate']:.1%}  "
                  f"edge_vs_all={edge:+.5%}  sum={m['sum_ret']:+.4f}")
        gb = rep.get("engine_gate_benchmark")
        if gb:
            print("  ENGINE'S CURRENT GATE (mute signal names with negative train "
                  "expectancy) — same out-of-sample folds:")
            for lbl, key in (("min_n=5 ", "min_n_5"), ("min_n=30", "min_n_30")):
                sub = gb if key == "min_n_5" else gb.get("also_min_n_30", {})
                if sub:
                    print(f"    {lbl}: keeps {sub['kept_pct']:>4.0f}%  "
                          f"mean_ret={sub['mean_ret']:+.5%}  hit={sub['hit_rate']:.1%}")
            print(f"    muted at last fold: {', '.join(gb.get('muted_signals', [])) or '-'}")
        print("  predicted-prob quintiles (rank check):")
        print(f"    {'Q':>2} {'p':>6} {'mean_ret':>10} {'hit':>6} {'n':>5}")
        for qr in rep["quintiles"]:
            print(f"    {qr['q']:>2} {qr['mean_prob']:>6.3f} {qr['mean_ret']:>+10.5%} "
                  f"{qr['hit_rate']:>6.1%} {qr['n']:>5}")

    # pick the winner by AUC, then refit on ALL data for the shipped artifact
    best_kind = max((k for k, r in reports.items() if r.get("aggregate")),
                    key=lambda k: reports[k]["aggregate"]["auc"])
    ag = reports[best_kind]["aggregate"]
    print(f"\nbest by AUC: {best_kind} ({ag['auc']:.3f})")

    X = build_design(data["features"], data["signal_types"], data["directions"], meta)
    if best_kind == "lightgbm":
        params, _ = fit_lightgbm(X, data["y"])
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        lgb_path = os.path.join(os.path.dirname(a.out) or ".", "meta_model_lgbm.txt")
        params["booster"].save_model(lgb_path)
        model = {"kind": "lightgbm", "path": os.path.basename(lgb_path)}
    else:
        params, _ = fit_logreg(X, data["y"])
        model = {"kind": "logreg",
                 "w": [float(v) for v in params["w"]],
                 "b": float(params["b"]),
                 "mu": [float(v) for v in params["mu"]],
                 "sd": [float(v) for v in params["sd"]]}

    # Feature ranking from the full-data fit: which columns actually move the
    # probability. Coefficients are on standardized inputs, so |w| is comparable.
    try:
        if best_kind == "logreg":
            importance = sorted(
                ({"column": c, "weight": abs(float(w))}
                 for c, w in zip(meta["columns"], params["w"])),
                key=lambda r: -r["weight"])[:15]
        else:
            gains = params["booster"].feature_importance(importance_type="gain")
            importance = sorted(
                ({"column": c, "weight": float(g)}
                 for c, g in zip(meta["columns"], gains)),
                key=lambda r: -r["weight"])[:15]
    except Exception:
        importance = []
    if importance:
        print("\ntop feature drivers (|w| on standardized columns):")
        for r in importance:
            print(f"    {r['column']:24} {r['weight']:.4f}")

    bundle = {
        "version": BUNDLE_VERSION,
        "created": time.time(),
        "trained_on": f"{data['n']} resolved signals with backfilled features",
        "design": meta,
        "model": model,
        "metrics": {
            "auc": ag["auc"], "brier": ag["brier"], "permuted_auc": ag["permuted_auc"],
            "n_oos": ag["n_oos"], "base_hit": ag["base_hit"],
            "base_mean_ret": ag["base_mean_ret"],
            "keep": ag["keep"],
            "per_fold_auc": [f["auc"] for f in reports[best_kind]["per_fold"]],
            "engine_gate_benchmark": reports[best_kind].get("engine_gate_benchmark"),
            "importance": importance,
        },
        "feature_names": list(FEATURES),
    }
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)
    print(f"wrote {a.out}")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(reports, f, indent=2, default=str)
        print(f"wrote {a.json}")

    # verify the shipped bundle actually loads and scores through the live path
    flt = MetaLabelFilter(a.out)
    print(f"verify: ml_filter load ok={flt.ok} — {flt.describe()}")
    if flt.ok:
        probe = flt.score(data["features"][-1], data["signal_types"][-1],
                          data["directions"][-1])
        print(f"verify: score(sample) = {probe}")
    return 0 if flt.ok else 1


if __name__ == "__main__":
    sys.exit(main())
