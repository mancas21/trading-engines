"""
orderflow_engine.py — Phase 2: live order-flow engine over the ATAS bridge.

Builds on the bridge's deterministic scorer (signal_engine.py in C:\\atas-bridge)
and the live order-flow data it exposes:
  - /indicators   -> LiveExporter footprint, tape, DOM, delta
  - /cvd/SOLUSDT  -> TRUE session cumulative delta (bar deltas)
  - /context/SOLUSDT -> value area (POC/VAH/VAL), vol regime, expected move

What it adds:
  1. monitor  -> continuous unattended loop: score -> log a call with a FULL
                 order-flow snapshot attached -> cooldown between calls.
  2. collect  -> archive /indicators + /cvd + /context snapshots to JSONL so
                 order-flow CAN eventually be backtested (no history exists today).
  3. score    -> one-shot: print the deterministic signal scorecard for a symbol.

Snapshot-on-call is the missing piece the audit flagged: pre-fix entries lacked
snapshots, so the calls track record was "NOT signal-validated". Every call this
engine logs carries its full snapshot so the forward track record IS validated.
"""
import json
import os
import sys
import time
import argparse
import datetime as _dt
import urllib.request
from urllib.error import URLError

# Reuse the bridge's deterministic scorer + rules (single source of truth)
_BRIDGE_DIR = os.getenv("ATAS_BRIDGE_DIR", os.getenv("BRIDGE_DIR", r"C:\atas-bridge"))
if os.path.exists(_BRIDGE_DIR) and _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

try:
    from signal_engine import build_snapshot_from_live, compute_signal  # noqa: E402
except ImportError:
    build_snapshot_from_live = None
    compute_signal = None

BASE = os.getenv("BRIDGE_URL", "http://127.0.0.1:8000").rstrip("/quant-alert").rstrip("/quant/filter").rstrip("/")

# Audit M5: honor a configured BRIDGE_TOKEN (same .env as the bridge). When set,
# every request carries X-Bridge-Token; when unset, requests are unauthenticated
# (bridge-side guard also allows all). Safe no-op unless the user enables auth.
_BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "").strip()


def _headers():
    h = {"User-Agent": "orderflow-engine"}
    if _BRIDGE_TOKEN:
        h["X-Bridge-Token"] = _BRIDGE_TOKEN
    return h


