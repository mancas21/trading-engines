"""
Strategy Backtester — a proper strategy layer on top of the quant engine signals.

Turns raw alert blasts into a DEFINED, tradeable strategy:
  - Entry: a signal (default MACD cross) with optional regime gate + confluence gate
  - Exit: ATR-based stop loss / take profit / max-hold time exit
  - Position sizing: fixed risk-per-trade % of equity (proper compounding)
  - Costs: commission + slippage + funding carry applied to every trade
  - Validation: full metrics + regime split + walk-forward folds (no lookahead)

Entry uses bar i's CLOSED indicators; fill occurs at bar i+1 open. Stops/targets
are tested on the next bars' high/low — no lookahead, no repaint.
"""
import logging
import numpy as np
import pandas as pd

from quant_alert_engine import TechnicalIndicators, CostModel, compute_metrics, TF_SECONDS, SignalAnalyzer

logger = logging.getLogger(__name__)


class StrategyBacktester:
    def __init__(self, config):
        self.config = config
        self.costs = CostModel(config)
        self.ind = TechnicalIndicators()
        # Reuse a SignalAnalyzer purely for OHLCV fetching (single exchange init)
        self._analyzer = None

    def _fetch(self, symbol, tf, bars):
        """Fetch up to `bars` OHLCV bars with pagination (Binance klines caps at
        ~1000/request). Fetches recent 1000, then pages backward in 1000-bar
        chunks, dedupes and returns the most recent `bars`. Without pagination,
        a longer window silently returns the same 1000-bar cap."""
        if self._analyzer is None:
            self._analyzer = SignalAnalyzer(self.config)
        ex = self._analyzer.exchange.exchange  # raw ccxt instance
        period_ms = TF_SECONDS.get(tf, 300) * 1000
        chunk = 1000
        frames = []
        need = bars
        since_ms = None
        guard = 0
        while need > 0 and guard < 200:
            guard += 1
            try:
                raw = ex.fetch_ohlcv(symbol, tf, since_ms, chunk)
            except Exception as e:
                logger.warning("fetch page failed: %s", e)
                break
            if not raw:
                break
            df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            frames.append(df)
            got = len(df)
            need -= got
            oldest = df['timestamp'].iloc[0]
            since_ms = int(oldest.timestamp() * 1000) - chunk * period_ms
            if since_ms <= 0:
                break
        if not frames:
            return None
        all_df = pd.concat(frames)
        all_df = (all_df.drop_duplicates(subset='timestamp')
                        .sort_values('timestamp').reset_index(drop=True))
        return all_df.tail(bars)

    # ── indicators computed once over the whole series ──
    def _indicators(self, c, h, l, v):
        cfg = self.config
        ind = {}
        ind['macd_line'], ind['macd_signal'], ind['macd_hist'] = self.ind.macd(
            c, cfg.get('macd.fast', 12), cfg.get('macd.slow', 26), cfg.get('macd.signal', 9))
        ind['rsi'] = self.ind.rsi(c, cfg.get('rsi.length', 14))
        ind['k'], ind['d'] = self.ind.stochastic(
            h, l, c, cfg.get('stochastic.k_len', 14), cfg.get('stochastic.d_len', 3), cfg.get('stochastic.smooth', 3))
        ind['bb_upper'], ind['bb_mid'], ind['bb_lower'] = self.ind.bollinger_bands(
            c, cfg.get('volatility.bb_len', 20), cfg.get('volatility.bb_mult', 2.0))
        ind['atr'] = self.ind.atr(h, l, c, cfg.get('volatility.atr_len', 14))
        ind['ema'] = self.ind.ema(c, cfg.get('general.regime_trend_len', 50))
        ind['vol_avg'] = self.ind.sma(v, cfg.get('volume.avg_len', 20))
        return ind

    def _regime(self, c, ema, atr, i):
        if i < 55 or np.isnan(ema[i]) or np.isnan(atr[i]):
            return 0
        band = 0.25 * atr[i]
        if abs(c[i] - ema[i]) < band:
            return 0
        return 1 if c[i] > ema[i] else (-1 if c[i] < ema[i] else 0)

    def _efficiency_ratio(self, c, er_len):
        """Kaufman Efficiency Ratio: |net move| / |path length| over er_len bars.
        Bounded [0,1]. High = trending, low = ranging. Vectorized loop."""
        n = len(c)
        er = np.zeros(n)
        diff_abs = np.abs(np.diff(c, prepend=c[0]))
        for i in range(er_len, n):
            net = abs(c[i] - c[i - er_len])
            path = diff_abs[i - er_len + 1:i + 1].sum()
            er[i] = net / path if path > 0 else 1.0
        return er

    def _range_mask(self, c, ind, er_len, er_range_thresh, bb_confirm):
        """Return a boolean array: True = RANGE state (low efficiency ratio,
        optionally also BB-width compressed). This is the dedicated 2-state
        regime model (0=range, 1=trend via ER)."""
        er = self._efficiency_ratio(c, er_len)
        is_range = er < er_range_thresh
        if bb_confirm:
            bbw = (ind['bb_upper'] - ind['bb_lower']) / np.maximum(ind['bb_mid'], 1e-9)
            med = np.full(len(bbw), np.nan)
            w = max(20, er_len)
            for i in range(w, len(bbw)):
                med[i] = np.nanmedian(bbw[i - w:i])
            compressed = bbw < med  # width below its own median = compression
            is_range = is_range & compressed
        return is_range

    def _value_location(self, c, v, i, window=288, bins=50):
        """Rolling volume-profile value area over the last `window` bars.
        Returns (location, poc, val, vah) where location in {'above','in','below'}.
        Mirrors the order-flow bridge's /context value area (POC/VAH/VAL) so the
        OHLCV backtester can use the same value-area concept as a strategy gate."""
        start = max(0, i - window + 1)
        cw = c[start:i + 1]
        vw = v[start:i + 1]
        if len(cw) < 20:
            return 'in', float(c[i]), float(c[i]), float(c[i])
        hist, edges = np.histogram(cw, bins=bins, weights=vw)
        if hist.sum() <= 0:
            return 'in', float(c[i]), float(c[i]), float(c[i])
        poc_idx = int(np.argmax(hist))
        poc = float((edges[poc_idx] + edges[poc_idx + 1]) / 2)
        total = hist.sum()
        target = total * 0.70
        acc = hist[poc_idx]
        lo = hi = poc_idx
        while acc < target and (lo > 0 or hi < len(hist) - 1):
            if lo == 0:
                hi += 1
            elif hi == len(hist) - 1:
                lo -= 1
            elif hist[lo - 1] > hist[hi + 1]:
                lo -= 1
            else:
                hi += 1
            acc = hist[lo:hi + 1].sum()
        val = float(edges[lo])
        vah = float(edges[hi + 1])
        cur = float(c[i])
        location = 'above' if cur > vah else ('below' if cur < val else 'in')
        return location, poc, val, vah

    def _confluence(self, c, h, l, v, ind, i):
        bull = bear = 0
        if not np.isnan(ind['rsi'][i]):
            bull += 1 if ind['rsi'][i] > 50 else 0
            bear += 1 if ind['rsi'][i] < 50 else 0
        if not np.isnan(ind['macd_line'][i]) and not np.isnan(ind['macd_signal'][i]):
            bull += 1 if ind['macd_line'][i] > ind['macd_signal'][i] else 0
            bear += 1 if ind['macd_line'][i] < ind['macd_signal'][i] else 0
        if not np.isnan(ind['k'][i]) and not np.isnan(ind['d'][i]):
            bull += 1 if ind['k'][i] > ind['d'][i] else 0
            bear += 1 if ind['k'][i] < ind['d'][i] else 0
        if not np.isnan(ind['bb_mid'][i]):
            bull += 1 if c[i] > ind['bb_mid'][i] else 0
            bear += 1 if c[i] < ind['bb_mid'][i] else 0
        if not np.isnan(ind['vol_avg'][i]) and not np.isnan(v[i]):
            bull += 1 if v[i] > ind['vol_avg'][i] else 0
            bear += 1 if v[i] < ind['vol_avg'][i] else 0
        if not np.isnan(ind['ema'][i]):
            bull += 1 if c[i] > ind['ema'][i] else 0
            bear += 1 if c[i] < ind['ema'][i] else 0
        return bull, bear

    def _entry(self, c, ind, i, entry, direction):
        if entry == 'macd':
            ml, sl = ind['macd_line'], ind['macd_signal']
            if i >= 2 and not np.isnan(ml[i]) and not np.isnan(ml[i - 1]):
                if direction in ('both', 'long') and ml[i] > sl[i] and ml[i - 1] <= sl[i - 1]:
                    return 'long'
                if direction in ('both', 'short') and ml[i] < sl[i] and ml[i - 1] >= sl[i - 1]:
                    return 'short'
        elif entry == 'rsi':
            r = ind['rsi']
            if i >= 2 and not np.isnan(r[i]) and not np.isnan(r[i - 1]):
                if direction in ('both', 'long') and r[i] > 50 and r[i - 1] <= 50:
                    return 'long'
                if direction in ('both', 'short') and r[i] < 50 and r[i - 1] >= 50:
                    return 'short'
        elif entry == 'stoch':
            k, d = ind['k'], ind['d']
            if i >= 2 and not np.isnan(k[i]) and not np.isnan(k[i - 1]):
                if direction in ('both', 'long') and k[i] > d[i] and k[i - 1] <= d[i - 1]:
                    return 'long'
                if direction in ('both', 'short') and k[i] < d[i] and k[i - 1] >= d[i - 1]:
                    return 'short'
        elif entry == 'mr_rsi':
            # Mean reversion: reclaim from an extreme. Was <= low, now > low -> long.
            r = ind['rsi']
            low_t, high_t = 35.0, 65.0
            if i >= 2 and not np.isnan(r[i]) and not np.isnan(r[i - 1]):
                if direction in ('both', 'long') and r[i - 1] <= low_t and r[i] > low_t:
                    return 'long'
                if direction in ('both', 'short') and r[i - 1] >= high_t and r[i] < high_t:
                    return 'short'
        elif entry == 'mr_bb':
            # Mean reversion: price pierces a band then closes back inside.
            upper, lower = ind['bb_upper'], ind['bb_lower']
            if i >= 2:
                if direction in ('both', 'long') and c[i - 1] <= lower[i - 1] and c[i] > lower[i]:
                    return 'long'
                if direction in ('both', 'short') and c[i - 1] >= upper[i - 1] and c[i] < upper[i]:
                    return 'short'
        return None

    def simulate(self, symbol, tf, bars=2000, entry='macd', direction='both',
                 regime_gate=True, conf_req=2, stop_mult=2.0, tp_mult=2.0,
                 max_hold=30, risk_pct=0.01, test_fraction=0.25, target_mode='atr',
                 regime_mode='none', er_len=20, er_range_thresh=0.25, bb_confirm=False,
                 value_area_gate=False, va_window=288, df=None):
        """Simulate the strategy over the full series. Returns metrics dict with trades.
        Pass a pre-fetched `df` to reuse a single fetch (reproducible folds).
        target_mode: 'atr' (entry +/- ATR*tp) or 'mean' (BB mid-band, for mean-reversion).
        regime_mode: 'none' (no regime gate) | 'range' (only trade in range state) |
                     'trend' (only trade in trend state), via Efficiency Ratio 2-state model.
        regime_gate: legacy EMA50 trend filter (kept for comparison).
        value_area_gate: trade long only at/below value (POC), short only at/above value
                         (uses the same POC/VAH/VAL concept as the bridge /context)."""
        if df is None:
            df = self._fetch(symbol, tf, bars)
        if df is None or len(df) < 60:
            logger.warning("Not enough data for strategy sim %s %s", symbol, tf)
            return None
        o = df['open'].values
        h = df['high'].values
        l = df['low'].values
        c = df['close'].values
        v = df['volume'].values
        ind = self._indicators(c, h, l, v)
        # 2-state regime model: range mask via Efficiency Ratio (+ optional BB compression)
        is_range = None
        if regime_mode in ('range', 'trend'):
            is_range = self._range_mask(c, ind, er_len, er_range_thresh, bb_confirm)
        n = len(df)
        split_i = int(n * (1 - test_fraction))
        equity = 1.0
        trades = []
        pos = None

        for i in range(55, n):
            if pos is None:
                side = self._entry(c, ind, i, entry, direction)
                if side:
                    if regime_gate:
                        rg = self._regime(c, ind['ema'], ind['atr'], i)
                        if (side == 'long' and rg < 0) or (side == 'short' and rg > 0):
                            side = None
                    if side and is_range is not None:
                        if regime_mode == 'range' and not is_range[i]:
                            side = None
                        elif regime_mode == 'trend' and is_range[i]:
                            side = None
                    if side and value_area_gate:
                        loc, _poc, _val, _vah = self._value_location(c, v, i, window=va_window)
                        if side == 'long' and loc == 'above':
                            side = None
                        elif side == 'short' and loc == 'below':
                            side = None
                    bc = bd = 0
                    if side and conf_req:
                        bc, bd = self._confluence(c, h, l, v, ind, i)
                        if (bc if side == 'long' else bd) < conf_req:
                            side = None
                    if side and i + 1 < n:
                        base_open = float(o[i + 1])
                        # Fix H7: Apply slippage to entry fill price directly
                        slip = self.costs.slippage_pct
                        entry_price = base_open * (1.0 + slip) if side == 'long' else base_open * (1.0 - slip)
                        atr = ind['atr'][i]
                        if np.isnan(atr) or atr <= 0:
                            atr = 0.005 * entry_price
                        stop_dist = atr * stop_mult
                        if target_mode == 'mean' and not np.isnan(ind['bb_mid'][i]):
                            target = float(ind['bb_mid'][i])
                            # ensure target is in the favourable direction
                            if side == 'long':
                                if target <= entry_price:
                                    target = entry_price + atr * tp_mult
                            else:
                                if target >= entry_price:
                                    target = entry_price - atr * tp_mult
                        elif side == 'long':
                            target = entry_price + atr * tp_mult
                        else:
                            target = entry_price - atr * tp_mult
                        if side == 'long':
                            stop = entry_price - stop_dist
                        else:
                            stop = entry_price + stop_dist
                        qty = (equity * risk_pct) / stop_dist
                        if qty <= 0:
                            continue
                        pos = dict(side=side, entry=entry_price, stop=stop, target=target,
                                   entry_bar=i + 1, qty=qty, stop_dist=stop_dist)
            else:
                reason = None
                exit_price = None
                if pos['side'] == 'long':
                    if l[i] <= pos['stop']:
                        exit_price, reason = pos['stop'], 'stop'
                    elif h[i] >= pos['target']:
                        exit_price, reason = pos['target'], 'target'
                else:
                    if h[i] >= pos['stop']:
                        exit_price, reason = pos['stop'], 'stop'
                    elif l[i] <= pos['target']:
                        exit_price, reason = pos['target'], 'target'
                if reason is None and (i - pos['entry_bar']) >= max_hold:
                    exit_price, reason = c[i], 'time'
                if reason is not None:
                    # Fix H7: Apply slippage to exit fill price directly
                    slip = self.costs.slippage_pct
                    realized_exit = exit_price * (1.0 - slip) if pos['side'] == 'long' else exit_price * (1.0 + slip)
                    sign = 1 if pos['side'] == 'long' else -1
                    gross = sign * (realized_exit - pos['entry']) * pos['qty']
                    commission = (pos['entry'] + realized_exit) * pos['qty'] * self.costs.commission_pct
                    hold = i - pos['entry_bar']
                    fund = self.costs.funding_cost(realized_exit, hold, pos['qty'])
                    net = gross - commission - fund
                    equity += net
                    ret = net / (pos['qty'] * pos['entry'])
                    regime = self._regime(c, ind['ema'], ind['atr'], pos['entry_bar'])
                    trades.append(dict(ret=ret, side=pos['side'], entry_idx=pos['entry_bar'],
                                       exit_idx=i, regime=regime, phase='OOS' if pos['entry_bar'] >= split_i else 'IS',
                                       reason=reason))
                    pos = None

        # force-close any open position at the end
        if pos is not None:
            i = n - 1
            sign = 1 if pos['side'] == 'long' else -1
            gross = sign * (c[i] - pos['entry']) * pos['qty']
            cost = (pos['entry'] + c[i]) * pos['qty'] * (self.costs.commission_pct + self.costs.slippage_pct)
            hold = i - pos['entry_bar']
            net = gross - cost - self.costs.funding_cost(c[i], hold, pos['qty'])
            equity += net
            ret = net / (pos['qty'] * pos['entry'])
            regime = self._regime(c, ind['ema'], ind['atr'], pos['entry_bar'])
            trades.append(dict(ret=ret, side=pos['side'], entry_idx=pos['entry_bar'], exit_idx=i,
                               regime=regime, phase='OOS' if pos['entry_bar'] >= split_i else 'IS', reason='end'))

        bpy = int((365 * 24 * 3600) / TF_SECONDS.get(tf, 300))
        metrics = compute_metrics(trades, bpy)
        metrics['trades'] = trades
        metrics['final_equity'] = equity
        return metrics

    def walkforward(self, symbol, tf, bars=2000, n_folds=5, **params):
        """Run the simulation once, then report per-fold + aggregate metrics
        over held-out folds (no lookahead: each trade uses only data up to its bar)."""
        # Fetch ONCE and reuse for both the simulation and the fold boundaries so
        # results are reproducible (fold assignment must not depend on a 2nd fetch).
        df = self._fetch(symbol, tf, bars)
        metrics = self.simulate(symbol, tf, bars=bars, df=df, **params)
        if metrics is None or not metrics.get('trades'):
            return None
        trades = metrics['trades']
        n = len(df)
        test_size = max(10, int(n * 0.2))
        start = n - n_folds * test_size
        folds = [(start + k * test_size, start + (k + 1) * test_size, f"fold{k + 1}") for k in range(n_folds)]

        def fold_of(i):
            for fs, fe, label in folds:
                if fs <= i < fe:
                    return label
            return "train"

        bpy = int((365 * 24 * 3600) / TF_SECONDS.get(tf, 300))
        fold_trades = {}
        for t in trades:
            t['fold'] = fold_of(t['entry_idx'])
            fold_trades.setdefault(t['fold'], []).append(t)
        fold_stats = {k: compute_metrics(v, bpy) for k, v in fold_trades.items() if v}
        agg = compute_metrics(trades, bpy)
        return {"fold_stats": fold_stats, "aggregate": agg, "n_folds": n_folds, "final_equity": metrics.get('final_equity')}


