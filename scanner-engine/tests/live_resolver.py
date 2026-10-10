"""Live proof that the outcome resolver actually closes rows.

The blocker was measured as: 336 rows, `forward_return NOT NULL` = 0. This runs
real cycles, lets horizons elapse, and reports what the resolver closed.

The horizon is 5 minutes, so this seeds rows with backdated timestamps rather
than waiting 5 minutes per row — the resolver's date arithmetic is exercised
against live bars, but the clock is not.

Run:  quant-alert-engine/venv/Scripts/python.exe scanner-engine/tests/live_resolver.py
"""
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import outcomes as OUT          # noqa: E402
import pairs as P              # noqa: E402
import recorder as RC          # noqa: E402
import scanner as SC           # noqa: E402

PY = HERE.parents[0] / "quant-alert-engine" / "venv" / "Scripts" / "python.exe"


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp())
    db_path = tmp / "s.db"

    sc = SC.Scanner(pairs=P.all_pairs(), db_path=str(db_path),
                    rules=__import__("rules_eval").load_rules(HERE / "rules.json"))

    print("cycle 1: poll the real venues and record verdicts")
    sc.run_cycle()
    c = RC.counts(sc.db)
    print(f"   rows={c['rows_total']}  candidate={c['rows_candidate_resolved']}  "
          f"resolved={c['rows_resolved']}")

    rows = sc.db.execute(
        """SELECT pair, candidate, direction, rules_met, fill_price, horizon_bars
             FROM verdicts ORDER BY pair""").fetchall()
    print("\n   pair        cand dir  horizon  met              fill")
    for r in rows:
        print(f"   {r[0]:10}  {r[1]}    {r[2]:>2}   {r[4]:>2}   {r[3]:16} "
              f"{r[4] and round(r[4], 4)}")

    cand = [r for r in rows if r[1] == 1]
    print(f"\n   candidate rows: {len(cand)}  "
          f"(these carry a provisional side and enter expectancy math)")

    # ── now the part that was missing: resolve them ───────────────────────
    # backdate every row so its 5-minute horizon has already elapsed
    print("\nbackdating rows 6 minutes so every horizon has elapsed ...")
    sc.db.execute("UPDATE verdicts SET ts = ts - 360")
    sc.db.commit()

    sc._install_bar_fetchers()
    t0 = time.time()
    n = sc.resolve_pending()
    print(f"resolved {n} rows in {time.time() - t0:.1f}s "
          f"(resolver_errors={sc.resolver_errors})")
    if sc.last_resolver_error:
        print(f"   last error: {sc.last_resolver_error}")

    after = RC.counts(sc.db)
    print(f"\n   rows={after['rows_total']}  resolved={after['rows_resolved']}  "
          f"directed+resolved={after['rows_directed_resolved']}  "
          f"candidate+resolved={after['rows_candidate_resolved']}")

    print("\n   resolved rows, RAW forward returns:")
    for r in sc.db.execute(
            """SELECT pair, candidate, direction, fill_price, forward_return
                 FROM verdicts WHERE resolved=1 ORDER BY pair"""
    ).fetchall():
        print(f"      {r[0]:10} cand={r[1]} dir={r[2]:>2} "
              f"fill={r[3] and round(r[3], 4):>10} fwd={r[4]:+.6f}")

    # ── and the audit now has something to measure ────────────────────────
    import expectancy as E
    a = E.audit(sc.db)
    print("\n" + "=" * 74)
    print(E.render(a))
    print("=" * 74)

    ok = after["rows_resolved"] > 0
    print("\nRESULT:", "PASS - the resolver closed rows against live bars"
          if ok else "FAIL - nothing resolved; Gap 1 is still open")

    sc.db.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())