"""Live poller smoke: poll every configured (pair, venue) once and report.

Proves the poller's failure routing against the real exchanges: which
(pairs, venues) returned a fresh snapshot, which failed, and what the
staleness/throttle reports say.

WHY VENUES ARE POLLED VENUE-BY-VENUE
====================================
The first version of this script polled pair-major (all 4 venues for SUI, then
all 4 for XRP...). Kraken rejects SPCX with `EQuery:Unknown asset pair`, the
poller correctly penalized the *venue*, and because that cooldown is venue-wide
it silently skipped Kraken for the remaining pairs in the same run — HYPE and
ETH came back with 3 venues and NO failure recorded. That is the exact
silent-drop failure this engine must not have, caused by the script's own
ordering. Polling venue-major keeps any single venue's penalty contained to a
visible line in the failure report.

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/tests/live_poller_smoke.py
"""
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from poller import Poller
import venues.binance as bn
import venues.okx as okx
import venues.kraken as kr
import venues.bybit as bb

PAIRS = ["SUI/USD", "XRP/USD", "SOL/USD", "SPCX/USD", "NVDA/USD",
         "HYPE/USD", "ETH/USD", "XAU/USD"]


def fetch_binance(pair):
    b = bn.fetch_depth(pair, limit=20)
    return {"book": b, "ts": time.time()}


def fetch_okx(pair):
    b = okx.fetch_depth(pair)
    return {"book": b, "ts": time.time()}


def fetch_kraken(pair):
    b = kr.fetch_depth(pair)
    return {"book": b, "ts": time.time()}


def fetch_bybit(pair):
    b = bb.fetch_depth(pair)
    return {"book": b, "ts": time.time()}


FETCH = {"binance": fetch_binance, "okx": fetch_okx,
         "kraken": fetch_kraken, "bybit": fetch_bybit}


if __name__ == "__main__":
    p = Poller(max_age_s=300.0)
    t0 = time.perf_counter()

    for pair in PAIRS:
        base = pair.split("/")[0]
        p.set_min_venues(pair, 2 if base in ("SPCX", "NVDA") else 3)

    # venue-major: all pairs for binance, then okx, ...
    for venue, fn in FETCH.items():
        for pair in PAIRS:
            p.run_venue(venue, pair, lambda fn=fn, pair=pair: fn(pair))

    dt = time.perf_counter() - t0
    p.begin_cycle()
    h = p.health()

    print(f"=== 4 venues x {len(PAIRS)} pairs in {dt:.2f}s "
          f"({dt/(len(PAIRS)*4)*1000:.0f}ms per poll) ===\n")

    print(f"{'PAIR':10} {'LIVE':>4}  {'FLOOR':>5}  {'OK?':>4}  {'SPREAD bps (by venue)':<44}")
    print("-" * 92)
    for pair in PAIRS:
        live = p.live_pairs(pair)
        floor = p.min_venues(pair)
        spread = {}
        for v, s in sorted(live.items()):
            b = s.get("book")
            if b is not None:
                spread[v] = f"{b.spread_bps:.3f}"
        ok = "yes" if p.has_evidence(pair) else "NO"
        print(f"{pair:10} {len(live):>4}  {floor:>5}  {ok:>4}  "
              f"{'  '.join(f'{k}={v}' for k, v in spread.items()):<44}")

    print("\n=== frozen venues (were live, now stale) ===")
    print("   ", h["frozen_venues"] or "none")

    print("\n=== throttled venues ===")
    print("   ", h["throttled_venues"] or "none")

    print("\n=== failures (declared, not zeroed) ===")
    for pair, vms in sorted(h["failures"].items()):
        for v, f in sorted(vms.items()):
            print(f"    {pair:10} {v:8} x{f['n']}  {f['error'][:70]}")

    total = len(PAIRS) * 4
    okc = sum(len(p.live_pairs(x)) for x in PAIRS)
    print(f"\nlive snapshots: {okc}/{total}")