def _fmt(st):
    if st is None or not st.get('n_trades'):
        return "n=0"
    return (f"n={st.get('n_trades')} win={st.get('win_rate',0)*100:.1f}% pf={st.get('profit_factor',0):.2f} "
            f"sharpe={st.get('sharpe',0):.2f} sortino={st.get('sortino',0):.2f} calmar={st.get('calmar',0):.2f} "
            f"maxdd={st.get('max_dd_pct',0):.1f}% tot={st.get('total_return',0)*100:.1f}% effN={st.get('effective_n',0):.0f}")


if __name__ == '__main__':
    from config_schema import load_config
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('symbol', nargs='?', default='SOL/USDT:USDT')
    parser.add_argument('tf', nargs='?', default='5m')
    parser.add_argument('--bars', type=int, default=3000)
    parser.add_argument('--entry', default='macd', choices=['macd', 'rsi', 'stoch', 'mr_rsi', 'mr_bb'])
    parser.add_argument('--direction', default='both', choices=['both', 'long', 'short'])
    parser.add_argument('--target-mode', dest='target_mode', default='atr', choices=['atr', 'mean'])
    parser.add_argument('--regime-mode', dest='regime_mode', default='none', choices=['none', 'range', 'trend'])
    parser.add_argument('--er-len', type=int, default=20)
    parser.add_argument('--er-thresh', type=float, default=0.25)
    parser.add_argument('--bb-confirm', dest='bb_confirm', action='store_true')
    parser.add_argument('--va-gate', dest='value_area_gate', action='store_true',
                        help='gate entries by value-area location (POC)')
    parser.add_argument('--va-window', type=int, default=288)
    parser.add_argument('--regime', dest='regime_gate', action='store_true', default=True)
    parser.add_argument('--no-regime', dest='regime_gate', action='store_false')
    parser.add_argument('--conf', type=int, default=2)
    parser.add_argument('--stop', type=float, default=2.0)
    parser.add_argument('--tp', type=float, default=2.0)
    parser.add_argument('--hold', type=int, default=30)
    parser.add_argument('--risk', type=float, default=0.01)
    parser.add_argument('--walkforward', action='store_true')
    parser.add_argument('--folds', type=int, default=5)
    args = parser.parse_args()

    cfg = load_config('config.json')
    bt = StrategyBacktester(cfg)
    params = dict(entry=args.entry, direction=args.direction, regime_gate=args.regime_gate,
                  conf_req=args.conf, stop_mult=args.stop, tp_mult=args.tp,
                  max_hold=args.hold, risk_pct=args.risk, target_mode=args.target_mode,
                  regime_mode=args.regime_mode, er_len=args.er_len,
                  er_range_thresh=args.er_thresh, bb_confirm=args.bb_confirm,
                  value_area_gate=args.value_area_gate, va_window=args.va_window)
    if args.walkforward:
        res = bt.walkforward(args.symbol, args.tf, bars=args.bars, n_folds=args.folds, **params)
        if res:
            print(f"=== STRATEGY WALK-FORWARD {args.symbol} {args.tf} ({args.entry}, "
                  f"conf={args.conf}, regime={args.regime_gate}, stop={args.stop}, tp={args.tp}, hold={args.hold}) ===")
            for label, st in res['fold_stats'].items():
                print(f"  [{label}] {_fmt(st)}")
            print("AGGREGATE:", _fmt(res['aggregate']))
            print("final_equity:", round(res['final_equity'], 4))
            if res['aggregate'].get('regime_stats'):
                print("REGIME:", {k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in res['aggregate']['regime_stats'].items()})
    else:
        m = bt.simulate(args.symbol, args.tf, bars=args.bars, **params)
        print(f"=== STRATEGY BACKTEST {args.symbol} {args.tf} ===")
        print("FULL:", _fmt(m))
        print("final_equity:", round(m.get('final_equity', 1.0), 4))
        if m.get('regime_stats'):
            print("REGIME:", {k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in m['regime_stats'].items()})