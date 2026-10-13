"""
polymarket_direct.py — Polymarket sentiment, standalone. NO depende de QAE.

Por que existe
==============
La via unica para leer Polymarket era el pipeline macro de QAE
(macro_context_pipeline.fetch_polymarket_sentiment). Desde 2026-10-11 QAE es
manual-only, asi que la fuente 11 del skill se rompia. Este modulo reproduce
la lectura sin importar nada de QAE: solo stdlib.

Endpoints (sin credenciales)
============================
    GET https://gamma-api.polymarket.com/events
        ?closed=false&active=true&tag_slug=<tag>&limit=N&order=volume24hr

Por que `/events` y no `/public-search`
--------------------------------------
`/public-search?q=XRP` devuelve mercados YA CERRADOS y de meses atras (probado
el 2026-10-11: 10 eventos, todos closed=True, vol24hr=None). Es un indice
de volumen historico, no un mercado vivo. `/events?tag_slug=` con
`closed=false&active=true` devuelve los mercados abiertos reales, ordenados
por volumen de 24h — que es lo que un analisis necesita.

El filtro por tag obviapasa el problema que el propio pipeline de QAE
documentaba: su fallback `/events` volume-sorted devolvia mercados de deporte
en una busqueda de Bitcoin. Filtrar por tag resuelve en origen.

Uso
===
    python polymarket_direct.py xrp
    python polymarket_direct.py sol 8
Salida JSON por stdout: {"query", "tag", "markets": [{question, end_date,
outcomes, pct, volume}]}
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

GAMMA = "https://gamma-api.polymarket.com"
TIMEOUT = 15.0

# Slugs de tag que Polymarket usa por activo. El tag no siempre es el simbolo.
TAGS: Dict[str, List[str]] = {
    "xrp": ["xrp", "ripple"],
    "sol": ["sol", "solana"],
    "sui": ["sui"],
    "hype": ["hype", "hyperliquid"],
    "btc": ["bitcoin", "btc"],
    "eth": ["ethereum", "eth"],
    "xau": ["gold", "xau"],
}


def _get(url: str, params: Dict[str, str]) -> Any:
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{q}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; quant-reader/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _as_list(v: Any) -> Any:
    """Polymarket devuelve outcomePrices/outcomes como JSON-string."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return v
    return v


def _markets_from(events: Any) -> List[Dict[str, Any]]:
    out = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        for m in ev.get("markets") or []:
            out.append({
                "question": m.get("question"),
                "slug": m.get("slug"),
                "condition_id": m.get("conditionId"),
                "outcomes": _as_list(m.get("outcomes")),
                "outcome_probabilities": _as_list(
                    m.get("outcomePrices") or m.get("prices")),
                "volume": m.get("volume") or ev.get("volume"),
                "volume24hr": ev.get("volume24hr"),
                "end_date": m.get("endDate"),
                "closed": m.get("closed"),
            })
    return out


def fetch(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Mercados ABIERTOS que mencionan `query`. Prioriza tag, luego search."""
    key = query.lower().strip()
    errors: List[str] = []

    for tag in TAGS.get(key, [key]):
        try:
            data = _get(f"{GAMMA}/events", {
                "closed": "false",
                "active": "true",
                "tag_slug": tag,
                "limit": str(max(limit * 3, 15)),
                "order": "volume24hr",
                "ascending": "false",
            })
        except Exception as e:
            errors.append(f"tag={tag}: {type(e).__name__}")
            continue

        events = data if isinstance(data, list) else (data.get("events") or [])
        rows = [m for m in _markets_from(events) if not m.get("closed")]
        if rows:
            rows.sort(key=lambda r: (r.get("volume24hr") or 0), reverse=True)
            return rows[:limit]

    # Sin tag: buscar, aunque suele devolver historico cerrado.
    try:
        res = _get(f"{GAMMA}/public-search", {"q": query})
        rows = [m for m in _markets_from(res.get("events")) if not m.get("closed")]
        return rows[:limit]
    except Exception as e:
        errors.append(f"search: {type(e).__name__}")

    return [{"error": "sin mercados abiertos",
             "detail": "; ".join(errors) or "tag sin resultados y search fallo"}]


def summarize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """YES/NO (o Up/Down) en porcentaje. Descarta lo que no se puede parsear."""
    out = []
    for r in rows:
        if "error" in r:
            out.append(r)
            continue
        probs, outs = r.get("outcome_probabilities"), r.get("outcomes")
        if not isinstance(probs, list) or not isinstance(outs, list):
            continue
        if len(probs) != len(outs):
            continue
        try:
            pct = [round(float(p) * 100, 2) for p in probs]
        except (TypeError, ValueError):
            continue
        out.append({
            "question": r.get("question"),
            "end_date": r.get("end_date"),
            "outcomes": outs,
            "pct": pct,
            "volume24hr": r.get("volume24hr"),
        })
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("uso: python polymarket_direct.py <XRP|SOL|...> [limit]",
              file=sys.stderr)
        return 2
    query = args[0]
    limit = int(args[1]) if len(args) > 1 else 5

    rows = fetch(query, limit)
    if rows and "error" in rows[0]:
        print(json.dumps({"query": query, **rows[0]}, ensure_ascii=False))
        return 1

    live = summarize(rows)
    print(json.dumps({"query": query, "open_markets": len(live),
                      "markets": live}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())