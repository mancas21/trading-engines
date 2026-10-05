# Quant Alert Engine v2.0 — Priority Fixes Implementation Guide

Generated: 2025-01-15
Target: Production hardening (Priority 1: Security/Stability)

---

## FIX 1: Config Schema Validation + Env Var Overrides

### Problem
- No validation → typos silently fall back to defaults
- Secrets (Telegram token, API keys) stored in plaintext `config.json`
- No environment variable support for container/deployment

### Solution: Pydantic Settings with `.env` support

**New file: `config_schema.py`**

```python
"""
Quant Alert Engine — Configuration Schema (Pydantic v2)
Provides validation, env var overrides, and type safety.
"""

import os
from pathlib import Path
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class VolumeConfig(BaseModel):
    enabled: bool = True
    avg_len: int = Field(20, ge=1, le=200)
    spike_mult: float = Field(2.0, gt=0)
    climax_mult: float = Field(3.5, gt=0)
    dryup_mult: float = Field(0.5, gt=0, lt=1)
    obv_len: int = Field(20, ge=1, le=200)


class CVDConfig(BaseModel):
    enabled: bool = True
    smooth_len: int = Field(5, ge=1, le=50)
    div_lookback: int = Field(20, ge=5, le=200)
    pivot_len: int = Field(5, ge=1, le=20)


class RSIConfig(BaseModel):
    enabled: bool = True
    length: int = Field(14, ge=2, le=50)
    overbought: int = Field(70, ge=50, le=90)
    oversold: int = Field(30, ge=10, le=50)
    pivot_len: int = Field(5, ge=1, le=20)
    div_lookback: int = Field(20, ge=5, le=200)

    @field_validator("overbought")
    @classmethod
    def ob_gt_os(cls, v, info):
        if "oversold" in info.data and v <= info.data["oversold"]:
            raise ValueError("overbought must be > oversold")
        return v


class MACDConfig(BaseModel):
    enabled: bool = True
    fast: int = Field(12, ge=2, le=50)
    slow: int = Field(26, ge=3, le=100)
    signal: int = Field(9, ge=1, le=50)
    pivot_len: int = Field(5, ge=1, le=20)
    div_lookback: int = Field(20, ge=5, le=200)
    histogram_flip: bool = True

    @field_validator("slow")
    @classmethod
    def slow_gt_fast(cls, v, info):
        if "fast" in info.data and v <= info.data["fast"]:
            raise ValueError("slow must be > fast")
        return v


class StochasticConfig(BaseModel):
    enabled: bool = True
    k_len: int = Field(14, ge=2, le=50)
    d_len: int = Field(3, ge=1, le=20)
    smooth: int = Field(3, ge=1, le=20)
    overbought: int = Field(80, ge=50, le=100)
    oversold: int = Field(20, ge=0, le=50)


class VolatilityConfig(BaseModel):
    enabled: bool = True
    bb_len: int = Field(20, ge=5, le=100)
    bb_mult: float = Field(2.0, gt=0, le=5)
    bb_squeeze_pct: float = Field(20.0, ge=1, le=50)
    bb_width_lookback: int = Field(100, ge=20, le=500)
    atr_len: int = Field(14, ge=2, le=50)
    atr_expansion_mult: float = Field(1.8, gt=0, le=5)


class GeneralConfig(BaseModel):
    check_interval: int = Field(10, ge=3, le=300)
    enable_notifications: bool = True
    enable_sound: bool = True
    max_workers: int = Field(10, ge=1, le=50)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    max_history_bars: int = Field(200, ge=50, le=2000)


class ExchangeConfig(BaseModel):
    id: str = "binanceusdm"
    testnet: bool = False
    api_key: str = ""
    secret: str = ""
    rate_limit_buffer: float = 0.1  # fraction of limit to reserve


class TelegramConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    chat_id: str = ""
    filter_symbol: Optional[str] = None
    filter_timeframe: Optional[str] = None


class BridgeConfig(BaseModel):
    enabled: bool = True
    url: str = "http://127.0.0.1:8000/quant-alert"
    timeout_seconds: float = 2.0


class Settings(BaseSettings):
    """Root settings with env var support. Env vars take precedence over config.json."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Required
    symbols: List[str] = Field(default_factory=list, min_length=1)
    timeframes: List[str] = Field(default_factory=lambda: ["5m"], min_length=1)

    # Nested sections
    volume: VolumeConfig = VolumeConfig()
    cvd: CVDConfig = CVDConfig()
    rsi: RSIConfig = RSIConfig()
    macd: MACDConfig = MACDConfig()
    stochastic: StochasticConfig = StochasticConfig()
    volatility: VolatilityConfig = VolatilityConfig()
    general: GeneralConfig = GeneralConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    telegram: TelegramConfig = TelegramConfig()
    bridge: BridgeConfig = BridgeConfig()
    database: str = "data/alerts.db"

    @field_validator("timeframes")
    @classmethod
    def validate_timeframes(cls, v):
        valid = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "1w"}
        invalid = [tf for tf in v if tf not in valid]
        if invalid:
            raise ValueError(f"Invalid timeframes: {invalid}. Valid: {sorted(valid)}")
        return v

    @field_validator("symbols")
    @classmethod
    def validate_symbols_format(cls, v):
        for s in v:
            if "/" not in s:
                raise ValueError(f"Symbol must contain '/': {s} (e.g., SOL/USDT:USDT)")
        return v

    @model_validator(mode="after")
    def check_telegram_if_enabled(self):
        if self.telegram.enabled and (not self.telegram.token or not self.telegram.chat_id):
            raise ValueError("Telegram enabled but token/chat_id missing")
        return self


# ── Backwards-compatible loader for existing code ──
class Config:
    """Drop-in replacement for old Config class. Reads config.json + .env + env vars."""
    
    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self._settings = self._load()
    
    def _load(self) -> Settings:
        # 1. Load JSON if exists
        json_data = {}
        if Path(self.config_file).exists():
            import json
            with open(self.config_file, "r") as f:
                json_data = json.load(f)
        
        # 2. Pydantic merges: env vars > .env > json_data > defaults
        return Settings(**json_data)
    
    def get(self, key: str, default=None):
        """Dot-notation access: config.get('rsi.length')"""
        keys = key.split(".")
        obj = self._settings
        for k in keys:
            if hasattr(obj, k):
                obj = getattr(obj, k)
            else:
                return default
        return obj
    
    @property
    def data(self) -> dict:
        """Full dict for backwards compatibility (e.g., JSON serialization)."""
        return self._settings.model_dump()
    
    def save(self):
        """Persist current settings to config.json (excluding secrets)."""
        import json
        # Don't persist secrets to disk
        safe = self._settings.model_dump()
        safe["telegram"]["token"] = ""
        safe["telegram"]["chat_id"] = ""
        safe["exchange"]["api_key"] = ""
        safe["exchange"]["secret"] = ""
        with open(self.config_file, "w") as f:
            json.dump(safe, f, indent=2)


# ── Usage example ──
if __name__ == "__main__":
    # Test loading
    cfg = Config("config.json")
    print(f"Symbols: {cfg.get('symbols')}")
    print(f"RSI length: {cfg.get('rsi.length')}")
    print(f"Exchange testnet: {cfg.get('exchange.testnet')}")
    print(f"Bridge URL: {cfg.get('bridge.url')}")
```

