"""Chequeo de conexion contra el testnet de Binance Futures. SOLO LECTURA.

    python execution/check_testnet.py --symbol XRPUSDT

No envia ordenes: como maximo construye la orden que se enviaria y la imprime
(`place_order(..., dry_run=True)`), que escribe una linea en logs/orders.jsonl
con result="not_sent".

Salidas:
  0 = conectado y (si hay claves) cuenta legible
  2 = faltan claves
  3 = claves presentes pero rechazadas por Binance
  4 = el simbolo no existe / no esta TRADING
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from binance_testnet import (BinanceError, SafetyError, TestnetClient,  # noqa: E402
                             ENV_FILE, fmt, mask)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XRPUSDT")
    ap.add_argument("--notional", type=float, default=20.0,
                    help="nocional de la orden de prueba en seco (USDT)")
    a = ap.parse_args()
    sym = a.symbol.upper()

    c = TestnetClient(dry_run=True)
    print(f"host            : {c.base}")
    print(f"simbolo         : {sym}")
    print(f"allow_symbols   : {', '.join(c.allow_symbols)}")
    print(f"max_order_usd   : {c.max_notional:g}")
    print(f"orders_enabled  : {c.orders_enabled}  (ALLOW_ORDERS={c.orders_enabled and '1' or '0'})")
    print(f"key             : {mask(c.key)}")
    print(f"secret          : {mask(c.secret)}")
    print(f"env file        : {ENV_FILE}  exists={ENV_FILE.is_file()}")

    t0 = time.time()
    off = c.sync_time()
    print(f"server time     : offset {off:+d} ms  (rtt {int((time.time()-t0)*1000)} ms)")

    try:
        r = c.rules(sym)
    except SafetyError as e:
        print(f"\nSIMBOLO: {e}")
        return 4
    print(f"reglas          : status={r['status']} tick={r['tick']} step={r['step']} "
          f"minQty={r['min_qty']} minNotional={r['min_notional']}")

    d = c.depth(sym, 5)
    bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
    mid = (bid + ask) / 2
    print(f"libro           : bid {bid} / ask {ask}  spread {(ask-bid)/mid*1e4:.2f} bps  "
          f"mark {c.mark_price(sym)}")

    if not c.has_keys():
        print("\nSIN CLAVES: el host y el simbolo responden, pero no puedo leer la cuenta.")
        print(f"Pon BINANCE_TESTNET_KEY y BINANCE_TESTNET_SECRET en:\n  {ENV_FILE}")
        return 2

    try:
        acc = c.account()
    except BinanceError as e:
        print(f"\nCUENTA RECHAZADA: {e}")
        if e.code in (-2015, -2014):
            print("  -> clave invalida, sin permiso de Futuros, o IP restringida.")
        return 3

    usdt = next((x for x in acc.get("assets", []) if x["asset"] == "USDT"), {})
    print(f"\ncuenta          : canTrade={acc.get('canTrade')} "
          f"canWithdraw={acc.get('canWithdraw')} feeTier={acc.get('feeTier')}")
    print(f"USDT            : wallet {fmt(float(usdt.get('walletBalance', 0)))}  "
          f"available {fmt(float(usdt.get('availableBalance', 0)))}  "
          f"unrealized {fmt(float(usdt.get('unrealizedProfit', 0)))}")
    print(f"posiciones      : {len(acc.get('positions', []))} (todas)  "
          f"dualSide={c.dual_side()}")

    pos = [p for p in c.positions(sym) if float(p.get("positionAmt", 0)) != 0]
    if pos:
        for p in pos:
            print(f"  {sym} amt={p['positionAmt']} entry={p['entryPrice']} "
                  f"upnl={fmt(float(p['unRealizedProfit']))} lev={p['leverage']}")
    else:
        print(f"  {sym}: sin posicion abierta")

    oo = c.open_orders(sym)
    print(f"ordenes abiertas: {len(oo)}")
    for o in oo[:5]:
        print(f"  {o['orderId']} {o['side']} {o['type']} {o['origQty']} @ {o.get('price')} "
              f"status={o['status']}")

    qty = c.round_qty(sym, a.notional / mid)
    try:
        plan = c.place_order(symbol=sym, side="BUY", qty=qty, order_type="MARKET")
        p = plan["would_send"]
        print(f"\norden en seco   : {p['side']} {p['type']} qty={p['quantity']} "
              f"~{plan['notional']:.2f} USDT  -> {plan['reason']}")
        print("  (no se ha enviado nada; queda en logs/orders.jsonl como not_sent)")
    except SafetyError as e:
        print(f"\norden en seco   : BLOQUEADA por seguridad -> {e}")

    print("\nOK: conexion y cuenta legibles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
