"""
Resilient CCXT Exchange Wrapper
- Exponential backoff with jitter on 429 / 5xx / network errors
- Circuit breaker pattern
- Request/response logging for debugging
"""
import time
import random
import logging
import threading
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

        # Circuit breaker state — per-symbol (fixes audit #2)
        self._consecutive_failures: Dict[str, int] = {}
        self._circuit_open_until: Dict[str, float] = {}
        self._failure_threshold = 10
        self._cb_lock = threading.Lock()
        # Fine-grained exchange lock per symbol to allow concurrent multi-symbol scanning (Fix H4)
        self._symbol_locks: Dict[str, threading.Lock] = {}
        self._symbol_locks_guard = threading.Lock()
        self._global_exchange_lock = threading.Lock()

        # Initialize CCXT exchange
        self.exchange = self._create_exchange(exchange_id, testnet, api_key, secret)

        # Load markets once with retry
        self._load_markets_with_retry()

    def _create_exchange(self, exchange_id: str, testnet: bool, api_key: str, secret: str):
        exchange_class = getattr(ccxt, exchange_id)
        config: Dict[str, Any] = {
            "enableRateLimit": True,
            "rateLimit": 100,
        }
        if api_key and secret:
            config["apiKey"] = api_key
            config["secret"] = secret
        # Testnet override — ccxt test urls vary by exchange
        if testnet:
            try:
                if hasattr(exchange_class, "urls") and isinstance(exchange_class.urls, dict) and "test" in exchange_class.urls:
                    config["urls"] = {"api": exchange_class.urls["test"]}
                else:
                    logger.warning("Testnet requested but %s has no test URL; using live", exchange_id)
            except Exception:
                pass
        return exchange_class(config)

    def _load_markets_with_retry(self):
        for attempt in range(self.max_retries):
            try:
                self.exchange.load_markets()
                logger.info("Loaded %s markets from %s", len(self.exchange.symbols), self.exchange_id)
                return
            except (RateLimitExceeded, ExchangeNotAvailable, RequestTimeout, NetworkError, DDoSProtection) as e:
                delay = self._calculate_delay(attempt, e)
                logger.warning("Load markets attempt %s/%s failed: %s. Retrying in %.1fs", attempt + 1, self.max_retries, e, delay)
                time.sleep(delay)
            except ExchangeError as e:
                logger.error("Exchange error loading markets: %s", e)
                raise
        raise ConnectionError(f"Failed to load markets after {self.max_retries} attempts")

    def _calculate_delay(self, attempt: int, error: Exception) -> float:
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        delay *= (0.75 + random.random() * 0.5)
        # Respect Retry-After header if present
        try:
            resp = getattr(error, "response", None)
            if resp is not None and hasattr(resp, "headers"):
                retry_after = resp.headers.get("Retry-After") if isinstance(resp.headers, dict) else None
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after) + 1)
                    except ValueError:
                        pass
        except Exception:
            pass
        return delay

    def _check_circuit(self, symbol: str = ""):
        key = symbol or "_global"
        with self._cb_lock:
            until = self._circuit_open_until.get(key, 0.0)
        now = time.time()
        if until > now:
            wait = until - now
            logger.warning("Circuit breaker OPEN for %s. Waiting %.1fs before retry", symbol or "global", wait)
            time.sleep(wait)

    def _record_success(self, symbol: str = ""):
        key = symbol or "_global"
        with self._cb_lock:
            self._consecutive_failures.pop(key, None)
            # clear circuit on success
            self._circuit_open_until.pop(key, None)

    def _record_failure(self, symbol: str = ""):
        key = symbol or "_global"
        with self._cb_lock:
            cnt = self._consecutive_failures.get(key, 0) + 1
            self._consecutive_failures[key] = cnt
            if cnt >= self._failure_threshold:
                self._circuit_open_until[key] = time.time() + 60
                logger.error("Circuit breaker TRIPPED for %s after %s failures. Cooling down 60s", symbol or "global", cnt)

    def _get_call_lock(self, symbol: str = "") -> threading.Lock:
        if not symbol:
            return self._global_exchange_lock
        with self._symbol_locks_guard:
            if symbol not in self._symbol_locks:
                self._symbol_locks[symbol] = threading.Lock()
            return self._symbol_locks[symbol]

    def _execute_with_retry(self, method_name: str, *args, **kwargs) -> Any:
        # Extract symbol for per-symbol circuit breaker & per-symbol lock
        symbol = ""
        if args and isinstance(args[0], str) and "/" in args[0]:
            symbol = args[0]
        elif "symbol" in kwargs and isinstance(kwargs["symbol"], str):
            symbol = kwargs["symbol"]
        self._check_circuit(symbol)
        call_lock = self._get_call_lock(symbol)
        last_error = None
        for attempt in range(self.max_retries):
            try:
                method = getattr(self.exchange, method_name)
                # Lock only per-symbol, not globally across symbols
                with call_lock:
                    result = method(*args, **kwargs)
                self._record_success(symbol)
                return result
            except RateLimitExceeded as e:
                last_error = e
                delay = self._calculate_delay(attempt, e)
                logger.warning("Rate limited on %s (%s) (attempt %s/%s). Backing off %.1fs", method_name, symbol or "-", attempt + 1, self.max_retries, delay)
                time.sleep(delay)
            except (ExchangeNotAvailable, RequestTimeout, NetworkError, DDoSProtection) as e:
                last_error = e
                delay = self._calculate_delay(attempt, e)
                logger.warning("Network error on %s (%s) (attempt %s/%s): %s. Retrying in %.1fs", method_name, symbol or "-", attempt + 1, self.max_retries, e, delay)
                time.sleep(delay)
            except ExchangeError as e:
                logger.error("Exchange error on %s (%s): %s", method_name, symbol or "-", e)
                self._record_failure(symbol)
                raise
            except Exception as e:
                logger.error("Unexpected error on %s (%s): %s", method_name, symbol or "-", e, exc_info=True)
                self._record_failure(symbol)
                raise
        self._record_failure(symbol)
        raise last_error or Exception(f"Max retries exceeded for {method_name}")

    # ── Public API (subset used by engine) ──

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> List[List]:
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
        valid = [s for s in symbols if s in self.exchange.symbols]
        invalid = [s for s in symbols if s not in self.exchange.symbols]
        for s in invalid:
            logger.error("Symbol %s not found on %s — removing from watch list", s, self.exchange_id)
        return valid