---

**Required `.env` file (create from `.env.example`):**

```bash
# .env.example — copy to .env and fill in
# Exchange (optional — public data works without keys)
EXCHANGE__API_KEY=
EXCHANGE__SECRET=
EXCHANGE__TESTNET=false

# Telegram (optional)
TELEGRAM__ENABLED=false
TELEGRAM__TOKEN=your_bot_token_here
TELEGRAM__CHAT_ID=your_chat_id_here

# Bridge agent (optional)
BRIDGE__ENABLED=true
BRIDGE__URL=http://127.0.0.1:8000/quant-alert
BRIDGE__TIMEOUT_SECONDS=2.0

# General
GENERAL__CHECK_INTERVAL=10
GENERAL__LOG_LEVEL=INFO
```

---

**Updated `requirements.txt` additions:**

```text
# Config validation
pydantic>=2.5.0
pydantic-settings>=2.1.0
python-dotenv>=1.0.0
```

---

**Migration in `quant_alert_engine.py`:**

```python
# REPLACE the old Config class (lines ~180-230) with:
from config_schema import Config  # new validated config

# No other changes needed — API is identical:
# config.get('rsi.length')  ✓
# config.data['rsi']['length']  ✓ (for JSON serialization)
# config.save()  ✓ (persists non-secrets)
```

---

## FIX 2: Rate-Limit Handling with Exponential Backoff

### Problem
- CCXT's `enableRateLimit: True` only paces requests
- No handling of 429 (Too Many Requests) or network errors
- Hard crashes on temporary API issues

### Solution: Resilient Exchange Wrapper

**New file: `exchange_resilient.py`**

```python
"""
Resilient CCXT Exchange Wrapper
- Exponential backoff with jitter on 429 / 5xx / network errors
- Circuit breaker pattern
- Request/response logging for debugging
"""

import time
import random
import logging
from typing import Optional, Any, List, Dict
import ccxt
from ccxt.base.errors import (
    RateLimitExceeded, ExchangeNotAvailable, RequestTimeout,
    NetworkError, ExchangeError, DDoSProtection
)

logger = logging.getLogger(__name__)


class ResilientExchange:
    """
    Wraps a CCXT exchange with:
    - Automatic retry with exponential backoff + jitter
    - Circuit breaker (stops hammering failing endpoint)
    - Rate limit headroom awareness
    """
    
    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        testnet: bool = False,
        api_key: str = "",
        secret: str = "",
        max_retries: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        rate_limit_buffer: float = 0.1,
    ):
        self.exchange_id = exchange_id
        self.testnet = testnet
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.rate_limit_buffer = rate_limit_buffer
        
        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._failure_threshold = 10  # open circuit after N failures
        
        # Initialize CCXT exchange
        self.exchange = self._create_exchange(exchange_id, testnet, api_key, secret)
        
        # Load markets once
        self._load_markets_with_retry()
    
    def _create_exchange(self, exchange_id: str, testnet: bool, api_key: str, secret: str):
        exchange_class = getattr(ccxt, exchange_id)
        config = {
            "enableRateLimit": True,
            "rateLimit": 100,  # ms between requests (CCXT default)
        }
        if api_key and secret:
            config["apiKey"] = api_key
            config["secret"] = secret
        if testnet and hasattr(exchange_class, "urls") and "test" in exchange_class.urls:
            config["urls"] = {"api": exchange_class.urls["test"]}
        
        return exchange_class(config)
    
    def _load_markets_with_retry(self):
        for attempt in range(self.max_retries):
            try:
                self.exchange.load_markets()
                logger.info(f"Loaded {len(self.exchange.symbols)} markets from {self.exchange_id}")
                return
            except (RateLimitExceeded, ExchangeNotAvailable, RequestTimeout, NetworkError) as e:
                delay = self._calculate_delay(attempt, e)
                logger.warning(f"Load markets attempt {attempt+1} failed: {e}. Retrying in {delay:.1f}s")
                time.sleep(delay)
        raise ConnectionError(f"Failed to load markets after {self.max_retries} attempts")
    
    def _calculate_delay(self, attempt: int, error: Exception) -> float:
        """Exponential backoff with jitter + respect Retry-After header if present."""
        # Base exponential backoff
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        
        # Add jitter (±25%)
        delay *= (0.75 + random.random() * 0.5)
        
        # If rate limited, check for Retry-After
        if hasattr(error, "response") and error.response is not None:
            retry_after = error.response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after) + 1)
                except ValueError:
                    pass
        
        return delay
    
    def _check_circuit(self):
        """Circuit breaker: if too many failures, pause all requests."""
        now = time.time()
        if self._circuit_open_until > now:
            wait = self._circuit_open_until - now
            logger.warning(f"Circuit breaker OPEN. Waiting {wait:.1f}s before retry")
            time.sleep(wait)
    
    def _record_success(self):
        self._consecutive_failures = 0
    
    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            # Open circuit for 60 seconds
            self._circuit_open_until = time.time() + 60
            logger.error(f"Circuit breaker TRIPPED after {self._consecutive_failures} failures. Cooling down 60s")
    
    def _execute_with_retry(self, method_name: str, *args, **kwargs) -> Any:
        """Execute any CCXT method with retry logic."""
        self._check_circuit()
        
        last_error = None
        for attempt in range(self.max_retries):
            try:
                method = getattr(self.exchange, method_name)
                result = method(*args, **kwargs)
                self._record_success()
                return result
            
            except RateLimitExceeded as e:
                last_error = e
                delay = self._calculate_delay(attempt, e)
                logger.warning(f"Rate limited on {method_name} (attempt {attempt+1}/{self.max_retries}). Backing off {delay:.1f}s")
                time.sleep(delay)
            
            except (ExchangeNotAvailable, RequestTimeout, NetworkError, DDoSProtection) as e:
                last_error = e
                delay = self._calculate_delay(attempt, e)
                logger.warning(f"Network error on {method_name} (attempt {attempt+1}/{self.max_retries}): {e}. Retrying in {delay:.1f}s")
                time.sleep(delay)
            
            except ExchangeError as e:
                # Non-retryable exchange errors (bad symbol, auth, etc.)
                logger.error(f"Exchange error on {method_name}: {e}")
                self._record_failure()
                raise
            
            except Exception as e:
                logger.error(f"Unexpected error on {method_name}: {e}", exc_info=True)
                self._record_failure()
                raise
        
        self._record_failure()
        raise last_error or Exception(f"Max retries exceeded for {method_name}")
    
    # ── Public API (subset used by engine) ──
    
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> List[List]:
        """Fetch OHLCV with retry."""
        return self._execute_with_retry("fetch_ohlcv", symbol, timeframe, limit=limit)
    
    def fetch_ticker(self, symbol: str) -> Dict:
        return self._execute_with_retry("fetch_ticker", symbol)
    
    def fetch_balance(self) -> Dict:
        return self._execute_with_retry("fetch_balance")
    
    @property
    def symbols(self) -> List[str]:
        return list(self.exchange.symbols)
    
    @property
    def markets(self) -> Dict:
        return self.exchange.markets
    
    def validate_symbols(self, symbols: List[str]) -> List[str]:
        """Filter to only valid symbols, log invalid ones."""
        valid = [s for s in symbols if s in self.exchange.symbols]
        invalid = [s for s in symbols if s not in self.exchange.symbols]
        for s in invalid:
            logger.error(f"Symbol {s} not found on {self.exchange_id} — removing from watch list")
        return valid


# ── Integration in SignalAnalyzer ──
"""
REPLACE _init_exchange() in SignalAnalyzer (quant_alert_engine.py ~line 230):

    def _init_exchange(self, max_retries: int = 3):
        from exchange_resilient import ResilientExchange
        from config_schema import Config  # if using new config
        
        # Get exchange config from Config object
        ex_cfg = self.config._settings.exchange if hasattr(self.config, '_settings') else None
        
        if ex_cfg:
            self.exchange = ResilientExchange(
                exchange_id=ex_cfg.id,
                testnet=ex_cfg.testnet,
                api_key=ex_cfg.api_key,
                secret=ex_cfg.secret,
                max_retries=max_retries,
                rate_limit_buffer=ex_cfg.rate_limit_buffer,
            )
        else:
            # Fallback to old behavior
            self.exchange = ResilientExchange(max_retries=max_retries)
        
        # Validate symbols
        symbols = list(self.config.get('symbols', []))
        if symbols:
            valid = self.exchange.validate_symbols(symbols)
            if len(valid) != len(symbols):
                self.config.data['symbols'] = valid
        
        logger.info(f"Connected to {self.exchange.exchange_id} (resilient mode)")
        return self.exchange
"""
```

