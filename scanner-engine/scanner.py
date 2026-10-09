"""Scanner loop — poll -> features -> rules -> record, forever.

THE MISSING LINK
================
Every layer below this one (adapters, poller, features, rules, recorder) was
built and verified in isolation. Without this loop none of them ever runs
against a clock, the recorder stays empty, and the auditor correctly reports
INSUFFICIENT DATA forever.

WHAT THIS DELIBERATELY DOES NOT DO
==================================
* It does not pick a direction. No rule has been audited, so no rule may name
  a side. Every row it writes carries direction 0 and is therefore excluded
  from the expectancy math. That is correct, not broken — it becomes non-zero
  only when `rules.json` marks a rule audited with a side.

* It does not decide a verdict is "good". It records what the rules said and
  what was UNEVALUABLE, and lets the auditor decide.

* It does not alert. Delivery is a separate concern so that alert spam can
  never suppress or distort recording.

`horizon_bars` is derived PER PAIR from that pair's bar interval
(`pairs.bar_for`), because a fixed horizon across a 1m and a 5m pair makes
expectancy incomparable.

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/scanner.py --once
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import features as F          # noqa: E402
import pairs as P            # noqa: E402
import recorder as RC        # noqa: E402
import rules_eval as RE      # noqa: E402
from poller import Poller    # noqa: E402

DEFAULT_POLL_S = 15.0
DEFAULT_MAX_AGE_S = 300.0

# Forward horizon defined in MINUTES, then converted to bars per pair.
#
# It must be wall-clock, not bar-count. A fixed bar count across different bar
# sizes is not comparable: 5 bars of 1m is 5 minutes, 5 bars of 5m is 25. That
# is precisely the flaw documented upstream ("forward_bars should become
# per-signal-type; a 12-bar horizon on a 1m bar is 12 minutes and on a 1h bar
# is 12 hours"), and this module must not reproduce it in a subtler form.
HORIZON_MINUTES = 5.0
BAR_MINUTES = {"1m": 1.0, "5m": 5.0}


def horizon_for(pair: str) -> int:
    """Forward horizon in BARS for `pair`, holding wall-clock time constant.

    A 1m pair gets 5 bars (5 min); a 5m pair gets 1 bar (5 min). An unconfigured
    pair falls back to 5 bars rather than raising — a bad pair must not crash
    the loop.
    """
    try:
        bar = P.bar_for(pair)
    except KeyError:
        return 5
    mins = BAR_MINUTES.get(bar, 1.0)
    return max(1, int(round(HORIZON_MINUTES / mins)))


def _min_venues_for(pair: str) -> int:
    """Evidence floor for `pair`. An unconfigured pair gets 3 (the rules.json
    default) instead of raising: the registry raising is correct for direct
    lookup, but a running loop must degrade rather than die."""
    try:
        return P.min_venues_for(pair)
    except KeyError:
        return 3


class Scanner:
    def __init__(self, pairs: list[str] | None = None, db_path: str = ":memory:",
                 rules: dict | None = None, poll_interval_s: float = DEFAULT_POLL_S,
                 max_age_s: float = DEFAULT_MAX_AGE_S, max_cycles: int | None = None):
        self.pairs = list(pairs) if pairs else P.all_pairs()
        self.rules = rules or RE.load_rules(HERE / "rules.json")
        self.poll_interval_s = poll_interval_s
        self.max_cycles = max_cycles
        self.cycles = 0
        self.stopped_cleanly = False

        self.poller = Poller(max_age_s=max_age_s)
        self.db = RC.open_db(db_path) if db_path != ":memory:" else RC.open_db(":memory:")

        self.verdicts: dict[str, RE.Verdict] = {}
        self.features: dict[str, F.CrossVenueFeatures] = {}

        for pair in self.pairs:
            self.poller.set_min_venues(pair, _min_venues_for(pair))

        self._fetchers: dict[str, callable] = {}

    # ── wiring ─────────────────────────────────────────────────────────────

    def inject_fetchers(self, fetchers: dict) -> None:
        """Install venue callables: venue -> fn(pair) -> (book, tape|None)."""
        self._fetchers = fetchers

    def default_fetchers(self) -> dict:
        """Wire the real venue adapters. Import is deferred so the unit tests
        can run without touching the network."""
        import venues.binance as bn
        import venues.okx as okx
        import venues.kraken as kr
        import venues.bybit as bb
        from flow import window_cvd, DEFAULT_WINDOW_S

        def wrap(fetch_book, fetch_trades=None):
            def fn(pair):
                book = fetch_book(pair)
                tape = None
                if fetch_trades is not None:
                    try:
                        w = window_cvd(fetch_trades(pair), window_s=DEFAULT_WINDOW_S)
                        if w is not None:
                            tape = {"delta_ratio": w.delta_ratio,
                                    "coverage_s": w.coverage_s,
                                    "n_trades": w.n_trades}
                    except Exception:                      # noqa: BLE001
                        tape = None
                return book, tape
            return fn

        return {
            "binance": wrap(bn.fetch_depth, bn.fetch_trades),
            "okx": wrap(okx.fetch_depth, okx.fetch_trades),
            "kraken": wrap(kr.fetch_depth, kr.fetch_trades),
            "bybit": wrap(bb.fetch_depth, bb.fetch_trades),
        }

    # ── one cycle ──────────────────────────────────────────────────────────

    def run_cycle(self) -> None:
        self.cycles += 1
        self.poller.begin_cycle()

        fetchers = self._fetchers or self.default_fetchers()

        # venue-major: a permanent failure on one pair must not remove that
        # venue's evidence for the others in the same cycle
        for venue in fetchers:
            for pair in self.pairs:
                try:
                    listed = P.venues_for(pair)
                except KeyError:
                    listed = ()          # unknown pair: nothing to poll, not a crash
                if venue not in listed:
                    continue              # not listed there; a fact, not a fault
                self.poller.run_venue(
                    venue, pair,
                    lambda v=venue, p=pair: self._fetch_one(fetchers[v], p),
                )

        for pair in self.pairs:
            snaps = self.poller.live_pairs(pair)
            feat = F.compute({v: {"book": s.get("book"), "tape": s.get("tape")}
                              for v, s in snaps.items()})
            self.features[pair] = feat

            v = RE.evaluate(feat, pair, _min_venues_for(pair), self.rules)
            self.verdicts[pair] = v

            met = [rid for rid, r in v.rules.items() if r.state == RE.MET]
            unev = [rid for rid, r in v.rules.items() if r.state == RE.UNEVALUABLE]

            RC.record_verdict(
                self.db, pair=pair, ts=v.ts,
                # No rule may name a side until audited -> always 0 today.
                direction=_direction(v),
                forward_return=None,
                horizon_bars=horizon_for(pair),
                fill_price=_mid(snaps),
                venues_live=v.n_venues,
                rules_met=met, rules_unevaluable=unev,
                score=v.score,
            )

    def _fetch_one(self, fn, pair: str):
        book, tape = fn(pair)
        return {"pair": pair, "book": book, "tape": tape, "ts": time.time()}

    # ── surfaces ───────────────────────────────────────────────────────────

    def health(self) -> dict:
        h = self.poller.health()
        h.update({
            "cycles": self.cycles,
            "pairs": self.pairs,
            "poll_interval_s": self.poll_interval_s,
            "stopped_cleanly": self.stopped_cleanly,
            "coverage": _coverage_for(self.pairs),
            "recorder": RC.counts(self.db),
            # Compare on PREFIX, not equality: rule 1's audit_status reads
                        # "UNVALIDATED - no recorded outcomes yet. Phase 4's auditor
                        # decides whether it survives." An exact `!= "UNVALIDATED"` check
                        # classified that descriptive suffix as AUDITED and would have let
                        # an unmeasured rule contribute a direction. A status field is not
                        # a place for prose.
                        "audited_rules": [
                            rid for rid, s in self.rules["rules"].items()
                            if not str(s.get("audit_status", "UNVALIDATED"))
                                 .upper().startswith("UNVALIDATED")
                        ],
        })
        return h

    def rank(self) -> list[dict]:
        """Conviction ranking. Pairs without adequate evidence are EXCLUDED,
        not ranked at the bottom — 'we cannot judge this' is not 'weakest'."""
        out = []
        for pair, v in self.verdicts.items():
            if not v.evidence_ok:
                continue
            out.append({
                "pair": pair,
                "score": v.score,
                "direction": v.direction,
                "n_venues": v.n_venues,
                "met": [r for r, x in v.rules.items() if x.state == RE.MET],
                "unevaluable": [r for r, x in v.rules.items()
                                if x.state == RE.UNEVALUABLE],
            })
        out.sort(key=lambda r: (-r["score"], r["pair"]))
        return out

    def verdict_json(self, pair: str | None = None) -> dict:
        if pair:
            v = self.verdicts.get(pair)
            return v.to_dict() if v else {"error": f"no verdict for {pair}"}
        return {"rank": self.rank(),
                "verdicts": {p: v.to_dict() for p, v in self.verdicts.items()}}

    # ── the loop ───────────────────────────────────────────────────────────

    def run_forever(self, poll_interval_s: float | None = None) -> None:
        interval = self.poll_interval_s if poll_interval_s is None else poll_interval_s
        try:
            while True:
                self.run_cycle()
                if self.max_cycles is not None and self.cycles >= self.max_cycles:
                    break
                if interval:
                    time.sleep(interval)
        except KeyboardInterrupt:
            pass
        except Exception as e:                              # noqa: BLE001
            # A non-zero exit tells the supervisor this was a CRASH and it may
            # relaunch. A clean stop must look clean.
            print(f"scanner crashed: {type(e).__name__}: {e}", file=sys.stderr)
            raise
        self.stopped_cleanly = True


def _coverage_for(pairs: list[str]) -> dict:
    """Registry coverage for the configured pairs, plus any pair that is not
    in the registry (reported honestly rather than omitted)."""
    cov = P.coverage()
    for pair in pairs:
        cov.setdefault(pair, {"venues": 0, "min_venues": 3,
                              "missing": ["not_in_registry"], "bar": "unknown"})
    return cov


def _direction(v: RE.Verdict) -> int:
    """direction from the verdict. Neutral -> 0, which the auditor excludes.
    Only an audited rule can ever move this off 0."""
    if v.direction == "bullish":
        return 1
    if v.direction == "bearish":
        return -1
    return 0


def _mid(snaps: dict) -> float | None:
    """Depth-weighted cross-venue mid, for the recorded fill price."""
    mids, weights = [], []
    for s in snaps.values():
        b = s.get("book")
        if b is None:
            continue
        mids.append(b.mid)
        weights.append(b.depth_within(5)[0] + b.depth_within(5)[1])
    if not mids:
        return None
    tw = sum(weights) or float(len(mids))
    return sum(m * w for m, w in zip(mids, weights)) / tw


def _serve(sc: Scanner, port: int) -> None:
    """Minimal stdlib HTTP surface: /health, /rank, /verdict.

    THREADED, not single-threaded, and this is not a style choice. A cycle takes
    ~22s live (32 venue polls). A single-threaded HTTPServer cannot answer
    while the loop is mid-cycle, so /health returned an empty response (curl
    code 000) for the whole duration of every cycle — measured: /rank answered
    fine between cycles while /health never did. ThreadingHTTPServer lets the
    health surface stay up during a cycle; /health reads poller state without
    mutating it.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    class H(BaseHTTPRequestHandler):
        def do_GET(self):                                  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/health":
                body = sc.health()
            elif u.path == "/rank":
                body = {"rank": sc.rank()}
            elif u.path == "/verdict":
                q = parse_qs(u.query)
                body = sc.verdict_json(q.get("pair", [None])[0])
            else:
                self.send_error(404, "unknown endpoint")
                return
            raw = json.dumps(body, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_a):
            pass

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="scanner-engine: multi-exchange scanner")
    ap.add_argument("--pairs", default=",".join(P.all_pairs()))
    ap.add_argument("--db", default=str(HERE / "data" / "scanner.db"))
    ap.add_argument("--interval", type=float, default=DEFAULT_POLL_S)
    ap.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_S)
    ap.add_argument("--port", type=int, default=8090, help="0 disables the HTTP surface")
    ap.add_argument("--cycles", type=int, default=None, help="bound the loop (testing)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = ap.parse_args()

    sc = Scanner(
        pairs=[p.strip() for p in args.pairs.split(",") if p.strip()],
        db_path=args.db,
        rules=RE.load_rules(HERE / "rules.json"),
        poll_interval_s=args.interval,
        max_age_s=args.max_age,
        max_cycles=args.cycles,
    )

    if args.once:
        sc.run_cycle()
        print(json.dumps(sc.health(), indent=2, default=str))
        raise SystemExit(0)

    if args.port:
        import threading
        threading.Thread(target=_serve, args=(sc, args.port), daemon=True).start()
        print(f"scanner-engine listening on :{args.port}")

    sc.run_forever()