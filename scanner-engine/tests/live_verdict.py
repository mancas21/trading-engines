"""Live verdict: real books + real flow from 4 venues through features -> rules.

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/tests/live_verdict.py
"""
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import features as F
import pairs
import rules_eval as RE
import venues.binance as bn
import venues.okx as okx
import venues.kraken as kr
import venues.bybit as bb

FETCH = {"binance": bn.fetch_depth, "okx": okx.fetch_depth,
         "kraken": kr.fetch_depth, "bybit": bb.fetch_depth}

TRADES = {"binance": bn.fetch_trades, "okx": okx.fetch_trades,
          "kraken": kr.fetch_trades, "bybit": bb.fetch_trades}


def snap(pair, venue, with_tape=True):
    book = FETCH[venue](pair)
    tape = None
    if with_tape:
        try:
            from flow import window_cvd, DEFAULT_WINDOW_S
            w = window_cvd(TRADES[venue](pair), window_s=DEFAULT_WINDOW_S)
            if w is not None:
                tape = {"delta_ratio": w.delta_ratio, "coverage_s": w.coverage_s,
                        "n_trades": w.n_trades}
        except Exception:
            tape = None
    return {"pair": pair, "venue": venue, "book": book, "tape": tape, "ts": time.time()}


if __name__ == "__main__":
    rules = RE.load_rules()
    print(f"{'PAIR':10} {'V':>2} {'FLR':>3}  {'R1':>12} {'R2':>12} {'R3':>12} {'R4':>12}  DIR")
    print("-" * 92)
    for pair in pairs.all_pairs():
        snaps = {}
        for venue in pairs.venues_for(pair):
            try:
                snaps[venue] = snap(pair, venue)
            except Exception as e:                        # noqa: BLE001
                pass
        f = F.compute(snaps)
        v = RE.evaluate(f, pair, pairs.min_venues_for(pair), rules)

        cells = []
        for rid in ("1", "2", "3", "4"):
            r = v.rules[rid]
            cells.append(f"{r.state:>12}")
        fl = v.n_venues / v.min_venues
        print(f"{pair:10} {v.n_venues:>2} {fl:>3}  " + " ".join(cells) +
              f"  {v.direction}")

    print()
    print("UNEVALUABLE is the honest state for any rule whose subcondition")
    print("cannot be asked yet (no depth history, no price-response window).")
    print("No rule is MET on day one because none has recorded outcomes.")
    print()
    v = RE.evaluate(F.compute({}), "SUI/USD", 3, rules)
    print("Empty-data verdict, full JSON:")
    import json
    print(json.dumps(v.to_dict(), indent=2)[:1200])