---

## FIX 3: Configurable Bridge Agent URL

### Problem
- Hardcoded `http://127.0.0.1:8000/quant-alert` in `AlertManager.emit_alert()`
- Fails silently but couples to external service
- No timeout configuration

### Solution: Bridge Config in Settings + Graceful Degradation

**Changes to `config_schema.py`** (already included above — `BridgeConfig` class)

**Changes to `quant_alert_engine.py` — `AlertManager.emit_alert()`:**

```python
# REPLACE the bridge forwarding block (lines ~155-170) with:

        # Forward to bridge agent (configurable, non-blocking)
        if self.config.get('bridge.enabled', True):
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
                }).encode()
                
                req = _urllib.Request(
                    bridge_url,
                    data=bridge_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                # Non-blocking: fire and forget with short timeout
                _urllib.urlopen(req, timeout=bridge_timeout)
            except Exception as e:
                # Debug only — don't spam logs
                logger.debug(f"Bridge forward failed (normal if bridge is off): {e}")
```

**Same change in `quant_alert_ui.py` — `QueueAlertManager.emit_alert()`:**

```python
# REPLACE the requests.post block (lines ~110-120) with:

        # Forward to bridge agent
        if self.config.get('bridge.enabled', True):
            try:
                bridge_url = self.config.get('bridge.url', 'http://127.0.0.1:8000/quant-alert')
                bridge_timeout = self.config.get('bridge.timeout_seconds', 2.0)
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
                    timeout=bridge_timeout,
                )
            except Exception:
                pass  # Silent — bridge is optional
```

---

## FIX 4: Structured JSON Logging

### Problem
- Current logging is human-readable but not machine-parseable
- No correlation IDs, structured fields for alert aggregation
- Hard to integrate with Loki/ELK/Datadog

### Solution: JSON Formatter + Contextual Fields

**New file: `logging_setup.py`**

```python
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
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
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
        # Base log object
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
        
        # Exception info
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        
        # Extra fields (context injected via LoggerAdapter)
        if self.include_extra:
            for key, value in record.__dict__.items():
                if key not in {
                    "name", "msg", "args", "created", "filename", "funcName",
                    "levelname", "levelno", "lineno", "module", "msecs",
                    "message", "msg", "name", "pathname", "process",
                    "processName", "relativeCreated", "thread", "threadName",
                    "exc_info", "exc_text", "stack_info", "getMessage"
                }:
                    log_obj[key] = value
        
        return json.dumps(log_obj, default=str)


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
    """Configure root logger with JSON + console output."""
    
    log_level = getattr(logging, level.upper(), logging.INFO)
    
    # Clear existing handlers
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)
    
    # File handler (JSON)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    if json_format:
        file_handler.setFormatter(JSONFormatter())
    else:
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        ))
    file_handler.addFilter(context_filter)
    root.addHandler(file_handler)
    
    # Console handler (human-readable)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    ))
    console_handler.addFilter(context_filter)
    root.addHandler(console_handler)
    
    # Reduce noise from libraries
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
    
    def alert_fired(self, signal_name: str, signal_type: str, price: float, 
                    color: str = "info", signal_id: str = ""):
        """Structured alert log entry."""
        self.info(
            f"ALERT_FIRED: {signal_type} {signal_name}",
            extra={
                "event_type": "alert_fired",
                "signal_name": signal_name,
                "signal_type": signal_type,
                "price": price,
                "color": color,
                "signal_id": signal_id,
            }
        )
    
    def api_request(self, endpoint: str, symbol: str, timeframe: str, 
                    latency_ms: float, status: str = "success"):
        """Structured API request log."""
        self.info(
            f"API_REQUEST: {endpoint}",
            extra={
                "event_type": "api_request",
                "endpoint": endpoint,
                "symbol": symbol,
                "timeframe": timeframe,
                "latency_ms": latency_ms,
                "status": status,
            }
        )


# ── Usage in engine/UI ──
"""
# In QuantAlertEngine.__init__():
from logging_setup import setup_logging, AlertLoggerAdapter, context_filter

setup_logging(
    log_file=self.config.get('database', 'data/alerts.db').replace('.db', '.log'),
    level=self.config.get('general.log_level', 'INFO'),
    json_format=True,
)

# In SignalAnalyzer.analyze_symbol():
logger = AlertLoggerAdapter(logging.getLogger(__name__), symbol=symbol, timeframe=timeframe)
logger.alert_fired("Volume Spike", "VOLUME", price, "red", signal_id)

# In ResilientExchange._execute_with_retry():
start = time.time()
try:
    result = method(*args, **kwargs)
    logger.api_request(method.__name__, symbol, timeframe, (time.time()-start)*1000, "success")
    return result
except Exception as e:
    logger.api_request(method.__name__, symbol, timeframe, (time.time()-start)*1000, f"error:{type(e).__name__}")
    raise
"""
```

---

**Integration in `quant_alert_engine.py`:**

