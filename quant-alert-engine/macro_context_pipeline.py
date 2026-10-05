"""
Macro Context Data Pipeline for Quant Alert Engine v2.5
Aggregates CoinGecko spot pricing, DefiLlama TVL, Polymarket prediction
sentiment, and Binance funding rates into a unified macro context that
complements the engine's technical + order-flow analysis.

Non-blocking: runs as a background async task; caches with TTL; fail-open.
"""
import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple, TypedDict, Union

import aiohttp

from web_macro_providers import (
    CMC_GLOBAL_TTL_SECONDS,
    DEFAULT_WEB_SOURCES,
    fetch_cmc_shared,
    is_fresh as _web_is_fresh,
    refresh_web_macro,
    summarize_cmc_global,
    summarize_web_macro,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #
class OkResult(TypedDict, total=False):
    ok: bool
    data: Any
    error: str


# --------------------------------------------------------------------------- #
# Config dataclass
# --------------------------------------------------------------------------- #
@dataclass
class MacroConfig:
    enabled: bool = False
    coingecko_key: str = ""
    coingecko_base: str = "https://api.coingecko.com/api/v3"
    defillama_base: str = "https://api.llama.fi"
    polymarket_gamma_base: str = "https://gamma-api.polymarket.com"
    polymarket_fallback_base: str = "https://data-api.polymarket.com"
    clob_base: str = "https://clob.polymarket.com"
    max_retries: int = 3
    max_concurrent_requests: int = 3
    cache_ttl_seconds: float = 60.0
    predictive_weight: float = 0.3
    # Asymmetric ATH/drawdown regime thresholds (v2.6). A 24h change must
    # breach the bear/bull bands to flag the overlay; the wide neutral band
    # between them absorbs normal chop instead of flipping every cycle.
    ath_bear_pct: float = -12.0
    ath_bull_pct: float = 15.0
    # Symbols to track (coin IDs for CoinGecko, protocol slugs for DefiLlama)
    tracked_coins: Dict[str, str] = field(default_factory=dict)  # symbol -> coin_id
    tracked_protocols: List[str] = field(default_factory=list)
    # CoinMarketCap API key (env-only, never committed). Empty = keyless
    # public API (lower limits, curated endpoint subset, same envelope).
    cmc_api_key: str = ""
    # Web macro providers (info/context only — never gates signals).
    # Each entry: {id, enabled, ttl_seconds, timeout_seconds, confidence}.
    web_sources: List[Dict[str, Any]] = field(default_factory=lambda: [dict(d) for d in DEFAULT_WEB_SOURCES])


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
class MacroContextPipeline:
    """Concurrent async data pipeline for macro/market context.

    Usage from the engine:
        pipeline = MacroContextPipeline(config)
        await pipeline.start()          # background task
        ctx = pipeline.get_context("SUI")  # returns dict (cached or fresh)
        await pipeline.stop()
    """

    def __init__(self, macro_config: Optional[MacroConfig] = None):
        self.cfg = macro_config or MacroConfig()
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._semaphore = asyncio.Semaphore(self.cfg.max_concurrent_requests)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # CoinMarketCap market-wide context (fear/greed + global metrics),
        # cached separately on its own TTL — shared by all symbols.
        self._cmc_global: Optional[Dict[str, Any]] = None
        self._cmc_global_ts: float = 0.0

    # ------------------------------------------------------------------- #
    # Shared HTTP helper (retry + jitter + concurrency throttle)
    # ------------------------------------------------------------------- #
    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> OkResult:
        last_err = ""
        async with self._semaphore:
            for attempt in range(1, self.cfg.max_retries + 1):
                try:
                    async with session.get(
                        url,
                        params=params,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            return {"ok": True, "data": await resp.json()}
                        body = await resp.text()
                        if resp.status in (429, 502, 503, 504):
                            last_err = f"HTTP {resp.status} from {url}: {body[:200]}"
                            logger.warning(
                                "Transient error (attempt %d/%d): %s",
                                attempt, self.cfg.max_retries, last_err,
                            )
                            await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
                            continue
                        logger.error("Non-retryable HTTP %d from %s: %s", resp.status, url, body[:200])
                        return {"ok": False, "error": f"HTTP {resp.status}: {body[:200]}"}
                except asyncio.TimeoutError:
                    last_err = f"Timeout calling {url}"
                    logger.warning("Timeout (attempt %d/%d): %s", attempt, self.cfg.max_retries, last_err)
                    await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
                except aiohttp.ClientError as e:
                    last_err = f"Client error calling {url}: {str(e)}"
                    logger.warning("Client error (attempt %d/%d): %s", attempt, self.cfg.max_retries, last_err)
                    await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
                except (json.JSONDecodeError, ValueError) as e:
                    logger.error("Bad JSON from %s: %s", url, str(e))
                    return {"ok": False, "error": f"Bad JSON from {url}: {str(e)}"}
        logger.error("All %d retries exhausted for %s: %s", self.cfg.max_retries, url, last_err)
        return {"ok": False, "error": last_err}

    async def _fetch_text(
        self, session: aiohttp.ClientSession, url: str, timeout: float
    ) -> Dict[str, Any]:
        """Page fetch for web-macro providers (HTML). Fail-open."""
        try:
            from web_macro_providers import fetch_text as _provider_fetch
        except ImportError as e:
            return {"ok": False, "status": 0, "error": f"web providers unavailable: {e}"}
        return await _provider_fetch(session, url, timeout, max_retries=self.cfg.max_retries)

    async def _fetch_shared_web_pages(self, session: aiohttp.ClientSession) -> Dict[str, Any]:
        """Fetch once-per-cycle shared pages (bubbles snapshot, movers listing)."""
        shared: Dict[str, Any] = {}
        defs = {d.get("id"): d for d in (self.cfg.web_sources or []) if isinstance(d, dict)}
        if defs.get("coinstats", {}).get("enabled", True):
            res = await self._fetch_text(
                session, "https://coinstats.app/crypto-bubbles/",
                float(defs["coinstats"].get("timeout_seconds", 12.0)),
            )
            if res.get("ok"):
                shared["coinstats_bubbles"] = res["text"]
        if defs.get("tradingkey_movers", {}).get("enabled", True):
            res = await self._fetch_text(
                session, "https://www.tradingkey.com/news/market-movers",
                float(defs["tradingkey_movers"].get("timeout_seconds", 12.0)),
            )
            if res.get("ok"):
                shared["tk_movers_listing"] = res["text"]
        # CoinMarketCap shared batch: quotes + fear/greed + global metrics.
        if defs.get("coinmarketcap", {}).get("enabled", True):
            try:
                shared["cmc"] = await fetch_cmc_shared(
                    session,
                    api_key=self.cfg.cmc_api_key or "",
                    timeout=float(defs["coinmarketcap"].get("timeout_seconds", 12.0)),
                    max_retries=self.cfg.max_retries,
                )
            except Exception as e:
                logger.debug("CMC shared fetch failed (fail-open): %s", str(e)[:100])
        return shared

    # ------------------------------------------------------------------- #
    # CoinGecko v4: spot price, volume, 24h change
    # ------------------------------------------------------------------- #
    def _cg_headers(self) -> Dict[str, str]:
        """Send the key in both demo and pro header forms — CoinGecko
        ignores the unknown one and authenticates on the valid one."""
        headers = {"accept": "application/json"}
        if self.cfg.coingecko_key:
            headers["x-cg-demo-api-key"] = self.cfg.coingecko_key
            headers["Authorization"] = f"Bearer {self.cfg.coingecko_key}"
        return headers

    async def fetch_token_metrics(
        self, session: aiohttp.ClientSession, coin_id: str
    ) -> Dict[str, Any]:
        url = f"{self.cfg.coingecko_base}/simple/price"
        params = {
            "ids": coin_id,
            "vs_currencies": "usd",
            "include_24hr_vol": "true",
            "include_24hr_change": "true",
        }
        result = await self._get_json(session, url, params=params, headers=self._cg_headers())
        if not result["ok"]:
            return {"error": result["error"]}

        coin_data = result["data"].get(coin_id)
        if not coin_data:
            return {"error": f"CoinGecko returned no entry for coin_id='{coin_id}'"}

        return {
            "price_usd": coin_data.get("usd"),
            "volume_24h_usd": coin_data.get("usd_24h_vol"),
            "change_24h_percent": coin_data.get("usd_24h_change"),
        }

    async def fetch_token_metrics_batch(
        self, session: aiohttp.ClientSession, coin_ids: List[str]
    ) -> Dict[str, Any]:
        """Single /simple/price call for MANY coins — 1 quota hit per cycle
        instead of 1 per symbol. This is what keeps the free tier alive."""
        if not coin_ids:
            return {}
        url = f"{self.cfg.coingecko_base}/simple/price"
        params = {
            "ids": ",".join(dict.fromkeys(coin_ids)),
            "vs_currencies": "usd",
            "include_24hr_vol": "true",
            "include_24hr_change": "true",
        }
        result = await self._get_json(session, url, params=params, headers=self._cg_headers())
        if not result["ok"]:
            return {"error": result["error"]}
        out: Dict[str, Any] = {}
        for cid in dict.fromkeys(coin_ids):
            cd = result["data"].get(cid)
            if not cd:
                out[cid] = {"error": f"CoinGecko returned no entry for coin_id='{cid}'"}
            else:
                out[cid] = {
                    "price_usd": cd.get("usd"),
                    "volume_24h_usd": cd.get("usd_24h_vol"),
                    "change_24h_percent": cd.get("usd_24h_change"),
                }
        return out

    # ------------------------------------------------------------------- #
    # DefiLlama: protocol TVL
    # ------------------------------------------------------------------- #
    async def fetch_protocol_tvl(
        self, session: aiohttp.ClientSession, protocol_slug: str
    ) -> Dict[str, Any]:
        url = f"{self.cfg.defillama_base}/tvl/{protocol_slug}"
        result = await self._get_json(session, url)
        if not result["ok"]:
            return {"error": result["error"]}
        try:
            return {"protocol": protocol_slug, "tvl_usd": float(result["data"])}
        except (ValueError, TypeError):
            return {"error": f"DefiLlama returned non-numeric body for '{protocol_slug}'"}

    # ------------------------------------------------------------------- #
    # Polymarket: keyword search with primary + fallback endpoint
    # ------------------------------------------------------------------- #
    async def fetch_polymarket_sentiment(
        self,
        session: aiohttp.ClientSession,
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        result = await self._get_json(
            session, f"{self.cfg.polymarket_gamma_base}/public-search",
            params={"q": query},
        )
        if not result["ok"]:
            logger.info("Gamma primary search failed, trying fallback: %s", result["error"])
            result = await self._get_json(
                session, f"{self.cfg.polymarket_fallback_base}/search/markets",
                params={"q": query, "limit": str(limit)},
            )
        if not result["ok"]:
            return [{"error": result["error"]}]

        events = result["data"].get("events", [])
        sentiment_data: List[Dict[str, Any]] = []
        for event in events:
            for m in event.get("markets", []):
                outcome_prices = m.get("outcomePrices") or m.get("prices")
                outcomes = m.get("outcomes")
                condition_id = m.get("conditionId") or m.get("condition_id")
                try:
                    if isinstance(outcome_prices, str):
                        outcome_prices = json.loads(outcome_prices)
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                except (json.JSONDecodeError, ValueError):
                    pass
                sentiment_data.append({
                    "event_title": event.get("title"),
                    "question": m.get("question"),
                    "slug": m.get("slug"),
                    "condition_id": condition_id,
                    "clob_token_ids": m.get("clobTokenIds"),
                    "outcomes": outcomes,
                    "outcome_probabilities": outcome_prices,
                    "volume": m.get("volume"),
                })
                if len(sentiment_data) >= limit:
                    return sentiment_data
        return sentiment_data

    # ------------------------------------------------------------------- #
    # Polymarket CLOB: orderbook + midpoint
    # ------------------------------------------------------------------- #
    async def fetch_polymarket_orderbook(
        self, session: aiohttp.ClientSession, clob_token_id: str
    ) -> Dict[str, Any]:
        book = await self._get_json(
            session, f"{self.cfg.clob_base}/book", params={"token_id": clob_token_id}
        )
        midpoint = await self._get_json(
            session, f"{self.cfg.clob_base}/midpoint", params={"token_id": clob_token_id}
        )
        return {
            "book": book["data"] if book["ok"] else {"error": book["error"]},
            "midpoint": midpoint["data"] if midpoint["ok"] else {"error": midpoint["error"]},
        }

    # ------------------------------------------------------------------- #
    # Binance funding rate (via ccxt, run in thread pool to avoid blocking)
    # ------------------------------------------------------------------- #
    async def fetch_funding_rate(self, symbol: str) -> Dict[str, Any]:
        """Async wrapper — runs ccxt fetch_ticker in a thread pool."""
        try:
            return await asyncio.to_thread(self._fetch_funding_rate_sync, symbol)
        except Exception as e:
            logger.debug("Funding rate fetch failed for %s: %s", symbol, str(e)[:100])
            return {"error": str(e)[:100]}

    def _fetch_funding_rate_sync(self, symbol: str) -> Dict[str, Any]:
        """Synchronous funding rate fetch (called via asyncio.to_thread).

        Uses Binance Futures public REST (premiumIndex lastFundingRate,
        fallback fundingRate) — no auth, no ccxt market load. ccxt's
        fetch_ticker does not populate fundingRate on binanceusdm, and the
        old symbol normalization produced nonexistent markets."""
        try:
            import urllib.request
            base = symbol.upper().strip().split("/")[0].split(":")[0]
            if not base.endswith("USDT"):
                base = base + "USDT"
            url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={base}"
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            funding = data.get("lastFundingRate")
            if funding is None:
                url2 = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={base}&limit=1"
                with urllib.request.urlopen(url2, timeout=10) as r2:
                    arr = json.loads(r2.read().decode("utf-8", "replace"))
                funding = arr[0].get("fundingRate") if arr else None
            return {
                "funding_rate": float(funding) if funding is not None else None,
                "next_funding_rate": None,
                "symbol": symbol,
            }
        except Exception as e:
            logger.debug("Funding rate fetch failed for %s: %s", symbol, str(e)[:100])
            return {"error": str(e)[:100]}

    # ------------------------------------------------------------------- #
    # ATH/DWD: distance from all-time high
    # ------------------------------------------------------------------- #
    def compute_ath_dwd(self, price_usd: Optional[float], change_24h: Optional[float]) -> Dict[str, Any]:
        """Heuristic ATH/DWD from CoinGecko data + 24h change.

        Asymmetric bands (v2.6): a 24h drawdown must breach
        `ath_bear_pct` (default -12%) to flag bear and a 24h rally must
        breach `ath_bull_pct` (default +15%) to flag bull. The wide neutral
        band between them stops a normal pullback from flipping the overlay."""
        if price_usd is None:
            return {"ath_dwd": None, "regime_hint": "unknown"}
        bear_pct = float(getattr(self.cfg, "ath_bear_pct", -12.0) or -12.0)
        bull_pct = float(getattr(self.cfg, "ath_bull_pct", 15.0) or 15.0)
        # If 24h change is very negative, likely far from ATH (distribution)
        if change_24h is not None and change_24h < bear_pct:
            return {"ath_dwd": "deep_drawdown", "regime_hint": "bear"}
        elif change_24h is not None and change_24h > bull_pct:
            return {"ath_dwd": "near_ath", "regime_hint": "bull"}
        return {"ath_dwd": "moderate", "regime_hint": "neutral"}

    # ------------------------------------------------------------------- #
    # Orchestrator: concurrent fan-out
    # ------------------------------------------------------------------- #
    async def gather_agent_context(
        self,
        coin_id: str,
        protocol_slug: str,
        prediction_query: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Dict[str, Any]:
        async def _do_gather(s: aiohttp.ClientSession) -> Dict[str, Any]:
            # TVL is optional: skip the DefiLlama call when no protocol given
            # (avoids a guaranteed 404 on /tvl/ with an empty slug).
            tvl_coro = (
                self.fetch_protocol_tvl(s, protocol_slug)
                if protocol_slug
                else asyncio.sleep(0, result={"skipped": True, "protocol": ""})
            )
            tasks = [
                self.fetch_token_metrics(s, coin_id),
                tvl_coro,
                self.fetch_polymarket_sentiment(s, prediction_query),
            ]
            results = await asyncio.gather(*tasks)
            return {
                "market_pricing": results[0],
                "defi_fundamentals": results[1],
                "macro_prediction_sentiment": results[2],
            }
        if session is None:
            async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as s:
                return await _do_gather(s)
        return await _do_gather(session)

    # ------------------------------------------------------------------- #
    # Synthesis for LLM / alert consumption
    # ------------------------------------------------------------------- #
    @staticmethod
    def synthesize_context(context: Dict[str, Any]) -> str:
        parts: List[str] = []
        pricing = context.get("market_pricing", {})
        if "error" in pricing:
            parts.append(f"Market pricing unavailable: {pricing['error']}")
        else:
            price = pricing.get("price_usd")
            vol = pricing.get("volume_24h_usd")
            chg = pricing.get("change_24h_percent")
            part = f"Token price ${price:,.2f}" if price is not None else "Token price N/A"
            if vol is not None:
                part += f", 24h vol ${vol:,.0f}"
            if chg is not None:
                part += f", 24h chg {chg:+.2f}%"
            parts.append(part)

        fundamentals = context.get("defi_fundamentals", {})
        if "error" in fundamentals:
            parts.append(f"DeFi fundamentals unavailable: {fundamentals['error']}")
        elif fundamentals.get("skipped"):
            pass  # no protocol tracked for this symbol — omit silently
        else:
            tvl = fundamentals.get("tvl_usd")
            proto = fundamentals.get("protocol", "protocol")
            parts.append(f"{proto.title()} TVL ${tvl:,.0f}" if tvl is not None else f"{proto.title()} TVL N/A")

        sentiment = context.get("macro_prediction_sentiment", [])
        if sentiment and isinstance(sentiment, list) and len(sentiment) > 0:
            first = sentiment[0]
            if "error" in first:
                parts.append(f"Prediction sentiment unavailable: {first['error']}")
            else:
                q = first.get("question", "unknown market")
                probs = first.get("outcome_probabilities")
                if isinstance(probs, list) and len(probs) >= 2:
                    try:
                        parts.append(f"Prediction '{q}': YES {float(probs[0]):.1%}, NO {float(probs[1]):.1%}")
                    except (ValueError, TypeError):
                        parts.append(f"Prediction '{q}' active")
                else:
                    parts.append(f"Prediction '{q}' active")
        else:
            parts.append("No prediction markets found")

        web = context.get("web_macro")
        if isinstance(web, dict) and web:
            try:
                parts.append(summarize_web_macro(web))
            except Exception:
                pass
        gweb = context.get("web_macro_global")
        if isinstance(gweb, dict) and (gweb.get("fear_greed") or gweb.get("global")):
            try:
                parts.append(summarize_cmc_global(gweb))
            except Exception:
                pass

        return " | ".join(parts)

    # ------------------------------------------------------------------- #
    # Cache + get_context (thread-safe, fail-open)
    # ------------------------------------------------------------------- #
    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < self.cfg.cache_ttl_seconds:
                return data
            del self._cache[key]
        return None

    def _cache_set(self, key: str, data: Dict[str, Any]):
        self._cache[key] = (time.time(), data)

    def get_context(self, symbol: str) -> Dict[str, Any]:
        """Synchronous getter for the engine's sync thread.
        Returns cached data or a 'stale' marker. Fail-open."""
        cached = self._cache_get(symbol)
        if cached is not None:
            return {**cached, "_from_cache": True}
        return {"_stale": True, "error": "macro data not yet available"}

    def set_context(self, symbol: str, data: Dict[str, Any]):
        """Called by the background async task to update the cache."""
        self._cache_set(symbol, data)

    # ------------------------------------------------------------------- #
    # Background task
    # ------------------------------------------------------------------- #
    async def start(self, symbols: List[str], protocols: Optional[List[str]] = None):
        """Start the background refresh task."""
        if not self.cfg.enabled:
            logger.info("MacroContextPipeline disabled — skipping start")
            return
        self._running = True
        protocols = protocols or self.cfg.tracked_protocols
        self._task = asyncio.create_task(self._background_loop(symbols, protocols))
        logger.info("MacroContextPipeline started for %d symbols, %d protocols", len(symbols), len(protocols))

    async def stop(self):
        """Stop the background task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MacroContextPipeline stopped")

    async def _background_loop(self, symbols: List[str], protocols: List[str]):
        """Periodically refresh macro context for all tracked symbols.

        CoinGecko is fetched ONCE per cycle as a batch (1 quota hit);
        per-symbol Polymarket + funding refreshes are stagger-started
        (~3s apart) against the generous endpoints.
        """
        while self._running:
            try:
                # Raised header limits: some editorial pages (Coinpedia) send
                # very large response headers that trip aiohttp's defaults.
                async with aiohttp.ClientSession(max_line_size=65536, max_field_size=65536) as session:
                    coin_ids = [
                        self.cfg.tracked_coins.get(s, s.lower().replace("/", "-"))
                        for s in symbols
                    ]
                    batch_pricing = await self.fetch_token_metrics_batch(session, coin_ids)
                    # Shared web-macro pages, fetched once per cycle (fail-open).
                    try:
                        shared_pages = await self._fetch_shared_web_pages(session)
                    except Exception as e:
                        logger.debug("Shared web pages fetch failed (fail-open): %s", str(e)[:100])
                        shared_pages = {}

                    async def _delayed(i: int, sym: str, cid: str) -> None:
                        if i:
                            await asyncio.sleep(i * 1)
                        pr = batch_pricing.get(cid, {"error": "batch pricing unavailable"})
                        await self._refresh_symbol(session, sym, cid, pricing=pr, shared_pages=shared_pages)

                    await asyncio.gather(
                        *(_delayed(i, s, c) for i, (s, c) in enumerate(zip(symbols, coin_ids))),
                        return_exceptions=True,
                    )

                    # Refresh protocols
                    for proto in protocols:
                        await self._refresh_protocol(session, proto)

            except Exception as e:
                logger.error("MacroContextPipeline background loop error: %s", str(e)[:200])

            await asyncio.sleep(self.cfg.cache_ttl_seconds)

    async def _refresh_symbol(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        coin_id: str,
        pricing: Optional[Dict[str, Any]] = None,
        shared_pages: Optional[Dict[str, Any]] = None,
    ):
        """Fetch and cache macro data for a single symbol.

        Pricing comes pre-fetched from the per-cycle batch call; only
        Polymarket sentiment + funding are fetched here. Web macro providers
        (info/context only) refresh here too — fail-open, never blocking.
        """
        try:
            if pricing is None:
                pricing = await self.fetch_token_metrics(session, coin_id)
            sentiment = await self.fetch_polymarket_sentiment(
                session, symbol.split("/")[0] if "/" in symbol else symbol
            )
            # Add funding rate (async, thread-pooled ccxt — must be awaited)
            fr = await self.fetch_funding_rate(symbol)
            # Add ATH/DWD
            price = pricing.get("price_usd") if isinstance(pricing, dict) else None
            chg = pricing.get("change_24h_percent") if isinstance(pricing, dict) else None
            ath_dwd = self.compute_ath_dwd(price, chg)
            # Web macro context (info-only): reuse fresh records, fail-open.
            base = (symbol.split("/")[0] if "/" in symbol else symbol).split(":")[0]
            prev_ctx = self._cache.get(symbol)
            prev_web = prev_ctx[1].get("web_macro") if isinstance(prev_ctx, tuple) else None

            # Bind the per-cycle session. refresh_web_macro invokes its fetch
            # callable as fetch_text_fn(url, timeout) — 2 args — whereas
            # _fetch_text takes (session, url, timeout). Passing the unbound
            # method raised TypeError on every crypto fetch and blanked the
            # whole web_macro context (web_total=0).
            async def _web_fetch(url: str, timeout: float) -> Dict[str, Any]:
                return await self._fetch_text(session, url, timeout)

            try:
                web_macro = await refresh_web_macro(
                    _web_fetch, symbol, base, coin_id,
                    sources=self.cfg.web_sources,
                    shared=shared_pages or {},
                    previous=prev_web if isinstance(prev_web, dict) else {},
                    cmc_api_key=self.cfg.cmc_api_key or "",
                )
            except Exception as e:
                logger.debug("Web macro refresh failed for %s (fail-open): %s", symbol, str(e)[:100])
                web_macro = prev_web if isinstance(prev_web, dict) else {}
            # CoinMarketCap market-wide context (fear/greed + global metrics):
            # symbol-independent, cached on its own TTL, attached to every
            # symbol context as info-only context.
            try:
                _cmc = (shared_pages or {}).get("cmc") or {}
                if _cmc.get("fear_greed") or _cmc.get("global"):
                    self._cmc_global = {
                        "retrieved_at": _cmc.get("retrieved_at"),
                        "keyed": bool(_cmc.get("keyed")),
                        "fear_greed": _cmc.get("fear_greed"),
                        "global": _cmc.get("global"),
                        "errors": _cmc.get("errors") or {},
                    }
                    self._cmc_global_ts = time.time()
                web_macro_global: Dict[str, Any] = {}
                if self._cmc_global and (time.time() - self._cmc_global_ts) < CMC_GLOBAL_TTL_SECONDS:
                    web_macro_global = self._cmc_global
            except Exception as e:
                logger.debug("CMC global attach failed for %s (fail-open): %s", symbol, str(e)[:100])
                web_macro_global = {}

            full_context = {
                "market_pricing": pricing,
                "defi_fundamentals": {"skipped": True, "protocol": ""},
                "macro_prediction_sentiment": sentiment,
                "funding_rate": fr.get("funding_rate"),
                "next_funding_rate": fr.get("next_funding_rate"),
                "ath_dwd": ath_dwd,
                "web_macro": web_macro,
                "web_macro_global": web_macro_global,
            }
            self.set_context(symbol, full_context)
            logger.debug("Macro context refreshed for %s: price=%s, regime=%s", symbol, price, ath_dwd.get("regime_hint"))
        except Exception as e:
            logger.error("Failed to refresh macro for %s: %s", symbol, str(e)[:100])

    async def _refresh_protocol(self, session: aiohttp.ClientSession, protocol: str):
        """Fetch and cache TVL for a protocol."""
        try:
            result = await self.fetch_protocol_tvl(session, protocol)
            self.set_context(f"proto_{protocol}", result)
        except Exception as e:
            logger.error("Failed to refresh protocol %s: %s", protocol, str(e)[:100])
