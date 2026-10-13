"""Auditor tick for the cron: emit ONLY when something actually changed.

The auditor runs every 30 minutes. At 12s per cycle the database gains ~150
rows per tick, so the sample size moves constantly — but "n grew" is not news.
The point is to be told when:

  * a rule crosses the significance floor (n >= 50 and |t| >= 1.65), or
  * a rule changes bucket (insufficient -> inconclusive -> keep -> drop), or
  * a rule is ELIGIBLE for promotion, or
  * the pipeline itself breaks (no data, resolver failing, venues frozen).

Everything else exits 0 with no output, and the cron delivers nothing.

STATE FILE: data/audit_state.json holds the last emitted summary. It is what
makes "changed" mean something across ticks.

Run:  python scanner-engine/audit_tick.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import expectancy as E          # noqa: E402
import promotion as PROM        # noqa: E402
import recorder as RC          # noqa: E402
from recorder import _guard     # noqa: E402
import rules_eval as RE         # noqa: E402

DB = HERE / "data" / "scanner.db"
RULES = HERE / "rules.json"
STATE = HERE / "data" / "audit_state.json"

MIN_N = E.DEFAULT_MIN_N
MIN_T = E.DEFAULT_MIN_T

# Bump whenever the POPULATION or the STATISTIC changes. The state file records
# the version it was written under; a mismatch means every stored verdict was
# computed on numbers that no longer mean what they meant, so the tick reports
# a retraction instead of comparing against them.
#
# v2 (2026-10-11) — the measurement-integrity pass:
#   * rows whose fill venue is not the exit venue are excluded (price_basis_ok)
#   * t is computed on the overlap-corrected effective sample size
#   * expectancy is net of a round-trip cost
#   * the threshold is Bonferroni-derived from the number of cells graded
# The rule-4 verdict stored under v1 ("n=691 t=-1.94 significant: true") was a
# measurement artifact and must not be carried forward.
#
# v3 (2026-10-11) — the horizon-selection pass:
#   * the exit bar is the first CLOSED bar whose close is AT OR AFTER
#     ts+horizon (ceil, outcomes.select_exit_bar), not the last close before
#     it. Under the old floor selection a 5m pair's exit bar was the bar
#     CONTAINING the signal, so its realized horizon was uniform in (0, 300s]
#     with mean ~150s — never the 300s the row claimed. Ceil guarantees every
#     row spans at least its intended horizon.
#   * rows record exit_close_ms, so the auditor reports the REALIZED horizon
#     per cell (expectancy: hz_med_s) instead of trusting the label.
# Every verdict resolved under v2 carries a realized horizon drawn from a
# different distribution than v3 rows; mixing them in one cell is exactly the
# incomparability this column exists to prevent.
#
# v4 (2026-10-11) — the decision-point ledger:
#   * the scan loop writes ONE row per (pair, exit window) via
#     recorder.upsert_verdict, not one per ~12s poll. The ~25x row inflation
#     that made lag-1 autocorrelation the central statistical patch is gone;
#     n_eff should now approach n.
#   * rules 1 and 2 are RETIRED (DROP, measured): they stopped producing
#     candidates, shrinking the graded cells and tightening Bonferroni.
# v3 rows carry ~25x the overlapping mass of v4 rows for the same wall-clock;
# their t-statistics are not comparable and must not be pooled.
MEASUREMENT_VERSION = 4


def fingerprint(audit: dict) -> dict:
    """The parts worth waking a human for."""
    out = {}
    for rid, row in (audit.get("rules") or {}).items():
        t = row.get("t")
        n_eff = row.get("n_eff", row.get("n", 0))
        out[rid] = {
            "bucket": row.get("bucket"),
            # bucket changes only; n alone is not news
            "n": row.get("n"),
            "n_eff": n_eff,
            "t": round(t, 2) if t is not None else None,
            "mean": row.get("mean"),
            "hit": row.get("hit_rate"),
            "significant": bool(n_eff >= MIN_N and t is not None
                                and abs(t) >= audit.get("min_t", abs(MIN_T))),
        }
    return out


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def main() -> int:
    if not DB.exists():
        print(f"AUDIT: database {DB} does not exist — the scanner has never run.")
        return 0

    db = RC.open_db(DB)
    audit = E.audit(db)
    rules = RE.load_rules(RULES)

    c = audit.get("counts", {})
    health = {
        "rows_total": c.get("rows_total", 0),
        "rows_resolved": c.get("rows_resolved", 0),
        "candidate_resolved": c.get("rows_candidate_resolved", 0),
        "directed_resolved": c.get("rows_directed_resolved", 0),
        "awaiting": (c.get("rows_total", 0) - c.get("rows_resolved", 0)),
        "unscorable": c.get("rows_unscorable", 0),
        "pending": None,          # filled from SQL below
    }

    # ── pipeline broken? that is always worth saying ───────────────────────
    broken = []
    if audit.get("verdict") == "INSUFFICIENT DATA" and health["rows_total"] > 20:
        broken.append("no directed rows despite data being recorded")
    # The legacy backlog predates the resolver and cannot be scored honestly:
    # a 5-minute forward move is not recoverable from a 16-hour-old row. It is
    # expected to stay pending forever and is NOT a fault. Only rows recorded
    # recently are a real signal about resolver health.
    with _guard(db):
        # resolved=0 AND unscorable=0: the only rows that still OWE an outcome.
        pending_now = db.execute(
            "SELECT COUNT(*) FROM verdicts WHERE resolved = 0 AND unscorable = 0"
        ).fetchone()[0]
    if health["rows_resolved"] == 0 and health["rows_total"] > 50:
        broken.append("zero rows resolved — outcome resolver is not running")
    elif pending_now > 40:
        broken.append(f"{pending_now} rows still pending an outcome "
                      f"(resolved {health['rows_resolved']} and climbing, "
                      f"so this is backlog, not a stall)")

    prev = load_state()
    fp = fingerprint(audit)
    prev_fp = prev.get("fingerprint", {})
    changes = []

    # -- measurement version changed: every stored verdict is stale ----------
    prev_version = prev.get("measurement_version")
    if prev_version is not None and prev_version != MEASUREMENT_VERSION:
        old4 = prev_fp.get("4", {})
        changes.append(
            f"MEASUREMENT BASIS CHANGED v{prev_version} -> v{MEASUREMENT_VERSION}. "
            f"Stored verdicts are retracted and re-derived; the previous "
            f"rule-4 result (n={old4.get('n')} t={old4.get('t')}) was computed "
            f"under a retired measurement basis (v1: overlapping samples, gross "
            f"of costs, wrong fill/exit basis; v2: 5m pairs resolved against "
            f"the bar containing the signal, realized horizon 0-300s). Do not "
            f"carry it forward."
        )
        prev_fp = {}      # re-baseline: every rule reads as a first measurement

    for rid, cur in fp.items():
        old = prev_fp.get(rid)
        if old is None:
            changes.append(f"rule {rid}: first measurement - {cur['bucket']} "
                           f"n={cur['n']} n_eff={cur['n_eff']} t={cur['t']} "
                           f"net mean={cur['mean']:+.6f}")
            continue
        if cur["bucket"] != old["bucket"]:
            changes.append(f"rule {rid}: bucket {old['bucket']} -> {cur['bucket']} "
                           f"(n={cur['n']} n_eff={cur['n_eff']} t={cur['t']})")
        elif not old.get("significant") and cur["significant"]:
            changes.append(f"rule {rid}: now SIGNIFICANT (n={cur['n']} "
                           f"n_eff={cur['n_eff']} t={cur['t']} "
                           f"net mean={cur['mean']:+.6f} hit={cur['hit']})")

    # promotion eligibility
    promo = PROM.promotion_report(db, rules, audit)
    eligible = [p for p in promo if p["state"] == "PROMOTE"]
    prev_elig = prev.get("eligible", [])
    now_elig = sorted(p["rule"] for p in eligible)
    for rid in now_elig:
        if rid not in prev_elig:
            changes.append(f"rule {rid}: ELIGIBLE for promotion — "
                           f"run `python promotion.py` to review")

    # row-count sanity: not news, but a big drop IS
    if prev.get("rows_total"):
        delta = health["rows_total"] - prev["rows_total"]
        if delta < 0:
            changes.append(f"row count DROPPED by {abs(delta)} "
                           f"({prev['rows_total']} -> {health['rows_total']})")

    # A CONTINUING pipeline problem is not news every 30 minutes. Report each
    # distinct problem once; re-report only if it persists AND the numbers
    # moved, or if a NEW problem appears. Measured: the "347 rows never
    # resolve" warning was identical on every tick because the row count had
    # not changed, so the change-detection never fired and the job repeated
    # itself forever.
    prev_problems = set(prev.get("problems", []))
    now_problems = set(broken)
    fresh = now_problems - prev_problems
    resolved_now = prev_problems - now_problems

    to_report = list(fresh)
    for b in resolved_now:
        to_report.append(f"RESOLVED: {b}")

    # Re-report a still-open problem only when the numbers behind it moved, so
    # a stuck backlog does not repeat verbatim forever. Keyed on resolved rows,
    # not total rows: the legacy pre-resolver backlog keeps total constant
    # while resolved climbs, and "resolved" is the number that proves the
    # resolver is making progress.
    still_open = now_problems & prev_problems
    if still_open and health["rows_resolved"] != prev.get("rows_resolved"):
        to_report.append("still open (progressing): " + "; ".join(sorted(still_open)))

    save_state({"fingerprint": fp, "eligible": now_elig,
                "measurement_version": MEASUREMENT_VERSION,
                "rows_total": health["rows_total"],
                "rows_resolved": health["rows_resolved"],
                "problems": sorted(now_problems),
                "ts": time.time()})
    db.close()

    # ── the ATAS ledger runs on the SAME scheduled task, same delivery ──
    # Two ledgers, two auditors, one cron, one chat. A reader failure must
    # never take the scanner tick down, so it is fenced.
    atas_lines: list[str] = []
    try:
        _reader_dir = str(HERE.parent / "reader")
        if _reader_dir not in sys.path:
            sys.path.insert(0, _reader_dir)
        import audit_atas as _ATAS                    # noqa: E402
        _atas_changes = _ATAS.tick()
        if _atas_changes:
            atas_lines.append("ATAS (reader.db):")
            atas_lines += [f"  * {c}" for c in _atas_changes]
    except Exception as e:                            # noqa: BLE001
        atas_lines.append(f"ATAS tick failed: {type(e).__name__}: {str(e)[:90]}")

    if not changes and not to_report and not atas_lines:
        return 0                       # silence: nothing changed anywhere

    lines = ["scanner-engine: audit update", ""]
    lines.append(f"  rows {health['rows_total']}  resolved {health['rows_resolved']}  "
                 f"auditable {c.get('rows_auditable', 0)}  "
                 f"candidate {health['candidate_resolved']}  "
                 f"directed {health['directed_resolved']}  "
                 f"pending {pending_now}  retired {health['unscorable']}  "
                 f"bad-basis {c.get('rows_bad_price_basis', 0)}")
    lines.append("")
    if to_report:
        lines.append("STATUS:")
        lines += [f"  - {b}" for b in to_report]
        lines.append("")
    if changes:
        lines.append("CHANGED:")
        lines += [f"  * {c}" for c in changes]
    else:
        lines.append("no rule changed state; sample still growing")

    for p in promo:
        if p["state"] != "no data":
            t = f"{p['t']:+.2f}" if p.get("t") is not None else "n/a"
            lines.append(f"    rule {p['rule']:2} {p['state']:8} n={p.get('n')} "
                         f"t={t}  {p['detail']}")

    if atas_lines:
        lines.append("")
        lines += atas_lines

    text = "\n".join(lines)
    _deliver(text)
    print(text)
    return 0


def _load_env() -> dict:
    """The SAME .env candidates the verdict notifier reads, in the same order.

    The credentials live in atas-bridge/.env (the bridge is who notifies);
    reading only reader/.env made the first delivery attempt skip with "sin
    credenciales". mirroring verdict_notify._ENV_CANDIDATES keeps one source
    of truth for where the bot token lives. The chat can be overridden with
    AUDIT_TG_CHAT_ID if the audit ever wants its own destination.
    """
    root = HERE.parent
    candidates = (
        root / "atas-bridge" / ".env",
        root / "reader" / ".env",
        root / "quant-alert-engine" / ".env",
    )
    out: dict[str, str] = {}
    for env_path in candidates:
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
        if out.get("TELEGRAM_TOKEN") or out.get("TELEGRAM__TOKEN"):
            break
    return out


def _deliver(text: str) -> None:
    """Push the tick output to Telegram. Failures are printed, never raised —
    a dead chat must not turn a healthy audit into a cron error."""
    import urllib.request

    env = _load_env()
    tok = env.get("TELEGRAM__TOKEN") or env.get("TELEGRAM_TOKEN") or ""
    chat = (env.get("AUDIT_TG_CHAT_ID")
            or env.get("TELEGRAM__CHAT_ID") or env.get("TELEGRAM_CHAT_ID") or "")
    if not tok or not chat:
        print("[audit_tick] entrega Telegram omitida: sin credenciales en "
              "reader/.env (el informe quedó arriba)")
        return
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    body = json.dumps({"chat_id": chat, "text": text,
                       "disable_web_page_preview": True}).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode())
        if not resp.get("ok"):
            print(f"[audit_tick] Telegram rechazó el envío: {resp}")
    except Exception as e:                            # noqa: BLE001
        print(f"[audit_tick] entrega Telegram falló: {type(e).__name__}: "
              f"{str(e)[:90]} (el informe quedó arriba)")


if __name__ == "__main__":
    raise SystemExit(main())