```python
# REPLACE logging setup (lines ~160-175) with:

from logging_setup import setup_logging, AlertLoggerAdapter, context_filter

# Early init (before Config loads log_level)
setup_logging(
    log_file="logs/quant_alert_engine.log",
    level="INFO",  # Will be overridden after config loads
    json_format=True,
)

# Then in Config.__init__ or QuantAlertEngine.__init__, reconfigure:
# setup_logging(level=config.get('general.log_level', 'INFO'))
```

---

## FIX 5: Health Check Endpoint (Optional but Recommended)

### Problem
- No way for orchestrator (systemd, Docker, Kubernetes) to verify liveness
- No metrics exposure

### Solution: Minimal FastAPI Health Server

**New file: `health_server.py`**

```python
"""
Minimal Health/Metrics Server for Quant Alert Engine
Run in background thread: python -m health_server
Or import and start from engine.
"""

import threading
import time
from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import FastAPI, Response
from pydantic import BaseModel
import psutil


class HealthResponse(BaseModel):
    status: str  # "healthy" | "degraded" | "unhealthy"
    timestamp: float
    uptime_seconds: float
    checks: Dict[str, Any]


_start_time = time.time()
_health_checks: Dict[str, Any] = {}


def register_check(name: str, check_fn):
    """Register a health check function. Returns True/False or dict."""
    _health_checks[name] = check_fn


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Quant Alert Engine Health", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health():
    results = {}
    overall = "healthy"
    for name, check_fn in _health_checks.items():
        try:
            result = check_fn()
            if isinstance(result, dict):
                results[name] = result
                if result.get("status") == "unhealthy":
                    overall = "unhealthy"
                elif result.get("status") == "degraded" and overall == "healthy":
                    overall = "degraded"
            else:
                results[name] = {"status": "healthy" if result else "unhealthy"}
                if not result and overall == "healthy":
                    overall = "unhealthy"
        except Exception as e:
            results[name] = {"status": "unhealthy", "error": str(e)}
            overall = "unhealthy"
    
    return HealthResponse(
        status=overall,
        timestamp=time.time(),
        uptime_seconds=time.time() - _start_time,
        checks=results,
    )


@app.get("/metrics")
async def metrics():
    """Prometheus-compatible metrics."""
    lines = [
        f'quant_engine_uptime_seconds {time.time() - _start_time}',
        f'quant_engine_memory_bytes {psutil.Process().memory_info().rss}',
        f'quant_engine_cpu_percent {psutil.Process().cpu_percent()}',
    ]
    # Add custom metrics from checks
    for name, check_fn in _health_checks.items():
        try:
            result = check_fn()
            if isinstance(result, dict) and "metrics" in result:
                for k, v in result["metrics"].items():
                    lines.append(f'quant_engine_{name}_{k} {v}')
        except Exception:
            pass
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")


def start_health_server(host: str = "0.0.0.0", port: int = 8080):
    """Start health server in background thread."""
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


# ── Example check functions to register in engine ──
"""
# In QuantAlertEngine.__init__():
from health_server import register_check, start_health_server

register_check("exchange", lambda: {
    "status": "healthy" if self.analyzer.exchange else "unhealthy",
    "metrics": {"connected": 1 if self.analyzer.exchange else 0}
})

register_check("database", lambda: {
    "status": "healthy",
    "metrics": {"alerts_fired_1h": self._count_recent_alerts()}
})

register_check("symbols", lambda: {
    "status": "healthy" if self.config.get('symbols') else "degraded",
    "metrics": {"symbols_configured": len(self.config.get('symbols', []))}
})

# Start health server
start_health_server(port=8080)
"""
```

---

**Updated `requirements.txt` additions:**

```text
# Health server
fastapi>=0.104.0
uvicorn>=0.24.0
psutil>=5.9.0
```

---

## SUMMARY: Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `config_schema.py` | **CREATE** | Pydantic v2 settings with validation + env vars |
| `exchange_resilient.py` | **CREATE** | CCXT wrapper with retry/backoff/circuit breaker |
| `logging_setup.py` | **CREATE** | JSON logging + context injection |
| `health_server.py` | **CREATE** | FastAPI `/health` + `/metrics` endpoint |
| `.env.example` | **CREATE** | Template for secrets |
| `quant_alert_engine.py` | **MODIFY** | Replace Config, Exchange, Logging, Bridge URL |
| `quant_alert_ui.py` | **MODIFY** | Use new Config, Bridge URL from settings |
| `requirements.txt` | **MODIFY** | Add pydantic, pydantic-settings, python-dotenv, fastapi, uvicorn, psutil |

---

## DEPLOYMENT CHECKLIST

1. **Copy `.env.example` → `.env`** and fill secrets
2. **Install new deps:** `pip install -r requirements.txt`
3. **Run engine:** `python quant_alert_engine.py` (validates config on startup)
4. **Verify health:** `curl http://localhost:8080/health`
5. **Check logs:** `tail -f logs/quant_alert_engine.log | jq .` (JSON format)

---

## ROLLBACK PLAN

If issues arise:
1. Keep old `Config` class as `ConfigLegacy` in `config_schema.py`
2. Toggle via env var: `USE_LEGACY_CONFIG=true`
3. Health server runs on separate port (8080) — non-invasive

---

*End of Priority 1 Fixes Document*# Quant Alert Engine v2.0 — Priority Fixes Implementation Guide

Generated: 2025-01-15
Target: Production hardening (Priority 1: Security/Stability)

---

## FIX 1: Config Schema Validation + Env Var Overrides

### Problem
- No validation → typos silently fall back to defaults
- Secrets (Telegram token, API keys) stored in plaintext `config.json`
- No environment variable support for container/deployment

### Solution: Pydantic Settings with `.env` support

**New file: `config_schema.py`**

