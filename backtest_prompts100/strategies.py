"""Definicion de las 100 estrategias de strategy_prompts_100.

Cada una devuelve un dict para btlib.evaluate (motor de un instrumento) o
usa un runner especial (pares, cross-sectional, TSM/flip). Las senales se
calculan con datos <= barra de senal; el motor llena en la apertura siguiente.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import btlib as B

TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


def ts_of(df):
    return pd.to_datetime(df["open_ts"], unit="ms", utc=True)


def SIG(df, le=None, se=None, lx=None, sx=None, sd=None, **kw):
    n = len(df)
    z = np.zeros(n, bool)
    d = dict(long_entry=le if le is not None else z,
             short_entry=se if se is not None else z,
             long_exit=lx if lx is not None else z.copy(),
             short_exit=sx if sx is not None else z.copy(),
             stop_dist=sd if sd is not None else np.zeros(n))
    d.update(kw)
    return d


def cross_up(a, b):
    return (a > b) & (a.shift(1) <= b.shift(1))


def cross_dn(a, b):
    return (a < b) & (a.shift(1) >= b.shift(1))


def align_hi(sym, tf_lo, tf_hi, series_hi):
    """Alinea una serie del TF alto al TF bajo usando solo barras CERRADAS."""
    hi = B.load(sym, tf_hi)
    s = pd.Series(np.asarray(series_hi, dtype=float), index=hi.index)
    s = s.shift(1)  # ultima barra cerrada
    frm = pd.DataFrame({"ts": hi["open_ts"] + TF_MS[tf_hi], "v": s.values})
    lo = B.load(sym, tf_lo)
    to = pd.DataFrame({"ts": lo["open_ts"]})
    out = pd.merge_asof(to.sort_values("ts"), frm.sort_values("ts"),
                        on="ts", direction="backward")
    return out["v"].values


def sess_vwap(df):
    """VWAP acumulada por dia UTC (solo datos de la sesion en curso)."""
    d = ts_of(df).dt.strftime("%Y-%m-%d").values
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["volume"]).groupby(d).cumsum()
    vv = df["volume"].groupby(d).cumsum()
    return pv / vv.replace(0, np.nan)


# ------------------------------------------------------------------ motor xs
def run_xs(syms, tf, ret_n, rebal_n, top=2, bot=2, lowvol=False, vol_scaled=False,
           cost_side=0.00055):
    """Rotacion cross-sectional: long top-N / short bottom-N, dollar-neutral."""
    closes = {}
    atrs = {}
    for s in syms:
        df = B.load(s, tf)
        closes[s] = pd.Series(df["close"].values, index=df["open_ts"].values)
        atrs[s] = pd.Series((B.atr(df, 20) / df["close"]).values,
                            index=df["open_ts"].values)
    C = pd.DataFrame(closes).dropna()
    mom = C.pct_change(ret_n)
    if lowvol:
        score = -pd.DataFrame(atrs).reindex(C.index)
    elif vol_scaled:
        score = mom / pd.DataFrame(atrs).reindex(C.index)
    else:
        score = mom
    w = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    idx = np.arange(len(C))
    rebal_idx = idx[idx % rebal_n == 0]
    for i in rebal_idx:
        row = score.iloc[i]
        if row.notna().sum() < len(C.columns):
            continue
        rk = row.rank(ascending=False)
        for c in C.columns:
            if rk[c] <= top:
                w.loc[C.index[i], c] = 0.5 / top
            elif rk[c] > len(C.columns) - bot:
                w.loc[C.index[i], c] = -0.5 / bot
    w = w.replace(0, np.nan).ffill().fillna(0.0)
    ret = C.pct_change().fillna(0.0)
    dw = w.diff().abs().sum(axis=1).fillna(0.0)
    port = (w.shift() * ret).sum(axis=1) - dw * cost_side
    eq = (1 + port).cumprod()
    # trades = cada periodo entre rebalanceos
    tr = []
    prev = 0
    for i in list(rebal_idx[1:]) + [len(C)]:
        seg = port.iloc[prev:i]
        if len(seg):
            tr.append(dict(r=float(seg.sum()), entry_i=int(prev)))
        prev = i
    return eq.values, tr


def run_flip(df, pos, weight, cost_side=0.00055):
    """Posicion continua por barra (TSM/SAR flips), peso = fraccion de equity
    tal que un stop de 2xATR pierde 1%. trades = cada cambio de signo.
    La posicion decidida en la barra i-1 (cierre) cobra el retorno de la barra i.
    Coste por barra sobre el giro de notional |d(w*p)|."""
    c = df["close"].values
    r = np.zeros(len(df))
    r[1:] = c[1:] / c[:-1] - 1
    w = np.asarray(weight, dtype=float)
    p = np.sign(np.asarray(pos, dtype=float))
    wp = w * p
    wp_prev = np.concatenate([[0.0], wp[:-1]])  # senal de la barra anterior
    turn = np.abs(wp - wp_prev)
    bar = wp_prev * r - turn * cost_side
    eq = np.cumprod(1 + bar)
    flips = np.where(np.diff(p, prepend=p[0]) != 0)[0]
    tr = []
    for a, b in zip(flips[:-1], flips[1:]):
        seg = bar[a + 1:b + 1] if b < len(bar) else bar[a + 1:]
        if len(seg):
            tr.append(dict(r=float(seg.sum()), entry_i=int(a)))
    if len(flips) == 0:
        tr.append(dict(r=float(bar.sum()), entry_i=0))
    elif flips[-1] < len(bar) - 1:
        tr.append(dict(r=float(bar[flips[-1] + 1:].sum()), entry_i=int(flips[-1])))
    return eq, tr


def pair_df(symA, symB, tf, beta_n=None, kalman=False):
    A = B.load(symA, tf)
    Bs = B.load(symB, tf)
    n = min(len(A), len(Bs))
    A = A.iloc[-n:].reset_index(drop=True)
    Bs = Bs.iloc[-n:].reset_index(drop=True)
    la = np.log(A["close"].values)
    lb = np.log(Bs["close"].values)
    if kalman:
        beta = np.zeros(len(la))
        b = 1.0
        p = 1.0
        for i in range(len(la)):
            p += 1e-5
            k_ = p / (p + 1e-3)
            b = b + k_ * (la[i] - b * lb[i])
            p = (1 - k_) * p
            beta[i] = b
        r = la - beta * lb
    elif beta_n:
        laa = pd.Series(la)
        lbb = pd.Series(lb)
        beta = (laa.rolling(beta_n).cov(lbb) / lbb.rolling(beta_n).var()).values
        r = la - beta * lb
    else:
        beta = None
        r = la - lb
    o = pd.Series(r).shift(1).fillna(r[0]).values
    hi = np.log(A["high"].values) - (np.log(Bs["low"].values) *
                                     (beta if beta is not None else 1.0))
    lo = np.log(A["low"].values) - (np.log(Bs["high"].values) *
                                    (beta if beta is not None else 1.0))
    h = np.maximum(o, hi)
    l = np.minimum(o, lo)
    out = pd.DataFrame({"open_ts": A["open_ts"].values, "open": o, "high": h,
                        "low": l, "close": r,
                        "volume": np.minimum(A["volume"].values, Bs["volume"].values)})
    return out


# ------------------------------------------------------------------ registro
STRATS = []


def strat(no, name, sym, tf, group, notes=""):
    def deco(fn):
        STRATS.append(dict(no=no, name=name, sym=sym, tf=tf, group=group,
                           build=fn, notes=notes))
        return fn
    return deco


def sig_single(s, sym, tf, name, group, no, notes=""):
    df = B.load(sym, tf)
    sig = s(df)
    res = B.evaluate(df, sig, sym, tf, name, group)
    return res, notes


# ============================== MEAN REVERSION 1-25 =========================
@strat(1, "RSI mean reversion", "SOLUSDT", "1h", "mean_reversion")
def s1(df):
    r = B.rsi(df["close"], 14)
    a = B.atr(df, 14)
    return SIG(df, le=r < 30, lx=r > 70, sd=2 * a)


@strat(2, "Bollinger + RSI", "BTCUSDT", "15m", "mean_reversion")
def s2(df):
    up, mid, lo = B.bollinger(df["close"], 20, 2)
    r = B.rsi(df["close"], 14)
    a = B.atr(df, 14)
    return SIG(df, le=(df["close"] < lo) & (r < 35), lx=df["close"] >= mid, sd=1.5 * a)


@strat(3, "VWAP z-score", "ETHUSDT", "5m", "mean_reversion")
def s3(df):
    v = sess_vwap(df)
    dev = df["close"] - v
    z = dev / dev.rolling(60).std()
    a = B.atr(df, 14)
    return SIG(df, le=z < -2.2, lx=z >= 0, sd=2 * a)


@strat(4, "Stochastic cross", "HYPEUSDT", "1h", "mean_reversion")
def s4(df):
    k, d = B.stoch(df, 14, 3)
    a = B.atr(df, 14)
    return SIG(df, le=cross_up(k, pd.Series(20.0, index=df.index)),
               lx=k > 80, sd=2 * a)


@strat(5, "Williams %R", "SUIUSDT", "15m", "mean_reversion")
def s5(df):
    w = B.willr(df, 14)
    a = B.atr(df, 14)
    return SIG(df, le=w < -90, lx=w > -30, sd=1.5 * a)


@strat(6, "CCI reversal", "JUPUSDT", "1h", "mean_reversion")
def s6(df):
    c = B.cci(df, 20)
    a = B.atr(df, 14)
    return SIG(df, le=c < -150, lx=c > 0, sd=2.5 * a)


@strat(7, "Connors RSI(2)", "BTCUSDT", "1h", "mean_reversion")
def s7(df):
    e200 = B.ema(df["close"], 200)
    r2 = B.rsi(df["close"], 2)
    hh5 = df["high"].rolling(5).max().shift(1)
    a = B.atr(df, 14)
    return SIG(df, le=(df["close"] > e200) & (r2 < 10),
               lx=df["close"] > hh5, sd=2 * a)


@strat(8, "Keltner fade", "ETHUSDT", "15m", "mean_reversion")
def s8(df):
    up, mid, lo = B.keltner(df, 20, 1.5)
    a = B.atr(df, 14)
    return SIG(df, le=df["close"] < lo, lx=df["close"] >= mid, sd=1.5 * a)


@strat(9, "Fast RSI", "SOLUSDT", "5m", "mean_reversion")
def s9(df):
    r = B.rsi(df["close"], 7)
    a = B.atr(df, 14)
    return SIG(df, le=r < 20, lx=(r > 60), sd=1.2 * a, max_bars=np.full(len(df), 12))


@strat(10, "Down-streak bounce", "HYPEUSDT", "15m", "mean_reversion")
def s10(df):
    r = B.rsi(df["close"], 14)
    dn3 = (df["close"] < df["close"].shift(1)) & (df["close"].shift(1) < df["close"].shift(2)) \
        & (df["close"].shift(2) < df["close"].shift(3))
    a = B.atr(df, 14)
    return SIG(df, le=dn3 & (r < 35), lx=df["close"] > df["high"].shift(1), sd=2 * a)


@strat(11, "EMA deviation", "SUIUSDT", "1h", "mean_reversion")
def s11(df):
    e20 = B.ema(df["close"], 20)
    a20 = B.atr(df, 20)
    a14 = B.atr(df, 14)
    return SIG(df, le=df["close"] < e20 - 2.5 * a20, lx=df["close"] >= e20, sd=3 * a14)


@strat(12, "Donchian fade", "JUPUSDT", "15m", "mean_reversion")
def s12(df):
    hh, ll = B.donchian(df, 20)
    mid = (hh + ll) / 2
    a = B.atr(df, 14)
    return SIG(df, le=df["low"] <= ll.shift(1), lx=df["close"] >= mid.shift(1),
               sd=1.5 * a)


@strat(13, "Volume-confirmed RSI", "BTCUSDT", "5m", "mean_reversion")
def s13(df):
    r = B.rsi(df["close"], 14)
    va = df["volume"].rolling(20).mean()
    a = B.atr(df, 14)
    return SIG(df, le=(r < 25) & (df["volume"] > 2 * va), lx=r > 55, sd=1.5 * a)


@strat(14, "50-EMA stretch", "ETHUSDT", "1h", "mean_reversion")
def s14(df):
    e50 = B.ema(df["close"], 50)
    a = B.atr(df, 14)
    return SIG(df, le=df["close"] < 0.97 * e50, lx=df["close"] >= e50, sd=2 * a)


@strat(15, "Bollinger %B", "SOLUSDT", "15m", "mean_reversion")
def s15(df):
    up, mid, lo = B.bollinger(df["close"], 20, 2)
    pb = (df["close"] - lo) / (up - lo).replace(0, np.nan)
    a = B.atr(df, 14)
    return SIG(df, le=pb < 0, lx=pb > 0.8, sd=1.5 * a,
               max_bars=np.full(len(df), 24))


@strat(16, "RSI divergence", "HYPEUSDT", "5m", "mean_reversion")
def s16(df):
    r = B.rsi(df["close"], 14)
    ll30 = df["low"].rolling(30).min()
    fresh = df["close"] <= ll30
    a = B.atr(df, 14)
    return SIG(df, le=fresh & (r > r.shift(30)), lx=r > 65, sd=1.5 * a,
               notes="aprox divergencia: minimo 30b nuevo con RSI mayor que hace 30b")


@strat(17, "Ultimate Oscillator", "SUIUSDT", "5m", "mean_reversion")
def s17(df):
    u = B.ultimate_osc(df)
    a = B.atr(df, 14)
    return SIG(df, le=u < 30, lx=u > 70, sd=1.5 * a)


@strat(18, "MFI reversal", "JUPUSDT", "5m", "mean_reversion")
def s18(df):
    m = B.mfi(df, 14)
    a = B.atr(df, 14)
    return SIG(df, le=m < 20, lx=m > 75, sd=1.5 * a)


@strat(19, "Range fade", "BTCUSDT", "15m", "mean_reversion")
def s19(df):
    hh = df["high"].rolling(20).max()
    a = B.atr(df, 14)
    sd = (df["close"] - (df["low"] - a)).clip(lower=0)
    return SIG(df, le=df["close"] < hh - 1.5 * a, lx=df["close"] > B.sma(df["close"], 10),
               sd=sd, notes="stop 1xATR por debajo del low de la barra de senal")


@strat(20, "Multi-timeframe RSI", "ETHUSDT", "5m", "mean_reversion")
def s20(df):
    r = B.rsi(df["close"], 14)
    r1h = align_hi("ETHUSDT", "5m", "1h", B.rsi(B.load("ETHUSDT", "1h")["close"], 14))
    a = B.atr(df, 14)
    return SIG(df, le=(r < 30) & (pd.Series(r1h) < 45), lx=r > 60, sd=1.5 * a)


@strat(21, "Trend-filtered RSI", "SOLUSDT", "1h", "mean_reversion")
def s21(df):
    r = B.rsi(df["close"], 14)
    e200 = B.ema(df["close"], 200)
    a = B.atr(df, 14)
    return SIG(df, le=(r < 30) & (df["close"] > e200), lx=r > 70, sd=2 * a)


@strat(22, "Band fade con time stop", "HYPEUSDT", "1h", "mean_reversion")
def s22(df):
    up, mid, lo = B.bollinger(df["close"], 20, 2.5)
    a = B.atr(df, 14)
    return SIG(df, le=df["low"] <= lo, lx=df["close"] >= mid, sd=2 * a,
               max_bars=np.full(len(df), 10))


@strat(23, "Connors RSI(3,2,100)", "SUIUSDT", "15m", "mean_reversion")
def s23(df):
    cr = B.connors_rsi(df)
    a = B.atr(df, 14)
    return SIG(df, le=cr < 10, lx=cr > 70, sd=1.5 * a)


@strat(24, "Failed breakdown reclaim", "JUPUSDT", "1h", "mean_reversion")
def s24(df):
    ll10 = df["low"].rolling(10).min()
    hh10 = df["high"].rolling(10).max()
    brk = df["close"] < ll10.shift(1)
    reclaim = (brk.shift(1) | brk.shift(2)) & (df["close"] > ll10.shift(1))
    a = B.atr(df, 14)
    sd = (df["close"] - (ll10.shift(1) - a)).clip(lower=0)
    return SIG(df, le=reclaim, lx=df["close"] > hh10.shift(1), sd=sd,
               notes="stop 1xATR bajo el minimo reclamado")


@strat(25, "VWAP band", "BTCUSDT", "1h", "mean_reversion")
def s25(df):
    v = B.roll_vwap(df, 30)
    dev = df["close"] - v
    z = dev / dev.rolling(30).std()
    a = B.atr(df, 14)
    return SIG(df, le=z < -2, lx=df["close"] >= v, sd=2 * a)


# ============================== TREND 26-45 =================================
@strat(26, "EMA cross", "ETHUSDT", "1h", "trend")
def s26(df):
    e20 = B.ema(df["close"], 20)
    e50 = B.ema(df["close"], 50)
    a = B.atr(df, 14)
    return SIG(df, le=cross_up(e20, e50), se=cross_dn(e20, e50),
               lx=cross_dn(e20, e50), sx=cross_up(e20, e50), sd=3 * a)


@strat(27, "Supertrend", "SOLUSDT", "15m", "trend")
def s27(df):
    st, d = B.supertrend(df, 10, 3)
    flip_up = (d == 1) & (d.shift(1) == -1)
    flip_dn = (d == -1) & (d.shift(1) == 1)
    sd = (df["close"] - st).abs()
    return SIG(df, le=flip_up, se=flip_dn, lx=flip_dn, sx=flip_up, sd=sd,
               notes="stop estatico = linea Supertrend en la senal (el ST real es dinamico)")


@strat(28, "Donchian breakout", "BTCUSDT", "1h", "trend")
def s28(df):
    hh, ll = B.donchian(df, 55)
    _, ll20 = B.donchian(df, 20)
    a = B.atr(df, 14)
    return SIG(df, le=df["close"] > hh.shift(1), lx=df["close"] < ll20.shift(1),
               sd=3 * a)


@strat(29, "MACD zero-cross", "HYPEUSDT", "15m", "trend")
def s29(df):
    line, sigl = B.macd_lines(df["close"])
    hist = line - sigl
    e200 = B.ema(df["close"], 200)
    a = B.atr(df, 14)
    return SIG(df, le=cross_up(hist, pd.Series(0.0, index=df.index)) & (df["close"] > e200),
               lx=cross_dn(hist, pd.Series(0.0, index=df.index)), sd=2 * a)


@strat(30, "ADX DI cross", "SUIUSDT", "1h", "trend")
def s30(df):
    adx, pdi, mdi = B.adx_di(df, 14)
    a = B.atr(df, 14)
    return SIG(df, le=cross_up(pdi, mdi) & (adx > 25), se=cross_dn(pdi, mdi) & (adx > 25),
               lx=cross_dn(pdi, mdi), sx=cross_up(pdi, mdi), sd=2.5 * a)


@strat(31, "Triple EMA pullback", "JUPUSDT", "15m", "trend")
def s31(df):
    e9, e21, e55 = B.ema(df["close"], 9), B.ema(df["close"], 21), B.ema(df["close"], 55)
    a = B.atr(df, 14)
    stop_lv = e55 - 0.5 * a
    sd = (df["close"] - stop_lv).clip(lower=0)
    le = (e9 > e21) & (e21 > e55) & (df["low"] <= e21)
    return SIG(df, le=le, lx=df["close"] < e55, sd=sd,
               notes="sin exit explicito en el prompt; anadido cierre bajo EMA55")


@strat(32, "HMA slope", "ETHUSDT", "5m", "trend")
def s32(df):
    hm = B.hma(df["close"], 21)
    sl = hm.diff() > 0
    a = B.atr(df, 14)
    return SIG(df, le=sl & sl.shift(1) & sl.shift(2), lx=~sl, sd=1.5 * a)


@strat(33, "Ichimoku", "BTCUSDT", "15m", "trend")
def s33(df):
    conv, base, spanA, spanB = B.ichimoku(df)
    cloud_top = pd.concat([spanA, spanB], axis=1).max(axis=1)
    cloud_bot = pd.concat([spanA, spanB], axis=1).min(axis=1)
    a = B.atr(df, 14)
    le = (df["close"] > cloud_top) & (conv > base)
    return SIG(df, le=le, lx=df["close"] < base, sd=2 * a)


@strat(34, "Regression slope", "SOLUSDT", "1h", "trend")
def s34(df):
    x = np.arange(50, dtype=float)
    den = 50 * (x ** 2).sum() - x.sum() ** 2

    def slope(win):
        return float((50 * (x * win).sum() - x.sum() * win.sum()) / den)

    sl = df["close"].rolling(50).apply(slope, raw=True)
    a = B.atr(df, 14)
    return SIG(df, le=cross_up(sl, pd.Series(0.0, index=df.index)),
               se=cross_dn(sl, pd.Series(0.0, index=df.index)),
               lx=cross_dn(sl, pd.Series(0.0, index=df.index)),
               sx=cross_up(sl, pd.Series(0.0, index=df.index)), sd=2 * a)


@strat(35, "480-bar breakout", "HYPEUSDT", "1h", "trend")
def s35(df):
    hh = df["high"].rolling(480).max()
    ll = df["low"].rolling(240).min()
    a = B.atr(df, 14)
    return SIG(df, le=df["close"] > hh.shift(1), lx=df["close"] < ll.shift(1), sd=3 * a)


@strat(36, "EMA(9/21) con ADX", "SUIUSDT", "5m", "trend")
def s36(df):
    e9, e21 = B.ema(df["close"], 9), B.ema(df["close"], 21)
    adx, _, _ = B.adx_di(df, 14)
    a = B.atr(df, 14)
    return SIG(df, le=cross_up(e9, e21) & (adx > 20), se=cross_dn(e9, e21) & (adx > 20),
               lx=cross_dn(e9, e21), sx=cross_up(e9, e21), sd=1.5 * a)


@strat(37, "Parabolic SAR", "JUPUSDT", "1h", "trend", notes="flips continuos, size 2xATR")
def s37(df):
    sar, trend = B.parabolic_sar(df)
    a = B.atr(df, 14)
    w = (0.01 / (2 * a / df["close"])).fillna(0.0)
    pos = trend.fillna(0.0)
    return dict(kind="flip", pos=pos.values, weight=w.values)


@strat(38, "Keltner breakout", "BTCUSDT", "5m", "trend")
def s38(df):
    up, mid, lo = B.keltner(df, 20, 2)
    a = B.atr(df, 14)
    return SIG(df, le=df["close"] > up, lx=df["close"] < mid, sd=1.5 * a)


@strat(39, "Aroon", "ETHUSDT", "15m", "trend")
def s39(df):
    up, dn = B.aroon(df, 25)
    a = B.atr(df, 14)
    le = (up > 70) & (dn < 30)
    lx = (up < 30) | (dn > 70)
    return SIG(df, le=le, lx=lx, sd=2 * a)


@strat(40, "VWMA cross", "SOLUSDT", "5m", "trend")
def s40(df):
    vwma = (df["close"] * df["volume"]).rolling(30).sum() / df["volume"].rolling(30).sum()
    sma30 = B.sma(df["close"], 30)
    a = B.atr(df, 14)
    return SIG(df, le=cross_up(vwma, sma30), se=cross_dn(vwma, sma30),
               lx=cross_dn(vwma, sma30), sx=cross_up(vwma, sma30), sd=1.5 * a)


@strat(41, "Efficiency ratio filter", "HYPEUSDT", "5m", "trend")
def s41(df):
    c = df["close"]
    er = (c - c.shift(30)).abs() / c.diff().abs().rolling(30).sum().replace(0, np.nan)
    e10, e30 = B.ema(c, 10), B.ema(c, 30)
    a = B.atr(df, 14)
    le = (er > 0.4) & (e10 > e30)
    lx = (er < 0.4) | (e10 < e30)
    return SIG(df, le=le, lx=lx, sd=1.5 * a)


@strat(42, "KAMA slope", "SUIUSDT", "15m", "trend")
def s42(df):
    km = B.kama(df["close"], 10)
    up = km.diff() > 0
    a = B.atr(df, 14)
    return SIG(df, le=up, lx=~up, sd=2 * a)


@strat(43, "HTF trend filter", "JUPUSDT", "5m", "trend")
def s43(df):
    df15 = B.load("JUPUSDT", "15m")
    e100_15 = B.ema(df15["close"], 100)
    e100 = align_hi("JUPUSDT", "5m", "15m", e100_15)
    c15 = align_hi("JUPUSDT", "5m", "15m", df15["close"])
    r = B.rsi(df["close"], 14)
    a = B.atr(df, 14)
    le = (pd.Series(c15) > pd.Series(e100)) & (r > 50)
    lx = pd.Series(c15) < pd.Series(e100)
    return SIG(df, le=le, lx=lx, sd=1.5 * a)


@strat(44, "168-bar breakout", "BTCUSDT", "1h", "trend")
def s44(df):
    hh = df["high"].rolling(168).max()
    ll = df["low"].rolling(72).min()
    a = B.atr(df, 14)
    return SIG(df, le=df["close"] > hh.shift(1), lx=df["close"] < ll.shift(1), sd=2.5 * a)


@strat(45, "Time-series momentum", "ETHUSDT", "1h", "trend",
       notes="signo del retorno 168b, rebalance 00:00 UTC, size 2xATR")
def s45(df):
    ts = ts_of(df)
    c = df["close"]
    mom = c / c.shift(168) - 1
    a = B.atr(df, 14)
    pos = np.sign(mom)
    daily = ts.dt.hour == 0
    pos_held = pos.where(daily).ffill()
    w = (0.01 / (2 * a / c)).fillna(0.0)
    return dict(kind="flip", pos=pos_held.fillna(0).values, weight=w.values)


# ==================== BREAKOUT / VOLATILIDAD 46-65 ==========================
@strat(46, "Bollinger squeeze", "SOLUSDT", "15m", "breakout")
def s46(df):
    up, mid, lo = B.bollinger(df["close"], 20, 2)
    bw = (up - lo) / mid
    squeeze = bw == bw.rolling(100).min()
    released = squeeze.rolling(5).max() == 1
    a = B.atr(df, 14)
    return SIG(df, le=released & (df["close"] > up), lx=df["close"] < mid, sd=1.5 * a)


@strat(47, "Opening range breakout", "BTCUSDT", "5m", "breakout")
def s47(df):
    ts = ts_of(df)
    hr = ts.dt.hour
    mi = ts.dt.minute
    in_range = (hr == 0)
    rng_hi = df["high"].where(in_range).groupby(ts.dt.strftime("%Y-%m-%d")).transform("max")
    rng_lo = df["low"].where(in_range).groupby(ts.dt.strftime("%Y-%m-%d")).transform("min")
    mid = (rng_hi + rng_lo) / 2
    after = (hr >= 1)
    a = B.atr(df, 14)
    c = df["close"]
    brk_l = after & (c > rng_hi)
    brk_s = after & (c < rng_lo)
    # una sola entrada por dia (primera ruptura)
    first_l = brk_l.groupby(ts.dt.strftime("%Y-%m-%d")).cumsum() == 1
    first_s = brk_s.groupby(ts.dt.strftime("%Y-%m-%d")).cumsum() == 1
    le = brk_l & first_l
    se = brk_s & first_s
    sd = (c - mid).abs()
    lx = (hr == 0) & (mi == 0)
    return SIG(df, le=le, se=se, lx=lx, sx=lx, sd=sd,
               notes="salida a la apertura del dia siguiente (fin de dia); 1 entrada/dia")


@strat(48, "NR7 breakout", "HYPEUSDT", "15m", "breakout")
def s48(df):
    rng = df["high"] - df["low"]
    nr = rng == rng.rolling(7).min()
    nr_hi = df["high"].where(nr).ffill()
    nr_lo = df["low"].where(nr).ffill()
    a = B.atr(df, 14)
    c = df["close"]
    sd_l = np.minimum(c - nr_lo, 1.5 * a)
    sd_s = np.minimum(nr_hi - c, 1.5 * a)
    le = c > nr_hi
    se = c < nr_lo
    sd = np.where(le, sd_l, np.where(se, sd_s, 1.5 * a))
    return SIG(df, le=le, se=se, sd=pd.Series(sd, index=df.index),
               notes="stop: lado opuesto de la barra NR o 1.5xATR, el mas estrecho")


@strat(49, "ATR expansion", "SUIUSDT", "1h", "breakout")
def s49(df):
    a = B.atr(df, 14)
    hh20 = df["high"].rolling(20).max()
    le = (a > 1.8 * a.rolling(100).median()) & (df["close"] > hh20.shift(1))
    return SIG(df, le=le, sd=2 * a, trail_dist=2 * a)


@strat(50, "Volatility breakout", "JUPUSDT", "15m", "breakout")
def s50(df):
    a = B.atr(df, 14)
    up_lvl = df["open"] + 0.5 * a
    dn_lvl = df["open"] - 0.5 * a
    le = df["close"] > up_lvl
    se = df["close"] < dn_lvl
    return SIG(df, le=le, se=se, sd=1 * a, max_bars=np.full(len(df), 96))


@strat(51, "TTM squeeze", "ETHUSDT", "5m", "breakout",
       notes="momentum aprox como close - SMA20 (el TTM usa regresion)")
def s51(df):
    up_bb, mid_bb, lo_bb = B.bollinger(df["close"], 20, 2)
    up_kc, mid_kc, lo_kc = B.keltner(df, 20, 1.5)
    squeeze = (up_bb < up_kc) & (lo_bb > lo_kc)
    release = (squeeze.shift(1) == True) & (squeeze == False)  # noqa: E712
    mom = df["close"] - B.sma(df["close"], 20)
    a = B.atr(df, 14)
    return SIG(df, le=release & (mom > 0), se=release & (mom < 0),
               lx=mom < 0, sx=mom > 0, sd=1.5 * a)


@strat(52, "Range compression", "BTCUSDT", "15m", "breakout")
def s52(df):
    a = B.atr(df, 14)
    hh = df["high"].rolling(192).max()
    ll = df["low"].rolling(192).min()
    comp = (hh - ll) < 1.5 * a
    le = comp & (df["close"] > hh.shift(1))
    se = comp & (df["close"] < ll.shift(1))
    return SIG(df, le=le, se=se, sd=2 * a)


@strat(53, "Volume breakout", "SOLUSDT", "5m", "breakout")
def s53(df):
    hh = df["high"].rolling(60).max()
    va = df["volume"].rolling(60).mean()
    ll30 = df["low"].rolling(30).min()
    a = B.atr(df, 14)
    return SIG(df, le=(df["close"] > hh.shift(1)) & (df["volume"] > 2 * va),
               lx=df["close"] < ll30.shift(1), sd=1.5 * a)


@strat(54, "Failed breakout fade", "HYPEUSDT", "5m", "breakout")
def s54(df):
    hh = df["high"].rolling(40).max()
    ll = df["low"].rolling(40).min()
    mid = (hh + ll) / 2
    broke = df["close"] > hh.shift(1)
    fail = broke.shift(1) | broke.shift(2) | broke.shift(3)
    se = fail & (df["close"] < hh.shift(1))
    a = B.atr(df, 14)
    sd = (hh.shift(1) - df["close"]).clip(lower=0) + a
    return SIG(df, se=se, sx=df["close"] <= mid, sd=sd,
               notes="stop 1xATR por encima del maximo roto")


@strat(55, "Pivot breakout", "SUIUSDT", "15m", "breakout")
def s55(df):
    ts = ts_of(df)
    day = ts.dt.strftime("%Y-%m-%d")
    daily_hi = df["high"].groupby(day).max().shift(1)
    prev_hi = daily_hi.reindex(day.values).values
    daily_lo = df["low"].groupby(day).min().shift(1)
    prev_lo = daily_lo.reindex(day.values).values
    adx, _, _ = B.adx_di(df, 14)
    c = df["close"]
    a = B.atr(df, 14)
    brk_l = (c > prev_hi) & (adx > 20)
    brk_s = (c < prev_lo) & (adx > 20)
    first_l = brk_l.groupby(day).cumsum() == 1
    first_s = brk_s.groupby(day).cumsum() == 1
    le = brk_l & first_l
    se = brk_s & first_s
    sd = pd.Series(np.where(le, (c - prev_hi).clip(lower=0),
                            np.where(se, (prev_lo - c).clip(lower=0), a)), index=df.index)
    return SIG(df, le=le, se=se, sd=sd, max_bars=np.full(len(df), 48),
               notes="1 entrada por dia")


@strat(56, "Donchian chandelier", "JUPUSDT", "1h", "breakout")
def s56(df):
    hh = df["high"].rolling(30).max()
    a = B.atr(df, 14)
    return SIG(df, le=df["close"] > hh.shift(1), sd=3 * a, trail_dist=3 * a)


@strat(57, "Volatility-percentile breakout", "ETHUSDT", "15m", "breakout",
       notes="exit simetrico anadido (cierre bajo minimo 20b): el prompt no define salida")
def s57(df):
    r = np.log(df["close"] / df["close"].shift(1))
    rv = r.rolling(20).std()
    pct = rv.rolling(500).rank(pct=True)
    hh20 = df["high"].rolling(20).max()
    ll20 = df["low"].rolling(20).min()
    a = B.atr(df, 14)
    le = (pct < 0.10) & (df["close"] > hh20.shift(1))
    lx = df["close"] < ll20.shift(1)
    return SIG(df, le=le, lx=lx, sd=1.5 * a)


@strat(58, "Gap continuation", "BTCUSDT", "1h", "breakout")
def s58(df):
    gap = df["open"] - df["close"].shift(1)
    a = B.atr(df, 14)
    le = gap > 0.8 * a
    se = gap < -0.8 * a
    return SIG(df, le=le, se=se, sd=1.5 * a, max_bars=np.full(len(df), 4))


@strat(59, "Range expansion", "SOLUSDT", "1h", "breakout")
def s59(df):
    a = B.atr(df, 14)
    rng = df["high"] - df["low"]
    pos_in_bar = (df["close"] - df["low"]) / rng.replace(0, np.nan)
    le = (rng > 2 * a) & (pos_in_bar > 0.8)
    sd = (df["close"] - df["low"]).clip(lower=0)
    lx = df["close"] < df["low"].shift(1)
    return SIG(df, le=le, lx=lx, sd=sd)


@strat(60, "Keltner con RSI", "HYPEUSDT", "1h", "breakout")
def s60(df):
    up, mid, lo = B.keltner(df, 20, 2)
    r = B.rsi(df["close"], 14)
    e20 = B.ema(df["close"], 20)
    a = B.atr(df, 14)
    return SIG(df, le=(df["close"] > up) & (r > 60), lx=df["close"] < e20, sd=2 * a)


@strat(61, "Opening drive", "SUIUSDT", "5m", "breakout")
def s61(df):
    ts = ts_of(df)
    day = ts.dt.strftime("%Y-%m-%d")
    in3 = ts.dt.hour == 0
    in3 &= ts.dt.minute < 15
    h1 = df["high"].where(in3).groupby(day).transform("first")
    l1 = df["low"].where(in3).groupby(day).transform("first")
    o1 = df["open"].where(in3).groupby(day).transform("first")
    rng3 = (df["high"].where(in3).groupby(day).transform("max")
            - df["low"].where(in3).groupby(day).transform("min"))
    a = B.atr(df, 14)
    drive = (ts.dt.hour == 0) & (ts.dt.minute == 15)
    le = drive & (df["close"] > o1) & (rng3 > a)
    se = drive & (df["close"] < o1) & (rng3 > a)
    sd = np.where(le, (df["close"] - l1).clip(lower=0),
                  np.where(se, (h1 - df["close"]).clip(lower=0), a))
    return SIG(df, le=le, se=se, sd=pd.Series(sd, index=df.index),
               max_bars=np.full(len(df), 12),
               notes="stop en el extremo de la primera barra de la sesion")


@strat(62, "Volume spike breakout", "JUPUSDT", "5m", "breakout")
def s62(df):
    va = df["volume"].rolling(50).mean()
    hh = df["high"].rolling(10).max()
    a = B.atr(df, 14)
    le = (df["volume"] > 3 * va) & (df["close"] > hh.shift(1))
    return SIG(df, le=le, sd=1.2 * a, target_dist=2 * a)


@strat(63, "Bollinger breakout con ADX", "BTCUSDT", "15m", "breakout")
def s63(df):
    up, mid, lo = B.bollinger(df["close"], 20, 2)
    adx, _, _ = B.adx_di(df, 14)
    a = B.atr(df, 14)
    le = (df["close"] > up) & (adx > adx.shift(1))
    se = (df["close"] < lo) & (adx > adx.shift(1))
    return SIG(df, le=le, se=se, sd=2 * a, trail_dist=2 * a)


@strat(64, "Consolidation breakout", "ETHUSDT", "1h", "breakout")
def s64(df):
    a = B.atr(df, 14)
    hh = df["high"].rolling(30).max()
    ll = df["low"].rolling(30).min()
    comp = (hh - ll) < 3 * a
    ll15 = df["low"].rolling(15).min()
    le = comp & (df["close"] > hh.shift(1))
    lx = df["close"] < ll15.shift(1)
    return SIG(df, le=le, lx=lx, sd=2 * a)


@strat(65, "Stop-hunt reclaim", "SOLUSDT", "15m", "breakout")
def s65(df):
    hh = df["high"].rolling(50).max()
    broke = df["close"] > hh.shift(1)
    back = broke.shift(1) | broke.shift(2)
    se = back & (df["close"] < hh.shift(1))
    a = B.atr(df, 14)
    sd = (df["high"].where(broke).ffill() - df["close"]).clip(lower=0) + 0.25 * a
    return SIG(df, se=se, sd=sd, target_dist=a,
               notes="objetivo 1xATR bajo el nivel reclamado; stop sobre la mecha")


# ============================== SESION/EVENTO 66-80 =========================
@strat(66, "London open range", "BTCUSDT", "15m", "session")
def s66(df):
    ts = ts_of(df)
    hr = ts.dt.hour
    in_rng = (hr == 7)
    day = ts.dt.strftime("%Y-%m-%d")
    hi = df["high"].where(in_rng).groupby(day).transform("max")
    lo = df["low"].where(in_rng).groupby(day).transform("min")
    mid = (hi + lo) / 2
    c = df["close"]
    le = (hr > 7) & (hr < 13) & (c > hi)
    se = (hr > 7) & (hr < 13) & (c < lo)
    sd = (c - mid).abs()
    lx = (hr == 13) & (ts.dt.minute == 0)
    return SIG(df, le=le, se=se, lx=lx, sx=lx, sd=sd)


@strat(67, "Asian range fade", "ETHUSDT", "15m", "session")
def s67(df):
    ts = ts_of(df)
    hr = ts.dt.hour
    day = ts.dt.strftime("%Y-%m-%d")
    in_rng = hr < 6
    hi = df["high"].where(in_rng).groupby(day).transform("max")
    lo = df["low"].where(in_rng).groupby(day).transform("min")
    rng = hi - lo
    mid = (hi + lo) / 2
    c = df["close"]
    a = B.atr(df, 14)
    le = (hr >= 6) & (hr <= 22) & (c <= lo + 0.25 * rng)
    se = (hr >= 6) & (hr <= 22) & (c >= hi - 0.25 * rng)
    sd = np.where(le, (c - (lo - rng)).clip(lower=0),
                  np.where(se, ((hi + rng) - c).clip(lower=0), a))
    lx = (c >= mid) & le
    sx = (c <= mid) & se
    return SIG(df, le=le, se=se, lx=lx, sx=sx, sd=pd.Series(sd, index=df.index))


@strat(68, "Weekend effect", "SOLUSDT", "1h", "session")
def s68(df):
    ts = ts_of(df)
    a = B.atr(df, 14)
    fri = (ts.dt.dayofweek == 4) & (ts.dt.hour == 20)
    mon0 = (ts.dt.dayofweek == 0) & (ts.dt.hour == 0)
    sun23 = (ts.dt.dayofweek == 6) & (ts.dt.hour == 23)
    return SIG(df, le=fri, lx=sun23 | mon0, sd=2 * a)


@strat(69, "US cash open fade", "HYPEUSDT", "15m", "session")
def s69(df):
    ts = ts_of(df)
    a = B.atr(df, 14)
    day = ts.dt.strftime("%Y-%m-%d")
    o1330 = df["open"].where((ts.dt.hour == 13) & (ts.dt.minute == 30)).groupby(day).transform("first")
    move = df["close"] - o1330
    window = (ts.dt.hour == 13) & (ts.dt.minute >= 45) | (ts.dt.hour == 14) & (ts.dt.minute == 0)
    se = window & (move > 1.5 * a)
    le = window & (move < -1.5 * a)
    return SIG(df, le=le, se=se, sd=1 * a, max_bars=np.full(len(df), 4))


@strat(70, "Funding-window fade", "SUIUSDT", "15m", "session")
def s70(df):
    ts = ts_of(df)
    a = B.atr(df, 14)
    hr, mi = ts.dt.hour, ts.dt.minute
    pre = ((hr.isin([7, 15, 23])) & (mi == 30))
    move = df["close"] - df["close"].shift(4)
    se = pre & (move > a)
    le = pre & (move < -a)
    return SIG(df, le=le, se=se, sd=1 * a, max_bars=np.full(len(df), 4),
               notes="ventana de settlement 00/08/16 UTC aproximada por hora de barra")


@strat(71, "Hour-of-day filter", "JUPUSDT", "1h", "session")
def s71(df):
    ts = ts_of(df)
    e20, e50 = B.ema(df["close"], 20), B.ema(df["close"], 50)
    a = B.atr(df, 14)
    win = ts.dt.hour.isin([14, 15, 16, 17])
    return SIG(df, le=cross_up(e20, e50) & win, se=cross_dn(e20, e50) & win,
               lx=cross_dn(e20, e50), sx=cross_up(e20, e50), sd=2 * a)


@strat(72, "Monday persistence", "BTCUSDT", "1h", "session")
def s72(df):
    ts = ts_of(df)
    a = B.atr(df, 14)
    mon0 = (ts.dt.dayofweek == 0) & (ts.dt.hour == 0)
    mon4 = (ts.dt.dayofweek == 0) & (ts.dt.hour == 4)
    open0 = df["open"].where(mon0).ffill()
    ret4 = df["close"] - open0
    le = mon4 & (ret4 > 0)
    se = mon4 & (ret4 < 0)
    tue0 = (ts.dt.dayofweek == 1) & (ts.dt.hour == 0)
    mon23 = (ts.dt.dayofweek == 0) & (ts.dt.hour == 23)
    lx = mon23 | tue0
    return SIG(df, le=le, se=se, lx=lx, sx=lx, sd=2 * a)


@strat(73, "Sunday-to-Monday reversion", "ETHUSDT", "1h", "session")
def s73(df):
    ts = ts_of(df)
    a = B.atr(df, 14)
    sun = ts.dt.dayofweek == 6
    sun_open = df["open"].where(sun & (ts.dt.hour == 0)).groupby(
        ts.dt.strftime("%Y-%m-%d")).transform("first")
    sun_ret = df["close"] - sun_open
    le = sun & (ts.dt.hour == 23) & (sun_ret < -a)
    mon23 = (ts.dt.dayofweek == 0) & (ts.dt.hour == 23)
    tue0 = (ts.dt.dayofweek == 1) & (ts.dt.hour == 0)
    lx = mon23 | tue0
    return SIG(df, le=le, lx=lx, sd=2 * a)


@strat(74, "NY close momentum", "SOLUSDT", "15m", "session")
def s74(df):
    ts = ts_of(df)
    a = B.atr(df, 14)
    sig = (ts.dt.hour == 21) & (ts.dt.minute == 0)
    ret = df["close"] - df["close"].shift(4)
    le = sig & (ret > 0)
    se = sig & (ret < 0)
    end = (ts.dt.hour == 23) & (ts.dt.minute == 0)
    return SIG(df, le=le, se=se, lx=end, sx=end, sd=1.5 * a)


@strat(75, "Month-end drift", "HYPEUSDT", "1h", "session")
def s75(df):
    ts = ts_of(df)
    dim = ts.dt.days_in_month
    dom = ts.dt.day
    month_end = (dim - dom) <= 1
    r = B.rsi(df["close"], 14)
    a = B.atr(df, 14)
    return SIG(df, le=month_end & (r < 50), sd=2 * a, max_bars=np.full(len(df), 24))


@strat(76, "Pre-US compression", "SUIUSDT", "5m", "session")
def s76(df):
    ts = ts_of(df)
    a = B.atr(df, 14)
    day = ts.dt.strftime("%Y-%m-%d")
    w = (ts.dt.hour == 13) & (ts.dt.minute < 30)
    hi = df["high"].where(w).groupby(day).transform("max")
    lo = df["low"].where(w).groupby(day).transform("min")
    comp = (hi - lo) < 0.5 * a
    sig = (ts.dt.hour == 13) & (ts.dt.minute == 30)
    c = df["close"]
    le = sig & comp & (c > hi)
    se = sig & comp & (c < lo)
    sd = np.where(le, (c - lo).clip(lower=0), np.where(se, (hi - c).clip(lower=0), a))
    return SIG(df, le=le, se=se, sd=pd.Series(sd, index=df.index),
               max_bars=np.full(len(df), 12))


@strat(77, "Asia gap fade", "JUPUSDT", "15m", "session")
def s77(df):
    ts = ts_of(df)
    a = B.atr(df, 14)
    first = (ts.dt.hour == 0) & (ts.dt.minute == 0)
    gap = df["open"] - df["close"].shift(1)
    se = first & (gap > a)
    le = first & (gap < -a)
    return SIG(df, le=le, se=se, sd=1 * a, max_bars=np.full(len(df), 8))


@strat(78, "Session VWAP band", "BTCUSDT", "5m", "session")
def s78(df):
    v = sess_vwap(df)
    day = ts_of(df).dt.strftime("%Y-%m-%d")
    dev = df["close"] - v
    sd_day = dev.groupby(day).transform("std")
    # VWAP y sigma del cierre de la sesion ANTERIOR (sin lookahead)
    v_prev = day.map(v.groupby(day).last().shift(1)).values
    sd_prev = day.map(sd_day.groupby(day).last().shift(1)).values
    a = B.atr(df, 14)
    c = df["close"]
    le = c <= v_prev - sd_prev
    se = c >= v_prev + sd_prev
    lx = (c >= v_prev) & le
    sx = (c <= v_prev) & se
    return SIG(df, le=le, se=se, lx=lx, sx=sx, sd=1.5 * a,
               notes="VWAP del dia anterior con sigma del dia anterior")


@strat(79, "Impulse fade", "ETHUSDT", "5m", "session")
def s79(df):
    a = B.atr(df, 14)
    move3 = df["close"] - df["close"].shift(3)
    imp_up = move3 > 2 * a
    imp_dn = move3 < -2 * a
    hi3 = df["high"].rolling(3).max().shift(1)
    lo3 = df["low"].rolling(3).min().shift(1)
    c = df["close"]
    retrace50_up = c <= hi3 - 0.5 * (hi3 - lo3)
    retrace50_dn = c >= lo3 + 0.5 * (hi3 - lo3)
    se = imp_up.shift(1) & retrace50_up
    le = imp_dn.shift(1) & retrace50_dn
    sd = np.where(se, (hi3 - c).clip(lower=0), np.where(le, (c - lo3).clip(lower=0), a))
    return SIG(df, le=le, se=se, sd=pd.Series(sd, index=df.index),
               max_bars=np.full(len(df), 6),
               notes="stop en el extremo del impulso; salida 30 min")


@strat(80, "Opening-bar momentum", "SOLUSDT", "5m", "session")
def s80(df):
    ts = ts_of(df)
    a = B.atr(df, 14)
    first = (ts.dt.hour == 0) & (ts.dt.minute == 0)
    ret1 = df["close"] - df["open"]
    le = first.shift(1, fill_value=False) & (ret1 > 0)
    se = first.shift(1, fill_value=False) & (ret1 < 0)
    return SIG(df, le=le, se=se, sd=1.5 * a, max_bars=np.full(len(df), 12))


# ==================== CROSS-SECTIONAL / RATIO 81-90 =========================
@strat(81, "SOL/ETH z-score", "SOL/ETH", "1h", "pairs")
def s81():
    df = pair_df("SOLUSDT", "ETHUSDT", "1h")
    z = B.zscore(pd.Series(df["close"]), 100)
    std = pd.Series(df["close"]).rolling(100).std()
    a = B.atr(df, 14)
    le = z < -2
    se = z > 2
    lx = z >= 0
    sx = z <= 0
    sd = 2 * std
    sig = SIG(df, le=le, se=se, lx=lx, sx=sx, sd=sd)
    return df, sig


@strat(82, "BTC/ETH z-score", "BTC/ETH", "1h", "pairs")
def s82():
    df = pair_df("BTCUSDT", "ETHUSDT", "1h")
    z = B.zscore(pd.Series(df["close"]), 100)
    std = pd.Series(df["close"]).rolling(100).std()
    le = z < -2
    se = z > 2
    lx = z > -0.5
    sx = z < 0.5
    sig = SIG(df, le=le, se=se, lx=lx, sx=sx, sd=1.5 * std)
    return df, sig


@strat(83, "Weekly basket rotation", "6 perps", "1h", "cross_sectional")
def s83():
    return dict(kind="xs", syms=["BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT",
                                 "SUIUSDT", "JUPUSDT"], tf="1h",
                ret_n=168, rebal_n=168)


@strat(84, "24-bar relative strength", "6 perps", "1h", "cross_sectional")
def s84():
    return dict(kind="xs", syms=["BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT",
                                 "SUIUSDT", "JUPUSDT"], tf="1h",
                ret_n=24, rebal_n=24)


@strat(85, "HYPE/SOL Kalman hedge", "HYPE/SOL", "1h", "pairs",
       notes="Kalman scalar se congelaba -> sustituido por beta OLS rodante 200b "
             "(marcar para re-implementacion antes de creer el numero)")
def s85():
    df = pair_df("HYPEUSDT", "SOLUSDT", "1h", beta_n=200)
    z = B.zscore(pd.Series(df["close"]), 100)
    std = pd.Series(df["close"]).rolling(100).std()
    le = z < -2
    se = z > 2
    lx = z >= 0
    sx = z <= 0
    return df, SIG(df, le=le, se=se, lx=lx, sx=sx, sd=2 * std)


@strat(86, "SUI/JUP cointegration", "SUI/JUP", "1h", "pairs",
       notes="beta OLS rodante 500b sin p-value (aprox): entrada |z|>2, stop |z|=5")
def s86():
    df = pair_df("SUIUSDT", "JUPUSDT", "1h", beta_n=500)
    z = B.zscore(pd.Series(df["close"]), 100)
    std = pd.Series(df["close"]).rolling(100).std()
    le = z < -2
    se = z > 2
    lx = z.abs() < 0.2
    sx = z.abs() < 0.2
    return df, SIG(df, le=le, se=se, lx=lx, sx=sx, sd=3 * std)


@strat(87, "Low-volatility rotation", "6 perps", "1h", "cross_sectional")
def s87():
    return dict(kind="xs", syms=["BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT",
                                 "SUIUSDT", "JUPUSDT"], tf="1h",
                ret_n=0, rebal_n=168, lowvol=True)


@strat(88, "Alt basket vs BTC", "basket/BTC", "1h", "pairs")
def s88():
    # spread = basket igual-ponderado de alts (log) - log BTC
    syms = ["ETHUSDT", "SOLUSDT", "HYPEUSDT", "SUIUSDT", "JUPUSDT"]
    base = B.load("BTCUSDT", "1h")
    frames = [base]
    for s in syms:
        d = B.load(s, "1h")
        frames.append(d)
    n = min(len(f) for f in frames)
    frames = [f.iloc[-n:].reset_index(drop=True) for f in frames]
    la = np.mean([np.log(f["close"].values) for f in frames[1:]], axis=0)
    lb = np.log(frames[0]["close"].values)
    r = la - lb
    o = pd.Series(r).shift(1).fillna(r[0]).values
    df = pd.DataFrame({"open_ts": frames[0]["open_ts"].values, "open": o,
                       "high": np.maximum(o, r), "low": np.minimum(o, r),
                       "close": r, "volume": frames[0]["volume"].values})
    mom_alt = pd.DataFrame({s: B.load(s, "1h")["close"] for s in syms}).pct_change(24).mean(axis=1)
    mom_btc = frames[0]["close"].pct_change(24)
    a_atr = B.atr(df, 14) / df["close"]
    diff = (mom_alt - mom_btc).iloc[-n:].reset_index(drop=True)
    thr = 2 * a_atr
    se = (diff > thr).values
    le = (diff < -thr).values
    lx = (diff.abs() < 0.25 * thr).values
    sx = (diff.abs() < 0.25 * thr).values
    std = pd.Series(df["close"]).rolling(100).std()
    sig = SIG(df, le=pd.Series(le), se=pd.Series(se), lx=pd.Series(lx),
              sx=pd.Series(sx), sd=2 * std)
    return df, sig


@strat(89, "ETH/BTC ratio breakout", "ETH/BTC", "15m", "pairs")
def s89():
    df = pair_df("ETHUSDT", "BTCUSDT", "15m")
    c = pd.Series(df["close"])
    hh = c.rolling(200).max()
    ma100 = c.rolling(100).mean()
    a = B.atr(df, 14)
    le = c > hh.shift(1)
    lx = c < ma100
    return df, SIG(df, le=le, lx=lx, sd=2 * a)


@strat(90, "Volatility-scaled momentum", "6 perps", "1h", "cross_sectional")
def s90():
    return dict(kind="xs", syms=["BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT",
                                 "SUIUSDT", "JUPUSDT"], tf="1h",
                ret_n=336, rebal_n=168, vol_scaled=True)


# ==================== FUNDING 91-100 ========================================
def settlement_bars(df):
    """Indices de barra donde ocurre (o ha ocurrido justo) un settlement 8h."""
    ts = df["open_ts"].values
    out = []
    seen = set()
    for i, t in enumerate(ts):
        hr = (t // 3600000) % 24
        if hr % 8 == 0 and (t // 3600000) not in seen:
            out.append(i)
            seen.add(t // 3600000)
    return out


def funding_on_bars(df, sym):
    f = B.funding_series(sym)
    ts = df["open_ts"].values
    idx = np.searchsorted(f.index.values, ts, side="right") - 1
    vals = f.values[np.clip(idx, 0, len(f) - 1)]
    vals = np.where(idx >= 0, vals, 0.0)
    return pd.Series(vals, index=df.index)


def s_funding_exit_after_next_settle(df, entries_long, entries_short):
    """Marca salida (senal) en la barra previa al siguiente settlement tras entrar."""
    sb = settlement_bars(df)
    n = len(df)
    lx = np.zeros(n, bool)
    sx = np.zeros(n, bool)
    nxt = np.full(n, -1)
    for i in range(n - 1, -1, -1):
        cand = [j for j in sb if j > i]
        nxt[i] = cand[0] if cand else -1
    for i in np.where(entries_long)[0]:
        if 0 < nxt[i] < n:
            lx[nxt[i] - 1] = True
    for i in np.where(entries_short)[0]:
        if 0 < nxt[i] < n:
            sx[nxt[i] - 1] = True
    return lx, sx


@strat(91, "Funding extreme fade", "BTCUSDT", "1h", "funding")
def s91(df):
    fr = funding_on_bars(df, "BTCUSDT")
    a = B.atr(df, 14)
    sb = set(settlement_bars(df))
    at_settle = pd.Series(df.index.isin(sb), index=df.index)
    se = at_settle & (fr > 0.0005)
    le = at_settle & (fr < -0.0002)
    lx, sx = s_funding_exit_after_next_settle(df, le.values, se.values)
    return SIG(df, le=le, se=se, lx=pd.Series(lx), sx=pd.Series(sx), sd=2 * a)


@strat(92, "Funding z-score", "ETHUSDT", "1h", "funding")
def s92(df):
    fr = funding_on_bars(df, "ETHUSDT")
    z = (fr - fr.rolling(30).mean()) / fr.rolling(30).std().replace(0, np.nan)
    a = B.atr(df, 14)
    se = z > 2
    lx = z < 0
    return SIG(df, se=se, sx=lx, sd=2 * a)


@strat(93, "Negative-funding long", "SOLUSDT", "1h", "funding")
def s93(df):
    fr = funding_on_bars(df, "SOLUSDT")
    a = B.atr(df, 14)
    sb = set(settlement_bars(df))
    at = pd.Series(df.index.isin(sb), index=df.index)
    neg3 = (fr < 0) & (fr.shift(1) < 0) & (fr.shift(2) < 0)
    le = at & neg3
    lx = at & (fr > 0.0001)
    return SIG(df, le=le, lx=lx, sd=2 * a)


@strat(94, "Funding persistence", "HYPEUSDT", "1h", "funding")
def s94(df):
    fr = funding_on_bars(df, "HYPEUSDT")
    a = B.atr(df, 14)
    sb = set(settlement_bars(df))
    at = pd.Series(df.index.isin(sb), index=df.index)
    hi3 = (fr > 0.0003) & (fr.shift(1) > 0.0003) & (fr.shift(2) > 0.0003)
    se = at & hi3
    lx, sx = s_funding_exit_after_next_settle(df, np.zeros(len(df), bool), se.values)
    return SIG(df, se=se, sx=pd.Series(sx), sd=1.5 * a,
               notes="hold 1 settlement")


@strat(95, "Price-funding divergence", "SUIUSDT", "1h", "funding")
def s95(df):
    fr = funding_on_bars(df, "SUIUSDT")
    a = B.atr(df, 14)
    ll48 = df["low"].rolling(48).min()
    new_low = df["close"] <= ll48.shift(1)
    f_hi30 = fr >= fr.rolling(30).max()
    se = new_low & f_hi30
    return SIG(df, se=se, sd=2 * a, max_bars=np.full(len(df), 24))


@strat(96, "Funding con trend filter", "JUPUSDT", "1h", "funding")
def s96(df):
    fr = funding_on_bars(df, "JUPUSDT")
    e200 = B.ema(df["close"], 200)
    a = B.atr(df, 14)
    se = (df["close"] > e200) & (fr > 0.0004)
    lx = fr < 0.0001
    return SIG(df, se=se, sx=lx, sd=2 * a)


@strat(97, "Funding-filtered reversion", "BTCUSDT", "1h", "funding")
def s97(df):
    fr = funding_on_bars(df, "BTCUSDT")
    r = B.rsi(df["close"], 14)
    a = B.atr(df, 14)
    le = (r < 35) & (fr < 0)
    lx = r > 60
    return SIG(df, le=le, lx=lx, sd=2 * a)


@strat(98, "Funding vs 4h RSI", "ETHUSDT", "1h", "funding")
def s98(df):
    fr = funding_on_bars(df, "ETHUSDT")
    df4 = df.copy()
    # RSI 4h calculado en velas 4h sinteticas
    ts = ts_of(df)
    key4 = df["open_ts"] // TF_MS["4h"]
    c4 = df["close"].groupby(key4).last()
    rsi4_full = B.rsi(c4, 14).shift(1)  # bloque 4h CERRADO
    rsi4 = rsi4_full.reindex(key4).values
    a = B.atr(df, 14)
    f_med = fr.rolling(30).median()
    se = (pd.Series(rsi4) > 70) & (fr > f_med)
    lx = pd.Series(rsi4) < 55
    return SIG(df, se=se, sx=lx, sd=2 * a)


@strat(99, "Funding momentum", "SOLUSDT", "1h", "funding")
def s99(df):
    fr = funding_on_bars(df, "SOLUSDT")
    ret24 = df["close"].pct_change(24)
    a = B.atr(df, 14)
    le = cross_up(fr, pd.Series(0.0001, index=df.index)) & (ret24 > 0)
    lx = fr < 0
    return SIG(df, le=le, lx=lx, sd=2 * a)


@strat(100, "Funding harvest", "HYPEUSDT", "15m", "funding")
def s100(df):
    fr = funding_on_bars(df, "HYPEUSDT")
    rsi1h = align_hi("HYPEUSDT", "15m", "1h", B.rsi(B.load("HYPEUSDT", "1h")["close"], 14))
    a = B.atr(df, 14)
    ts = ts_of(df)
    mins = ts.dt.hour * 60 + ts.dt.minute
    to_settle = (-(mins) % 480)
    pre30 = to_settle == 30
    se = pre30 & (fr > 0) & (pd.Series(rsi1h) > 70)
    post30 = to_settle == 450  # 30 min tras el settlement
    lx = post30
    return SIG(df, se=se, sx=lx, sd=1 * a)
