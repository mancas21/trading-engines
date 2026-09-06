"""
watchdog.py — Data-quality watchdog & heartbeat.

Monitors:
  - stale live indicators (file mtime vs now)
  - CVD gaps / fast_skips / race_ticks / segments
  - WS reconnects / DOM staleness
  - tick history freshness
  - calls checker liveness

Exposes a dict for GET /health/detailed. No imports of bridge_server at module load;
bridge_server wires it by calling watchdog.collect(...).
"""
from __future__ import annotations

import os
import time
import json as _json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# In-memory counters (updated by bridge_server)
# ---------------------------------------------------------------------------
@dataclass
class WatchdogState:
    ws_reconnects: dict[str, int] = field(default_factory=dict)  # symbol -> count
    ws_last_msg_ts: dict[str, float] = field(default_factory=dict)
    dom_updates: int = 0
    last_cvd_save_ts: float = 0.0
    last_calls_check_ts: float = 0.0
    last_indicator_ts: float = 0.0  # LiveExporter timestamp field
    last_indicator_file_mtime: float = 0.0
    indicator_stale_threshold_s: float = 15.0
    dom_stale_threshold_s: float = 20.0
    started_at: float = field(default_factory=time.time)

    def record_ws_reconnect(self, symbol: str) -> None:
        self.ws_reconnects[symbol] = self.ws_reconnects.get(symbol, 0) + 1

    def record_ws_msg(self, symbol: str) -> None:
        self.ws_last_msg_ts[symbol] = time.time()

    def to_dict(self) -> dict:
        return {
            "ws_reconnects": dict(self.ws_reconnects),
            "ws_last_msg_ts": dict(self.ws_last_msg_ts),
            "dom_updates": self.dom_updates,
            "last_cvd_save_ts": self.last_cvd_save_ts,
            "last_calls_check_ts": self.last_calls_check_ts,
            "last_indicator_ts": self.last_indicator_ts,
            "last_indicator_file_mtime": self.last_indicator_file_mtime,
            "uptime_s": round(time.time() - self.started_at, 1),
        }


STATE = WatchdogState()


def collect(
    live_file: str | None = None,
    dom_state: dict | None = None,
    cvd_state: dict | None = None,
    calls_open: int | None = None,
    tick_history_path: str | None = None,
) -> dict:
    """
    Build GET /health/detailed payload.

    Pass live snapshots from bridge_server; missing args are treated as unknown.
    """
    now = time.time()
    out: dict = {
        "status": "ok",
        "ts": now,
        "uptime_s": round(now - STATE.started_at, 1),
        "checks": {},
        "warnings": [],
        "critical": [],
    }

    # ---- Live indicators freshness ----
    if live_file:
        try:
            mtime = os.path.getmtime(live_file) if os.path.exists(live_file) else 0
            age = now - mtime if mtime else 9999
            stale = age > STATE.indicator_stale_threshold_s
            out["checks"]["live_indicators"] = {
                "file": live_file,
                "mtime_age_s": round(age, 1),
                "stale": stale,
                "threshold_s": STATE.indicator_stale_threshold_s,
            }
            if stale:
                out["warnings"].append(f"live_indicators stale: {age:.1f}s > {STATE.indicator_stale_threshold_s}s")
                out["status"] = "degraded"
            # Also compare LiveExporter internal timestamp if available
            if STATE.last_indicator_ts:
                # LiveExporter timestamp is milliseconds since epoch (JS) or seconds
                ts = STATE.last_indicator_ts
                # Heuristic: if > 1e12 it's ms
                ts_s = ts / 1000.0 if ts > 1e12 else ts
                lag = now - ts_s
                out["checks"]["live_indicators"]["exporter_lag_s"] = round(lag, 1)
                if lag > STATE.indicator_stale_threshold_s:
                    out["warnings"].append(f"exporter lag {lag:.1f}s")
        except Exception as e:
            out["checks"]["live_indicators"] = {"error": str(e)}

    # ---- Tick history ----
    if tick_history_path and os.path.exists(tick_history_path):
        try:
            mtime = os.path.getmtime(tick_history_path)
            age = now - mtime
            sz = os.path.getsize(tick_history_path)
            out["checks"]["tick_history"] = {
                "path": tick_history_path,
                "age_s": round(age, 1),
                "size_bytes": sz,
                "stale": age > 60,
            }
            if age > 60:
                out["warnings"].append(f"tick_history stale {age:.1f}s")
                if out["status"] == "ok":
                    out["status"] = "degraded"
        except Exception as e:
            out["checks"]["tick_history"] = {"error": str(e)}

    # ---- DOM staleness per symbol ----
    if dom_state is not None:
        dom_issues = []
        for sym, st in dom_state.items():
            ts = st.get("ts", 0) if isinstance(st, dict) else 0
            age = now - ts if ts else 9999
            stale = age > STATE.dom_stale_threshold_s
            if stale:
                dom_issues.append(f"{sym}:{age:.0f}s")
        out["checks"]["dom"] = {
            "symbols": len(dom_state),
            "stale_symbols": dom_issues,
            "threshold_s": STATE.dom_stale_threshold_s,
            "ws_reconnects": dict(STATE.ws_reconnects),
        }
        if dom_issues:
            out["warnings"].append(f"DOM stale: {', '.join(dom_issues)}")
            out["status"] = "degraded"
        out["checks"]["dom"]["last_msg_age"] = {
            sym: round(now - ts, 1) for sym, ts in STATE.ws_last_msg_ts.items()
        }

    # ---- CVD gaps ----
    if cvd_state is not None:
        gaps = []
        for sym, st in cvd_state.items():
            g = st.get("gaps", 0) if isinstance(st, dict) else 0
            fs = st.get("fast_skips", 0) if isinstance(st, dict) else 0
            rt = st.get("race_ticks", 0) if isinstance(st, dict) else 0
            segs = st.get("segments", 0) if isinstance(st, dict) else 0
            if g or fs or rt:
                gaps.append({"symbol": sym, "gaps": g, "fast_skips": fs, "race_ticks": rt, "segments": segs})
        out["checks"]["cvd"] = {
            "symbols": len(cvd_state),
            "issues": gaps,
            "last_save_age_s": round(now - STATE.last_cvd_save_ts, 1) if STATE.last_cvd_save_ts else None,
        }
        if gaps:
            out["warnings"].append(f"CVD issues: {gaps}")

    # ---- Calls checker ----
    if STATE.last_calls_check_ts:
        age = now - STATE.last_calls_check_ts
        out["checks"]["calls_checker"] = {
            "age_s": round(age, 1),
            "stale": age > 30,
            "open_calls": calls_open,
        }
        if age > 30:
            out["critical"].append(f"calls_checker stale {age:.1f}s")
            out["status"] = "critical"
    else:
        out["checks"]["calls_checker"] = {"age_s": None, "note": "no check yet"}

    # Overall
    if out["critical"]:
        out["status"] = "critical"
    elif out["warnings"] and out["status"] == "ok":
        out["status"] = "degraded"

    out["watchdog"] = STATE.to_dict()
    return out


def is_healthy() -> bool:
    """Quick boolean for supervisor scripts."""
    d = collect()
    return d.get("status") != "critical"