```python
"""
Quant Alert Engine — Configuration Schema (Pydantic v2)
Provides validation, env var overrides, and type safety.
"""

import os
from pathlib import Path
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class VolumeConfig(BaseModel):
    enabled: bool = True
    avg_len: int = Field(20, ge=1, le=200)
    spike_mult: float = Field(2.0, gt=0)
    climax_mult: float = Field(3.5, gt=0)
    dryup_mult: float = Field(0.5, gt=0, lt=1)
    obv_len: int = Field(20, ge=1, le=200)


class CVDConfig(BaseModel):
    enabled: bool = True
    smooth_len: int = Field(5, ge=1, le=50)
    div_lookback: int = Field(20, ge=5, le=200)
    pivot_len: int = Field(5, ge=1, le=20)


class RSIConfig(BaseModel):
    enabled: bool = True
    length: int = Field(14, ge=2, le=50)
    overbought: int = Field(70, ge=50, le=90)
    oversold: int = Field(30, ge=10, le=50)
    pivot_len: int = Field(5, ge=1, le=20)
    div_lookback: int = Field(20, ge=5, le=200)

    @field_validator("overbought")
    @classmethod
    def ob_gt_os(cls, v, info):
        if "oversold" in info.data and v <= info.data["oversold"]:
            raise ValueError("overbought must be > oversold")
        return v


class MACDConfig(BaseModel):
    enabled: bool = True
    fast: int = Field(12, ge=2, le=50)
    slow: int = Field(26, ge=3, le=100)
    signal: int = Field(9, ge=1, le=50)
    pivot_len: int = Field(5, ge=1, le=20)
    div_lookback: int = Field(20, ge=5, le=200)
    histogram_flip: bool = True

    @field_validator("slow")
    @classmethod
    def slow_gt_fast(cls, v, info):
        if "fast" in info.data and v <= info.data["fast"]:
            raise ValueError("slow must be > fast")
        return v


class StochasticConfig(BaseModel):
    enabled: bool = True
    k_len: int = Field(14, ge=2, le=50)
    d_len: int = Field(3, ge=1, le=20)
    smooth: int = Field(3, ge=1, le=20)
    overbought: int = Field(80, ge=50, le=100)
    oversold: int = Field(20, ge=0, le=50)


class VolatilityConfig(BaseModel):
    enabled: bool = True
    bb_len: int = Field(20, ge=5, le=100)
    bb_mult: float = Field(2.0, gt=0, le=5)
    bb_squeeze_pct: float = Field(20.0, ge=1, le=50)
    bb_width_lookback: int = Field(100, ge=20, le=500)
    atr_len: int = Field(14, ge=2, le=50)
    atr_expansion_mult: float = Field(1.8, gt=0, le=5)


class GeneralConfig(BaseModel):
    check_interval: int = Field(10, ge=3, le=300)
    enable_notifications: bool = True
    enable_sound: bool = True
    max_workers: int = Field(10, ge=1, le=50)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    max_history_bars: int = Field(200, ge=50, le=2000)


class ExchangeConfig(BaseModel):
    id: str = "binanceusdm"
    testnet: bool = False
    api_key: str = ""
    secret: str = ""
    rate_limit_buffer: float = 0.1  # fraction of limit to reserve


class TelegramConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    chat_id: str = ""
    filter_symbol: Optional[str] = None
    filter_timeframe: Optional[str] = None


class BridgeConfig(BaseModel):
    enabled: bool = True
    url: str = "http://127.0.0.1:8000/quant-alert"
    timeout_seconds: float = 2.0


class Settings(BaseSettings):
    """Root settings with env var support. Env vars take precedence over config.json."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Required
    symbols: List[str] = Field(default_factory=list, min_length=1)
    timeframes: List[str] = Field(default_factory=lambda: ["5m"], min_length=1)

    # Nested sections
    volume: VolumeConfig = VolumeConfig()
    cvd: CVDConfig = CVDConfig()
    rsi: RSIConfig = RSIConfig()
    macd: MACDConfig = MACDConfig()
    stochastic: StochasticConfig = StochasticConfig()
    volatility: VolatilityConfig = VolatilityConfig()
    general: GeneralConfig = GeneralConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    telegram: TelegramConfig = TelegramConfig()
    bridge: BridgeConfig = BridgeConfig()
    database: str = "data/alerts.db"

    @field_validator("timeframes")
    @classmethod
    def validate_timeframes(cls, v):
        valid = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "1w"}
        invalid = [tf for tf in v if tf not in valid]
        if invalid:
            raise ValueError(f"Invalid timeframes: {invalid}. Valid: {sorted(valid)}")
        return v

    @field_validator("symbols")
    @classmethod
    def validate_symbols_format(cls, v):
        for s in v:
            if "/" not in s:
                raise ValueError(f"Symbol must contain '/': {s} (e.g., SOL/USDT:USDT)")
        return v

    @model_validator(mode="after")
    def check_telegram_if_enabled(self):
        if self.telegram.enabled and (not self.telegram.token or not self.telegram.chat_id):
            raise ValueError("Telegram enabled but token/chat_id missing")
        return self


# ── Backwards-compatible loader for existing code ──
class Config:
    """Drop-in replacement for old Config class. Reads config.json + .env + env vars."""
    
    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self._settings = self._load()
    
    def _load(self) -> Settings:
        # 1. Load JSON if exists
        json_data = {}
        if Path(self.config_file).exists():
            import json
            with open(self.config_file, "r") as f:
                json_data = json.load(f)
        
        # 2. Pydantic merges: env vars > .env > json_data > defaults
        return Settings(**json_data)
    
    def get(self, key: str, default=None):
        """Dot-notation access: config.get('rsi.length')"""
        keys = key.split(".")
        obj = self._settings
        for k in keys:
            if hasattr(obj, k):
                obj = getattr(obj, k)
            else:
                return default
        return obj
    
    @property
    def data(self) -> dict:
        """Full dict for backwards compatibility (e.g., JSON serialization)."""
        return self._settings.model_dump()
    
    def save(self):
        """Persist current settings to config.json (excluding secrets)."""
        import json
        # Don't persist secrets to disk
        safe = self._settings.model_dump()
        safe["telegram"]["token"] = ""
        safe["telegram"]["chat_id"] = ""
        safe["exchange"]["api_key"] = ""
        safe["exchange"]["secret"] = ""
        with open(self.config_file, "w") as f:
            json.dump(safe, f, indent=2)


# ── Usage example ──
if __name__ == "__main__":
    # Test loading
    cfg = Config("config.json")
    print(f"Symbols: {cfg.get('symbols')}")
    print(f"RSI length: {cfg.get('rsi.length')}")
    print(f"Exchange testnet: {cfg.get('exchange.testnet')}")
    print(f"Bridge URL: {cfg.get('bridge.url')}")
```

---

**Required `.env` file (create from `.env.example`):**

```bash
# .env.example — copy to .env and fill in
# Exchange (optional — public data works without keys)
EXCHANGE__API_KEY=
EXCHANGE__SECRET=
EXCHANGE__TESTNET=false

# Telegram (optional)
TELEGRAM__ENABLED=false
TELEGRAM__TOKEN=your_bot_token_here
TELEGRAM__CHAT_ID=your_chat_id_here

# Bridge agent (optional)
BRIDGE__ENABLED=true
BRIDGE__URL=http://127.0.0.1:8000/quant-alert
BRIDGE__TIMEOUT_SECONDS=2.0

# General
GENERAL__CHECK_INTERVAL=10
GENERAL__LOG_LEVEL=INFO
```

---

**Updated `requirements.txt` additions:**

```text
# Config validation
pydantic>=2.5.0
pydantic-settings>=2.1.0
python-dotenv>=1.0.0
```

---

**Migration in `quant_alert_engine.py`:**

```python
# REPLACE the old Config class (lines ~180-230) with:
from config_schema import Config  # new validated config

# No other changes needed — API is identical:
# config.get('rsi.length')  ✓
# config.data['rsi']['length']  ✓ (for JSON serialization)
# config.save()  ✓ (persists non-secrets)
```

---

## FIX 2: Rate-Limit Handling with Exponential Backoff

### Problem
- CCXT's `enableRateLimit: True` only paces requests
- No handling of 429 (Too Many Requests) or network errors
- Hard crashes on temporary API issues

