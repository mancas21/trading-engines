"""Aplana un simbolo: cancela sus ordenes (normales y algo) y cierra la posicion.

    python execution/flatten.py --symbol XRPUSDT            # plan
    python execution/flatten.py --symbol XRPUSDT --execute   # lo hace

Es la herramienta de emergencia y de limpieza de pruebas. Solo toca el simbolo
que se le pasa; nunca otros. Con --execute exige ALLOW_ORDERS=1 (via cliente).

Orden de operaciones: primero cancela las protecciones, luego cierra a mercado.
Si el cierre falla, lo dice y deja de tocar nada mas.

`run()` es importable: el auto-router y el reconciliador lo usan para cerrar
sin duplicar logica.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from binance_testnet import BinanceError, SafetyError, TestnetClient  # noqa: E402
import state_store  # noqa: E402


def run(sym: str, execute: bool = False, *, quiet: bool = False) -> int:
    sym = sym.upper()
    say = (lambda *a, **k: None) if quiet else print

    c = TestnetClient(dry_run=not execute)
    c.sync_time()
    if sym not in c.allow_symbols:
        say(f"BLOQUEADO: {sym} no esta en ALLOW_SYMBOLS={c.allow_symbols}")
        return 2

    pos = [p for p in c.positions(sym) if float(p.get("positionAmt", 0)) != 0]
    oo = c.open_orders(sym)
    algo = c.open_algo_orders(sym)
    mark = c.mark_price(sym)

    say(f"{sym}  mark {mark:g}  modo {'EXECUTE' if execute else 'PLAN'}")
    say("  posicion : " + (f"{pos[0]['positionAmt']} @ {pos[0]['entryPrice']} "
                           f"upnl {float(pos[0]['unRealizedProfit']):+.2f}" if pos else "ninguna"))
    say(f"  ordenes  : {len(oo)} normales, {len(algo)} algo")
    for o in oo:
        say(f"    normal {o['orderId']} {o['side']} {o['type']} {o['origQty']} @ {o.get('price')}")
    for o in algo:
        say(f"    algo   {o['algoId']} {o['side']} {o.get('orderType')} "
            f"trigger {o.get('triggerPrice')} closePosition={o.get('closePosition')}")

    if not pos and not oo and not algo:
        say("\nYa esta plano. Nada que hacer.")
        state_store.remove(sym)
        return 0

    if not execute:
        say("\nPLAN: cancelar todo y cerrar a mercado. Repite con --execute.")
        return 0
    if not c.orders_enabled:
        say("\nBLOQUEADO: --execute sin ALLOW_ORDERS=1")
        return 2

    for o in algo:
        try:
            c.cancel_algo(o["algoId"], sym)
            say(f"  cancelada algo {o['algoId']}")
        except BinanceError as e:
            say(f"  AVISO: no pude cancelar algo {o['algoId']}: {e}")
    for o in oo:
        try:
            c.cancel(sym, order_id=o["orderId"])
            say(f"  cancelada {o['orderId']}")
        except BinanceError as e:
            say(f"  AVISO: no pude cancelar {o['orderId']}: {e}")

    if pos:
        amt = abs(float(pos[0]["positionAmt"]))
        side = "SELL" if float(pos[0]["positionAmt"]) > 0 else "BUY"
        try:
            r = c.close_position_market(sym, side, amt)
            say(f"  cierre {side} {amt:g} -> orderId={r.get('orderId')} "
                f"status={r.get('status')} exec={r.get('executedQty')}")
        except (BinanceError, SafetyError) as e:
            say(f"  CRITICO: el cierre fallo: {e}")
            return 6

    time.sleep(1)
    pos2 = [p for p in c.positions(sym) if float(p.get("positionAmt", 0)) != 0]
    oo2 = c.open_orders(sym)
    algo2 = c.open_algo_orders(sym)
    say(f"\nfinal: posicion={'NO' if not pos2 else pos2[0]['positionAmt']} "
        f"ordenes={len(oo2)} algo={len(algo2)}")
    if pos2 or oo2 or algo2:
        say("REVISAR: quedo estado abierto.")
        return 5
    state_store.remove(sym)
    say("OK: simbolo plano.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    return run(a.symbol, a.execute)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BinanceError as e:
        print(f"BINANCE: {e}")
        raise SystemExit(7)
