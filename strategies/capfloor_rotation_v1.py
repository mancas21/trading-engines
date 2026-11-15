#!/usr/bin/env python
"""capfloor_rotation_v1.py — Fund-Squeeze Cross-Sectional Rotation.

LA LÓGICA DE ESTA SESIÓN, VUELTA FACTOR CROSS-SECTIONAL
=======================================================
La lógica live del early-engine/episodios: el piso de una caída llega cuando
el funding se vuelve fuertemente NEGATIVO (shorts apretados pagando) y el OI
se lava. Aquí eso se convierte en un FACTOR DE POSICIONAMIENTO rotativo, no
en una llamada direccional:

  LONG  los k símbolos con funding MÁS NEGATIVO (shorts apiñados pagando:
         trade fácil cargado — exhaustión vendedora relativa)
  SHORT los k símbolos con funding MÁS POSITIVO (longs apiñados pagando:
         vulnerables a unwind — subida sin combustible)

Market-neutral por construcción (long basket vs short basket, igual peso).
NO es carry: no cobramos el funding — lo usamos como SEÑAL de posicionamiento;
el PnL es de precio. El carry direccional midió −76.9% y el neutro −88.8%
(STRATEGY_GENERATION_PROMPT): ambos cobraban funding; aquí el funding filtra
a QUIÉN está del lado equivocado.

LA TRAMPA §5.7 (leer antes de creer en el resultado)
====================================================
La capa "early-watch" de trading-engines midió en sección cruzada (60 perps,
n=846) que el funding-capitulación NO tenía edge direccional y que el régimen
estaba INVERTIDO respecto al backtest original: la celda verde era manía de
mercado (2020-21 + FTX), no mecanismo. Por eso esta estrategia lleva un FILTRO
DE RÉGIMEN pre-registrado: el spread solo se toma cuando la DISPERSIÓN de
funding del panel está por encima de su mediana rodante — el factor se activa
solo cuando hay capitulación/euforia detectable, no siempre. Y los parámetros
están PRE-REGISTRADOS abajo: no se tunean sobre la muestra.

HIPÓTESIS
=========
Posicionamiento extremo revierte de forma relativa: el lado que paga funding
extremo está sobreapiñado; su siguiente tramo relativo es peor que el del
lado que cobra. La dispersión alta del panel es la condición bajo la cual esa
reversión de posicionamiento tiene señal y no ruido.

Universo: 8 símbolos con funding histórico en data/binance_funding.
Timeframe: 1h. Rebalance: cada 24 barras (un ciclo de funding completo).
Costes: 11 bps round-trip por pierna por rebalance (estándar del repo).

Ejecutar:  python capfloor_rotation_v1.py   (desde cualquier cwd; lee
data/binance_klines_1h y data/binance_funding del backtest-system)
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = r"C:\Users\Lenovo\Documents\backtest-system"
DATA = os.path.join(ROOT, "data")

# ── PARÁMETROS PRE-REGISTRADOS (no tunear sobre la muestra) ──────────────
PARAMS = {
    "universe": ["BTCUSDT", "ETHUSDT", "HYPEUSDT", "JUPUSDT",
                 "SOLUSDT", "SUIUSDT", "XRPUSDT"],  # SPX sin funding limpio -> fuera
    "k_per_side": 2,           # long top-k / short bottom-k por funding
    "rebalance_bars": 24,      # 1 ciclo de funding (8h) en velas 1h
    "funding_avg_settlements": 3,   # media de 3 settlements (24h) para denoiser
    "min_abs_funding_pct": 0.010,   # |funding| mínimo por settlement (%) para contar como extremo
    "regime_dispersion_quantile": 0.5,  # dispersión del panel sobre su mediana rodante 90d
    "regime_lookback_bars": 90 * 24,
    "roc_guard_bars": 168,     # guardia anti-cuchillo: long solo si ROC168 > -30%
    "roc_guard_floor": -0.30,
    "cost_rt_bps": 11.0,
}


def load_1h(sym):
    p = os.path.join(DATA, "binance_klines_1h", f"{sym}_1h.csv")
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p)
    d.columns = [c.strip().lower() for c in d.columns]
    d["ts"] = pd.to_datetime(d["open_ts"], unit="ms", utc=True)
    return d.sort_values("ts").reset_index(drop=True)


def load_funding(sym):
    p = os.path.join(DATA, "binance_funding", f"{sym}_funding.json")
    if not os.path.exists(p):
        return None
    raw = json.load(open(p))
    rows = []
    for r in raw:
        t = r.get("t") or r.get("fundingTime")
        rate = r.get("r") or r.get("fundingRate")
        if rate is None:
            continue
        rows.append({"ts": pd.to_datetime(t, unit="ms", utc=True),
                     "rate": float(rate) * 100.0})   # % por settlement
    if not rows:
        return None
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def build_matrices(params):
    prices, funding = {}, {}
    for sym in params["universe"]:
        df = load_1h(sym)
        if df is not None:
            prices[sym] = df
        fd = load_funding(sym)
        if fd is not None:
            funding[sym] = fd
    all_ts = sorted(set().union(*[set(prices[s]["ts"]) for s in prices]))
    close = pd.DataFrame(index=all_ts)
    for sym in prices:
        close[sym] = prices[sym].set_index("ts")["close"].reindex(all_ts)
    # funding forward-filled a velas 1h: el rate vigente en cada hora
    fund = pd.DataFrame(index=all_ts)
    for sym in funding:
        s = funding[sym].set_index("ts")["rate"]
        fund[sym] = s.reindex(all_ts).ffill()
    return close, fund


def funding_signal(fund: pd.DataFrame, i: int, params: dict) -> dict:
    """Score por símbolo en la vela i: funding medio de los últimos
    `funding_avg_settlements` settlements (24h). Devuelve {sym: rate%}."""
    win = params["funding_avg_settlements"] * 8      # 3 settlements = 24h
    window = fund.iloc[max(0, i - win):i]
    return window.mean().to_dict()


def dispersion_regime(fund: pd.DataFrame, i: int, params: dict) -> bool:
    """Régimen activo cuando la dispersión (std cross-seccional del funding)
    está por encima de su mediana rodante — capitulación/euforia detectable."""
    lookback = params["regime_lookback_bars"]
    lo = max(0, i - lookback)
    disp = fund.iloc[lo:i].std(axis=1)               # std por fila (panel)
    disp = disp.dropna()
    if len(disp) < 100 or i < 1:
        return False
    med = disp.median()
    return bool(disp.iloc[-1] > med)


def weights(close: pd.DataFrame, fund: pd.DataFrame, i: int,
            params: dict) -> dict[str, float] | None:
    """Pesos objetivo en la vela i. None = sin posición (régimen no activo)."""
    if not dispersion_regime(fund, i, params):
        return None
    sig = funding_signal(fund, i, params)
    sig = {k: v for k, v in sig.items()
           if v == v and abs(v) >= params["min_abs_funding_pct"]}
    if len(sig) < 2 * params["k_per_side"]:
        return None
    ranked = sorted(sig.items(), key=lambda kv: kv[1])   # más negativo primero
    longs = [s for s, _ in ranked[:params["k_per_side"]]]
    shorts = [s for s, _ in ranked[-params["k_per_side"]:]]
    # guardia anti-cuchillo: no comprar lo que ya se desplomó >30% en 7 días
    longs = [s for s in longs
             if close[s].iloc[i] / close[s].iloc[max(0, i - params["roc_guard_bars"])] - 1
             >= params["roc_guard_floor"]]
    if not longs:
        return None
    w = {}
    for s in longs:
        w[s] = +1.0 / len(longs)
    for s in shorts:
        w[s] = -1.0 / len(shorts)
    return w


def backtest(close: pd.DataFrame, fund: pd.DataFrame, params: dict) -> dict:
    n = len(close)
    start = params["regime_lookback_bars"] + params["funding_avg_settlements"] * 8
    w_cur = None
    last_rebalance = -10**9
    eq = 1.0
    eq_curve = [1.0]
    entries = 0
    for i in range(start, n):
        # PnL de la vela con los pesos vigentes (solo símbolos con precio)
        if w_cur:
            rets = []
            for sym, w in w_cur.items():
                p0 = close[sym].iloc[i - 1]
                p1 = close[sym].iloc[i]
                if p0 == p0 and p1 == p1 and p0 > 0:
                    rets.append(w * (p1 / p0 - 1.0))
            if rets:
                eq *= (1.0 + sum(rets))
        eq_curve.append(eq)
        # rebalance programado: coste completo del RT en cada rotación
        # (conservador: asume que las dos piernas rotan por completo)
        if i - last_rebalance >= params["rebalance_bars"]:
            entries += 1
            w_cur = weights(close, fund, i, params)
            eq *= (1.0 - params["cost_rt_bps"] / 1e4)
            last_rebalance = i
    # métricas
    curve = pd.Series(eq_curve)
    rets = curve.pct_change().dropna()
    yrs = n / (365 * 24)
    total = eq - 1.0
    cagr = (eq ** (1 / yrs) - 1) if yrs > 0 and eq > 0 else 0.0
    sharpe = (rets.mean() / rets.std() * np.sqrt(24 * 365)) if rets.std() > 0 else 0.0
    dd = ((curve / curve.cummax()) - 1).min()
    return {"n_bars": n - start, "rebalances": entries, "total_ret": total,
            "cagr": cagr, "sharpe": sharpe, "max_dd": dd}


def main() -> int:
    close, fund = build_matrices(PARAMS)
    res = backtest(close, fund, PARAMS)
    print("=== capfloor_rotation_v1 ===")
    for k, v in res.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
    print("  (parámetros PRE-REGISTRADOS — ver PARAMS; el harness aplica IS/OOS,")
    print("   block bootstrap y gates G1-G11 encima de esto)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
