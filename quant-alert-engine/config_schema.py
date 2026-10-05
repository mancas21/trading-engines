"""
Quant Alert Engine — Configuration Schema (Pydantic v2)
Provides validation, env var overrides, and type safety.
Env vars take precedence over config.json via SettingsConfigDict.
Supports legacy flat config.json (exchange as string, missing bridge/general fields).
"""
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Literal, Any, Dict

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env from project root regardless of CWD
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=False)
    # Also try .env in CWD as fallback
    load_dotenv(override=False)
except Exception:
    pass

# Resolve absolute env file for Settings
_ENV_FILE = str((Path(__file__).parent / ".env").resolve())


# ── Legacy env var migration (fix #5) ──
# Older .env.example documented TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (flat).
# Before pydantic_settings validates, map those to nested TELEGRAM__TOKEN / TELEGRAM__CHAT_ID.
_LEGACY_ENV_MIGRATIONS = {
    "TELEGRAM_BOT_TOKEN": "TELEGRAM__TOKEN",
    "TELEGRAM_TOKEN": "TELEGRAM__TOKEN",
    "TELEGRAM_CHAT_ID": "TELEGRAM__CHAT_ID",
    "TELEGRAM_ENABLED": "TELEGRAM__ENABLED",
    "EXCHANGE_ID": "EXCHANGE__ID",
    "EXCHANGE_TESTNET": "EXCHANGE__TESTNET",
    "BRIDGE_URL": "BRIDGE__URL",
    "BRIDGE_ENABLED": "BRIDGE__ENABLED",
}


def _apply_legacy_env_migrations() -> None:
    """Promote flat legacy env vars into nested form so Settings(env_nested_delimiter='__') can see them.
    Idempotent — nested vars win; legacy vars are only copied if nested missing.
    """
    for legacy, nested in _LEGACY_ENV_MIGRATIONS.items():
        legacy_val = os.environ.get(legacy)
        if not legacy_val:
            continue
        # Only set nested if not already present (env var > legacy)
        if not os.environ.get(nested):
            os.environ[nested] = legacy_val


# Run migration as a side-effect of import, before any Settings() build
_apply_legacy_env_migrations()


# ── Nested configs ──
class VolumeConfig(BaseModel):
    enabled: bool = True
    avg_len: int = Field(20, ge=1, le=200)
    spike_mult: float = Field(2.0, gt=0)
    climax_mult: float = Field(3.5, gt=0)
    dryup_mult: float = Field(0.5, gt=0, lt=1)
    dryup_exit_mult: float = Field(0.8, gt=0, lt=2)
    state_persistence_bars: int = Field(2, ge=1, le=10)
    baseline_ttl_seconds: float = Field(21600, gt=0)
    obv_break_sigma: float = Field(0.75, ge=0, le=5)
    obv_break_bars: int = Field(2, ge=1, le=10)
    obv_len: int = Field(20, ge=1, le=200)


class CVDConfig(BaseModel):
    enabled: bool = True
    smooth_len: int = Field(5, ge=1, le=50)
    div_lookback: int = Field(20, ge=5, le=200)
    pivot_len: int = Field(5, ge=1, le=20)
    min_gap: int = Field(3, ge=1, le=10)  # min bars between compared pivots


class RSIConfig(BaseModel):
    enabled: bool = True
    length: int = Field(14, ge=2, le=50)
    overbought: int = Field(70, ge=50, le=90)
    oversold: int = Field(30, ge=10, le=50)
    pivot_len: int = Field(5, ge=1, le=20)
    div_lookback: int = Field(20, ge=5, le=200)
    min_gap: int = Field(3, ge=1, le=10)  # min bars between compared pivots

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
    min_gap: int = Field(3, ge=1, le=10)  # min bars between compared pivots

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

    @field_validator("overbought")
    @classmethod
    def ob_gt_os(cls, v, info):
        if "oversold" in info.data and v <= info.data["oversold"]:
            raise ValueError("overbought must be > oversold")
        return v


