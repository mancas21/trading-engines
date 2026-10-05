"""
ml_regime.py — Regime detection (Gaussian HMM) and expected-move quantiles.

Two replacements for hand-set heuristics in the engines:

1. `GaussianHMM` — a 2/3-state diagonal Gaussian HMM fitted with Baum-Welch
   (numpy only, no hmmlearn). Replaces the threshold heuristics that decide
   "range vs trend": `StrategyBacktester._range_mask` (Efficiency Ratio < 0.25,
   optionally + BB compression) and the EMA50 band in `SignalAnalyzer._regime`.
   Those cut a continuous statistic at a fixed number; an HMM infers the states
   from the data and returns the posterior probability, which is usable as a
   feature instead of a binary flag.

2. `QuantileLinear` — linear quantile regression by pinball-loss descent
   (numpy only, no statsmodels/sklearn). Replaces the heuristic
   `decision_engine.expected_move_distribution`, which reports a median and a p90
   of trailing moves instead of conditional quantiles. Predicting tau=0.5 and
   tau=0.9 turns "expected move" into a calibrated, conditional interval.

Honest caveat: both are fitted on the same thin history as the meta-label model
(~1 week). Treat the numbers below as a smoke test of the machinery, not as
validated edge.

CLI:
    python ml_regime.py --symbol SOLUSDT --tf 15m --bars 1500
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from features import FeatureExtractor

FAPI = "https://fapi.binance.com/fapi/v1/klines"
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}


# ---------------------------------------------------------------------------
# 1. Gaussian HMM (Baum-Welch / Viterbi), numpy only
# ---------------------------------------------------------------------------

class GaussianHMM:
    """Diagonal-covariance Gaussian HMM.

    Standardised inputs are required (the caller standardises); variances are
    floored so a state that collapses onto a point cannot produce inf log-lik.
    """

    def __init__(self, n_states: int = 2, n_iter: int = 100, tol: float = 1e-5,
                 seed: int = 0, var_floor: float = 1e-4):
        self.k = int(n_states)
        self.n_iter = int(n_iter)
        self.tol = float(tol)
        self.seed = int(seed)
        self.var_floor = float(var_floor)
        self.pi_: Optional[np.ndarray] = None
        self.A_: Optional[np.ndarray] = None
        self.mu_: Optional[np.ndarray] = None
        self.var_: Optional[np.ndarray] = None
        self.log_lik_: float = float("-inf")
        self.n_iter_run_: int = 0

    # -- emission ----------------------------------------------------------
    def _log_gauss(self, X: np.ndarray) -> np.ndarray:
        T, d = X.shape
        out = np.empty((T, self.k))
        for k in range(self.k):
            var = np.maximum(self.var_[k], self.var_floor)
            diff = X - self.mu_[k]
            out[:, k] = -0.5 * (np.sum(diff ** 2 / var, axis=1)
                                + np.sum(np.log(2 * np.pi * var)))
        return out

    def _init_params(self, X: np.ndarray):
        rng = np.random.default_rng(self.seed)
        T, d = X.shape
        # seed means along the first principal direction (spreads the states)
        order = np.argsort(X[:, 0])
        self.mu_ = np.vstack([X[order[int(T * (i + 0.5) / self.k)]]
                              for i in range(self.k)])
        self.mu_ = self.mu_ + rng.normal(0, 0.05, size=self.mu_.shape)
        self.var_ = np.tile(X.var(axis=0) + 0.1, (self.k, 1))
        self.pi_ = np.full(self.k, 1.0 / self.k)
        A = np.full((self.k, self.k), 0.15 / max(1, self.k - 1))
        np.fill_diagonal(A, 0.85)
        self.A_ = A

    # -- inference ---------------------------------------------------------
    def _forward_backward(self, logB: np.ndarray):
        T = logB.shape[0]
        logA = np.log(self.A_ + 1e-300)
        logpi = np.log(self.pi_ + 1e-300)
        logalpha = np.empty((T, self.k))
        logalpha[0] = logpi + logB[0]
        for t in range(1, T):
            logalpha[t] = logB[t] + _logsumexp(logalpha[t - 1][:, None] + logA, axis=0)
        logbeta = np.zeros((T, self.k))
        for t in range(T - 2, -1, -1):
            logbeta[t] = _logsumexp(logA + (logB[t + 1] + logbeta[t + 1])[None, :], axis=1)
        ll = _logsumexp(logalpha[-1], axis=0)
        loggamma = logalpha + logbeta - ll
        gamma = np.exp(loggamma)
        logxi = (logalpha[:-1, :, None] + logA[None, :, :]
                 + (logB[1:] + logbeta[1:])[:, None, :] - ll)
        xi = np.exp(logxi)
        return ll, gamma, xi

    def fit(self, X: np.ndarray) -> "GaussianHMM":
        X = np.asarray(X, dtype=np.float64)
        if X.ndim == 1:
            X = X[:, None]
        T, d = X.shape
        if T < 10 * self.k:
            raise ValueError(f"not enough rows for {self.k} states: T={T}")
        self._init_params(X)
        prev = -np.inf
        for it in range(self.n_iter):
            logB = self._log_gauss(X)
            ll, gamma, xi = self._forward_backward(logB)
            self.pi_ = gamma[0] / gamma[0].sum()
            A = xi.sum(axis=0) + 1e-12
            self.A_ = A / A.sum(axis=1, keepdims=True)
            w = gamma.sum(axis=0) + 1e-12
            self.mu_ = (gamma.T @ X) / w[:, None]
            for k in range(self.k):
                diff = X - self.mu_[k]
                self.var_[k] = (gamma[:, k][:, None] * diff ** 2).sum(axis=0) / w[k]
            self.log_lik_ = float(ll)
            self.n_iter_run_ = it + 1
            if abs(ll - prev) < self.tol * max(1.0, abs(prev)):
                break
            prev = ll
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=np.float64)
        if X.ndim == 1:
            X = X[:, None]
        _, gamma, _ = self._forward_backward(self._log_gauss(X))
        return gamma

    def viterbi(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=np.float64)
        if X.ndim == 1:
            X = X[:, None]
        logB = self._log_gauss(X)
        logA = np.log(self.A_ + 1e-300)
        T = X.shape[0]
        delta = np.empty((T, self.k))
        psi = np.zeros((T, self.k), dtype=int)
        delta[0] = np.log(self.pi_ + 1e-300) + logB[0]
        for t in range(1, T):
            m = delta[t - 1][:, None] + logA
            psi[t] = np.argmax(m, axis=0)
            delta[t] = m[psi[t], np.arange(self.k)] + logB[t]
        path = np.empty(T, dtype=int)
        path[-1] = int(np.argmax(delta[-1]))
        for t in range(T - 2, -1, -1):
            path[t] = psi[t + 1, path[t + 1]]
        return path


def _logsumexp(a: np.ndarray, axis=None):
    amax = np.max(a, axis=axis, keepdims=True)
    amax = np.where(np.isfinite(amax), amax, 0.0)
    return np.squeeze(amax, axis=axis) + np.log(np.sum(np.exp(a - amax), axis=axis))


# ---------------------------------------------------------------------------
# 2. linear quantile regression (pinball loss)
# ---------------------------------------------------------------------------

class QuantileLinear:
    """Linear model for a conditional quantile, fitted by pinball-loss descent."""

    def __init__(self, tau: float = 0.5, l2: float = 1e-3, lr: float = 0.08,
                 iters: int = 2500):
        self.tau = float(tau)
        self.l2 = float(l2)
        self.lr = float(lr)
        self.iters = int(iters)
        self.w: Optional[np.ndarray] = None
        self.b: float = 0.0
        self.mu: Optional[np.ndarray] = None
        self.sd: Optional[np.ndarray] = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> "QuantileLinear":
        X = np.asarray(X, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        n, d = X.shape
        self.mu, self.sd = X.mean(0), X.std(0)
        self.sd[self.sd == 0] = 1.0
        Z = (X - self.mu) / self.sd
        w = np.zeros(d)
        b = float(np.quantile(y, self.tau))  # start at the unconditional quantile
        for it in range(self.iters):
            r = y - (Z @ w + b)          # residual: >0 under-predicted
            # d(pinball)/d(prediction): -tau when under-predicted, +(1-tau) when over
            g = np.where(r >= 0, -self.tau, 1.0 - self.tau)
            lr = self.lr / (1.0 + it / 800.0)
            w -= lr * (Z.T @ g / n + self.l2 * w)
            b -= lr * float(g.mean())
        self.w, self.b = w, b
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        Z = (np.asarray(X, dtype=np.float64) - self.mu) / self.sd
        return Z @ self.w + self.b

    # -- evaluation --------------------------------------------------------
    def pinball(self, X: np.ndarray, y: np.ndarray) -> float:
        r = np.asarray(y, dtype=np.float64) - self.predict(X)
        return float(np.mean(np.where(r >= 0, self.tau * r, (self.tau - 1) * r)))


# ---------------------------------------------------------------------------
# 3. application: regime + expected move on real bars
# ---------------------------------------------------------------------------

def fetch_klines(symbol: str, tf: str, limit: int = 1500) -> List[list]:
    url = f"{FAPI}?symbol={symbol}&interval={tf}&limit={min(limit, 1500)}"
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.loads(r.read())


REGIME_DIMS = ("atr_rank_pct", "er20", "trend_consistency")


def build_matrix(kl, window: int, stride: int = 1) -> Tuple[List[Dict[str, float]], np.ndarray]:
    """(feature dicts, bar indices with enough history). One pass, no lookahead."""
    o = np.array([float(k[1]) for k in kl])
    h = np.array([float(k[2]) for k in kl])
    l = np.array([float(k[3]) for k in kl])
    c = np.array([float(k[4]) for k in kl])
    v = np.array([float(k[5]) for k in kl])
    fx = FeatureExtractor(window=window)
    rows, idxs = [], []
    for i in range(window, len(c), max(1, stride)):
        vec = fx.vector(o, h, l, c, v, i=i)
        if not vec:
            continue
        rows.append(vec)
        idxs.append(i)
    return rows, np.asarray(idxs, dtype=int)


def main() -> int:
    ap = argparse.ArgumentParser(description="HMM regime + quantile expected move")
    ap.add_argument("--symbol", default="SOLUSDT")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--bars", type=int, default=1500)
    ap.add_argument("--window", type=int, default=200)
    ap.add_argument("--states", type=int, default=2)
    ap.add_argument("--horizon", type=int, default=16,
                    help="forward bars for the expected-move quantiles")
    a = ap.parse_args()

    kl = fetch_klines(a.symbol.upper(), a.tf, a.bars)
    if not kl:
        print("no klines")
        return 1
    rows, idxs = build_matrix(kl, a.window)
    if len(rows) < 200:
        print(f"only {len(rows)} rows with enough history — need more `--bars`")
        return 1
    print(f"{a.symbol.upper()} {a.tf}: {len(kl)} bars -> {len(rows)} feature rows "
          f"({len(rows[0])} features)")

    # ---- regime ----------------------------------------------------------
    Xr = np.array([[r[d] for d in REGIME_DIMS] for r in rows], dtype=np.float64)
    Xr = (Xr - Xr.mean(0)) / np.where(Xr.std(0) == 0, 1.0, Xr.std(0))
    hmm = GaussianHMM(n_states=a.states, seed=0).fit(Xr)
    states = hmm.viterbi(Xr)
    post = hmm.predict_proba(Xr)

    # order states by mean ATR rank so "0 = quiet, k-1 = volatile" reads naturally
    order = np.argsort([hmm.mu_[k][0] for k in range(a.states)])
    remap = {int(old): new for new, old in enumerate(order)}
    states = np.array([remap[int(s)] for s in states])
    post = post[:, order]

    print(f"\nHMM ({a.states} states, {hmm.n_iter_run_} EM iterations, "
          f"loglik={hmm.log_lik_:.1f}, dims={REGIME_DIMS})")
    print(f"  persistence P(stay) = {np.diag(hmm.A_)[order].round(3).tolist()}")
    atr_pct_arr = np.array([r["atr_pct"] for r in rows])
    for k in range(a.states):
        m = states == k
        if not m.any():
            continue
        print(f"  state {k}: {100*m.mean():5.1f}% of bars | "
              f"atr_rank={Xr[m,0].mean():+.2f} er20={Xr[m,1].mean():+.2f} "
              f"trend_cons={Xr[m,2].mean():+.2f} | "
              f"mean atr_pct={atr_pct_arr[m].mean():.3f}%")
    sp = float(np.mean(post.max(axis=1)))
    print(f"  mean posterior confidence = {sp:.3f} "
          f"(1.0 = hard assignment, {1/a.states:.2f} = coin flip)")

    # ---- expected move: quantile regression vs the heuristic ------------
    c = np.array([float(k[4]) for k in kl])
    keep = idxs + a.horizon < len(c)
    F = np.array([[rows[j].get(n, 0.0) for n in _EM_FEATURES_ORDER]
                  for j in range(len(rows)) if keep[j]], dtype=np.float64)
    Y = np.abs(c[idxs[keep] + a.horizon] / c[idxs[keep]] - 1.0) * 100.0
    cut = int(len(Y) * 0.7)
    if cut < 100:
        print("\nnot enough rows for the expected-move split")
        return 0
    print(f"\nexpected move |{a.horizon}-bar| in %: {len(Y)} rows "
          f"(features: {', '.join(_EM_FEATURES_ORDER)}), train={cut} test={len(Y)-cut}")

    q50 = QuantileLinear(tau=0.5).fit(F[:cut], Y[:cut])
    q90 = QuantileLinear(tau=0.9).fit(F[:cut], Y[:cut])
    p50, p90 = q50.predict(F[cut:]), q90.predict(F[cut:])
    yt = Y[cut:]
    cov90 = float(np.mean(yt <= p90))
    naive_med = float(np.median(Y[:cut]))
    naive_p90 = float(np.quantile(Y[:cut], 0.9))
    print(f"  quantile regression : median pinball={q50.pinball(F[cut:], yt):.4f} "
          f"p90 pinball={q90.pinball(F[cut:], yt):.4f}")
    print(f"  naive trailing      : median={naive_med:.3f}% p90={naive_p90:.3f}%")
    print(f"  p90 coverage TRAIN  : model={float(np.mean(Y[:cut] <= q90.predict(F[:cut]))):.1%} "
          f"| naive={float(np.mean(Y[:cut] <= naive_p90)):.1%} (target 90%)")
    print(f"  p90 coverage on test: model={cov90:.1%} (target 90%) | "
          f"naive={float(np.mean(yt <= naive_p90)):.1%}")
    print(f"  median level on test: model={p50.mean():.3f}% "
          f"naive={naive_med:.3f}% | realized mean={yt.mean():.3f}%")
    return 0


# the expected-move model uses a compact, scale-free subset of features.py
_EM_FEATURES_ORDER = ("atr_pct", "atr_rank_pct", "er20", "bb_width_pct",
                      "vol_ratio", "range_pct", "ret5_pct", "regime")


if __name__ == "__main__":
    sys.exit(main())
