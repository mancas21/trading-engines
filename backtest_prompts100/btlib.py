"""Motor de backtest para la criba de los 100 prompts (Binance USDT-M perps).

Convenciones del documento fuente:
- senal en barra cerrada, fill en la apertura siguiente
- el stop gana empates (si low<=stop y high>=target, sale el stop)
- 1% de riesgo por trade desde la distancia del stop ATR
- costes: 11 bps roundtrip (5 fee + 0.5 slip por lado, modelo del repo),
  22 bps como sensibilidad
- split 70/30 IS/OOS por numero de barras
Datos: read-only de backtest-system + rellenos en ./data

2026-11-06: `load` carries a feather disk cache (data/_csv_cache, keyed on the
source files' name+mtime_ns+size) so the ~212 MB of CSV are parsed once per
data change, not once per process. The cached frame is the RAW parsed frame;
the dtype/astype/dedupe/sort post-processing runs on BOTH paths, so the
returned DataFrame is bit-identical to the CSV-parse path. Backtest-system's
data dir stays read-only: the cache lives in this repo.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(r"C:\Users\Lenovo\Documents\backtest-system\data")
WORK = Path(__file__).parent
ALT = WORK / "data"
CACHE_DIR = WORK / "data" / "_csv_cache"

TF_DIR = {
    "1h": SRC / "binance_klines_1h",
    "15m": SRC / "binance_klines_15m",
    "5m": SRC / "binance_klines_5m_taker",
}

_cache: dict = {}


def _src_files(sym: str, tf: str) -> list:
    """Source CSVs behind (sym, tf), in load() order (main first, tail last)."""
    if tf == "5m":
        return [TF_DIR["5m"] / f"{sym}_5m.csv"]
    if tf == "1h" and sym == "JUPUSDT":
        return [ALT / "JUPUSDT_1h.csv"]
    if tf == "15m" and sym == "JUPUSDT":
        return [ALT / "JUPUSDT_15m.csv"]
    files = [TF_DIR[tf] / f"{sym}_{tf}.csv"]
    if sym == "HYPEUSDT" and tf == "1h" and (ALT / "HYPEUSDT_1h_tail.csv").exists():
        files.append(ALT / "HYPEUSDT_1h_tail.csv")
    return files


def _cache_key(files: list) -> str:
    h = hashlib.sha256()
    for fp in files:
        st = fp.stat()
        h.update(f"{fp.name}|{st.st_mtime_ns}|{st.st_size};".encode())
    return h.hexdigest()[:24]


def load(sym: str, tf: str) -> pd.DataFrame:
    """sym tipo BTCUSDT, tf en 1h/15m/5m. Devuelve df [ts,open,high,low,close,volume]."""
    key = (sym, tf)
    if key in _cache:
        return _cache[key]
    files = _src_files(sym, tf)
    ck = CACHE_DIR / f"{sym}_{tf}_{_cache_key(files)}.feather"
    if ck.exists():
        df = pd.read_feather(ck)
    else:
        if len(files) > 1:
            df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
        else:
            df = pd.read_csv(files[0])
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = ck.with_suffix(f".tmp{os.getpid()}")
            df.to_feather(tmp)
            os.replace(tmp, ck)
        except OSError:
            pass  # cache is an accelerator, never a requirement
    df = df[["open_ts", "open", "high", "low", "close", "volume"]].astype(float)
    df = df.drop_duplicates("open_ts").sort_values("open_ts").reset_index(drop=True)
    _cache[key] = df
    return df


def funding_series(sym: str) -> pd.Series:
    """Rate por settlement indexado por ts ms."""
    if sym not in _cache:
        d = json.loads((SRC / "binance_funding" / f"{sym}_funding.json").read_text())
        s = pd.Series({int(x["t"]): float(x["r"]) for x in d}).sort_index()
        _cache[sym] = s
    return _cache[sym]


# ---------------------------------------------------------------- indicadores
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def sma(s, n):
    return s.rolling(n).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    ru = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = ru / rd.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def true_range(df):
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                      (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df, n=14):
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def bollinger(close, n=20, k=2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return mid + k * sd, mid, mid - k * sd


def keltner(df, n=20, m=1.5):
    mid = ema(df["close"], n)
    a = atr(df, n)
    return mid + m * a, mid, mid - m * a


def stoch(df, n=14, k=3):
    hh = df["high"].rolling(n).max()
    ll = df["low"].rolling(n).min()
    raw = 100 * (df["close"] - ll) / (hh - ll).replace(0, np.nan)
    kk = raw.rolling(k).mean()
    return kk, kk.rolling(3).mean()


def willr(df, n=14):
    hh = df["high"].rolling(n).max()
    ll = df["low"].rolling(n).min()
    return -100 * (hh - df["close"]) / (hh - ll).replace(0, np.nan)


def cci(df, n=20):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    m = tp.rolling(n).mean()
    md = (tp - m).abs().rolling(n).mean()
    return (tp - m) / (0.015 * md.replace(0, np.nan))


def mfi(df, n=14):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    flow = tp * df["volume"]
    up = flow.where(tp > tp.shift(1), 0.0).rolling(n).sum()
    dn = flow.where(tp < tp.shift(1), 0.0).rolling(n).sum()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def ultimate_osc(df, p1=7, p2=14, p3=28):
    pc = df["close"].shift(1)
    bp = df["close"] - pd.concat([df["low"], pc], axis=1).min(axis=1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    def avg(p):
        return bp.rolling(p).sum() / tr.rolling(p).sum().replace(0, np.nan)
    return 100 * (4 * avg(p1) + 2 * avg(p2) + avg(p3)) / 7


def adx_di(df, n=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = true_range(df)
    atr_ = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_
    mdi = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean(), pdi, mdi


def aroon(df, n=25):
    # 2026-11-06: vectorized (sliding-window argmax over the reversed window,
    # same tie-breaking as argmax(x[::-1])); verified bit-identical to the
    # rolling.apply version on BTCUSDT 1h and SOLUSDT 5m
    def _arr(s):
        x = s.values.astype(float)
        out = np.full(len(x), np.nan)
        if len(x) >= n + 1:
            wv = np.lib.stride_tricks.sliding_window_view(x, n + 1)
            pos = np.argmax(wv[:, ::-1], axis=1)
            out[n:] = 100 * (n - pos) / n
        return out
    return (pd.Series(_arr(df["high"]), index=df.index),
            pd.Series(_arr(df["low"]), index=df.index))


def macd_lines(close, f=12, s=26, sg=9):
    line = ema(close, f) - ema(close, s)
    return line, line.ewm(span=sg, adjust=False).mean()


def wma(s, n):
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n).apply(lambda x: float(np.dot(x, w) / w.sum()), raw=True)


def hma(s, n=21):
    return wma(2 * wma(s, n // 2) - wma(s, n), max(int(np.sqrt(n)), 2))


def kama(s, n=10, fast=2, slow=30):
    change = (s - s.shift(n)).abs()
    vol = s.diff().abs().rolling(n).sum().replace(0, np.nan)
    er = (change / vol).clip(0, 1)
    sc = (er * (2 / fast - 2 / slow) + 2 / slow) ** 2
    vals = s.values.astype(float).copy()
    scv = sc.values
    outv = np.empty(len(s))
    outv[0] = vals[0]
    for i in range(1, len(s)):
        if np.isnan(scv[i]):
            outv[i] = vals[i]
        else:
            outv[i] = outv[i - 1] + scv[i] * (vals[i] - outv[i - 1])
    return pd.Series(outv, index=s.index)


def supertrend(df, n=10, m=3.0):
    a = atr(df, n)
    hl2 = (df["high"] + df["low"]) / 2
    ub = (hl2 + m * a).values
    lb = (hl2 - m * a).values
    c = df["close"].values
    n_ = len(df)
    st = np.zeros(n_)
    dir_ = np.ones(n_)
    fub, flb = ub.copy(), lb.copy()
    for i in range(1, n_):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if dir_[i - 1] == 1:
            dir_[i] = -1 if c[i] < flb[i] else 1
        else:
            dir_[i] = 1 if c[i] > fub[i] else -1
        st[i] = flb[i] if dir_[i] == 1 else fub[i]
    return pd.Series(st, index=df.index), pd.Series(dir_, index=df.index)


def parabolic_sar(df, af=0.02, mx=0.2):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n_ = len(df)
    sar = np.zeros(n_)
    trend = np.ones(n_)
    ep = h[0]
    accel = af
    sar[0] = l[0]
    for i in range(1, n_):
        sar[i] = sar[i - 1] + accel * (ep - sar[i - 1])
        if trend[i - 1] == 1:
            sar[i] = min(sar[i], l[i - 1], l[i - 2] if i > 1 else l[i - 1])
            if l[i] < sar[i]:
                trend[i] = -1
                sar[i] = ep
                ep = l[i]
                accel = af
            else:
                trend[i] = 1
                if h[i] > ep:
                    ep = h[i]
                    accel = min(accel + af, mx)
        else:
            sar[i] = max(sar[i], h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > sar[i]:
                trend[i] = 1
                sar[i] = ep
                ep = h[i]
                accel = af
            else:
                trend[i] = -1
                if l[i] < ep:
                    ep = l[i]
                    accel = min(accel + af, mx)
    return pd.Series(sar, index=df.index), pd.Series(trend, index=df.index)


def ichimoku(df, t=9, k=26, s=52):
    conv = (df["high"].rolling(t).max() + df["low"].rolling(t).min()) / 2
    base = (df["high"].rolling(k).max() + df["low"].rolling(k).min()) / 2
    spanA = ((conv + base) / 2).shift(k)
    spanB = ((df["high"].rolling(s).max() + df["low"].rolling(s).min()) / 2).shift(k)
    return conv, base, spanA, spanB


def donchian(df, n):
    return df["high"].rolling(n).max(), df["low"].rolling(n).min()


def connors_rsi(df):
    c = df["close"]
    r3 = rsi(c, 3)
    # streak: vectorized run-length of up/down streaks, reset on equal closes;
    # verified bit-identical to the original Python loop (same two datasets)
    vals = c.values.astype(float)
    st_arr = np.zeros(len(c))
    if len(c) > 1:
        # a NaN close compares False both ways in the original loop -> streak 0;
        # sign(NaN) maps to 0 so the run-length logic reproduces that exactly
        d = np.nan_to_num(np.sign(np.diff(vals)), nan=0.0)
        idx = np.arange(len(d))
        reset = np.empty(len(d), dtype=bool)
        reset[0] = True
        reset[1:] = (d[1:] != d[:-1]) | (d[1:] == 0) | (d[:-1] == 0)
        last = np.maximum.accumulate(np.where(reset, idx, 0))
        st_arr[1:] = d * (idx - last + 1)
    streak = pd.Series(st_arr, index=df.index)
    rs = rsi(streak, 2)
    roc1 = c.pct_change(1) * 100
    pr = percentrank(roc1, 100) * 100   # connors percentrank is 0-100, not 0-1
    return (r3 + rs + pr) / 3


def roll_vwap(df, n):
    pv = ((df["high"] + df["low"] + df["close"]) / 3 * df["volume"]).rolling(n).sum()
    return pv / df["volume"].rolling(n).sum()


def zscore(s, n):
    m = s.rolling(n).mean()
    sd = s.rolling(n).std()
    return (s - m) / sd.replace(0, np.nan)


def percentrank(s, n):
    # 2026-11-06: vectorized ((window[:-1] < last).sum() / n == (x[-1] > x).mean());
    # verified bit-identical to the rolling.apply version on BTCUSDT 1h / SOLUSDT 5m,
    # including the pandas NaN rule: a window containing any NaN yields NaN
    x = s.values.astype(float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        wv = np.lib.stride_tricks.sliding_window_view(x, n)
        ok = ~np.isnan(wv).any(axis=1)
        idx = np.arange(n - 1, len(x))[ok]
        out[idx] = (wv[ok][:, :-1] < wv[ok][:, -1:]).sum(axis=1) / n
    return pd.Series(out, index=s.index)


# ---------------------------------------------------------------- motor
def run_engine(df, sig, cost_rt=0.0011, risk=0.01, funding=None):
    """Motor de eventos para un instrumento.

    sig: dict con arrays alineados al df:
      long_entry, short_entry, long_exit, short_exit (bool, senal en cierre)
      stop_dist (float, distancia de precio calculada en la barra de senal)
      max_bars (opcional), target_dist (opcional), trail_dist (opcional, precio)
      atr (serie para trailing si trail_dist depende de ATR -> pasar array)
    funding: pd.Series ts_ms -> rate (devengo mientras hay posicion)
    Devuelve (eq_curve np, trades list[dict]).
    """
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(df)
    long_entry = np.asarray(sig["long_entry"], dtype=bool)
    short_entry = np.asarray(sig["short_entry"], dtype=bool)
    long_exit = np.asarray(sig.get("long_exit", np.zeros(n, bool)), dtype=bool)
    short_exit = np.asarray(sig.get("short_exit", np.zeros(n, bool)), dtype=bool)
    stop_d = np.asarray(sig["stop_dist"], dtype=float)
    max_bars = sig.get("max_bars")
    tgt_d = sig.get("target_dist")
    trail_d = sig.get("trail_dist")
    if max_bars is not None:
        max_bars = np.asarray(max_bars)
    if tgt_d is not None:
        tgt_d = np.asarray(tgt_d, dtype=float)
    if trail_d is not None:
        trail_d = np.asarray(trail_d, dtype=float)

    fund_by_ts = {}
    if funding is not None:
        fund_by_ts = dict(zip(funding.index.values, funding.values))
        # mapear ts de settlement a barra: primera barra con open_ts >= t
        ts = df["open_ts"].values
        fts = np.array(sorted(fund_by_ts))
        fidx = np.searchsorted(ts, fts)
        frate = np.array([fund_by_ts[t] for t in fts])
    else:
        fidx = np.array([], dtype=int)
        frate = np.array([])

    pos = 0.0
    entry = stop = tgt = 0.0
    d0 = 1.0
    entry_i = -1
    entry_px = 0.0
    hi = lo = 0.0
    fund_r = 0.0
    realized = 1.0
    eq_curve = np.ones(n)
    trades = []

    fund_ptr = 0
    for i in range(n):
        while fund_ptr < len(fidx) and fidx[fund_ptr] == i:
            if pos != 0:
                fund_r += pos * (-frate[fund_ptr]) * c[i] / d0
            fund_ptr += 1

        if pos > 0:
            exit_px = None
            if long_exit[i - 1] if i >= 1 else False:
                exit_px = o[i]
            elif max_bars is not None and (i - entry_i) >= max_bars[i]:
                exit_px = o[i]
            elif l[i] <= stop:
                exit_px = min(o[i], stop)
            elif tgt_d is not None and tgt_d[i] > 0 and h[i] >= entry + tgt_d[i]:
                exit_px = max(o[i], entry + tgt_d[i])
            if exit_px is not None:
                r = (exit_px - entry) / d0 - cost_rt * abs(entry_px) / d0 + fund_r
                realized *= (1 + risk * r)
                trades.append(dict(entry_i=entry_i, exit_i=i, dir=1, r=r))
                pos = 0.0
                fund_r = 0.0
            elif trail_d is not None:
                stop = max(stop, h[i] - trail_d[i])
        elif pos < 0:
            exit_px = None
            if short_exit[i - 1] if i >= 1 else False:
                exit_px = o[i]
            elif max_bars is not None and (i - entry_i) >= max_bars[i]:
                exit_px = o[i]
            elif h[i] >= stop:
                exit_px = max(o[i], stop)
            elif tgt_d is not None and tgt_d[i] > 0 and l[i] <= entry - tgt_d[i]:
                exit_px = min(o[i], entry - tgt_d[i])
            if exit_px is not None:
                r = (entry - exit_px) / d0 - cost_rt * abs(entry_px) / d0 + fund_r
                realized *= (1 + risk * r)
                trades.append(dict(entry_i=entry_i, exit_i=i, dir=-1, r=r))
                pos = 0.0
                fund_r = 0.0
            elif trail_d is not None:
                stop = min(stop, l[i] + trail_d[i])

        if pos == 0 and i >= 1:
            if long_entry[i - 1] and stop_d[i - 1] > 0:
                pos = 1.0
                entry = o[i]
                entry_px = o[i]
                d0 = stop_d[i - 1]
                entry_i = i
                stop = entry - d0
                hi = h[i]
                lo = l[i]
                tgt = 0.0
                if l[i] <= stop:  # stop puede saltar en la propia barra de entrada
                    r = (min(o[i], stop) - entry) / d0 - cost_rt * abs(entry_px) / d0
                    realized *= (1 + risk * r)
                    trades.append(dict(entry_i=entry_i, exit_i=i, dir=1, r=r))
                    pos = 0.0
                    fund_r = 0.0
            elif short_entry[i - 1] and stop_d[i - 1] > 0:
                pos = -1.0
                entry = o[i]
                entry_px = o[i]
                d0 = stop_d[i - 1]
                entry_i = i
                stop = entry + d0
                hi = h[i]
                lo = l[i]
                if h[i] >= stop:
                    r = (entry - max(o[i], stop)) / d0 - cost_rt * abs(entry_px) / d0
                    realized *= (1 + risk * r)
                    trades.append(dict(entry_i=entry_i, exit_i=i, dir=-1, r=r))
                    pos = 0.0
                    fund_r = 0.0

        if pos > 0:
            unreal = (c[i] - entry) / d0 + fund_r - cost_rt * abs(entry_px) / d0
            eq_curve[i] = realized * (1 + risk * unreal)
        elif pos < 0:
            unreal = (entry - c[i]) / d0 + fund_r - cost_rt * abs(entry_px) / d0
            eq_curve[i] = realized * (1 + risk * unreal)
        else:
            eq_curve[i] = realized

    if pos != 0:  # cerrar al final
        exit_px = c[-1]
        if pos > 0:
            r = (exit_px - entry) / d0 - cost_rt * abs(entry_px) / d0 + fund_r
        else:
            r = (entry - exit_px) / d0 - cost_rt * abs(entry_px) / d0 + fund_r
        realized *= (1 + risk * r)
        trades.append(dict(entry_i=entry_i, exit_i=n - 1, dir=int(pos), r=r))
        eq_curve[-1] = realized
    return eq_curve, trades


# ---------------------------------------------------------------- metricas
def max_drawdown(eq):
    peak = np.maximum.accumulate(eq)
    return float((eq / peak - 1).min())


def sharpe(eq, bars_per_year):
    r = np.diff(eq) / eq[:-1]
    r = r[np.isfinite(r)]
    if len(r) < 2 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * np.sqrt(bars_per_year))


def pf_winrate(trades):
    rs = np.array([t["r"] for t in trades]) if trades else np.array([])
    if len(rs) == 0:
        return 0.0, 0.0, 0.0
    gp = rs[rs > 0].sum()
    gl = -rs[rs < 0].sum()
    pf = gp / gl if gl > 0 else (np.inf if gp > 0 else 0.0)
    return float(pf), float((rs > 0).mean()), float(rs.sum())


def segment_stats(eq, trades, i0, i1, bars_per_year):
    seg = eq[i0:i1]
    seg_tr = [t for t in trades if i0 <= t["entry_i"] < i1]
    net = float(seg[-1] / seg[0] - 1) if len(seg) > 1 else 0.0
    pf, wr, _ = pf_winrate(seg_tr)
    return dict(net=net, mdd=max_drawdown(seg), pf=pf, wr=wr, n=len(seg_tr),
                sharpe=sharpe(seg, bars_per_year))


def evaluate(df, sig, sym, tf, name, group, cost_rt=0.0011, risk=0.01, funding=None):
    eq, trades = run_engine(df, sig, cost_rt=cost_rt, risk=risk, funding=funding)
    n = len(df)
    split = int(n * 0.7)
    bpy = {"1h": 8760, "15m": 35040, "5m": 105120}[tf]
    full = segment_stats(eq, trades, 0, n, bpy)
    is_ = segment_stats(eq, trades, 0, split, bpy)
    oos = segment_stats(eq, trades, split, n, bpy)
    c0, c1 = df["close"].iloc[0], df["close"].iloc[-1]
    cs = df["close"].iloc[split]
    bh_full = c1 / c0 - 1
    bh_oos = c1 / cs - 1
    oos["excess"] = oos["net"] - bh_oos
    full["excess"] = full["net"] - bh_full
    gate = bool(full["pf"] > 1.2 and full["n"] >= 30 and full["net"] > 0)
    oos_ok = bool(oos["pf"] > 1.2 and oos["net"] > 0 and oos["n"] >= 30)
    return dict(name=name, sym=sym, tf=tf, group=group, eq=eq, trades=trades,
                full=full, is_=is_, oos=oos, bh_full=bh_full, bh_oos=bh_oos,
                gate=gate, oos_ok=oos_ok, split=split)
