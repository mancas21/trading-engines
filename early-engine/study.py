#!/usr/bin/env python3
"""study.py — is an early event better than a random moment?

Reads the shadow log written by early_engine.py and, per detector and horizon,
compares the mean signed forward return after an EVENT against the mean after
an unconditional CONTROL sample. Only the difference against control is an
edge claim. An event that merely moves up is not evidence of anything: in a
rising market every control moves up too.

Reports for each (event name, horizon):
  n_event, n_control, mean_event_bps, mean_control_bps, excess_bps,
  t-stat of the difference (Welch, sample variance), hit rate vs control.

The t-stat ignores overlap between events that share a move. Treat it as an
upper bound on significance and require several independent days before
trusting any row.

Usage:  python study.py logs/early_events.jsonl
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict


def _welch(a: list[float], b: list[float]) -> float:
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (ma - mb) / se if se > 0 else float("nan")


def main(path: str) -> None:
    fwd = defaultdict(list)  # (name, horizon) -> returns
    ctrl = defaultdict(list)
    events_seen = defaultdict(int)
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    key_name = {}
    for r in rows:
        if r.get("kind") == "event":
            key_name[r["key"]] = r["name"]
            events_seen[r["name"]] += 1
    for r in rows:
        if r.get("kind") != "fwd":
            continue
        h = r["horizon_s"]
        if r["row"] == "control":
            ctrl[("CONTROL", h)].append(r["ret_bps"])
        else:
            name = key_name.get(r["key"], "?")
            fwd[(name, h)].append(r["ret_bps"])

    print(f"{'event':<20}{'h(s)':>6}{'n_ev':>7}{'n_ctl':>7}"
          f"{'ev_bps':>9}{'ctl_bps':>9}{'excess':>9}{'t':>7}{'hit_ev':>8}{'hit_ctl':>9}")
    for (name, h) in sorted(fwd):
        e = fwd[(name, h)]
        c = ctrl.get(("CONTROL", h), [])
        if not e or not c:
            continue
        me, mc = sum(e) / len(e), sum(c) / len(c)
        t = _welch(e, c)
        he = sum(1 for x in e if x > 0) / len(e)
        hc = sum(1 for x in c if x > 0) / len(c)
        print(f"{name:<20}{h:>6}{len(e):>7}{len(c):>7}"
              f"{me:>9.2f}{mc:>9.2f}{me - mc:>9.2f}{t:>7.2f}{he:>8.2%}{hc:>9.2%}")
    if not any(fwd.values()):
        print("no matched event/control horizons yet — let shadow mode run longer")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "logs/early_events.jsonl")