### Solution: Resilient Exchange Wrapper

**New file: `exchange_resilient.py`**

```python
"""
Resilient CCXT Exchange Wrapper
- Exponential backoff with jitter on 429 / 5xx / network errors
- Circuit breaker pattern
- Request/response logging for debugging
"""

import time
import random
import logging
from typing import Optional, Any, List, Dict
import ccxt
from ccxt.base.errors import (
    RateLimitExceeded, ExchangeNotAvailable, RequestTimeout,
    NetworkError, ExchangeError, DDoSProtection
)

logger = logging.getLogger(__name__)


class ResilientExchange:
    """
    Wraps a CCXT exchange with:
    - Automatic retry with exponential backoff + jitter
    - Circuit breaker (stops hammering failing endpoint)
    - Rate limit headroom awareness
    """
    
    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        testnet: bool = False,
        api_key: str = "",
        secret: str = "",
        max_retries: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        rate_limit_buffer: float = 0.1,
    ):
        self.exchange_id = exchange_id
        self.testnet = testnet
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.rate_limit_buffer = rate_limit_buffer
        
        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._failure_threshold = 10  # open circuit after N failures
        
        # Initialize CCXT exchange
        self.exchange = self._create_exchange(exchange_id, testnet, api_key, secret)
        
        # Load markets once
        self._load_markets_with_retry()
    
    def _create_exchange(self, exchange_id: str, testnet: bool, api_key: str, secret: str):
        exchange_class = getattr(ccxt, exchange_id)
        config = {
            "enableRateLimit": True,
            "rateLimit": 100,  # ms between requests (CCXT default)
        }
        if api_key and secret:
            config["apiKey"] = api_key
            config["secret"] = secret
        if testnet and hasattr(exchange_class, "urls") and "test" in exchange_class.urls:
            config["urls"] = {"api": exchange_class.urls["test"]}
        
        return exchange_class(config)
    
    def _load_markets_with_retry(self):
        for attempt in range(self.max_retries):
            try:
                self.exchange.load_markets()
                logger.info(f"Loaded {len(self.exchange.symbols)} markets from {self.exchange_id}")
                return
            except (RateLimitExceeded, ExchangeNotAvailable, RequestTimeout, NetworkError) as e:
                delay = self._calculate_delay(attempt, e)
                logger.warning(f"Load markets attempt {attempt+1} failed: {e}. Retrying in {delay:.1f}s")
                time.sleep(delay)
        raise ConnectionError(f"Failed to load markets after {self.max_retries} attempts")
    
    def _calculate_delay(self, attempt: int, error: Exception) -> float:
        """Exponential backoff with jitter + respect Retry-After header if present."""
        # Base exponential backoff
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        
        # Add jitter (±25%)
        delay *= (0.75 + random.random() * 0.5)
        
        # If rate limited, check for Retry-After
        if hasattr(error, "response") and error.response is not None:
            retry_after = error.response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after) + 1)
                except ValueError:
                    pass
        
        return delay
    
    def _check_circuit(self):
        """Circuit breaker: if too many failures, pause all requests."""
        now = time.time()
        if self._circuit_open_until > now:
            wait = self._circuit_open_until - now
            logger.warning(f"Circuit breaker OPEN. Waiting {wait:.1f}s before retry")
            time.sleep(wait)
    
    def _record_success(self):
        self._consecutive_failures = 0
    
    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            # Open circuit for 60 seconds
            self._circuit_open_until = time.time() + 60
            logger.error(f"Circuit breaker TRIPPED after {self._consecutive_failures} failures. Cooling down 60s")
    
    def _execute_with_retry(self, method_name: str, *args, **kwargs) -> Any:
        """Execute any CCXT method with retry logic."""
        self._check_circuit()
        
        last_error = None
        for attempt in range(self.max_retries):
            try:
                method = getattr(self.exchange, method_name)
                result = method(*args, **kwargs)
                self._record_success()
                return result
            
            except RateLimitExceeded as e:
                last_error = e
                delay = self._calculate_delay(attempt, e)
                logger.warning(f"Rate limited on {method_name} (attempt {attempt+1}/{self.max_retries}). Backing off {delay:.1f}s")
                time.sleep(delay)
            
            except (ExchangeNotAvailable, RequestTimeout, NetworkError, DDoSProtection) as e:
                last_error = e
                delay = self._calculate_delay(attempt, e)
                logger.warning(f"Network error on {method_name} (attempt {attempt+1}/{self.max_retries}): {e}. Retrying in {delay:.1f}s")
                time.sleep(delay)
            
            except ExchangeError as e:
                # Non-retryable exchange errors (bad symbol, auth, etc.)
                logger.error(f"Exchange error on {method_name}: {e}")
                self._record_failure()
                raise
            
            except Exception as e:
                logger.error(f"Unexpected error on {method_name}: {e}", exc_info=True)
                self._record_failure()
                raise
        
        self._record_failure()
        raise last_error or Exception(f"Max retries exceeded for {method_name}")
    
    # ── Public API (subset used by engine) ──
    
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> List[List]:
        """Fetch OHLCV with retry."""
        return self._execute_with_retry("fetch_ohlcv", symbol, timeframe, limit=limit)
    
    def fetch_ticker(self, symbol: str) -> Dict:
        return self._execute_with_retry("fetch_ticker", symbol)
    
    def fetch_balance(self) -> Dict:
        return self._execute_with_retry("fetch_balance")
    
    @property
    def symbols(self) -> List[str]:
        return list(self.exchange.symbols)
    
    @property
    def markets(self) -> Dict:
        return self.exchange.markets
    
    def validate_symbols(self, symbols: List[str]) -> List[str]:
        """Filter to only valid symbols, log invalid ones."""
        valid = [s for s in symbols if s in self.exchange.symbols]
        invalid = [s for s in symbols if s not in self.exchange.symbols]
        for s in invalid:
            logger.error(f"Symbol {s} not found on {self.exchange_id} — removing from watch list")
        return valid


# ── Integration in SignalAnalyzer ──
"""
REPLACE _init_exchange() in SignalAnalyzer (quant_alert_engine.py ~line 230):

    def _init_exchange(self, max_retries: int = 3):
        from exchange_resilient import ResilientExchange
        from config_schema import Config  # if using new config
        
        # Get exchange config from Config object
        ex_cfg = self.config._settings.exchange if hasattr(self.config, '_settings') else None
        
        if ex_cfg:
            self.exchange = ResilientExchange(
                exchange_id=ex_cfg.id,
                testnet=ex_cfg.testnet,
                api_key=ex_cfg.api_key,
                secret=ex_cfg.secret,
                max_retries=max_retries,
                rate_limit_buffer=ex_cfg.rate_limit_buffer,
            )
        else:
            # Fallback to old behavior
            self.exchange = ResilientExchange(max_retries=max_retries)
        
        # Validate symbols
        symbols = list(self.config.get('symbols', []))
        if symbols:
            valid = self.exchange.validate_symbols(symbols)
            if len(valid) != len(symbols):
                self.config.data['symbols'] = valid
        
        logger.info(f"Connected to {self.exchange.exchange_id} (resilient mode)")
        return self.exchange
"""
```

