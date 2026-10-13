"""Long entry monitor — alerts Telegram when Rule 7 triggers for SUIUSDT long.
Uses ATR-based stop/target sizing instead of fixed offsets (P5)."""
import json, os, time, urllib.request
from signal_engine import build_snapshot_from_live, compute_signal

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
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID")
BRIDGE = "http://127.0.0.1:8000"
ALERTED = False

# ATR parameters (P5)
ATR_MULT_STOP = 1.5    # stop = price - ATR * this
ATR_MULT_T1 = 1.5      # T1 = price + ATR * this
ATR_MULT_T2 = 3.0      # T2 = price + ATR * this
_prev_close = None
_atr_ema = None
ATR_ALPHA = 0.33  # EMA smoothing for running ATR

def send_tg(msg):
    payload = json.dumps({"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        print("Alert sent to Telegram")
    except Exception as e:
        print(f"TG send failed: {e}")

def check():
    global ALERTED, _prev_close, _atr_ema
    try:
        live = json.loads(urllib.request.urlopen(f"{BRIDGE}/indicators", timeout=5).read())
    except:
        return

    price = live.get("price", {}).get("last", 0)
    high = live.get("price", {}).get("high", price)
    low = live.get("price", {}).get("low", price)
    p_open = live.get("price", {}).get("open", price)

    # P5: ATR calculation from bar range
    bar_range = high - low
    if _prev_close is not None:
        # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        true_range = max(bar_range, abs(high - _prev_close), abs(low - _prev_close))
        if _atr_ema is None:
            _atr_ema = true_range
        else:
            _atr_ema = _atr_ema * (1 - ATR_ALPHA) + true_range * ATR_ALPHA
    else:
        _atr_ema = bar_range if bar_range > 0 else 0.005  # fallback 50 bps
    _prev_close = price

    atr = _atr_ema if _atr_ema and _atr_ema > 0 else 0.005

    snap = build_snapshot_from_live(live)
    if not snap:
        return
    sig = compute_signal(snap)

    fp_ok = sum(1 for lv in snap.footprint_levels if lv.get("delta", 0) > 0 and lv.get("volume", 0) > 0) >= 3
    dom_ok = snap.dom_imbalance_pct > 10
    tape_ok = snap.tape_buy_count_pct >= 60
    price_up = snap.delta > 0

    if fp_ok and dom_ok and tape_ok and price_up and not ALERTED:
        # P5: ATR-based stop/targets
        stop = price - atr * ATR_MULT_STOP
        t1 = price + atr * ATR_MULT_T1
        t2 = price + atr * ATR_MULT_T2

        msg = (
            f"🚀 *LONG ENTRY TRIGGERED* | SUIUSDT ${price:.4f}\n\n"
            f"Rule 7 FULLY CONFIRMED:\n"
            f"✅ Footprint: {sum(1 for lv in snap.footprint_levels if lv.get('delta',0)>0)}/{len(snap.footprint_levels)} buy levels\n"
            f"✅ DOM: +{snap.dom_imbalance_pct:.1f}%\n"
            f"✅ Tape: {snap.tape_buy_count_pct:.0f}% buys\n"
            f"✅ Delta: +{snap.delta:+.1f}\n\n"
            f"ATR: ${atr:.4f}\n"
            f"Entry: ${price:.4f}\n"
            f"Stop: ${stop:.4f} ({atr*ATR_MULT_STOP:.4f} away)\n"
            f"T1: ${t1:.4f} | T2: ${t2:.4f}"
        )
        send_tg(msg)
        ALERTED = True
        return

    print(f"[{time.strftime('%H:%M:%S')}] Price=${price:.4f} ATR=${atr:.4f} Δ={snap.delta:+.0f} DOM={snap.dom_imbalance_pct:+.1f}% Tape={snap.tape_buy_count_pct:.0f}% Score={sig.bias_score:.0f}")

    if not (fp_ok and dom_ok and tape_ok and price_up):
        ALERTED = False

print("Monitoring SUIUSDT for long entry (Rule 7, ATR-based sizing)...")
while True:
    check()
    time.sleep(15)
