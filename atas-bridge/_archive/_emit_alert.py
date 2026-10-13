"""
_emit_alert.py — Evaluate live data and emit Telegram alert if conditions trigger.

Uses the deterministic signal engine (signal_engine.py) for all scoring.
No hardcoded bias scores.
"""

import json
import os
import urllib.request
from urllib.parse import quote
from signal_engine import build_snapshot_from_live, compute_signal, _check_rule_7_accumulation

# ── Config ──
# Load .env (same pattern as bridge_server.py)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip()
                if _k and _v and not os.environ.get(_k):
                    os.environ[_k] = _v

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

LIVE_PATH = r"C:\atas-bridge\_live_indicators.json"


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "none":
        print("Telegram token not set.")
        return
    payload = f"chat_id={TELEGRAM_CHAT_ID}&text={quote(text)}&parse_mode=HTML"
    try:
        req = urllib.request.Request(TELEGRAM_API, data=payload.encode(), method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            print("Telegram alert emitted successfully:", resp.status)
    except Exception as e:
        print("Telegram send failed:", e)


def evaluate_and_emit():
    """Evaluate current market state via signal_engine and emit alert if warranted."""
    if not os.path.exists(LIVE_PATH):
        print("No live indicators found.")
        return

    with open(LIVE_PATH, "r", encoding="utf-8-sig") as f:
        live = json.load(f)

    price = live.get("price", {}).get("last", 0.0)
    dom = live.get("dom", {})
    imbalance = dom.get("imbalance_pct", 0.0)

    snap = build_snapshot_from_live(live)
    if not snap:
        print("Could not build snapshot from live data.")
        return

    sig = compute_signal(snap)

    print(f"Bias score: {sig.bias_score:.1f} — {sig.bias_label.upper()}")
    print(f"Rule 6 (distribution): {sig.rule_6_active}")
    print(f"Rule 7 (accumulation): {sig.rule_7_active}")
    print(f"Trade plan allowed: {sig.trade_plan_allowed}")

    # Only emit alert for actionable signals
    if not sig.trade_plan_allowed:
        print("No actionable signal — not emitting alert.")
        return

    if sig.bias_label == "bullish" and sig.rule_7_active:
        direction = "LONG"
        emoji = "🟢"
        msg = (
            f"{emoji} <b>LONG SIGNAL TRIGGERED</b> {emoji}\n"
            f"SUIUSDT @ ${price:.4f}\n"
            f"Bias: {sig.bias_score:.0f}/100 — {sig.bias_label.upper()}\n"
            f"DOM: {imbalance:+.1f}%\n"
            f"Rule 7 accumulation conditions met"
        )
    elif sig.bias_label == "bearish" and sig.rule_6_active:
        direction = "SHORT"
        emoji = "🔴"
        msg = (
            f"{emoji} <b>SHORT SIGNAL TRIGGERED</b> {emoji}\n"
            f"SUIUSDT @ ${price:.4f}\n"
            f"Bias: {sig.bias_score:.0f}/100 — {sig.bias_label.upper()}\n"
            f"DOM: {imbalance:+.1f}%\n"
            f"Rule 6 distribution conditions met"
        )
    else:
        print(f"Bias {sig.bias_label} but Rule 6/7 not both active — no alert.")
        return

    print(msg)
    send_telegram(msg)


if __name__ == "__main__":
    evaluate_and_emit()
