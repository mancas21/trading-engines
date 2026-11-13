"""Entrada condicional testnet por cierre de vela 1h.

La senal se lee del mercado REAL (fapi.binance.com: klines y openInterestHist
publicos); la orden va al TESTNET. Para enviar hacen falta las dos llaves de
siempre: `--send` y ALLOW_ORDERS=1 (TestnetClient lo exige por su cuenta).

Modos:
  breakout : entra si una vela 1h CERRADA cierra por encima de --trigger.
  retest   : entra si una vela 1h CERRADA cierra dentro de
             [--zone-low, --zone-high] y el OI 1h sube mas de --oi-min-pct.

Reglas:
  - Tamano: --risk-pct del equity al stop, recortado por MAX_ORDER_NOTIONAL_USD.
  - Si ya hay posicion en el simbolo, aborta (no apila).
  - Si el precio ya esta bajo el stop, aborta (entrada sin sentido).
  - Expira a las --expiry-hours sin gatillo.
  - Estado terminal en logs/trigger_entry_<SYMBOL>.state: la tarea programada
    no relanza un watcher que ya entro o expiro.
  - Lock con pid en logs/trigger_entry_<SYMBOL>.lock: nunca dos instancias.
  - Cada decision en logs/trigger_entry.jsonl; las ordenes, en orders.jsonl.

Uso:
  python execution/trigger_entry.py --symbol JUPUSDT --mode retest \
      --zone-low 0.2748 --zone-high 0.2810 --stop 0.2692 --oi-min-pct 0 --send
  python execution/trigger_entry.py --symbol JUPUSDT --mode retest ... --once   # dry
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from binance_testnet import TestnetClient  # noqa: E402

PUBLIC = "https://fapi.binance.com"
LOGDIR = HERE / "logs"
LOG = LOGDIR / "trigger_entry.jsonl"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(event: str, **data) -> None:
    rec = {"ts": now_iso(), "event": event, **data}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False), flush=True)


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def last_closed(symbol: str) -> dict:
    ks = _get_json(f"{PUBLIC}/fapi/v1/klines?symbol={symbol}&interval=1h&limit=3")
    k = ks[-2]  # la ultima vela ABIERTA es ks[-1]
    return {
        "open_ms": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "close_ms": int(k[6]),
        "open_iso": time.strftime("%H:%M", time.gmtime(k[0] / 1000)),
        "close_iso": time.strftime("%H:%M", time.gmtime(k[6] / 1000)),
    }


def oi_change_1h(symbol: str) -> float | None:
    """Cambio % del OI en la ultima hora (mainnet, publico)."""
    try:
        h = _get_json(
            f"{PUBLIC}/futures/data/openInterestHist?symbol={symbol}&period=1h&limit=2")
        if len(h) < 2:
            return None
        a, b = float(h[-2]["sumOpenInterest"]), float(h[-1]["sumOpenInterest"])
        return (b / a - 1.0) * 100.0 if a else None
    except Exception as e:
        log("error", stage="oi", err=str(e))
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(path: Path) -> bool:
    if path.exists():
        try:
            pid = int(path.read_text().strip() or "0")
        except Exception:
            pid = 0
        if pid_alive(pid):
            return False
        path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def write_state(path: Path, status: str, **extra) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ts": now_iso(), "status": status, **extra}, ensure_ascii=False),
        encoding="utf-8")


def already_open(client: TestnetClient, symbol: str) -> bool:
    for p in client.positions(symbol):
        if abs(float(p.get("positionAmt", 0))) > 0:
            return True
    return False


def fire(args, candle: dict, oi_pct: float | None, state: Path | None) -> int:
    """Ejecuta la entrada (o la simula) una sola vez."""
    dry = not args.send
    client = TestnetClient(dry_run=dry)

    if already_open(client, args.symbol):
        log("abort", why="ya hay posicion abierta", symbol=args.symbol)
        if state:
            write_state(state, "abort_position", symbol=args.symbol)
        return 1

    mark = client.mark_price(args.symbol)
    if mark <= args.stop:
        log("abort", why="precio ya bajo el stop", symbol=args.symbol,
            mark=mark, stop=args.stop)
        if state:
            write_state(state, "abort_below_stop", symbol=args.symbol)
        return 1

    equity = float(client.account().get("totalWalletBalance", 0) or 0)
    risk_usd = equity * args.risk_pct / 100.0
    qty_risk = risk_usd / (mark - args.stop)
    qty_cap = float(client.max_notional) / mark
    qty = client.round_qty(args.symbol, min(qty_risk, qty_cap))
    risk_at_stop = qty * (mark - args.stop)
    log("decide", symbol=args.symbol, candle=candle, oi_1h_pct=oi_pct,
        mark=mark, equity=round(equity, 2), risk_usd=round(risk_usd, 2),
        qty=qty, notional=round(qty * mark, 2),
        risk_at_stop=round(risk_at_stop, 2),
        stop=args.stop, dry_run=dry)

    if args.send:
        client.set_leverage(args.symbol, args.leverage)

    order = client.place_order(
        symbol=args.symbol, side="BUY", qty=qty, order_type="MARKET",
        client_id=f"qae-trg-{int(time.time() * 1000)}", dry_run=dry)
    if dry:
        log("entry_dry", symbol=args.symbol, order=order)
        return 0

    if str(order.get("status")) != "FILLED":
        log("entry_failed", symbol=args.symbol, order=order)
        if state:
            write_state(state, "entry_failed", symbol=args.symbol)
        return 1
    log("entry_filled", symbol=args.symbol, order_id=order.get("orderId"),
        executed_qty=order.get("executedQty"))

    try:
        stop = client.protective_order(
            kind="STOP_MARKET", symbol=args.symbol, side="SELL",
            stop_price=args.stop, close_position=True, dry_run=dry)
    except Exception as e:
        # Nunca dejar una posicion sin stop: si la proteccion falla, cerrar.
        log("stop_failed", symbol=args.symbol, err=str(e))
        held = 0.0
        for p in client.positions(args.symbol):
            held = abs(float(p.get("positionAmt", 0)))
        if held > 0:
            try:
                client.close_position_market(args.symbol, "SELL", held)
                log("emergency_close", symbol=args.symbol, qty=held)
            except Exception as e2:
                log("emergency_close_failed", symbol=args.symbol, err=str(e2))
        if state:
            write_state(state, "closed_no_stop", symbol=args.symbol, err=str(e))
        return 1
    log("stop_placed", symbol=args.symbol, algo_id=stop.get("algoId"),
        algo_status=stop.get("algoStatus"), trigger=args.stop)
    if state:
        write_state(state, "filled", symbol=args.symbol,
                    order_id=order.get("orderId"), algo_id=stop.get("algoId"),
                    entry_mark=mark, qty=qty, stop=args.stop)
    return 0


def condition_met(args, c: dict, oi_pct: float | None) -> bool:
    if args.mode == "breakout":
        return c["close"] > args.trigger
    if args.zone_low is None or args.zone_high is None:
        return False
    if not (args.zone_low <= c["close"] <= args.zone_high):
        return False
    return oi_pct is not None and oi_pct > args.oi_min_pct


def main() -> int:
    ap = argparse.ArgumentParser(description="Entrada testnet por cierre 1h")
    ap.add_argument("--symbol", default="JUPUSDT")
    ap.add_argument("--mode", choices=("breakout", "retest"), default="breakout")
    ap.add_argument("--trigger", type=float)
    ap.add_argument("--zone-low", type=float)
    ap.add_argument("--zone-high", type=float)
    ap.add_argument("--oi-min-pct", type=float, default=0.0)
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--risk-pct", type=float, default=0.25)
    ap.add_argument("--leverage", type=int, default=5)
    ap.add_argument("--expiry-hours", type=float, default=24.0)
    ap.add_argument("--poll", type=int, default=20)
    ap.add_argument("--send", action="store_true", help="enviar de verdad (exige ALLOW_ORDERS=1)")
    ap.add_argument("--once", action="store_true", help="evaluar la ultima vela cerrada y salir (dry)")
    a = ap.parse_args()

    if a.mode == "breakout" and a.trigger is None:
        ap.error("--trigger es obligatorio en modo breakout")
    if a.mode == "retest" and (a.zone_low is None or a.zone_high is None):
        ap.error("--zone-low y --zone-high son obligatorios en modo retest")
    if a.mode == "retest" and not (a.zone_low < a.zone_high):
        ap.error("zona invalida: zone_low >= zone_high")

    state = LOGDIR / f"trigger_entry_{a.symbol}.state"
    lock = LOGDIR / f"trigger_entry_{a.symbol}.lock"

    log("start", symbol=a.symbol, mode=a.mode, trigger=a.trigger,
        zone_low=a.zone_low, zone_high=a.zone_high, oi_min_pct=a.oi_min_pct,
        stop=a.stop, risk_pct=a.risk_pct, send=a.send, expiry_h=a.expiry_hours)

    if a.once:
        c = last_closed(a.symbol)
        oi_pct = oi_change_1h(a.symbol) if a.mode == "retest" else None
        fresh = (time.time() * 1000 - c["close_ms"]) < 2 * 3600 * 1000
        log("once", candle=c, oi_1h_pct=oi_pct, fresh=fresh,
            met=bool(fresh and condition_met(a, c, oi_pct)))
        if fresh and condition_met(a, c, oi_pct):
            return fire(a, c, oi_pct, None)
        return 0

    if state.exists():
        log("already_done", state=str(state),
            content=json.loads(state.read_text(encoding="utf-8")))
        return 0
    if not acquire_lock(lock):
        log("already_running", lock=str(lock))
        return 0

    try:
        started = time.time()
        seen_open_ms = -1
        while True:
            if time.time() - started > a.expiry_hours * 3600:
                log("expired", symbol=a.symbol, mode=a.mode)
                write_state(state, "expired", symbol=a.symbol)
                return 0
            try:
                c = last_closed(a.symbol)
            except Exception as e:  # red caida: reintentar, no abortar
                log("error", stage="klines", err=str(e))
                time.sleep(max(5, a.poll))
                continue

            oi_pct = oi_change_1h(a.symbol) if a.mode == "retest" else None
            fresh = (time.time() * 1000 - c["close_ms"]) < 2 * 3600 * 1000
            if c["open_ms"] != seen_open_ms:
                seen_open_ms = c["open_ms"]
                ref = a.trigger if a.mode == "breakout" else a.zone_high
                log("watching", symbol=a.symbol, mode=a.mode, candle=c,
                    oi_1h_pct=oi_pct, fresh=fresh,
                    dist_pct=round((ref / c["close"] - 1) * 100, 3))

            if fresh and condition_met(a, c, oi_pct):
                return fire(a, c, oi_pct, state)
            time.sleep(max(5, a.poll))
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
