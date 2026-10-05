"""
Quant Alert Engine Pro — PyQt6 Desktop UI v2.5
QSplitter-based layout guarantees 12+ visible alert rows.
Fixes: Pydantic config, SQLite dedup unification, configurable bridge, debug-level TG skips, structured logging, thread-safe UI signals.
"""

import os, sys, json, queue, re, threading, time as _time, logging
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Callable, Any
from pathlib import Path

import requests

try:
    import winsound
except ImportError:
    winsound = None

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QSplitter, QFrame, QScrollArea, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QColor

sys.path.insert(0, str(Path(__file__).parent))
from config_schema import Config, load_config
from quant_alert_engine import AlertManager, SignalAnalyzer, TimeframeCadence
from logging_setup import setup_logging

# Ensure log dir exists and init structured logging (UI shares same log file)
os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)
try:
    setup_logging(log_file="logs/quant_alert_engine.log", level="INFO", json_format=True)
except Exception:
    logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── PALETTE ──
BG          = "#0b0e11"
PANEL       = "#161a1f"
ELEVATED    = "#1e2329"
RAISED      = "#2a3038"
BORDER      = "#2a3038"
BORDER_STR  = "#3a4149"
TEXT        = "#eaecef"
TEXT_DIM    = "#b7bdc6"
TEXT_MUTED  = "#848e9c"
TEXT_FAINT  = "#5e6673"
BULL        = "#02c076"
BEAR        = "#f6465d"
NEUTRAL     = "#fcd535"
INFO        = "#1e88e5"
BRAND       = "#fcd535"

CAT_COLORS = {
    "VOLUME":    INFO,
    "CVD":       "#9b59b6",
    "CVD_DIV":   "#9b59b6",
    "RSI":       "#e67e22",
    "RSI_DIV":   "#e67e22",
    "MACD_DIV":  "#16a085",
    "VOL":       TEXT_MUTED,
}

# Alert-feed column indices (single source of truth — avoids magic numbers).
COL_TIME, COL_SYMBOL, COL_TF, COL_TYPE, COL_SIGNAL, COL_PRICE, COL_TG = range(7)
COL_COLORED = (COL_TYPE, COL_SIGNAL, COL_PRICE)   # tinted by signal direction
COL_BOLD = (COL_TYPE, COL_SIGNAL)


def _short_pair(symbol: str) -> str:
    """'SUI/USDT:USDT' -> 'SUI/USDT' for the feed's SYMBOL column."""
    return symbol.replace(":USDT", "").replace(":USD", "")

FONT_FAMILY = "Segoe UI"

QSS = """
QMainWindow { background-color: %s; }
QWidget { color: %s; font-family: '%s', Arial, sans-serif; font-size: 11px; }
QFrame#Panel { background-color: %s; border: 1px solid %s; border-radius: 6px; }
QFrame#Sidebar { background-color: #12161a; border-right: 1px solid %s; }
QFrame#HeroCard { background-color: %s; border: 1px solid %s; border-radius: 8px; }
QFrame#TickerCard { background-color: %s; border: 1px solid %s; border-radius: 6px; }
QFrame#Pill { background-color: %s; border-radius: 4px; border: none; }
QPushButton { background-color: %s; border: 1px solid %s; color: %s;
  border-radius: 4px; padding: 4px 10px; font-weight: bold; font-size: 10px; }
QPushButton:hover { background-color: #303742; }
QPushButton:checked { background-color: %s; color: %s; border: 1px solid %s; }
QTableWidget { background-color: %s; border: none; gridline-color: #23282e; }
QHeaderView::section { background-color: %s; color: %s; padding: 4px 6px;
  border: none; border-bottom: 1px solid %s; font-size: 10px; font-weight: bold; }
QTableWidget::item { border-bottom: 1px solid #1f242a; padding: 3px 5px; }
QScrollBar:vertical { background-color: #12161a; width: 6px; margin: 0; }
QScrollBar::handle:vertical { background-color: %s; min-height: 20px; border-radius: 3px; }
QScrollBar::handle:vertical:hover { background-color: #475261; }
QSplitter::handle { background-color: %s; height: 1px; }
""" % (BG, TEXT, FONT_FAMILY, PANEL, BORDER, BORDER, PANEL, BORDER,
       ELEVATED, BORDER, ELEVATED, RAISED, BORDER_STR, TEXT,
       NEUTRAL, BG, NEUTRAL, PANEL, PANEL, TEXT_MUTED, BORDER,
       BORDER, BORDER)

# ── Helpers ──
def _rsi_color(v):
    if v is None: return TEXT
    if v >= 70: return BEAR
    if v <= 30: return BULL
    return TEXT

def _macd_hist_color(v):
    if v is None: return TEXT
    if v > 0: return BULL
    if v < 0: return BEAR
    return TEXT

def _price_color(curr, prev):
    if curr is None or prev is None: return TEXT
    if curr > prev: return BULL
    if curr < prev: return BEAR
    return TEXT

def _change_color(pct):
    if pct is None: return TEXT_MUTED
    if pct > 0: return BULL
    if pct < 0: return BEAR
    return TEXT_MUTED

def _price_decimals(price):
    if price is None: return 2
    s = f"{price:.10f}"
    if s.find("e") != -1: return 2
    if abs(price) >= 100: return 2
    if abs(price) >= 1: return 3
    if abs(price) >= 0.01: return 5
    return 8

def _fmt_price(p):
    if p is None: return "—"
    return f"{p:.{_price_decimals(p)}f}"

def _quote_of(symbol: str) -> str:
    base, sep, rest = symbol.partition("/")
    if not sep:
        return "USDT"
    return rest.split(":")[0] if rest else "USDT"


def _md_escape(s: Any) -> str:
    """Escape Telegram Markdown metacharacters in free-text fields."""
    return re.sub(r"([_*\[\]()])", r"\\\1", str(s))


# Phase 3 (audit 2026-09-17): bounded fire-and-forget sender pool. Unbounded
# thread-per-alert risks handle exhaustion under alert storms, and daemon
# threads vanish on exit mid-send. Cap at 8 concurrent; overflow DROPS with a
# warning (the UI row then honestly stays pending instead of lying sent).
_SEND_SEM = threading.BoundedSemaphore(8)


def _spawn_sender(fn, *args, **kwargs):
    def _wrapped():
        try:
            fn(*args, **kwargs)
        finally:
            _SEND_SEM.release()
    if _SEND_SEM.acquire(blocking=False):
        threading.Thread(target=_wrapped, daemon=True).start()
    else:
        logger.warning("sender pool saturated (8) — dropping background send")


# ============================================================================
# TELEGRAM NOTIFIER
# ============================================================================
logger = logging.getLogger(__name__)
_LOG = logger