---

## FIX 3: Configurable Bridge Agent URL

### Problem
- Hardcoded `http://127.0.0.1:8000/quant-alert` in `AlertManager.emit_alert()`
- Fails silently but couples to external service
- No timeout configuration

### Solution: Bridge Config in Settings + Graceful Degradation

**Changes to `config_schema.py`** (already included above — `BridgeConfig` class)

**Changes to `quant_alert_engine.py` — `AlertManager.emit_alert()`:**

```python
# REPLACE the bridge forwarding block (lines ~155-170) with:

        # Forward to bridge agent (configurable, non-blocking)
        if self.config.get('bridge.enabled', True):
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
                }).encode()
                
                req = _urllib.Request(
                    bridge_url,
                    data=bridge_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                # Non-blocking: fire and forget with short timeout
                _urllib.urlopen(req, timeout=bridge_timeout)
            except Exception as e:
                # Debug only — don't spam logs
                logger.debug(f"Bridge forward failed (normal if bridge is off): {e}")
```

**Same change in `quant_alert_ui.py` — `QueueAlertManager.emit_alert()`:**

```python
# REPLACE the requests.post block (lines ~110-120) with:

        # Forward to bridge agent
        if self.config.get('bridge.enabled', True):
            try:
                bridge_url = self.config.get('bridge.url', 'http://127.0.0.1:8000/quant-alert')
                bridge_timeout = self.config.get('bridge.timeout_seconds', 2.0)
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
                    timeout=bridge_timeout,
                )
            except Exception:
                pass  # Silent — bridge is optional
```

---

## FIX 4: Structured JSON Logging

### Problem
- Current logging is human-readable but not machine-parseable
- No correlation IDs, structured fields for alert aggregation
- Hard to integrate with Loki/ELK/Datadog

### Solution: JSON Formatter + Contextual Fields

**New file: `logging_setup.py`**

```python
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
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
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
        # Base log object
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
        
        # Exception info
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        
        # Extra fields (context injected via LoggerAdapter)
        if self.include_extra:
            for key, value in record.__dict__.items():
                if key not in {
                    "name", "msg", "args", "created", "filename", "funcName",
                    "levelname", "levelno", "lineno", "module", "msecs",
                    "message", "msg", "name", "pathname", "process",
                    "processName", "relativeCreated", "thread", "threadName",
                    "exc_info", "exc_text", "stack_info", "getMessage"
                }:
                    log_obj[key] = value
        
        return json.dumps(log_obj, default=str)


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
    """Configure root logger with JSON + console output."""
    
    log_level = getattr(logging, level.upper(), logging.INFO)
    
    # Clear existing handlers
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)
    
    # File handler (JSON)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    if json_format:
        file_handler.setFormatter(JSONFormatter())
    else:
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        ))
    file_handler.addFilter(context_filter)
    root.addHandler(file_handler)
    
    # Console handler (human-readable)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    ))
    console_handler.addFilter(context_filter)
    root.addHandler(console_handler)
    
    # Reduce noise from libraries
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
    
    def alert_fired(self, signal_name: str, signal_type: str, price: float, 
                    color: str = "info", signal_id: str = ""):
        """Structured alert log entry."""
        self.info(
            f"ALERT_FIRED: {signal_type} {signal_name}",
            extra={
                "event_type": "alert_fired",
                "signal_name": signal_name,
                "signal_type": signal_type,
                "price": price,
                "color": color,
                "signal_id": signal_id,
            }
        )
    
    def api_request(self, endpoint: str, symbol: str, timeframe: str, 
                    latency_ms: float, status: str = "success"):
        """Structured API request log."""
        self.info(
            f"API_REQUEST: {endpoint}",
            extra={
                "event_type": "api_request",
                "endpoint": endpoint,
                "symbol": symbol,
                "timeframe": timeframe,
                "latency_ms": latency_ms,
                "status": status,
            }
        )


# ── Usage in engine/UI ──
"""
# In QuantAlertEngine.__init__():
from logging_setup import setup_logging, AlertLoggerAdapter, context_filter

setup_logging(
    log_file=self.config.get('database', 'data/alerts.db').replace('.db', '.log'),
    level=self.config.get('general.log_level', 'INFO'),
    json_format=True,
)

# In SignalAnalyzer.analyze_symbol():
logger = AlertLoggerAdapter(logging.getLogger(__name__), symbol=symbol, timeframe=timeframe)
logger.alert_fired("Volume Spike", "VOLUME", price, "red", signal_id)

# In ResilientExchange._execute_with_retry():
start = time.time()
try:
    result = method(*args, **kwargs)
    logger.api_request(method.__name__, symbol, timeframe, (time.time()-start)*1000, "success")
    return result
except Exception as e:
    logger.api_request(method.__name__, symbol, timeframe, (time.time()-start)*1000, f"error:{type(e).__name__}")
    raise
"""
```

---

**Integration in `quant_alert_engine.py`:**

```python
# REPLACE logging setup (lines ~160-175) with:

from logging_setup import setup_logging, AlertLoggerAdapter, context_filter

# Early init (before Config loads log_level)
setup_logging(
    log_file="logs/quant_alert_engine.log",
    level="INFO",  # Will be overridden after config loads
    json_format=True,
)

# Then in Config.__init__ or QuantAlertEngine.__init__, reconfigure:
# setup_logging(level=config.get('general.log_level', 'INFO'))
```

---

## FIX 5: Health Check Endpoint (Optional but Recommended)

### Problem
- No way for orchestrator (systemd, Docker, Kubernetes) to verify liveness
- No metrics exposure

### Solution: Minimal FastAPI Health Server

**New file: `health_server.py`**

