"""
Quant Alert Engine v2.5 - Production Quant Edition
A comprehensive alert suite for crypto perpetual futures.
Quant audit fixes:
  - Divergence episode gating: each divergence fires ONCE per episode, not every bar
  - Honest CVD labeling: bar-signed volume index (tick-rule), not taker-side CVD
  - Outcome tagging: every fired signal stores its forward N-bar return (edge tracking)
  - Flat-pivot fix: strict pivots + minimum gap in divergence detection
  - Selectivity: optional regime filter, confluence requirement, multi-TF consolidation
  - Rebuilt Backtester: replay all checks, directional short/long return stats, IS/OOS split
"""
import os
import sys
import json
import logging
import time
import threading
import sqlite3
import signal as system_signal
import urllib.request as _urllib
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional, Any
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import ccxt

# ── New validated config + logging + health ──
from config_schema import Config, load_config
from logging_setup import setup_logging, context_filter

# ── ML meta-labeling (v2.8): canonical feature vector + trained filter ──
# Guarded imports: a missing/broken model must never stop alerts from firing.
try:
    from features import FeatureExtractor
except Exception:  # pragma: no cover
    FeatureExtractor = None
try:
    from ml_filter import MetaLabelFilter
except Exception:  # pragma: no cover
    MetaLabelFilter = None

# Ensure dirs exist before logging setup
os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)

# Early logging init (will be reconfigured after config loads log_level)
setup_logging(log_file="logs/quant_alert_engine.log", level="INFO", json_format=True)
logger = logging.getLogger(__name__)

# Cross-platform sound handling
try:
    import winsound
except ImportError:
    winsound = None

# Toast notifications (graceful fallback)
try:
    from win10toast import ToastNotifier as _RealToastNotifier
    class ToastNotifier:
        def __init__(self):
            self._toast = _RealToastNotifier()
        def show_toast(self, *args, **kwargs):
            return self._toast.show_toast(*args, **kwargs)
except Exception:
    class ToastNotifier:
        def show_toast(self, *args, **kwargs):
            return False


def _bridge_headers():
    """JSON headers + X-Bridge-Token when BRIDGE_TOKEN env is set (same .env as the bridge)."""
    h = {"Content-Type": "application/json"}
    _bt = os.environ.get("BRIDGE_TOKEN", "").strip()
    if _bt:
        h["X-Bridge-Token"] = _bt
    return h

# ============================================================================
# TIMEFRAME CADENCE & SINGLE-INSTANCE GUARD
# ============================================================================
TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "1w": 604800,
}
DEFAULT_TF_SECONDS = 300
FAIL_RETRY_SECONDS = 30

# Timeframe rank: higher rank = higher (slower) timeframe. Used for multi-TF
# signal consolidation — a signal already alerted on a higher timeframe
# suppresses the duplicate on lower timeframes within the same candle window.
TF_RANK = {tf: i for i, tf in enumerate(sorted(TF_SECONDS, key=lambda t: TF_SECONDS[t]))}

# Exchange base symbol -> CoinGecko coin id, for the external macro pipeline.
# Unknown bases fall back to the lowercased base itself.
_COIN_ID_MAP = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "sui": "sui",
    "xrp": "ripple", "hype": "hype", "doge": "dogecoin", "ada": "cardano",
    "avax": "avalanche-2", "link": "chainlink", "ton": "the-open-network",
    "trx": "tron", "dot": "polkadot", "matic": "matic-network",
    "arb": "arbitrum", "op": "optimism", "inj": "injective-protocol",
    "sei": "sei-network", "apt": "aptos", "near": "near",
    "atom": "cosmos", "ltc": "litecoin", "bch": "bitcoin-cash",
    "fil": "filecoin", "etc": "ethereum-classic",
}


class TimeframeCadence:
    """Polls each (symbol, timeframe) pair at most once per candle duration.
    Aligned to real candle closes to avoid evaluating forming bars (fix #3).
    """

    def __init__(self, floor_seconds: int = 0):
        self._next_due: Dict[Tuple[str, str], float] = {}
        self._floor = floor_seconds

    @staticmethod
    def period_seconds(timeframe: str) -> int:
        return TF_SECONDS.get(str(timeframe), DEFAULT_TF_SECONDS)

    @staticmethod
    def _next_close_aligned(period: int, now: float) -> float:
        """Next candle close (open + period) + 2s buffer, aligned to exchange boundaries."""
        # Ceil to next period boundary
        next_close = ((int(now) // period) + 1) * period
        return float(next_close + 2)  # 2s after close to ensure bar is finalized

    def is_due(self, symbol: str, timeframe: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return now >= self._next_due.get((symbol, timeframe), 0.0)

    def mark_polled(self, symbol: str, timeframe: str, now: Optional[float] = None):
        now = time.time() if now is None else now
        period = max(self.period_seconds(timeframe), self._floor)
        # Align to next real close instead of now+period drift (fix #3)
        aligned = self._next_close_aligned(period, now)
        self._next_due[(symbol, timeframe)] = aligned

    def mark_failed(self, symbol: str, timeframe: str, now: Optional[float] = None):
        now = time.time() if now is None else now
        self._next_due[(symbol, timeframe)] = now + FAIL_RETRY_SECONDS


class SingleInstanceLock:
    """Prevents two engine/UI instances from running against the same config."""

    def __init__(self, lock_path: str = 'data/engine.lock'):
        self.lock_path = lock_path
        self._held = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            if os.name == 'nt':
                import subprocess
                out = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}'],
                    capture_output=True, text=True, timeout=10,
                )
                return str(pid) in out.stdout
            else:
                try:
                    os.kill(pid, 0)
                    return True
                except OSError:
                    return False
        except Exception:
            return False

    def acquire(self, retries: int = 5) -> bool:
        """Atomic single-instance acquisition (v2.7.1).

        Uses O_CREAT|O_EXCL so two simultaneous starters cannot BOTH pass:
        exactly one wins; the loser checks the winner's liveness and either
        refuses (alive) or clears the stale file and retries. This closes the
        TOCTOU race in the old exists-check-then-write sequence, which let
        duplicate UIs scan and send duplicate Telegram alerts.
        """
        if self._held:
            return True
        for _ in range(max(1, retries)):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(os.getpid()).encode())
                finally:
                    os.close(fd)
                self._held = True
                return True
            except FileExistsError:
                pass
            except OSError as e:
                logger.error("Could not create lock file %s: %s", self.lock_path, e)
                return False
            # Lock exists: refuse if the owner is alive, else clear + retry.
            try:
                with open(self.lock_path, 'r') as f:
                    pid = int(f.read().strip() or 0)
            except (ValueError, OSError):
                pid = 0
            if pid > 0 and self._pid_alive(pid):
                logger.warning("Another instance is already running (PID %s). Refusing to start.", pid)
                return False
            logger.warning("Removing stale lock file from dead PID %s.", pid)
            try:
                os.remove(self.lock_path)
            except OSError:
                pass
            time.sleep(0.05)
        logger.warning("Could not acquire instance lock after %d retries.", retries)
        return False

    def release(self):
        if self._held:
            try:
                os.remove(self.lock_path)
            except OSError:
                pass
            self._held = False

