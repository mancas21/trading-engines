"""Live feature check: real books from 4 venues, cross-venue features computed.

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/tests/live_features.py
"""
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from poller import Poller
import features as F
import pairs
import venues.binance as bn
import venues.okx as okx
import venues.kraken as kr
import venues.bybit as bb

FETCH = {"binance": bn.fetch_depth, "okx": okx.fetch_depth,
         "kraken": kr.fetch_depth, "bybit": bb.fetch_depth}


def snap(pair, venue):
    book = FETCH[venue](pair)
    return {"pair": pair, "venue": venue, "book": book, "ts": time.time()}


if __name__ == "__main__":
    print(f"{'PAIR':10} {'V':>2}  {'DISPERSION':>12} {'IN SPREADS':>11} "
          f"{'FLOW DIV':>9}  Z BY VENUE (normalized by own spread)")
    print("-" * 104)
    for pair in pairs.all_pairs():
        snaps = {}
        for venue in pairs.venues_for(pair):
            try:
                snaps[venue] = snap(pair, venue)
            except Exception as e:                       # noqa: BLE001
                print(f"  {pair} {venue}: {type(e).__name__}: {str(e)[:50]}")
        f = F.compute(snaps)

        zs = " ".join(f"{v[:4]}={f.basis_z[v]:+7.1f}" for v in sorted(f.basis_z))
        disp = f"{f.basis_dispersion_bps:.3f}" if f.basis_dispersion_bps is not None else "n/a"
        insp = f"{f.dispersion_in_spreads:.1f}" if f.dispersion_in_spreads is not None else "n/a"
        fdiv = f"{f.flow_divergence:.4f}" if f.flow_divergence is not None else "n/a"
        print(f"{pair:10} {f.n_venues:>2}  {disp:>12} {insp:>11} {fdiv:>9}  {zs}")

    print()
    print("Read: 'IN SPREADS' is dispersion measured in units of the median venue")
    print("spread, which is what makes pairs comparable. A venue only registers a")
    print("large z if its gap is big relative to ITS OWN tick size.")
    print("All four rules are UNVALIDATED - no recorded outcomes yet.")