class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.enabled = False
        self._last_error_ts = 0.0
        self._filter_symbol: Optional[str] = None
        self._filter_timeframe: Optional[str] = None
        self._base = f"https://api.telegram.org/bot{token}"

    def set_enabled(self, enabled: bool):
        self.enabled = enabled

    def set_filter(self, symbol: Optional[str] = None, timeframe: Optional[str] = None):
        self._filter_symbol = symbol
        self._filter_timeframe = timeframe

    def _passes_filter(self, symbol: str, timeframe: str) -> bool:
        short = symbol.split("/")[0].split(":")[0]
        if self._filter_symbol and short != self._filter_symbol:
            return False
        if self._filter_timeframe and timeframe != self._filter_timeframe:
            return False
        return True

    def send_alert(self, signal_name: str, symbol: str, price: float,
                   signal_type: str, timeframe: str = "", color: str = "info",
                   market_data: Optional[Dict[str, Any]] = None,
                   on_result: Optional[Callable[[bool], None]] = None,
                   max_retries: int = 3):
        if not self.enabled:
            logger.debug("TG skipped: not enabled (%s %s)", symbol, timeframe)
            if on_result:
                try:
                    on_result(False)
                except Exception:
                    pass
            return
        if not self._passes_filter(symbol, timeframe):
            logger.debug("TG skipped: filtered out %s %s", symbol, timeframe)
            if on_result:
                try:
                    on_result(False)
                except Exception:
                    pass
            return
        now = _time.time()
        if now - self._last_error_ts < 30:
            return

        def _report(ok: bool):
            if on_result:
                try:
                    on_result(ok)
                except Exception:
                    logger.debug("TG on_result callback failed", exc_info=True)

        def _do_send():
            try:
                emoji_map = {"green": "\U0001f7e2", "red": "\U0001f534", "yellow": "\U0001f7e1", "info": "\u26aa"}
                emoji = emoji_map.get(color, "\u26aa")
                direction_map = {"green": "BULLISH", "red": "BEARISH", "yellow": "WARNING", "info": "INFO"}
                direction = direction_map.get(color, "INFO")
                msg = (
                    f"{emoji} *{_md_escape(symbol)}* [{_md_escape(timeframe)}]\n"
                    f"{direction}\n"
                    f"`{signal_type}` {_md_escape(signal_name)}\n"
                    f"Price: `{price:.4f}`"
                )
                # Enrich with fresh market data so every alert is unique & informative
                md_parts = []
                if market_data:
                    if market_data.get("rsi") is not None:
                        md_parts.append(f"*RSI:* `{market_data['rsi']:.1f}`")
                    if market_data.get("cvd") is not None:
                        md_parts.append(f"*CVD:* `{market_data['cvd']:+,.0f}`")
                    if market_data.get("delta") is not None:
                        md_parts.append(f"*Delta:* `{market_data['delta']:+,.0f}`")
                    if market_data.get("volume") is not None and market_data.get("vol_avg"):
                        ratio = market_data['volume'] / market_data['vol_avg']
                        md_parts.append(f"*Vol:* `{ratio:.1f}x`")
                    if market_data.get("atr") is not None:
                        md_parts.append(f"*ATR:* `{market_data['atr']:.4f}`")
                    if market_data.get("bull_conf") is not None and market_data.get("bear_conf") is not None:
                        md_parts.append(f"*Confluence:* B={market_data['bull_conf']} S={market_data['bear_conf']}")
                    if market_data.get("regime") is not None:
                        reg = market_data['regime']
                        reg_str = "UP" if reg > 0 else ("DOWN" if reg < 0 else "NEU")
                        md_parts.append(f"*Regime:* `{reg_str}`")
                    # External macro overlay (v2.7): one compact summary line
                    macro = market_data.get("macro")
                    if isinstance(macro, dict) and not macro.get("stale", True):
                        mparts = []
                        if macro.get("price_usd") is not None:
                            mparts.append(f"${macro['price_usd']:,.2f}")
                        if macro.get("chg_24h") is not None:
                            mparts.append(f"{macro['chg_24h']:+.2f}% 24h")
                        if macro.get("funding_rate") is not None:
                            mparts.append(f"fund {macro['funding_rate']:+.4%}")
                        if macro.get("poly_yes") is not None:
                            mparts.append(f"Poly YES {macro['poly_yes']:.0%}")
                        if macro.get("regime_hint") and macro["regime_hint"] != "neutral":
                            mparts.append(f"macro {macro['regime_hint'].upper()}")
                        if mparts:
                            md_parts.append("*Macro:* " + " · ".join(mparts))
                    if md_parts:
                        msg += "\n" + "\n".join(md_parts)
                # Retry with exponential backoff; HTTP errors back off too
                # (a 429/5xx without backoff risks a Telegram ban).
                sent = False
                delay = 2.0
                for attempt in range(max(1, max_retries)):
                    try:
                        resp = requests.post(
                            f"{self._base}/sendMessage",
                            json={"chat_id": self.chat_id, "text": msg, "parse_mode": "Markdown"},
                            timeout=10,
                        )
                    except Exception as e:
                        logger.warning("Telegram error (attempt %d): %s", attempt + 1, e)
                    else:
                        if resp.ok:
                            logger.info("TG sent: %s %s %s", symbol, signal_name, timeframe)
                            sent = True
                            break
                        logger.warning("Telegram send failed (attempt %d): %s",
                                       attempt + 1, resp.text)
                    _time.sleep(delay)
                    delay *= 2.0
                if not sent:
                    self._last_error_ts = _time.time()
                _report(sent)
            except Exception as e:
                logger.warning("Telegram error: %s", e)
                self._last_error_ts = _time.time()
                _report(False)

        _spawn_sender(_do_send)


# ============================================================================
# QUEUE ALERT MANAGER — Unified SQLite dedup (fixes P1 split-brain)
# ============================================================================
class QueueAlertManager(AlertManager):
    def __init__(self, config: Config, alert_queue: queue.Queue,
                 telegram_bot: Optional[TelegramNotifier] = None):
        super().__init__(config)
        self.alert_queue = alert_queue
        self.telegram_bot = telegram_bot
        # Keep in-memory cache for fast path, but SQLite is source of truth
        self._mem_cache: Dict[str, float] = {}
        self.max_alert_history = 5000
        # Toast-coalescing state (engine thread only touches it under _toast_lock)
        self._toast_lock = threading.Lock()
        self._last_toast_ts = 0.0
        self._toast_pending: List[Dict[str, Any]] = []

    def emit_alert(self, signal_name: str, symbol: str, price: float,
                   signal_type: str = "SIGNAL", color: str = "info",
                   bar_key: str = "", timeframe: str = "",
                   market_data: Optional[Dict[str, Any]] = None,
                   direction: int = 0):
        signal_id = f"{symbol}_{signal_name}_{timeframe}_{bar_key}"
        # Fast in-memory check
        if signal_id in self._mem_cache:
            return
        # SQLite dedup — survives restarts, shared with headless engine
        if self._is_fired(signal_id):
            self._mem_cache[signal_id] = _time.time()
            return
        # Prune in-memory cache
        if len(self._mem_cache) > self.max_alert_history:
            cutoff = _time.time() - 3600
            self._mem_cache = {k: v for k, v in self._mem_cache.items() if v > cutoff}
        self._mem_cache[signal_id] = _time.time()
        self._mark_fired(signal_id)

        # Outcome tagging (quant audit fix #3): track forward return of every signal.
        # v2.8: the feature vector and the emit-time direction must travel with it,
        # or the UI path (the one that actually runs) writes no training data.
        self.tag_signal(signal_name, symbol, price, timeframe, bar_key, signal_type,
                        self.config.get('general.outcome_forward_bars', 12),
                        features=(market_data or {}).get("features"),
                        direction=direction)

        msg = f"[{signal_type}] {symbol} - {signal_name} @ {price:.4f}"
        if market_data and market_data.get("ml_prob") is not None:
            msg += f" | ml={market_data['ml_prob']:.2f}"
        logger.info(msg, extra={"event_type": "alert_fired", "symbol": symbol, "timeframe": timeframe, "signal_name": signal_name, "signal_type": signal_type, "price": price, "color": color, "signal_id": signal_id, "market_data": market_data or {}})
        if self.config.get('general.enable_notifications', True):
            self._toast_coalesced(symbol, signal_name, price, color)

        self.alert_queue.put({
            "kind": "alert",
            "timestamp": datetime.now(),
            "symbol": symbol,
            "signal_type": signal_type,
            "signal_name": signal_name,
            "price": price,
            "color": color,
            "timeframe": timeframe,
            "market_data": market_data or {},
            "signal_id": signal_id,
            "tg_status": "pending",
        })

        if self.telegram_bot:
            def _on_tg(ok: bool, _sid=signal_id):
                try:
                    self.alert_queue.put({"kind": "delivery", "signal_id": _sid,
                                          "ok": bool(ok), "timestamp": datetime.now()})
                except Exception:
                    pass
            self.telegram_bot.send_alert(signal_name, symbol, price, signal_type, timeframe, color, market_data=market_data, on_result=_on_tg)
        else:
            self.alert_queue.put({"kind": "delivery", "signal_id": signal_id,
                                  "ok": False, "timestamp": datetime.now()})

        # Forward to bridge agent — configurable via config.json/.env (Fix M8: async dispatch)
        if self.config.get('bridge.enabled', True):
            def _post_bridge():
                try:
                    bridge_url = self.config.get('bridge.url', 'http://127.0.0.1:8000/quant-alert')
                    bridge_timeout = self.config.get('bridge.timeout_seconds', 2.0)
                    import os as _os
                    _bt = _os.getenv("BRIDGE_TOKEN", "")
                    _headers = {"X-Bridge-Token": _bt} if _bt else {}
                    requests.post(
                        bridge_url,
                        json={
                            "symbol": symbol,
                            "signal_type": signal_type,
                            "signal_name": signal_name,
                            "price": price,
                            "timeframe": timeframe,
                            "color": color,
                            "market_data": market_data or {},
                        },
                        headers=_headers,
                        timeout=bridge_timeout,
                    )
                except Exception:
                    logger.debug("Bridge forward failed (normal if bridge is off) for %s %s", symbol, signal_name)

            _spawn_sender(_post_bridge)

    # Toast coalescing (fix: every alert fired its own 10s Windows toast with
    # no cooldown, so bursts flashed and stacked on the desktop). At most one
    # toast per _TOAST_COOLDOWN_S; alerts inside the window collapse into a
    # single digest. Sound follows displayed toasts only — no beep storms.
    _TOAST_COOLDOWN_S = 8.0
    _TOAST_DURATION_S = 5

    def _toast_coalesced(self, symbol: str, signal_name: str, price: float, color: str):
        """Show at most one Windows toast per cooldown window.

        Alerts arriving inside the window are accumulated; when the window
        lapses the next alert flushes them as ONE digest toast. A lone alert
        shows immediately. Sound plays only for displayed toasts.
        """
        with self._toast_lock:
            now = _time.time()
            self._toast_pending.append(
                {"symbol": symbol, "signal": signal_name, "price": price})
            if now - self._last_toast_ts < self._TOAST_COOLDOWN_S:
                return
            batch = self._toast_pending
            self._toast_pending = []
            self._last_toast_ts = now
        if len(batch) == 1:
            b = batch[0]
            text = f"{b['symbol']} {b['signal']} @ {b['price']:.4f}"
        else:
            b = batch[-1]
            text = (f"{len(batch)} alerts — latest {b['symbol']} {b['signal']} "
                    f"@ {b['price']:.4f}")
        try:
            self.notifier.show_toast("Quant Alert Engine", text,
                                     duration=self._TOAST_DURATION_S, threaded=True)
        except Exception:
            pass
        if self.config.get('general.enable_sound', True) and winsound:
            try:
                winsound.PlaySound('SystemExclamation', winsound.SND_ASYNC)
            except Exception:
                pass


    def publish_snapshot(self, symbol: str, price: float,
                         indicators: Dict[str, Optional[float]],
                         bar_timestamp: Optional[str] = None):
        self.alert_queue.put({
            "kind": "snapshot",
            "timestamp": datetime.now(),
            "symbol": symbol,
            "price": price,
            "indicators": indicators,
            "bar_timestamp": bar_timestamp,
        })