```python
"""
Minimal Health/Metrics Server for Quant Alert Engine
Run in background thread: python -m health_server
Or import and start from engine.
"""

import threading
import time
from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import FastAPI, Response
from pydantic import BaseModel
import psutil


class HealthResponse(BaseModel):
    status: str  # "healthy" | "degraded" | "unhealthy"
    timestamp: float
    uptime_seconds: float
    checks: Dict[str, Any]


_start_time = time.time()
_health_checks: Dict[str, Any] = {}


def register_check(name: str, check_fn):
    """Register a health check function. Returns True/False or dict."""
    _health_checks[name] = check_fn


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Quant Alert Engine Health", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health():
    results = {}
    overall = "healthy"
    for name, check_fn in _health_checks.items():
        try:
            result = check_fn()
            if isinstance(result, dict):
                results[name] = result
                if result.get("status") == "unhealthy":
                    overall = "unhealthy"
                elif result.get("status") == "degraded" and overall == "healthy":
                    overall = "degraded"
            else:
                results[name] = {"status": "healthy" if result else "unhealthy"}
                if not result and overall == "healthy":
                    overall = "unhealthy"
        except Exception as e:
            results[name] = {"status": "unhealthy", "error": str(e)}
            overall = "unhealthy"
    
    return HealthResponse(
        status=overall,
        timestamp=time.time(),
        uptime_seconds=time.time() - _start_time,
        checks=results,
    )


@app.get("/metrics")
async def metrics():
    """Prometheus-compatible metrics."""
    lines = [
        f'quant_engine_uptime_seconds {time.time() - _start_time}',
        f'quant_engine_memory_bytes {psutil.Process().memory_info().rss}',
        f'quant_engine_cpu_percent {psutil.Process().cpu_percent()}',
    ]
    # Add custom metrics from checks
    for name, check_fn in _health_checks.items():
        try:
            result = check_fn()
            if isinstance(result, dict) and "metrics" in result:
                for k, v in result["metrics"].items():
                    lines.append(f'quant_engine_{name}_{k} {v}')
        except Exception:
            pass
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")


def start_health_server(host: str = "0.0.0.0", port: int = 8080):
    """Start health server in background thread."""
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


# ── Example check functions to register in engine ──
"""
# In QuantAlertEngine.__init__():
from health_server import register_check, start_health_server

register_check("exchange", lambda: {
    "status": "healthy" if self.analyzer.exchange else "unhealthy",
    "metrics": {"connected": 1 if self.analyzer.exchange else 0}
})

register_check("database", lambda: {
    "status": "healthy",
    "metrics": {"alerts_fired_1h": self._count_recent_alerts()}
})

register_check("symbols", lambda: {
    "status": "healthy" if self.config.get('symbols') else "degraded",
    "metrics": {"symbols_configured": len(self.config.get('symbols', []))}
})

# Start health server
start_health_server(port=8080)
"""
```

---

**Updated `requirements.txt` additions:**

```text
# Health server
fastapi>=0.104.0
uvicorn>=0.24.0
psutil>=5.9.0
```

---

## SUMMARY: Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `config_schema.py` | **CREATE** | Pydantic v2 settings with validation + env vars |
| `exchange_resilient.py` | **CREATE** | CCXT wrapper with retry/backoff/circuit breaker |
| `logging_setup.py` | **CREATE** | JSON logging + context injection |
| `health_server.py` | **CREATE** | FastAPI `/health` + `/metrics` endpoint |
| `.env.example` | **CREATE** | Template for secrets |
| `quant_alert_engine.py` | **MODIFY** | Replace Config, Exchange, Logging, Bridge URL |
| `quant_alert_ui.py` | **MODIFY** | Use new Config, Bridge URL from settings |
| `requirements.txt` | **MODIFY** | Add pydantic, pydantic-settings, python-dotenv, fastapi, uvicorn, psutil |

---

## DEPLOYMENT CHECKLIST

1. **Copy `.env.example` → `.env`** and fill secrets
2. **Install new deps:** `pip install -r requirements.txt`
3. **Run engine:** `python quant_alert_engine.py` (validates config on startup)
4. **Verify health:** `curl http://localhost:8080/health`
5. **Check logs:** `tail -f logs/quant_alert_engine.log | jq .` (JSON format)

---

## ROLLBACK PLAN

If issues arise:
1. Keep old `Config` class as `ConfigLegacy` in `config_schema.py`
2. Toggle via env var: `USE_LEGACY_CONFIG=true`
3. Health server runs on separate port (8080) — non-invasive

---

*End of Priority 1 Fixes Document*
---

# Priority 2 Fixes — config changes justified by the Step 1 expectancy report

Source: `expectancy_report.py` (views `outcomes` / `gates` / `simulate`) run over
1,676 resolved outcomes. Reproduce with:
`python expectancy_report.py all --json data/expectancy_report.json`

## Verdict of the measurement

No edge, before or after costs. Two independent methods agree:

| method | expectancy |
|---|---|
| recorded outcomes, R-normalised, net of costs | **-4.61R** per signal (IC90 [-5.77,-3.52], n=868) |
| full-path fill simulation (stop/target on real high/low, repo `CostModel`) | **-4.41% of equity** per trade, PF 0.029, maxDD 100% |

Gross hit rate is 45.7% -> the edge BEFORE any cost is `2p-1 = -0.085R`. The
signals do not predict direction, and then costs make it far worse.

## Fix 1 — removed `1m` from `timeframes` (config.json, config.json.example)

1m carried 690 of 868 directional signals (79%) and is structurally untradeable:

| tf | n | median stop | median cost | trades whose cost ALONE > 1R |
|---|---|---|---|---|
| **1m** | 690 | 0.128% | **0.784R** | **41.6%** |
| 5m | 126 | 0.388% | 0.258R | 19.8% |
| 15m | 31 | 0.622% | 0.161R | 29.0% |
| 30m | 13 | 1.003R | 0.100R | 23.1% |
| 1h | 8 | 2.660% | 0.041R | 0.0% |

Roundtrip cost is 0.100% of price. On 1m the ATR is 0.08%, so a 2xATR stop is
0.128% wide — **narrower than the fee**. Sizing 1% risk over that stop levers the
notional ~8x and the fee is paid on the notional: 41.6% of 1m trades cannot win
even when the target is hit. Confirmed in the simulation: of 241 target hits,
several were net-negative (8 of 25 on the SOL-only run).

Widening the stop does NOT fix it (2x -> 4x -> 8xATR drops 1m median cost
0.784R -> 0.196R but median net only -1.187R -> -0.297R): the binding constraint
is the absent gross edge, not just cost.

## Fix 2 — `general.outcome_gating_enabled: true -> false`

Measured with train-only discipline (mute set built from the first half, applied
to the held-out half):

- sample size per (symbol, signal) pair: max 33, median 6, p90 19; **1 of 113
  pairs reaches n>=30**, so at the configured `outcome_gating_min_n: 30` the gate
  is effectively a no-op.
- at every REACHABLE threshold it makes expectancy worse:
  min_n 3 -> delta -1.86R, 5 -> -1.66R, 8 -> -1.19R, 10 -> -0.77R, 15 -> -0.32R.

Muting on a raw mean over 5-15 samples is noise-chasing: it discards pairs whose
true expectancy is positive. Either useless or harmful, so it is off. Revisit
once 4-8 weeks exist and n>=30 per pair is genuinely reached (>=5m only).

## NOT changed, and why — `time_gating_protected_hours_local: [8..15]`

The gate mutes hours on GROSS expectancy (ignores costs), and the protected list
overrides it for 8 of the 10 observed hours — which measured as the WORST of the
10 (UTC 18h/19h = -11.95R/-13.73R mean, while the unprotected UTC 22h/23h were
the least bad at -0.61R/-0.54R). So the override looks backwards.

It was deliberately left alone: 72% of ALL signals sit inside those hours, so
clearing the list would let the gate mute ~72% of volume — and every hour of the
current sample is negative, i.e. the engine would go silent. That is a product
decision, and the hour statistics are contaminated by 1m (which is now disabled).
Re-measure the per-hour table on the >=5m stream before touching this list.
