"""audit_sync.py — estado del bot vs exchange, y reparacion del estado.

QUE MIRA
========
Tres fuentes que deben coincidir:
  1. el exchange (posiciones, ordenes abiertas, ordenes algo/stops)
  2. state/grid_state.json — lo que el neutral_bot (grid) cree que tiene
  3. state/positions.json — estado del auto_router RETIRADO el 12-nov

Por que existe: el 16-nov el repo llevaba 99 commits sin subir, el bot corria
con un reescrito de neutral_bot.py sin commitear, positions.json describia un
XRP que ya no existia y el reconciliador —del auto_router— trataba las 8
posiciones del grid como huerfanas: `reconcile.py --execute --close-orphans`
cerraba el libro entero a mercado.

MODO LECTURA (por defecto)
==========================
No coloca, no cancela y no escribe estado.

REPARACION (`--repair`)
=======================
El 17-nov el estado tenia 28 slots y el exchange 64 ordenes vivas: 56 ordenes
que el estado no conocia. Arrancar el bot asi colocaba otra vez cada nivel
encima de la orden que ya estaba puesta (dos ordenes al mismo precio = doble
exposicion en ese nivel).

`--repair` NO toca el exchange: **adopta el libro como verdad** e infiere el
ancla que explica las ordenes vivas, reconstruye los slots y escribe
grid_state.json (con backup). Es la direccion correcta: el estado es una nota
derivada del exchange, no al reves.

Si alguna orden viva NO se puede explicar por la retícula del grid (otra
logica, otro espaciado, una orden manual), ese simbolo se RECHAZA y se
informa: no se inventa un slot para que cuadre.

Uso:
    python execution/audit_sync.py                  # solo lectura
    python execution/audit_sync.py --repair         # enseña lo que haria
    python execution/audit_sync.py --repair --apply # escribe el estado
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import binance_testnet as BT  # noqa: E402
import risk_guard  # noqa: E402

BT.load_env(HERE / ".env")

GRID_STATE = HERE / "state" / "grid_state.json"
ROUTER_STATE = HERE / "state" / "positions.json"

# La retícula que usa neutral_bot. Si cambia alli, cambia aqui: son las dos
# unicas piezas que tienen que estar de acuerdo en la forma del grid.
GRID_LEVELS = 4
GRID_SPACING_PCT = 0.005


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _fmt_orders(ids: list[int]) -> str:
    return ", ".join(str(i) for i in ids[:10]) + (" ..." if len(ids) > 10 else "")


# ─────────────────────────────────────────── retícula: ancla desde el libro ──
def _lattice(anchor: float, levels: int, spacing: float, rnd) -> dict:
    """{('BUY', i): precio} redondeado al tick del simbolo."""
    out = {}
    for i in range(1, levels + 1):
        for side, sign in (("BUY", -1), ("SELL", +1)):
            out[(side, i)] = rnd(anchor * (1 + sign * spacing * i))
    return out


def infer_anchor(orders: list[dict], levels: int, spacing: float,
                 tick: float, rnd) -> tuple[float | None, dict, list[int]]:
    """Ancla que explica TODAS las ordenes vivas. (None, {}, sobrantes) si no.

    Cada orden (lado, precio) implica un ancla por cada nivel posible:
        BUY  i ->  A = px / (1 - i*s)
        SELL i ->  A = px / (1 + i*s)
    El ancla buena es la que explica todas las ordenes con niveles DISTINTOS.
    Se exige que explique el 100%: media retícula no es un estado, es una
    adivinanza.
    """
    if not orders:
        return None, {}, []
    cands: list[float] = []
    for o in orders:
        try:
            px = float(o["price"])
        except (KeyError, TypeError, ValueError):
            continue
        side = str(o.get("side") or "").upper()
        for i in range(1, levels + 1):
            if side == "BUY":
                cands.append(px / (1 - spacing * i))
            elif side == "SELL":
                cands.append(px / (1 + spacing * i))
    if not cands:
        return None, {}, [int(o["orderId"]) for o in orders]

    tol = max(tick * 1.5, max(abs(c) for c in cands) * 1e-9)
    cands.sort()
    clusters: list[list[float]] = [[cands[0]]]
    for c in cands[1:]:
        if c - clusters[-1][-1] <= tol:
            clusters[-1].append(c)
        else:
            clusters.append([c])

    best: tuple[int, float, dict] | None = None
    for cl in clusters:
        anchor = sum(cl) / len(cl)
        lat = _lattice(anchor, levels, spacing, rnd)
        slots: dict[str, dict] = {}
        leftover: list[int] = []
        for o in orders:
            try:
                px = float(o["price"])
            except (KeyError, TypeError, ValueError):
                leftover.append(int(o.get("orderId") or 0))
                continue
            side = str(o.get("side") or "").upper()
            hit = None
            for (s, i), lpx in lat.items():
                if s == side and abs(lpx - px) <= tick * 1.5:
                    hit = (s, i)
                    break
            if hit is None:
                leftover.append(int(o.get("orderId") or 0))
                continue
            key = f"{hit[0]}{hit[1]}"
            if key in slots:
                leftover.append(int(o.get("orderId") or 0))
                continue
            slots[key] = {"level": hit[1], "price": lat[hit],
                          "qty": float(o.get("origQty") or 0),
                          "order_id": int(o.get("orderId") or 0)}
        score = len(slots)
        if best is None or score > best[0]:
            best = (score, anchor, slots)
    if best is None:
        return None, {}, [int(o["orderId"]) for o in orders]
    score, anchor, slots = best
    mapped = {s["order_id"] for s in slots.values()}
    leftover = [int(o["orderId"]) for o in orders if int(o["orderId"]) not in mapped]
    if leftover or not slots:
        return None, {}, leftover
    return anchor, slots, []


def repair(apply: bool) -> int:
    c = BT.TestnetClient(dry_run=True)   # dry_run: no puede enviar ordenes
    c.sync_time()
    state = _load(GRID_STATE)
    if not isinstance(state, dict) or "symbols" not in state:
        print("CRITICO: grid_state.json ilegible o sin 'symbols'. No se repara.")
        return 6
    symbols = list(state.get("symbols") or {})
    if not symbols:
        print("sin simbolos en el estado: nada que reparar")
        return 0

    changes: dict[str, dict] = {}
    refused: dict[str, str] = {}
    for sym in symbols:
        st = state["symbols"].get(sym) or {}
        try:
            orders = c.open_orders(sym)
        except (BT.BinanceError, BT.SafetyError) as e:
            refused[sym] = f"open_orders fallo: {e}"
            continue
        if not orders:
            continue
        r = c.rules(sym)
        tick = float(r.get("tick") or 0)
        anchor, slots, leftover = infer_anchor(
            orders, GRID_LEVELS, GRID_SPACING_PCT, tick,
            lambda px, _s=sym: c.round_price(_s, px))
        if anchor is None:
            refused[sym] = ("ordenes que la retícula no explica "
                            f"(ids {_fmt_orders(leftover)})")
            continue
        old_anchor = st.get("anchor")
        old_n = len(st.get("slots") or {})
        changes[sym] = {"old_anchor": old_anchor, "anchor": anchor,
                        "old_slots": old_n, "slots": slots,
                        "orders": len(orders)}

    print(f"{'simbolo':14s} {'ancla estado':>14s} {'ancla libro':>14s} "
          f"{'slots':>10s} {'ordenes':>8s}")
    for sym, ch in changes.items():
        print(f"{sym:14s} {str(ch['old_anchor'])[:14]:>14s} "
              f"{ch['anchor']:<14.8g} {ch['old_slots']:>4d} -> {len(ch['slots']):<2d} "
              f"{ch['orders']:>8d}")
    for sym, why in refused.items():
        print(f"{sym:14s} RECHAZADO: {why}")
    print(f"\nreparables={len(changes)} rechazados={len(refused)}")

    if not apply:
        print("\n(dry-run: nada escrito. --apply para persistir el estado)")
        return 0 if not refused else 1
    if refused:
        print("\nNO se escribe: hay simbolos rechazados. Un estado a medias "
              "es peor que un estado viejo. Resuelvelos a mano.")
        return 1

    backup = GRID_STATE.with_name(
        f"grid_state.json.pre-repair-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    backup.write_text(GRID_STATE.read_text(encoding="utf-8"), encoding="utf-8")
    for sym, ch in changes.items():
        st = state["symbols"].setdefault(sym, {})
        st["anchor"] = ch["anchor"]
        st["slots"] = ch["slots"]
    tmp = GRID_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(GRID_STATE)
    print(f"\nestado escrito. backup: {backup.name}")
    return 0


# ─────────────────────────────────────────────────────── auditoria (lectura) ─
def audit() -> int:
    c = BT.TestnetClient(dry_run=False)
    acc = c.account()
    print("=" * 74)
    print(f"EQUITY {acc.get('totalWalletBalance')} USD | margen {acc.get('totalMarginBalance')} | "
          f"libre {acc.get('availableBalance')} | uPnL {acc.get('totalUnrealizedProfit')}")

    # una sola fuente de exposicion: la misma que usa el bot y el router
    snap = risk_guard.exposure_snapshot(c)
    pos = {s: v for s, v in snap["positions"].items()}
    if pos:
        print("\n--- POSICIONES (risk_guard.exposure_snapshot) ---")
        for s in sorted(pos):
            v = pos[s]
            print(f"  {s:<14} {'LONG' if v['amt'] > 0 else 'SHORT':<5} "
                  f"amt={v['amt']:>12} mark={v['mark']:>12} "
                  f"notional={v['notional']:>9.2f} upl={v['upnl']:>8.2f}")
    print(f"\nexposicion bruta={snap['gross']:.2f} neta={snap['net']:+.2f} "
          f"verificable={snap['verifiable']} ok={snap['ok']}")
    print(f"  topes: {snap['caps']['per_symbol']:.0f}/simbolo "
          f"{snap['caps']['total']:.0f} total "
          f"(gross {snap['caps']['gross']:.0f} / net {snap['caps']['net']:.0f})")
    if snap["unpriced"]:
        print(f"  SIN PRECIO (exposicion DESCONOCIDA): {', '.join(snap['unpriced'])}")

    orders = c.open_orders()
    algos = c.open_algo_orders()
    by_sym: dict[str, list] = {}
    for o in orders:
        by_sym.setdefault(o["symbol"], []).append(o)

    gs = _load(GRID_STATE)
    state_ids: set[tuple[str, int]] = set()
    print("\n--- GRID_STATE vs EXCHANGE ---")
    for sym, st in (gs.get("symbols") or {}).items():
        registered = {int(r.get("order_id") or 0)
                      for r in (st.get("slots") or {}).values()}
        live = {int(o["orderId"]) for o in by_sym.get(sym, [])}
        for oid in sorted(registered):
            state_ids.add((sym, oid))
            if oid not in live:
                print(f"  SLOT NO VIVO: {sym} order_id={oid} "
                      f"(probable fill sin procesar u orden muerta)")
        orphans = sorted(live - registered)
        if orphans:
            print(f"  ORDENES SIN REGISTRO: {sym} {len(orphans)} -> "
                  f"{_fmt_orders(orphans)}")
    print(f"  slots en estado={len(state_ids)}  ordenes en exchange={len(orders)}")

    ps = _load(ROUTER_STATE)
    print("\n--- POSITIONS.JSON (auto_router retirado) ---")
    if not ps:
        print("  vacio (correcto: el auto_router fue retirado el 12-nov)")
    for sym, rec in ps.items():
        exch = pos.get(sym)
        print(f"  {sym}: side={rec.get('side')} qty={rec.get('qty')} "
              f"stop_algo_id={rec.get('stop_algo_id')} updated={rec.get('updated_utc')}")
        print("    -> " + ("SIN POSICION en el exchange: FANTASMA"
                           if exch is None else
                           f"exchange amt={exch['amt']}"))
        sid = rec.get("stop_algo_id")
        if sid:
            alive = [a for a in algos if str(a.get("algoId")) == str(sid)]
            print(f"    -> stop_algo_id={sid} {'VIVO' if alive else 'MUERTO'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repair", action="store_true",
                    help="reconstruye grid_state.json desde el libro (no toca el exchange)")
    ap.add_argument("--apply", action="store_true",
                    help="con --repair: escribe el estado (si no, dry-run)")
    a = ap.parse_args(argv)
    if a.repair:
        return repair(a.apply)
    return audit()


if __name__ == "__main__":
    raise SystemExit(main())