# ============================================================================
# ENGINE THREAD
# ============================================================================
class EngineThread(QThread):
    status_signal = pyqtSignal(str, str)

    def __init__(self, config_path: str, alert_queue: queue.Queue,
                 status_callback: Optional[Callable] = None):
        super().__init__()
        self.config_path = config_path
        self.alert_queue = alert_queue
        if status_callback:
            # Explicit queued connection: run() executes on the worker thread,
            # so the slot must run on the GUI thread. AutoConnection would do
            # the same here, but implicitness is what made the direct-call
            # mistake possible before — be explicit.
            self.status_signal.connect(
                status_callback, Qt.ConnectionType.QueuedConnection)
        self.running = False
        self.connected = False
        # Set by stop() so an in-progress loop sleep returns immediately instead
        # of holding the process open for up to `check_interval` seconds.
        self._stop_event = threading.Event()
        try:
            self.config_obj = load_config(config_path)
        except SystemExit:
            raise
        except Exception as e:
            # load_config already printed guidance; surface as status callback if available, then re-raise
            logger.error(f"[CONFIG ERROR] {e}")
            # Defer error display to run() via status_signal if caller didn't catch
            self.config_obj = None  # type: ignore
            self._init_error = e  # type: ignore
            return
        self.telegram_bot: Optional[TelegramNotifier] = None
        self.alert_mgr: Optional[QueueAlertManager] = None
        self._last_stats_log = _time.time()

    def stop(self, timeout: float = 8.0) -> bool:
        """Stop the scan loop and WAIT for the QThread to actually finish.

        The old version only set `self.running = False` and returned. The thread
        was usually parked in `_time.sleep(check_interval)`, so it outlived the
        window: `app.exec()` returned, CPython started shutting down with a live
        QThread in it, and the process stayed running with no window on screen —
        the "won't close" bug. Waiting here (plus the interruptible sleep below)
        is what makes the close deterministic.

        Returns True when the thread is really gone.
        """
        self.running = False
        self._stop_event.set()
        if not self.isRunning():
            return True
        if self.wait(int(timeout * 1000)):
            return True
        # Last resort at shutdown: a thread wedged in a blocking network call.
        # terminate() is unsafe in general but leaving the process alive is worse.
        logger.warning("Engine thread did not stop within %.0fs — terminating it", timeout)
        self.terminate()
        self.wait(2000)
        return not self.isRunning()

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep that returns as soon as stop() is called. False = time to exit."""
        self._stop_event.wait(seconds)
        return self.running

    def set_telegram_filter(self, symbol: Optional[str] = None,
                            timeframe: Optional[str] = None):
        if self.telegram_bot:
            self.telegram_bot.set_filter(symbol, timeframe)
        # Also sync filter to bridge agent — configurable
        try:
            if not self.config_obj.get('bridge.enabled', True):
                return
            bridge_filter_url = self.config_obj.get('bridge.filter_url', 'http://127.0.0.1:8000/quant/filter')
            bridge_timeout = self.config_obj.get('bridge.timeout_seconds', 2.0)
            sf = symbol or ""
            tf = timeframe or ""
            import json as _j, urllib.request as _ur
            payload = _j.dumps({"symbol": sf, "timeframe": tf}).encode()
            req = _ur.Request(
                bridge_filter_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            _ur.urlopen(req, timeout=bridge_timeout)
        except Exception:
            pass

    def run(self):
        self.running = True
        self._stop_event.clear()
        # If __init__ failed to load config, report cleanly and stop (fix #8)
        if getattr(self, 'config_obj', None) is None or hasattr(self, '_init_error'):
            err = getattr(self, '_init_error', 'config load failed')
            self.status_signal.emit("error", f"Config error: {err}"[:80])
            logger.error(f"EngineThread not started due to config error: {err}")
            return
        token = self.config_obj.get("telegram.token", "")
        chat_id = self.config_obj.get("telegram.chat_id", "")
        if token and chat_id:
            self.telegram_bot = TelegramNotifier(token, chat_id)
            enabled = self.config_obj.get("telegram.enabled", False)
            self.telegram_bot.set_enabled(enabled)

        self.alert_mgr = QueueAlertManager(
            self.config_obj, self.alert_queue, self.telegram_bot
        )

        analyzer = SignalAnalyzer(self.config_obj, alert_mgr=self.alert_mgr)

        check_interval = self.config_obj.get("general.check_interval", 5)
        symbols = self.config_obj.get("symbols", [])
        timeframes = self.config_obj.get("timeframes", ["5m"])

        exc_ok = analyzer.exchange is not None
        self.connected = exc_ok
        if exc_ok:
            self.status_signal.emit("connected", "●  Live — Binance")
        else:
            self.status_signal.emit("error", "Failed to connect")

        # Health server (mirror headless QuantAlertEngine init — fix: UI path
        # never started it, so /health on :8080 was dead in UI mode)
        self._health_thread = None
        if self.config_obj.get('general.health_enabled', True):
            try:
                from health_server import register_check, start_health_server
                register_check("exchange", lambda: {
                    "status": "healthy" if getattr(analyzer, 'exchange', None) else "unhealthy",
                    "metrics": {"connected": 1 if getattr(analyzer, 'exchange', None) else 0}
                })
                register_check("database", lambda: {
                    "status": "healthy",
                    "metrics": {"symbols_configured": len(self.config_obj.get('symbols', []))}
                })
                register_check("symbols", lambda: {
                    "status": "healthy" if self.config_obj.get('symbols') else "degraded",
                    "metrics": {"symbols_configured": len(self.config_obj.get('symbols', []))}
                })
                register_check("macro_regime", lambda: analyzer.macro_health() if hasattr(analyzer, 'macro_health') else {"status": "healthy", "metrics": {}})
                register_check("outcome_gating", lambda: analyzer.gating_health() if hasattr(analyzer, 'gating_health') else {"status": "healthy", "metrics": {}})
                register_check("time_gating", lambda: analyzer.time_health() if hasattr(analyzer, 'time_health') else {"status": "healthy", "metrics": {}})
                register_check("blackout", lambda: analyzer.blackout_health() if hasattr(analyzer, 'blackout_health') else {"status": "healthy", "metrics": {"enabled": False}})
                register_check("level2", lambda: analyzer.level2_health() if hasattr(analyzer, 'level2_health') else {"status": "healthy", "metrics": {"enabled": False}})
                register_check("macro_context", lambda: analyzer.macro_context_health() if hasattr(analyzer, 'macro_context_health') else {"status": "healthy", "metrics": {"enabled": 0}})
                register_check("ml_filter", lambda: analyzer.ml_health() if hasattr(analyzer, 'ml_health') else {"status": "disabled", "metrics": {}})
                _hport = self.config_obj.get('general.health_port', 8080)
                _hhost = self.config_obj.get('general.health_host', '127.0.0.1')
                self._health_thread = start_health_server(host=_hhost, port=_hport)
            except Exception as e:
                logger.warning("Health server not started: %s", e)

        cadence = TimeframeCadence()
        self._symbol_failures: Dict[str, int] = {}
        self._symbol_error_sent: Dict[str, bool] = {}

        while self.running:
            try:
                for symbol in symbols:
                    if not self.running:
                        break
                    for tf in timeframes:
                        if not self.running:
                            break
                        if not cadence.is_due(symbol, tf):
                            continue
                        ok = analyzer.analyze_symbol(symbol, tf)
                        if ok is False:
                            cadence.mark_failed(symbol, tf)
                            failures = self._symbol_failures.get(symbol, 0) + 1
                            self._symbol_failures[symbol] = failures
                            if failures >= 3 and not self._symbol_error_sent.get(symbol):
                                self._symbol_error_sent[symbol] = True
                                self.alert_queue.put({"kind": "symbol_error", "symbol": symbol})
                        else:
                            cadence.mark_polled(symbol, tf)
                            self._symbol_failures.pop(symbol, None)
                            self._symbol_error_sent.pop(symbol, None)
                self.status_signal.emit("scanning", datetime.now().strftime("%H:%M:%S"))

                # Periodic outcome-stat digest (quant audit fix #3)
                if _time.time() - self._last_stats_log > 600:
                    self._last_stats_log = _time.time()
                    try:
                        st = self.alert_mgr.outcome_stats() if self.alert_mgr else None
                        if st and st.get("resolved"):
                            parts = [f"{r['signal_name']}: n={r['count']} win={r['win_rate']*100:.0f}% avgRet={r['avg_ret']*100:+.2f}% pf={r['profit_factor']:.2f}"
                                     for r in st["rows"][:10]]
                            logger.info("OUTCOME STATS (resolved=%d): %s", st["resolved"], " | ".join(parts))
                    except Exception:
                        pass

                if not self._interruptible_sleep(check_interval):
                    break
            except Exception as e:
                logger.error("Engine error: %s", e, exc_info=True)
                self.status_signal.emit("error", str(e)[:60])
                if not self._interruptible_sleep(5):
                    break


# ============================================================================
# CUSTOM WIDGETS
# ============================================================================
class StatPill(QFrame):
    def __init__(self, label: str, width: int = 140):
        super().__init__()
        self.setObjectName("Pill")
        self.setFixedWidth(width)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.title = QLabel(label)
        self.title.setStyleSheet("font-size: 10px; color: %s; font-weight: bold;" % TEXT_MUTED)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value = QLabel("—")
        self.value.setStyleSheet("font-size: 14px; font-weight: bold; color: %s;" % TEXT)
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._last = None
        lay.addWidget(self.title, alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addSpacing(2)
        lay.addWidget(self.value, alignment=Qt.AlignmentFlag.AlignCenter)

    def set(self, text: str, color: str = TEXT):
        # Phase 4: skip redundant restyles (was: full repolish every snapshot).
        key = (text, color)
        if key == self._last:
            return
        self._last = key
        self.value.setText(text)
        self.value.setStyleSheet("font-size: 14px; font-weight: bold; color: %s;" % color)
        self.title.setStyleSheet("font-size: 10px; color: %s; font-weight: bold;" % color)


class TickerCard(QFrame):
    def __init__(self, short: str):
        super().__init__()
        self.setObjectName("TickerCard")
        self.short = short
        self.last_price_val = None
        self.last_change_val = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        self.name = QLabel(short)
        self.name.setStyleSheet("font-weight: bold; font-size: 12px;")
        self.price_lbl = QLabel("—")
        self.price_lbl.setStyleSheet("font-weight: 500; font-size: 12px;")
        self.chg_lbl = QLabel("—")
        self.chg_lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % TEXT_MUTED)
        lay.addWidget(self.name)
        lay.addStretch()
        lay.addWidget(self.price_lbl)
        lay.addWidget(self.chg_lbl)

    def update_ticker(self, price: Optional[float], change_pct: Optional[float]):
        self.last_price_val = price
        self.last_change_val = change_pct
        # Phase 4: static name style set once in __init__; dynamic parts only
        # repaint when their value actually changed.
        price_txt = _fmt_price(price) if price is not None else "—"
        c = _change_color(change_pct)
        chg_txt = f"{change_pct:+.2f}%" if change_pct is not None else "—"
        key = (price_txt, chg_txt, c)
        if key == getattr(self, "_last_tick", None):
            return
        self._last_tick = key
        self.price_lbl.setText(price_txt)
        self.chg_lbl.setText(chg_txt)
        self.chg_lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % c)

    def set_error(self, err: bool):
        if err:
            self.name.setStyleSheet("font-weight: bold; font-size: 12px; color: %s;" % BEAR)
            self.price_lbl.setText("ERR")
            self.price_lbl.setStyleSheet("font-weight: 500; font-size: 12px; color: %s;" % BEAR)
            self.chg_lbl.setText("—")
            self.chg_lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % BEAR)
            self._last_tick = ("ERR", "—", BEAR)


class MarketListItem(QFrame):
    def __init__(self, short: str, full: Optional[str], on_click: Callable, selected: bool = False):
        super().__init__()
        self.setObjectName("MarketListItem")  # Phase 4: selector needs this or selection never paints
        self.full = full
        self.selected = selected
        self._on_click = on_click
        self.last_price_val = None
        self.last_change_val = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        self.name = QLabel(short)
        self.name.setStyleSheet("font-weight: bold; font-size: 12px;")
        self.price_lbl = QLabel("—")
        self.price_lbl.setStyleSheet("font-size: 11px;")
        self.chg_lbl = QLabel("—")
        self.chg_lbl.setStyleSheet("font-size: 10px; font-weight: bold;")
        lay.addWidget(self.name)
        lay.addStretch()
        lay.addWidget(self.price_lbl)
        lay.addWidget(self.chg_lbl)
        self._apply_style()

    def _apply_style(self):
        bg = "#1a1f26" if self.selected else "transparent"
        self.setStyleSheet(
            "QFrame#MarketListItem { background-color: %s; border-radius: 4px; }" % bg +
            "QFrame#MarketListItem:hover { background-color: #1e2329; }"
        )

    def set_selected(self, sel: bool):
        self.selected = sel
        self._apply_style()

    def update_ticker(self, price: Optional[float], change_pct: Optional[float]):
        self.last_price_val = price
        self.last_change_val = change_pct
        price_txt = _fmt_price(price) if price is not None else "—"
        c = _change_color(change_pct)
        chg_txt = f"{change_pct:+.2f}%" if change_pct is not None else "—"
        key = (price_txt, chg_txt, c)
        if key == getattr(self, "_last_tick", None):
            return
        self._last_tick = key
        self.price_lbl.setText(price_txt)
        self.chg_lbl.setText(chg_txt)
        self.chg_lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % c)

    def set_error(self, err: bool):
        if err:
            self.name.setStyleSheet("font-weight: bold; font-size: 12px; color: %s;" % BEAR)
            self.price_lbl.setText("ERR")
            self.price_lbl.setStyleSheet("font-size: 11px; color: %s;" % BEAR)
            self.chg_lbl.setText("—")
            self.chg_lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % BEAR)

    def mousePressEvent(self, e):
        self._on_click(self.full)
        super().mousePressEvent(e)


# ============================================================================
# MAIN WINDOW
# ============================================================================
class QuantAlertUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Quant Alert Engine Pro")
        self.resize(1280, 800)
        self.setStyleSheet(QSS)

        try:
            self.config = load_config("config.json")
        except SystemExit:
            # load_config already printed guidance; show dialog then exit
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "Config Error", "Could not start: invalid config.json / .env\n\nCheck console for details (see .env.example).")
            sys.exit(1)
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "Config Error", f"Could not start: {e}\n\nCheck .env.example")
            sys.exit(1)
        self.symbols: List[str] = self.config.get("symbols", [])
        self.timeframes: List[str] = self.config.get("timeframes", ["5m"])
        self.alert_queue: queue.Queue = queue.Queue()
        self.engine: Optional[EngineThread] = None
        self._telegram_ready = bool(self.config.get("telegram.token")) and bool(self.config.get("telegram.chat_id"))

        self.selected_symbol: Optional[str] = None
        self._selected_timeframe: Optional[str] = None
        self.ticker_cards: Dict[str, TickerCard] = {}
        self.market_items: Dict[str, MarketListItem] = {}
        self._tf_buttons: Dict[str, QPushButton] = {}
        self.last_known_prices: Dict[str, float] = {}
        # Latest snapshot per symbol. The engine only publishes a snapshot on
        # bar close, so without this cache selecting a pair shows "—" until the
        # next bar. Cache lets selection repopulate the detail panel instantly.
        self.last_snapshots: Dict[str, dict] = {}

        self.detail_alerts: List[dict] = []
        self.detail_cat_counts: Dict[str, int] = {}
        self.overview_alerts: List[dict] = []
        # Phase 2 (audit 2026-09-17): master store — EVERY alert is kept here
        # (filtered views must never destroy data). detail_/overview_ are
        # view caches rebuilt by _refresh_feed; ack lives on the alert dict.
        self.all_alerts: List[dict] = []
        self._all_cap = 1000

        self._build_ui()
        self._start_engine()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_queue)
        self._poll_timer.start(100)

    # ── BUILD UI ──
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── HEADER ──
        header = QFrame()
        header.setFixedHeight(52)
        header.setStyleSheet("background-color: %s; border-bottom: 1px solid %s;" % (BG, BORDER))
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 16, 0)

        brand = QFrame()
        brand.setFixedWidth(4)
        brand.setStyleSheet("background-color: %s;" % BRAND)
        hl.addWidget(brand)
        hl.addSpacing(10)

        title = QLabel("QUANT ALERT ENGINE")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        hl.addWidget(title)
        pro = QLabel("PRO")
        pro.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % BRAND)
        hl.addWidget(pro)
        hl.addSpacing(20)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("font-size: 12px; color: %s;" % NEUTRAL)
        self.status_label = QLabel("CONNECTING…")
        self.status_label.setStyleSheet("font-size: 10px; font-weight: bold;")
        hl.addWidget(self.status_dot)
        hl.addWidget(self.status_label)
        hl.addStretch()

        self.sound_btn = QPushButton("SOUND")
        self.sound_btn.setCheckable(True)
        self.sound_btn.setChecked(self.config.get("general.enable_sound", False))
        self.sound_btn.toggled.connect(self._toggle_sound)
        hl.addWidget(self.sound_btn)

        tg_label = "TELEGRAM"
        if self._telegram_ready and self.config.get("telegram.enabled", False):
            tg_label = "TELEGRAM ON"
        elif not self._telegram_ready:
            tg_label = "NO TOKEN"
        self.telegram_btn = QPushButton(tg_label)
        self.telegram_btn.setCheckable(True)
        self.telegram_btn.setChecked(self._telegram_ready and self.config.get("telegram.enabled", False))
        self.telegram_btn.toggled.connect(self._toggle_telegram)
        if not self._telegram_ready:
            self.telegram_btn.setEnabled(False)
        hl.addWidget(self.telegram_btn)

        clear_btn = QPushButton("CLEAR")
        clear_btn.clicked.connect(self._clear_alerts)
        hl.addWidget(clear_btn)

        # Phase 2: system-tray notifier (out-of-band path when minimized).
        # Standard style icon — no asset files needed.
        try:
            from PyQt6.QtWidgets import QSystemTrayIcon, QStyle
            _tray_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
            self._tray = QSystemTrayIcon(_tray_icon, self)
            self._tray.setToolTip("Quant Alert Engine Pro")
            self._tray.show()
        except Exception:
            logger.debug("System tray unavailable", exc_info=True)
            self._tray = None

        outer.addWidget(header)

        # ── TICKER STRIP (scrollable — Phase 4: >6 symbols used to clip) ──
        ticker_strip = QFrame()
        ticker_strip.setFixedHeight(68)
        ticker_strip.setStyleSheet("background-color: %s;" % BG)
        strip_outer = QHBoxLayout(ticker_strip)
        strip_outer.setContentsMargins(8, 8, 8, 8)
        strip_scroll = QScrollArea()
        strip_scroll.setWidgetResizable(True)
        strip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        strip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        strip_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        strip_inner = QFrame()
        strip_inner.setStyleSheet("background-color: %s;" % BG)
        tl = QHBoxLayout(strip_inner)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(6)
        for sym in self.symbols:
            short = sym.split("/")[0].split(":")[0]
            card = TickerCard(short)
            self.ticker_cards[sym] = card
            tl.addWidget(card)
        tl.addStretch()
        strip_scroll.setWidget(strip_inner)
        strip_outer.addWidget(strip_scroll)
        outer.addWidget(ticker_strip)

        # ── BODY (sidebar + center) ──
        body = QWidget()
        body.setStyleSheet("background-color: %s;" % BG)
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)

        # ── SIDEBAR ──
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(240)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(12, 12, 12, 12)

        markets_label = QLabel("MARKETS")
        markets_label.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % TEXT_MUTED)
        sl.addWidget(markets_label)

        tf_row = QHBoxLayout()
        tf_row.setSpacing(3)
        tf_row.setContentsMargins(0, 0, 0, 0)
        for tf in ["ALL"] + self.timeframes:
            btn = QPushButton(tf)
            btn.setCheckable(True)
            btn.setFixedHeight(22)
            btn.setStyleSheet(
                "QPushButton { background-color: %s; border: 1px solid %s; color: %s;"
                " border-radius: 4px; padding: 1px 5px; font-weight: bold; font-size: 10px; }"
                "QPushButton:hover { background-color: #303742; }"
                "QPushButton:checked { background-color: %s; color: %s; border: 1px solid %s; }"
                % (ELEVATED, BORDER_STR, TEXT_DIM, NEUTRAL, BG, NEUTRAL)
            )
            btn.clicked.connect(lambda checked, t=tf: self._select_timeframe(t))
            self._tf_buttons[tf] = btn
            tf_row.addWidget(btn)
        self._tf_buttons.get("ALL", QPushButton()).setChecked(True)
        self._selected_timeframe = None
        sl.addLayout(tf_row)
        sl.addSpacing(12)

        sym_scroll = QScrollArea()
        sym_scroll.setWidgetResizable(True)
        sym_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sym_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        sym_container = QWidget()
        sym_container.setStyleSheet("background: transparent;")
        sym_lay = QVBoxLayout(sym_container)
        sym_lay.setContentsMargins(0, 0, 0, 0)
        sym_lay.setSpacing(2)

        self._all_market_item = MarketListItem("ALL MARKETS", None, self._select_symbol, selected=True)
        sym_lay.addWidget(self._all_market_item)

        for sym in self.symbols:
            short = sym.split("/")[0].split(":")[0]
            item = MarketListItem(short, sym, self._select_symbol)
            self.market_items[sym] = item
            sym_lay.addWidget(item)
        sym_lay.addStretch()
        sym_scroll.setWidget(sym_container)
        sl.addWidget(sym_scroll)

        bl.addWidget(sidebar)

        # ── CENTER PANEL ──
        center = QWidget()
        center.setObjectName("Panel")
        cl = QVBoxLayout(center)
        cl.setContentsMargins(16, 16, 16, 16)
        cl.setSpacing(12)

        # Symbol header
        sym_header = QHBoxLayout()
        self.main_title = QLabel("Select a market")
        self.main_title.setStyleSheet("font-size: 26px; font-weight: bold;")
        self.main_tf_lbl = QLabel("")
        self.main_tf_lbl.setStyleSheet("font-size: 11px; color: %s;" % TEXT_MUTED)
        self.main_price = QLabel("")
        self.main_price.setStyleSheet("font-size: 28px; font-weight: bold;")
        self.main_chg = QLabel("")
        self.main_chg.setStyleSheet("font-size: 14px; font-weight: bold;")

        sym_header.addWidget(self.main_title)
        sym_header.addWidget(self.main_tf_lbl)
        sym_header.addSpacing(12)
        sym_header.addWidget(self.main_price)
        sym_header.addWidget(self.main_chg)
        sym_header.addStretch()

        self.ohlcv_labels = {}
        for label, key, color in [("HIGH", "high", BULL), ("LOW", "low", BEAR),
                                   ("VOL", "volume", TEXT), ("OPEN", "open", TEXT_DIM)]:
            vbox = QVBoxLayout()
            hdr = QLabel(label)
            hdr.setStyleSheet("font-size: 8px; color: %s; font-weight: bold;" % TEXT_MUTED)
            val = QLabel("—")
            val.setStyleSheet("font-size: 11px; font-weight: bold; color: %s;" % color)
            vbox.addWidget(hdr)
            vbox.addWidget(val)
            self.ohlcv_labels[key] = val
            sym_header.addLayout(vbox)

        self.updated_lbl = QLabel("Waiting for first scan…")
        self.updated_lbl.setStyleSheet("font-size: 10px; color: %s;" % TEXT_MUTED)
        sym_header.addWidget(self.updated_lbl, alignment=Qt.AlignmentFlag.AlignBottom)

        cl.addLayout(sym_header)

        # ── QSPLITTER ──
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        # Top: indicator strip
        ind_container = QWidget()
        ind_lay = QVBoxLayout(ind_container)
        ind_lay.setContentsMargins(0, 0, 0, 0)

        ind_title = QLabel("LIVE INDICATORS")
        ind_title.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % TEXT_MUTED)
        ind_lay.addWidget(ind_title)

        self.pill_widgets: Dict[str, StatPill] = {}
        pill_specs = [
            ("RSI (14)",     "rsi"),
            ("MACD HIST",    "macd_hist"),
            ("MACD SIG",     "macd_signal"),
            ("ATR",          "atr"),
            ("BB UPPER",     "bb_upper"),
            ("BB LOWER",     "bb_lower"),
        ]
        grid_rows = []
        for idx, (label, key) in enumerate(pill_specs):
            if idx % 4 == 0:
                row = QHBoxLayout()
                row.setSpacing(6)
                grid_rows.append(row)
            pill = StatPill(label)
            self.pill_widgets[key] = pill
            row.addWidget(pill)
        for row in grid_rows:
            ind_lay.addLayout(row)

        splitter.addWidget(ind_container)

        # Bottom: alert feed
        feed_container = QWidget()
        feed_lay = QVBoxLayout(feed_container)
        feed_lay.setContentsMargins(0, 8, 0, 0)

        feed_title = QLabel("ALERT FEED")
        feed_title.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % TEXT_MUTED)
        feed_lay.addWidget(feed_title)

        badge_row = QHBoxLayout()
        self.badge_labels: Dict[str, QLabel] = {}
        for cat in CAT_COLORS:
            lbl = QLabel(f"{cat} 0")
            lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % CAT_COLORS[cat])
            self.badge_labels[cat] = lbl
            badge_row.addWidget(lbl)
        badge_row.addStretch()
        feed_lay.addLayout(badge_row)

        self.alert_table = QTableWidget()
        self.alert_table.setColumnCount(7)
        self.alert_table.setHorizontalHeaderLabels(
            ["TIME", "SYMBOL", "TF", "TYPE", "SIGNAL", "PRICE", "TG"])
        self.alert_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.alert_table.horizontalHeader().setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        self.alert_table.horizontalHeader().setSectionResizeMode(COL_SYMBOL, QHeaderView.ResizeMode.ResizeToContents)
        self.alert_table.horizontalHeader().setSectionResizeMode(COL_TF, QHeaderView.ResizeMode.ResizeToContents)
        self.alert_table.horizontalHeader().setSectionResizeMode(COL_TG, QHeaderView.ResizeMode.ResizeToContents)
        self.alert_table.verticalHeader().setVisible(False)
        self.alert_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.alert_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Phase 4: sortable feed (default newest-first by TIME). Row lookups
        # elsewhere are signal_id-based, so visual order is always safe.
        self.alert_table.setSortingEnabled(True)
        self.alert_table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.alert_table.itemDoubleClicked.connect(
            lambda item: self._ack_row(item.row()))

        feed_lay.addWidget(self.alert_table)

        self.feed_footer = QLabel("0 alerts")
        self.feed_footer.setStyleSheet("font-size: 10px; color: %s;" % TEXT_MUTED)
        feed_lay.addWidget(self.feed_footer)

        splitter.addWidget(feed_container)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        cl.addWidget(splitter)
        bl.addWidget(center)
        outer.addWidget(body, 1)

        # ── STATUS BAR ──
        status_bar = QFrame()
        status_bar.setFixedHeight(26)
        status_bar.setStyleSheet("background-color: %s;" % PANEL)
        sl2 = QHBoxLayout(status_bar)
        sl2.setContentsMargins(14, 0, 14, 0)
        self.conn_label = QLabel("●  Connecting to Binance…")
        self.conn_label.setStyleSheet("font-size: 10px; color: %s;" % TEXT_MUTED)
        sl2.addWidget(self.conn_label)
        sl2.addStretch()
        self.scan_label = QLabel("Last scan: --")
        self.scan_label.setStyleSheet("font-size: 10px; color: %s;" % TEXT_MUTED)
        sl2.addWidget(self.scan_label)
        interval = self.config.get("general.check_interval", 5)
        tfs = ", ".join(self.timeframes)
        self.sym_label = QLabel(f"TF: {tfs}" if tfs else "TF: 5m")
        self.sym_label.setStyleSheet("font-size: 10px; color: %s;" % TEXT_MUTED)
        sl2.addWidget(self.sym_label)
        outer.addWidget(status_bar)

    # ── TIMEFRAME SELECTION ──
    # NOTE (audit 2026-09-17): view selection NEVER touches Telegram delivery.
    # Telegram filtering lives solely in TelegramNotifier.set_filter, driven by
    # its own control — browsing the UI must not change what gets delivered.
    def _select_timeframe(self, tf: str):
        self._selected_timeframe = None if tf == "ALL" else tf
        for t, btn in self._tf_buttons.items():
            btn.setChecked(t == tf)

    # ── SYMBOL SELECTION ──
    def _select_symbol(self, symbol: Optional[str]):
        self.selected_symbol = symbol
        # SYMBOL column only matters when alerts from many pairs share the feed;
        # in single-pair view it is redundant, so hide it.
        self.alert_table.setColumnHidden(COL_SYMBOL, symbol is not None)
        for s, item in self.market_items.items():
            item.set_selected(s == symbol)
        self._all_market_item.set_selected(symbol is None)
        if symbol is not None:
            self._reset_detail_for_symbol(symbol)
            # Repopulate instantly from the cached snapshot (fix: indicators
            # stayed "—" after selecting a pair until the next bar close).
            cached = self.last_snapshots.get(symbol)
            if cached:
                self._update_detail(symbol, cached.get("price"),
                                    cached.get("indicators", {}) or {},
                                    cached.get("timestamp"))
        else:
            self.main_title.setText("ALL MARKETS")
            self.main_tf_lbl.setText("")
            self.main_price.setText("")
            self.main_chg.setText("")
        self._refresh_feed()

    # ── RESET DETAIL ──
    def _reset_detail_for_symbol(self, symbol: str):
        # Phase 2: resets PANELS only — alert stores are owned by all_alerts
        # and rendered by _refresh_feed (switching views destroys nothing).
        short = symbol.split("/")[0].split(":")[0]
        self.main_title.setText(f"{short}/{_quote_of(symbol)}")
        tf_txt = self._selected_timeframe if self._selected_timeframe else "ALL"
        self.main_tf_lbl.setText(tf_txt)
        self.main_price.setText("—")
        self.main_chg.setText("")
        for key in ("high", "low", "volume", "open"):
            self.ohlcv_labels[key].setText("—")
        for key in self.pill_widgets:
            self.pill_widgets[key].set("—")
        for cat in CAT_COLORS:
            self.badge_labels[cat].setText(f"{cat} 0")
        self.feed_footer.setText("0 alerts")

    # ── ENGINE ──
    def _start_engine(self):
        self.engine = EngineThread("config.json", self.alert_queue, self._update_status)
        self.engine.start()

    # ── QUEUE POLLING ──
    def _poll_queue(self):
        # Coalesce snapshots to the LATEST per symbol and bound the work per tick
        # so the main thread never blocks draining a backlog (fixes UI freeze).
        latest: Dict[str, dict] = {}
        processed = 0
        try:
            while processed < 200:
                msg = self.alert_queue.get_nowait()
                processed += 1
                kind = msg.get("kind")
                if kind == "snapshot":
                    latest[msg["symbol"]] = msg  # keep most recent per symbol
                elif kind == "symbol_error":
                    self._handle_symbol_error(msg)
                elif kind == "delivery":
                    self._handle_delivery(msg)
                else:
                    self._handle_alert(msg)
        except queue.Empty:
            pass
        # Process one (most recent) snapshot per symbol
        for sym in list(latest.keys()):
            self._handle_snapshot(latest[sym])

    def _handle_message(self, msg: dict):
        kind = msg.get("kind")
        if kind == "snapshot":
            self._handle_snapshot(msg)
        elif kind == "symbol_error":
            self._handle_symbol_error(msg)
        else:
            self._handle_alert(msg)

    def _handle_symbol_error(self, msg: dict):
        symbol = msg["symbol"]
        card = self.ticker_cards.get(symbol)
        if card:
            card.set_error(True)
        item = self.market_items.get(symbol)
        if item:
            item.set_error(True)

    def _handle_snapshot(self, msg: dict):
        symbol = msg["symbol"]
        ind = msg.get("indicators", {})
        price = msg.get("price")
        ts = msg.get("timestamp")

        # Cache latest snapshot per symbol (see __init__) so a later selection
        # can repopulate the detail panel without waiting for the next bar.
        self.last_snapshots[symbol] = msg

        # Capture prev before overwriting so _update_detail can color correctly (fix #6)
        prev_price = self.last_known_prices.get(symbol)
        if price is not None:
            self.last_known_prices[symbol] = price
        change_pct = ind.get("session_change_pct")

        card = self.ticker_cards.get(symbol)
        if card:
            card.set_error(False)
            card.update_ticker(price, change_pct)
        item = self.market_items.get(symbol)
        if item:
            item.set_error(False)
            item.update_ticker(price, change_pct)

        if self.selected_symbol == symbol:
            self._update_detail(symbol, price, ind, ts, prev_price)

    def _update_detail(self, symbol: str, price: float, ind: dict, ts, prev: Optional[float] = None):
        short = symbol.split("/")[0].split(":")[0]
        self.main_title.setText(f"{short}/{_quote_of(symbol)}")
        # prev is passed from _handle_snapshot to avoid reading already-overwritten dict (fix #6)
        if prev is None:
            prev = self.last_known_prices.get(symbol)
            # If prev is still == price (first tick after overwrite), try to infer it's same-tick
            # Keep neutral in that edge case; caller now passes correct prev so this is fallback.
            if prev == price:
                prev = None
        pc = _price_color(price, prev)
        self.main_price.setText(_fmt_price(price))
        self.main_price.setStyleSheet("font-size: 28px; font-weight: bold; color: %s;" % pc)
        chg = ind.get("session_change_pct")
        if chg is not None:
            cc = _change_color(chg)
            self.main_chg.setText(f"{chg:+.2f}%")
            self.main_chg.setStyleSheet("font-size: 14px; font-weight: bold; color: %s;" % cc)
        for key in ("high", "low", "volume", "open"):
            val = ind.get(key)
            self.ohlcv_labels[key].setText(_fmt_price(val) if val is not None else "—")
        if ts:
            self.updated_lbl.setText(ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else str(ts)[:8])

        pill_map = {
            "rsi": ("rsi", _rsi_color),
            "macd_hist": ("macd_hist", _macd_hist_color),
            "macd_signal": ("macd_signal", lambda v: TEXT),
            "atr": ("atr", lambda v: TEXT),
            "bb_upper": ("bb_upper", lambda v: TEXT),
            "bb_lower": ("bb_lower", lambda v: TEXT),
        }
        for key, (ind_key, color_fn) in pill_map.items():
            pw = self.pill_widgets.get(key)
            if pw:
                val = ind.get(ind_key)
                if val is not None:
                    if ind_key in ("macd_hist", "macd_signal", "atr"):
                        text = f"{val:.4f}".rstrip("0").rstrip(".")
                    else:
                        text = f"{val:.2f}"
                    pw.set(text, color_fn(val))
                else:
                    pw.set("—")

    def _handle_alert(self, alert: dict):
        # Phase 2: NEVER drop — every alert lands in the master store first.
        # The visible table is a filtered render of it (_refresh_feed).
        alert.setdefault("ack", False)
        alert.setdefault("tg_status", alert.get("tg_status", "pending"))
        self.all_alerts.append(alert)
        if len(self.all_alerts) > self._all_cap:
            del self.all_alerts[:len(self.all_alerts) - self._all_cap]
        self._notify_out_of_band(alert)
        self._refresh_feed()

    def _notify_out_of_band(self, alert: dict):
        """Tray bubble + taskbar flash, only when the window is not active."""
        try:
            if self.isActiveWindow():
                return
            QApplication.alert(self, 3000)
            tray = getattr(self, "_tray", None)
            if tray is not None:
                tray.showMessage(
                    f"{alert.get('symbol', '?')} [{alert.get('timeframe', '')}]",
                    f"{alert.get('signal_type', '')} {alert.get('signal_name', '')} @ {alert.get('price', '?')}",
                    tray.MessageIcon.Information, 5000)
        except Exception:
            logger.debug("Out-of-band notify failed", exc_info=True)

    def _visible_alerts(self):
        """Master store filtered by current view, newest first."""
        out = []
        for alert in self.all_alerts:
            sym = alert["symbol"]
            tf = alert.get("timeframe", "")
            if self.selected_symbol and sym != self.selected_symbol:
                continue
            if self._selected_timeframe and tf != self._selected_timeframe:
                continue
            out.append(alert)
        out.reverse()
        return out

    def _refresh_feed(self):
        """Rebuild table, badges and footer from the master store."""
        MAX_ROWS = 300
        visible = self._visible_alerts()
        # Keep legacy view caches in sync (delivery updates + footer read them).
        self.detail_alerts = list(visible)
        self.detail_cat_counts = {}
        for alert in visible:
            cat = alert["signal_type"]
            self.detail_cat_counts[cat] = self.detail_cat_counts.get(cat, 0) + 1
        self.alert_table.setRowCount(0)
        self.alert_table.setSortingEnabled(False)
        for row_idx, alert in enumerate(visible[:MAX_ROWS]):
            self.alert_table.insertRow(row_idx)
            self._populate_alert_row(row_idx, alert)
        self.alert_table.setSortingEnabled(True)
        for c in CAT_COLORS:
            cnt = self.detail_cat_counts.get(c, 0)
            self.badge_labels[c].setText(f"{c} {cnt}")
        self._refresh_footer_unread()

    def _handle_delivery(self, msg: dict):
        """Update TG delivery glyph (⏳/✅/❌) for a previously shown alert row."""
        sid = msg.get("signal_id")
        if not sid:
            return
        glyph = "✅" if msg.get("ok") else "❌"
        status = "sent" if msg.get("ok") else "failed"
        for store in (self.all_alerts, self.detail_alerts, self.overview_alerts):
            for alert in store:
                if alert.get("signal_id") == sid:
                    alert["tg_status"] = status
        tg_col = COL_TG
        for row in range(self.alert_table.rowCount()):
            item0 = self.alert_table.item(row, 0)
            if item0 and item0.data(Qt.ItemDataRole.UserRole) == sid:
                tg_item = self.alert_table.item(row, tg_col)
                if tg_item is None:
                    tg_item = QTableWidgetItem(glyph)
                    self.alert_table.setItem(row, tg_col, tg_item)
                else:
                    tg_item.setText(glyph)
                break

    def _populate_alert_row(self, row: int, alert: dict):
        ts = alert.get("timestamp")
        time_str = ts.strftime("%H:%M:%S") if isinstance(ts, datetime) else str(ts)[:8]
        tf = alert.get("timeframe", "")
        sig_type = alert.get("signal_type", "")
        sig_name = alert.get("signal_name", "")
        price = alert.get("price")
        price_str = _fmt_price(price) if price is not None else "—"
        tg_status = alert.get("tg_status", "pending")
        tg_glyph = {"sent": "✅", "failed": "❌"}.get(tg_status, "⏳")

        items = [
            QTableWidgetItem(time_str),
            QTableWidgetItem(_short_pair(alert.get("symbol", ""))),
            QTableWidgetItem(tf),
            QTableWidgetItem(sig_type),
            QTableWidgetItem(sig_name),
            QTableWidgetItem(price_str),
            QTableWidgetItem(tg_glyph),
        ]
        # Row identity for delivery-status updates (Phase 1: TG ⏳/✅/❌).
        if alert.get("signal_id"):
            items[COL_TIME].setData(Qt.ItemDataRole.UserRole, alert["signal_id"])
        alert_color = alert.get("color", "info")
        color_map = {"green": BULL, "red": BEAR, "yellow": NEUTRAL, "info": TEXT}
        row_color = color_map.get(alert_color, TEXT)

        for col, item in enumerate(items):
            self.alert_table.setItem(row, col, item)
        self._paint_alert_row(row, alert)

    def _paint_alert_row(self, row: int, alert: dict):
        """(Re)apply row coloring: acked rows dim, else signal color."""
        if alert.get("ack"):
            dim = QColor(TEXT_MUTED)
            for col in range(self.alert_table.columnCount()):
                item = self.alert_table.item(row, col)
                if item is not None:
                    item.setForeground(dim)
            return
        alert_color = alert.get("color", "info")
        color_map = {"green": BULL, "red": BEAR, "yellow": NEUTRAL, "info": TEXT}
        row_color = color_map.get(alert_color, TEXT)
        for col in range(self.alert_table.columnCount()):
            item = self.alert_table.item(row, col)
            if item is None:
                continue
            if col in COL_COLORED:
                item.setForeground(QColor(row_color))
            else:
                item.setForeground(QColor(TEXT))
            if col in COL_BOLD:
                font = item.font()
                font.setBold(True)
                item.setFont(font)

    # ── STATUS CALLBACK ──
    def _update_status(self, status: str, msg: str):
        # EngineThread calls this from a worker thread; widgets may already be
        # destroyed when the window closes (RuntimeError: C++ object deleted).
        # Guard so a closing window can't crash the engine loop.
        try:
            if status == "connected":
                self.status_dot.setStyleSheet("font-size: 12px; color: %s;" % BULL)
                self.status_label.setText("LIVE")
                self.conn_label.setText(msg)
            elif status == "scanning":
                self.scan_label.setText(f"Last scan: {msg}")
            elif status == "error":
                self.status_dot.setStyleSheet("font-size: 12px; color: %s;" % BEAR)
                self.status_label.setText("ERROR")
                self.conn_label.setText(msg)
        except RuntimeError:
            # Window is being torn down — nothing left to update.
            logger.debug("Status update skipped: UI closing (%s)", status)

    # ── TOGGLES ──
    def _save_config(self):
        # Use validated Config.save() which strips secrets; for toggle we persist non-secrets only
        try:
            self.config.save()
        except Exception:
            data = self.config.data
            # Phase 5: the raw-dump fallback must never persist secrets that
            # Config.save() would have stripped (token/chat_id/api keys).
            for _sec, _keys in (("telegram", ("token", "chat_id")),
                                ("exchange", ("api_key", "secret")),
                                ("macro", ("coingecko_key", "cmc_api_key"))):
                _sub = data.get(_sec)
                if isinstance(_sub, dict):
                    for _k in _keys:
                        _sub.pop(_k, None)
            with open(self.config.config_file, 'w') as f:
                json.dump(data, f, indent=2)

    def _toggle_sound(self, enabled: bool):
        try:
            self.config.set('general.enable_sound', enabled)
        except Exception:
            try:
                self.config._settings.general.enable_sound = enabled  # type: ignore
            except Exception:
                logger.warning("Failed to toggle sound setting: %s", enabled)

        self._save_config()

    def _toggle_telegram(self, enabled: bool):
        try:
            self.config.set('telegram.enabled', enabled)
        except Exception:
            try:
                self.config._settings.telegram.enabled = enabled  # type: ignore
            except Exception:
                logger.warning("Failed to toggle telegram setting: %s", enabled)
        self._save_config()
        if self.engine and self.engine.telegram_bot:
            self.engine.telegram_bot.set_enabled(enabled)
        self.telegram_btn.setText("TELEGRAM ON" if enabled else "TELEGRAM")

    def _clear_alerts(self):
        from PyQt6.QtWidgets import QMessageBox
        n = len(self.all_alerts)
        if n == 0:
            return
        ans = QMessageBox.question(
            self, "Clear feed",
            f"Discard all {n} stored alerts? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.all_alerts.clear()
        self._refresh_feed()

    def _ack_row(self, row: int):
        """Double-click a feed row toggles acknowledged (dims the row)."""
        target = None
        item0 = self.alert_table.item(row, 0)
        sid = item0.data(Qt.ItemDataRole.UserRole) if item0 is not None else None
        if sid:
            for alert in self.all_alerts:
                if alert.get("signal_id") == sid:
                    target = alert
                    break
        if target is None and 0 <= row < len(self.detail_alerts):
            target = self.detail_alerts[row]
        if target is None:
            return
        target["ack"] = not target.get("ack", False)
        self._paint_alert_row(row, target)
        self._refresh_footer_unread()

    def _refresh_footer_unread(self):
        visible = self._visible_alerts()
        unread = sum(1 for a in visible if not a.get("ack"))
        self.feed_footer.setText(f"{len(visible)} alerts · {unread} unread")

    # ── CLOSE ──
    def closeEvent(self, e):
        """Deterministic shutdown.

        The window closing must actually end the process. Previously the engine
        QThread was left running (stop() only flipped a flag), so the window
        disappeared but the process stayed alive — the app "would not close".
        """
        if getattr(self, "_shutting_down", False):
            e.accept()
            return
        self._shutting_down = True
        logger.info("Shutting down — stopping engine thread")
        try:
            if getattr(self, "_poll_timer", None) is not None:
                self._poll_timer.stop()
        except Exception:
            pass
        if self.engine:
            stopped = self.engine.stop()          # blocks until the QThread ends
            # Release the SQLite handle only AFTER the thread stopped using it.
            try:
                if self.engine.alert_mgr:
                    self.engine.alert_mgr.close()
            except Exception:
                pass
            self._engine_stopped = bool(stopped)
            logger.info("Engine thread stopped=%s", stopped)
        e.accept()
        super().closeEvent(e)
        QApplication.quit()


if __name__ == "__main__":
    from quant_alert_engine import SingleInstanceLock
    _lock = SingleInstanceLock('data/engine.lock')
    if not _lock.acquire():
        # Phase 5: silent exit left users thinking the app was broken.
        # No QApplication exists yet, so use the native message box.
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "Quant Alert Engine Pro is already running.\n\n"
                "Check the taskbar / system tray — only one instance "
                "is allowed (duplicate engines send duplicate Telegram alerts).",
                "Already running", 0x40)
        except Exception:
            pass
        sys.exit(1)
    win = None
    _exit_code = 0
    try:
        app = QApplication(sys.argv)
        win = QuantAlertUI()
        win.show()
        _exit_code = app.exec()
    finally:
        # Release the lock only when the engine is really dead. Releasing
        # first opens a window where a second instance starts a duplicate
        # engine (and duplicate Telegram sends) while this one still lives.
        # A live QThread blocks interpreter shutdown with no UI on screen, so
        # a wedged engine forces process exit WITHOUT releasing — the stale
        # lock is reaped by PID on next start (SingleInstanceLock).
        _eng = getattr(win, "engine", None)
        if _eng is None or not _eng.isRunning():
            _lock.release()
        else:
            logger.warning("Engine thread still running after close — forcing process exit")
            os._exit(_exit_code or 0)
    sys.exit(_exit_code)
