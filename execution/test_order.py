"""Prueba de plumbing contra el testnet. Dos modos, uno de ellos toca dinero ficticio.

    python execution/test_order.py --mode limit    # recomendado: coloca y cancela
    python execution/test_order.py --mode market   # compra y cierra (roundtrip real)

`limit` coloca un BUY LIMIT post-only (GTX) un 3% por debajo del best bid, lo
lee de vuelta, lo cancela y confirma CANCELED. No deberia llenarse nunca: si el
precio cae un 3% en los segundos que dura, el script lo detecta y cancela igual.

`market` abre 20 USDT a mercado y cierra inmediatamente con reduceOnly, para
probar fills, posiciones y comisiones. Cuesta el spread + fees del testnet.

Guardas:
  * exige ALLOW_ORDERS=1 y dry_run=False (dos llaves, igual que place_order)
  * no toca el simbolo si ya tiene posicion u ordenes abiertas
  * no toca otros simbolos
  * tope de nocional del cliente
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from binance_testnet import BinanceError, SafetyError, TestnetClient  # noqa: E402


def state(c: TestnetClient, sym: str) -> tuple[list[dict], list[dict]]:
    pos = [p for p in c.positions(sym) if float(p.get("positionAmt", 0)) != 0]
    return pos, c.open_orders(sym)


def show(sym: str, pos: list[dict], oo: list[dict]) -> None:
    if pos:
        for p in pos:
            print(f"  posicion {sym}: amt={p['positionAmt']} entry={p['entryPrice']} "
                  f"upnl={float(p['unRealizedProfit']):+.2f}")
    else:
        print(f"  posicion {sym}: ninguna")
    print(f"  ordenes abiertas {sym}: {len(oo)}")
    for o in oo:
        print(f"    {o['orderId']} {o['side']} {o['type']} {o['origQty']} @ {o.get('price')} "
              f"{o['status']}")


def fees_from_trades(c: TestnetClient, sym: str, order_id: int) -> list[dict]:
    try:
        return c.user_trades(sym, order_id=order_id)
    except BinanceError as e:
        print(f"  (userTrades no disponible: {e})")
        return []


def avg_from_trades(trades: list[dict]) -> float:
    q = sum(float(t["qty"]) for t in trades)
    return sum(float(t["price"]) * float(t["qty"]) for t in trades) / q if q else 0.0


def print_trades(trades: list[dict], mid: float) -> float:
    total_fee = 0.0
    for t in trades:
        px, qty = float(t["price"]), float(t["qty"])
        fee = float(t["commission"])
        total_fee += fee
        bps = (px - mid) / mid * 1e4
        print(f"    trade {t['id']}: {'BUY ' if t['side'] == 'BUY' else 'SELL'} "
              f"{qty:g} @ {px:g}  {bps:+6.2f} bps vs mid  comision {fee:.6f} {t['commissionAsset']}")
    return total_fee


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--mode", choices=("limit", "market"), default="limit")
    ap.add_argument("--notional", type=float, default=20.0)
    ap.add_argument("--offset-pct", type=float, default=3.0,
                    help="para --mode limit: distancia al best bid en %%")
    a = ap.parse_args()

    c = TestnetClient(dry_run=False)
    sym = (a.symbol or c.allow_symbols[0]).upper()
    if not c.orders_enabled:
        print(f"BLOQUEADO: ALLOW_ORDERS={1 if c.orders_enabled else 0}; hace falta ALLOW_ORDERS=1")
        return 2
    if not c.has_keys():
        print("BLOQUEADO: faltan claves en el .env")
        return 2

    off = c.sync_time()
    d = c.depth(sym, 5)
    bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
    mid = (bid + ask) / 2
    r = c.rules(sym)
    print(f"{sym}  bid {bid} / ask {ask}  spread {(ask-bid)/mid*1e4:.2f} bps  offset {off:+d} ms")
    print(f"modo {a.mode}  nocional objetivo {a.notional:.2f} USDT  (max {c.max_notional:g})")
    try:
        cr = c.commission_rate(sym)
        print(f"comision del exchange: maker {float(cr['makerCommissionRate'])*1e4:.2f} bps / "
              f"taker {float(cr['takerCommissionRate'])*1e4:.2f} bps")
    except (BinanceError, KeyError) as e:
        print(f"comision del exchange: no disponible ({e})")

    pos, oo = state(c, sym)
    print("\nANTES:")
    show(sym, pos, oo)
    if pos or oo:
        print("\nABORTADO: el simbolo ya tiene posicion u ordenes. No mezclo estado ajeno.")
        return 3

    if a.mode == "limit":
        px = c.round_price(sym, bid * (1 - a.offset_pct / 100))
        qty = c.round_qty(sym, a.notional / px)
        print(f"\n1) BUY LIMIT GTX {qty:g} @ {px} (~{qty*px:.2f} USDT, {a.offset_pct:g}% bajo el bid)")
        resp = c.place_order(symbol=sym, side="BUY", qty=qty, order_type="LIMIT",
                             price=px, time_in_force="GTX")
        oid = resp.get("orderId")
        print(f"   orderId={oid} status={resp.get('status')} clientId={resp.get('clientOrderId')}")
        if not oid:
            print("   RESPUESTA SIN orderId:", resp)
            return 4

        q = c.query_order(sym, order_id=oid)
        print(f"2) leida de vuelta: status={q['status']} qty={q['origQty']} price={q['price']} "
              f"executed={q['executedQty']}")
        if q["status"] not in ("NEW", "PARTIALLY_FILLED"):
            print("   no quedo viva; nada que cancelar")
        else:
            if float(q["executedQty"]) > 0:
                print(f"   AVISO: se lleno parcialmente {q['executedQty']} antes de cancelar")

        can = c.cancel(sym, order_id=oid)
        print(f"3) cancelada: status={can.get('status')} executedQty={can.get('executedQty')}")
        q2 = c.query_order(sym, order_id=oid)
        print(f"4) estado final: {q2['status']}")

        pos2, oo2 = state(c, sym)
        print("\nDESPUES:")
        show(sym, pos2, oo2)
        filled = float(q2.get("executedQty", 0))
        ok = q2["status"] == "CANCELED" and not pos2 and not oo2
        print("\nOK: firma, filtros, colocacion, lectura y cancelacion." if ok
              else f"\nREVISAR: status={q2['status']} filled={filled} pos={len(pos2)} ordenes={len(oo2)}")
        return 0 if ok else 5

    # ── market roundtrip ───────────────────────────────────────────────
    qty = c.round_qty(sym, a.notional / ask)
    print(f"\n1) BUY MARKET {qty:g} (~{qty*ask:.2f} USDT)")
    b = c.place_order(symbol=sym, side="BUY", qty=qty, order_type="MARKET")
    bq = float(b.get("executedQty", 0))
    print(f"   orderId={b.get('orderId')} status={b.get('status')} exec={bq:g}")
    print("   comisiones de la entrada:")
    bt = fees_from_trades(c, sym, b["orderId"])
    f1 = print_trades(bt, mid)
    # El testnet devuelve avgPrice=0 en los MARKET: se reconstruye de los fills.
    bp = avg_from_trades(bt) or float(b.get("avgPrice", 0) or 0)
    print(f"   precio medio real: {bp:g}  ({(bp-mid)/mid*1e4:+.2f} bps vs mid)")
    if bq <= 0:
        print("   no se ejecuto nada:", b)
        return 6

    pos, oo = state(c, sym)
    print("\nEN POSICION:")
    show(sym, pos, oo)

    print(f"\n2) SELL MARKET reduceOnly {bq:g}")
    s = c.place_order(symbol=sym, side="SELL", qty=bq, order_type="MARKET", reduce_only=True)
    sq = float(s.get("executedQty", 0))
    print(f"   orderId={s.get('orderId')} status={s.get('status')} exec={sq:g}")
    print("   comisiones de la salida:")
    st = fees_from_trades(c, sym, s["orderId"])
    f2 = print_trades(st, mid)
    sp = avg_from_trades(st) or float(s.get("avgPrice", 0) or 0)
    print(f"   precio medio real: {sp:g}  ({(sp-mid)/mid*1e4:+.2f} bps vs mid)")

    pos2, oo2 = state(c, sym)
    print("\nDESPUES:")
    show(sym, pos2, oo2)

    if bp and sp:
        pnl = (sp - bp) * min(bq, sq)
        print(f"\nroundtrip: entrada {bp:g} -> salida {sp:g}  pnl bruto {pnl:+.6f} USDT  "
              f"fees {f1 + f2:.6f}  neto {pnl - f1 - f2:+.6f}")
        print(f"coste medido: entrada {(bp-mid)/mid*1e4:+.2f} bps, salida {(sp-mid)/mid*1e4:+.2f} bps "
              f"(contra el mid de antes de entrar)")
    ok = not pos2 and not oo2 and abs(sq - bq) < 1e-9
    print("\nOK: fill, posicion y cierre reduceOnly." if ok else "\nREVISAR: quedo estado abierto.")
    return 0 if ok else 7


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafetyError as e:
        print(f"BLOQUEADO por seguridad: {e}")
        raise SystemExit(8)
    except BinanceError as e:
        print(f"BINANCE: {e}")
        raise SystemExit(9)
