"""Scanner loop — poll -> features -> rules -> record, forever.

THE MISSING LINK
================
Every layer below this one (adapters, poller, features, rules, recorder) was
built and verified in isolation. Without this loop none of them ever runs
against a clock, the recorder stays empty, and the auditor correctly reports
INSUFFICIENT DATA forever.

WHAT THIS DELIBERATELY DOES NOT DO
==================================
* It does not pick a direction for the VERDICT ledger. A rule that has not
  been audited may not name a side that downstream acts on. What it DOES do is
  write a PROVISIONAL side on the CANDIDATE ledger (`direction` from the
  firing rule's `direction_hint`, `candidate=1`) so the auditor can measure
  whether that rule has an edge. Candidate rows enter expectancy math; they
  cannot suppress, alert or gate. The verdict ledger stays direction=0 until
  `rules.json` marks a rule audited (`outcomes.promoted_direction`).

* It does not decide a verdict is "good". It records what the rules said and
  what was UNEVALUABLE, and lets the auditor decide.

* It does not alert. Delivery is a separate concern so that alert spam can
  never suppress or distort recording.

`horizon_bars` is derived PER PAIR from that pair's bar interval
(`pairs.bar_for`), because a fixed horizon across a 1m and a 5m pair makes
expectancy incomparable.

Run:  python scanner-engine/scanner.py --once
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
import outcomes as OUT       # noqa: E402
import promotion as PROM     # noqa: E402  (the retirement half of the loop)
import recorder as RC        # noqa: E402
import rules_eval as RE      # noqa: E402
import backtest_link as BTL  # noqa: E402  (capa de validacion: backtest-system)
from poller import Poller    # noqa: E402

DEFAULT_POLL_S = 15.0
DEFAULT_MAX_AGE_S = 300.0

# Venue used for a pair's fill and exit when the registry has no opinion.
DEFAULT_REF_VENUE = "binance"

# Forward horizon defined in MINUTES, then converted to bars per pair.
#
# It must be wall-clock, not bar-count. A fixed bar count across different bar
# sizes is not comparable: 5 bars of 1m is 5 minutes, 5 bars of 5m is 25. That
# is precisely the flaw documented upstream ("forward_bars should become
# per-signal-type; a 12-bar horizon on a 1m bar is 12 minutes and on a 1h bar
# is 12 hours"), and this module must not reproduce it in a subtler form.
HORIZON_MINUTES = 5.0
# Derived from `outcomes.BAR_MS` so the two tables cannot drift: BAR_MS is what
# the resolver multiplies horizon_bars by, so this map must cover the same bar
# sizes. A literal dict here went stale the moment a bar size existed in one
# and not the other — a 15m pair would have silently gotten horizon 5 bars of
# 15m (75 min) while the resolver measured 75 min but the intent was 5.
BAR_MINUTES = {k: v / 60_000.0 for k, v in OUT.BAR_MS.items()}


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


def _ref_venue(pair: str) -> str:
    """The venue whose price this pair is measured on — fill AND exit.

    Falls back to Binance for an unconfigured pair, matching the registry
    default. A loop that crashes on an unknown pair loses the cycle; a loop
    that names a default keeps running and the pair stays visible as
    unregistered.
    """
    try:
        return P.ref_venue_for(pair)
    except KeyError:
        return DEFAULT_REF_VENUE


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
        self.rows_resolved = 0
        self.rows_unscorable = 0
        self.resolver_errors = 0
        self.last_resolver_error = ""

        # Cross-cycle memory for rules 1-3. Without it three of the four rules
        # could never reach MET — conditions about persistence and about a
        # pair's own trailing baseline cannot be asked from one snapshot.
        self.history = RE.RuleHistory()
        # the tape-quality floors rules.json actually implies, so a declared
        # threshold is binding rather than decorative
        self.coverage_floor = RE.coverage_floor(self.rules)
        self.trade_floor = RE.trade_floor(self.rules)
        # rules the auditor measured into DROP: they keep firing as
        # observations (rules_met stays truthful) but stop producing
        # candidates, so they leave the expectancy math and the Bonferroni
        # count. Computed once at startup; a retirement written to rules.json
        # takes effect on the next scanner restart, like a promotion.
        self._retired = {rid for rid, spec in
                         (self.rules.get("rules") or {}).items()
                         if PROM.is_retired(spec)}

        for pair in self.pairs:
            self.poller.set_min_venues(pair, _min_venues_for(pair))

        self._fetchers: dict[str, callable] = {}
        self._bar_fetch: dict[str, callable] = {}

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

        def wrap(venue, fetch_book, fetch_trades=None):
            def fn(pair):
                book = fetch_book(pair)
                tape = None
                # Bring this venue onto the PAIR's reference scale before
                # anything compares it with another venue. See pairs.py.
                try:
                    pm = P.venue_price_mult(pair, venue)
                    sm = P.venue_size_mult(pair, venue)
                except KeyError:
                    pm = sm = 1.0
                if fetch_trades is not None:
                    try:
                        trades = fetch_trades(pair)
                        if pm != 1.0 or sm != 1.0:
                            trades = _scale_trades(trades, pm, sm)
                        w = window_cvd(trades, window_s=DEFAULT_WINDOW_S)
                        if w is not None:
                            tape = {"delta_ratio": w.delta_ratio,
                                    "coverage_s": w.coverage_s,
                                    "n_trades": w.n_trades,
                                    # the effort-vs-result leg rule 2 needs
                                    "price_delta_bps": w.price_delta_bps}
                    except Exception:                      # noqa: BLE001
                        tape = None
                if pm != 1.0 or sm != 1.0:
                    book = _scale_book(book, pm, sm)
                return book, tape
            return fn

        return {
            "binance": wrap("binance", bn.fetch_depth, bn.fetch_trades),
            "okx": wrap("okx", okx.fetch_depth, okx.fetch_trades),
            "kraken": wrap("kraken", kr.fetch_depth, kr.fetch_trades),
            "bybit": wrap("bybit", bb.fetch_depth, bb.fetch_trades),
        }

    def _install_bar_fetchers(self) -> None:
        """Bar fetcher for outcome resolution, built once.

        Resolution uses ONE venue per pair — the pair's REFERENCE venue, the
        same one the fill was taken from. Four venues' prices for the same
        instant is not four independent observations; it is one observation
        measured four times. Worse, mixing venues is not merely noisy, it is
        WRONG: filling at a cross-venue weighted mid and exiting at one venue's
        close measures that venue's basis rather than the signal. See
        `recorder.py` defect 4 for the measured cost of doing it the other way.
        """
        import venues.binance as bn
        import venues.okx as okx
        import venues.bybit as bb
        import venues.kraken as kr

        self._bar_fetch = {
            "binance": bn.fetch_closed_bar,
            "okx": okx.fetch_closed_bar,
            "bybit": bb.fetch_closed_bar,
            "kraken": kr.fetch_closed_bar,
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
                              for v, s in snaps.items()},
                             min_coverage_s=self.coverage_floor,
                             min_trades=self.trade_floor)
            self.features[pair] = feat

            # Snapshot BEFORE observing: rule 1's persistence condition is about
            # what happened on the PREVIOUS cycle.
            hist = self.history.snapshot(pair)
            v = RE.evaluate(feat, pair, _min_venues_for(pair), self.rules,
                            history=hist)
            self.verdicts[pair] = v
            self.history.observe(pair, feat,
                                 z_threshold=self._z_threshold("1"))

            met = [rid for rid, r in v.rules.items() if r.state == RE.MET]
            unev = [rid for rid, r in v.rules.items() if r.state == RE.UNEVALUABLE]
            # The loop-closing half of promotion: rules the auditor measured
            # into DROP stop producing candidates. They stay in rules_met (the
            # ledger records what actually fired) but no longer enter
            # expectancy math, so they stop consuming Bonferroni cells and
            # batch slots. UNVALIDATED rules keep producing candidates — that
            # is how a hypothesis earns its first measurement.
            live_met = [rid for rid in met if rid not in self._retired]

            # CANDIDATE side: a hypothesis under test, from the firing rule's
            # own direction_hint. It enters expectancy math so the auditor can
            # measure whether the rule has an edge. It CANNOT suppress, alert
            # or gate -- only an AUDITED rule reaches the verdict ledger.
            cand_dir = OUT.hint_direction(self.rules, _flow_by_rule(feat, live_met))
            # VERDICT side: what downstream may act on. Zero unless every
            # fired rule has been promoted by the auditor.
            verdict_dir = OUT.promoted_direction(
                self.rules, live_met, _flow_by_rule(feat, live_met))

            fill = self._fill_for(pair, snaps, feat)

            # DECISION-POINT LEDGER: one row per (pair, exit window), not one
            # per poll. ~25 consecutive polls share a single exit window; each
            # used to become its own row and the auditor had to repair the
            # overlap with an effective-sample-size correction. The upsert
            # keeps the window's FIRST row (its ts is the decision's birth)
            # and refreshes the state fields on every later poll, so the row
            # the resolver scores carries the freshest reading taken before
            # the exit bar closed.
            RC.upsert_verdict(
                self.db, pair=pair, ts=v.ts,
                direction=cand_dir if live_met else verdict_dir,
                horizon_bars=horizon_for(pair),
                exit_window_ms=self._exit_window_ms(pair, v.ts),
                fill_price=fill["price"],
                venues_live=v.n_venues,
                rules_met=met, rules_unevaluable=unev,
                score=v.score,
                candidate=bool(live_met),
                ref_venue=fill["venue"],
                fill_spread_bps=fill["spread_bps"],
                cross_basis_bps=fill["cross_basis_bps"],
                price_basis_ok=fill["ok"],
            )

        # close out rows whose horizon bar has now closed
        self._install_bar_fetchers()
        # Retire rows whose horizon bar no longer exists, then score what is
        # still scoreable. Without this the batch fills with un-retrievable
        # rows and nothing ever resolves.
        self.rows_unscorable += OUT.mark_unscorable(self.db)
        self.resolve_pending()

    def _fetch_one(self, fn, pair: str):
        book, tape = fn(pair)
        return {"pair": pair, "book": book, "tape": tape, "ts": time.time()}

    def _fill_for(self, pair: str, snaps: dict, feat) -> dict:
        """The recorded fill: ONE venue's mid, on the pair's REFERENCE venue.

        This replaces a depth-weighted mid across every live venue, which was
        the single most damaging defect in the engine.

        Depth-weighting across venues makes the fill price a function of which
        venue happens to quote the largest number, not of where the pair
        trades. Measured: OKX quotes ~287,000 units of XAU depth against
        Binance's ~7, so the XAU "fill" was 99.99% the OKX price while the exit
        was Binance's close. OKX gold sits ~30bps above Binance, and every one
        of 599 XAU rows resolved to the same ~-30bps with a 1.65bps standard
        deviation — the auditor read that constant offset as the one
        statistically significant loser in the book.

        `cross_basis_bps` is still recorded. The dispersion is real information
        and rule 1 is built on it; it just must not be used as a price.
        """
        venue = _ref_venue(pair)
        book = (snaps.get(venue) or {}).get("book")
        if book is None:
            return {"price": None, "venue": venue, "spread_bps": None,
                    "cross_basis_bps": feat.basis_dispersion_bps, "ok": False}
        return {
            "price": book.mid,
            "venue": venue,
            "spread_bps": book.spread_bps,
            "cross_basis_bps": feat.basis_dispersion_bps,
            "ok": True,
        }

    def _z_threshold(self, rule_id: str) -> float:
        spec = (self.rules.get("rules") or {}).get(rule_id) or {}
        try:
            return float(spec["thresholds"]["z_threshold"])
        except (KeyError, TypeError, ValueError):
            return 6.0

    def resolve_pending(self) -> int:
        """Close out rows whose horizon bar has now closed.

        GAP 1. `recorder.resolve()` existed and was tested but NOTHING CALLED
        IT: measured on the running engine, 336 rows and `forward_return NOT
        NULL` = 0. Without this call every row stays direction=0 forever and the
        auditor can never measure anything.

        A bar fetch failure leaves the row untouched rather than writing a zero.
        """
        try:
            n = OUT.resolve_pending(self.db, fetch_bar=self._fetch_horizon_bar,
                                    bar_ms_for=self._bar_ms_for)
        except Exception as e:                               # noqa: BLE001
            self.resolver_errors += 1
            self.last_resolver_error = f"{type(e).__name__}: {e}"
            return 0
        self.rows_resolved += n
        return n

    def _bar_ms_for(self, pair: str) -> int:
        """This pair's bar size in ms -- what `horizon_bars` is counted in.

        `horizon_for` holds a fixed 5-minute wall-clock horizon and converts it
        to bars per pair, so `horizon_bars=5` on a 1m pair and `horizon_bars=1`
        on a 5m pair both mean 300 seconds. The resolver needs the pair's real
        bar size to turn the count back into milliseconds; assuming 1 minute
        made every 5m pair measure a 60-second move instead.
        """
        try:
            return OUT.BAR_MS[P.bar_for(pair)]
        except KeyError:
            return 60_000

    def _exit_window_ms(self, pair: str, ts: float) -> int:
        """The decision-window dedup key: first bar close at or after
        ts+horizon, in epoch ms.

        MUST stay identical to the resolver's exit selection —
        `outcomes.select_exit_bar` picks the first venue-bar close >= at_ms,
        and venue bar closes are aligned to bar_ms multiples, so ceil() to the
        bar grid IS that selection. If this key and the realized exit ever
        disagree, the dedup would merge rows that resolve differently.
        """
        bar_ms = self._bar_ms_for(pair)
        at_ms = int(ts * 1000) + horizon_for(pair) * bar_ms
        return ((at_ms + bar_ms - 1) // bar_ms) * bar_ms

    def _fetch_horizon_bar(self, pair: str, at_ms: int):
        """The CLOSED bar covering `at_ms`, in the pair's own bar size.

        ONLY the pair's reference venue is consulted — the same venue the fill
        was recorded from. Falling back to another venue here would silently
        reintroduce the fill/exit basis mismatch that produced the XAU artifact,
        so a reference venue that cannot serve the bar yields None and the row
        retires unscorable. Losing a measurement beats measuring the wrong one.
        """
        try:
            bar = P.bar_for(pair)
        except KeyError:
            bar = "1m"
        fn = self._bar_fetch.get(_ref_venue(pair))
        if fn is None:
            return None
        try:
            return fn(pair, bar, at_ms)
        except Exception:                                  # noqa: BLE001
            return None

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
            "rows_resolved": self.rows_resolved,
            "rows_unscorable": self.rows_unscorable,
            "resolver_errors": self.resolver_errors,
            "last_resolver_error": self.last_resolver_error,
            # the price every fill and exit is measured on. If this is not the
            # venue the resolver scores against, expectancy is measuring a
            # venue spread and every number downstream is meaningless.
            "reference_venues": {p: _ref_venue(p) for p in self.pairs},
            "tape_coverage_floor_s": self.coverage_floor,
            "tape_trade_floor": self.trade_floor,
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
                        # reglas medidas a DROP: observadas, no candidatas
                        "retired_rules": sorted(self._retired),
            # Puente a la capa de VALIDACION (backtest-system). Dice que par
            # tiene una estrategia que pasa los gates del repo con artefacto
            # verificado. No lleva metricas: /health se lee mucho y las
            # metricas viven en el artefacto (run_id). Detalle: `for_pair()`.
            "backtest_link": BTL.link_summary(),
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

    def features_payload(self, pair: str | None = None) -> dict:
        """RAW per-venue features for live pairs — the contract surface for
        consumers that need the numbers, not the verdict (the ATAS rules 6/7
        gate, docs/FIXES-gate-DOM.md Fix 2-A, and any future regime
        conditioning). Only LIVE snapshots are exposed: a frozen venue is
        absent, never zero — the same rule the poller enforces upstream.

        `book_imb_5` is the top-5 BOOK imbalance per venue,
        (bid5 - ask5) / (bid5 + ask5) in [-1, 1] — the same cumulative
        construction as the ATAS/Binance DOM numbers, per venue. The
        touch_imbalance (best level only) is published alongside but is NOT
        gate material: measured 2026-10-11, it is structurally one-sided
        (median |x-0.5| = 0.275) and uncorrelated across venues — 3-of-4
        same-side consensus happened 0 times in 480 cycles. Consensus lives
        in the accumulated book, not in the touch.
        """
        out = {}
        pairs = [pair] if pair else self.pairs
        for p in pairs:
            snaps = self.poller.live_pairs(p)
            feat = self.features.get(p)
            if not snaps or feat is None:
                out[p] = {"live": False}
                continue
            imb5 = {}
            for v, s in snaps.items():
                b = s.get("book")
                if b is None:
                    continue
                d_bid, d_ask = b.depth_within(5)
                tot = d_bid + d_ask
                if tot > 0:
                    imb5[v] = (d_bid - d_ask) / tot
            out[p] = {
                "live": True,
                "n_venues": feat.n_venues,
                "touch_imbalance": {v: s.get("book").touch_imbalance
                                    for v, s in snaps.items()
                                    if s.get("book") is not None},
                "book_imb_5": imb5,
                "spread_bps": dict(feat.spread_bps),
                "flow_by_venue": dict(feat.flow_by_venue),
                "depth_total": dict(feat.depth_total),
                "ts": max((s.get("ts", 0) for s in snaps.values()), default=0),
            }
        return out

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


def _scale_book(book, price_mult: float, size_mult: float):
    """Bring a venue's book onto the pair's reference scale.

    A venue may quote the same underlying under a different contract
    multiplier: Binance and Bybit list 1000PEPEUSDT (the price is per 1000
    PEPE), OKX lists PEPE-USDT-SWAP (the price is per PEPE, the size in
    CONTRACTS of 10,000,000 PEPE) and Kraken lists PEPEUSD (the price is per
    PEPE, the size in PEPE).

    Price and size move TOGETHER so NOTIONAL is preserved: the registry
    picks m and n such that m*n is the number of tokens one price unit
    represents. Checked on a measured OKX touch: 17 contracts x 10,000,000
    PEPE at 4.3818e-06 = $744.9, and 17 x 10,000 = 170,000 units of 1000PEPE
    x 0.0043818 = $744.9. Same money.

    A ratio such as book_imb_5 is invariant under this transform; the price
    comparison in basis_bps is not, which is why it has to happen here and
    not be left to each consumer.
    """
    from types_ import Book

    if price_mult == 1.0 and size_mult == 1.0:
        return book

    bids = [(p * price_mult, q * size_mult) for p, q in book.bids]
    asks = [(p * price_mult, q * size_mult) for p, q in book.asks]
    return Book(bid=bids[0][0], bids=bids, asks=asks)


def _scale_trades(trades, price_mult: float, size_mult: float):
    """Same scale transform, applied to the raw trade list.

    Applied BEFORE the tape is aggregated so the tape's volumes and prices
    are in the same unit as the book — a tape that counts contracts next to
    a book that counts tokens is a unit bug waiting to be read as a signal.
    """
    if price_mult == 1.0 and size_mult == 1.0:
        return trades
    return [{"ts": t["ts"],
             "price": t["price"] * price_mult,
             "size": t["size"] * size_mult,
             "taker_side": t["taker_side"]} for t in trades]


def _coverage_for(pairs: list[str]) -> dict:
    """Registry coverage for the configured pairs, plus any pair that is not
    in the registry (reported honestly rather than omitted)."""
    cov = P.coverage()
    for pair in pairs:
        cov.setdefault(pair, {"venues": 0, "min_venues": 3,
                              "missing": ["not_in_registry"], "bar": "unknown"})
    return cov


def _flow_by_rule(feat, met: list[str]) -> dict[str, float]:
    """The flow each fired rule is asserting, as a delta_ratio in [0,1].

    For rule 4 (flow agreement) that is the median ratio of the agreeing
    venues. For rules without a flow leg it is the overall median, which is
    what a `follow` hint needs. A rule with no usable tape gets nothing, and
    therefore no provisional side.
    """
    out: dict[str, float] = {}
    if not met:
        return out
    if feat.flow_median is not None:
        for rid in met:
            out[rid] = feat.flow_median
    elif feat.flow_by_venue:
        vals = sorted(feat.flow_by_venue.values())
        med = vals[len(vals) // 2]
        for rid in met:
            out[rid] = med
    return out


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
            elif u.path == "/features":
                q = parse_qs(u.query)
                body = sc.features_payload(q.get("pair", [None])[0])
            elif u.path == "/backtest":
                # Capa de validacion: candidatos con artefacto verificado.
                #   /backtest                -> resumen por par
                #   /backtest?pair=XRP%2FUSD -> candidatos + run_id + metricas
                q = parse_qs(u.query)
                pair = q.get("pair", [None])[0]
                body = (BTL.for_pair(pair) if pair
                        else BTL.link_summary())
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