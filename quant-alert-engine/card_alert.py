#!/usr/bin/env python3
"""Telegram card alert — sends the EXACT card the chat analysis delivers.

There is one implementation of the card (card_live/: fetch_live.py -> live_read.py ->
live_card.py) and two delivery channels that both read it: the chat read and this
script. No second copy of the scoring/formatting, so the two can never drift.

  python card_alert.py            # print the card that would be sent (dry run)
  python card_alert.py --send     # print and post it to the configured Telegram chat
  python card_alert.py --json     # emit {"card": ..., "sent": bool} for callers (the engine)

Read-only: public market data only, no exchange keys, no order endpoints.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CARD_DIR = os.path.join(HERE, "card_live")


def build_card(timeout=420):
    """Refresh the snapshot and return the card text (raises on failure)."""
    py = sys.executable or "python"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for step in ("fetch_live.py", "live_read.py"):
        r = subprocess.run([py, os.path.join(CARD_DIR, step)], capture_output=True,
                           text=True, timeout=timeout, cwd=CARD_DIR, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"{step} failed (rc={r.returncode}): {(r.stderr or '')[-300:]}")
    r = subprocess.run([py, os.path.join(CARD_DIR, "live_card.py")], capture_output=True,
                       text=True, timeout=timeout, cwd=CARD_DIR, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"live_card.py failed (rc={r.returncode}): {(r.stderr or '')[-300:]}")
    return r.stdout.strip("\n")


def send(text, parse_mode=None, cfg_path=None):
    """Post to the chat configured in config.json (plain text by default)."""
    import urllib.error
    import urllib.request
    cfg = json.load(open(cfg_path or os.path.join(HERE, "config.json"), encoding="utf-8"))
    tg = cfg.get("telegram") or {}
    token, chat = tg.get("token"), tg.get("chat_id")
    if not token or token == "none" or not chat:
        print("telegram token/chat_id missing in config.json", file=sys.stderr)
        return False
    body = {"chat_id": chat, "text": text}
    if parse_mode:
        body["parse_mode"] = parse_mode
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except urllib.error.HTTPError as e:
        print("telegram HTTP", e.code, e.read().decode()[:300], file=sys.stderr)
        return False


def main():
    t0 = time.time()
    card = build_card()
    if "--json" in sys.argv:
        sent = send(card) if "--send" in sys.argv else False
        print(json.dumps({"card": card, "sent": sent, "build_s": round(time.time() - t0, 1)}))
        return
    print(card)
    if "--send" in sys.argv:
        print(f"\ntelegram send -> {send(card)}  ({len(card)} chars, built in {time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