class VolatilityConfig(BaseModel):
    enabled: bool = True
    bb_len: int = Field(20, ge=5, le=100)
    bb_mult: float = Field(2.0, gt=0, le=5)
    bb_squeeze_pct: float = Field(20.0, ge=1, le=50)
    bb_width_lookback: int = Field(100, ge=20, le=500)
    atr_len: int = Field(14, ge=2, le=50)
    atr_expansion_mult: float = Field(1.8, gt=0, le=5)


class GeneralConfig(BaseModel):
    check_interval: int = Field(5, ge=1, le=300)
    enable_notifications: bool = True
    enable_sound: bool = True
    max_workers: int = Field(10, ge=1, le=50)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    max_history_bars: int = Field(200, ge=50, le=2000)
    health_port: int = Field(8081, ge=1024, le=65535)
    health_enabled: bool = True
    health_host: str = Field("127.0.0.1", description="Health server bind host; use 0.0.0.0 to expose LAN")
    # Quant audit fixes (v2.2)
    outcome_forward_bars: int = Field(12, ge=1, le=200, description="Forward bars used to score each signal's outcome")
    confluence_required: int = Field(2, ge=0, le=6, description="Min directional confirmations (0=off): RSI/MACD/STOCH/BB/Volume/Trend")
    regime_filter_enabled: bool = Field(True, description="Suppress counter-trend signals (opt-in)")
    regime_trend_len: int = Field(50, ge=10, le=200, description="EMA length for the trend regime filter")
    tf_consolidation_enabled: bool = Field(True, description="Suppress lower-TF duplicates when the same signal already fired on a higher TF")
    macro_regime_enabled: bool = Field(True, description="Gate signals by BTC/ETH macro trend (bull/bear filter)")
    macro_regime_symbols: List[str] = Field(default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT"], description="Symbols for macro regime detection")
    outcome_gating_enabled: bool = Field(True, description="Mute signal families with negative trailing expectancy")
    outcome_gating_lookback_days: int = Field(7, ge=1, le=90, description="Trailing window for outcome gating")
    outcome_gating_min_n: int = Field(30, ge=5, le=500, description="Min sample size before muting")
    time_gating_enabled: bool = Field(True, description="Mute signals during historically negative expectancy hours (local time = UTC + utc_offset)")
    time_gating_lookback_days: int = Field(14, ge=1, le=90, description="Trailing window for time gating")
    time_gating_min_n: int = Field(20, ge=5, le=500, description="Min sample size per hour before muting")
    time_gating_utc_offset: int = Field(-5, ge=-12, le=14, description="Hour offset added to UTC when bucketing/checking muted hours (e.g. -5 for Colombia)")
    time_gating_protected_hours_local: List[int] = Field(default_factory=lambda: [8, 9, 10, 11, 12, 13, 14, 15], description="Local hours NEVER muted regardless of expectancy (e.g. New York session 08-15 in UTC-5)")
    # Recency blackout (v2.5): time-based throttle independent of condition filters.
    blackout_enabled: bool = Field(True, description="Enforce a minimum gap between consecutive alerts per symbol")
    blackout_seconds: int = Field(300, ge=0, le=86400, description="Min seconds between ANY alert for the same symbol")
    signal_cooldown_seconds: int = Field(600, ge=0, le=86400, description="Min seconds between repeats of the SAME signal on a symbol (per timeframe)")


class ExchangeConfig(BaseModel):
    id: str = "binanceusdm"
    testnet: bool = False
    api_key: str = ""
    secret: str = ""
    rate_limit_buffer: float = 0.1


class TelegramConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    chat_id: str = ""
    filter_symbol: Optional[str] = None
    filter_timeframe: Optional[str] = None


class BridgeConfig(BaseModel):
    enabled: bool = True
    url: str = "http://127.0.0.1:8000/quant-alert"
    filter_url: str = "http://127.0.0.1:8000/quant/filter"
    timeout_seconds: float = Field(2.0, gt=0, le=30)


class Level2Config(BaseModel):
    """Use the ATAS bridge as a Level-2 order-flow validator before firing
    directional engine alerts. Weighted score across DOM imbalance, CVD trend,
    and quant multi-TF consensus; pass when score >= min_score."""
    enabled: bool = Field(False, description="Gate directional alerts by bridge order-flow validation")
    base_url: str = Field("http://127.0.0.1:8000", description="ATAS bridge base URL")
    timeout_seconds: float = Field(1.5, gt=0, le=10, description="Per-request bridge timeout")
    min_score: float = Field(2.0, ge=0.0, le=3.0, description="Validation score needed to pass (0..3)")
    require_dom: bool = Field(True, description="Include DOM imbalance in validation score")
    dom_imbalance_min: float = Field(5.0, ge=0.0, le=50.0, description="Min |imbalance %| to count as strong DOM confirmation")
    require_cvd: bool = Field(True, description="Include CVD trend in validation score")
    require_quant: bool = Field(True, description="Include quant multi-TF consensus in validation score")
    quant_min_timeframes: int = Field(1, ge=1, le=4, description="Min distinct timeframes with agreement for quant consensus")
    fail_open: bool = Field(True, description="If the bridge is unreachable, allow the alert instead of blocking")
    cache_ttl_seconds: float = Field(4.0, ge=1.0, le=60.0, description="Cache bridge reads per symbol to avoid hammering")


class WebMacroSourceConfig(BaseModel):
    """One mandatory-attempt web macro source (info/context only).

    These providers complement CoinGecko/Polymarket/funding with web context
    (CoinStats, TradingKey, DiarioBitcoin, Coinpedia). They never gate
    signals; missing pages fail open as unavailable/not_covered."""
    id: str = Field(..., description="Provider id: coinstats|tradingkey_ta|tradingkey_movers|diariobitcoin|coinpedia")
    enabled: bool = Field(True, description="Attempt this provider every refresh cycle")
    ttl_seconds: float = Field(300.0, ge=30.0, le=86400.0, description="Freshness window per provider record")
    timeout_seconds: float = Field(12.0, ge=3.0, le=60.0, description="Per-request timeout for provider pages")
    confidence: str = Field("medium", description="Analyst trust tier: high|medium|low")


def _default_web_sources() -> List["WebMacroSourceConfig"]:
    return [
        WebMacroSourceConfig(id="coinstats", enabled=True, ttl_seconds=120.0, timeout_seconds=12.0, confidence="high"),
        WebMacroSourceConfig(id="coinmarketcap", enabled=True, ttl_seconds=120.0, timeout_seconds=12.0, confidence="high"),
        WebMacroSourceConfig(id="tradingkey_ta", enabled=True, ttl_seconds=300.0, timeout_seconds=12.0, confidence="medium"),
        WebMacroSourceConfig(id="tradingkey_movers", enabled=True, ttl_seconds=900.0, timeout_seconds=12.0, confidence="low"),
        WebMacroSourceConfig(id="diariobitcoin", enabled=True, ttl_seconds=900.0, timeout_seconds=12.0, confidence="medium"),
        WebMacroSourceConfig(id="coinpedia", enabled=True, ttl_seconds=1800.0, timeout_seconds=12.0, confidence="low"),
    ]


class MacroConfig(BaseModel):
    """External macro/market-context pipeline (CoinGecko + DefiLlama +
    Polymarket + Binance funding). Complements the engine's technical and
    order-flow analysis with spot pricing, fundamentals, and prediction-
    market sentiment. Fail-open: missing/stale macro data never blocks
    signal emission."""
    enabled: bool = Field(False, description="Enable the macro context background pipeline")
    coingecko_key: str = Field("", description="CoinGecko API key (free tier works empty)")
    cmc_api_key: str = Field("", description="CoinMarketCap API key (env-only; empty = keyless public API)")
    cache_ttl_seconds: float = Field(60.0, ge=10.0, le=900.0, description="Seconds between macro refreshes per symbol")
    predictive_weight: float = Field(0.3, ge=0.0, le=1.0, description="How much Polymarket sentiment influences macro score (0=info only, 1=full gate)")
    max_concurrent_requests: int = Field(3, ge=1, le=10, description="Max concurrent HTTP requests to external APIs")
    max_retries: int = Field(3, ge=1, le=10, description="Max retries per request")
    gate_signals: bool = Field(False, description="If true, suppress counter-macro directional signals; if false, macro is info-only overlay")
    ath_bear_pct: float = Field(-12.0, ge=-90.0, le=0.0, description="24h change below this flags the overlay bear (asymmetric; wider than the old -10%)")
    ath_bull_pct: float = Field(15.0, ge=0.0, le=200.0, description="24h change above this flags the overlay bull (asymmetric; wider than the old +10%)")
    web_sources: List[WebMacroSourceConfig] = Field(default_factory=_default_web_sources, description="Mandatory-attempt web macro providers (info/context only, fail-open)")


class MLConfig(BaseModel):
    """Meta-labeling layer: a supervised filter trained on the engine's own
    tagged signal outcomes (ml_train.py), scored against the canonical feature
    vector from features.py.

    Shadow by default: the model scores every signal and records P(pays), but
    only blocks emission when gate_signals is true. That lets a model be
    validated live against the existing track record before it is trusted.
    Fail-open: a missing or broken model never blackholes alerts.
    """
    enabled: bool = Field(False, description="Load the meta-label model and score every signal")
    gate_signals: bool = Field(False, description="If true, suppress signals below min_prob (shadow/log-only when false)")
    model_path: str = Field("data/ml/meta_model.json", description="Model bundle written by ml_train.py")
    min_prob: float = Field(0.55, ge=0.0, le=1.0, description="Minimum P(signal pays) required to emit when gating")
    fail_open: bool = Field(True, description="If the model is missing/broken, keep emitting")
    window: int = Field(200, ge=60, le=1000, description="Trailing bars for the feature window (must match ml_backfill)")
    log_features: bool = Field(True, description="Persist the feature vector of every fired signal into signal_features")


class PeriodicSnapshotConfig(BaseModel):
    enabled: bool = False
    interval_seconds: int = 900
    symbols: List[str] = Field(default_factory=list)


class Settings(BaseSettings):
    """Root settings with env var support. Env vars take precedence over config.json."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    symbols: List[str] = Field(default_factory=list, min_length=1)
    timeframes: List[str] = Field(default_factory=lambda: ["5m"], min_length=1)
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
    level2: Level2Config = Level2Config()
    macro: MacroConfig = MacroConfig()
    ml: MLConfig = MLConfig()
    periodic_snapshot: PeriodicSnapshotConfig = PeriodicSnapshotConfig()
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
            raise ValueError("Telegram enabled but token/chat_id missing — set TELEGRAM__TOKEN and TELEGRAM__CHAT_ID in .env or disable telegram.enabled")
        return self


def _normalize_legacy_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce legacy flat config.json into Settings shape."""
    data = dict(raw)  # shallow copy

    # Legacy: "exchange": "binanceusdm"  -> {"id": "binanceusdm"}
    if "exchange" in data and isinstance(data["exchange"], str):
        data["exchange"] = {"id": data["exchange"]}
    # Legacy: database at top-level string is already compatible
    # Legacy: ensure general defaults exist if missing keys like log_level
    return data


# ── Backwards-compatible loader for existing code ──
class Config:
    """Drop-in replacement for old Config class. Reads config.json + .env + env vars."""

    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self._settings = self._load()

    def _load(self) -> Settings:
        json_data: Dict[str, Any] = {}
        p = Path(self.config_file)
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    json_data = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in {self.config_file}: {e}") from e
            json_data = _normalize_legacy_config(json_data)
            # Strip empty secrets from json so .env / env vars can populate them
            # (otherwise Settings(**json_data) would pass "" and override env)
            for section, keys in [("telegram", ["token", "chat_id"]), ("exchange", ["api_key", "secret"]), ("macro", ["coingecko_key", "cmc_api_key"])]:
                sec = json_data.get(section)
                if isinstance(sec, dict):
                    for k in keys:
                        if k in sec and (sec[k] == "" or sec[k] is None):
                            sec.pop(k)
                    if not sec:
                        json_data.pop(section, None)
        # Pydantic merges: env vars > .env > json_data > defaults
        # But init_kwargs override env, so we stripped empty secrets above
        return Settings(**json_data)

    def get(self, key: str, default=None):
        """Dot-notation access: config.get('rsi.length')"""
        keys = key.split(".")
        obj: Any = self._settings
        for k in keys:
            if isinstance(obj, BaseModel):
                if k in obj.model_fields_set or hasattr(obj, k):
                    try:
                        obj = getattr(obj, k)
                    except AttributeError:
                        return default
                else:
                    # allow defaults that weren't explicitly set
                    if hasattr(obj, k):
                        obj = getattr(obj, k)
                    else:
                        return default
            elif isinstance(obj, dict) and k in obj:
                obj = obj[k]
            else:
                return default
        return obj

    @property
    def data(self) -> dict:
        """Full dict for backwards compatibility."""
        return self._settings.model_dump()

    @data.setter
    def data(self, value: dict):
        """Allow legacy mutation like config.data['symbols'] = [...]"""
        # Re-parse via Settings to keep validation
        try:
            self._settings = Settings(**value)
        except Exception:
            # Fallback: mutate underlying dict without validation (legacy behavior)
            d = self._settings.model_dump()
            d.update(value)
            try:
                self._settings = Settings(**d)
            except Exception:
                # Last resort: keep raw
                self._settings = Settings(**_normalize_legacy_config(d))

    def set(self, key: str, value: Any) -> None:
        """Validated set for dot-notation keys — mutates the real Settings object.
        Use this instead of `cfg.data['symbols'] = ...` which mutates a throwaway copy.
        """
        parts = key.split(".")
        if len(parts) == 1:
            setattr(self._settings, key, value)
            return
        # Navigate to parent object
        obj: Any = self._settings
        for p in parts[:-1]:
            obj = getattr(obj, p)
        setattr(obj, parts[-1], value)

    def save(self, persist_secrets: bool = False):
        """Persist current settings to config.json.
        If persist_secrets is False, any secrets already stored in config.json on disk
        are preserved (never wiped), but secrets loaded from environment variables
        are not leaked into config.json.
        """
        data = self._settings.model_dump()
        if not persist_secrets:
            existing_secrets = {
                ("telegram", "token"): "",
                ("telegram", "chat_id"): "",
                ("exchange", "api_key"): "",
                ("exchange", "secret"): "",
                ("macro", "coingecko_key"): "",
                ("macro", "cmc_api_key"): "",
            }
            if os.path.exists(self.config_file):
                try:
                    with open(self.config_file, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    for (parent, key) in existing_secrets:
                        if parent in raw and isinstance(raw[parent], dict):
                            existing_secrets[(parent, key)] = raw[parent].get(key, "")
                except Exception:
                    pass
            for (parent, key), val in existing_secrets.items():
                data[parent][key] = val

        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def reload(self):
        self._settings = self._load()


# Legacy alias for rollback via env var
if os.getenv("USE_LEGACY_CONFIG", "").lower() in ("1", "true", "yes"):
    # Keep old Config behavior if needed — not used in normal path
    pass


def load_config(config_file: str = "config.json") -> Config:
    """Load and validate config, returning a Config instance.
    Catches pydantic ValidationError and prints the message + guidance instead of a stack trace (fix #8).
    """
    try:
        # Apply migrations on every load attempt so .env.example legacy vars work
        _apply_legacy_env_migrations()
        return Config(config_file)
    except Exception as e:
        msg = str(e)
        # Trim pydantic's verbose multi-line to the essentials
        if hasattr(e, "errors") and callable(getattr(e, "errors", None)):
            try:
                errs = e.errors()  # type: ignore
                compact = []
                for err in errs:
                    loc = ".".join(str(x) for x in err.get("loc", []))
                    compact.append(f"  - {loc}: {err.get('msg', '')}")
                msg = "\n".join(compact)
            except Exception:
                pass
        sys.stderr.write(
            f"\n[CONFIG ERROR] Could not start: {type(e).__name__} in {config_file}\n{msg}\n\n"
            "Fix: edit config.json or .env (see .env.example for env var format).\n"
            "Quick start: copy .env.example to .env and set TELEGRAM__TOKEN/CHAT_ID if using Telegram.\n\n"
        )
        sys.stderr.flush()
        raise


if __name__ == "__main__":
    cfg = Config("config.json")
    print(f"Symbols: {cfg.get('symbols')}")
    print(f"RSI length: {cfg.get('rsi.length')}")
    print(f"Exchange id: {cfg.get('exchange.id')}")
    print(f"Bridge URL: {cfg.get('bridge.url')}")
    print(f"Log level: {cfg.get('general.log_level')}")
