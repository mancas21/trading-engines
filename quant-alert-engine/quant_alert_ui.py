"""
Quant Alert Engine Pro — PyQt6 Desktop UI v2.5
QSplitter-based layout guarantees 12+ visible alert rows.
Fixes: Pydantic config, SQLite dedup unification, configurable bridge, debug-level TG skips, structured logging, thread-safe UI signals.
"""

import os, sys, json, queue, threading, time as _time, logging
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Callable
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
    "MACD":      "#16a085",
    "MACD_DIV":  "#16a085",
    "STOCH":     NEUTRAL,
    "VOL":       TEXT_MUTED,
}

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

def _stoch_color(v):
    if v is None: return TEXT
    if v >= 80: return BEAR
    if v <= 20: return BULL
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
                   signal_type: str, timeframe: str = "", color: str = "info"):
        if not self.enabled:
            logger.debug("TG skipped: not enabled (%s %s)", symbol, timeframe)
            return
        if not self._passes_filter(symbol, timeframe):
            logger.debug("TG skipped: filtered out %s %s", symbol, timeframe)
            return
        now = _time.time()
        if now - self._last_error_ts < 30:
            return

        def _do_send():
            try:
                emoji_map = {"green": "\U0001f7e2", "red": "\U0001f534", "yellow": "\U0001f7e1", "info": "\u26aa"}
                emoji = emoji_map.get(color, "\u26aa")
                direction_map = {"green": "BULLISH", "red": "BEARISH", "yellow": "WARNING", "info": "INFO"}
                direction = direction_map.get(color, "INFO")
                msg = (
                    f"{emoji} *{symbol}* [{timeframe}]\n"
                    f"{direction}\n"
                    f"`{signal_type}` {signal_name}\n"
                    f"Price: `{price:.4f}`"
                )
                resp = requests.post(
                    f"{self._base}/sendMessage",
                    json={"chat_id": self.chat_id, "text": msg, "parse_mode": "Markdown"},
                    timeout=10,
                )
                if resp.ok:
                    logger.info("TG sent: %s %s %s", symbol, signal_name, timeframe)
                else:
                    logger.warning("Telegram send failed: %s", resp.text)
            except Exception as e:
                logger.warning("Telegram error: %s", e)
                self._last_error_ts = _time.time()

        threading.Thread(target=_do_send, daemon=True).start()


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

    def emit_alert(self, signal_name: str, symbol: str, price: float,
                   signal_type: str = "SIGNAL", color: str = "info",
                   bar_key: str = "", timeframe: str = ""):
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

        # Outcome tagging (quant audit fix #3): track forward return of every signal
        self.tag_signal(signal_name, symbol, price, timeframe, bar_key, signal_type,
                        self.config.get('general.outcome_forward_bars', 12))

        msg = f"[{signal_type}] {symbol} - {signal_name} @ {price:.4f}"
        logger.info(msg, extra={"event_type": "alert_fired", "symbol": symbol, "timeframe": timeframe, "signal_name": signal_name, "signal_type": signal_type, "price": price, "color": color, "signal_id": signal_id})
        if self.config.get('general.enable_sound', True) and winsound:
            try:
                winsound.PlaySound('SystemExclamation', winsound.SND_ASYNC)
            except Exception:
                pass
        if self.config.get('general.enable_notifications', True):
            try:
                self.notifier.show_toast("Quant Alert Engine", msg, duration=10, threaded=True)
            except Exception:
                pass

        self.alert_queue.put({
            "kind": "alert",
            "timestamp": datetime.now(),
            "symbol": symbol,
            "signal_type": signal_type,
            "signal_name": signal_name,
            "price": price,
            "color": color,
            "timeframe": timeframe,
        })

        if self.telegram_bot:
            self.telegram_bot.send_alert(signal_name, symbol, price, signal_type, timeframe, color)

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
                        },
                        headers=_headers,
                        timeout=bridge_timeout,
                    )
                except Exception:
                    logger.debug("Bridge forward failed (normal if bridge is off) for %s %s", symbol, signal_name)

            threading.Thread(target=_post_bridge, daemon=True).start()

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
            self.status_signal.connect(status_callback)
        self.running = False
        self.connected = False
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

    def stop(self):
        self.running = False

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

                _time.sleep(check_interval)
            except Exception as e:
                logger.error("Engine error: %s", e, exc_info=True)
                self.status_signal.emit("error", str(e)[:60])
                _time.sleep(5)


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
        self.title.setStyleSheet("font-size: 8px; color: %s; font-weight: bold;" % TEXT_MUTED)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value = QLabel("—")
        self.value.setStyleSheet("font-size: 14px; font-weight: bold; color: %s;" % TEXT)
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.title, alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addSpacing(2)
        lay.addWidget(self.value, alignment=Qt.AlignmentFlag.AlignCenter)

    def set(self, text: str, color: str = TEXT):
        self.value.setText(text)
        self.value.setStyleSheet("font-size: 14px; font-weight: bold; color: %s;" % color)
        self.title.setStyleSheet("font-size: 8px; color: %s; font-weight: bold;" % color)


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
        self.price_lbl.setText(_fmt_price(price) if price is not None else "—")
        c = _change_color(change_pct)
        txt = f"{change_pct:+.2f}%" if change_pct is not None else "—"
        self.chg_lbl.setText(txt)
        self.chg_lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % c)
        self.price_lbl.setStyleSheet("font-weight: 500; font-size: 12px;")
        self.name.setStyleSheet("font-weight: bold; font-size: 12px;")

    def set_error(self, err: bool):
        if err:
            self.name.setStyleSheet("font-weight: bold; font-size: 12px; color: %s;" % BEAR)
            self.price_lbl.setText("ERR")
            self.price_lbl.setStyleSheet("font-weight: 500; font-size: 12px; color: %s;" % BEAR)
            self.chg_lbl.setText("—")
            self.chg_lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % BEAR)