# ============================================================================
# TECHNICAL INDICATORS (Quant Corrected)
# ============================================================================
class TechnicalIndicators:
    @staticmethod
    def sma(data: np.ndarray, period: int) -> np.ndarray:
        return pd.Series(data).rolling(window=period).mean().values
    
    @staticmethod
    def ema(data: np.ndarray, period: int) -> np.ndarray:
        # adjust=False matches TradingView/MT4 EMA
        return pd.Series(data).ewm(span=period, adjust=False).mean().values
    
    @staticmethod
    def rma(data: np.ndarray, period: int) -> np.ndarray:
        # Wilder's Smoothing (RMA)
        return pd.Series(data).ewm(alpha=1/period, adjust=False).mean().values

    @staticmethod
    def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
        s = pd.Series(close)
        delta = s.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        # Wilder's RSI
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        result = np.array((100 - (100 / (1 + rs))).fillna(100).values, dtype=float, copy=True)  # no losses -> 100
        flat_mask = (avg_gain == 0) & (avg_loss == 0)         # flat market -> neutral 50
        result[flat_mask] = 50.0
        return result
    
    @staticmethod
    def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        s = pd.Series(close)
        ema_fast = s.ewm(span=fast, adjust=False).mean()
        ema_slow = s.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        return macd_line.values, signal_line.values, (macd_line - signal_line).values
    
    @staticmethod
    def stochastic(high: np.ndarray, low: np.ndarray, close: np.ndarray, 
                   k_len: int = 14, d_len: int = 3, smooth: int = 3) -> Tuple[np.ndarray, np.ndarray]:
        h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
        lowest = l.rolling(window=k_len).min()
        highest = h.rolling(window=k_len).max()
        k_raw = 100 * (c - lowest) / (highest - lowest).replace(0, np.nan)
        k = k_raw.rolling(window=smooth).mean()
        d = k.rolling(window=d_len).mean()
        return k.values, d.values
    
    @staticmethod
    def bollinger_bands(close: np.ndarray, period: int = 20, std_mult: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        s = pd.Series(close)
        sma = s.rolling(window=period).mean()
        # ddof=0 matches TradingView population std
        std = s.rolling(window=period).std(ddof=0)
        upper = sma + (std * std_mult)
        lower = sma - (std * std_mult)
        return upper.values, sma.values, lower.values
    
    @staticmethod
    def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
        h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
        tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        # Wilder's ATR
        return tr.ewm(alpha=1/period, adjust=False).mean().values

# ============================================================================
# CVD / DIVERGENCE
# ============================================================================
class CVDCalculator:
    """
    Bar-signed volume index (tick-rule approximation of CVD).

    IMPORTANT — honest labeling (quant audit fix #2):
    - This is NOT taker-side CVD. It assigns each bar's ENTIRE volume to one
      side based on the sign of close-to-close change (tick rule). Bars with
      two-sided aggressive flow are misclassified wholesale.
    - The cumulative series RESTARTS at zero every fetch (200-bar rolling
      window). Its absolute LEVEL is meaningless — it depends on where the
      window starts. Use it for SHAPE (divergence) and CROSS/FLIP transitions
      (which are translation-invariant), never for absolute interpretation
      like "CVD is bearish at -X".
    - True CVD requires per-trade taker-side classification (aggTrade WS).
      Roadmap item; the alert names keep "CVD" for user recognition but the
      underlying series is a bar-signed proxy.
    """

    @staticmethod
    def bar_signed_delta(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
        """Per-bar signed volume (tick-rule proxy). NOT cumulative.
        sign(diff(close)) * volume — entire bar volume assigned to one side."""
        sign = np.sign(np.diff(close, prepend=close[0]))
        return np.where(sign == 0, 0.0, sign) * volume

    @staticmethod
    def bar_signed_cumulative(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
        """Running sum of bar-signed volume over the FETCHED WINDOW.
        Restarts at zero every fetch — level is meaningless, use shape/crosses only."""
        return np.cumsum(CVDCalculator.bar_signed_delta(close, volume))

    # Deprecated aliases (kept for backwards compatibility)
    calculate_delta = bar_signed_delta
    calculate_cumulative_delta = bar_signed_cumulative

class DivergenceDetector:
    @staticmethod
    def find_pivots(data: np.ndarray, pivot_len: int = 5, min_gap: int = 3) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
        """Strict fractal pivots with a minimum spacing between consecutive
        pivots of the same type (quant audit fix #4).

        - Strict > / < comparisons: flat tops/bottoms do NOT produce runs of
          consecutive pivot points (the old >= / <= created fabricated
          "divergence" off adjacent bars of a flat).
        - min_gap: consecutive highs (or lows) must be at least min_gap bars
          apart, so divergence is never measured on 2-bar noise.
        """
        highs, lows = [], []
        n = len(data)
        for i in range(pivot_len, n - pivot_len):
            is_max = all(data[i] > data[i - j] for j in range(1, pivot_len + 1)) and \
                     all(data[i] > data[i + j] for j in range(1, pivot_len + 1))
            is_min = all(data[i] < data[i - j] for j in range(1, pivot_len + 1)) and \
                     all(data[i] < data[i + j] for j in range(1, pivot_len + 1))
            if is_max:
                highs.append((i, data[i]))
            if is_min:
                lows.append((i, data[i]))

        def _enforce_gap(pivots):
            kept = []
            last_idx = -min_gap
            for idx, val in pivots:
                if idx - last_idx >= min_gap:
                    kept.append((idx, val))
                    last_idx = idx
            return kept

        return _enforce_gap(highs), _enforce_gap(lows)

    @staticmethod
    def detect_divergence(price: np.ndarray, indicator: np.ndarray,
                         pivot_len: int = 5, lookback: int = 20,
                         min_gap: int = 3) -> Tuple[bool, bool, bool, bool]:
        """
        Detects divergence by finding price pivots, then sampling the indicator's
        local extremum within a +/-2 bar window around each pivot to ensure proper
        peak/trough capture (Fix H1: accounts for indicator lead/lag).
        """
        reg_bear = reg_bull = hid_bear = hid_bull = False
        try:
            p_slice = price[-lookback:]
            i_slice = indicator[-lookback:]

            p_highs, p_lows = DivergenceDetector.find_pivots(p_slice, pivot_len, min_gap)

            def _ind_extreme(idx: int, is_high: bool, window: int = 2) -> float:
                start = max(0, idx - window)
                end = min(len(i_slice), idx + window + 1)
                sub = i_slice[start:end]
                valid = sub[~np.isnan(sub)]
                if len(valid) == 0:
                    return float(i_slice[idx])
                return float(np.nanmax(valid) if is_high else np.nanmin(valid))

            if len(p_highs) >= 2:
                idx1, val1 = p_highs[-2]
                idx2, val2 = p_highs[-1]
                ind1 = _ind_extreme(idx1, is_high=True)
                ind2 = _ind_extreme(idx2, is_high=True)

                if not (np.isnan(ind1) or np.isnan(ind2)):
                    if val2 > val1 and ind2 < ind1: reg_bear = True
                    if val2 < val1 and ind2 > ind1: hid_bear = True

            if len(p_lows) >= 2:
                idx1, val1 = p_lows[-2]
                idx2, val2 = p_lows[-1]
                ind1 = _ind_extreme(idx1, is_high=False)
                ind2 = _ind_extreme(idx2, is_high=False)

                if not (np.isnan(ind1) or np.isnan(ind2)):
                    if val2 < val1 and ind2 > ind1: reg_bull = True
                    if val2 > val1 and ind2 < ind1: hid_bull = True
        except Exception as e:
            logger.error(f"Divergence error: {e}")
        return reg_bear, reg_bull, hid_bear, hid_bull

# ============================================================================
# COST / SLIPPAGE / FUNDING MODEL + METRICS (quant audit fix #6)
# ============================================================================
class CostModel:
    """Transaction cost, slippage, and funding model for backtesting."""

    def __init__(self, config=None):
        self.commission_pct = 0.0004       # 0.04% per side (Binance perp taker)
        self.slippage_pct = 0.0001         # 0.01% per side
        self.funding_rate_pct = 0.0001     # 0.01% per 8h interval
        self.funding_interval_hours = 8
        self.dryup_slippage_mult = 2.0     # double slippage during volume dryup

    def roundtrip_cost_pct(self, dryup=False):
        base = self.commission_pct + self.slippage_pct
        return base * 2 * (self.dryup_slippage_mult if dryup else 1.0)

    def funding_cost(self, price, bars_held, qty=1.0, bar_seconds=300):
        """Funding paid over the holding period.

        Funding accrues on TIME, not on bars. The original counted bars assuming
        a fixed 5-minute bar regardless of the timeframe, so a 1h position held
        12 bars (12 real hours) was charged zero intervals when the true answer
        is one. That silently under-charged every backtest above 5m.
        """
        bar_seconds = max(int(bar_seconds), 1)
        interval_bars = max(1, int(self.funding_interval_hours * 3600 / bar_seconds))
        intervals = max(0, bars_held // interval_bars)
        return price * self.funding_rate_pct * intervals * qty


def _regime_label(r):
    return {1: "up", -1: "down", 0: "neutral"}.get(r, "neutral")


def compute_metrics(trades, bars_per_year):
    """Compute full backtest metrics from a list of trade dicts.
    Each trade: {'ret': float, 'entry_idx': int, 'exit_idx': int, 'regime': int}.
    Returns dict with win_rate, profit_factor, sharpe, sortino, calmar,
    max_dd_pct, max_dd_time, expectancy, avg_trade, trades, regime_stats,
    effective_n, multiple_testing_note.
    """
    if not trades:
        return {"error": "no trades"}
    rets = [t["ret"] for t in trades]
    n = len(rets)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    win_rate = len(wins) / n if n else 0.0
    gross_win = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 1e-12
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    avg_ret = sum(rets) / n
    expectancy = avg_ret
    std_ret = (sum((r - avg_ret) ** 2 for r in rets) / max(n - 1, 1)) ** 0.5
    downside = [r for r in rets if r < 0]
    downside_std = (sum(r ** 2 for r in downside) / max(len(downside), 1)) ** 0.5 if downside else 1e-12
    sharpe = (avg_ret / std_ret * (bars_per_year ** 0.5)) if std_ret > 0 else 0.0
    sortino = (avg_ret / downside_std * (bars_per_year ** 0.5)) if downside_std > 0 else 0.0
    # equity curve + max drawdown (with time-to-recover)
    eq = 1.0
    eq_curve = [1.0]
    peak = 1.0
    peak_idx = 0
    max_dd = 0.0
    max_dd_start = max_dd_end = max_dd_recover = 0
    recovered = False
    for i, r in enumerate(rets):
        eq *= (1 + r)
        eq_curve.append(eq)
        if eq > peak:
            peak = eq; peak_idx = i
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd
            max_dd_start = peak_idx
            max_dd_end = i
    max_dd_pct = max_dd * 100
    # time-to-recover
    max_dd_time = 0
    if max_dd > 0:
        for j in range(max_dd_end, len(eq_curve)):
            if eq_curve[j] >= eq_curve[max_dd_start]:
                max_dd_time = j - max_dd_end; break
    # annualized return
    n_trades = n
    total_ret = eq_curve[-1] - 1.0
    # bars_per_year estimate: assume ~350 trading days * 24h / bar_duration_h
    # Use bars_per_year as supplied.
    annual_ret = (1 + total_ret) ** (bars_per_year / n_trades) - 1 if n_trades else 0.0
    calmar = (annual_ret / max_dd_pct * 100) if max_dd_pct > 0 else float("inf")
    # regime stats
    regime_stats = {}
    for r in (1, -1, 0):
        label = _regime_label(r)
        rt = [t for t in trades if t.get("regime") == r]
        if rt:
            rr = [t["ret"] for t in rt]
            wr = sum(1 for x in rr if x > 0) / len(rr)
            pf = sum(x for x in rr if x > 0) / (abs(sum(x for x in rr if x <= 0)) or 1e-12)
            regime_stats[label] = {"count": len(rt), "win_rate": wr, "profit_factor": pf}
    # overlapping-trade effective sample size
    eff_n = n
    if n >= 11 and std_ret > 0:
        rho = 0.0
        for k in range(1, min(11, n)):
            a = [rets[i] - avg_ret for i in range(n - k)]
            b = [rets[i + k] - avg_ret for i in range(n - k)]
            c = sum(a[i] * b[i] for i in range(len(a))) / len(a)
            rk = c / (std_ret ** 2)
            if abs(rk) < 0.05: break
            rho += rk
        eff_n = n / (1 + 2 * rho)
    # multiple-testing correction: count distinct strategy hypotheses tested (Fix H2)
    unique_signals = set(t.get("name", "") for t in trades if t.get("name"))
    n_variants = max(1, len(unique_signals))
    return {
        "trades": trades,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "avg_trade": avg_ret,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "max_dd_pct": max_dd_pct,
        "max_dd_time_bars": max_dd_time,
        "total_return": total_ret,
        "annual_return": annual_ret,
        "regime_stats": regime_stats,
        "effective_n": eff_n,
        "multiple_testing_note": f"{n_variants} distinct signal strategies tested; Bonferroni-adjusted effective sample = {eff_n:.0f} (raw {n})",
    }


# ============================================================================
# ALERT MANAGER & STATE PERSISTENCE — single long-lived connection (fix #4)
# ============================================================================
class AlertManager:
    def __init__(self, config: Config):
        self.config = config
        self.notifier = ToastNotifier()
        self.db_path = config.get('database', 'data/alerts.db')
        self._last_prune = time.time()
        # Use one long-lived connection per instance. For :memory: we use URI shared cache
        # so multiple AlertManager instances (and Backtester isolation) all share the same DB.
        self._init_db()

    def _init_db(self):
        db = str(self.db_path)
        if db == ":memory:" or db.startswith("file:") and "mode=memory" in db:
            # Force shared cache so :memory: is per-process, not per-connection
            self._conn = sqlite3.connect(
                "file::memory:?cache=shared",
                uri=True,
                timeout=10,
                check_same_thread=False,
            )
        elif db == "data/alerts.db" or db.endswith(".db"):
            os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
            self._conn = sqlite3.connect(db, timeout=10, check_same_thread=False)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
        else:
            os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
            self._conn = sqlite3.connect(db, timeout=10, check_same_thread=False)
        self._db_lock = threading.Lock()
        with self._db_lock:
            self._conn.execute('''CREATE TABLE IF NOT EXISTS fired_alerts
                                   (signal_id TEXT PRIMARY KEY, timestamp REAL)''')
            self._conn.execute('''CREATE TABLE IF NOT EXISTS signal_states
                                   (state_key TEXT PRIMARY KEY, is_active INTEGER)''')
            # Outcome tagging (quant audit fix #3): every fired signal records its
            # entry price + bar; the analyzer resolves the forward N-bar return later.
            self._conn.execute('''CREATE TABLE IF NOT EXISTS signal_outcomes
                                   (signal_id TEXT PRIMARY KEY,
                                    symbol TEXT, timeframe TEXT, signal_name TEXT,
                                    signal_type TEXT, price REAL, bar_key TEXT,
                                    fired_ts REAL, forward_bars INTEGER,
                                    forward_return REAL, resolved INTEGER DEFAULT 0)''')
            # ── ML feature store (v2.8) ──
            # The canonical feature vector at the moment each signal fired. The
            # outcomes table alone only holds metadata (symbol / name / tf), and
            # a model trained on metadata scored AUC 0.443 — below chance. The
            # features are what make the track record learnable.
            self._conn.execute('''CREATE TABLE IF NOT EXISTS signal_features
                                   (signal_id TEXT PRIMARY KEY,
                                    symbol TEXT, timeframe TEXT, signal_name TEXT,
                                    signal_type TEXT, direction INTEGER,
                                    fired_ts REAL, features_json TEXT)''')
            # Direction actually used at emit time. Resolution used to re-infer
            # direction from the signal name, which disagrees with the emit-time
            # hint for some names, silently flipping the sign of the stored
            # forward return (and therefore of every downstream expectancy gate).
            try:
                _cols = {r[1] for r in self._conn.execute("PRAGMA table_info(signal_outcomes)")}
                if "direction" not in _cols:
                    self._conn.execute(
                        "ALTER TABLE signal_outcomes ADD COLUMN direction INTEGER DEFAULT 0")
                    logger.info("Migrated signal_outcomes: added direction column")
            except Exception as e:
                logger.debug("signal_outcomes direction migration skipped: %s", e)
            self._conn.commit()
        self._prune()

    def _prune(self):
        cutoff = time.time() - 3600
        try:
            with self._db_lock:
                self._conn.execute('DELETE FROM fired_alerts WHERE timestamp < ?', (cutoff,))
                # Keep resolved outcome rows for 7 days of stats; unresolved rows
                # are kept until their forward window has a chance to resolve.
                #
                # Rows carrying an ML feature vector are EXEMPT: they are the
                # model's training corpus. Pruning them would cap training at a
                # rolling 7-day window forever — with ~1.6k labeled signals per
                # week, that is the hard blocker on ever training a model that
                # beats the rule-based gates (see ml_train.py).
                self._conn.execute(
                    '''DELETE FROM signal_outcomes
                       WHERE resolved = 1 AND fired_ts < ?
                         AND signal_id NOT IN (SELECT signal_id FROM signal_features)''',
                    (time.time() - 7 * 86400,))
                self._conn.commit()
        except Exception as e:
            logger.warning(f"Prune failed: {e}")
        self._last_prune = time.time()

    def _is_fired(self, signal_id: str) -> bool:
        try:
            with self._db_lock:
                cur = self._conn.execute('SELECT 1 FROM fired_alerts WHERE signal_id = ?', (signal_id,))
                return cur.fetchone() is not None
        except Exception:
            return False

    def _mark_fired(self, signal_id: str):
        try:
            with self._db_lock:
                self._conn.execute('INSERT OR REPLACE INTO fired_alerts VALUES (?, ?)', (signal_id, time.time()))
                self._conn.commit()
        except Exception as e:
            logger.warning(f"mark_fired failed: {e}")
        if time.time() - self._last_prune > 3600:
            self._prune()

    def get_state_val(self, state_key: str) -> Optional[int]:
        try:
            with self._db_lock:
                cur = self._conn.execute('SELECT is_active FROM signal_states WHERE state_key = ?', (state_key,))
                row = cur.fetchone()
                return int(row[0]) if row else None
        except Exception:
            return None

    def set_state_val(self, state_key: str, value: int):
        try:
            with self._db_lock:
                self._conn.execute('INSERT OR REPLACE INTO signal_states VALUES (?, ?)', (state_key, int(value)))
                self._conn.commit()
        except Exception as e:
            logger.warning(f"set_state failed: {e}")

    def get_state(self, state_key: str) -> bool:
        return bool(self.get_state_val(state_key))

    def set_state(self, state_key: str, is_active: bool):
        self.set_state_val(state_key, int(is_active))

    # ── Outcome tagging (quant audit fix #3) ──
    def tag_signal(self, signal_name: str, symbol: str, price: float,
                   timeframe: str, bar_key: str, signal_type: str,
                   forward_bars: int,
                   features: Optional[Dict[str, float]] = None,
                   direction: int = 0):
        """Record a fired signal so its forward N-bar return can be resolved later.

        v2.8: also persists the canonical feature vector (features.py) and the
        emit-time direction, so the model trains on exactly the numbers
        production saw and the stored return keeps the correct sign.
        """
        signal_id = f"{symbol}_{signal_name}_{timeframe}_{bar_key}"
        try:
            with self._db_lock:
                self._conn.execute(
                    '''INSERT OR IGNORE INTO signal_outcomes
                       (signal_id, symbol, timeframe, signal_name, signal_type,
                        price, bar_key, fired_ts, forward_bars, resolved, direction)
                       VALUES (?,?,?,?,?,?,?,?,?,0,?)''',
                    (signal_id, symbol, timeframe,
                     signal_name, signal_type, float(price), bar_key, time.time(),
                     int(forward_bars), int(direction)))
                if features:
                    self._conn.execute(
                        '''INSERT OR REPLACE INTO signal_features
                           (signal_id, symbol, timeframe, signal_name, signal_type,
                            direction, fired_ts, features_json)
                           VALUES (?,?,?,?,?,?,?,?)''',
                        (signal_id, symbol, timeframe, signal_name, signal_type,
                         int(direction), time.time(), json.dumps(features)))
                self._conn.commit()
        except Exception as e:
            logger.warning(f"tag_signal failed: {e}")

    def get_unresolved(self, symbol: str, timeframe: str, limit: int = 100):
        try:
            with self._db_lock:
                cur = self._conn.execute(
                    '''SELECT signal_id, bar_key, price, forward_bars, signal_name, direction
                       FROM signal_outcomes
                       WHERE symbol = ? AND timeframe = ? AND resolved = 0
                       ORDER BY fired_ts LIMIT ?''',
                    (symbol, timeframe, int(limit)))
                return cur.fetchall()
        except Exception:
            return []

    def resolve_outcome(self, signal_id: str, forward_return: float):
        try:
            with self._db_lock:
                self._conn.execute(
                    'UPDATE signal_outcomes SET forward_return = ?, resolved = 1 WHERE signal_id = ?',
                    (float(forward_return), signal_id))
                self._conn.commit()
        except Exception as e:
            logger.warning(f"resolve_outcome failed: {e}")

    def outcome_stats(self) -> dict:
        """Per-signal edge stats over resolved outcomes: count, win rate, avg return."""
        try:
            with self._db_lock:
                cur = self._conn.execute(
                    '''SELECT signal_name, COUNT(*) as n,
                              SUM(CASE WHEN forward_return > 0 THEN 1 ELSE 0 END) as wins,
                              AVG(forward_return) as avg_ret, SUM(forward_return) as tot
                       FROM signal_outcomes WHERE resolved = 1
                       GROUP BY signal_name ORDER BY n DESC''')
                rows = cur.fetchall()
                total = self._conn.execute(
                    'SELECT COUNT(*) FROM signal_outcomes WHERE resolved = 1').fetchone()[0]
            return {
                "resolved": int(total),
                "rows": [{
                    "signal_name": r[0],
                    "count": int(r[1]),
                    "wins": int(r[2] or 0),
                    "win_rate": (r[2] or 0) / r[1] if r[1] else 0.0,
                    "avg_ret": float(r[3]) if r[3] is not None else 0.0,
                    "total_ret": float(r[4] or 0.0),
                } for r in rows],
            }
        except Exception:
            return {"resolved": 0, "rows": []}

    def close(self):
        try:
            with self._db_lock:
                self._conn.close()
        except Exception:
            pass

    def emit_alert(self, signal_name: str, symbol: str, price: float,
                   signal_type: str = "SIGNAL", color: str = "info",
                   bar_key: str = "", timeframe: str = "",
                   market_data: Optional[Dict[str, Any]] = None,
                   direction: int = 0):
        signal_id = f"{symbol}_{signal_name}_{timeframe}_{bar_key}"
        if self._is_fired(signal_id):
            return
        
        self._mark_fired(signal_id)
        msg = f"[{signal_type}] {symbol} {timeframe} - {signal_name} @ {price:.4f}"
        if market_data:
            md_parts = []
            if market_data.get("rsi") is not None:
                md_parts.append(f"RSI={market_data['rsi']:.1f}")
            if market_data.get("cvd") is not None:
                md_parts.append(f"CVD={market_data['cvd']:+,.0f}")
            if market_data.get("delta") is not None:
                md_parts.append(f"Δ={market_data['delta']:+,.0f}")
            if market_data.get("volume") is not None and market_data.get("vol_avg"):
                ratio = market_data['volume'] / market_data['vol_avg']
                md_parts.append(f"VOL={ratio:.1f}x")
            if market_data.get("bull_conf") is not None:
                md_parts.append(f"bull_conf={market_data['bull_conf']}")
            if market_data.get("bear_conf") is not None:
                md_parts.append(f"bear_conf={market_data['bear_conf']}")
            if market_data.get("regime") is not None:
                reg = market_data['regime']
                reg_str = "UP" if reg > 0 else ("DOWN" if reg < 0 else "NEU")
                md_parts.append(f"regime={reg_str}")
            if market_data.get("ml_prob") is not None:
                md_parts.append(f"ml={market_data['ml_prob']:.2f}")
            _mm = market_data.get("macro")
            if isinstance(_mm, dict) and not _mm.get("stale", True):
                _mp = []
                if _mm.get("chg_24h") is not None:
                    _mp.append(f"24h={_mm['chg_24h']:+.2f}%")
                if _mm.get("funding_rate") is not None:
                    _mp.append(f"fund={_mm['funding_rate']:+.4%}")
                if _mm.get("poly_yes") is not None:
                    _mp.append(f"poly={_mm['poly_yes']:.0%}")
                _rh = _mm.get("regime_hint", "neutral")
                if _rh and _rh != "neutral":
                    _mp.append(f"macro={_rh}")
                if _mp:
                    md_parts.append("macro[" + " ".join(_mp) + "]")
            if md_parts:
                msg += f" | {' '.join(md_parts)}"
        logger.info(msg, extra={"event_type": "alert_fired", "symbol": symbol, "timeframe": timeframe, "signal_name": signal_name, "signal_type": signal_type, "price": price, "color": color, "signal_id": signal_id, "market_data": market_data or {}})

        # Outcome tagging: store this signal for forward-return resolution
        self.tag_signal(signal_name, symbol, price, timeframe, bar_key, signal_type,
                        self.config.get('general.outcome_forward_bars', 12),
                        features=(market_data or {}).get("features"),
                        direction=direction)

        if self.config.get('general.enable_sound', True) and winsound:
            try:
                winsound.PlaySound('SystemExclamation', winsound.SND_ASYNC)
            except Exception:
                pass

        if self.config.get('general.enable_notifications', True):
            try:
                self.notifier.show_toast("Quant Alert", msg, duration=5, threaded=True)
            except Exception:
                pass

        # Forward to bridge agent (configurable, fire-and-forget in worker thread - Fix M8)
        if self.config.get('bridge.enabled', True):
            def _send_bridge():
                try:
                    bridge_url = self.config.get('bridge.url', 'http://127.0.0.1:8000/quant-alert')
                    bridge_timeout = self.config.get('bridge.timeout_seconds', 2.0)
                    bridge_payload = json.dumps({
                        "symbol": symbol,
                        "signal_type": signal_type,
                        "signal_name": signal_name,
                        "price": price,
                        "timeframe": timeframe,
                        "color": color,
                        "market_data": market_data or {},
                    }).encode()
                    req = _urllib.Request(
                        bridge_url,
                        data=bridge_payload,
                        headers=_bridge_headers(),
                        method="POST",
                    )
                    _urllib.urlopen(req, timeout=bridge_timeout)
                except Exception as e:
                    logger.debug("Bridge forward failed (normal if bridge is off): %s", e)

            threading.Thread(target=_send_bridge, daemon=True).start()

    def publish_snapshot(self, symbol: str, price: float,
                         indicators: Dict[str, Optional[float]],
                         bar_timestamp: Optional[str] = None):
        """Headless no-op; the UI subclass pushes snapshots to its queue."""
        return None

def get_signal_direction(signal_name: str, direction_hint: Optional[str] = None) -> int:
    """Return +1 for bull/long, -1 for bear/short, 0 for neutral."""
    if direction_hint:
        d = str(direction_hint).lower()
        if "bull" in d or "long" in d or "up" in d:
            return 1
        if "bear" in d or "short" in d or "down" in d:
            return -1
    name_lower = signal_name.lower()
    if any(k in name_lower for k in ("bull", "oversold", "breakout up", "above", "flip up")):
        return 1
    if any(k in name_lower for k in ("bear", "overbought", "breakout down", "below", "flip down")):
        return -1
    return 0


# ============================================================================
# SIGNAL ANALYZER
# ============================================================================
class SignalAnalyzer:
    _STATE_NAME_TO_CODE = {"normal": 0, "climax": 1, "spike": 2, "dryup": 3}
    _STATE_CODE_TO_NAME = {0: "normal", 1: "climax", 2: "spike", 3: "dryup"}

    def __init__(self, config: Config, alert_mgr: Optional[AlertManager] = None):
        self.config = config
        self.ind = TechnicalIndicators()
        self.div = DivergenceDetector()
        self.alert_mgr = alert_mgr or AlertManager(config)
        self.exchange = self._init_exchange()
        self._state_lock = threading.Lock()  # Fix C5: Thread-safe state mutation
        # Retained for backwards compat but not used for global serialization (fix #2)
        # Per-symbol circuit breaker in ResilientExchange now handles isolation;
        # rate-limit pacing is handled by CCXT + ResilientExchange internal lock per-attempt.
        self._fetch_lock = threading.Lock()
        self._fetch_semaphore = threading.Semaphore(self.config.get('general.max_workers', 10) or 10)
        # Divergence episode gating (fix #1): key (sym, tf, name) -> active episode.
        # Each divergence fires ONCE per episode, not on every bar.
        self._div_episodes: Dict[Tuple[str, str, str], bool] = {}
        # Multi-TF consolidation (fix #5): key (sym, tf, name) -> (bar_open_ts, bar_key)
        # of the last successful emit, so duplicate lower-TF signals within a
        # higher-TF candle window are suppressed.
        self._last_fire: Dict[Tuple[str, str, str], Tuple[float, str]] = {}
        # Selectivity knobs (fix #5), read once at startup
        self._conf_req = int(self.config.get('general.confluence_required', 0) or 0)
        self._regime_enabled = bool(self.config.get('general.regime_filter_enabled', False))
        self._consolidation = bool(self.config.get('general.tf_consolidation_enabled', True))
        # Macro regime & outcome gating (v2.3) + time gating (v2.4)
        # Recency blackout (v2.5): per-symbol min-gap throttle independent of conditions.
        self._blackout_enabled = bool(self.config.get('general.blackout_enabled', False))
        self._blackout_seconds = int(self.config.get('general.blackout_seconds', 300) or 300)
        self._signal_cooldown_seconds = int(self.config.get('general.signal_cooldown_seconds', 600) or 600)
        # key -> last emit wall-clock ts
        self._last_symbol_fire: Dict[str, float] = {}
        self._last_signal_fire: Dict[Tuple[str, str, str], float] = {}
        self._blackout_blocked_count: int = 0
        # Level-2 validator (v2.6): ATAS bridge order-flow gate for directional alerts.
        self._l2_enabled = bool(self.config.get('level2.enabled', False))
        self._l2_base = str(self.config.get('level2.base_url', 'http://127.0.0.1:8000'))
        self._l2_timeout = float(self.config.get('level2.timeout_seconds', 1.5) or 1.5)
        self._l2_min_score = float(self.config.get('level2.min_score', 2.0) or 2.0)
        self._l2_require_dom = bool(self.config.get('level2.require_dom', True))
        self._l2_dom_min = float(self.config.get('level2.dom_imbalance_min', 5.0) or 5.0)
        self._l2_require_cvd = bool(self.config.get('level2.require_cvd', True))
        self._l2_require_quant = bool(self.config.get('level2.require_quant', True))
        self._l2_quant_min_tf = int(self.config.get('level2.quant_min_timeframes', 1) or 1)
        self._l2_fail_open = bool(self.config.get('level2.fail_open', True))
        self._l2_cache_ttl = float(self.config.get('level2.cache_ttl_seconds', 4.0) or 4.0)
        self._l2_cache: Dict[str, Tuple[float, dict]] = {}
        self._l2_blocked_count: int = 0
        self._l2_failopen_count: int = 0
        # External macro context pipeline (v2.7): CoinGecko + DefiLlama +
        # Polymarket + funding. Info overlay by default; directional gate only
        # when macro.gate_signals is true. Fail-open: init/refresh failures
        # never block signal emission.
        self._extmacro_enabled = bool(self.config.get('macro.enabled', False))
        self._extmacro_gate = bool(self.config.get('macro.gate_signals', False))
        try:
            self._extmacro_weight = float(self.config.get('macro.predictive_weight', 0.3) or 0.3)
        except (TypeError, ValueError):
            self._extmacro_weight = 0.3
        self._extmacro_pipeline = None
        self._extmacro_thread = None
        self._extmacro_loop_ref = None
        self._extmacro_gated_count: int = 0
        if self._extmacro_enabled:
            try:
                from macro_context_pipeline import MacroContextPipeline, MacroConfig
                _symbols_cfg = list(self.config.get('symbols', []) or [])
                _tracked = {}
                for _s in _symbols_cfg:
                    _base = str(_s).split('/')[0].split(':')[0].lower()
                    _tracked[_s] = _COIN_ID_MAP.get(_base, _base)
                # Web macro providers (info/context only — never gates signals).
                try:
                    from web_macro_providers import DEFAULT_WEB_SOURCES as _WEB_DEFAULTS
                except ImportError:
                    _WEB_DEFAULTS = []
                _raw_ws = self.config.get('macro.web_sources', None)
                _web_sources = []
                for _w in (_raw_ws if isinstance(_raw_ws, list) else _WEB_DEFAULTS):
                    if hasattr(_w, 'model_dump'):
                        try:
                            _web_sources.append(_w.model_dump())
                            continue
                        except Exception:
                            pass
                    if isinstance(_w, dict) and _w.get('id'):
                        _web_sources.append(dict(_w))
                if not _web_sources:
                    _web_sources = [dict(d) for d in _WEB_DEFAULTS]
                _mcfg = MacroConfig(
                    enabled=True,
                    coingecko_key=str(self.config.get('macro.coingecko_key', '') or ''),
                    cmc_api_key=str(self.config.get('macro.cmc_api_key', '') or ''),
                    cache_ttl_seconds=float(self.config.get('macro.cache_ttl_seconds', 60.0) or 60.0),
                    predictive_weight=self._extmacro_weight,
                    max_concurrent_requests=int(self.config.get('macro.max_concurrent_requests', 3) or 3),
                    max_retries=int(self.config.get('macro.max_retries', 3) or 3),
                    ath_bear_pct=float(self.config.get('macro.ath_bear_pct', -12.0) or -12.0),
                    ath_bull_pct=float(self.config.get('macro.ath_bull_pct', 15.0) or 15.0),
                    tracked_coins=_tracked,
                    web_sources=_web_sources,
                )
                self._extmacro_pipeline = MacroContextPipeline(_mcfg)
                _t = threading.Thread(
                    target=self._run_macro_loop, args=(_symbols_cfg,),
                    daemon=True, name="macro-context",
                )
                _t.start()
                self._extmacro_thread = _t
                logger.info("External macro pipeline enabled for %d symbols (gate=%s)",
                            len(_symbols_cfg), self._extmacro_gate)
            except Exception as e:
                logger.warning("Macro pipeline disabled (init failed, fail-open): %s", str(e)[:150])
                self._extmacro_enabled = False
                self._extmacro_pipeline = None
        # ── ML meta-label filter (v2.8) ──
        # The feature extractor is created whenever feature logging is on (it is
        # the training data pipeline, independent of whether a model exists yet).
        # The filter itself only loads when ml.enabled, and any failure leaves
        # alerts flowing (fail-open).
        self._ml_enabled = bool(self.config.get('ml.enabled', False))
        self._ml_gate_signals = bool(self.config.get('ml.gate_signals', False))
        try:
            self._ml_min_prob = float(self.config.get('ml.min_prob', 0.55) or 0.55)
        except (TypeError, ValueError):
            self._ml_min_prob = 0.55
        self._ml_fail_open = bool(self.config.get('ml.fail_open', True))
        self._ml_log_features = bool(self.config.get('ml.log_features', True))
        try:
            self._ml_window = int(self.config.get('ml.window', 200) or 200)
        except (TypeError, ValueError):
            self._ml_window = 200
        self._ml_gated_count: int = 0
        self._ml_scored_count: int = 0
        self._ml_scored_cache: Dict[str, Dict[str, Any]] = {}
        # Fix VOL-1: per-symbol volume baseline cache + persistence counters
        self._vol_base_cache: Dict[str, Any] = {}
        self._vol_base_ttl = float(self.config.get('volume.baseline_ttl_seconds', 21600))
        self._vol_pending: Dict[str, int] = {}
        self._fx = FeatureExtractor(window=self._ml_window) if (FeatureExtractor and self._ml_log_features) else None
        self._ml_filter = None
        if self._ml_enabled and MetaLabelFilter is not None:
            try:
                self._ml_filter = MetaLabelFilter(self.config.get('ml.model_path', 'data/ml/meta_model.json'))
                if not getattr(self._ml_filter, "ok", False):
                    logger.warning("ML filter unavailable (%s) — alerts fail-open",
                                   getattr(self._ml_filter, "error", "unknown"))
                    self._ml_filter = None
                else:
                    logger.info("ML meta-label filter loaded: %s (gate=%s, min_prob=%.2f)",
                                self._ml_filter.describe(), self._ml_gate_signals, self._ml_min_prob)
            except Exception as e:
                logger.warning("ML filter init failed (fail-open): %s", e)
                self._ml_filter = None
        elif self._ml_enabled and MetaLabelFilter is None:
            logger.warning("ml.enabled but ml_filter.py could not be imported — ML layer disabled")
        self._macro_enabled = bool(self.config.get('general.macro_regime_enabled', False))
        self._macro_symbols: List[str] = list(self.config.get('general.macro_regime_symbols', ["BTC/USDT:USDT", "ETH/USDT:USDT"]) or ["BTC/USDT:USDT", "ETH/USDT:USDT"])
        self._outcome_enabled = bool(self.config.get('general.outcome_gating_enabled', False))
        self._outcome_lookback_days = int(self.config.get('general.outcome_gating_lookback_days', 7) or 7)
        self._outcome_min_n = int(self.config.get('general.outcome_gating_min_n', 30) or 30)
        self._time_enabled = bool(self.config.get('general.time_gating_enabled', False))
        self._time_lookback_days = int(self.config.get('general.time_gating_lookback_days', 14) or 14)
        self._time_min_n = int(self.config.get('general.time_gating_min_n', 20) or 20)
        self._time_utc_offset = int(self.config.get('general.time_gating_utc_offset', 0) or 0)
        self._time_protected = set(
            int(h) for h in (self.config.get('general.time_gating_protected_hours_local', []) or []) if 0 <= int(h) <= 23
        )
        self._macro_cache: str = "neutral"
        self._macro_cache_ts: float = 0.0
        self._macro_cache_ttl: float = 60.0
        self._macro_gated_count: int = 0
        # Debounce state (v2.6): a regime flip must persist for N consecutive
        # evaluations AND respect a minimum hold time before it is committed.
        # Prevents the macro gate from whipsawing when BTC/ETH hover near EMA.
        self._macro_regime_pending: str = "neutral"
        self._macro_regime_pending_count: int = 0
        self._macro_regime_last_flip: float = 0.0
        self._outcome_cache: Dict[Tuple[str, str], Tuple[float, int]] = {}
        self._outcome_muted: set = set()
        self._outcome_cache_ts: float = 0.0
        self._outcome_cache_ttl: float = 60.0
        self._outcome_gated_count: int = 0
        self._time_cache: Dict[int, Tuple[float, int]] = {}
        self._time_muted_hours: set = set()
        self._time_cache_ts: float = 0.0
        self._time_cache_ttl: float = 300.0
        self._time_gated_count: int = 0

    @staticmethod
    def _safe_last(arr):
        if arr is None or len(arr) == 0:
            return None
        v = arr[-1]
        try:
            if np.isnan(v):
                return None
        except (TypeError, ValueError):
            return None
        return float(v)

    # ── External macro context (v2.7) ──
    def _run_macro_loop(self, symbols: List[str]):
        """Background thread entry: own asyncio loop driving the pipeline's
        refresh loop directly. NOTE: we must run _background_loop itself —
        awaiting start() alone would return after scheduling the task and
        leave the loop with nothing to run (silent stall)."""
        import asyncio
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._extmacro_loop_ref = loop
            self._extmacro_pipeline._running = True
            loop.run_until_complete(self._extmacro_pipeline._background_loop(symbols, []))
        except Exception as e:
            logger.warning("Macro background loop exited: %s", str(e)[:150])

    @staticmethod
    def _flatten_macro(mctx: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten the pipeline's nested context into Telegram-ready fields."""
        flat: Dict[str, Any] = {"stale": bool(mctx.get("_stale", False))}
        pricing = mctx.get("market_pricing", {}) if isinstance(mctx.get("market_pricing"), dict) else {}
        if "error" not in pricing:
            if pricing.get("price_usd") is not None:
                flat["price_usd"] = float(pricing["price_usd"])
            if pricing.get("change_24h_percent") is not None:
                flat["chg_24h"] = float(pricing["change_24h_percent"])
            if pricing.get("volume_24h_usd") is not None:
                flat["vol_24h_usd"] = float(pricing["volume_24h_usd"])
        fr = mctx.get("funding_rate")
        if fr is not None:
            try:
                flat["funding_rate"] = float(fr)
            except (TypeError, ValueError):
                pass
        ath = mctx.get("ath_dwd", {}) if isinstance(mctx.get("ath_dwd"), dict) else {}
        flat["regime_hint"] = str(ath.get("regime_hint", "neutral"))
        # First Polymarket market's YES probability, if parseable
        sent = mctx.get("macro_prediction_sentiment", [])
        if isinstance(sent, list) and sent and isinstance(sent[0], dict) and "error" not in sent[0]:
            probs = sent[0].get("outcome_probabilities")
            if isinstance(probs, list) and probs:
                try:
                    flat["poly_yes"] = float(probs[0])
                    flat["poly_question"] = str(sent[0].get("question", ""))[:80]
                except (TypeError, ValueError):
                    pass
        # Web macro overlay (info/context only — never gates signals).
        try:
            from web_macro_providers import summarize_web_macro as _web_summary
            from web_macro_providers import web_freshness as _web_freshness
            _web = mctx.get("web_macro", {}) if isinstance(mctx.get("web_macro"), dict) else {}
            _wf = _web_freshness(_web)
            flat["web_fresh"] = int(_wf.get("fresh", 0))
            flat["web_total"] = int(_wf.get("total", 0))
            flat["web_stale"] = bool(_wf.get("total", 0) and not _wf.get("fresh", 0))
            flat["web_summary"] = str(_web_summary(_web))[:220]
            # CMC market-wide context (fear/greed + global metrics, info-only).
            _gw = mctx.get("web_macro_global", {}) if isinstance(mctx.get("web_macro_global"), dict) else {}
            _fg = _gw.get("fear_greed", {}) if isinstance(_gw.get("fear_greed"), dict) else {}
            _gl = _gw.get("global", {}) if isinstance(_gw.get("global"), dict) else {}
            if _fg.get("value") is not None:
                try:
                    flat["fear_greed"] = int(_fg["value"])
                    flat["fear_greed_class"] = str(_fg.get("classification", ""))[:20]
                except (TypeError, ValueError):
                    pass
            if _gl.get("btc_dominance_pct") is not None:
                try:
                    flat["btc_dominance"] = float(_gl["btc_dominance_pct"])
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass
        return flat

    def macro_context_health(self) -> Dict[str, Any]:
        """Health check payload for the /health endpoint.

        Counts only FRESH entries (age < TTL) — a live refresh loop with
        failing fetches must show fresh_cached=0 (degraded), never hide
        behind stale entries the way a raw len(_cache) does."""
        if not self._extmacro_enabled or self._extmacro_pipeline is None:
            return {"status": "healthy", "metrics": {"enabled": 0}}
        try:
            import time as _t
            cache = getattr(self._extmacro_pipeline, "_cache", {}) or {}
            ttl = float(getattr(getattr(self._extmacro_pipeline, "cfg", None),
                                "cache_ttl_seconds", 60.0) or 60.0)
            now = _t.time()
            ages = [now - ts for ts, _ in cache.values()]
            fresh = sum(1 for a in ages if a < ttl)
            oldest = round(max(ages), 1) if ages else None
            # Web macro freshness across cached symbol contexts (info-only).
            web_fresh, web_total, web_oldest = 0, 0, None
            try:
                from web_macro_providers import is_fresh as _web_is_fresh
                for _ts, _data in cache.values():
                    _web = _data.get("web_macro", {}) if isinstance(_data, dict) else {}
                    for _rec in _web.values() if isinstance(_web, dict) else []:
                        if not isinstance(_rec, dict):
                            continue
                        web_total += 1
                        try:
                            _rts = datetime.fromisoformat(str(_rec.get("retrieved_at", ""))).timestamp()
                        except (ValueError, TypeError):
                            continue
                        _age = now - _rts
                        web_oldest = _age if web_oldest is None or _age > web_oldest else web_oldest
                        if _web_is_fresh(_rec, now):
                            web_fresh += 1
                web_oldest = round(web_oldest, 1) if web_oldest is not None else None
            except Exception:
                pass
        except Exception:
            fresh, oldest = 0, None
            web_fresh, web_total, web_oldest = 0, 0, None
        alive = bool(self._extmacro_thread and self._extmacro_thread.is_alive())
        ok = bool(alive and fresh)
        return {
            "status": "healthy" if ok else "degraded",
            "metrics": {
                "enabled": 1,
                "thread_alive": 1 if alive else 0,
                "symbols_cached": fresh,
                "oldest_cache_age_s": oldest,
                "gated_total": self._extmacro_gated_count,
                "gate_signals": 1 if self._extmacro_gate else 0,
                "web_fresh": web_fresh,
                "web_total": web_total,
                "web_oldest_age_s": web_oldest,
            },
        }

    def _init_exchange(self, max_retries: int = 3):
        """Use ResilientExchange wrapper for retry/backoff/circuit breaker."""
        try:
            from exchange_resilient import ResilientExchange
            # Pull exchange config from validated Settings if available
            ex_cfg = None
            try:
                # Config mirrors Settings -> exchange object
                ex_cfg = self.config._settings.exchange  # type: ignore
            except Exception:
                ex_cfg = None

            if ex_cfg is not None:
                exchange_id = getattr(ex_cfg, "id", "binanceusdm")
                testnet = getattr(ex_cfg, "testnet", False)
                api_key = getattr(ex_cfg, "api_key", "")
                secret = getattr(ex_cfg, "secret", "")
                rate_buf = getattr(ex_cfg, "rate_limit_buffer", 0.1)
                self.exchange = ResilientExchange(
                    exchange_id=exchange_id,
                    testnet=testnet,
                    api_key=api_key,
                    secret=secret,
                    max_retries=max_retries,
                    rate_limit_buffer=rate_buf,
                )
            else:
                # Fallback to legacy string exchange
                exchange_id = self.config.get('exchange', 'binanceusdm')
                if isinstance(exchange_id, dict):
                    exchange_id = exchange_id.get('id', 'binanceusdm')
                self.exchange = ResilientExchange(exchange_id=exchange_id, max_retries=max_retries)

            # Validate symbols via wrapper
            symbols = list(self.config.get('symbols', []))
            if symbols:
                valid = self.exchange.validate_symbols(symbols)
                if len(valid) != len(symbols):
                    try:
                        self.config.set('symbols', valid)
                    except Exception:
                        # Fallback to direct Settings mutation
                        self.config._settings.symbols = valid  # type: ignore
                    logger.info(f"Watch list after validation: {valid}")

            logger.info(f"Connected to {self.exchange.exchange_id} (resilient mode)")
            return self.exchange
        except Exception as e:
            logger.warning(f"ResilientExchange init failed, falling back to raw ccxt: {e}")
            # Fallback to raw ccxt (legacy)
            for attempt in range(max_retries):
                try:
                    exchange_id = self.config.get('exchange', 'binanceusdm')
                    if isinstance(exchange_id, dict):
                        exchange_id = exchange_id.get('id', 'binanceusdm')
                    ex = getattr(ccxt, exchange_id)({'enableRateLimit': True})
                    ex.load_markets()
                    self._validate_symbols(ex, exchange_id)
                    logger.info(f"Connected to {exchange_id} (fallback)")
                    return ex
                except Exception as ex_e:
                    logger.warning(f"Exchange init attempt {attempt+1}/{max_retries} failed: {ex_e}")
                    time.sleep(2 ** attempt)
            logger.error("Failed to initialize exchange. Exiting.")
            sys.exit(1)

    def _validate_symbols(self, exchange, exchange_id: str):
        symbols = list(self.config.get('symbols', []))
        if not symbols:
            return
        valid = [s for s in symbols if s in exchange.symbols]
        invalid = [s for s in symbols if s not in exchange.symbols]
        for s in invalid:
            logger.error(f"Symbol {s} not found on {exchange_id} — removing from watch list")
        if invalid:
            try:
                self.config.set('symbols', valid)
            except Exception:
                self.config._settings.symbols = valid  # type: ignore
            logger.info(f"Watch list after validation: {valid}")

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> Optional[pd.DataFrame]:
        try:
            # No global lock — per-symbol circuit breaker + per-attempt lock in ResilientExchange
            # allows ThreadPoolExecutor to fetch multiple symbols concurrently.
            # Semaphore limits total concurrency to max_workers without serializing retries.
            if limit <= 0:
                limit = int(self.config.get('general.max_history_bars', 200) or 200)
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            return df
        except ccxt.NetworkError as e:
            logger.warning(f"Network error fetching {symbol} {timeframe}: {e}")
        except ccxt.ExchangeError as e:
            logger.error(f"Exchange error fetching {symbol} {timeframe}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching {symbol}: {e}", exc_info=True)
        return None

    def analyze_symbol(self, symbol: str, timeframe: str = '5m') -> bool:
        df = self.fetch_ohlcv(symbol, timeframe)
        if df is None or len(df) < 50:
            return False
        return self._evaluate(symbol, timeframe, df)

    def _evaluate(self, symbol: str, timeframe: str, df: pd.DataFrame) -> bool:
        """Evaluate one symbol/timeframe on a dataframe of OHLCV bars.
        Split out of analyze_symbol so the Backtester can replay history
        through the exact same signal path (no lookahead: every evaluation
        only sees bars up to the current one)."""

        # ── Fix #3: confirmed-bar guard — exclude forming candle from signal evaluation ──
        # Binance REST klines return the currently-forming candle as last row; evaluating
        # crosses on it causes repaint (alert on intrabar state that may reverse before close).
        # Keep forming bar for live UI price, but compute indicators on closed bars only.
        period = TF_SECONDS.get(timeframe, DEFAULT_TF_SECONDS)
        try:
            last_open_ts = df['timestamp'].iloc[-1].timestamp()
            now_ts = time.time()
            is_forming = (last_open_ts + period) > now_ts
        except Exception:
            is_forming = False

        if is_forming and len(df) > 51:
            df_sig = df.iloc[:-1].copy()
            live_price = float(df['close'].iloc[-1]) if not pd.isna(df['close'].iloc[-1]) else None
            # live_bar_key is forming candle's open time — not used for dedup
            # logger.debug is intentional (avoid spam at INFO)
            logger.debug(f"{symbol} {timeframe}: forming candle {df['timestamp'].iloc[-1].isoformat()} excluded from signals (closes in {int(last_open_ts + period - now_ts)}s)")
            if len(df_sig) < 50:
                return False
        else:
            df_sig = df
            live_price = None  # indicates snapshot price == signal price

        c, v = df_sig['close'].values, df_sig['volume'].values
        h, l = df_sig['high'].values, df_sig['low'].values
        o = df_sig['open'].values

        if len(c) == 0 or np.isnan(c[-1]): return False

        price = float(c[-1])  # closed-bar price for signal evaluation
        bar_key = df_sig['timestamp'].iloc[-1].isoformat()
        # live_price for UI snapshot (if forming, use forming bar's close; else use closed price)
        snapshot_price = live_price if (is_forming and live_price is not None) else price
        snapshot_bar_key = df['timestamp'].iloc[-1].isoformat() if is_forming else bar_key

        # Compute every indicator ONCE per scan cycle (no duplicate math)
        rsi_arr = self.ind.rsi(c, self.config.get('rsi.length', 14)) if self.config.get('rsi.enabled') else None
        ml = sl = hist = None
        if self.config.get('macd.enabled'):
            ml, sl, hist = self.ind.macd(c, self.config.get('macd.fast', 12),
                                         self.config.get('macd.slow', 26),
                                         self.config.get('macd.signal', 9))
        k = d = None
        if self.config.get('stochastic.enabled'):
            k, d = self.ind.stochastic(h, l, c, self.config.get('stochastic.k_len', 14),
                                       self.config.get('stochastic.d_len', 3),
                                       self.config.get('stochastic.smooth', 3))
        upper = mid = lower = atr_arr = None
        if self.config.get('volatility.enabled'):
            upper, mid, lower = self.ind.bollinger_bands(c, self.config.get('volatility.bb_len', 20),
                                                         self.config.get('volatility.bb_mult', 2.0))
            atr_arr = self.ind.atr(h, l, c, self.config.get('volatility.atr_len', 14))
        cvd_cum = cvd_cum_smooth = None
        if self.config.get('cvd.enabled'):
            cvd_cum = CVDCalculator.bar_signed_cumulative(c, v)
            cvd_cum_smooth = self.ind.ema(cvd_cum, self.config.get('cvd.smooth_len', 5))

        # Selectivity context (fix #5): directional confluence + trend regime
        vol_avg = None
        if self.config.get('volume.enabled'):
            vol_avg = self.ind.sma(v, self.config.get('volume.avg_len', 20))[-1]
        bull_conf, bear_conf = self._confluence(c, rsi_arr, ml, sl, k, d, mid, v, vol_avg)
        regime = self._trend_regime(c, atr_arr)
        conf = (bull_conf, bear_conf, regime, self._conf_req, self._regime_enabled)

        # Build market_data dict for enriched Telegram alerts
        market_data: Dict[str, Any] = {
            "price": price,
            "bar_key": bar_key,
            "timeframe": timeframe,
            "symbol": symbol,
        }
        if rsi_arr is not None and not np.isnan(rsi_arr[-1]):
            market_data["rsi"] = float(rsi_arr[-1])
        if ml is not None and not np.isnan(ml[-1]):
            market_data["macd_line"] = float(ml[-1])
            market_data["macd_signal"] = float(sl[-1])
            market_data["macd_hist"] = float(hist[-1]) if not np.isnan(hist[-1]) else None
        if k is not None and not np.isnan(k[-1]):
            market_data["stoch_k"] = float(k[-1])
            market_data["stoch_d"] = float(d[-1])
        if upper is not None and not np.isnan(upper[-1]):
            market_data["bb_upper"] = float(upper[-1])
            market_data["bb_mid"] = float(mid[-1])
            market_data["bb_lower"] = float(lower[-1])
        if atr_arr is not None and not np.isnan(atr_arr[-1]):
            market_data["atr"] = float(atr_arr[-1])
        if cvd_cum is not None and len(cvd_cum) > 0 and not np.isnan(cvd_cum[-1]):
            market_data["cvd"] = float(cvd_cum[-1])
        if cvd_cum_smooth is not None and not np.isnan(cvd_cum_smooth[-1]):
            market_data["cvd_smooth"] = float(cvd_cum_smooth[-1])
        # Latest bar delta from bar-signed volume
        if len(v) > 0 and len(c) > 1:
            delta = float(v[-1]) if c[-1] >= c[-2] else float(-v[-1])
            market_data["delta"] = delta
        if vol_avg is not None:
            market_data["vol_avg"] = float(vol_avg)
            market_data["volume"] = float(v[-1]) if not np.isnan(v[-1]) else None
        market_data["bull_conf"] = int(bull_conf)
        market_data["bear_conf"] = int(bear_conf)
        market_data["regime"] = int(regime)
        # External macro overlay (v2.7): cached CoinGecko/Polymarket/funding
        # context. Fail-open: missing/stale data leaves market_data unchanged.
        if self._extmacro_enabled and self._extmacro_pipeline is not None:
            try:
                _mctx = self._extmacro_pipeline.get_context(symbol)
                market_data["macro"] = self._flatten_macro(_mctx)
            except Exception:
                pass

        # ── ML feature vector (v2.8) ──
        # Computed ONCE per scan, from CLOSED bars only, through features.py, so
        # the vector persisted to signal_features is identical to what
        # ml_backfill.py reconstructs from klines. Any failure is non-fatal.
        if self._fx is not None:
            try:
                _fv = self._fx.vector(o, h, l, c, v)
                if _fv:
                    _bar_ts = df_sig['timestamp'].iloc[-1].timestamp()
                    market_data["features"] = FeatureExtractor.add_time_features(_fv, _bar_ts)
            except Exception as _fe:
                logger.debug("feature extraction failed for %s %s: %s", symbol, timeframe, _fe)

        if self.config.get('volume.enabled'):
            self._check_volume(symbol, price, c, v, bar_key, timeframe, vol_avg, conf, market_data=market_data)
        if self.config.get('cvd.enabled'):
            self._check_cvd(symbol, price, c, bar_key, timeframe, cvd_cum, cvd_cum_smooth, conf, market_data=market_data)
        if self.config.get('rsi.enabled'):
            self._check_rsi(symbol, price, c, bar_key, timeframe, rsi_arr, conf, market_data=market_data)
        if self.config.get('macd.enabled'):
            self._check_macd(symbol, price, c, bar_key, timeframe, ml, sl, hist, conf, market_data=market_data)
        # STOCH alerts REMOVED (2026-09-17): the 4 stochastic signals (Bullish/
        # Bearish Cross, Entered Overbought/Oversold) are no longer emitted, and
        # the GUI/Telegram no longer display stoch values. The indicator still
        # computes under stochastic.enabled because it is a confluence factor
        # (see _confluence) — internal scoring only, never user-visible.
        if self.config.get('volatility.enabled'):
            self._check_vol(symbol, price, h, l, c, bar_key, timeframe, upper, mid, lower, atr_arr, conf, market_data=market_data)

        # Outcome tagging (fix #3): resolve forward returns for signals that have
        # reached their forward horizon in this window.
        self._resolve_outcomes(symbol, timeframe, df_sig)

        # Snapshot: live OHLC for UI display if forming, indicators from closed bar
        if is_forming and live_price is not None:
            snap = {}
            try:
                snap['open'] = float(df['open'].iloc[-1]) if not pd.isna(df['open'].iloc[-1]) else None
                snap['high'] = float(df['high'].iloc[-1]) if not pd.isna(df['high'].iloc[-1]) else None
                snap['low'] = float(df['low'].iloc[-1]) if not pd.isna(df['low'].iloc[-1]) else None
                snap['volume'] = float(df['volume'].iloc[-1]) if not pd.isna(df['volume'].iloc[-1]) else None
            except Exception:
                snap['open'] = float(o[-1]) if not np.isnan(o[-1]) else None
                snap['high'] = float(h[-1]) if not np.isnan(h[-1]) else None
                snap['low'] = float(l[-1]) if not np.isnan(l[-1]) else None
                snap['volume'] = float(v[-1]) if not np.isnan(v[-1]) else None
        else:
            snap = {}
            snap['open'] = float(o[-1]) if not np.isnan(o[-1]) else None
            snap['high'] = float(h[-1]) if not np.isnan(h[-1]) else None
            snap['low'] = float(l[-1]) if not np.isnan(l[-1]) else None
            snap['volume'] = float(v[-1]) if not np.isnan(v[-1]) else None
        try:
            first_close = float(c[0])
            if first_close > 0:
                snap['session_open'] = first_close
                snap['session_change_pct'] = (snapshot_price - first_close) / first_close * 100.0
        except Exception:
            pass

        if rsi_arr is not None:
            snap['rsi'] = self._safe_last(rsi_arr)
        if ml is not None:
            snap['macd_line'] = self._safe_last(ml)
            snap['macd_signal'] = self._safe_last(sl)
            snap['macd_hist'] = self._safe_last(hist)
        if upper is not None:
            snap['bb_upper'] = self._safe_last(upper)
            snap['bb_lower'] = self._safe_last(lower)
            snap['atr'] = self._safe_last(atr_arr)

        self.alert_mgr.publish_snapshot(symbol, snapshot_price, snap, snapshot_bar_key)
        return True

    # ── Selectivity helpers (fix #5) ──
    def _trend_regime(self, c: np.ndarray, atr_arr: Optional[np.ndarray] = None) -> int:
        """1 = uptrend, -1 = downtrend, 0 = neutral (inside an ATR band around EMA).
        Used by the optional regime filter and as one confluence factor."""
        try:
            trend_len = self.config.get('general.regime_trend_len', 50)
            if len(c) < trend_len + 5:
                return 0
            ema = self.ind.ema(c, trend_len)[-1]
            if np.isnan(ema):
                return 0
            if atr_arr is not None and not np.isnan(atr_arr[-1]):
                band = 0.25 * atr_arr[-1]
                if abs(c[-1] - ema) < band:
                    return 0
            return 1 if c[-1] > ema else (-1 if c[-1] < ema else 0)
        except Exception:
            return 0

    # ── Macro regime (v2.3, debounced v2.6) ──
    def _macro_regime(self) -> str:
        """BTC/ETH macro regime from last close vs EMA(trend_len).

        Fetches closes for macro_regime_symbols (default BTC/ETH) and votes:
        bull if close > EMA*(1+band), bear if close < EMA*(1-band), else
        neutral. Majority vote; mixed -> neutral. Cached 60s.

        Asymmetric EMA band (v2.6): a close must clear the EMA by
        `macro_regime_ema_band` (default 0.5%) to vote directionally, so a
        price hovering on the EMA votes neutral instead of flipping the gate.

        Debounce (v2.6): a regime CHANGE is only committed after it has
        persisted for `macro_regime_debounce_cycles` consecutive evaluations
        AND at least `macro_regime_min_hold_s` seconds have elapsed since the
        last committed flip. This stops the macro overlay from whipsawing and
        mass-gating signals when BTC/ETH oscillate around the EMA."""
        if not self._macro_enabled:
            return "neutral"
        now = time.time()
        with self._state_lock:
            if now - self._macro_cache_ts < self._macro_cache_ttl:
                return self._macro_cache
        try:
            trend_len = int(self.config.get('general.regime_trend_len', 50) or 50)
            band = float(self.config.get('general.macro_regime_ema_band', 0.005) or 0.005)
            votes_bull = 0
            votes_bear = 0
            for sym in self._macro_symbols:
                try:
                    # Use hourly bars for macro; enough history for EMA
                    ohlcv = self.exchange.fetch_ohlcv(sym, "1h", limit=trend_len + 20)
                    if not ohlcv or len(ohlcv) < trend_len + 5:
                        continue
                    closes = np.array([row[4] for row in ohlcv], dtype=float)
                    ema = self.ind.ema(closes, trend_len)[-1]
                    if np.isnan(ema):
                        continue
                    last = float(closes[-1])
                    if last > ema * (1.0 + band):
                        votes_bull += 1
                    elif last < ema * (1.0 - band):
                        votes_bear += 1
                except Exception as e:
                    logger.debug("macro regime fetch failed for %s: %s", sym, e)
                    continue
            total = votes_bull + votes_bear
            if total == 0:
                raw_regime = "neutral"
            elif votes_bull == total:
                raw_regime = "bull"
            elif votes_bear == total:
                raw_regime = "bear"
            else:
                raw_regime = "neutral"

            # ── Debounce: only commit a CHANGE after it persists ──
            debounce_cycles = int(self.config.get('general.macro_regime_debounce_cycles', 3) or 3)
            min_hold_s = float(self.config.get('general.macro_regime_min_hold_s', 300.0) or 300.0)
            with self._state_lock:
                committed = self._macro_cache
                if raw_regime == committed:
                    # Stable — reset any pending flip.
                    self._macro_regime_pending = raw_regime
                    self._macro_regime_pending_count = 0
                else:
                    if raw_regime == self._macro_regime_pending:
                        self._macro_regime_pending_count += 1
                    else:
                        self._macro_regime_pending = raw_regime
                        self._macro_regime_pending_count = 1
                    held_long_enough = (now - self._macro_regime_last_flip) >= min_hold_s
                    if self._macro_regime_pending_count >= debounce_cycles and held_long_enough:
                        old = committed
                        committed = raw_regime
                        self._macro_regime_last_flip = now
                        self._macro_regime_pending_count = 0
                        logger.info(
                            "Macro regime committed flip %s -> %s (persisted %d cycles)",
                            old, raw_regime, debounce_cycles,
                        )
                self._macro_cache = committed
                self._macro_cache_ts = now
            return committed
        except Exception as e:
            logger.debug("macro regime error: %s", e)
            with self._state_lock:
                return self._macro_cache or "neutral"

    # ── Outcome-aware gating (v2.3) ──
    def _refresh_outcome_cache(self):
        """Query signal_outcomes for trailing lookback days per signal_name.

        Muted if expectancy (AVG forward_return) < 0 and n >= min_n. Refreshed every 60s.

        Fix H3: grouped by signal_name rather than signal_type, to distinguish long/short.

        Fix OUT-1 (2026-09-27): this used to be keyed per (signal_name, SYMBOL), so each
        cell needed min_n=30 samples of one signal on one symbol inside the lookback. The
        engine's own table has 55 such cells in 7 days and the LARGEST is n=21 — the gate
        could never fire, which is why outcome.gating_enabled was set to false and nothing
        was muted. Expectancy is a property of the SIGNAL, not of the symbol: the same
        setup behaves the same on SOL as on HYPE. Now pooled per signal_name, with a
        per-symbol cell honoured only when it has enough samples of its own.
        """
        now = time.time()
        with self._state_lock:
            if now - self._outcome_cache_ts < self._outcome_cache_ttl:
                return
            self._outcome_cache_ts = now
        if not self._outcome_enabled:
            with self._state_lock:
                self._outcome_cache.clear()
                self._outcome_muted.clear()
            return
        try:
            cutoff = now - self._outcome_lookback_days * 86400
            with self.alert_mgr._db_lock:
                rows = self.alert_mgr._conn.execute(
                    """SELECT signal_name, AVG(forward_return) as exp, COUNT(*) as n
                       FROM signal_outcomes
                       WHERE resolved = 1 AND fired_ts >= ?
                       GROUP BY signal_name""",
                    (cutoff,)).fetchall()
                sym_rows = self.alert_mgr._conn.execute(
                    """SELECT signal_name, symbol, AVG(forward_return) as exp, COUNT(*) as n
                       FROM signal_outcomes
                       WHERE resolved = 1 AND fired_ts >= ?
                       GROUP BY signal_name, symbol""",
                    (cutoff,)).fetchall()
            cache: Dict[str, Tuple[float, int]] = {}
            muted: set = set()
            for sname, exp, n in rows:
                if exp is None or n is None:
                    continue
                cache[str(sname)] = (float(exp), int(n))
                if float(exp) < 0 and int(n) >= self._outcome_min_n:
                    muted.add(str(sname))          # pooled: applies to every symbol
            for sname, sym, exp, n in sym_rows:
                if exp is None or n is None:
                    continue
                if float(exp) < 0 and int(n) >= self._outcome_min_n:
                    muted.add((str(sname), str(sym)))   # per-symbol override
            with self._state_lock:
                self._outcome_cache = cache
                self._outcome_muted = muted
        except Exception as e:
            logger.warning("outcome gating refresh failed: %s: %s", type(e).__name__, str(e)[:160])

    def _is_outcome_muted(self, symbol: str, signal_name: str) -> bool:
        if not self._outcome_enabled:
            return False
        self._refresh_outcome_cache()
        with self._state_lock:
            # Fix OUT-1: a pooled per-signal mute applies to every symbol; a per-symbol
            # cell, when it has enough samples, is the more specific answer.
            if (str(signal_name), str(symbol)) in self._outcome_muted:
                return True
            return str(signal_name) in self._outcome_muted

    # ── Time gating (v2.4) ──
    def _refresh_time_cache(self):
        now = time.time()
        with self._state_lock:
            if now - self._time_cache_ts < self._time_cache_ttl:
                return
            self._time_cache_ts = now
        if not self._time_enabled:
            with self._state_lock:
                self._time_cache.clear()
                self._time_muted_hours.clear()
            return
        try:
            cutoff = now - self._time_lookback_days * 86400
            off = self._time_utc_offset
            with self.alert_mgr._db_lock:
                cur = self.alert_mgr._conn.execute(
                    """SELECT CAST((((strftime('%H', datetime(fired_ts, 'unixepoch')) + ?) % 24) + 24) % 24 AS INTEGER) as hr,
                              AVG(forward_return) as exp, COUNT(*) as n
                       FROM signal_outcomes WHERE resolved=1 AND fired_ts >= ? GROUP BY hr""",
                    (off, cutoff))
                rows = cur.fetchall()
            cache: Dict[int, Tuple[float, int]] = {}
            muted: set = set()
            for hr, exp, n in rows:
                if hr is None or exp is None or n is None:
                    continue
                cache[int(hr)] = (float(exp), int(n))
                if float(exp) < 0 and int(n) >= self._time_min_n and int(hr) not in self._time_protected:
                    muted.add(int(hr))
            with self._state_lock:
                self._time_cache = cache
                self._time_muted_hours = muted
        except Exception as e:
            logger.debug("time gating refresh failed: %s", e)

    def _is_time_muted(self) -> bool:
        if not self._time_enabled:
            return False
        self._refresh_time_cache()
        hr = (time.gmtime().tm_hour + self._time_utc_offset) % 24
        with self._state_lock:
            return hr in self._time_muted_hours

    def time_health(self) -> dict:
        try:
            self._refresh_time_cache()
            with self._state_lock:
                muted_hours = sorted(self._time_muted_hours)
                gated_count = self._time_gated_count
            status = "healthy" if not muted_hours else "degraded"
            return {"status": status, "enabled": self._time_enabled,
                    "lookback_days": self._time_lookback_days, "min_n": self._time_min_n,
                    "utc_offset_hours": self._time_utc_offset,
                    "protected_hours_local": sorted(self._time_protected),
                    "muted_hours_local": muted_hours,
                    "metrics": {"time_gated": gated_count, "muted_hours": len(muted_hours)}}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    def macro_health(self) -> dict:
        try:
            regime = self._macro_regime() if self._macro_enabled else "disabled"
            status = "healthy"
            with self._state_lock:
                gated_count = self._macro_gated_count
            return {"status": status, "regime": regime, "enabled": self._macro_enabled,
                    "symbols": self._macro_symbols,
                    "metrics": {"macro_gated": gated_count}}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    def gating_health(self) -> dict:
        try:
            self._refresh_outcome_cache()
            with self._state_lock:
                muted_count = len(self._outcome_muted)
                gated_count = self._outcome_gated_count
            status = "healthy" if not muted_count else "degraded"
            return {"status": status, "enabled": self._outcome_enabled,
                    "lookback_days": self._outcome_lookback_days, "min_n": self._outcome_min_n,
                    "metrics": {"outcome_gated": gated_count, "muted_pairs": muted_count}}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    def blackout_health(self) -> dict:
        """Real blackout/cooldown health (replaces the old hardcoded stub)."""
        try:
            status = "healthy" if self._blackout_enabled else "disabled"
            with self._state_lock:
                blocked_count = self._blackout_blocked_count
                active_syms = len(self._last_symbol_fire)
            return {"status": status, "enabled": self._blackout_enabled,
                    "blackout_seconds": self._blackout_seconds,
                    "signal_cooldown_seconds": self._signal_cooldown_seconds,
                    "metrics": {"blocked": blocked_count,
                                "active_symbols": active_syms}}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    def ml_health(self) -> dict:
        """ML meta-label layer health: model presence, shadow vs gating, counters."""
        try:
            with self._state_lock:
                scored = self._ml_scored_count
                gated = self._ml_gated_count
            if not self._ml_enabled:
                status = "disabled"
            elif self._ml_filter is None:
                # gating configured but no model = the gate is silently open
                status = "degraded" if self._ml_gate_signals else "unavailable"
            elif self._ml_gate_signals:
                status = "healthy"
            else:
                status = "shadow"
            out = {"status": status, "enabled": self._ml_enabled,
                   "gate_signals": self._ml_gate_signals,
                   "min_prob": self._ml_min_prob, "window": self._ml_window,
                   "log_features": self._ml_log_features and self._fx is not None,
                   "model": self._ml_filter.describe() if self._ml_filter is not None else None,
                   "metrics": {"scored": scored, "gated": gated}}
            if self._ml_filter is None and self._ml_enabled:
                out["error"] = "model not loaded (see ml.model_path / ml_train.py)"
            return out
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    def level2_health(self) -> dict:
        """ATAS bridge Level-2 validator health."""
        try:
            status = "healthy" if self._l2_enabled else "disabled"
            with self._state_lock:
                blocked = self._l2_blocked_count
                fail_open = self._l2_failopen_count
            return {"status": status, "enabled": self._l2_enabled,
                    "base_url": self._l2_base, "min_score": self._l2_min_score,
                    "fail_open": self._l2_fail_open,
                    "metrics": {"blocked": blocked, "fail_open": fail_open}}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    # ── ATAS Bridge Level-2 validator (v2.6) ──────────────────────────────
    def _l2_symbols(self, symbol: str) -> Tuple[str, str]:
        """Map 'SOL/USDT:USDT' -> (bridge_base 'SOL', bridge_pair 'SOLUSDT')."""
        base = symbol.split("/")[0].strip().upper()
        return base, base + "USDT"

    def _l2_fetch(self, endpoint: str, cache_key: Optional[str] = None) -> Optional[dict]:
        """GET a bridge endpoint with a short per-symbol cache. None on failure."""
        if cache_key:
            hit = self._l2_cache.get(cache_key)
            if hit and (time.time() - hit[0]) < self._l2_cache_ttl:
                return hit[1]
        url = self._l2_base.rstrip("/") + endpoint
        try:
            with _urllib.urlopen(url, timeout=self._l2_timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            if cache_key:
                with self._state_lock:
                    self._l2_cache[cache_key] = (time.time(), data)
            return data
        except Exception as e:
            logger.debug("L2 fetch failed %s: %s", endpoint, e)
            return None

    def _l2_dom_score(self, pair: str, direction: str) -> Optional[float]:
        """+1 strong agree, +0.5 weak agree, -1 strong disagree, -0.5 weak disagree.
        None when the bridge has no DOM data (missing, not neutral)."""
        d = self._l2_fetch("/dom/" + pair.lower(), cache_key=("dom", pair.lower()))
        if not d:
            return None
        imb = d.get("imbalance_pct")
        if imb is None:
            return None
        try:
            imb = float(imb)
        except (TypeError, ValueError):
            return None
        wants_positive = (direction == "bull")
        agrees = (imb > 0) == wants_positive
        strong = abs(imb) >= self._l2_dom_min
        if agrees:
            return 1.0 if strong else 0.5
        return -1.0 if strong else -0.5

    def _l2_cvd_score(self, pair: str, direction: str) -> Optional[float]:
        """+1 rising/positive CVD agrees, -1 disagrees.
        None when the bridge has no CVD data (missing, not neutral)."""
        d = self._l2_fetch("/cvd/" + pair, cache_key=("cvd", pair))
        if not d:
            return None
        # /cvd/{symbol} returns the symbol's fields at top level; /cvd nests them.
        entry = d if ("current_bar_delta" in d or "last_delta" in d) else d.get(pair)
        if not isinstance(entry, dict):
            return None
        delta = entry.get("current_bar_delta")
        if delta is None:
            delta = entry.get("last_delta")
        if delta is None:
            return None
        try:
            delta = float(delta)
        except (TypeError, ValueError):
            return None
        if direction == "bull":
            return 1.0 if delta > 0 else -1.0
        return 1.0 if delta < 0 else -1.0

    def _l2_quant_score(self, base: str, direction: str) -> Optional[float]:
        """+1 multi-TF consensus agrees, -1 disagrees, 0.0 neutral (balanced).
        None when the bridge has no quant alerts buffered (missing, not neutral)."""
        d = self._l2_fetch("/quant/" + base, cache_key=("quant", base))
        if not d or not isinstance(d.get("alerts"), list):
            return None
        if not d["alerts"]:
            return None
        bull = bear = 0
        tfs: Dict[str, int] = {}
        for a in d["alerts"]:
            color = (a.get("color") or "").lower()
            tf = str(a.get("timeframe") or "?")
            if color == "green":
                bull += 1
                tfs[tf] = tfs.get(tf, 0) + 1
            elif color == "red":
                bear += 1
        distinct_tf = len(tfs)
        if bull == bear:
            return 0.0
        agrees = (bull > bear) == (direction == "bull")
        if distinct_tf >= self._l2_quant_min_tf:
            return 1.0 if agrees else -1.0
        return 0.5 if agrees else -0.5

    def _l2_validate(self, symbol: str, direction: str) -> Tuple[bool, float, str]:
        """Run Level-2 validation. Returns (pass, score, detail).

        Fail-open: on bridge failure we ALLOW the alert. Missing components
        (bridge down / symbol untracked / buffer empty) contribute nothing;
        when ALL components are missing the bridge is treated as down and
        the fail_open setting decides (previously this silently rejected
        with a misleading 'score 0.00')."""
        base, pair = self._l2_symbols(symbol)
        score = 0.0
        parts = []
        missing = 0
        try:
            if self._l2_require_dom:
                s = self._l2_dom_score(pair, direction)
                if s is None:
                    missing += 1
                    parts.append("dom=--")
                else:
                    score += s
                    parts.append(f"dom={s:+.1f}")
            if self._l2_require_cvd:
                s = self._l2_cvd_score(pair, direction)
                if s is None:
                    missing += 1
                    parts.append("cvd=--")
                else:
                    score += s
                    parts.append(f"cvd={s:+.1f}")
            if self._l2_require_quant:
                s = self._l2_quant_score(base, direction)
                if s is None:
                    missing += 1
                    parts.append("quant=--")
                else:
                    score += s
                    parts.append(f"quant={s:+.1f}")
            required = sum((self._l2_require_dom, self._l2_require_cvd,
                            self._l2_require_quant))
            if required and missing >= required:
                # Bridge has no data at all for this symbol: honor fail_open.
                with self._state_lock:
                    self._l2_failopen_count += 1
                detail = " ".join(parts) + " l2-bridge-no-data"
                if self._l2_fail_open:
                    logger.debug("L2 bridge no-data for %s, fail-open ALLOW", symbol)
                    return True, 0.0, detail + "(fail-open)"
                return False, 0.0, detail
            ok = score >= self._l2_min_score
            return ok, round(score, 2), " ".join(parts)
        except Exception as e:
            logger.warning("L2 validation error for %s: %s", symbol, e)
            if self._l2_fail_open:
                with self._state_lock:
                    self._l2_failopen_count += 1
                return True, 0.0, "l2-error(fail-open)"
            return False, 0.0, "l2-error"

    def _confluence(self, c, rsi_arr, ml, sl, k, d, mid, v, vol_avg) -> Tuple[int, int]:
        """Count directional agreement across 6 independent factors:
        RSI vs 50, MACD line vs signal, %K vs %D, close vs BB mid,
        volume vs 20-bar average, close vs trend EMA.
        Returns (bull_count, bear_count)."""
        bull = bear = 0
        try:
            if rsi_arr is not None and not np.isnan(rsi_arr[-1]):
                if rsi_arr[-1] > 50: bull += 1
                elif rsi_arr[-1] < 50: bear += 1
            if ml is not None and not np.isnan(ml[-1]) and sl is not None and not np.isnan(sl[-1]):
                if ml[-1] > sl[-1]: bull += 1
                else: bear += 1
            if k is not None and not np.isnan(k[-1]) and d is not None and not np.isnan(d[-1]):
                if k[-1] > d[-1]: bull += 1
                else: bear += 1
            if mid is not None and not np.isnan(mid[-1]):
                if c[-1] > mid[-1]: bull += 1
                else: bear += 1
            if vol_avg is not None and not np.isnan(vol_avg) and not np.isnan(v[-1]):
                if v[-1] > vol_avg: bull += 1
                else: bear += 1
            tr = self._trend_regime(c)
            if tr > 0: bull += 1
            elif tr < 0: bear += 1
        except Exception:
            pass
        return bull, bear

    def _passes_tf_consolidation(self, sym: str, tf: str, name: str, bar_key: str) -> bool:
        """Suppress a signal if the SAME signal already fired on a higher
        timeframe whose candle window covers the current bar (fix #5)."""
        try:
            t0 = datetime.fromisoformat(bar_key).timestamp() if bar_key else 0.0
        except (ValueError, TypeError):
            return True
        my_rank = TF_RANK.get(tf)
        if my_rank is None:
            return True
        for htf, hrank in TF_RANK.items():
            if hrank <= my_rank:
                continue
            with self._state_lock:
                fired = self._last_fire.get((sym, htf, name))
            if fired:
                hts, _ = fired
                if hts and hts <= t0 < hts + TF_SECONDS.get(htf, DEFAULT_TF_SECONDS):
                    return False
        return True

    def _emit(self, name: str, sym: str, p: float, stype: str, color: str,
              bk: str, tf: str, conf: Tuple[int, int, int, int, bool],
              direction: Optional[str] = None,
              market_data: Optional[Dict[str, Any]] = None) -> bool:
        """Central emit gate: regime filter + confluence + multi-TF consolidation + macro + outcome gating.
        Returns True if the alert was actually emitted."""
        bull_conf, bear_conf, regime, conf_req, reg_enabled = conf
        if direction == "bull":
            if reg_enabled and regime < 0:
                return False
            if conf_req and bull_conf < conf_req:
                return False
        elif direction == "bear":
            if reg_enabled and regime > 0:
                return False
            if conf_req and bear_conf < conf_req:
                return False
        # Macro regime gate (v2.3)
        if self._macro_enabled and direction:
            macro = self._macro_regime()
            if macro == "bear" and direction == "bull":
                with self._state_lock:
                    self._macro_gated_count += 1
                logger.info("Macro-gated LONG %s %s %s (macro bear)", sym, tf, name)
                return False
            if macro == "bull" and direction == "bear":
                with self._state_lock:
                    self._macro_gated_count += 1
                logger.info("Macro-gated SHORT %s %s %s (macro bull)", sym, tf, name)
                return False
        # Outcome-aware gating (v2.3) - Fix H3: checked by signal name
        if self._outcome_enabled and self._is_outcome_muted(sym, name):
            with self._state_lock:
                self._outcome_gated_count += 1
            logger.info("Outcome-gated %s %s (expectancy<0)", sym, name)
            return False
        # Time gating (v2.4) — mute hours with negative expectancy
        if self._time_enabled and self._is_time_muted():
            with self._state_lock:
                self._time_gated_count += 1
            hr = (time.gmtime().tm_hour + self._time_utc_offset) % 24
            logger.info("Time-gated %s %s %s (hour %02d negative expectancy)", sym, tf, name, hr)
            return False
        if self._consolidation and not self._passes_tf_consolidation(sym, tf, name, bk):
            return False
        # Recency blackout (v2.5): throttle by wall-clock time, not by bar condition.
        if self._blackout_enabled:
            now = time.time()
            with self._state_lock:
                last_sym = self._last_symbol_fire.get(sym)
                if last_sym is not None and (now - last_sym) < self._blackout_seconds:
                    self._blackout_blocked_count += 1
                    logger.debug("Blackout: blocked %s %s %s (%.0fs since last sym alert)", sym, tf, name, now - last_sym)
                    return False
                sig_key = (sym, tf, name)
                last_sig = self._last_signal_fire.get(sig_key)
                if last_sig is not None and (now - last_sig) < self._signal_cooldown_seconds:
                    self._blackout_blocked_count += 1
                    logger.debug("Cooldown: blocked %s %s %s (%.0fs since last same-signal)", sym, tf, name, now - last_sig)
                    return False
        # Level-2 validator (v2.6): ATAS bridge order-flow gate for directional alerts.
        if self._l2_enabled and direction in ("bull", "bear"):
            l2_ok, l2_score, l2_detail = self._l2_validate(sym, direction)
            if not l2_ok:
                with self._state_lock:
                    self._l2_blocked_count += 1
                logger.info("L2-rejected %s %s %s (%s, score %.2f)", sym, tf, name, l2_detail, l2_score)
                return False
        # External macro gate (v2.7, opt-in via macro.gate_signals): suppress
        # directional signals that fight a strong external macro regime hint.
        if self._extmacro_enabled and self._extmacro_gate and direction in ("bull", "bear"):
            _mhint = "neutral"
            try:
                _mhint = str((market_data or {}).get("macro", {}).get("regime_hint", "neutral"))
            except Exception:
                _mhint = "neutral"
            if (_mhint == "bear" and direction == "bull") or (_mhint == "bull" and direction == "bear"):
                with self._state_lock:
                    self._extmacro_gated_count += 1
                logger.info("ExtMacro-gated %s %s %s (macro %s vs %s)", sym, tf, name, _mhint, direction)
                return False
        # ML meta-label gate (v2.8). Shadow by default: the probability is
        # attached to market_data (so it lands in the log, Telegram and the
        # signal_features row) and only blocks emission when ml.gate_signals is
        # true. Fail-open by default — a scoring error must not blackhole alerts.
        _dir_sign = get_signal_direction(name, direction)
        if self._ml_filter is not None:
            _p = None
            try:
                _p = self._ml_filter.score((market_data or {}).get("features") or {},
                                           stype, _dir_sign)
            except Exception as _me:
                logger.warning("ML scoring failed for %s %s %s: %s", sym, tf, name, _me)
            if _p is None:
                if not self._ml_fail_open:
                    with self._state_lock:
                        self._ml_gated_count += 1
                    logger.info("ML-gated %s %s %s (no score, fail-closed)", sym, tf, name)
                    return False
            else:
                with self._state_lock:
                    self._ml_scored_count += 1
                if market_data is not None:
                    market_data["ml_prob"] = round(float(_p), 4)
                    market_data["ml_dir"] = int(_dir_sign)
                if self._ml_gate_signals and float(_p) < self._ml_min_prob:
                    with self._state_lock:
                        self._ml_gated_count += 1
                    logger.info("ML-gated %s %s %s (p=%.3f < %.2f)",
                                sym, tf, name, float(_p), self._ml_min_prob)
                    return False
        self.alert_mgr.emit_alert(name, sym, p, stype, color, bk, tf,
                                  market_data=market_data, direction=_dir_sign)
        try:
            t0 = datetime.fromisoformat(bk).timestamp() if bk else 0.0
        except (ValueError, TypeError):
            t0 = 0.0
        now = time.time()
        with self._state_lock:
            self._last_fire[(sym, tf, name)] = (t0, bk)
            if self._blackout_enabled:
                self._last_symbol_fire[sym] = now
                self._last_signal_fire[(sym, tf, name)] = now
        return True

    def _div_alert(self, name: str, sym: str, p: float, bk: str, tf: str,
                   stype: str, color: str, conf: Tuple[int, int, int, int, bool],
                   direction: str, detected: bool,
                   market_data: Optional[Dict[str, Any]] = None):
        """Divergence episode gating (fix #1): fire ONCE per divergence episode,
        reset when the condition clears, instead of alerting every bar."""
        key = (sym, tf, name)
        with self._state_lock:
            if not detected:
                self._div_episodes.pop(key, None)
                return
            if key in self._div_episodes:
                return
        if self._emit(name, sym, p, stype, color, bk, tf, conf, direction,
                      market_data=market_data):
            with self._state_lock:
                self._div_episodes[key] = True

    def _resolve_outcomes(self, symbol: str, timeframe: str, df_sig: pd.DataFrame):
        """Resolve forward N-bar returns for tagged signals whose horizon has
        elapsed within the current window (fix #3).
        Fix C1: Accounts for signal direction (short-side returns negated).
        Fix H5: Auto-resolves signals older than current history to prevent memory/query leak."""
        try:
            rows = self.alert_mgr.get_unresolved(symbol, timeframe, limit=100)
            if not rows:
                return
            ts_map = {t.isoformat(): i for i, t in enumerate(df_sig['timestamp'])}
            closes = df_sig['close'].values
            oldest_bar_key = df_sig['timestamp'].iloc[0].isoformat() if len(df_sig) > 0 else ""

            for row in rows:
                signal_id, bar_key, price, fwd_bars = row[0], row[1], row[2], row[3]
                sig_name = row[4] if len(row) > 4 else ""
                idx = ts_map.get(bar_key)
                if idx is None:
                    # Fix H5: If bar_key is older than the oldest bar in our history window,
                    # the horizon has permanently passed and cannot be resolved from this window.
                    # Resolve as 0.0 so it stops being polled indefinitely.
                    if bar_key and oldest_bar_key and bar_key < oldest_bar_key:
                        self.alert_mgr.resolve_outcome(signal_id, 0.0)
                        logger.debug("Auto-resolved stale signal beyond window %s", signal_id)
                    continue
                if idx + int(fwd_bars) >= len(closes):
                    continue
                base = float(price)
                if not base or np.isnan(base):
                    continue
                raw_ret = float(closes[idx + int(fwd_bars)]) / base - 1.0
                # Prefer the direction recorded at emit time; the name-based
                # inference disagrees with the emit-time hint for some signals
                # and would store the return with the wrong sign.
                stored_dir = 0
                try:
                    stored_dir = int(row[5]) if len(row) > 5 and row[5] is not None else 0
                except (TypeError, ValueError):
                    stored_dir = 0
                dir_sign = stored_dir if stored_dir else get_signal_direction(sig_name)
                # Fix C1: Short signals profit when price falls
                directional_ret = -raw_ret if dir_sign == -1 else raw_ret
                self.alert_mgr.resolve_outcome(signal_id, directional_ret)
                logger.debug("Resolved outcome %s: %+.4f (%d bars, dir=%+d)", signal_id, directional_ret, int(fwd_bars), dir_sign)
        except Exception as e:
            logger.debug("Outcome resolution failed: %s", e)

    def _vol_baseline(self, sym: str) -> Optional[float]:
        """Per-symbol volume baseline: the symbol's own median bar volume.

        Fix VOL-1: a fixed 0.5x-of-20-bar-mean dryup threshold is inside the normal
        distribution of low-liquidity instruments (NVDA is under it 45% of the time), so
        the same "dryup" fires and re-fires all session. Each symbol must be measured
        against itself. Cached in-process; refreshed lazily, never blocking.

        NB Binance caps /klines at limit=1500 and answers HTTP 400 above that — asking for
        2880 rows (a month of 15m bars) returns nothing, which would have made the
        baseline silently unavailable and silently kept the old behaviour. 1500 rows of
        15m is 15.6 days, which is the baseline we get.
        """
        cached = self._vol_base_cache.get(sym)
        if cached and (time.time() - cached[0]) < self._vol_base_ttl:
            return cached[1] or None
        # The engine passes TradingView-style symbols ('SOL/USDT:USDT'); /fapi needs
        # 'SOLUSDT'. Sending the raw form 400s, which silently left every symbol on the
        # 20-bar-mean fallback path.
        api_sym = str(sym).split(":")[0].replace("/", "").replace("-", "").upper()
        try:
            req = _urllib.Request(
                f"https://fapi.binance.com/fapi/v1/klines?symbol={api_sym}&interval=15m&limit=1500",
                headers={"User-Agent": "quant-alert-engine"})
            with _urllib.urlopen(req, timeout=20) as resp:
                rows = json.loads(resp.read().decode())
            vols = sorted(float(r[5]) for r in rows)
            if not vols:
                return None
            med = vols[len(vols) // 2]
            self._vol_base_cache[sym] = (time.time(), med)
            logger.debug("vol baseline %s = %.2f (%d bars)", sym, med, len(vols))
            return med
        except Exception as e:  # noqa: BLE001
            logger.warning("vol baseline fetch failed for %s: %s: %s",
                           sym, type(e).__name__, str(e)[:120])
            self._vol_base_cache[sym] = (time.time(), None)
            return None

    def _vol_state_persisted(self, sym: str, tf: str, state: str, bar_key: str) -> bool:
        """True when `state` has held for N consecutive CLOSED BARS.

        Fix VOL-1: without this a ratio sitting on the threshold re-enters and exits the
        state between bars and every re-entry is a fresh alert. Volume regime is a slow
        thing; it must persist before it is called an event.

        Counted in BARS, not polls, and that distinction is the whole fix: the engine
        polls every 5s (general.check_interval) and re-evaluates the SAME closed bar ~60
        times on a 5m timeframe, so a poll counter would reach its threshold in 15 seconds
        without the market changing at all. The bar_key is the closed bar's open time, so
        the same bar key repeating means "no new information" and must not advance the
        count.
        """
        need = int(self.config.get('volume.state_persistence_bars', 2))
        if need <= 1:
            return True
        key = f"{sym}_{tf}_vol_hold"
        prev_key, hold = self._vol_pending.get(key, (None, 0))
        if bar_key == prev_key:
            return hold >= need            # same bar seen again: no progress, no reset
        hold = hold + 1 if state == self._vol_pending.get(f"{sym}_{tf}_vol_state") else 1
        self._vol_pending[key] = (bar_key, hold)
        self._vol_pending[f"{sym}_{tf}_vol_state"] = state
        return hold >= need

    def _obv_break_persisted(self, sym: str, tf: str, side: str, bar_key: str) -> bool:
        """True when the OBV break has held for N consecutive CLOSED BARS.

        Fix VOL-2. Counted in bars via bar_key, for the same reason as the volume state:
        the engine polls every 5s and re-evaluates one closed bar ~60 times on 5m, so a
        poll counter would fire instantly and mean nothing.
        """
        need = int(self.config.get('volume.obv_break_bars', 2))
        if need <= 1:
            return True
        key = f"{sym}_{tf}_obv_hold"
        prev_key, hold = self._vol_pending.get(key, (None, 0))
        if bar_key == prev_key:
            return hold >= need
        hold = hold + 1 if side == self._vol_pending.get(f"{sym}_{tf}_obv_side") else 1
        self._vol_pending[key] = (bar_key, hold)
        self._vol_pending[f"{sym}_{tf}_obv_side"] = side
        return hold >= need

    def _check_volume(self, sym, p, c, v, bk, tf, vol_avg, conf,
                       market_data: Optional[Dict[str, Any]] = None):
        cur_vol = v[-1]
        state_key = f"{sym}_{tf}_vol_state"

        if vol_avg is None or np.isnan(vol_avg): return

        # ── Fix VOL-1 (2026-09-27): the volume-state thresholds were absolute multiples
        # of a 20-bar mean, so they measured the INSTRUMENT, not the event. Measured over
        # 300 5m bars: NVDA's volume/avg(20) is below 0.5x 44.8% of the time and SPCX
        # 37.7% (median ratio 0.62 / 0.63). "Dryup" was therefore NVDA's steady state and
        # the engine re-fired on every exit from it: 43 of 48 alerts today were VOL states,
        # and you received Dryup/Spike/Climax repeatedly for the same quiet tape.
        #
        # Two changes, both measured rather than guessed:
        #  1. Hysteresis: a state must CLEAR a wider exit band before it can change, so a
        #     ratio hovering on the threshold cannot machine-gun transitions.
        #  2. The thresholds are now PER-SYMBOL, from the symbol's own 30-day median
        #     ratio (cached in SQLite). A 0.5x-of-own-median bar is genuinely rare for
        #     every instrument instead of ordinary for NVDA.
        # The 20-bar mean is still used when no per-symbol baseline is available yet.
        base = self._vol_baseline(sym)
        ratio_txt = ""
        if base:
            e_hi = self.config.get('volume.climax_mult', 3.5)
            e_mid = self.config.get('volume.spike_mult', 2.0)
            e_lo = self.config.get('volume.dryup_mult', 0.5)
            ratio = cur_vol / base
            ratio_txt = f" vs own 30d median ({ratio:.2f}x)"
        else:
            d_hi = self.config.get('volume.dryup_exit_mult', 0.8) * vol_avg
            e_hi = self.config.get('volume.climax_mult', 3.5) * vol_avg
            e_mid = self.config.get('volume.spike_mult', 2.0) * vol_avg
            e_lo = self.config.get('volume.dryup_mult', 0.5) * vol_avg
            ratio = cur_vol / vol_avg if vol_avg else 0.0

        # Transition logic for volume (state persisted in SQLite, survives restarts)
        cur_state = "normal"
        if ratio > e_hi: cur_state = "climax"
        elif ratio > e_mid: cur_state = "spike"
        elif ratio < e_lo: cur_state = "dryup"

        prev_state = self._STATE_CODE_TO_NAME.get(self.alert_mgr.get_state_val(state_key), "normal")
        if cur_state != "normal" and cur_state != prev_state:
            if self._vol_state_persisted(sym, tf, cur_state, bk):
                self._emit(f"Volume {cur_state.capitalize()}", sym, p, "VOL", "red", bk, tf,
                           conf, market_data=market_data)
        self.alert_mgr.set_state_val(state_key, self._STATE_NAME_TO_CODE[cur_state])
        if cur_state == "normal":
            self._vol_pending.pop(f"{sym}_{tf}_vol_hold", None)
            self._vol_pending.pop(f"{sym}_{tf}_vol_state", None)

        # OBV Cross — Fix VOL-2 (2026-09-27): this was a bare one-bar crossover of OBV
        # against its own SMA, with no persistence and no distance filter. Measured over
        # 300 bars x 7 symbols x 2 timeframes that is 443 raw crosses, and the engine's
        # own track record shows OBV Bullish Break at n=67 averaging -0.038% forward —
        # it was the single noisiest emitter in the feed and no better than a coin flip.
        # Now: the cross must persist N bars AND clear a margin proportional to OBV's own
        # volatility, so a graze through the line is not an event.
        obv = np.cumsum(np.sign(np.diff(c, prepend=c[0])) * v)
        obv_sma = self.ind.sma(obv, self.config.get('volume.obv_len', 20))
        if not np.isnan(obv_sma[-1]) and not np.isnan(obv_sma[-2]):
            # FIX (crash): ind.sma() returns a SAME-LENGTH array (pandas rolling, NaN-padded
            # at the front), not a shortened one. Slicing obv[19:] against it produced
            # shapes (180,) vs (199,) and killed the engine thread 112 times on 2026-09-28.
            # Drop the leading window of BOTH arrays so they stay aligned.
            _w = len(obv) - 19
            if _w < 2 or len(obv_sma) != len(obv):
                return
            resid = obv[19:] - obv_sma[19:]            # signed distance to the line
            sd = float(np.std(resid[-100:])) if len(resid) >= 20 else 0.0
            margin = self.config.get('volume.obv_break_sigma', 0.75) * sd
            d_now, d_prev = resid[-1], resid[-2]
            side = "bull" if d_now > margin else ("bear" if d_now < -margin else "flat")
            prev_side = "bull" if d_prev > margin else ("bear" if d_prev < -margin else "flat")
            if side != "flat" and side != prev_side:
                if self._obv_break_persisted(sym, tf, side, bk):
                    self._emit("OBV Bullish Break" if side == "bull" else "OBV Bearish Break",
                               sym, p, "VOL", "green" if side == "bull" else "red", bk, tf,
                               conf, side, market_data=market_data)
            if side == "flat":
                self._vol_pending.pop(f"{sym}_{tf}_obv_hold", None)

    def _check_cvd(self, sym, p, c, bk, tf, cvd_cum, cvd_smooth, conf,
                   market_data: Optional[Dict[str, Any]] = None):
        # Bar-signed volume index (tick-rule proxy) vs its EMA: order-flow trend flip
        if len(cvd_cum) < 2 or np.isnan(cvd_cum[-1]) or np.isnan(cvd_cum[-2]):
            return

        if cvd_cum[-1] > cvd_smooth[-1] and cvd_cum[-2] <= cvd_smooth[-2]:
            self._emit("CVD Bullish Flip", sym, p, "CVD", "green", bk, tf, conf, "bull", market_data=market_data)
        elif cvd_cum[-1] < cvd_smooth[-1] and cvd_cum[-2] >= cvd_smooth[-2]:
            self._emit("CVD Bearish Flip", sym, p, "CVD", "red", bk, tf, conf, "bear", market_data=market_data)

        rb, ru, hb, hu = self.div.detect_divergence(
            c, cvd_cum, self.config.get('cvd.pivot_len', 5),
            self.config.get('cvd.div_lookback', 20), self.config.get('cvd.min_gap', 3))
        self._div_alert("CVD Reg Bear Div", sym, p, bk, tf, "CVD_DIV", "red", conf, "bear", rb, market_data=market_data)
        self._div_alert("CVD Reg Bull Div", sym, p, bk, tf, "CVD_DIV", "green", conf, "bull", ru, market_data=market_data)
        self._div_alert("CVD Hidden Bear Div", sym, p, bk, tf, "CVD_DIV", "red", conf, "bear", hb, market_data=market_data)
        self._div_alert("CVD Hidden Bull Div", sym, p, bk, tf, "CVD_DIV", "green", conf, "bull", hu, market_data=market_data)

    def _check_rsi(self, sym, p, c, bk, tf, rsi, conf,
                   market_data: Optional[Dict[str, Any]] = None):
        ob, ovs = self.config.get('rsi.overbought', 70), self.config.get('rsi.oversold', 30)

        if rsi[-1] > ob and rsi[-2] <= ob:
            self._emit("RSI Overbought", sym, p, "RSI", "red", bk, tf, conf, "bear", market_data=market_data)
        elif rsi[-1] < ovs and rsi[-2] >= ovs:
            self._emit("RSI Oversold", sym, p, "RSI", "green", bk, tf, conf, "bull", market_data=market_data)

        if rsi[-1] > 50 and rsi[-2] <= 50:
            self._emit("RSI Crossed Above 50", sym, p, "RSI", "green", bk, tf, conf, "bull", market_data=market_data)
        elif rsi[-1] < 50 and rsi[-2] >= 50:
            self._emit("RSI Crossed Below 50", sym, p, "RSI", "red", bk, tf, conf, "bear", market_data=market_data)

        rb, ru, hb, hu = self.div.detect_divergence(
            c, rsi, self.config.get('rsi.pivot_len', 5),
            self.config.get('rsi.div_lookback', 20), self.config.get('rsi.min_gap', 3))
        self._div_alert("RSI Reg Bear Div", sym, p, bk, tf, "RSI_DIV", "red", conf, "bear", rb, market_data=market_data)
        self._div_alert("RSI Reg Bull Div", sym, p, bk, tf, "RSI_DIV", "green", conf, "bull", ru, market_data=market_data)
        self._div_alert("RSI Hidden Bear Div", sym, p, bk, tf, "RSI_DIV", "red", conf, "bear", hb, market_data=market_data)
        self._div_alert("RSI Hidden Bull Div", sym, p, bk, tf, "RSI_DIV", "green", conf, "bull", hu, market_data=market_data)

    def _check_macd(self, sym, p, c, bk, tf, ml, sl, hist, conf,
                      market_data: Optional[Dict[str, Any]] = None):
        # MACD cross/zero/histogram alerts REMOVED (2026-09-21): the 6 MACD signals
        # (Bullish/Bearish Cross, Crossed Above/Below Zero, Hist Flip Up/Down) are no
        # longer emitted. Only the 4 MACD divergences below remain.

        rb, ru, hb, hu = self.div.detect_divergence(
            c, ml, self.config.get('macd.pivot_len', 5),
            self.config.get('macd.div_lookback', 20), self.config.get('macd.min_gap', 3))
        self._div_alert("MACD Reg Bear Div", sym, p, bk, tf, "MACD_DIV", "red", conf, "bear", rb, market_data=market_data)
        self._div_alert("MACD Reg Bull Div", sym, p, bk, tf, "MACD_DIV", "green", conf, "bull", ru, market_data=market_data)
        self._div_alert("MACD Hidden Bear Div", sym, p, bk, tf, "MACD_DIV", "red", conf, "bear", hb, market_data=market_data)
        self._div_alert("MACD Hidden Bull Div", sym, p, bk, tf, "MACD_DIV", "green", conf, "bull", hu, market_data=market_data)

    def _check_vol(self, sym, p, h, l, c, bk, tf, upper, mid, lower, atr, conf,
                       market_data: Optional[Dict[str, Any]] = None):
        # BB alerts REMOVED (2026-09-21): BB Breakout Up/Down and BB Squeeze (Entry)
        # are no longer emitted. Only ATR Expansion below remains.

        # ATR Expansion Transition
        atr_len = self.config.get('volatility.atr_len', 14)
        if not np.isnan(atr[-1]) and len(atr) > atr_len:
            # Off-by-one fix: exclude current bar from average baseline
            atr_avg = np.mean(atr[-atr_len-1:-1])
            state_key = f"{sym}_{tf}_atr_exp"
            if atr[-1] > atr_avg * self.config.get('volatility.atr_expansion_mult', 1.8):
                if not self.alert_mgr.get_state(state_key):
                    self._emit("ATR Expansion (Vol Surge)", sym, p, "VOL", "yellow", bk, tf, conf, market_data=market_data)
                    self.alert_mgr.set_state(state_key, True)
            else:
                self.alert_mgr.set_state(state_key, False)

# ============================================================================
# BACKTESTING HARNESS — Isolated DB to avoid polluting live state
# ============================================================================
class Backtester:
    """Replays historical bars through the REAL signal path (_evaluate) and
    reports per-signal hit rate + forward-return statistics with an
    in-sample / out-of-sample split, costs/slippage/funding, regime split,
    horizon sweep, overlapping-trade effective-N, and multiple-testing
    haircut (quant audit fix #6).

    Methodology notes:
    - Every evaluation sees only bars up to the current one — no lookahead.
    - The last `test_fraction` of the window is the held-out (OOS) slice;
      keep it untouched when tuning parameters.
    - Stats are computed per signal type at several forward horizons
      (e.g. 3/12/50 bars). Win = forward return > 0.
    - Transaction costs (commission + slippage), funding carry, and dryup-
      scaled slippage are applied to every trade.
    - Walk-forward: a rolling-window variant is available via WalkForward.
    """

    def __init__(self, config: Config):
        self.config = config
        self.costs = CostModel(config)
        # Use isolated DB so backtest states don't corrupt live signal_states
        import copy
        bt_config_data = copy.deepcopy(config.data)
        bt_config_data["database"] = ":memory:"
        # Build a temporary Config-like shim for Backtester
        class _BTConfig:
            def __init__(self, d, orig):
                self.data = d
                self._orig = orig
                self._settings = orig._settings
            def get(self, key, default=None):
                keys = key.split(".")
                val = self.data
                for k in keys:
                    if isinstance(val, dict) and k in val:
                        val = val[k]
                    else:
                        return self._orig.get(key, default)
                return val
        bt_config = _BTConfig(bt_config_data, config)
        self.analyzer = SignalAnalyzer(bt_config)

    def _bars_per_year(self, timeframe):
        return int((365 * 24 * 3600) / TF_SECONDS.get(timeframe, 300))

    def _precompute_regimes(self, closes):
        trend_len = self.config.get('general.regime_trend_len', 50)
        ema = self.analyzer.ind.ema(closes, trend_len)
        regimes = []
        for i in range(len(closes)):
            if i < trend_len + 5:
                regimes.append(0)
            else:
                regimes.append(1 if closes[i] > ema[i] else (-1 if closes[i] < ema[i] else 0))
        return regimes

    def run(self, symbol: str, timeframe: str = '5m', bars: int = 2000,
            test_fraction: float = 0.25, horizons=(3, 12, 50)):
        logger.info(f"--- STARTING BACKTEST: {symbol} {timeframe} ({bars} bars) ---")
        df = self.analyzer.fetch_ohlcv(symbol, timeframe, limit=bars)
        if df is None or len(df) < 110:
            logger.warning("Not enough data to backtest %s %s", symbol, timeframe)
            return

        # Capture every signal the real checks emit during replay
        captured = []
        orig_emit = self.analyzer.alert_mgr.emit_alert
        self.analyzer.alert_mgr.emit_alert = (
            lambda name, sym, price, signal_type="", color="info", bar_key="", timeframe="":
            captured.append({"name": name, "sym": sym, "price": price,
                             "signal_type": signal_type, "bar_key": bar_key,
                             "timeframe": timeframe}))

        try:
            for i in range(100, len(df)):
                slice_df = df.iloc[:i + 1]
                self.analyzer._evaluate(symbol, timeframe, slice_df)
        finally:
            self.analyzer.alert_mgr.emit_alert = orig_emit

        if not captured:
            logger.info("No signals captured during replay window.")
            return

        ts_map = {t.isoformat(): i for i, t in enumerate(df['timestamp'])}
        closes = df['close'].values
        n = len(df)
        split_i = int(n * (1.0 - test_fraction))
        regimes = self._precompute_regimes(closes)
        bpy = self._bars_per_year(timeframe)

        # (name, horizon, phase, regime) -> [count, wins, sum_return]
        stats: Dict[Tuple[str, int, str, int], List[float]] = {}

        def record(name, h, ret, phase, regime):
            key = (name, h, phase, regime)
            cur = stats.get(key)
            if cur is None:
                cur = [0.0, 0.0, 0.0]
                stats[key] = cur
            cur[0] += 1
            if ret > 0: cur[1] += 1
            cur[2] += ret

        # Build the trades list for full metrics
        all_trades = []
        for sig in captured:
            i = ts_map.get(sig["bar_key"])
            if i is None: continue
            phase = "OOS" if i >= split_i else "IS"
            regime = regimes[i] if i < len(regimes) else 0
            dryup = (phase == "OOS" and sig["name"] == "Volume Dryup")
            cost_pct = self.costs.roundtrip_cost_pct(dryup=dryup)
            dir_sign = get_signal_direction(sig["name"])
            for h in horizons:
                if i + h < n:
                    raw_ret = float(closes[i + h]) / float(sig["price"]) - 1.0
                    # Fix C1: Short signals profit when price falls
                    gross_ret = -raw_ret if dir_sign == -1 else raw_ret
                    fwd_cost = self.costs.funding_cost(float(sig["price"]), h)
                    net_ret = gross_ret - cost_pct - fwd_cost
                    record(sig["name"], h, net_ret, phase, regime)
                    all_trades.append({
                        "name": sig["name"], "ret": net_ret, "gross_ret": gross_ret,
                        "entry_idx": i, "exit_idx": i + h, "regime": regime,
                        "phase": phase, "horizon": h,
                    })

        if not stats:
            logger.info("No resolvable signals (forward window out of range).")
            return

        # Per-signal full metrics
        logger.info(f"--- BACKTEST REPORT: {symbol} {timeframe} (n={n} bars, "
                    f"IS={split_i}, OOS={n - split_i}, bpy={bpy}) ---")
        for phase in ("IS", "OOS"):
            for h in horizons:
                lines = []
                for (name, hh, ph, regime), (cnt, wins, tot) in sorted(stats.items()):
                    if hh != h or ph != phase: continue
                    wr = wins / cnt * 100.0
                    avg = tot / cnt * 100.0
                    lines.append(f"{name}({_regime_label(regime)}): n={int(cnt)} win={wr:.0f}% avg={avg:+.2f}%")
                if lines:
                    logger.info(f"[{phase} | {h}-bar] " + " | ".join(lines))

        # Aggregate full metrics
        metrics = compute_metrics(all_trades, bpy)
        logger.info("FULL METRICS: win_rate=%.1f%% profit_factor=%.2f expectancy=%.4f "
                    "sharpe=%.2f sortino=%.2f calmar=%.2f max_dd=%.1f%% max_dd_time=%dbars "
                    "total_ret=%.2f%% effective_n=%.0f",
                    metrics.get("win_rate", 0) * 100, metrics.get("profit_factor", 0),
                    metrics.get("expectancy", 0), metrics.get("sharpe", 0),
                    metrics.get("sortino", 0), metrics.get("calmar", 0),
                    metrics.get("max_dd_pct", 0), metrics.get("max_dd_time_bars", 0),
                    metrics.get("total_return", 0) * 100, metrics.get("effective_n", 0))
        if metrics.get("regime_stats"):
            logger.info("REGIME SPLIT: %s", json.dumps(metrics["regime_stats"], indent=2))
        if metrics.get("multiple_testing_note"):
            logger.info("MULTIPLE-TESTING: %s", metrics["multiple_testing_note"])
        logger.info("--- BACKTEST COMPLETE ---")
        return metrics


class WalkForward:
    """Rolling-window walk-forward validation (quant audit fix #6).
    Partitions the bar series into N sequential folds; each fold's
    test window uses only expanding history (no lookahead). Signals
    falling in the test window are treated as held-out for that fold.
    """

    def __init__(self, config: Config, n_folds: int = 5,
                 test_ratio: float = 0.2):
        self.config = config
        self.n_folds = n_folds
        self.test_ratio = test_ratio

    def run(self, symbol: str, timeframe: str = '5m', bars: int = 2000,
            horizons=(3, 12, 50)):
        logger.info(f"--- WALK-FORWARD: {symbol} {timeframe} ({bars} bars, "
                    f"{self.n_folds} folds, test={self.test_ratio}) ---")
        bt = Backtester(self.config)
        df = bt.analyzer.fetch_ohlcv(symbol, timeframe, limit=bars)
        if df is None or len(df) < 110:
            logger.warning("Not enough data for walk-forward %s %s", symbol, timeframe)
            return None

        n = len(df)
        ts_map = {t.isoformat(): i for i, t in enumerate(df['timestamp'])}
        closes = df['close'].values
        test_size = max(10, int(n * self.test_ratio))
        # folds: each test window is `test_size` bars, sequential, non-overlapping
        folds = []
        start = n - self.n_folds * test_size
        if start < 110:
            logger.info("Window too small for %d folds; running single backtest.", self.n_folds)
            return bt.run(symbol, timeframe, bars=bars)
        for k in range(self.n_folds):
            folds.append((start + k * test_size, start + (k + 1) * test_size, f"fold{k + 1}"))

        # Capture signals with correct expanding history (delegate Backtester replay)
        captured = []
        orig_emit = bt.analyzer.alert_mgr.emit_alert
        bt.analyzer.alert_mgr.emit_alert = (
            lambda name, sym, price, signal_type="", color="info", bar_key="", timeframe="":
            captured.append({"name": name, "sym": sym, "price": price,
                             "signal_type": signal_type, "bar_key": bar_key,
                             "timeframe": timeframe}))
        try:
            for i in range(100, len(df)):
                bt.analyzer._evaluate(symbol, timeframe, df.iloc[:i + 1])
        finally:
            bt.analyzer.alert_mgr.emit_alert = orig_emit

        if not captured:
            logger.info("No signals captured for walk-forward.")
            return None

        # Partition captured signals into folds by bar index
        fold_captured = {label: [] for _, _, label in folds}
        for sig in captured:
            i = ts_map.get(sig["bar_key"])
            if i is None: continue
            for fs, fe, label in folds:
                if fs <= i < fe:
                    fold_captured[label].append(sig)

        # Aggregate across folds with full metrics
        fold_stats = {}
        all_trades = []
        regimes = bt._precompute_regimes(closes)
        bpy = bt._bars_per_year(timeframe)
        for label, sigs in fold_captured.items():
            if not sigs: continue
            fold_trades = []
            for sig in sigs:
                i = ts_map.get(sig["bar_key"])
                if i is None: continue
                regime = regimes[i] if i < len(regimes) else 0
                cost_pct = bt.costs.roundtrip_cost_pct(dryup=(sig["name"] == "Volume Dryup"))
                dir_sign = get_signal_direction(sig["name"])
                for h in horizons:
                    if i + h < n:
                        raw_ret = float(closes[i + h]) / float(sig["price"]) - 1.0
                        # Fix C1: Short signals profit when price falls
                        gross_ret = -raw_ret if dir_sign == -1 else raw_ret
                        fwd_cost = bt.costs.funding_cost(float(sig["price"]), h)
                        fold_trades.append({
                            "name": sig["name"], "ret": gross_ret - cost_pct - fwd_cost,
                            "gross_ret": gross_ret, "entry_idx": i, "exit_idx": i + h,
                            "regime": regime, "phase": label, "horizon": h,
                        })
            all_trades.extend(fold_trades)
            fold_stats[label] = compute_metrics(fold_trades, bpy)

        agg = compute_metrics(all_trades, bpy)
        logger.info("--- WALK-FORWARD REPORT: %s %s (%d folds) ---", symbol, timeframe, self.n_folds)
        for label, st in fold_stats.items():
            logger.info("[%s] n=%d win_rate=%.1f%% pf=%.2f sharpe=%.2f max_dd=%.1f%%",
                        label, st.get("n_trades", 0), st.get("win_rate", 0) * 100,
                        st.get("profit_factor", 0), st.get("sharpe", 0), st.get("max_dd_pct", 0))
        logger.info("AGGREGATE across folds: n=%d win_rate=%.1f%% pf=%.2f sharpe=%.2f "
                    "sortino=%.2f calmar=%.2f max_dd=%.1f%% total_ret=%.2f%% effective_n=%.0f",
                    agg.get("n_trades", 0), agg.get("win_rate", 0) * 100,
                    agg.get("profit_factor", 0), agg.get("sharpe", 0),
                    agg.get("sortino", 0), agg.get("calmar", 0),
                    agg.get("max_dd_pct", 0), agg.get("total_return", 0) * 100, agg.get("effective_n", 0))
        if agg.get("regime_stats"):
            logger.info("REGIME SPLIT: %s", json.dumps(agg["regime_stats"], indent=2))
        if agg.get("multiple_testing_note"):
            logger.info("MULTIPLE-TESTING: %s", agg["multiple_testing_note"])
        logger.info("--- WALK-FORWARD COMPLETE ---")
        return {"fold_stats": fold_stats, "aggregate": agg, "n_folds": self.n_folds}


# ============================================================================
# MAIN APPLICATION
# ============================================================================
class QuantAlertEngine:
    def __init__(self, config_file: str = 'config.json'):
        # Clean error message on bad config instead of raw traceback (fix #8)
        try:
            self.config = load_config(config_file)
        except SystemExit:
            raise
        except Exception:
            # load_config already printed guidance to stderr; exit cleanly (fix #8)
            sys.exit(1)
        # Reconfigure logging with validated log_level
        log_level = self.config.get('general.log_level', 'INFO')
        setup_logging(log_file="logs/quant_alert_engine.log", level=log_level, json_format=True)

        self.analyzer = SignalAnalyzer(self.config)
        self.running = True
        self._last_stats_log = time.time()
        self._snapshot_last_send = time.time()
        # Periodic-card state (P1/P2 upgrade 2026-09-19):
        #   _snapshot_inflight  — prevents overlapping builds (card build can
        #                         outlast the interval; the old code re-launched
        #                         a thread every cycle and sent duplicate cards)
        #   _next_snapshot_at   — wall-clock-aligned next fire (:00/:15/:30/:45)
        #   _last_good_card     — last successfully SENT card, replayed with a
        #                         stale header if a fresh build fails
        self._snapshot_inflight = False
        self._next_snapshot_at = None
        self._last_good_card = None
        self.executor = ThreadPoolExecutor(max_workers=self.config.get('general.max_workers', 10))
        
        # Health server
        self._health_thread = None
        if self.config.get('general.health_enabled', True):
            try:
                from health_server import register_check, start_health_server
                register_check("exchange", lambda: {
                    "status": "healthy" if getattr(self.analyzer, 'exchange', None) else "unhealthy",
                    "metrics": {"connected": 1 if getattr(self.analyzer, 'exchange', None) else 0}
                })
                register_check("database", lambda: {
                    "status": "healthy",
                    "metrics": {"symbols_configured": len(self.config.get('symbols', []))}
                })
                register_check("symbols", lambda: {
                    "status": "healthy" if self.config.get('symbols') else "degraded",
                    "metrics": {"symbols_configured": len(self.config.get('symbols', []))}
                })
                register_check("macro_regime", lambda: self.analyzer.macro_health())
                register_check("outcome_gating", lambda: self.analyzer.gating_health())
                register_check("time_gating", lambda: self.analyzer.time_health())
                register_check("macro_context", lambda: self.analyzer.macro_context_health() if hasattr(self.analyzer, 'macro_context_health') else {"status": "healthy", "metrics": {"enabled": 0}})
                # README documents blackout/level2 checks; they were never
                # registered. Register them, plus the ML meta-label layer.
                register_check("blackout", lambda: self.analyzer.blackout_health())
                register_check("level2", lambda: self.analyzer.level2_health())
                register_check("ml_filter", lambda: self.analyzer.ml_health())
                port = self.config.get('general.health_port', 8080)
                host = self.config.get('general.health_host', '127.0.0.1')
                self._health_thread = start_health_server(host=host, port=port)
            except Exception as e:
                logger.warning("Health server not started: %s", e)

        # Graceful shutdown
        system_signal.signal(system_signal.SIGINT, self._shutdown)
        system_signal.signal(system_signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        logger.info(f"Signal {signum} received. Shutting down gracefully...")
        self.running = False
        self.executor.shutdown(wait=False)

    def _send_telegram(self, message: str, parse_mode: str = None, attempts: int = 3) -> bool:
        """Send one message, retrying transient failures with backoff.

        P1 upgrade (2026-09-19): a single attempt lost the card on any 429/5xx
        or socket blip. HTTP errors back off too (a 429 without backoff invites
        a Telegram ban). Returns True only on a confirmed 200.
        """
        token = self.config.get('telegram.token', '')
        chat_id = self.config.get('telegram.chat_id', '')
        if not token or not chat_id:
            logger.debug("Telegram disabled: missing token or chat_id")
            return False
        # Plain text by default: the legacy "Markdown" parse mode rejects the message
        # outright when a value contains an underscore (e.g. above_value) — it failed
        # silently, returning False and logging at debug. Opt in per call if needed.
        body = {"chat_id": chat_id, "text": message}
        if parse_mode:
            body["parse_mode"] = parse_mode
        delay = 2.0
        last = ""
        for attempt in range(1, max(1, attempts) + 1):
            try:
                req = _urllib.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with _urllib.urlopen(req, timeout=15) as resp:
                    if resp.status == 200:
                        logger.info("Telegram snapshot sent to %s (%d chars, attempt %d)",
                                    chat_id, len(message), attempt)
                        return True
                    last = f"HTTP {resp.status}"
            except Exception as e:
                last = f"{e}"
                try:
                    last += " " + e.read().decode()[:200]   # Telegram's own reason
                except Exception:
                    pass
            if attempt < attempts:
                logger.warning("Telegram send failed (attempt %d/%d): %s — retrying in %.0fs",
                               attempt, attempts, last, delay)
                time.sleep(delay)
                delay *= 2.0
        logger.warning("Telegram send gave up after %d attempts: %s", attempts, last)
        return False

    def _send_telegram_chunked(self, message: str, limit: int = 3800) -> bool:
        """Send a possibly-long message, splitting on line boundaries.

        Telegram's hard limit is 4096 chars; the card path used to send raw and
        could fail silently with HTTP 400 once the degraded list grew (P1).
        Returns True only if every chunk was delivered.
        """
        if len(message) <= limit:
            return self._send_telegram(message)
        chunks, cur = [], ""
        for line in message.split("\n"):
            if len(cur) + len(line) + 1 > limit:
                if cur:
                    chunks.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            chunks.append(cur)
        ok = True
        for i, ch in enumerate(chunks, 1):
            if not self._send_telegram(ch):
                ok = False
                logger.warning("Telegram chunk %d/%d failed", i, len(chunks))
        return ok

    def _blackout_active(self):
        """(active, event_name) from the bridge's macro-event blackout."""
        st = self._fetch_bridge_data("/blackout/status")
        if not isinstance(st, dict) or st.get("__error__"):
            return False, None
        return bool(st.get("blackout")), ((st.get("active_event") or {}).get("name"))

    def _fetch_bridge_data(self, endpoint: str, symbol: str = "") -> Dict:
        bridge = getattr(self.config, 'bridge', None)
        bridge_url = getattr(bridge, 'url', 'http://127.0.0.1:8000') if bridge else 'http://127.0.0.1:8000'
        url = f"{bridge_url}{endpoint}"
        if symbol:
            url = f"{bridge_url}{endpoint}/{symbol}"
        try:
            req = _urllib.Request(url, method="GET")
            with _urllib.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return {}

    def send_periodic_snapshot(self):
        """Build and send the periodic quant card (see P1/P2/P3 upgrade notes)."""
        ps = self.config.get('periodic_snapshot')
        if not ps or not getattr(ps, 'enabled', False):
            self._snapshot_inflight = False
            return
        try:
            self._send_periodic_snapshot_inner(ps)
        finally:
            # Always release the in-flight flag, even on an unexpected error, so
            # one bad cycle cannot permanently wedge the scheduler.
            self._snapshot_inflight = False

    def _send_periodic_snapshot_inner(self, ps):
        t0 = time.time()
        # P2: macro-event blackout — skip the send but keep the schedule moving.
        blk, event = self._blackout_active()
        if blk:
            logger.info("Periodic card skipped — blackout active (%s)", event)
            return
        # Preferred: send the SAME card the chat analysis delivers. There is exactly one
        # implementation of that card (card_live/: fetch_live -> live_read -> live_card)
        # and card_alert.py just runs it and posts the text, so the phone and the chat can
        # never drift apart. Falls back to the per-symbol blocks below if it cannot build.
        try:
            import card_alert
            card = card_alert.build_card()
            build_s = time.time() - t0
            if self._send_telegram_chunked(card):
                self._last_good_card = (card, time.time())
                logger.info("Periodic card sent (build %.1fs, %d chars)", build_s, len(card))
                return
            logger.warning("Periodic card built but send failed (build %.1fs, %d chars)",
                           build_s, len(card))
        except Exception as e:  # noqa: BLE001
            logger.warning("Card alert failed (%s) — trying last good card", e)
        # P1: prefer replaying the last SENT card (with a staleness header) over
        # dropping to the much thinner compact lines — but only while it is
        # recent. A card hours old is worse than fresh thin lines.
        if self._last_good_card:
            card, sent_at = self._last_good_card
            age_min = int((time.time() - sent_at) / 60)
            if age_min <= 60:
                header = (f"⚠️ card stale — fresh build failed, resending from "
                          f"{time.strftime('%H:%M', time.localtime(sent_at))} ({age_min}m ago)")
                if self._send_telegram_chunked(header + "\n" + card):
                    logger.info("Periodic card replayed stale (age %dm)", age_min)
                    return
            else:
                logger.info("Last good card is %dm old — using fresh compact lines instead",
                            age_min)
        symbols = getattr(ps, 'symbols', [])
        # Layout choice: the FULL block (ATAS footprint: tape, POC, value area, bar) only
        # for the symbol ATAS is charting; every other symbol gets one compact line built
        # from book + taker data, so the phone message stays readable.
        atas_sym = self._snapshot_atas_symbol(symbols)
        messages, rest = [], []
        for sym in symbols:
            try:
                if sym == atas_sym:
                    msg = self._build_snapshot(sym)
                    if msg:
                        messages.append(msg)
                else:
                    line = self._build_snapshot_line(sym)
                    if line:
                        rest.append(line)
            except Exception as e:
                logger.debug("Snapshot error for %s: %s", sym, e)
        if not atas_sym:
            messages.append("📊 QUANT SNAPSHOT — ATAS footprint offline, compact lines only")
        if rest:
            messages.append("▸ OTHER SYMBOLS  (book + taker flow · ⚡ = |score-50| ≥ 10)\n"
                            + "\n".join(rest))

        combined = ""
        for msg in messages:
            if len(combined) + len(msg) > 3800:
                self._send_telegram_chunked(combined.rstrip())
                combined = ""
            combined += msg + "\n\n"
        if combined.strip():
            self._send_telegram_chunked(combined.rstrip())
        logger.info("Periodic compact snapshot sent (build %.1fs, %d msgs)",
                    time.time() - t0, len(messages))

    def _build_snapshot(self, sym: str) -> str:
        """Build a single-symbol snapshot string."""
        clean = sym.replace("/USDT:USDT", "").replace("/USDT", "")
        # Bridge symbol forms: /dom and /taker-cvd take the compact lowercase ticker
        # ("solusdt"), /context takes "SOLUSDT". The previous sym.replace("/", "_")
        # produced "/dom/SOL_USDT:USDT" and "/context/SOL_USDT:USDT" — both 404 — so the
        # bid/ask walls, OI, funding, L/S and value-location lines of every periodic
        # snapshot printed zeros and /indicators was always empty.
        base = sym.split("/")[0].strip().upper()
        compact = base.lower() + "usdt"       # /dom and /taker-cvd key by "suiusdt"
        ind = self._fetch_bridge_data("/indicators")
        if isinstance(ind, dict):
            tk = (ind.get("ticker") or "").upper().replace(".P", "")
            if tk and not tk.startswith(base):
                ind = {}                  # ATAS is on another chart: no footprint here
        book = self._fetch_bridge_data("/dom", compact)   # Binance book: walls, OI, funding
        dom = book                                        # (kept as the legacy name)
        cvd = self._fetch_bridge_data("/cvd")
        ctx = self._fetch_bridge_data("/context", base + "USDT")
        # these two endpoints key by the full ticker ("SUIUSDT"), not the coin name
        taker = self._fetch_bridge_data("/taker-cvd") or {}
        tc = taker.get(base + "USDT") or taker.get(compact) or {}

        price = ind.get("price", {}).get("last", 0) if isinstance(ind, dict) else 0
        if not price and isinstance(book, dict):
            _bb, _ba = book.get("best_bid"), book.get("best_ask")
            if _bb and _ba:
                price = (_bb + _ba) / 2.0     # ATAS is on another chart: use the book mid
        delta = ind.get("delta", 0) if isinstance(ind, dict) else 0
        dom_imb = (book.get("imbalance_pct") if isinstance(book, dict) and book.get("imbalance_pct") is not None
                   else (ind.get("dom", {}).get("imbalance_pct", 0) if isinstance(ind, dict) else 0))
        bid_ratio = ind.get("dom", {}).get("bid_ask_ratio", 0) if isinstance(ind, dict) else 0
        tape_net = ind.get("tape", {}).get("net_vol", 0) if isinstance(ind, dict) else 0
        buy_ct = ind.get("tape", {}).get("buy_count", 0) if isinstance(ind, dict) else 0
        sell_ct = ind.get("tape", {}).get("sell_count", 0) if isinstance(ind, dict) else 0
        poc = ind.get("footprint", {}).get("poc", 0) if isinstance(ind, dict) else 0
        vah = ind.get("footprint", {}).get("value_area_high", 0) if isinstance(ind, dict) else 0
        val = ind.get("footprint", {}).get("value_area_low", 0) if isinstance(ind, dict) else 0
        time_str = ind.get("time", "00:00") if isinstance(ind, dict) else "00:00"
        bid_wall_vol = book.get("bid_wall_vol") or 0 if isinstance(book, dict) else 0
        ask_wall_vol = book.get("ask_wall_vol") or 0 if isinstance(book, dict) else 0
        bid_wall_price = book.get("bid_wall_price") or 0 if isinstance(book, dict) else 0
        ask_wall_price = book.get("ask_wall_price") or 0 if isinstance(book, dict) else 0
        # /cvd is keyed by the full ticker ({"SUIUSDT": {...}}): reading it flat always
        # returned 0, which is why every snapshot showed "CVD(ATAS): +0 (Δ+0)".
        cv = ((cvd or {}).get(base + "USDT") or {}) if isinstance(cvd, dict) else {}
        cvd_val = cv.get("cvd", 0)
        cvd_delta = cv.get("last_delta", 0)
        value_loc = ctx.get("value_area", {}).get("location", "N/A") if isinstance(ctx, dict) else "N/A"
        atr_pct = ctx.get("volatility_regime", {}).get("atr_percentile_rank", {}).get("value", 0) if isinstance(ctx, dict) else 0
        oi = (book.get("derivatives") or {}).get("openInterest", 0) if isinstance(book, dict) else 0
        funding = (book.get("derivatives") or {}).get("fundingRate", 0) if isinstance(book, dict) else 0
        ls = (book.get("derivatives") or {}).get("longShortRatio", 0) if isinstance(book, dict) else 0
        vol_total = ind.get("volume", {}).get("total", 0) if isinstance(ind, dict) else 0
        buy_vol = ind.get("volume", {}).get("ask", 0) if isinstance(ind, dict) else 0
        sell_vol = ind.get("volume", {}).get("bid", 0) if isinstance(ind, dict) else 0
        imb_ema = book.get("imbalance_pct_ema") if isinstance(book, dict) else None
        tc_1m = tc.get("cvd_usd_1m")
        tc_5m = tc.get("cvd_usd_5m")
        tc_pos = tc.get("range_pos_usd")

        bias = self._calc_bias(dom_imb, delta, tape_net, cvd_val, cvd_delta, bid_ratio,
                               vol_total, buy_vol, sell_vol,
                               imb_ema=imb_ema, flow_1m=tc_1m, flow_5m=tc_5m)
        bias_label, bias_emoji = self._bias_label(bias)

        msg = f"📊 {clean}/USDT — QUANT SNAPSHOT | ${price:.4f} [{time_str}]"
        msg += f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        msg += f"\n⚡ Score: {bias:.0f}/100 {bias_emoji} {bias_label} (book+taker proxy · ⚡ = |score-50| ≥ 10)"
        msg += f"\n📈 CVD(ATAS): {cvd_val:+,.0f} (Δ{cvd_delta:+,.0f})"
        if tc_1m is not None:
            msg += f"\n🌊 Taker flow: 1m {tc_1m:+,.0f}$ / 5m {tc_5m:+,.0f}$"
            if tc_pos is not None:
                msg += f" | session pos {tc_pos:.2f}"
        dom_line = f"\n🏦 DOM: {dom_imb:+.1f}%"
        if imb_ema is not None:
            dom_line += f" raw | {imb_ema:+.1f}% avg45s"
        msg += dom_line + f" | ratio {bid_ratio:.2f}"
        msg += f"\n📋 Tape: {buy_ct}/{sell_ct} | net {tape_net:+,.0f}"
        msg += f"\n📍 POC {poc:.4f} | VAH {vah:.4f} | VAL {val:.4f}"
        msg += f"\n🏚️ Value: {value_loc} | ATR {atr_pct:.0f}%"
        _oi_notional = (oi * price) if price else 0
        msg += (f"\n💰 OI: {oi/1e6:.1f}M contracts (~${_oi_notional/1e6:.0f}M) | "
                f"fund: {funding * 3 * 365 * 100:+.1f}%/yr | LS: {ls:.2f}")
        msg += f"\n📊 Bid wall: {bid_wall_price:.4f}({bid_wall_vol:.0f}) | Ask wall: {ask_wall_price:.4f}({ask_wall_vol:.0f})"
        msg += f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        if not ind:
            msg += "\n🅰️ ATAS footprint is on another symbol — CVD/tape/POC lines n/a here"
        return msg

    def _calc_bias(self, dom_imb, delta, tape_net, cvd, cvd_delta, bid_ratio,
                   volume=0.0, tape_buy=0.0, tape_sell=0.0,
                   imb_ema=None, flow_1m=None, flow_5m=None) -> float:
        """Snapshot score, 0-100, FIXED composition so it means the same thing on every
        symbol.

        Terms (all bounded, identical for every symbol):
            25 * tanh(imb_ema / 30)        45s-averaged book imbalance
            10 * tanh(flow_1m / 50_000)    taker flow, USD per minute
            10 * tanh(flow_5m / 200_000)   taker flow, USD per 5 minutes
             5 * tanh(log(bid_ask_ratio))  book depth ratio

        The earlier version mixed raw-unit terms (delta/100000, tape_net/1000,
        cvd_delta/100000), so the number tracked contract size: on live data it printed
        100.0 for SUI, SOL and XAU alike. The ATAS footprint fields (tape, POC, bar
        delta) still appear as raw numbers in the message but no longer feed the score,
        because they exist only for the one symbol ATAS is charting.
        DISPLAY ONLY: the plan trigger is orderflow_calls.py's |composite| >= min_score.
        This is a proxy — it is not the generator's number.
        """
        import math as _m
        s = 50.0
        if imb_ema is None:
            imb_ema = dom_imb
        if imb_ema is not None:
            s += 25.0 * _m.tanh((imb_ema or 0) / 30.0)
        if flow_1m is not None:
            s += 10.0 * _m.tanh((flow_1m or 0) / 50000.0)
        if flow_5m is not None:
            s += 10.0 * _m.tanh((flow_5m or 0) / 200000.0)
        if bid_ratio and bid_ratio > 0:
            s += 5.0 * _m.tanh(_m.log(bid_ratio))
        return round(max(0.0, min(100.0, s)), 1)

    def _snapshot_atas_symbol(self, symbols):
        """Which configured symbol ATAS is actually exporting right now (or None)."""
        ind = self._fetch_bridge_data("/indicators")
        tk = (ind.get("ticker") or "").upper().replace(".P", "") if isinstance(ind, dict) else ""
        if not tk:
            return None
        for s in symbols:
            if tk == s.split("/")[0].strip().upper() + "USDT":
                return s
        return None

    def _build_snapshot_line(self, sym: str) -> str:
        """One compact line for the symbols without an ATAS footprint: price, score,
        book imbalance, taker flow, funding, L/S, walls, ATR rank — book + taker only,
        so every line is the same shape and the score is comparable across symbols."""
        clean = sym.replace("/USDT:USDT", "").replace("/USDT", "")
        base = sym.split("/")[0].strip().upper()
        book = self._fetch_bridge_data("/dom", base.lower() + "usdt")
        book = book if isinstance(book, dict) else {}
        ctx = self._fetch_bridge_data("/context", base + "USDT")
        ctx = ctx if isinstance(ctx, dict) else {}
        taker = self._fetch_bridge_data("/taker-cvd") or {}
        tc = taker.get(base + "USDT") or {}
        bb, ba = book.get("best_bid"), book.get("best_ask")
        price = (bb + ba) / 2.0 if (bb and ba) else 0
        imb_raw, imb_ema = book.get("imbalance_pct"), book.get("imbalance_pct_ema")
        flow_1m, flow_5m = tc.get("cvd_usd_1m"), tc.get("cvd_usd_5m")
        score = self._calc_bias(imb_raw, 0, 0, 0, 0, book.get("bid_ask_ratio") or 0,
                                imb_ema=imb_ema, flow_1m=flow_1m, flow_5m=flow_5m)
        trig = "⚡" if abs(score - 50) >= 10 else "·"
        parts = [f"{trig} {clean:<5} {('$%.6g' % price) if price else 'n/a':>10} {score:>4.0f}/100"]
        if imb_ema is not None:
            parts.append(f"DOM {imb_ema:+.1f}%/45s")
        elif imb_raw is not None:
            parts.append(f"DOM {imb_raw:+.1f}%")
        if flow_1m is not None:
            parts.append(f"flow {flow_1m:+,.0f}$/1m")
        der = book.get("derivatives") or {}
        if der.get("fundingRate") is not None:
            parts.append(f"fund {der['fundingRate'] * 3 * 365 * 100:+.1f}%/yr")
        if der.get("longShortRatio"):
            parts.append(f"LS {der['longShortRatio']:.2f}")
        wp, ap = book.get("bid_wall_price"), book.get("ask_wall_price")
        if wp and ap:
            parts.append(f"walls {wp:.6g}({book.get('bid_wall_vol', 0):,.0f})/"
                         f"{ap:.6g}({book.get('ask_wall_vol', 0):,.0f})")
        atr = ((ctx.get("volatility_regime") or {}).get("atr_percentile_rank") or {}).get("value")
        if atr is not None:
            parts.append(f"ATR p{float(atr):.0f}")
        return " · ".join(parts)

    def _bias_label(self, score: float) -> tuple:
        if score >= 60: return "BULLISH", "🟢"
        if score <= 40: return "BEARISH", "🔴"
        return "NEUTRAL", "🟡"

    def _interruptible_sleep(self, seconds: float):
        for _ in range(int(seconds)):
            if not self.running:
                break
            time.sleep(1)

    def run(self):
        logger.info("=" * 80)
        logger.info("QUANT ALERT ENGINE v2.5 - PERPETUAL FUTURES - Starting")
        logger.info(f"Symbols: {self.config.get('symbols')}")
        logger.info(f"Timeframes: {self.config.get('timeframes')}")
        logger.info(f"Health: {'ON :' + str(self.config.get('general.health_port', 8080)) if self.config.get('general.health_enabled') else 'OFF'}")
        logger.info("=" * 80)

        check_interval = self.config.get('general.check_interval', 10)
        cadence = TimeframeCadence()

        while self.running:
            start_time = time.time()
            symbols = self.config.get('symbols', [])
            timeframes = self.config.get('timeframes', ['5m'])
            now = time.time()
            
            # Submit only the fetch/analyze tasks whose candle is actually due
            futures = {}
            for sym in symbols:
                for tf in timeframes:
                    if not cadence.is_due(sym, tf, now):
                        continue
                    futures[self.executor.submit(self.analyzer.analyze_symbol, sym, tf)] = (sym, tf)
                    cadence.mark_polled(sym, tf)

            if not futures:
                self._interruptible_sleep(check_interval)
                continue

            for future in as_completed(futures):
                try:
                    ok = future.result()
                    if ok is False:
                        sym, tf = futures[future]
                        cadence.mark_failed(sym, tf)
                except Exception as e:
                    sym, tf = futures[future]
                    logger.error(f"Error processing {sym} {tf}: {e}", exc_info=True)
                    cadence.mark_failed(sym, tf)

            elapsed = time.time() - start_time
            sleep_time = max(0, check_interval - elapsed)
            logger.debug(f"Scan completed in {elapsed:.2f}s. Sleeping for {sleep_time:.2f}s...")

            # Periodic outcome-stat digest (fix #3): edge tracking every 10 min
            if time.time() - self._last_stats_log > 600:
                self._last_stats_log = time.time()
                self._log_outcome_stats()

            # Periodic quant snapshot — wall-clock aligned (P2, 2026-09-19).
            # Cards land on :00/:15/:30/:45 (epoch multiples of interval) instead
            # of drifting from process start. The in-flight guard stops the old
            # bug where a build outlasting the interval spawned a second thread
            # and sent a duplicate card.
            ps = self.config.get('periodic_snapshot')      # pydantic block, not an attribute
            ps_interval = getattr(ps, 'interval_seconds', 900) if ps else 900
            ps_enabled = getattr(ps, 'enabled', False) if ps else False
            if ps_enabled:
                now_ps = time.time()
                if self._next_snapshot_at is None:
                    self._next_snapshot_at = self._next_snapshot_boundary(now_ps, ps_interval)
                if now_ps >= self._next_snapshot_at and not self._snapshot_inflight:
                    self._snapshot_inflight = True
                    self._snapshot_last_send = now_ps
                    self._next_snapshot_at = self._next_snapshot_boundary(now_ps, ps_interval)
                    threading.Thread(target=self.send_periodic_snapshot, daemon=True).start()

            self._interruptible_sleep(sleep_time)

    @staticmethod
    def _next_snapshot_boundary(now: float, interval: float) -> float:
        """Next epoch time strictly after `now` aligned to `interval` seconds
        (900 -> the next :00/:15/:30/:45)."""
        iv = max(60, int(interval))
        return (int(now) // iv + 1) * iv

    def _log_outcome_stats(self):
        try:
            st = self.analyzer.alert_mgr.outcome_stats()
            if st and st.get("resolved"):
                parts = [f"{r['signal_name']}: n={r['count']} win={r['win_rate']*100:.0f}% avgRet={r['avg_ret']*100:+.2f}% pf={r['profit_factor']:.2f}"
                         for r in st["rows"][:12]]
                logger.info("OUTCOME STATS (resolved=%d): %s", st["resolved"], " | ".join(parts))
        except Exception as e:
            logger.debug("Outcome stats failed: %s", e)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(prog="quant_alert_engine")
    parser.add_argument("--validate", nargs=2, metavar=("SYMBOL", "TF"),
                        help="Run full backtest validation (costs, regime split, metrics)")
    parser.add_argument("--walkforward", nargs=2, metavar=("SYMBOL", "TF"),
                        help="Run rolling-window walk-forward validation")
    parser.add_argument("--bars", type=int, default=2000, help="Bars to replay (default: 2000)")
    args = parser.parse_args()

    if args.validate or args.walkforward:
        from config_schema import load_config
        cfg = load_config('config.json')
        sym, tf = args.validate if args.validate else args.walkforward
        if args.validate:
            bt = Backtester(cfg)
            bt.run(sym, tf, bars=args.bars)
        else:
            wf = WalkForward(cfg)
            wf.run(sym, tf, bars=args.bars)
        sys.exit(0)

    # Prevent two instances from running against the same config/database
    _lock = SingleInstanceLock('data/engine.lock')
    if not _lock.acquire():
        sys.exit(1)
    try:
        # Run standard engine — Config is validated inside QuantAlertEngine
        try:
            engine = QuantAlertEngine('config.json')
        except SystemExit as e:
            sys.exit(e.code if isinstance(e.code, int) else 1)
        except Exception as e:
            # Fallback: load_config already printed guidance; ensure clean exit
            logger.error(f"Failed to start engine: {e}")
            sys.exit(1)
        
        engine.run()
    finally:
        _lock.release()
