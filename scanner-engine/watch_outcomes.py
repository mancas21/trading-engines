"""Watch the outcome pipeline drain -- the queue-ordering fix in action.

`pending_rows` ordered `ts DESC` (newest first) while `resolve_pending` can
only score a row once its horizon bar has CLOSED. The newest rows are the ones
that cannot be resolved yet, so the resolver fetched its batch, resolved
nothing, and the ready rows starved until `mark_unscorable` retired them at
1800s. Measured before the fix: 298 candidate rows retired unscorable.

What to look for after the fix:
  * `pendientes` stays BOUNDED (it should hover around the number of rows
    inside one horizon window, not grow without limit)
  * `pendientes>30min` stays at 0 -- nothing should be waiting past the
    retirement age
  * `resueltos` grows every cycle
  * `unscorable` grows slowly (only genuinely unresolvable rows), not in bursts

Usage:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/watch_outcomes.py [minutes]
"""
import sqlite3
import sys
import time
from pathlib import Path

DB = Path(__file__).resolve().parent / "data" / "scanner.db"
MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0


def snap() -> dict:
    db = sqlite3.connect(str(DB))
    one = lambda sql, *a: db.execute(sql, a).fetchone()[0]  # noqa: E731
    now = time.time()
    out = {
        "pending": one("SELECT COUNT(*) FROM verdicts WHERE resolved=0"),
        "resolved": one("SELECT COUNT(*) FROM verdicts "
                        "WHERE resolved=1 AND unscorable=0"),
        "unscorable": one("SELECT COUNT(*) FROM verdicts WHERE unscorable=1"),
        "lost_signals": one("SELECT COUNT(*) FROM verdicts "
                            "WHERE candidate=1 AND unscorable=1"),
        "pending_over_age": one("SELECT COUNT(*) FROM verdicts "
                                "WHERE resolved=0 AND ts < ?", now - 1800.0),
        "oldest_pending_s": (now - (one("SELECT MIN(ts) FROM verdicts "
                                        "WHERE resolved=0") or now)),
    }
    db.close()
    return out


def main() -> int:
    first = snap()
    print(f"  {'t':>5}  {'pend':>5} {'resueltos':>9} {'unscor':>7} "
          f"{'perdidas':>8} {'>30min':>7} {'mas vieja':>10}")
    base = None
    steps = max(1, int(MINUTES * 60 / 30))
    for i in range(steps + 1):
        s = snap()
        if base is None:
            base = s
        flag = ""
        if s["pending_over_age"] > 0:
            flag = "  <-- ALGO ESTA VARADO PASADO EL LIMITE"
        print(f"  {i*30:>5}s  {s['pending']:>5} {s['resolved']:>9} "
              f"{s['unscorable']:>7} {s['lost_signals']:>8} "
              f"{s['pending_over_age']:>7} {s['oldest_pending_s']:>10.0f}{flag}")
        if i < steps:
            time.sleep(30)

    last = snap()
    print()
    print(f"  pendientes : {first['pending']} -> {last['pending']}")
    print(f"  resueltos  : +{last['resolved']-first['resolved']}")
    print(f"  unscorable : +{last['unscorable']-first['unscorable']}")
    print(f"  >30min     : {last['pending_over_age']}  "
          f"{'(OK)' if last['pending_over_age']==0 else '(VARADO)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())