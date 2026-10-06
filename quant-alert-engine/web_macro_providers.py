"""
Web macro providers for the Quant Alert Engine (info/context only).

Mandatory-attempt, fail-open web context layer that complements the
structured macro pipeline (CoinGecko + Polymarket + funding). These sources
are NEVER used to gate signals or adjust bias scores — they only feed the
Macro Context section of the quant analysis output.

Providers (all attempted for every requested crypto pair):
  - coinstats        CoinStats bubbles snapshot (price/mcap/24h vol/change)
  - tradingkey_ta    TradingKey per-symbol technical snapshot (indicators/MAs)
  - tradingkey_movers TradingKey market-movers article (drivers/risks narrative)
  - diariobitcoin    DiarioBitcoin simbolo strip + latest analysis article
  - coinpedia        Coinpedia coin page headlines (narrative only)

Non-crypto symbols return {"status": "not_covered"} per source.
Any fetch/parse failure returns {"status": "unavailable", ...} — fail-open.
"""

import html as _html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PARSER_VERSION = "webmacro/v1"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QAE-macro/1.0"

# Bases treated as crypto (everything else -> not_covered for web sources).
CRYPTO_BASES = {
    "btc", "eth", "sol", "sui", "xrp", "hype", "doge", "ada", "avax",
    "link", "ton", "trx", "dot", "matic", "arb", "op", "inj", "sei",
    "apt", "near", "atom", "ltc", "bch", "fil", "etc", "bnb", "xlm",
    "uni", "aave", "shib", "wif", "pepe", "ondo", "jup", "render",
}

COINSTATS_SLUGS = {"sol": "solana", "xrp": "ripple", "sui": "sui", "hype": "hyperliquid"}
TRADINGKEY_TA_SLUGS = {"sol": "solana", "xrp": "xrp", "sui": "sui"}
DIARIO_QUERY = {"sol": "solana", "xrp": "xrp", "sui": "sui", "hype": "hyperliquid"}
DB_SLUG_TOKENS = {
    "sol": ("solana-", "/sol-"),
    "xrp": ("xrp-", "ripple-"),
    "sui": ("-sui-", "sui-"),
    "hype": ("hyperliquid-", "-hype-"),
}

DEFAULT_WEB_SOURCES: List[Dict[str, Any]] = [
    {"id": "coinstats", "enabled": True, "ttl_seconds": 120.0, "timeout_seconds": 12.0, "confidence": "high"},
    {"id": "coinmarketcap", "enabled": True, "ttl_seconds": 120.0, "timeout_seconds": 12.0, "confidence": "high"},
    {"id": "tradingkey_ta", "enabled": True, "ttl_seconds": 300.0, "timeout_seconds": 12.0, "confidence": "medium"},
    # TradingKey Movers is DISABLED by default (2026-10-05).
    # It is structurally dead for this engine, not transiently broken:
    #   * /technical-analysis/<slug> works, but the movers desk
    #     (https://www.tradingkey.com/news/market-movers) now publishes
    #     ONLY US equities — 20/20 slugs are tickers like mrna/vlo/cbrs,
    #     0/20 crypto, and the per-symbol URL shape the parser needs
    #     (/news/market-movers/<n>-market-movers-<sym>-usd-<date>) no longer
    #     exists (current slugs are /news/market-movers/<n>-market-movers-<ticker>-<date>).
    #   * Measured 0/7 symbols fresh. It is confidence:"low" and feeds no
    #     alert field, so every cycle was a guaranteed-useless fetch plus a
    #     "no movers article for symbol" record that counted against the
    #     freshness metric.
    # Re-enable only if TradingKey restores crypto movers coverage.
    {"id": "tradingkey_movers", "enabled": False, "ttl_seconds": 900.0, "timeout_seconds": 12.0, "confidence": "low"},
    {"id": "diariobitcoin", "enabled": True, "ttl_seconds": 900.0, "timeout_seconds": 12.0, "confidence": "medium"},
    {"id": "coinpedia", "enabled": True, "ttl_seconds": 1800.0, "timeout_seconds": 12.0, "confidence": "low"},
]

