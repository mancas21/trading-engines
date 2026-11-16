"""audit_sync.py — el bot contra el exchange, contra su estado y contra el
estado del auto_router retirado.

QUE MIRA
========
Tres fuentes que tienen que coincidir:
  1. el exchange (posiciones, ordenes abiertas, ordenes algo/stops)
  2. state/grid_state.json — lo que el neutral_bot (grid) cree que tiene
  3. state/positions.json — estado del auto_router RETIRADO el 12-nov

Por que existe: el 16-nov el repo llevaba 99 commits sin subir, el bot
corria con un reescrito de neutral_bot.py sin commitear, positions.json
describia un XRP que ya no existia (el auto_router lo abrio el 5-nov y
murio el 12-nov) y el reconciliador —que es del auto_router— trataba
las 8 posiciones del GRID como huerfanas: `reconcile.py --execute
--close-orphans` cerraba el libro entero a mercado. Este script deja
esa clase de desajuste a la vista sin tocar nada.

SOLO LECTURA: no coloca, no cancela y no escribe estado.

Uso:
    python execution/audit_sync.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import binance_testnet as BT  # noqa: E402

BT.load_env(HERE / ".env")

GRID_STATE = HERE / "state" / "grid_state.json"
ROUTER_STATE = HERE / "state" / "positions.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> int:
    c = BT.TestnetClient(dry_run=False)
    acc = c.account()
    print("=" * 74)
    print(f"EQUITY {acc.get('totalWalletBalance')} USD | margen {acc.get('totalMarginBalance')} | "
          f"libre {acc.get('availableBalance')} | uPnL {acc.get('totalUnrealizedProfit')}")

    pos = {p["symbol"]: p for p in c.positions()
           if float(p.get("positionAmt", 0) or 0) != 0}
    orders = c.open_orders()
    algos = c.open_algo_orders()
    by_sym: dict[str, list] = {}
    for o in orders:
        by_sym.setdefault(o["symbol"], []).append(o)

    print("\n--- POSICIONES ---")
    gross = 0.0
    for s, p in sorted(pos.items()):
        amt = float(p["positionAmt"])
        mark = float(p.get("markPrice") or 0)
        upl = float(p.get("unRealizedProfit") or 0)
        notional = abs(amt) * mark
        gross += notional
        print(f"  {s:<14} {'LONG' if amt > 0 else 'SHORT':<5} amt={amt:>12} "
              f"entry={float(p['entryPrice']):>12} mark={mark:>12} "
              f"notional={notional:>9.2f} upl={upl:>8.2f} lev={p.get('leverage')}x")

    print(f"\n--- ORDENES ABIERTAS: {len(orders)} | ALGO (stops/TP): {len(algos)} ---")
    for s, lst in sorted(by_sym.items()):
        sides: dict[str, int] = {}
        for o in lst:
            sides[o["side"]] = sides.get(o["side"], 0) + 1
        print(f"  {s:<14} {len(lst):>2}  {sides}")

    # ── grid_state vs exchange ────────────────────────────────────────────
    gs = _load(GRID_STATE)
    state_ids: set[tuple[str, int]] = set()
    print("\n--- GRID_STATE vs EXCHANGE ---")
    for sym, st in (gs.get("symbols") or {}).items():
        for slot, sl in (st.get("slots") or {}).items():
            oid = int(sl["order_id"])
            state_ids.add((sym, oid))
            if not any(int(o["orderId"]) == oid for o in by_sym.get(sym, [])):
                print(f"  SLOT HUERFANO: {sym} {slot} order_id={oid} "
                      f"price={sl['price']} qty={sl['qty']}")
        for o in by_sym.get(sym, []):
            if (sym, int(o["orderId"])) not in state_ids:
                print(f"  ORDEN SIN REGISTRO: {sym} orderId={o['orderId']} "
                      f"side={o['side']} {o.get('type')} price={o.get('price')} "
                      f"qty={o.get('origQty')}")
    print(f"  slots en estado={len(state_ids)}  ordenes en exchange={len(orders)}")

    # ── positions.json (auto_router retirado) ─────────────────────────────
    ps = _load(ROUTER_STATE)
    print("\n--- POSITIONS.JSON (auto_router retirado) ---")
    if not ps:
        print("  vacio (correcto: el auto_router fue retirado el 12-nov)")
    for sym, rec in ps.items():
        exch = pos.get(sym)
        print(f"  {sym}: side={rec.get('side')} qty={rec.get('qty')} entry={rec.get('entry')} "
              f"stop={rec.get('stop')} stop_algo_id={rec.get('stop_algo_id')} "
              f"updated={rec.get('updated_utc')}")
        if exch is None:
            print("    -> NO HAY POSICION de este simbolo en el exchange: FANTASMA")
        else:
            amt = float(exch["positionAmt"])
            same = abs(abs(amt) - float(rec.get("qty") or 0)) < 1e-6
            print(f"    -> exchange: amt={amt} entry={exch['entryPrice']} "
                  f"({'coincide' if same else 'DIFIERE'})")
        sid = rec.get("stop_algo_id")
        if sid:
            alive = [a for a in algos if str(a.get("algoId")) == str(sid)]
            print(f"    -> stop_algo_id={sid} "
                  f"{'VIVO' if alive else 'MUERTO (no esta en las ordenes algo)'}")

    print("\n--- INVENTARIO vs TOPES DEL BOT ---")
    print(f"  inventario bruto: {gross:.2f} USD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
