"""Reconciliador: la cuenta real contra el estado que cree el engine.

El router escribe en `state/positions.json` lo que abrio y el stop que dejo
puesto. Este proceso comprueba, cada ciclo:

  1. POSICION CERRADA. Si el estado dice abierta y la cuenta esta plana,
     reconstruye el cierre desde `userTrades` (precio, PnL, comisiones) e
     infiere si salto el stop, el TP o fue otra cosa. Borra el estado.

  2. STOP DESAPARECIDO. Si hay posicion pero no hay STOP_MARKET en
     `/fapi/v1/openAlgoOrders`, lo vuelve a poner al nivel guardado. Si el
     precio ya cruzo ese nivel mientras estaba desprotegida, NO coloca un stop
     que se dispararia al instante: cierra a mercado, que es lo que el stop
     habria hecho.

  3. HUERFANA. Posicion en un simbolo permitido sin estado (abierta a mano o
     por un proceso anterior). Solo avisa; no la toca salvo --close-orphans.

  4. DESAJUSTE. Si el estado dice LONG y la cuenta esta SHORT, o la cantidad
     no cuadra, no toca nada: lo marca para revision humana.

    python execution/reconcile.py                     # plan
    python execution/reconcile.py --execute           # recrea stops que falten
    python execution/reconcile.py --loop --execute

Sin --execute no envia nada: dice lo que haria. Recrear un stop no es una
operacion nueva, es restaurar la proteccion que el estado ya exige.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import proclock  # noqa: E402
import state_store  # noqa: E402
from binance_testnet import BinanceError, SafetyError, TestnetClient  # noqa: E402

LOG = HERE / "logs" / "reconcile.jsonl"
LOCK = "reconcile"
TOL = 0.005          # 0,5%: holgura para decir "salio por aqui"
ENTRY_GRACE_S = 120  # margen para no pisar una entrada en vuelo


def log(rec: dict) -> None:
    try:
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _ms(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        return int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return 0


def _amt(c: TestnetClient, sym: str) -> float:
    for p in c.positions(sym):
        a = float(p.get("positionAmt", 0) or 0)
        if a:
            return a
    return 0.0


def _stops(algo: list[dict]) -> list[dict]:
    return [a for a in algo
            if str(a.get("orderType") or a.get("type") or "").upper() == "STOP_MARKET"]


def _tps(algo: list[dict]) -> list[dict]:
    return [a for a in algo
            if str(a.get("orderType") or a.get("type") or "").upper() == "TAKE_PROFIT_MARKET"]


def classify_exit(px: float, stop: float | None, tp: float | None,
                  long_: bool) -> str:
    """Por donde salio, comparando el precio con los niveles guardados.

    Es una INFERENCIA: el exchange no dice "salto el stop". Un hueco puede
    dejar el fill mas alla del nivel, por eso la holgura TOL. Si no encaja con
    ninguno, se dice "otro" en vez de atribuirlo al stop.
    """
    if stop:
        hit = (px <= float(stop) * (1 + TOL)) if long_ else (px >= float(stop) * (1 - TOL))
        if hit:
            return "stop"
    if tp:
        hit = (px >= float(tp) * (1 - TOL)) if long_ else (px <= float(tp) * (1 + TOL))
        if hit:
            return "take_profit"
    return "otro (manual o mercado)"


def closed_report(c: TestnetClient, sym: str, rec: dict) -> dict:
    """Reconstruye el cierre con userTrades. Sin inventar: si no hay trades, lo dice."""
    opened = _ms(rec.get("opened_utc"))
    trades = [t for t in c.user_trades(sym, limit=1000)
              if int(t.get("time", 0)) >= opened - 1000]
    close_side = "SELL" if str(rec.get("side", "")).upper() == "LONG" else "BUY"
    entry_side = "BUY" if close_side == "SELL" else "SELL"
    exits = [t for t in trades if str(t.get("side", "")).upper() == close_side]
    entries = [t for t in trades if str(t.get("side", "")).upper() == entry_side]
    out = {"symbol": sym, "event": "closed", "opened_utc": rec.get("opened_utc"),
           "side": rec.get("side"), "qty_state": rec.get("qty"),
           "stop": rec.get("stop"), "take_profit": rec.get("take_profit"),
           "trades_since_open": len(trades), "exit_trades": len(exits)}
    if not exits:
        out["note"] = "posicion plana pero sin trades de cierre desde opened_utc"
        return out

    qty = sum(abs(float(t.get("qty", 0))) for t in exits)
    notional = sum(abs(float(t.get("qty", 0))) * float(t.get("price", 0)) for t in exits)
    px = notional / qty if qty else 0.0
    pnl = sum(float(t.get("realizedPnl", 0)) for t in exits)
    fee_exit = sum(float(t.get("commission", 0)) for t in exits)
    fee_entry = sum(float(t.get("commission", 0)) for t in entries)
    long_ = str(rec.get("side", "")).upper() == "LONG"
    out.update({"exit_price_wavg": round(px, 10), "exit_qty": qty,
                "realized_pnl_usdt": round(pnl, 6),
                "fee_entry_usdt": round(fee_entry, 6),
                "fee_exit_usdt": round(fee_exit, 6),
                "fees_usdt": round(fee_entry + fee_exit, 6),
                "net_usdt": round(pnl - fee_entry - fee_exit, 6),
                "reason_inferred": classify_exit(px, rec.get("stop"),
                                                 rec.get("take_profit"), long_)})
    return out


def protect(c: TestnetClient, sym: str, rec: dict, mark: float,
            execute: bool) -> tuple[str, int]:
    """Recrea el stop (y el TP si estaba anotado). Devuelve (estado, rc)."""
    long_ = str(rec.get("side", "")).upper() == "LONG"
    close_side = "SELL" if long_ else "BUY"
    stop = float(rec["stop"])
    d = 1 if long_ else -1

    # el precio ya cruzo el stop mientras estaba desprotegida
    if (mark - stop) * d <= 0:
        print(f"  CRITICO: mark {mark:g} ya cruzo el stop {stop:g}. "
              f"El stop habria cerrado: {'cierro a mercado' if execute else 'cerraria a mercado'}.")
        if not execute:
            return "past_stop_plan", 0
        try:
            r = c.close_position_market(sym, close_side, abs(float(rec.get("qty") or 0)))
            print(f"  cierre {close_side} -> orderId={r.get('orderId')} "
                  f"status={r.get('status')} exec={r.get('executedQty')}")
        except (BinanceError, SafetyError) as e:
            print(f"  CRITICO: el cierre fallo: {e}. POSICION DESCUBIERTA.")
            state_store.patch(sym, status="unprotected")
            return "unprotected", 6
        time.sleep(1)
        if _amt(c, sym) == 0:
            state_store.remove(sym)
            return "closed_past_stop", 0
        return "unprotected", 6

    try:
        resp = c.protective_order(kind="STOP_MARKET", symbol=sym, side=close_side,
                                  stop_price=stop, close_position=True,
                                  dry_run=not execute)
    except (BinanceError, SafetyError) as e:
        print(f"  CRITICO: no pude recrear el stop {stop:g}: {e}")
        if execute:
            state_store.patch(sym, status="unprotected")
        return "unprotected", 6

    if not execute:
        print(f"  plan: STOP_MARKET {close_side} trigger {stop:g} closePosition=true")
        return "would_recreate", 0

    sid = resp.get("algoId") or resp.get("orderId")
    trig = float(resp.get("triggerPrice") or stop)
    print(f"  STOP recreado algoId={sid} trigger {trig:g}")
    state_store.patch(sym, status="open", stop=trig, stop_algo_id=sid,
                      stop_recreated_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    tp = rec.get("take_profit")
    if tp:
        tp = float(tp)
        ok_side = (tp > mark) if long_ else (tp < mark)
        if ok_side:
            try:
                tresp = c.protective_order(kind="TAKE_PROFIT_MARKET", symbol=sym,
                                           side=close_side, stop_price=tp,
                                           close_position=True, dry_run=not execute)
                state_store.patch(sym, tp_algo_id=tresp.get("algoId") or tresp.get("orderId"))
                print(f"  TP recreado algoId={tresp.get('algoId') or tresp.get('orderId')}")
            except (BinanceError, SafetyError) as e:
                print(f"  AVISO: no pude recrear el TP {tp:g}: {e}")
    return "recreated", 0


def process(c: TestnetClient, sym: str, rec: dict, execute: bool) -> int:
    amt = _amt(c, sym)
    algo = c.open_algo_orders(sym)
    stops, tps = _stops(algo), _tps(algo)
    pos_txt = f"{amt:g}" if amt else "plana"
    print(f"\n{sym}  estado={rec.get('status')} {rec.get('side')} qty={rec.get('qty')} "
          f"stop={rec.get('stop')}  |  cuenta={pos_txt} stops={len(stops)} tps={len(tps)}")

    # El router escribe "entering" ANTES de enviar la entrada. Si el
    # reconciliador mira en ese hueco, veria "cerrada" y borraria el estado de
    # una posicion que esta naciendo. Se le da un margen; pasado el margen, un
    # "entering" es un proceso que murio y hay que protegerlo como cualquier otro.
    if str(rec.get("status")) == "entering":
        age = (time.time() * 1000 - _ms(rec.get("opened_utc"))) / 1000
        if 0 <= age < ENTRY_GRACE_S:
            print(f"  EN VUELO: entrada escrita hace {age:.0f}s; espero "
                  f"(margen {ENTRY_GRACE_S}s).")
            return 0

    if amt == 0:
        rep = closed_report(c, sym, rec)
        print(f"  POSICION CERRADA: precio {rep.get('exit_price_wavg')} "
              f"PnL {rep.get('realized_pnl_usdt')} fees {rep.get('fees_usdt')} "
              f"net {rep.get('net_usdt')} -> "
              f"{rep.get('reason_inferred') or rep.get('note')}")
        log(rep)
        if execute:
            state_store.remove(sym)
        else:
            print("  plan: borrar el estado (cerrada)")
        return 0

    have_long = amt > 0
    if str(rec.get("side", "")).upper() != ("LONG" if have_long else "SHORT"):
        print(f"  DESAJUSTE: el estado dice {rec.get('side')} y la cuenta esta "
              f"{'LONG' if have_long else 'SHORT'} {amt:g}. No toco nada; revisar a mano.")
        log({"symbol": sym, "event": "side_mismatch", "state": rec.get("side"), "account_amt": amt})
        return 6
    q = rec.get("qty")
    if q and abs(abs(float(q)) - abs(amt)) > max(1e-9, abs(amt) * 0.02):
        print(f"  AVISO: cantidad del estado {q} != cuenta {amt:g} (parcial o manual).")

    if stops:
        trig = float(stops[0].get("triggerPrice") or 0)
        want = float(rec.get("stop") or 0)
        if want and trig and abs(trig - want) / want > 1e-4:
            print(f"  AVISO: stop en el exchange {trig:g} != estado {want:g}. No lo muevo.")
        else:
            print(f"  OK: stop vivo algoId={stops[0].get('algoId')} trigger {trig:g}")
        if rec.get("status") != "open" and execute:
            state_store.patch(sym, status="open",
                              stop_algo_id=stops[0].get("algoId"))
        return 0

    print("  FALTA EL STOP: la posicion esta desprotegida.")
    mark = c.mark_price(sym)
    status, rc = protect(c, sym, rec, mark, execute)
    log({"symbol": sym, "event": "protect", "result": status, "mark": mark,
         "stop": rec.get("stop"), "execute": execute})
    return rc


def run_cycle(execute: bool, close_orphans: bool, only: str | None) -> int:
    state = state_store.load()
    c = TestnetClient(dry_run=not execute)
    c.sync_time()
    rc = 0

    syms = [only.upper()] if only else sorted(state)
    for sym in syms:
        rec = state.get(sym)
        if not rec:
            print(f"\n{sym}: sin estado; nada que reconciliar.")
            continue
        if sym not in c.allow_symbols:
            print(f"\n{sym}: fuera de ALLOW_SYMBOLS={c.allow_symbols}; no lo toco.")
            continue
        try:
            rc = max(rc, process(c, sym, rec, execute))
        except (BinanceError, SafetyError) as e:
            print(f"\n{sym}: ERROR {e}")
            rc = max(rc, 1)

    # huerfanas: posicion abierta sin estado, solo en simbolos permitidos
    if not only:
        pos = [p for p in c.positions()
               if float(p.get("positionAmt", 0) or 0) != 0
               and p["symbol"] in c.allow_symbols and p["symbol"] not in state]
        for p in pos:
            print(f"\nHUERFANA: {p['symbol']} {p['positionAmt']} @ {p['entryPrice']} "
                  f"upnl {float(p.get('unRealizedProfit', 0)):+.2f} sin estado.")
            log({"event": "orphan", "symbol": p["symbol"],
                 "amt": p["positionAmt"], "entry": p["entryPrice"]})
            if close_orphans and execute:
                import flatten  # noqa: PLC0415
                r = flatten.run(p["symbol"], True)
                print(f"  cerrada (rc={r})")
                rc = max(rc, r)
            else:
                print("  no la toco (hace falta --close-orphans).")

    print(f"\n{'OK' if rc == 0 else f'REVISAR (rc={rc})'}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="recrear stops y borrar estados de cierres (exige ALLOW_ORDERS=1)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--symbol", help="solo este simbolo")
    ap.add_argument("--close-orphans", action="store_true",
                    help="cerrar posiciones sin estado (por defecto solo avisa)")
    a = ap.parse_args()

    if not proclock.acquire(LOCK):
        print(f"ya hay un reconciliador vivo (pid {proclock.holder(LOCK)})")
        return 3
    try:
        while True:
            rc = run_cycle(a.execute, a.close_orphans, a.symbol)
            if not a.loop:
                return rc
            time.sleep(max(15, a.interval))
    except KeyboardInterrupt:
        return 0
    finally:
        proclock.release(LOCK)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BinanceError as e:
        print(f"BINANCE: {e}")
        raise SystemExit(7)
