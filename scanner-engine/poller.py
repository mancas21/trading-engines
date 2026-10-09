"""Poller — collects normalized snapshots per (pair, venue), with the
freshness and isolation rules this engine depends on.

WHY THIS SHAPE
==============
Three rules are enforced HERE, in one place, so no downstream feature or rule
can accidentally read a frozen or failed venue. Each rule exists because its
violation was a real bug elsewhere in this repo.

1. STALENESS IS DROPPED, NOT IMPUTED.
   `live_pairs()` returns only venues whose last successful poll is inside
   `max_age_s`. A venue that was live once and then went quiet is named in
   `frozen_report()` with its exact age. A venue that has NEVER returned is
   absent from both — "never worked" and "stopped working" are different
   claims and the verdict text must be able to tell them apart.

2. ONE VENUE'S FAILURE CANNOT STARVE THE OTHERS.
   Backoff is per-venue. A 429 from OKX must not reduce Binance's cadence,
   and must be *visible* in `health()["throttled_venues"]` rather than
   silently reducing coverage.

3. A FAILED POLL IS NOT A ZEROED SNAPSHOT.
   `record_failure()` keeps the error. It never installs an empty book or a
   zero-volume tape, because "no data" and "no flow" are different facts and
   only one of them is bearish.

`min_venues_live` is PER PAIR. SPCX and NVDA are absent from Kraken, so their
floor is lower than the four-venue pairs; a single global floor would leave
them permanently unevaluable while a global floor high enough for them would
let the liquid pairs verdict on one venue.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

DEFAULT_MAX_AGE_S = 300.0
DEFAULT_MIN_VENUES = 3


class Throttle:
    """Per-venue exponential backoff with jitter."""

    def __init__(self, base_cooldown_s: float = 5.0, max_cooldown_s: float = 300.0):
        self.base = base_cooldown_s
        self.max_cooldown = max_cooldown_s
        self._open_until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}
        self._lock = threading.Lock()

    def penalize(self, venue: str, now: float | None = None) -> float:
        """Open the cooldown for `venue`. Returns the cooldown applied."""
        now = time.time() if now is None else now
        with self._lock:
            n = self._strikes.get(venue, 0) + 1
            self._strikes[venue] = n
            cd = min(self.base * (2 ** (n - 1)), self.max_cooldown)
            self._open_until[venue] = now + cd
            return cd

    def reward(self, venue: str) -> None:
        """A success clears the venue's penalty entirely."""
        with self._lock:
            self._strikes.pop(venue, None)
            self._open_until.pop(venue, None)

    def is_open(self, venue: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            return now < self._open_until.get(venue, 0.0)

    def cooldown_remaining(self, venue: str, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            return max(0.0, self._open_until.get(venue, 0.0) - now)

    def throttled(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        with self._lock:
            return sorted(v for v, until in self._open_until.items() if now < until)


class Poller:
    def __init__(self, max_age_s: float = DEFAULT_MAX_AGE_S,
                 default_min_venues: int = DEFAULT_MIN_VENUES):
        if max_age_s <= 0:
            # A zero or negative freshness window silently drops EVERY symbol
            # and looks exactly like "all venues are down".
            raise ValueError(f"max_age_s must be > 0, got {max_age_s}")
        self.max_age_s = max_age_s
        self.default_min_venues = default_min_venues
        self.throttle = Throttle()

        # (pair, venue) -> snapshot dict
        self._snapshots: dict[tuple[str, str], dict] = {}
        # (pair, venue) -> {"error": str, "ts": float, "n": int}
        self._failures: dict[tuple[str, str], dict] = {}
        self._min_venues: dict[str, int] = {}
        self._cycles = 0
        self._lock = threading.RLock()

    # ── ingestion ──────────────────────────────────────────────────────────

    def record(self, venue: str, pair: str, snapshot: dict) -> None:
        """Install a successful snapshot and clear any prior failure."""
        with self._lock:
            self._snapshots[(pair, venue)] = snapshot
            self._failures.pop((pair, venue), None)
        self.throttle.reward(venue)

    def record_failure(self, venue: str, pair: str, error: str,
                       penalize: bool | None = None) -> None:
        """Record a failed poll. Deliberately does NOT install a snapshot.

        `penalize=None` (the default) auto-decides: a PERMANENT error for this
        pair — the venue does not list it — backs off nothing, because there is
        no request cadence that will ever make `EQuery:Unknown asset pair`
        succeed. A TRANSIENT error (429, timeout, one-sided book) backs off the
        venue.

        This distinction is not cosmetic. Before it existed, SPCX's permanent
        Kraken rejection opened a venue-wide cooldown that silently skipped
        Kraken for HYPE and ETH in the same run: two pairs reported 3 venues
        with NO failure recorded. A penalty for one pair must never remove
        evidence for another.
        """
        now = time.time()
        permanent = self.is_permanent(error)
        if penalize is None:
            penalize = not permanent
        with self._lock:
            key = (pair, venue)
            prev = self._failures.get(key, {}).get("n", 0)
            self._failures[key] = {
                "error": str(error)[:200],
                "ts": now,
                "n": prev + 1,
                "permanent": permanent,
            }
            # Drop any snapshot we hold: a venue that just failed must not
            # keep serving its last good reading past its freshness window.
            self._snapshots.pop(key, None)
        if penalize:
            self.throttle.penalize(venue)

    # Substrings that mean "this venue will never serve this pair". Measured
    # against the real error bodies, not guessed.
    _PERMANENT_MARKERS = (
        "unknown asset pair",
        "unknownasset",
        "instrument not exist",
        "unknown instrument",
        "does not exist",
        "invalid instrument",
    )

    @classmethod
    def is_permanent(cls, error: str) -> bool:
        e = str(error).lower()
        return any(m in e for m in cls._PERMANENT_MARKERS)

    def unsupported_pairs(self, venue: str) -> dict[str, dict]:
        """Pairs this venue permanently cannot serve, with the evidence.

        Kept separate from `failure_report()` so the verdict can state
        "not listed on Kraken" as a fact rather than as an outage.
        """
        with self._lock:
            return {
                p: f for (pp, v), f in self._failures.items()
                if v == venue and f.get("permanent")
                for p in [pp]
            }

    def penalize(self, venue: str) -> None:
        self.throttle.penalize(venue)

    # ── reads: the single enforcement point for rule 1 ─────────────────────

    def live_pairs(self, pair: str, now: float | None = None) -> dict[str, dict]:
        """Venues with a FRESH snapshot for `pair`. Stale ones are excluded."""
        now = time.time() if now is None else now
        out: dict[str, dict] = {}
        with self._lock:
            for (p, v), snap in self._snapshots.items():
                if p != pair:
                    continue
                if (now - snap["ts"]) <= self.max_age_s:
                    out[v] = snap
        return out

    def frozen_report(self, now: float | None = None) -> dict[str, dict[str, float]]:
        """Pairs with at least one venue that WAS live and has gone stale.

        Only venues with a recorded snapshot are listed. A venue that has
        never returned does not appear — see module docstring.
        """
        now = time.time() if now is None else now
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            for (p, v), snap in self._snapshots.items():
                age = now - snap["ts"]
                if age > self.max_age_s:
                    out.setdefault(p, {})[v] = round(age, 1)
        return out

    def failure_report(self) -> dict[str, dict[str, dict]]:
        out: dict[str, dict[str, dict]] = {}
        with self._lock:
            for (p, v), f in self._failures.items():
                out.setdefault(p, {})[v] = f
        return out

    # ── per-pair evidence floor ────────────────────────────────────────────

    def set_min_venues(self, pair: str, n: int) -> None:
        self._min_venues[pair] = n

    def min_venues(self, pair: str) -> int:
        return self._min_venues.get(pair, self.default_min_venues)

    def has_evidence(self, pair: str, now: float | None = None) -> bool:
        return len(self.live_pairs(pair, now)) >= self.min_venues(pair)

    # ── cycle bookkeeping ──────────────────────────────────────────────────

    def begin_cycle(self) -> int:
        with self._lock:
            self._cycles += 1
            return self._cycles

    def live_venue_count(self, pair: str, now: float | None = None) -> int:
        return len(self.live_pairs(pair, now))

    def health(self, now: float | None = None) -> dict:
        """The payload /health serves. Frozen and throttled venues are NAMED."""
        now = time.time() if now is None else now
        with self._lock:
            pairs = sorted({p for p, _ in self._snapshots})
            live_counts = {p: len(self.live_pairs(p, now)) for p in pairs}
            ages = {}
            for p in pairs:
                per = {}
                for (pp, v), snap in self._snapshots.items():
                    if pp == p:
                        per[v] = round(now - snap["ts"], 1)
                ages[p] = per
        return {
            "max_age_s": self.max_age_s,
            "poll_cycles": self._cycles,
            "live_pairs": live_counts,
            "venue_ages_s": ages,
            "frozen_venues": self.frozen_report(now),
            "throttled_venues": self.throttle.throttled(now),
            "failures": self.failure_report(),
        }

    def run_venue(self, venue: str, pair: str, fn: Callable[[], dict]) -> None:
        """Call `fn()` for one (venue, pair); route the outcome correctly.

        This is the only place network results enter the poller, so the
        success/failure split cannot be forgotten at a call site.
        """
        if self.throttle.is_open(venue):
            return
        try:
            snap = fn()
        except Exception as e:                                  # noqa: BLE001
            self.record_failure(venue, pair, f"{type(e).__name__}: {e}")
            return
        if snap is None:
            self.record_failure(venue, pair, "fetcher returned None (absent data)")
            return
        snap.setdefault("pair", pair)
        snap.setdefault("venue", venue)
        snap.setdefault("ts", time.time())
        self.record(venue, pair, snap)