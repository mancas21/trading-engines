"""
event_blackout.py — Event blackout windows (FOMC/CPI/NFP + crypto majors).

Provides a static list of high-impact events and helpers to check if
a given timestamp falls inside a blackout window. Falls back to free
crypto event APIs when available but never requires network to function.

Endpoint GET /blackout/status is mounted by bridge_server if this module is importable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
import os
import json as _json

try:
    from urllib.request import urlopen
    HAS_URLLIB = True
except ImportError:
    HAS_URLLIB = False

# ---------------------------------------------------------------------------
# Static high-impact events (UTC).
# Update quarterly. Times are the scheduled release in UTC.
# Blackout window: by default ±60 min around the event (configurable).
# ---------------------------------------------------------------------------
@dataclass
class BlackoutEvent:
    name: str
    ts_utc: float  # epoch seconds UTC
    kind: str  # "FOMC" | "CPI" | "NFP" | "PPI" | "CRYPTO" | "OTHER"
    window_min: int = 60  # minutes each side
    source: str = "static"

    def to_dict(self) -> dict:
        dt = datetime.fromtimestamp(self.ts_utc, tz=timezone.utc)
        return {
            "name": self.name,
            "kind": self.kind,
            "ts_utc": self.ts_utc,
            "iso_utc": dt.isoformat(),
            "window_min": self.window_min,
            "source": self.source,
            "blackout_start": self.ts_utc - self.window_min * 60,
            "blackout_end": self.ts_utc + self.window_min * 60,
        }


# Static calendar: H2 2026 + early 2027 (approximate scheduled times)
# FOMC statements typically 18:00 UTC (14:00 ET), CPI/NFP 12:30 UTC (08:30 ET)
def _static_events() -> list[BlackoutEvent]:
    def _utc(y, m, d, hh=12, mm=30):
        return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp()

    return [
        # 2026 FOMC (remaining)
        BlackoutEvent("FOMC Statement", _utc(2026, 9, 16, 18, 0), "FOMC", 75),
        BlackoutEvent("FOMC Statement", _utc(2026, 10, 28, 18, 0), "FOMC", 75),
        BlackoutEvent("FOMC Statement", _utc(2026, 12, 9, 18, 0), "FOMC", 75),
        # 2026 CPI
        BlackoutEvent("US CPI", _utc(2026, 9, 11, 12, 30), "CPI", 45),
        BlackoutEvent("US CPI", _utc(2026, 10, 15, 12, 30), "CPI", 45),
        BlackoutEvent("US CPI", _utc(2026, 11, 13, 12, 30), "CPI", 45),
        BlackoutEvent("US CPI", _utc(2026, 12, 10, 12, 30), "CPI", 45),
        # 2026 NFP
        BlackoutEvent("US NFP", _utc(2026, 9, 4, 12, 30), "NFP", 45),
        BlackoutEvent("US NFP", _utc(2026, 10, 2, 12, 30), "NFP", 45),
        BlackoutEvent("US NFP", _utc(2026, 11, 6, 12, 30), "NFP", 45),
        BlackoutEvent("US NFP", _utc(2026, 12, 4, 12, 30), "NFP", 45),
        # PPI (also market-moving for crypto)
        BlackoutEvent("US PPI", _utc(2026, 9, 10, 12, 30), "PPI", 30),
        BlackoutEvent("US PPI", _utc(2026, 10, 14, 12, 30), "PPI", 30),
        # Crypto-specific: major unlocks / halving-adjacent (illustrative)
        BlackoutEvent("BTC Options Expiry (Deribit)", _utc(2026, 9, 25, 8, 0), "CRYPTO", 30),
        BlackoutEvent("BTC Options Expiry (Deribit)", _utc(2026, 12, 25, 8, 0), "CRYPTO", 30),
        # Early 2027
        BlackoutEvent("FOMC Statement", _utc(2027, 1, 27, 18, 0), "FOMC", 75),
        BlackoutEvent("US CPI", _utc(2027, 1, 13, 12, 30), "CPI", 45),
        BlackoutEvent("US NFP", _utc(2027, 1, 8, 12, 30), "NFP", 45),
    ]


# Cache for fetched events
_FETCH_CACHE: dict = {"ts": 0.0, "events": []}
_FETCH_TTL = 3600 * 6  # 6h


def _fetch_crypto_events() -> list[BlackoutEvent]:
    """Best-effort fetch from free crypto calendar APIs. Never raises."""
    if not HAS_URLLIB:
        return []
    now = time.time()
    if now - _FETCH_CACHE["ts"] < _FETCH_TTL and _FETCH_CACHE["events"]:
        return _FETCH_CACHE["events"]
    events: list[BlackoutEvent] = []
    # Example: try CoinMarketCal or similar free endpoint; keep optional.
    # We use a very conservative approach — only parse if response looks valid.
    # Default enabled via env CRYPTO_EVENTS_URL, otherwise skip network.
    url = os.getenv("CRYPTO_EVENTS_URL", "").strip()
    if not url:
        return []
    try:
        raw = urlopen(url, timeout=4).read().decode("utf-8", errors="replace")
        data = _json.loads(raw)
        # Expect list of {title, date, importance}
        items = data if isinstance(data, list) else data.get("events", data.get("data", []))
        for it in items[:20]:
            title = str(it.get("title", it.get("name", ""))).strip()
            date_str = str(it.get("date", it.get("timestamp", it.get("ts", "")))).strip()
            if not title or not date_str:
                continue
            try:
                # Try ISO parse
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
            except Exception:
                continue
            # Only future events within 30 days
            if ts < now - 3600 or ts > now + 30 * 86400:
                continue
            events.append(BlackoutEvent(title, ts, "CRYPTO", 30, source="remote"))
        _FETCH_CACHE["ts"] = now
        _FETCH_CACHE["events"] = events
    except Exception:
        pass
    return events


def all_events() -> list[BlackoutEvent]:
    """Combined static + fetched events, sorted by ts."""
    evs = list(_static_events())
    try:
        evs.extend(_fetch_crypto_events())
    except Exception:
        pass
    evs.sort(key=lambda e: e.ts_utc)
    return evs


def is_blackout(now: float | None = None, window_override_min: int | None = None) -> tuple[bool, BlackoutEvent | None, float]:
    """
    Check if `now` (epoch seconds, default time.time()) is inside any blackout window.

    Returns (is_blackout, event_or_None, seconds_remaining_in_window).
    seconds_remaining = max(0, event_end - now) if blackout else time_to_next_event.
    """
    t = now if now is not None else time.time()
    for ev in all_events():
        w = window_override_min if window_override_min is not None else ev.window_min
        start = ev.ts_utc - w * 60
        end = ev.ts_utc + w * 60
        if start <= t <= end:
            return True, ev, end - t
    # Not in blackout: time to next event
    nxt = None
    min_delta = float("inf")
    for ev in all_events():
        w = window_override_min if window_override_min is not None else ev.window_min
        start = ev.ts_utc - w * 60
        if start > t and (start - t) < min_delta:
            min_delta = start - t
            nxt = ev
    return False, nxt, min_delta if min_delta != float("inf") else float("inf")


def status(now: float | None = None) -> dict:
    """JSON-serializable status for GET /blackout/status."""
    t = now if now is not None else time.time()
    blackout, ev, delta = is_blackout(t)
    nxt_events = [e.to_dict() for e in all_events() if e.ts_utc >= t - 3600][:10]
    out: dict = {
        "now": t,
        "now_iso": datetime.fromtimestamp(t, tz=timezone.utc).isoformat(),
        "blackout": blackout,
        "active_event": ev.to_dict() if (blackout and ev) else None,
        "next_event": ev.to_dict() if (not blackout and ev) else None,
        "seconds_remaining": round(delta, 1) if delta != float("inf") else None,
        "seconds_to_next": round(delta, 1) if (not blackout and delta != float("inf")) else None,
        "upcoming": nxt_events,
        "suppress_plans": blackout,
        "reason": f"Blackout: {ev.name} ({ev.kind})" if (blackout and ev) else "No blackout",
    }
    return out


def should_suppress(now: float | None = None) -> bool:
    """Convenience: True if plans should be suppressed right now."""
    b, _, _ = is_blackout(now)
    return b
