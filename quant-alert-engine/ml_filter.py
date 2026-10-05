"""
ml_filter.py — Meta-label filter: the inference half of the ML layer.

Loads the model bundle written by `ml_train.py` and scores a signal's
probability of paying, using the SAME design matrix the model was trained on.

Design-matrix contract (shared with ml_train.py — never duplicate it):
    columns = [features.py FEATURES...] + ["st:<signal_type>"...] + ["direction"]
stored in the bundle as `columns`, so a model can never be scored with a
different column order than it was trained with.

Everything here is fail-open by contract: `score()` returns None instead of
raising when the model or the feature vector is unusable, and the engine treats
None as "no opinion" (see SignalAnalyzer._emit).
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

BUNDLE_VERSION = 1


# ---------------------------------------------------------------------------
# design matrix — the single source of truth for column layout
# ---------------------------------------------------------------------------

def build_columns(feature_names: Sequence[str], signal_types: Sequence[str]) -> List[str]:
    """Fixed column order: features, signal-type one-hot, direction."""
    return list(feature_names) + [f"st:{s}" for s in signal_types] + ["direction"]


def expand_row(features: Dict[str, float], signal_type: str, direction: int,
               meta: Dict[str, Any]) -> List[float]:
    """One (features, signal_type, direction) triple -> design row."""
    row: Dict[str, float] = {}
    for n in meta["feature_names"]:
        try:
            v = float(features.get(n, 0.0) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        row[n] = v if math.isfinite(v) else 0.0
    for s in meta.get("signal_types", []):
        row[f"st:{s}"] = 1.0 if signal_type == s else 0.0
    row["direction"] = float(direction or 0)
    return [row.get(c, 0.0) for c in meta["columns"]]


def build_design(feature_dicts: Sequence[Dict[str, float]],
                 signal_types: Sequence[str],
                 directions: Sequence[int],
                 meta: Dict[str, Any]) -> np.ndarray:
    rows = [expand_row(fd, st, d, meta)
            for fd, st, d in zip(feature_dicts, signal_types, directions)]
    X = np.asarray(rows, dtype=np.float64)
    if X.size == 0:
        return np.zeros((0, len(meta["columns"])), dtype=np.float64)
    return np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)


def _sigmoid(z):
    z = np.clip(z, -60.0, 60.0)
    return 1.0 / (1.0 + np.exp(-z))


# ---------------------------------------------------------------------------
# filter
# ---------------------------------------------------------------------------

class MetaLabelFilter:
    """Loads a model bundle and scores P(this signal's forward return > 0)."""

    def __init__(self, path: str):
        self.path = str(path)
        self.ok = False
        self.error = ""
        self.bundle: Dict[str, Any] = {}
        self.meta: Dict[str, Any] = {}
        self._w: Optional[np.ndarray] = None
        self._b: float = 0.0
        self._mu: Optional[np.ndarray] = None
        self._sd: Optional[np.ndarray] = None
        self._booster = None
        self._kind = ""
        self.metrics: Dict[str, Any] = {}
        self.created: float = 0.0
        try:
            self._load()
            self.ok = True
        except Exception as e:  # keep the engine alive on any bad bundle
            self.error = f"{type(e).__name__}: {e}"
            self.ok = False

    # -- loading ------------------------------------------------------------
    def _load(self):
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"model bundle not found: {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            self.bundle = json.load(f)
        if int(self.bundle.get("version", 0)) != BUNDLE_VERSION:
            raise ValueError(f"unsupported bundle version {self.bundle.get('version')}")
        self.meta = dict(self.bundle["design"])
        if not self.meta.get("columns") or not self.meta.get("feature_names"):
            raise ValueError("bundle design is missing columns/feature_names")
        self.metrics = dict(self.bundle.get("metrics", {}))
        self.created = float(self.bundle.get("created", 0.0))
        model = dict(self.bundle["model"])
        self._kind = str(model.get("kind", ""))
        if self._kind == "logreg":
            self._w = np.asarray(model["w"], dtype=np.float64)
            self._b = float(model["b"])
            self._mu = np.asarray(model["mu"], dtype=np.float64)
            self._sd = np.asarray(model["sd"], dtype=np.float64)
            if not (len(self._w) == len(self._mu) == len(self._sd) == len(self.meta["columns"])):
                raise ValueError("logreg coefficient/scale length mismatch")
        elif self._kind == "lightgbm":
            import lightgbm as lgb  # optional dependency
            mp = model.get("path") or os.path.join(os.path.dirname(self.path),
                                                   "meta_model_lgbm.txt")
            self._booster = lgb.Booster(model_file=mp)
            if self._booster.num_feature() != len(self.meta["columns"]):
                raise ValueError("lightgbm feature count mismatch with design columns")
        else:
            raise ValueError(f"unknown model kind: {self._kind!r}")

    # -- inference ----------------------------------------------------------
    def score(self, features: Dict[str, float], signal_type: str,
              direction: int) -> Optional[float]:
        """P(forward return > 0). None when there is nothing to score with."""
        if not self.ok:
            return None
        if not features:
            return None  # no vector -> no opinion (never a fabricated 0.5)
        X = build_design([features], [signal_type], [direction], self.meta)
        if X.shape[1] != len(self.meta["columns"]):
            return None
        if self._kind == "logreg":
            Z = (X - self._mu) / np.where(self._sd == 0, 1.0, self._sd)
            return float(_sigmoid(Z @ self._w + self._b)[0])
        # lightgbm: raw design, no standardisation
        return float(np.asarray(self._booster.predict(X)).ravel()[0])

    def describe(self) -> str:
        if not self.ok:
            return f"unavailable ({self.error})"
        auc = self.metrics.get("auc")
        auc_s = f"{float(auc):.3f}" if isinstance(auc, (int, float)) else "n/a"
        return (f"{self._kind} v{self.bundle.get('version')} "
                f"cols={len(self.meta['columns'])} "
                f"trained_on={self.bundle.get('trained_on', '?')} "
                f"auc={auc_s}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Inspect / score with a model bundle")
    ap.add_argument("model", nargs="?", default="data/ml/meta_model.json")
    ap.add_argument("--signal-type", default="MACD")
    ap.add_argument("--direction", type=int, default=1)
    ap.add_argument("--zero-features", action="store_true",
                    help="score an all-zero feature vector (sanity check)")
    a = ap.parse_args()
    f = MetaLabelFilter(a.model)
    print("ok:", f.ok, "| error:", f.error or "-")
    if f.ok:
        print("describe:", f.describe())
        print("metrics:", json.dumps(f.metrics, indent=2, default=str))
        if a.zero_features:
            from features import FEATURES
            fv = {k: 0.0 for k in FEATURES}
            print("score(all-zero, %s, %+d) = %s"
                  % (a.signal_type, a.direction, f.score(fv, a.signal_type, a.direction)))
