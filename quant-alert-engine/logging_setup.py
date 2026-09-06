"""
Structured JSON Logging Setup
- JSON lines format for log aggregation
- Context injection (symbol, timeframe, signal_id)
- Compatible with stdlib logging
"""
import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from typing import Any, Dict
import threading


class JSONFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def __init__(self, include_extra: bool = True):
        super().__init__()
        self.include_extra = include_extra
        self._base_fields = {
            "service": "quant-alert-engine",
            "version": "2.0.0",
        }

    def format(self, record: logging.LogRecord) -> str:
        log_obj: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            **self._base_fields,
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_obj["exception"] = self.formatException(record.exc_info)
        if self.include_extra:
            skip = {
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "message", "pathname", "process", "processName", "relativeCreated",
                "thread", "threadName", "exc_info", "exc_text", "stack_info", "getMessage",
                "asctime",
            }
            for key, value in record.__dict__.items():
                if key not in skip:
                    try:
                        json.dumps(value, default=str)
                        log_obj[key] = value
                    except Exception:
                        log_obj[key] = str(value)
        return json.dumps(log_obj, default=str, ensure_ascii=False)


class ContextFilter(logging.Filter):
    """Inject contextual fields into all log records."""

    def __init__(self):
        super().__init__()
        self._context: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def set_context(self, **kwargs):
        with self._lock:
            self._context.update(kwargs)

    def clear_context(self, *keys):
        with self._lock:
            for k in keys:
                self._context.pop(k, None)

    def filter(self, record: logging.LogRecord) -> bool:
        with self._lock:
            for key, value in self._context.items():
                setattr(record, key, value)
        return True


# Global context filter instance
context_filter = ContextFilter()


def setup_logging(
    log_file: str = "logs/quant_alert_engine.log",
    level: str = "INFO",
    json_format: bool = True,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure root logger with JSON file + human console output."""
    # Ensure log dir exists
    import os
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)

    log_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    # Clear existing handlers to avoid duplicate logs on re-init
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    root.setLevel(log_level)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    if json_format:
        file_handler.setFormatter(JSONFormatter())
    else:
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    file_handler.addFilter(context_filter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    console_handler.addFilter(context_filter)
    root.addHandler(console_handler)

    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info("Logging initialized", extra={"log_file": log_file, "level": level, "json": json_format})
    return logger


class AlertLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that automatically adds symbol/timeframe/signal context."""

    def __init__(self, logger: logging.Logger, symbol: str = "", timeframe: str = ""):
        super().__init__(logger, {})
        self.symbol = symbol
        self.timeframe = timeframe

    def process(self, msg: str, kwargs: Dict) -> tuple:
        extra = kwargs.get("extra", {})
        if self.symbol:
            extra["symbol"] = self.symbol
        if self.timeframe:
            extra["timeframe"] = self.timeframe
        kwargs["extra"] = extra
        return msg, kwargs

    def alert_fired(self, signal_name: str, signal_type: str, price: float, color: str = "info", signal_id: str = ""):
        self.info(
            f"ALERT_FIRED: {signal_type} {signal_name}",
            extra={
                "event_type": "alert_fired",
                "signal_name": signal_name,
                "signal_type": signal_type,
                "price": price,
                "color": color,
                "signal_id": signal_id,
            },
        )

    def api_request(self, endpoint: str, symbol: str, timeframe: str, latency_ms: float, status: str = "success"):
        self.info(
            f"API_REQUEST: {endpoint}",
            extra={
                "event_type": "api_request",
                "endpoint": endpoint,
                "symbol": symbol,
                "timeframe": timeframe,
                "latency_ms": latency_ms,
                "status": status,
            },
        )
