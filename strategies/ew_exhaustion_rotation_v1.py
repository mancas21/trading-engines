#!/usr/bin/env python
"""ew_exhaustion_rotation_v1.py — EW Impulse-Exhaustion Cross-Sectional Rotation.

LA LÓGICA DE ESTA SESIÓN (análisis EW 15m/1D de PEPE), MECANIZADA
=================================================================
El análisis Elliott de PEPE encontró que el mínimo de capitulación (0.00369)
era el final de un impulso de 5 ondas, con la W4 en retroceso dorado (58.5%)
y la confluencia del piso estadístico del episodio. La hipótesis traducible a
factor: **un impulso de 5 ondas COMPLETADO marca agotamiento direccional, y el
lado agotado revierte relativo frente al panel.**

  LONG  los símbolos que ACABAN de completar un impulso de 5 ondas A LA BAJA
        (regla dura: W4 no solapa W1, W3 no es el más corto, longitudes en
        rango — misma mecánica que el conteo de PEPE)
  SHORT los que ACABAN de completar un impulso de 5 ondas AL ALZA

Market-neutral cuando ambos lados tienen candidatos. Score con decaimiento por
edad (freshness) + invalidación estructural por pierna: un long muere si el
precio cierra bajo el mínimo de su W5 (el impulso se extendió — el conteo era
 incorrecto); un short, sobre el H5.

Por qué podría diferir del mean-reversion genérico (0/N en este harness): no es
"oversold" arbitrario — es un impulso COMPLETO con las 3 reglas duras de
Elliott verificadas mecánicamente. La falsabilidad: si esas reglas no añaden
nada sobre RSI crudo, el harness lo matará igual.

ADVERTENCIA DE FAMILIA: mean-reversion midió 0/N en este repo (200+ backtests).
Esta es la variante condicionada a estructura completa — el harness decide.

Universo: 11 pares con 1h (igual que MP-18). Rebalance cada 12 barras (6h).
Costes: 11 bps RT por rebalance. Parámetros PRE-REGISTRADOS abajo.

Ejecutar:  python ew_exhaustion_rotation_v1.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = r"C:\Users\Lenovo\Documents\backtest-system"
DATA = os.path.join(ROOT, "data")

# ── PARÁMETROS PRE-REGISTRADOS (no tunear sobre la muestra) ──────────────
PARAMS = {
    "universe": ["ADAUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT",
                 "ETHUSDT", "LINKUSDT", "NEARUSDT", "SOLUSDT", "SUIUSDT", "XRPUSDT"],
    "k_per_side": 2,
    "rebalance_bars": 12,          # 6h en 1h
    "pivot_strength": 3,           # pivote 1h confirmado 3 barras por lado
    "w2_retrace": (0.20, 0.90),    # W2 dentro de W1
    "w3_min_ratio": 0.90,          # W3 >= 0.9×W1 (y no el más corto)
    "w4_retrace": (0.15, 0.85),    # W4 dentro de W3
    "w5_ratio": (0.40, 1.80),      # W5 dentro de W1
    "score_decay_bars": 72,        # freshness: 3 días
    "cost_rt_bps": 11.0,
    "warmup_bars": 100,
}


def load_1h(sym):
    p = os.path.join(DATA, "binance_klines_1h", f"{sym}_1h.csv")
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p)
    d.columns = [c.strip().lower() for c in d.columns]
    d["ts"] = pd.to_datetime(d["open_ts"], unit="ms", utc=True)
    return d.sort_values("ts").reset_index(drop=True)


def _impulse_complete(piv: list[tuple[str, float]], direction: str,
                      params: dict) -> bool:
    """piv: últimos 6 pivotes alternados terminando en la confirmación.
    direction='down': [H0, L1, H2, L3, H4, L5] impulso bajista completo.
    direction='up':   espejo [L0, H1, L2, H3, L4, H5]."""
    if len(piv) < 6:
        return False
    kinds = [k for k, _ in piv]
    exp = (["H", "L", "H", "L", "H", "L"] if direction == "down"
           else ["L", "H", "L", "H", "L", "H"])
    if kinds != exp:
        return False
    v = [p for _, p in piv]
    if direction == "down":
        w1 = v[0] - v[1]            # H0 -> L1
        w2r = (v[2] - v[1]) / w1 if w1 > 0 else 0
        w3 = v[2] - v[3]
        w4r = (v[4] - v[3]) / w3 if w3 > 0 else 0
        w5 = v[4] - v[5]
        if not (v[0] > v[2] > v[4] and v[1] > v[3] > v[5]):
            return False
        if not (params["w2_retrace"][0] <= w2r <= params["w2_retrace"][1]):
            return False
        if w3 < params["w3_min_ratio"] * w1 or w3 < min(w1, w5):
            return False
        if not (v[4] > v[1]):       # W4 no solapa territorio de W1 (regla dura)
            return False
        if not (params["w4_retrace"][0] <= w4r <= params["w4_retrace"][1]):
            return False
        if not (params["w5_ratio"][0] * w1 <= w5 <= params["w5_ratio"][1] * w1):
            return False
        return True
    # espejo up
    w1 = v[1] - v[0]
    w2r = (v[1] - v[2]) / w1 if w1 > 0 else 0
    w3 = v[3] - v[2]
    w4r = (v[3] - v[4]) / w3 if w3 > 0 else 0
    w5 = v[5] - v[4]
    if not (v[1] > v[3] > v[5] and v[0] < v[2] < v[4]):
        return False
    if not (params["w2_retrace"][0] <= w2r <= params["w2_retrace"][1]):
        return False
    if w3 < params["w3_min_ratio"] * w1 or w3 < min(w1, w5):
        return False
    if not (v[4] < v[1]):
        return False
    if not (params["w4_retrace"][0] <= w4r <= params["w4_retrace"][1]):
        return False
    return params["w5_ratio"][0] * w1 <= w5 <= params["w5_ratio"][1] * w1


def build_events(params: dict):
    """Por símbolo: lista de (bar_confirm, side, level_invalidation)."""
    events = {}
    closes = {}
    for sym in params["universe"]:
        df = load_1h(sym)
        if df is None or len(df) < params["warmup_bars"]:
            continue
        closes[sym] = df["close"].tolist()
        piv: list[tuple[str, float]] = []
        evs = []
        # un pivote necesita k barras futuras para confirmarse: hasta len-k
        for i in range(params["pivot_strength"], len(df) - params["pivot_strength"]):
            hi, lo = float(df["high"].iloc[i]), float(df["low"].iloc[i])
            win = range(i - params["pivot_strength"], i + params["pivot_strength"] + 1)
            is_h = all(hi > float(df["high"].iloc[j]) for j in win if j != i)
            is_l = all(lo < float(df["low"].iloc[j]) for j in win if j != i)
            if not (is_h or is_l) or i < 2 * params["pivot_strength"]:
                continue
            kind = "H" if is_h else "L"
            px = hi if is_h else lo
            if piv and piv[-1][0] == kind:
                if (kind == "H" and px >= piv[-1][1]) or (kind == "L" and px <= piv[-1][1]):
                    piv[-1] = (kind, px)
                continue
            piv.append((kind, px))
            if len(piv) >= 6:
                last6 = piv[-6:]
                if _impulse_complete(last6, "down", params):
                    evs.append((i, "long", last6[-1][1]))     # long: invalidación bajo L5
                elif _impulse_complete(last6, "up", params):
                    evs.append((i, "short", last6[-1][1]))    # short: invalidación sobre H5
        events[sym] = evs
    return closes, events


def weights(closes: dict, events: dict, i: int, params: dict) -> dict[str, float]:
    """Legs activas: eventos con edad <= decay, con invalidación estructural
    viva (close no cruzó el nivel del patrón)."""
    legs: dict[str, tuple[float, float]] = {}   # sym -> (score, invalidation_price)
    for sym, evs in events.items():
        if sym not in closes or i >= len(closes[sym]):
            continue
        c = closes[sym][i]
        for (bi, side, lvl) in evs:
            age = i - bi
            if age < 0 or age > params["score_decay_bars"]:
                continue
            sc = (1.0 - age / params["score_decay_bars"]) * (+1.0 if side == "long" else -1.0)
            prev = legs.get(sym)
            if prev is None or abs(sc) > abs(prev[0]):
                legs[sym] = (sc, lvl)
    w = {}
    for sym, (sc, lvl) in legs.items():
        c = closes[sym][i]
        # invalidación estructural: long muerto si cierra bajo L5; short sobre H5
        if sc > 0 and c < lvl:
            continue
        if sc < 0 and c > lvl:
            continue
        w[sym] = sc
    longs = {s: w_ for s, w_ in w.items() if w_ > 0}
    shorts = {s: w_ for s, w_ in w.items() if w_ < 0}
    out = {}
    if longs:
        for s in longs:
            out[s] = +1.0 / len(longs)
    if shorts:
        for s in shorts:
            out[s] = -1.0 / len(shorts)
    return out


def backtest(closes: dict, events: dict, params: dict) -> dict:
    n = max(len(c) for c in closes.values())
    eq = 1.0
    curve = [1.0]
    entries = 0
    w_cur = {}
    last_rb = -10**9
    period_rets = []
    for i in range(params["warmup_bars"], n):
        if w_cur:
            rets = []
            for sym, w in w_cur.items():
                c = closes[sym]
                if i < len(c):
                    p0, p1 = c[i - 1], c[i]
                    if p0 > 0 and p1 == p1:
                        rets.append(w * (p1 / p0 - 1.0))
            if rets:
                eq *= (1.0 + sum(rets))
            curve.append(eq)
            r = curve[-1] / curve[-2] - 1.0 if len(curve) > 1 else 0.0
            period_rets.append(r)
        if i - last_rb >= params["rebalance_bars"]:
            entries += 1
            w_cur = weights(closes, events, i, params)
            if w_cur:
                eq *= (1.0 - params["cost_rt_bps"] / 1e4)
            last_rb = i
    series = pd.Series(curve)
    rets = series.pct_change().dropna()
    yrs = n / (365 * 24)
    sharpe = (rets.mean() / rets.std() * np.sqrt(24 * 365)) if len(rets) > 1 and rets.std() > 0 else 0.0
    dd = ((series / series.cummax()) - 1).min()
    wins = [r for r in period_rets if r > 0]
    losses = [r for r in period_rets if r <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    return {"n_bars": n - params["warmup_bars"], "rebalances": entries,
            "total_ret": eq - 1.0,
            "cagr": (eq ** (1 / yrs) - 1) if eq > 0 else 0.0,
            "sharpe": sharpe, "max_dd": dd, "pf": pf}


def main() -> int:
    closes, events = build_events(PARAMS)
    n_ev = sum(len(v) for v in events.values())
    print(f"símbolos con impulsos completos: {len(events)} | eventos 5-ondas: {n_ev}")
    res = backtest(closes, events, PARAMS)
    print("=== ew_exhaustion_rotation_v1 ===")
    for k, v in res.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
    print("  (parámetros PRE-REGISTRADOS — el harness aplica IS/OOS, bootstrap y gates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
