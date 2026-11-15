#!/usr/bin/env python
"""smc_structure_rotation_v1.py — SMC Structure-State Cross-Sectional Rotation.

LA LÓGICA DE ESTA SESIÓN, VUELTA FACTOR CROSS-SECTIONAL
=======================================================
El alertador live (early-engine smc.py) marca CHOCH/BOS por símbolo: hechos de
estructura. Aquí el estado de estructura se convierte en un FACTOR rotativo:

  SCORE por símbolo = estado de su estructura en 1h, con decaimiento por edad
    +2  CHOCH bull reciente (primer giro alcista — la reversión más fuerte)
    +1  BOS bull reciente (continuación alcista)
    -1  BOS bear reciente
    -2  CHOCH bear reciente
    el score decae linealmente a 0 en `score_decay_bars` (168 = 1 semana)

  LONG  los k símbolos con MAYOR score (estructura recién girada al alza)
  SHORT los k con MENOR score (estructura recién girada a la baja)
  gate de momento absoluto (herencia MP-18): long solo si ROC336 > 0,
  short solo si ROC336 < 0 — no se compra un giro bull dentro de un colapso.

Market-neutral (long basket vs short basket). Es momentum CONDICIONADO a
estructura: no es el ROC de MP-18 (que ya está validado) ni un patrón
direccional de un solo activo — es un ranking relativo por freshness de
estructura. Hipótesis: un rompimiento de swing RECIENTE marca persistencia
de régimen a nivel de swing; el que acaba de girar bull rinde relativamente
mejor que el que acaba de girar bear durante las siguientes `rebalance_bars`.

Sin lookahead: los pivotes se confirman `strength` barras DESPUÉS del extremo
(mismo diseño que early-engine/smc.py — probado por sus tests unitarios).
Los eventos usan la vela de CONFIRMACIÓN, nunca la del extremo.

Universo: los 11 pares con 1h (igual que MP-18). Timeframe 1h.
Rebalance: cada 48 barras (2 días — la estructura 1h se mueve más lento).
Costes: 11 bps RT por rebalance.

Ejecutar:  python smc_structure_rotation_v1.py
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
    "rebalance_bars": 48,          # 2 días en 1h
    "swing_strength": 3,           # pivote k=3 en 1h (3 velas de confirmación)
    "score_decay_bars": 168,       # el evento decae a 0 en 1 semana
    "momentum_gate_bars": 336,     # ROC gate de MP-18
    "cost_rt_bps": 11.0,
    "warmup_bars": 400,            # historia mínima antes de puntuar
}


def load_1h(sym):
    p = os.path.join(DATA, "binance_klines_1h", f"{sym}_1h.csv")
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p)
    d.columns = [c.strip().lower() for c in d.columns]
    d["ts"] = pd.to_datetime(d["open_ts"], unit="ms", utc=True)
    return d.sort_values("ts").reset_index(drop=True)


class StructureState:
    """Estado de estructura SMC en streaming — port de early-engine/smc.py
    (mismas reglas: pivote confirmado k barras después, CHOCH prioriza,
    nivel roto se consume). update() devuelve el evento de la vela o None."""

    def __init__(self, strength: int):
        self.k = strength
        self.trend: str | None = None
        self.last_sh: float | None = None
        self.last_sl: float | None = None
        self.sh_used = self.sl_used = False
        self.win: list[dict] = []

    def update(self, bar: dict) -> dict | None:
        self.win.append(bar)
        while len(self.win) >= 2 * self.k + 1:
            cand = self.win[self.k]
            if all(cand["h"] > self.win[j]["h"] for j in range(len(self.win)) if j != self.k):
                self.last_sh, self.sh_used = float(cand["h"]), False
            if all(cand["l"] < self.win[j]["l"] for j in range(len(self.win)) if j != self.k):
                self.last_sl, self.sl_used = float(cand["l"]), False
            self.win.pop(0)
        c = bar["c"]
        ev = None
        if self.trend == "bull" and self.last_sl is not None and not self.sl_used and c < self.last_sl:
            ev, self.trend, self.sl_used = ("CHOCH", "bear"), "bear", True
        elif self.trend == "bear" and self.last_sh is not None and not self.sh_used and c > self.last_sh:
            ev, self.trend, self.sh_used = ("CHOCH", "bull"), "bull", True
        elif self.trend == "bull" and self.last_sh is not None and not self.sh_used and c > self.last_sh:
            ev, self.sh_used = ("BOS", "bull"), True
        elif self.trend == "bear" and self.last_sl is not None and not self.sl_used and c < self.last_sl:
            ev, self.sl_used = ("BOS", "bear"), True
        elif self.trend is None:
            if self.last_sh is not None and not self.sh_used and c > self.last_sh:
                ev, self.trend, self.sh_used = ("BOS", "bull"), "bull", True
            elif self.last_sl is not None and not self.sl_used and c < self.last_sl:
                ev, self.trend, self.sl_used = ("BOS", "bear"), "bear", True
        return ev


def build_states(params: dict):
    """Por símbolo: la secuencia de eventos (bar_index_conf, kind, side)."""
    events = {}
    closes = {}
    for sym in params["universe"]:
        df = load_1h(sym)
        if df is None or len(df) < params["warmup_bars"]:
            continue
        closes[sym] = df["close"].tolist()
        st = StructureState(params["swing_strength"])
        evs = []
        for i in range(params["warmup_bars"], len(df)):
            bar = {"h": float(df["high"].iloc[i]), "l": float(df["low"].iloc[i]),
                   "c": float(df["close"].iloc[i])}
            e = st.update(bar)
            if e:
                evs.append((i, e[0], e[1]))     # (confirm bar, kind, side)
        events[sym] = evs
    return closes, events


def score(sym: str, i: int, events: dict, params: dict) -> float:
    """Score con decaimiento por edad desde la barra de CONFIRMACIÓN."""
    sc = 0.0
    for (bi, kind, side) in events.get(sym, []):
        age = i - bi
        if age < 0 or age > params["score_decay_bars"]:
            continue
        base = {"BOS": 1.0, "CHOCH": 2.0}[kind]
        sign = 1.0 if side == "bull" else -1.0
        sc += sign * base * (1.0 - age / params["score_decay_bars"])
    return sc


def weights(closes: dict, events: dict, i: int, params: dict) -> dict[str, float]:
    scores = {}
    for sym, c in closes.items():
        if i >= len(c) or i < params["warmup_bars"]:
            continue
        sc = score(sym, i, events, params)
        roc = c[i] / c[i - params["momentum_gate_bars"]] - 1.0
        if sc > 0 and roc > 0:
            scores[sym] = sc
        elif sc < 0 and roc < 0:
            scores[sym] = sc
    if len(scores) < 2 * params["k_per_side"]:
        return {}
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    shorts = [s for s, _ in ranked[:params["k_per_side"]]]
    longs = [s for s, _ in ranked[-params["k_per_side"]:]]
    w = {}
    for s in longs:
        w[s] = +1.0 / len(longs)
    for s in shorts:
        w[s] = -1.0 / len(shorts)
    return w


def backtest(closes: dict, events: dict, params: dict) -> dict:
    n = max(len(c) for c in closes.values())
    eq = 1.0
    curve = [1.0]
    entries = 0
    w_cur = {}
    last_rb = -10**9
    for i in range(params["warmup_bars"] + params["momentum_gate_bars"], n):
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
        if i - last_rb >= params["rebalance_bars"]:
            entries += 1
            w_cur = weights(closes, events, i, params)
            eq *= (1.0 - params["cost_rt_bps"] / 1e4)
            last_rb = i
    series = pd.Series(curve)
    rets = series.pct_change().dropna()
    yrs = n / (365 * 24)
    sharpe = (rets.mean() / rets.std() * np.sqrt(24 * 365)) if rets.std() > 0 else 0.0
    dd = ((series / series.cummax()) - 1).min()
    return {"n_bars": n - params["warmup_bars"] - params["momentum_gate_bars"],
            "rebalances": entries, "total_ret": eq - 1.0,
            "cagr": (eq ** (1 / yrs) - 1) if eq > 0 else 0.0,
            "sharpe": sharpe, "max_dd": dd}


def main() -> int:
    closes, events = build_states(PARAMS)
    print(f"símbolos con eventos: {len(events)} | "
          f"total eventos: {sum(len(v) for v in events.values())}")
    res = backtest(closes, events, PARAMS)
    print("=== smc_structure_rotation_v1 ===")
    for k, v in res.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
    print("  (parámetros PRE-REGISTRADOS — ver PARAMS; el harness aplica IS/OOS,")
    print("   block bootstrap y gates G1-G11 encima de esto)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