class MarketListItem(QFrame):
    def __init__(self, short: str, full: Optional[str], on_click: Callable, selected: bool = False):
        super().__init__()
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
        self.price_lbl.setText(_fmt_price(price) if price is not None else "—")
        c = _change_color(change_pct)
        txt = f"{change_pct:+.2f}%" if change_pct is not None else "—"
        self.chg_lbl.setText(txt)
        self.chg_lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: %s;" % c)
        self.price_lbl.setStyleSheet("font-size: 11px;")
        self.name.setStyleSheet("font-weight: bold; font-size: 12px;")

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

        self.detail_alerts: List[dict] = []
        self.detail_cat_counts: Dict[str, int] = {}
        self.overview_alerts: List[dict] = []

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

        outer.addWidget(header)

        # ── TICKER STRIP ──
        ticker_strip = QFrame()
        ticker_strip.setFixedHeight(68)
        ticker_strip.setStyleSheet("background-color: %s;" % BG)
        tl = QHBoxLayout(ticker_strip)
        tl.setContentsMargins(8, 8, 8, 8)
        tl.setSpacing(6)
        for sym in self.symbols:
            short = sym.split("/")[0].split(":")[0]
            card = TickerCard(short)
            self.ticker_cards[sym] = card
            tl.addWidget(card)
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
                " border-radius: 4px; padding: 1px 5px; font-weight: bold; font-size: 9px; }"
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
        ind_title.setStyleSheet("font-size: 9px; font-weight: bold; color: %s;" % TEXT_MUTED)
        ind_lay.addWidget(ind_title)

        self.pill_widgets: Dict[str, StatPill] = {}
        pill_specs = [
            ("RSI (14)",     "rsi"),
            ("MACD HIST",    "macd_hist"),
            ("MACD SIG",     "macd_signal"),
            ("ATR",          "atr"),
            ("STOCH %K",     "stoch_k"),
            ("STOCH %D",     "stoch_d"),
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
        feed_title.setStyleSheet("font-size: 9px; font-weight: bold; color: %s;" % TEXT_MUTED)
        feed_lay.addWidget(feed_title)

        badge_row = QHBoxLayout()
        self.badge_labels: Dict[str, QLabel] = {}
        for cat in CAT_COLORS:
            lbl = QLabel(f"{cat} 0")
            lbl.setStyleSheet("font-size: 9px; font-weight: bold; color: %s;" % CAT_COLORS[cat])
            self.badge_labels[cat] = lbl
            badge_row.addWidget(lbl)
        badge_row.addStretch()
        feed_lay.addLayout(badge_row)

        self.alert_table = QTableWidget()
        self.alert_table.setColumnCount(5)
        self.alert_table.setHorizontalHeaderLabels(["TIME", "TF", "TYPE", "SIGNAL", "PRICE"])
        self.alert_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.alert_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.alert_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.alert_table.verticalHeader().setVisible(False)
        self.alert_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.alert_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        feed_lay.addWidget(self.alert_table)

        self.feed_footer = QLabel("0 alerts")
        self.feed_footer.setStyleSheet("font-size: 9px; color: %s;" % TEXT_MUTED)
        feed_lay.addWidget(self.feed_footer)

        splitter.addWidget(feed_container)
        splitter.setSizes([180, 480])
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
        self.conn_label.setStyleSheet("font-size: 9px; color: %s;" % TEXT_MUTED)
        sl2.addWidget(self.conn_label)
        sl2.addStretch()
        self.scan_label = QLabel("Last scan: --")
        self.scan_label.setStyleSheet("font-size: 9px; color: %s;" % TEXT_MUTED)
        sl2.addWidget(self.scan_label)
        interval = self.config.get("general.check_interval", 5)
        tfs = ", ".join(self.timeframes)
        self.sym_label = QLabel(f"TF: {tfs}" if tfs else "TF: 5m")
        self.sym_label.setStyleSheet("font-size: 9px; color: %s;" % TEXT_MUTED)
        sl2.addWidget(self.sym_label)
        outer.addWidget(status_bar)

    # ── TIMEFRAME SELECTION ──
    def _select_timeframe(self, tf: str):
        self._selected_timeframe = None if tf == "ALL" else tf
        for t, btn in self._tf_buttons.items():
            btn.setChecked(t == tf)
        self._update_telegram_filter()

    # ── SYMBOL SELECTION ──
    def _select_symbol(self, symbol: Optional[str]):
        self.selected_symbol = symbol
        for s, item in self.market_items.items():
            item.set_selected(s == symbol)
        self._all_market_item.set_selected(symbol is None)
        self._update_telegram_filter()
        if symbol is not None:
            self._reset_detail_for_symbol(symbol)
        else:
            self.main_title.setText("ALL MARKETS")
            self.main_tf_lbl.setText("")
            self.main_price.setText("")
            self.main_chg.setText("")
            self.alert_table.setRowCount(len(self.overview_alerts))
            for row_idx, alert in enumerate(self.overview_alerts):
                self._populate_alert_row(row_idx, alert)
            self.feed_footer.setText(f"{len(self.overview_alerts)} alerts")

    def _update_telegram_filter(self):
        if self.engine:
            sym = self.selected_symbol
            short = sym.split("/")[0].split(":")[0] if sym else None
            self.engine.set_telegram_filter(short, self._selected_timeframe)

    # ── RESET DETAIL ──
    def _reset_detail_for_symbol(self, symbol: str):
        self.detail_alerts.clear()
        self.detail_cat_counts.clear()
        self.alert_table.setRowCount(0)
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
            "stoch_k": ("stoch_k", _stoch_color),
            "stoch_d": ("stoch_d", _stoch_color),
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
        sym = alert["symbol"]
        tf = alert.get("timeframe", "")
        if self.selected_symbol and sym != self.selected_symbol:
            return
        if self._selected_timeframe and tf != self._selected_timeframe:
            return

        self.detail_alerts.append(alert)
        cat = alert["signal_type"]
        self.detail_cat_counts[cat] = self.detail_cat_counts.get(cat, 0) + 1

        # Cap retained alerts/table rows so inserts stay O(1)-ish (fixes UI freeze
        # from unbounded table growth under heavy alert volume).
        MAX_ROWS = 300
        if len(self.detail_alerts) > MAX_ROWS:
            del self.detail_alerts[:len(self.detail_alerts) - MAX_ROWS]

        if self.selected_symbol:
            self.alert_table.insertRow(0)
            self._populate_alert_row(0, alert)
            self.feed_footer.setText(f"{len(self.detail_alerts)} alerts")
            while self.alert_table.rowCount() > MAX_ROWS:
                self.alert_table.removeRow(self.alert_table.rowCount() - 1)
            for c in CAT_COLORS:
                cnt = self.detail_cat_counts.get(c, 0)
                self.badge_labels[c].setText(f"{c} {cnt}")

        if self.selected_symbol is None:
            self.overview_alerts.append(alert)
            if len(self.overview_alerts) > MAX_ROWS:
                del self.overview_alerts[:len(self.overview_alerts) - MAX_ROWS]
            self.alert_table.insertRow(0)
            self._populate_alert_row(0, alert)
            self.feed_footer.setText(f"{len(self.overview_alerts)} alerts")
            while self.alert_table.rowCount() > MAX_ROWS:
                self.alert_table.removeRow(self.alert_table.rowCount() - 1)

    def _populate_alert_row(self, row: int, alert: dict):
        ts = alert.get("timestamp")
        time_str = ts.strftime("%H:%M:%S") if isinstance(ts, datetime) else str(ts)[:8]
        tf = alert.get("timeframe", "")
        sig_type = alert.get("signal_type", "")
        sig_name = alert.get("signal_name", "")
        price = alert.get("price")
        price_str = _fmt_price(price) if price is not None else "—"

        items = [
            QTableWidgetItem(time_str),
            QTableWidgetItem(tf),
            QTableWidgetItem(sig_type),
            QTableWidgetItem(sig_name),
            QTableWidgetItem(price_str),
        ]
        alert_color = alert.get("color", "info")
        color_map = {"green": BULL, "red": BEAR, "yellow": NEUTRAL, "info": TEXT}
        row_color = color_map.get(alert_color, TEXT)

        for col, item in enumerate(items):
            if col in (2, 3, 4):
                item.setForeground(QColor(row_color))
            else:
                item.setForeground(QColor(TEXT))
            if col in (2, 3):
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.alert_table.setItem(row, col, item)

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
            with open(self.config.config_file, 'w') as f:
                json.dump(self.config.data, f, indent=2)

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
        self.detail_alerts.clear()
        self.detail_cat_counts.clear()
        self.overview_alerts.clear()
        self.alert_table.setRowCount(0)
        for cat in CAT_COLORS:
            self.badge_labels[cat].setText(f"{cat} 0")
        self.feed_footer.setText("0 alerts")

    # ── CLOSE ──
    def closeEvent(self, e):
        if self.engine:
            self.engine.stop()
        super().closeEvent(e)


if __name__ == "__main__":
    from quant_alert_engine import SingleInstanceLock
    _lock = SingleInstanceLock('data/engine.lock')
    if not _lock.acquire():
        sys.exit(1)
    try:
        app = QApplication(sys.argv)
        win = QuantAlertUI()
        win.show()
        _exit_code = app.exec()
    finally:
        _lock.release()
    sys.exit(_exit_code)