SOURCE_LABELS = {
    "coinstats": "CoinStats",
    "tradingkey_ta": "TradingKey TA",
    "tradingkey_movers": "TradingKey Movers",
    "diariobitcoin": "DiarioBitcoin",
    "coinpedia": "Coinpedia",
    "coinmarketcap": "CoinMarketCap",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(raw: Any) -> Optional[float]:
    """Parse US- or Spanish-formatted numbers ($59.85B, 102,18, -2.152)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace("$", "").replace("%", "").replace(" ", "")
    if not s:
        return None
    mult = 1.0
    if s[-1:] in ("B", "b"):
        mult, s = 1e9, s[:-1]
    elif s[-1:] in ("M", "m"):
        mult, s = 1e6, s[:-1]
    elif s[-1:] in ("K", "k"):
        mult, s = 1e3, s[:-1]
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s) * mult
    except ValueError:
        return None


def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = _html.unescape(h)
    return re.sub(r"\s+", " ", h).strip()


def make_record(
    source_id: str,
    url: str,
    status: str,
    data: Optional[Dict[str, Any]] = None,
    confidence: str = "low",
    notes: str = "",
    ttl_seconds: float = 300.0,
    retrieved_at: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "source_id": source_id,
        "parser": PARSER_VERSION,
        "url": url,
        "status": status,  # fresh|stale|unavailable|not_covered
        "retrieved_at": retrieved_at or utcnow_iso(),
        "ttl_seconds": ttl_seconds,
        "confidence": confidence,
        "notes": notes,
        "data": data or {},
    }


def is_fresh(rec: Dict[str, Any], now: Optional[float] = None) -> bool:
    if not isinstance(rec, dict) or rec.get("status") != "fresh":
        return False
    try:
        ts = datetime.fromisoformat(str(rec.get("retrieved_at", ""))).timestamp()
    except (ValueError, TypeError):
        return False
    now = time.time() if now is None else now
    try:
        ttl = float(rec.get("ttl_seconds", 300.0))
    except (TypeError, ValueError):
        ttl = 300.0
    return (now - ts) < ttl


async def fetch_text(session: Any, url: str, timeout: float, max_retries: int = 2) -> Dict[str, Any]:
    """GET a page as text. 404/410 -> not covered (no retry); 5xx -> retry."""
    import asyncio

    import aiohttp

    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            async with session.get(
                url,
                headers={"User-Agent": USER_AGENT, "accept": "text/html"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                body = await resp.text()
                if resp.status == 200:
                    return {"ok": True, "text": body, "status": 200}
                if resp.status in (404, 410):
                    return {"ok": False, "status": resp.status, "error": f"HTTP {resp.status} (not covered)"}
                last_err = f"HTTP {resp.status}"
                if resp.status in (429, 502, 503, 504):
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                return {"ok": False, "status": resp.status, "error": last_err}
        except Exception as e:  # timeout / connection errors -> retry
            last_err = f"{type(e).__name__}: {str(e)[:100]}"
            await asyncio.sleep(min(2 ** attempt, 8))
    return {"ok": False, "status": 0, "error": last_err or "fetch failed"}


# --------------------------------------------------------------------------- #
# CoinStats bubbles snapshot
# --------------------------------------------------------------------------- #
def parse_coinstats_bubbles(html: str, slug: str) -> Optional[Dict[str, Any]]:
    m_time = re.search(r'<time dateTime="([^"]+)"', html)
    anchor = html.find("bubbles-coin-link-" + slug)
    if anchor < 0:
        return None
    seg = html[anchor:anchor + 6000]
    out: Dict[str, Any] = {}
    if m_time:
        out["as_of"] = m_time.group(1)
    for col, key in (
        ("price", "price_usd"),
        ("marketCap", "market_cap_usd"),
        ("volume24h", "volume_24h_usd"),
        ("percentChange1h", "change_1h_percent"),
        ("percentChange24h", "change_24h_percent"),
    ):
        m = re.search(r'data-column-id="' + col + r'"[^>]*data-value="([^"]+)"', seg)
        if m:
            out[key] = _num(m.group(1))
    if out.get("price_usd") is None:
        return None
    return out


# --------------------------------------------------------------------------- #
# TradingKey technical snapshot
# --------------------------------------------------------------------------- #
_WANTED_INDICATORS = {
    "MACD(12,26,9)", "RSI(14)", "STOCH(KDJ)(9,3,3)", "ATR(14)",
    "CCI(14)", "Williams %R", "TRIX(12,20)", "StochRSI(14)",
}
_WANTED_MAS = {"MA5", "MA10", "MA20", "MA50", "MA100", "MA200"}


def parse_tradingkey_ta(html: str) -> Optional[Dict[str, Any]]:
    out: Dict[str, Any] = {}
    m = re.search(r'property="og:title" content="([^"]+)"', html)
    if m:
        parts = m.group(1).split("|")
        for p in parts:
            if p.startswith("Price:"):
                out["headline_price"] = _num(p.split(":", 1)[1])
            elif p.startswith("Chg%:"):
                out["headline_change_pct"] = _num(p.split(":", 1)[1])
    for gauge in ("Technical Indicators", "Summary", "Moving Average"):
        g = re.search(re.escape(gauge) + r".{0,1200}?class=\"result-[a-z0-9\- ]*\">([^<]+)</div>", html, re.DOTALL)
        if g:
            out["gauge_" + gauge.lower().replace(" ", "_")] = g.group(1).strip()
    rows = re.findall(
        r"<tr><td>([^<]+)</td><td>([^<]+)</td><td[^>]*><div[^>]*>([^<]+)</div></td></tr>", html
    )
    indicators: Dict[str, Any] = {}
    mas: Dict[str, Any] = {}
    for name, val, direction in rows:
        name = name.strip()
        if name in _WANTED_INDICATORS:
            indicators[name] = {"value": _num(val), "direction": direction.strip()}
        elif name in _WANTED_MAS:
            mas[name] = {"value": _num(val), "direction": direction.strip()}
    if indicators:
        out["indicators"] = indicators
    if mas:
        out["moving_averages"] = mas
    if not out:
        return None
    return out


def discover_tradingkey_movers(listing_html: str, base: str) -> Optional[str]:
    m = re.search(r'href="(/news/market-movers/\d+-market-movers-' + re.escape(base.lower()) + r'usd-\d+)"', listing_html)
    if not m:
        return None
    return "https://www.tradingkey.com" + m.group(1)


def parse_tradingkey_movers(html: str) -> Optional[Dict[str, Any]]:
    text = html_to_text(html)
    out: Dict[str, Any] = {}
    m = re.search(r"[Ii]s (up|down) ([\d.]+)% on ([A-Za-z]+ \d+)", text)
    if m:
        out["direction"], out["move_pct"], out["move_date"] = m.group(1), _num(m.group(2)), m.group(3)
    m = re.search(r"now at \$([\d,\.]+)", text)
    if m:
        out["price_usd"] = _num(m.group(1))
    m = re.search(r"7-day (up|down) of ([\d\.]+)%", text)
    if m:
        out["change_7d_percent"] = _num(m.group(2)) * (1 if m.group(1) == "up" else -1)
    tech: Dict[str, Any] = {}
    m = re.search(r"MACD \(12,26,9\) value of ([\-\d\.]+)", text)
    if m:
        tech["macd"] = _num(m.group(1))
    m = re.search(r"RSI at ([\d\.]+)", text)
    if m:
        tech["rsi"] = _num(m.group(1))
    m = re.search(r"Williams %R at ([\-\d\.]+)", text)
    if m:
        tech["williams_r"] = _num(m.group(1))
    if tech:
        out["technical"] = tech
    # Risk/narrative headers live in the "More details" section as <strong>.
    tail = html[html.find("More details about"):] if "More details about" in html else ""
    risks = [r.strip().rstrip(":") for r in re.findall(r"<strong>([^<]{8,140})</strong>", tail)][:6]
    if risks:
        out["risk_headers"] = risks
    if "AI-generated" in html:
        out["ai_reviewed"] = True
    if not out:
        return None
    return out


# --------------------------------------------------------------------------- #
# DiarioBitcoin: simbolo strip + latest analysis article
# --------------------------------------------------------------------------- #
def parse_diario_simbolo(html: str, symbol: str) -> Optional[Dict[str, Any]]:
    m = re.search(
        r'symbolContainer" title="([^"]+)">.*?simbolo/' + re.escape(symbol)
        + r'">\s*' + re.escape(symbol) + r'\s*</a>\s*</span>\s*([\d\., ]+?)\s*\(\s*'
        + r'<span class="(positive|negative)Change">\s*<a[^>]*>\s*([+\-\d\.,]+)%',
        html, re.DOTALL,
    )
    if not m:
        return None
    return {
        "name": m.group(1),
        "price": _num(m.group(2)),
        "change_24h_percent": _num(m.group(4)),
        "session_bias": m.group(3),
    }


def discover_diario_analysis(search_html: str, base: str) -> Optional[str]:
    links = re.findall(r'href="(https://www\.diariobitcoin\.com/analisis/[^"]+)"', search_html)
    tokens = DB_SLUG_TOKENS.get(base.lower(), (base.lower() + "-",))
    for link in dict.fromkeys(links):
        slug = link.rsplit("/", 2)[-2] if link.endswith("/") else link.rsplit("/", 1)[-1]
        if any(t in slug for t in tokens):
            return link
    return None


def parse_diario_analysis(html: str) -> Optional[Dict[str, Any]]:
    text = html_to_text(html)
    out: Dict[str, Any] = {}
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        out["title"] = _html.unescape(m.group(1)).strip()[:160]
    m = re.search(r"Fecha:\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        out["article_date"] = m.group(1)
    for pat in (r"cotiza en USD \$([\d\.,]+)", r"se negocia en USD \$([\d\.,]+)", r"Precio de USD \$([\d\.,]+)"):
        m = re.search(pat, text)
        if m:
            out["price_usd"] = _num(m.group(1))
            break
    m = re.search(r"RSI(?: \(14\))? en ([\d\.,]+)", text)
    if m:
        out["rsi"] = _num(m.group(1))
    m = re.search(r"histograma (?:negativo )?de ([\-\d\.,]+)", text)
    if m:
        out["macd_histogram"] = _num(m.group(1))
    m = re.search(r"VWAP.*?USD \$([\d\.,]+)", text)
    if m:
        out["vwap"] = _num(m.group(1))
    m = re.search(r"ATR de \$([\d\.,]+)", text)
    if m:
        out["atr"] = _num(m.group(1))
    supports, resistances = [], []
    for kind, val in re.findall(r"(Soporte|Resistencia)\s+\d+\s+([\d\.,]+)", text):
        (supports if kind == "Soporte" else resistances).append(_num(val))
    if supports:
        out["supports"] = supports[:4]
    if resistances:
        out["resistances"] = resistances[:4]
    if not out:
        return None
    return out


# --------------------------------------------------------------------------- #
# Coinpedia coin page (narrative headlines only — low confidence)
# --------------------------------------------------------------------------- #
def parse_coinpedia_coin(html: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"<title>([^<]+)</title>", html)
    title = _html.unescape(m.group(1)).strip()[:160] if m else ""
    heads = [_html.unescape(x).strip()[:160] for x in re.findall(r"<a[^>]+>([^<]*[Pp]rice [Pp]rediction[^<]*)</a>", html)]
    heads = list(dict.fromkeys(heads))[:5]
    if not title and not heads:
        return None
    return {"page_title": title, "prediction_headlines": heads}


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
async def _fetch_providers(
    fetch_text_fn: Any,
    jobs: List[Tuple[str, str, float]],
) -> Dict[str, Dict[str, Any]]:
    import asyncio

    async def _one(key: str, url: str, timeout: float) -> Tuple[str, Dict[str, Any]]:
        try:
            return key, await fetch_text_fn(url, timeout)
        except Exception as e:
            return key, {"ok": False, "status": 0, "error": f"{type(e).__name__}: {str(e)[:100]}"}

    out: Dict[str, Dict[str, Any]] = {}
    for key, res in await asyncio.gather(*(_one(k, u, t) for k, u, t in jobs)):
        out[key] = res
    return out


async def refresh_web_macro(
    fetch_text_fn: Any,
    symbol: str,
    base: str,
    coin_id: str,
    sources: Optional[List[Dict[str, Any]]] = None,
    shared: Optional[Dict[str, Any]] = None,
    previous: Optional[Dict[str, Any]] = None,
    cmc_api_key: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Refresh all mandatory web-macro providers for one symbol (fail-open)."""
    base_l = str(base).lower()
    crypto = base_l in CRYPTO_BASES
    defs = {d.get("id"): d for d in (sources or DEFAULT_WEB_SOURCES) if isinstance(d, dict) and d.get("id")}
    shared = shared or {}
    previous = previous or {}
    now = time.time()
    out: Dict[str, Dict[str, Any]] = {}

    # Defensive fail-open: wrap the fetch callable so a single provider fetch
    # that raises can never abort the whole fan-out. Without this, one bad
    # call discarded every already-built record and surfaced as web_total=0.
    _raw_fetch = fetch_text_fn

    async def fetch_text_fn(url: str, timeout: float, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[misc]
        try:
            return await _raw_fetch(url, timeout, *args, **kwargs)
        except Exception as e:  # noqa: BLE001 — fail-open by design
            logger.debug("web fetch raised (fail-open): %s: %s", type(e).__name__, str(e)[:100])
            return {"ok": False, "status": 0, "error": f"{type(e).__name__}: {str(e)[:100]}"}

    def _reuse_or_none(sid: str) -> Optional[Dict[str, Any]]:
        prev = previous.get(sid)
        if isinstance(prev, dict) and is_fresh(prev, now):
            return prev
        return None

    def _nc(sid: str, url: str, reason: str) -> Dict[str, Any]:
        d = defs.get(sid, {})
        return make_record(sid, url, "not_covered", {"reason": reason},
                           confidence=str(d.get("confidence", "low")),
                           notes=reason, ttl_seconds=float(d.get("ttl_seconds", 300.0)))

    def _un(sid: str, url: str, reason: str) -> Dict[str, Any]:
        d = defs.get(sid, {})
        return make_record(sid, url, "unavailable", {"reason": reason},
                           confidence=str(d.get("confidence", "low")),
                           notes=reason, ttl_seconds=float(d.get("ttl_seconds", 300.0)))

    # ---- coinstats (shared bubbles page) ---- #
    sid = "coinstats"
    if defs.get(sid, {}).get("enabled", True):
        reused = _reuse_or_none(sid)
        if reused is not None:
            out[sid] = reused
        elif not crypto or base_l not in COINSTATS_SLUGS:
            out[sid] = _nc(sid, "https://coinstats.app/crypto-bubbles/", "asset not covered by CoinStats bubbles")
        else:
            d = defs[sid]
            html = shared.get("coinstats_bubbles")
            if html is None:
                res = await fetch_text_fn("https://coinstats.app/crypto-bubbles/", float(d.get("timeout_seconds", 12.0)))
                html = res.get("text") if res.get("ok") else None
                if html is None:
                    out[sid] = _un(sid, "https://coinstats.app/crypto-bubbles/", str(res.get("error", "fetch failed")))
            if sid not in out and html is not None:
                data = parse_coinstats_bubbles(html, COINSTATS_SLUGS[base_l])
                if data is None:
                    out[sid] = _un(sid, "https://coinstats.app/crypto-bubbles/", "symbol row not found (selector changed?)")
                else:
                    out[sid] = make_record(sid, "https://coinstats.app/crypto-bubbles/", "fresh", data,
                                           confidence=str(d.get("confidence", "high")),
                                           ttl_seconds=float(d.get("ttl_seconds", 120.0)))

    # ---- coinmarketcap (shared batched quotes; per-symbol quotes ONLY) ---- #
    sid = "coinmarketcap"
    if defs.get(sid, {}).get("enabled", True):
        reused = _reuse_or_none(sid)
        if reused is not None:
            out[sid] = reused
        elif not crypto or base_l not in CMC_IDS:
            out[sid] = _nc(sid, cmc_base(cmc_api_key), "asset not covered by CoinMarketCap crypto API")
        else:
            d = defs[sid]
            cmc = (shared or {}).get("cmc") or {}
            entry = (cmc.get("quotes") or {}).get(str(CMC_IDS[base_l]))
            url = cmc_base(cmc_api_key) + "/v3/cryptocurrency/quotes/latest"
            if entry is None:
                out[sid] = _un(sid, url, str((cmc.get("errors") or {}).get("quotes", "quote missing from batch")))
            else:
                out[sid] = make_record(sid, url, "fresh", dict(entry),
                                       confidence=str(d.get("confidence", "high")),
                                       notes="keyed" if cmc.get("keyed") else "keyless",
                                       ttl_seconds=float(d.get("ttl_seconds", 120.0)))

    # ---- tradingkey_ta ---- #
    sid = "tradingkey_ta"
    if defs.get(sid, {}).get("enabled", True):
        reused = _reuse_or_none(sid)
        if reused is not None:
            out[sid] = reused
        elif not crypto or base_l not in TRADINGKEY_TA_SLUGS:
            out[sid] = _nc(sid, "https://www.tradingkey.com/markets/cryptocurrencies/", "no TradingKey TA page for asset")
        else:
            d = defs[sid]
            url = f"https://www.tradingkey.com/markets/cryptocurrencies/{TRADINGKEY_TA_SLUGS[base_l]}/technical-analysis"
            res = await fetch_text_fn(url, float(d.get("timeout_seconds", 12.0)))
            if not res.get("ok"):
                if res.get("status") in (404, 410):
                    out[sid] = _nc(sid, url, f"HTTP {res.get('status')} (no page for asset)")
                else:
                    out[sid] = _un(sid, url, str(res.get("error", "fetch failed")))
            else:
                data = parse_tradingkey_ta(res["text"])
                out[sid] = make_record(sid, url, "fresh", data, confidence=str(d.get("confidence", "medium")),
                                       ttl_seconds=float(d.get("ttl_seconds", 300.0))) if data else \
                    _un(sid, url, "indicator tables not found (selector changed?)")

    # ---- tradingkey_movers (listing discovery -> article) ---- #
    sid = "tradingkey_movers"
    if defs.get(sid, {}).get("enabled", True):
        reused = _reuse_or_none(sid)
        if reused is not None:
            out[sid] = reused
        elif not crypto:
            out[sid] = _nc(sid, "https://www.tradingkey.com/news/market-movers", "asset not covered by movers desk")
        else:
            d = defs[sid]
            listing = shared.get("tk_movers_listing")
            if listing is None:
                res = await fetch_text_fn("https://www.tradingkey.com/news/market-movers", float(d.get("timeout_seconds", 12.0)))
                listing = res.get("text") if res.get("ok") else None
                if listing is None:
                    out[sid] = _un(sid, "https://www.tradingkey.com/news/market-movers", str(res.get("error", "fetch failed")))
            if sid not in out and listing is not None:
                art_url = discover_tradingkey_movers(listing, base_l)
                if art_url is None:
                    out[sid] = _un(sid, "https://www.tradingkey.com/news/market-movers", "no movers article for symbol")
                else:
                    res = await fetch_text_fn(art_url, float(d.get("timeout_seconds", 12.0)))
                    if not res.get("ok"):
                        out[sid] = _un(sid, art_url, str(res.get("error", "fetch failed")))
                    else:
                        data = parse_tradingkey_movers(res["text"])
                        if data is None:
                            out[sid] = _un(sid, art_url, "article body not parseable")
                        else:
                            data["flags"] = ["ai_reviewed_possible"] if data.pop("ai_reviewed", False) else []
                            out[sid] = make_record(sid, art_url, "fresh", data,
                                                   confidence=str(d.get("confidence", "low")),
                                                   ttl_seconds=float(d.get("ttl_seconds", 900.0)))

    # ---- diariobitcoin (simbolo strip + analysis discovery) ---- #
    sid = "diariobitcoin"
    if defs.get(sid, {}).get("enabled", True):
        reused = _reuse_or_none(sid)
        if reused is not None:
            out[sid] = reused
        elif not crypto:
            out[sid] = _nc(sid, "https://www.diariobitcoin.com/criptomercados", "asset not covered")
        else:
            d = defs[sid]
            sym = base.upper()
            strip_url = f"https://www.diariobitcoin.com/simbolo/{sym}"
            res = await fetch_text_fn(strip_url, float(d.get("timeout_seconds", 12.0)))
            data: Dict[str, Any] = {}
            art_url: Optional[str] = None
            if res.get("ok"):
                strip = parse_diario_simbolo(res["text"], sym)
                if strip:
                    data["strip"] = strip
            q = DIARIO_QUERY.get(base_l, base_l)
            sres = await fetch_text_fn(f"https://www.diariobitcoin.com/?s={q}", float(d.get("timeout_seconds", 12.0)))
            if sres.get("ok"):
                art_url = discover_diario_analysis(sres["text"], base_l)
            if art_url:
                ares = await fetch_text_fn(art_url, float(d.get("timeout_seconds", 12.0)))
                if ares.get("ok"):
                    art = parse_diario_analysis(ares["text"])
                    if art:
                        data["analysis"] = art
                        data["analysis_url"] = art_url
            if not data:
                reason = "strip + analysis not parseable"
                if not res.get("ok"):
                    reason = str(res.get("error", reason))
                out[sid] = _un(sid, strip_url, reason)
            else:
                out[sid] = make_record(sid, art_url or strip_url, "fresh", data,
                                       confidence=str(d.get("confidence", "medium")),
                                       ttl_seconds=float(d.get("ttl_seconds", 900.0)))

    # ---- coinpedia (coin page headlines, narrative only) ---- #
    sid = "coinpedia"
    if defs.get(sid, {}).get("enabled", True):
        reused = _reuse_or_none(sid)
        if reused is not None:
            out[sid] = reused
        elif not crypto:
            out[sid] = _nc(sid, "https://coinpedia.org/", "asset not covered")
        else:
            d = defs[sid]
            slug = str(coin_id or base_l).lower()
            url = f"https://coinpedia.org/coins/{slug}/"
            res = await fetch_text_fn(url, float(d.get("timeout_seconds", 12.0)))
            if not res.get("ok"):
                if res.get("status") in (404, 410):
                    out[sid] = _nc(sid, url, f"HTTP {res.get('status')} (no coin page)")
                else:
                    out[sid] = _un(sid, url, str(res.get("error", "fetch failed")))
            else:
                data = parse_coinpedia_coin(res["text"])
                if data is None:
                    out[sid] = _un(sid, url, "coin page not parseable")
                else:
                    data["flags"] = ["editorial", "narrative_only"]
                    out[sid] = make_record(sid, url, "fresh", data,
                                           confidence=str(d.get("confidence", "low")),
                                           ttl_seconds=float(d.get("ttl_seconds", 1800.0)))

    return out


def summarize_web_macro(web: Optional[Dict[str, Any]]) -> str:
    """One-line info-only summary for synthesis/Telegram."""
    if not web:
        return "WebMacro: no data"
    bits = []
    cs = web.get("coinstats", {})
    if cs.get("status") == "fresh":
        dd = cs.get("data", {})
        bits.append(f"CoinStats ${dd.get('price_usd')} {dd.get('change_24h_percent', '?'):+}%/24h")
    ta = web.get("tradingkey_ta", {})
    if ta.get("status") == "fresh":
        dd = ta.get("data", {})
        bits.append(f"TK {dd.get('gauge_summary', '?')}/RSI {((dd.get('indicators') or {}).get('RSI(14)', {}) or {}).get('value', '?')}")
    cm = web.get("coinmarketcap", {})
    if cm.get("status") == "fresh":
        dd = cm.get("data", {})
        bits.append(f"CMC ${dd.get('price_usd')} {dd.get('change_24h_percent', '?'):+}%/24h")
    db = web.get("diariobitcoin", {})
    if db.get("status") == "fresh":
        dd = db.get("data", {})
        st = dd.get("strip") or {}
        an = dd.get("analysis") or {}
        px = st.get("price", an.get("price_usd", "?"))
        ch = st.get("change_24h_percent", "?")
        bits.append(f"DB {px} ({ch}%)")
    fresh = sum(1 for r in web.values() if isinstance(r, dict) and r.get("status") == "fresh")
    head = f"WebMacro {fresh}/{len(web)} fresh"
    return head + (": " + " | ".join(bits) if bits else ": unavailable")


def web_freshness(web: Optional[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    total, fresh, oldest = 0, 0, None
    # not_covered is a DELIBERATE outcome (the source does not cover this asset),
    # not a failure. Counting it against freshness made the health check report
    # "degraded" for symbols the engine never expected web macro for -- 3 of the
    # 7 tracked symbols (SPCX/NVDA/XAU) are equities/gold and correctly have no
    # crypto-web coverage. Report it separately so "broken" stays meaningful.
    not_covered, unavailable = 0, 0
    for r in (web or {}).values():
        if not isinstance(r, dict):
            continue
        total += 1
        status = r.get("status")
        if status == "not_covered":
            not_covered += 1
        elif status == "unavailable":
            unavailable += 1
        try:
            ts = datetime.fromisoformat(str(r.get("retrieved_at", ""))).timestamp()
        except (ValueError, TypeError):
            continue
        age = now - ts
        oldest = age if oldest is None or age > oldest else oldest
        if status == "fresh":
            try:
                ttl = float(r.get("ttl_seconds", 300.0))
            except (TypeError, ValueError):
                ttl = 300.0
            if age < ttl:
                fresh += 1
    # "eligible" = records this source could actually have covered.
    eligible = total - not_covered
    return {
        "total": total,
        "fresh": fresh,
        "eligible": eligible,
        "not_covered": not_covered,
        "unavailable": unavailable,
        # Fraction of ELIGIBLE records that are fresh. None when nothing is
        # eligible, so a caller cannot read 0/0 as "everything is broken".
        "coverage_pct": round(100.0 * fresh / eligible, 1) if eligible else None,
        "oldest_age_s": round(oldest, 1) if oldest is not None else None,
    }


# --------------------------------------------------------------------------- #
# CoinMarketCap: keyless-first JSON API (keyed upgrade via env key only).
#
# Keyless:  base https://pro-api.coinmarketcap.com/public-api, no headers.
# Keyed:    base https://pro-api.coinmarketcap.com + X-CMC_PRO_API_KEY header.
# Same response envelope both ways. Per-symbol data = quotes/latest ONLY;
# market-wide Fear & Greed + global metrics are fetched once per cycle as
# shared context. Info-only, fail-open, never gates signals.
# --------------------------------------------------------------------------- #
CMC_KEYLESS_BASE = "https://pro-api.coinmarketcap.com/public-api"
CMC_KEYED_BASE = "https://pro-api.coinmarketcap.com"

# Verified CoinMarketCap IDs (via /v1/cryptocurrency/map, is_active + rank).
CMC_IDS = {"btc": 1, "eth": 1027, "sol": 5426, "sui": 20947, "xrp": 52, "hype": 32196}

CMC_GLOBAL_TTL_SECONDS = 600.0


def cmc_base(api_key: str = "") -> str:
    return CMC_KEYED_BASE if api_key else CMC_KEYLESS_BASE


def cmc_headers(api_key: str = "") -> Dict[str, str]:
    h = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        # Key header is ONLY sent on the keyed base, never on keyless calls.
        h["X-CMC_PRO_API_KEY"] = api_key
    return h


def cmc_status_ok(status_obj: Any) -> bool:
    """error_code arrives as int (keyed) or str (keyless) — normalize."""
    if not isinstance(status_obj, dict):
        return False
    try:
        return int(str(status_obj.get("error_code", -1))) == 0
    except (TypeError, ValueError):
        return False


async def fetch_cmc_json(
    session: Any, url: str, api_key: str = "", timeout: float = 12.0, max_retries: int = 2
) -> Dict[str, Any]:
    import asyncio

    import aiohttp

    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            async with session.get(
                url,
                headers=cmc_headers(api_key),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                try:
                    body = await resp.json()
                except Exception:
                    body = {}
                if resp.status == 200 and cmc_status_ok(body.get("status")):
                    return {"ok": True, "data": body.get("data"), "status": 200}
                last_err = f"HTTP {resp.status} err={body.get('status') if isinstance(body, dict) else body}"
                if resp.status in (429, 502, 503, 504):
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                return {"ok": False, "status": resp.status, "error": last_err[:150]}
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:100]}"
            await asyncio.sleep(min(2 ** attempt, 8))
    return {"ok": False, "status": 0, "error": last_err or "fetch failed"}


def parse_cmc_quote(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        raw_q = entry.get("quote")
        if isinstance(raw_q, list):
            q = next((x for x in raw_q if isinstance(x, dict) and x.get("symbol") == "USD"), {}) or {}
        elif isinstance(raw_q, dict):
            q = raw_q.get("USD") or {}
        else:
            q = {}
        price = q.get("price")
        if price is None:
            return None
        return {
            "cmc_id": entry.get("id"),
            "cmc_rank": entry.get("cmc_rank"),
            "price_usd": float(price),
            "volume_24h_usd": q.get("volume_24h"),
            "volume_change_24h_pct": q.get("volume_change_24h"),
            "change_1h_percent": q.get("percent_change_1h"),
            "change_24h_percent": q.get("percent_change_24h"),
            "change_7d_percent": q.get("percent_change_7d"),
            "change_30d_percent": q.get("percent_change_30d"),
            "market_cap_usd": q.get("market_cap"),
            "market_cap_dominance_pct": q.get("market_cap_dominance"),
            "fully_diluted_market_cap_usd": q.get("fully_diluted_market_cap"),
            "circulating_supply": entry.get("circulating_supply"),
            "max_supply": entry.get("max_supply"),
            "num_market_pairs": entry.get("num_market_pairs"),
            "last_updated": q.get("last_updated") or entry.get("last_updated"),
        }
    except (TypeError, ValueError, AttributeError):
        return None


def parse_cmc_fear_greed(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    try:
        return {
            "value": int(data.get("value")),
            "classification": str(data.get("value_classification", "")),
            "update_time": data.get("update_time"),
        }
    except (TypeError, ValueError):
        return None


def parse_cmc_global(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    try:
        q = ((data.get("quote") or {}).get("USD")) or {}
        return {
            "total_market_cap_usd": q.get("total_market_cap"),
            "total_volume_24h_usd": q.get("total_volume_24h"),
            "btc_dominance_pct": data.get("btc_dominance"),
            "eth_dominance_pct": data.get("eth_dominance"),
            "active_cryptocurrencies": data.get("active_cryptocurrencies"),
            "last_updated": data.get("last_updated") or q.get("last_updated"),
        }
    except (TypeError, ValueError, AttributeError):
        return None


async def fetch_cmc_shared(
    session: Any,
    api_key: str = "",
    ids: Optional[List[int]] = None,
    timeout: float = 12.0,
    max_retries: int = 2,
) -> Dict[str, Any]:
    """Once-per-cycle CMC fetch: batched quotes + fear/greed + global metrics."""
    base = cmc_base(api_key)
    ids = ids or sorted(set(CMC_IDS.values()))

    async def _get(path: str) -> Dict[str, Any]:
        return await fetch_cmc_json(session, base + path, api_key, timeout, max_retries)

    import asyncio

    qres, fres, gres = await asyncio.gather(
        _get(f"/v3/cryptocurrency/quotes/latest?id={','.join(str(i) for i in ids)}&convert=USD"),
        _get("/v3/fear-and-greed/latest"),
        _get("/v1/global-metrics/quotes/latest?convert=USD"),
    )
    shared: Dict[str, Any] = {
        "keyed": bool(api_key),
        "retrieved_at": utcnow_iso(),
        "quotes": {},
        "fear_greed": None,
        "global": None,
        "errors": {},
    }
    if qres.get("ok") and isinstance(qres.get("data"), list):
        for entry in qres["data"]:
            if isinstance(entry, dict) and entry.get("id") is not None:
                parsed = parse_cmc_quote(entry)
                if parsed:
                    shared["quotes"][str(entry["id"])] = parsed
    else:
        shared["errors"]["quotes"] = str(qres.get("error", "fetch failed"))
    if fres.get("ok"):
        shared["fear_greed"] = parse_cmc_fear_greed(fres.get("data"))
        if shared["fear_greed"] is None:
            shared["errors"]["fear_greed"] = "unparseable body"
    else:
        shared["errors"]["fear_greed"] = str(fres.get("error", "fetch failed"))
    if gres.get("ok"):
        shared["global"] = parse_cmc_global(gres.get("data"))
        if shared["global"] is None:
            shared["errors"]["global"] = "unparseable body"
    else:
        shared["errors"]["global"] = str(gres.get("error", "fetch failed"))
    return shared


def summarize_cmc_global(shared: Optional[Dict[str, Any]]) -> str:
    if not shared:
        return "CMC global: no data"
    bits = []
    fg = shared.get("fear_greed") or {}
    if fg.get("value") is not None:
        bits.append(f"Fear&Greed {fg['value']} ({fg.get('classification', '?')})")
    gl = shared.get("global") or {}
    if gl.get("btc_dominance_pct") is not None:
        bits.append(f"BTC dom {float(gl['btc_dominance_pct']):.1f}%")
    if gl.get("total_market_cap_usd") is not None:
        bits.append(f"Total mcap ${float(gl['total_market_cap_usd']):,.0f}")
    mode = "keyed" if shared.get("keyed") else "keyless"
    head = f"CMC global [{mode}]"
    return head + (": " + " | ".join(bits) if bits else ": unavailable")
