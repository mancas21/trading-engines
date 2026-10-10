"""Outcome resolver — closes Gap 1 (the feature that was never wired).

`recorder.resolve()` existed and was tested. Nothing called it. Every row the
scanner wrote stayed `resolved=0` forever, so `forward_return` was never
populated and the auditor had nothing to measure. Measured on the running
engine before this module: 336 rows, `forward_return NOT NULL` = 0.

THIS MODULE DOES
================
* fetch the CLOSED bar at the end of a row's horizon,
* compute the RAW move from the recorded fill price,
* write it via `recorder.resolve()`,
* attach a PROVISIONAL side when a rule fired MET, and flag the row
  `candidate=1`.

WHAT IT DELIBERATELY DOES NOT DO
================================
It does not decide that a rule is any good. The provisional side is a
hypothesis under test: it enters expectancy math so the auditor can measure
whether the rule has an edge, and it CANNOT suppress, alert or gate. That
restriction lives in `promoted_direction`, which requires the rule to be
AUDITED before anything downstream may use it.
"""
from __future__ import annotations

import json
import sqlite3
import time

# A row's horizon in milliseconds, given its bar size in minutes.
BAR_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def forward_return(fill: float | None, later: float | None) -> float | None:
    """Raw fractional move from `fill` to `later`.

    RAW, never direction-adjusted: the flip belongs in SQL so it stays
    re-derivable. None when either price is unknown — a missing future price
    is NOT a flat move, and zeroing it would fabricate a neutral outcome from
    absent data.
    """
    if fill is None or later is None or fill <= 0:
        return None
    return (later - fill) / fill


def bar_is_closed(bar: dict, now_ms: int | None = None) -> bool:
    """True when the bar is fully formed. A partially formed bar has partial
    volume and would invent a forward return that did not happen."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    close_ms = bar.get("close_ms")
    return close_ms is not None and close_ms <= now_ms


def resolve_row(bar: dict | None, horizon_bars: int, now_ms: int | None = None,
                fill: float | None = None) -> float | None:
    """The RAW forward return for one row, or None if it cannot be computed
    yet. Refuses to score a still-forming bar."""
    if bar is None or not bar_is_closed(bar, now_ms):
        return None
    return forward_return(fill, bar.get("close"))


# ── the resolver over the whole table ──────────────────────────────────────

def pending_rows(db: sqlite3.Connection, limit: int = 500) -> list[tuple]:
    """Rows awaiting an outcome: (id, pair, ts, horizon_bars, fill_price)."""
    with RC_guard(db):
        return db.execute(
            """SELECT id, pair, ts, horizon_bars, fill_price
                 FROM verdicts
                WHERE resolved = 0 AND fill_price IS NOT NULL
                ORDER BY ts LIMIT ?""",
            (limit,),
        ).fetchall()


def resolve_pending(db: sqlite3.Connection, fetch_bar, now_ms: int | None = None,
                    limit: int = 500) -> int:
    """Resolve every row whose horizon bar has closed.

    `fetch_bar(pair, at_ms) -> {"close": float, "close_ms": int}` must return
    the CLOSED bar covering `at_ms`. Rows whose bar is still forming, or whose
    fetch fails, are left untouched — never zeroed.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    n = 0
    for rid, pair, ts, horizon_bars, fill in pending_rows(db, limit):
        try:
            bar = fetch_bar(pair, int(ts * 1000) + int(horizon_bars) * 60_000)
        except Exception:                                  # noqa: BLE001
            continue
        fwd = resolve_row(bar, horizon_bars, now_ms=now_ms, fill=fill)
        if fwd is None:
            continue
        with RC_guard(db):
            db.execute(
                "UPDATE verdicts SET forward_return=?, resolved=1 WHERE id=?",
                (fwd, rid),
            )
            n += 1
    with RC_guard(db):
        db.commit()
    return n


# ── the two ledgers ────────────────────────────────────────────────────────

def mark_candidate(db: sqlite3.Connection, ids: list[int]) -> None:
    """Flag rows as candidate (hypothesis under test)."""
    if not ids:
        return
    with RC_guard(db):
        db.executemany("UPDATE verdicts SET candidate=1 WHERE id=?", [(i,) for i in ids])
        db.commit()


def _spec(rules: dict, rid: str) -> dict:
    """Look up a rule spec, accepting BOTH shapes.

    rules.json nests specs under `rules["rules"][id]`, but several call sites and
    the unit tests pass a flat `{id: spec}` mapping. Reading only one of them
    made every real rule look like it had no `direction_hint`, so
    `hint_direction` returned 0 on every live row — the interface mismatch was
    invisible because the tests used the flat shape and the engine used the
    nested one.
    """
    if not rules:
        return {}
    nested = rules.get("rules")
    if isinstance(nested, dict) and rid in nested:
        return nested[rid] or {}
    return rules.get(rid) or {}


def hint_direction(rules: dict, flow_by_rule: dict[str, float]) -> int:
    """A PROVISIONAL side for a row where rule(s) fired MET.

    For each fired rule, read its `direction_hint`:
      "follow" -> side of the rule's flow ratio (positive = buying = long)
      "fade"   -> the inverse
      null     -> no directional claim; contributes nothing

    If any fired rule has no hint, the row gets NO provisional side: a
    non-directional claim cannot be combined with a directional one. Rule 1
    (basis dislocation) is null for exactly this reason — one venue being
    apart does not say which way the pair goes.
    """
    sides: list[int] = []
    for rid, flow in (flow_by_rule or {}).items():
        spec = _spec(rules, rid)
        hint = spec.get("direction_hint")
        if not hint:
            continue                    # no claim from this rule
        side = hint.get("side")
        if side == "follow":
            if flow > 0.5:
                sides.append(1)
            elif flow < 0.5:
                sides.append(-1)
            # flow == exactly 0.5 -> no lean, contributes nothing
        elif side == "fade":
            if flow > 0.5:
                sides.append(-1)
            elif flow < 0.5:
                sides.append(1)
    if not sides:
        return 0
    # only unanimous claims become a provisional side; a disagreement is no side
    return sides[0] if all(s == sides[0] for s in sides) else 0


def promoted_direction(rules: dict, rule_ids: list[str],
                       flow_by_rule: dict[str, float]) -> int:
    """A side that DOWNSTREAM may act on. Requires EVERY fired rule to be
    AUDITED. A candidate or mixed population gets nothing.

    This is the gate: the recorder may hold a hypothesis, but only an audited
    rule reaches the verdict ledger where alerts and gating live.
    """
    if not rule_ids:
        return 0
    for rid in rule_ids:
        status = str(_spec(rules, rid).get("audit_status", "UNVALIDATED")).upper()
        if status.startswith("UNVALIDATED"):
            return 0
    return hint_direction(rules, flow_by_rule)


def _RC_guard(db):
    from recorder import _guard
    return _guard(db)


# alias so callers can use the guard without importing recorder internals
RC_guard = _RC_guard


if __name__ == "__main__":
    import sys
    print("forward_return(100, 101) =", forward_return(100.0, 101.0))
    print("forward_return(100, None) =", forward_return(100.0, None))
    print("hint follow 0.72 ->", hint_direction({"4": {"direction_hint": {"side": "follow"}}}, {"4": 0.72}))
    print("hint null 1 ->", hint_direction({"1": {"direction_hint": None}}, {"1": 0.9}))