def _get(path, timeout=8):
    req = urllib.request.Request(BASE + path, headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path, payload, timeout=4):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={**_headers(), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ── Telegram delivery (same .env + API pattern as the bridge) ──
def _load_env():
    env_path = os.path.join(_BRIDGE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k and v and not os.environ.get(k):
                        os.environ[k] = v


_load_env()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram(text, parse_mode="Markdown"):
    """Send a message to Telegram. Returns True on success."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text,
                           "parse_mode": parse_mode}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8")).get("ok", False)
    except Exception:  # noqa: BLE001
        return False


def _alert_text(sym, sig, price, raw):
    """Build a compact order-flow alert message for Telegram."""
    cvd = raw.get("cvd", {})
    dom = raw.get("dom", {})
    va = raw.get("context", {}).get("value_area", {})
    direction = "LONG" if sig.bias_label == "bullish" else "SHORT"
    icon = "🟢" if direction == "LONG" else "🔴"
    cvd_v = cvd.get("cvd")
    lines = [
        f"{icon} *{direction} TRIGGER — {sym}*",
        f"Bias {sig.bias_score:.0f}/100 ({sig.bias_label}) · Rule 10 plan allowed",
        f"Price: `{price:.2f}`",
    ]
    if cvd_v is not None:
        lines.append(f"CVD: `{cvd_v:+,.0f}` ({cvd.get('bars','?')} bars)")
    if dom.get("imbalance_pct") is not None:
        lines.append(f"DOM imbalance: `{dom['imbalance_pct']:+.1f}%`")
    if va.get("poc"):
        lines.append(f"Value area: POC `{va['poc']:.2f}` VAH `{va['vah']:.2f}` VAL `{va['val']:.2f}` ({va.get('location','?')})")
    lines.append(f"_Order-flow snapshot logged via /calls (validated)_")
    return "\n".join(lines)


def live_snapshot(symbol):
    """Pull all order-flow data for one symbol into a dict.

    /cvd/{sym}, /context/{sym}, /dom/{sym} are per-symbol (Binance-derived).
    /indicators (LiveExporter footprint/tape/delta) reflects whichever symbol
    ATAS currently has on the chart. If that ticker != symbol, footprint/tape/
    delta are stripped and the snapshot is built from per-symbol DOM+CVD+context
    (still a valid bias score; footprint/tape score as neutral)."""
    symbol = symbol.upper().rstrip("/")
    raw = {}
    for name, path in [
        ("live", "/indicators"),
        ("cvd", f"/cvd/{symbol}"),
        ("context", f"/context/{symbol}"),
        ("dom", f"/dom/{symbol.lower()}"),
        ("state", f"/state/{symbol}"),
    ]:
        try:
            raw[name] = _get(path)
        except (URLError, Exception) as e:  # noqa: BLE001
            raw[name] = {"_err": str(e)[:80]}

    # Reconcile /indicators with the target symbol
    live = raw.get("live", {})
    dom = raw.get("dom", {})
    if isinstance(dom, dict) and not dom.get("_err"):
        # Per-symbol DOM is authoritative for the target symbol
        live = dict(live)
        live["dom"] = {
            "imbalance_pct": dom.get("imbalance_pct", live.get("dom", {}).get("imbalance_pct", 0)),
            "cumulative_bid_vol": dom.get("total_bid_vol", 0),
            "cumulative_ask_vol": dom.get("total_ask_vol", 0),
        }
    ind_ticker = str(live.get("ticker", "")).upper()
    if ind_ticker and ind_ticker != symbol:
        # Indicators are for another symbol -> strip chart-bound fields
        live = dict(live)
        live["ticker"] = symbol
        live["footprint"] = {}
        live["tape"] = {}
        live["delta"] = 0.0
        live["volume"] = {"total": 0, "bid": 0, "ask": 0}
        if isinstance(dom, dict) and dom.get("best_bid"):
            live["price"] = {"last": dom["best_bid"], "open": dom["best_bid"],
                             "high": dom["best_bid"], "low": dom["best_bid"]}
    raw["live"] = live
    return raw


def build_trade_plan(live_price, direction, ctx, bias_score):
    """Replicate the live-anchored trade plan sizing from _integrated_analysis.py
    (entry ±0.15%, SL 0.85x med, T1 1.0x med, T2 1.8x p90) using context expected move."""
    exp_med = ctx.get("expected_move", {}).get("value", 0.44) if isinstance(ctx, dict) else 0.44
    # p90 now rides on its own field (audit M4); fall back to legacy CI[1] if present
    em = ctx.get("expected_move", {}) if isinstance(ctx, dict) else {}
    exp_p90 = em.get("p90")
    if exp_p90 is None:
        ci = em.get("confidence_interval")
        exp_p90 = ci[1] if isinstance(ci, (list, tuple)) and len(ci) > 1 else None
    if exp_p90 is None:
        exp_p90 = exp_med * 4.6
    if isinstance(exp_med, dict):
        exp_med = exp_med.get("value", 0.44)
    if exp_med < 0.05:
        exp_med *= 100
    if exp_p90 < 0.05:
        exp_p90 *= 100
    half = live_price * 0.0015
    sl_d = live_price * (exp_med / 100) * 0.85
    t1_d = live_price * (exp_med / 100)
    t2_d = live_price * (exp_med / 100) * 1.8
    entry_low, entry_high = live_price - half, live_price + half
    if direction == "long":
        return dict(entry_low=round(entry_low, 4), entry_high=round(entry_high, 4),
                    sl=round(entry_low - sl_d, 4), t1=round(entry_high + t1_d, 4),
                    t2=round(entry_high + t2_d, 4), exp_med=exp_med, exp_p90=exp_p90)
    return dict(entry_low=round(entry_low, 4), entry_high=round(entry_high, 4),
                sl=round(entry_high + sl_d, 4), t1=round(entry_low - t1_d, 4),
                t2=round(entry_low - t2_d, 4), exp_med=exp_med, exp_p90=exp_p90)


def compact_snapshot(raw):
    """Extract a compact, validation-grade snapshot for the call record."""
    live = raw.get("live", {})
    fp = live.get("footprint", {})
    tape = live.get("tape", {})
    dom = live.get("dom", {})
    cvd = raw.get("cvd", {})
    return {
        "ts": time.time(),
        "price": live.get("price", {}).get("last"),
        "delta": live.get("delta"),
        "volume_total": live.get("volume", {}).get("total"),
        "volume_bid": live.get("volume", {}).get("bid"),
        "volume_ask": live.get("volume", {}).get("ask"),
        "poc": fp.get("poc"),
        "va_high": fp.get("value_area_high"),
        "va_low": fp.get("value_area_low"),
        "top_levels": fp.get("top_levels", []),
        "dom_imbalance": dom.get("imbalance_pct"),
        "dom_bid_ask_ratio": dom.get("bid_ask_ratio"),
        "tape_buy_vol": tape.get("buy_vol"),
        "tape_sell_vol": tape.get("sell_vol"),
        "tape_largest": tape.get("largest_trade"),
        "tape_largest_side": tape.get("largest_trade_side"),
        "cvd": cvd.get("cvd"),
        "cvd_bars": cvd.get("bars"),
        "cvd_bar_deltas": cvd.get("bar_deltas", []),
        "value_area": raw.get("context", {}).get("value_area", {}),
        "expected_move": raw.get("context", {}).get("expected_move", {}),
    }


def _last_logged_ts(path):
    try:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                if obj.get("ts"):
                    last = obj["ts"]
            return last
    except Exception:  # noqa: BLE001
        return 0.0


def monitor(symbols, cooldown_s=600, history=None, snapdir=None, poll=2.0):
    """Continuous loop over ANY list of symbols: score each, log validated calls
    with snapshots, archive history. Each symbol has its own cooldown.
    Long when bullish, short when bearish (both directions)."""
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    symbols = [s.upper().rstrip("/") for s in symbols]
    if not symbols:
        raise ValueError("no symbols given")
    call_file = history or os.path.join(os.path.dirname(__file__), "data", "orderflow_calls.jsonl")
    os.makedirs(os.path.dirname(call_file) or ".", exist_ok=True)
    hist = snapdir or os.path.join(os.path.dirname(__file__), "data", "orderflow_history")
    os.makedirs(hist, exist_ok=True)
    last_call = {s: 0.0 for s in symbols}

    print(f"[orderflow] monitoring {symbols} (cooldown {cooldown_s}s, both dirs, history -> {hist})")
    while True:
        for sym in symbols:
            try:
                raw = live_snapshot(sym)
                live = raw.get("live", {})
                if "_err" in live or not live.get("price") or not live["price"].get("last"):
                    continue
                price = float(live["price"]["last"])
                snap = build_snapshot_from_live(live, raw.get("cvd"))
                sig = compute_signal(snap) if snap else None
                now = time.time()

                if sig and sig.trade_plan_allowed and (now - last_call[sym]) >= cooldown_s:
                    direction = "long" if sig.bias_label == "bullish" else "short"
                    plan = build_trade_plan(price, direction, raw.get("context", {}), sig.bias_score)
                    snap_compact = compact_snapshot(raw)
                    note = (f"phase2 live-anchored {price:.2f} (bias {sig.bias_score:.0f}, "
                            f"cov={getattr(sig, 'data_coverage', 1.0):.2f}, "
                            f"sub={json.dumps(sig.sub_scores)})")
                    payload = {
                        "symbol": sym, "direction": direction,
                        "entry_low": plan["entry_low"], "entry_high": plan["entry_high"],
                        "sl": plan["sl"], "t1": plan["t1"], "t2": plan["t2"],
                        "bias_score": sig.bias_score,
                        "note": note,
                        "snapshot": snap_compact,
                    }
                    try:
                        res = _post("/calls", payload)
                        last_call[sym] = now
                        print(f"[{_dt.datetime.now().strftime('%H:%M:%S')}] CALL {res.get('call_id')} "
                              f"{sym} {direction.upper()} bias {sig.bias_score:.0f} cov={getattr(sig, 'data_coverage', 1.0):.2f} @ {price:.2f}")
                        with open(call_file, "a") as f:
                            f.write(json.dumps({"ts": now, "call_id": res.get("call_id"),
                                                "symbol": sym, **payload}) + "\n")
                        # Telegram alert on trigger
                        sent = send_telegram(_alert_text(sym, sig, price, raw))
                        print(f"  telegram_sent={sent}")
                    except Exception as e:  # noqa: BLE001
                        print(f"  ! {sym} call log failed: {e}")
                elif sig and not sig.trade_plan_allowed:
                    reasons = getattr(sig, "plan_blocked_reasons", [])
                    cov = getattr(sig, "data_coverage", 1.0)
                    rs = ",".join(reasons) if reasons else f"rule10 neutral (cov {cov:.2f})"
                    print(f"[{_dt.datetime.now().strftime('%H:%M:%S')}] {sym} bias "
                          f"{sig.bias_score:.0f} @ {price:.2f} (no plan: {rs})")

                # Archive history for future backtesting
                try:
                    hist_file = os.path.join(hist, f"{sym.lower()}.jsonl")
                    with open(hist_file, "a") as f:
                        f.write(json.dumps({"ts": now, **compact_snapshot(raw)}) + "\n")
                except Exception as e:  # noqa: BLE001
                    print(f"  ! {sym} history write failed: {e}")
            except Exception as e:  # noqa: BLE001
                print(f"  ! {sym} monitor error: {e}")
        time.sleep(poll)


def collect(symbol, seconds, out=None, poll=2.0):
    """Archive order-flow snapshots for `seconds` to JSONL (future backtesting)."""
    hist = out or os.path.join(os.path.dirname(__file__), "data", "orderflow_history")
    os.makedirs(hist, exist_ok=True)
    fpath = os.path.join(hist, f"{symbol.lower()}.jsonl")
    end = time.time() + seconds
    n = 0
    print(f"[collect] archiving {symbol} for {seconds}s -> {fpath}")
    while time.time() < end:
        try:
            raw = live_snapshot(symbol)
            live = raw.get("live", {})
            if not live.get("price"):
                time.sleep(poll); continue
            snap = compact_snapshot(raw)
            with open(fpath, "a") as f:
                f.write(json.dumps(snap) + "\n")
            n += 1
            if n % 10 == 0:
                print(f"  {n} snapshots ...")
        except Exception as e:  # noqa: BLE001
            print(f"  ! {e}")
        time.sleep(poll)
    print(f"[collect] done: {n} snapshots written to {fpath}")


def score(symbol):
    """One-shot: print the deterministic order-flow scorecard."""
    raw = live_snapshot(symbol)
    live = raw.get("live", {})
    if "_err" in live or not live.get("price"):
        print("No live data"); return
    snap = build_snapshot_from_live(live, raw.get("cvd"))
    sig = compute_signal(snap) if snap else None
    print(f"=== {symbol} ORDER-FLOW SCORE ===")
    print(f"price {live['price']['last']:.2f} | delta {live.get('delta'):+.1f} | "
          f"DOM imb {live.get('dom',{}).get('imbalance_pct'):+.1f}%")
    if sig:
        icon = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(sig.bias_label, "🟡")
        print(f"BIAS: {sig.bias_score:.0f}/100 {icon} {sig.bias_label.upper()} | "
              f"plan_allowed={sig.trade_plan_allowed}")
        for k, v in sig.sub_scores.items():
            print(f"  {k}: {v:.1f}/10")
        print(f"  rule_2_exhaustion={sig.rule_2_exhaustion} rule_6_dist={sig.rule_6_active} rule_7_acc={sig.rule_7_active}")
    va = raw.get("context", {}).get("value_area", {})
    if va:
        print(f"Value area: POC {va.get('poc')} VAH {va.get('vah')} VAL {va.get('val')} "
              f"loc={va.get('location')}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="orderflow_engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("monitor", help="continuous scoring + validated calls")
    m.add_argument("symbols", nargs="*", default=["SOLUSDT"],
                   help="one or more symbols (comma or space separated)")
    m.add_argument("--cooldown", type=float, default=600)
    m.add_argument("--poll", type=float, default=2.0)
    m.add_argument("--history", default=None)
    c = sub.add_parser("collect", help="archive order-flow history")
    c.add_argument("symbol", nargs="?", default="SOLUSDT")
    c.add_argument("--seconds", type=float, default=60.0)
    c.add_argument("--out", default=None)
    c.add_argument("--poll", type=float, default=2.0)
    s = sub.add_parser("score", help="one-shot scorecard")
    s.add_argument("symbol", nargs="?", default="SOLUSDT")
    a = p.parse_args()

    if a.cmd == "monitor":
        monitor(a.symbols, cooldown_s=a.cooldown, poll=a.poll, history=a.history)
    elif a.cmd == "collect":
        collect(a.symbol, seconds=a.seconds, out=a.out, poll=a.poll)
    elif a.cmd == "score":
        score(a.symbol)