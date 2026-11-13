"""Prueba end-to-end de auto_router + reconcile contra el testnet.

Sustituye la senal del engine por una sintetica FRESCA en XRPUSDT (el engine
esta plano ahora mismo) para ejercitar el camino real: decision -> router ->
bracket -> estado -> stop desaparecido -> reconciliacion -> cierre detectado.

No valida estrategia; valida plumbing. El nocional se limita a 25 USDT.

    $env:ALLOW_ORDERS="1"; python execution/selftest_plumbing.py

Deja la cuenta plana al final (flatten). Pensado para re-ejecutar tras tocar
router/reconcile/estado, no para produccion.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

EXEC = str(Path(__file__).resolve().parent)
sys.path.insert(0, EXEC)

import auto_router  # noqa: E402
import flatten  # noqa: E402
import live_signal  # noqa: E402
import reconcile  # noqa: E402
import state_store  # noqa: E402
from binance_testnet import TestnetClient  # noqa: E402

SYM = "XRPUSDT"
NOTIONAL = 25.0
fails: list[str] = []


def check(cond: bool, what: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {what}")
    if not cond:
        fails.append(what)


def report(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> int:
    c = TestnetClient(dry_run=False, max_notional_usd=NOTIONAL)
    c.sync_time()
    # el intento anterior pudo dejar una intencion sin posicion
    from_state = state_store.load()
    if SYM in from_state and not [p for p in c.positions(SYM)
                                  if float(p.get("positionAmt", 0))]:
        state_store.remove(SYM)
        print(f"limpiado estado huerfano de {SYM}: {from_state[SYM].get('status')}")
    print(f"cuenta canTrade y ALLOW_ORDERS: orders_enabled={c.orders_enabled}")
    print(f"ALLOW_SYMBOLS={c.allow_symbols} max_notional={c.max_notional}")
    print(f"posicion previa {SYM}: {[p['positionAmt'] for p in c.positions(SYM)]}")
    print(f"algo previas: {len(c.open_algo_orders(SYM))}")

    cand = live_signal.candidates()[SYM]
    real_compute = live_signal.compute
    marks: dict[str, float] = {}

    def fake_compute(symbol: str, strategy: str = "S10", bars: int = 3000,
                     interval: str = "1h") -> dict:
        if symbol.upper() != SYM:
            return {"symbol": symbol.upper(), "direction": 0, "trusted": True,
                    "last_close": None, "bars_held": None}
        m = c.mark_price(SYM)
        marks["mark"] = m
        return {"symbol": SYM, "strategy": strategy, "direction": 1,
                "entry": m, "stop": m * 0.98, "sl_dist_last": m * 0.02,
                "sl_dist": m * 0.02, "sl_dist_bps": 200.0,
                "bars_held": 1, "trusted": True, "last_close": m,
                "entry_i": 2900, "open_pnl_pct": 0.0,
                "entry_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    live_signal.compute = fake_compute
    auto_router.live_signal.candidates = lambda: {SYM: cand}

    # ── 1. auto_router abre con bracket ──────────────────────────────
    report("1) auto_router --execute (senal sintetica fresca LONG)")
    rc = auto_router.run_cycle(True, 24, 5, NOTIONAL)
    check(rc == 0, f"run_cycle rc={rc}")

    st = state_store.load().get(SYM) or {}
    check(st.get("status") == "open", f"estado status={st.get('status')}")
    check(bool(st.get("stop_algo_id")), f"stop_algo_id={st.get('stop_algo_id')}")
    check(st.get("side") == "LONG", f"side={st.get('side')}")

    pos = [p for p in c.positions(SYM) if float(p.get("positionAmt", 0))]
    algo = c.open_algo_orders(SYM)
    check(len(pos) == 1, f"posicion viva {pos[0]['positionAmt'] if pos else 0}")
    check(any(str(a.get("orderType")) == "STOP_MARKET" for a in algo),
          f"{len(algo)} algo(s), hay STOP_MARKET")
    mark = c.mark_price(SYM)
    check(bool(st.get("stop")) and st["stop"] < mark, f"stop {st.get('stop')} < mark {mark}")
    trig1 = [float(a["triggerPrice"]) for a in algo
             if str(a.get("orderType")) == "STOP_MARKET"]
    check(bool(trig1) and abs(trig1[0] - float(st.get("stop"))) < 1e-12,
          f"estado {st.get('stop')} == trigger del exchange {trig1}")
    print(f"  detalle estado: {json.dumps(st, ensure_ascii=False)}")

    # ── 2. simular stop desaparecido ─────────────────────────────────
    report("2) borro el stop a proposito y reconcilio")
    for a in algo:
        c.cancel_algo(a["algoId"], SYM)
    time.sleep(1)
    check(not c.open_algo_orders(SYM), "sin algo en el exchange (desprotegida)")

    rc_plan = reconcile.run_cycle(False, False, SYM)
    check(rc_plan == 0, f"plan rc={rc_plan}")
    check(not c.open_algo_orders(SYM), "el plan NO envio nada")

    rc_exec = reconcile.run_cycle(True, False, SYM)
    check(rc_exec == 0, f"execute rc={rc_exec}")
    time.sleep(1)
    algo2 = c.open_algo_orders(SYM)
    stops2 = [a for a in algo2 if str(a.get("orderType")) == "STOP_MARKET"]
    check(len(stops2) == 1, f"stop recreado: {len(stops2)}")
    st2 = state_store.load().get(SYM) or {}
    if stops2:
        trig = float(stops2[0]["triggerPrice"])
        check(abs(trig - float(st2["stop"])) < 1e-12,
              f"trigger {trig} == estado {st2.get('stop')}")
    check(st2.get("status") == "open", f"estado tras reconciliar={st2.get('status')}")

    # ── 3. cerrar a mano y detectar el cierre ────────────────────────
    report("3) cierro la posicion sin tocar el estado y reconcilio")
    for a in c.open_algo_orders(SYM):
        c.cancel_algo(a["algoId"], SYM)
    amt = abs(float([p for p in c.positions(SYM)
                     if float(p.get("positionAmt", 0))][0]["positionAmt"]))
    r = c.close_position_market(SYM, "SELL", amt)
    print(f"  cierre manual {amt:g} -> {r.get('status')} exec={r.get('executedQty')}")
    time.sleep(1)
    check(not [p for p in c.positions(SYM) if float(p.get("positionAmt", 0))],
          "cuenta plana")

    rc_close = reconcile.run_cycle(True, False, SYM)
    check(rc_close == 0, f"reconcile cierre rc={rc_close}")
    check(SYM not in state_store.load(), "estado borrado tras detectar el cierre")

    # ── 4. huerfana: avisa y no toca ─────────────────────────────────
    report("4) posicion huerfana (sin estado)")
    r = c.place_order(symbol=SYM, side="BUY", qty=c.round_qty(SYM, 10.0),
                      order_type="MARKET", client_id="qae-harness-orphan")
    print(f"  abierta huerfana: {r.get('status')} {r.get('executedQty')}")
    time.sleep(1)
    rc_orphan = reconcile.run_cycle(False, False, None)
    check(rc_orphan == 0, f"reconcile huerfana rc={rc_orphan}")
    pos_h = [p for p in c.positions(SYM) if float(p.get("positionAmt", 0))]
    check(len(pos_h) == 1, "la huerfana sigue abierta (no se cerro sola)")

    # ── 4b. "entering" recien escrito: no se pisa ────────────────────
    report("4b) estado entering en vuelo")
    mark = c.mark_price(SYM)
    state_store.upsert(SYM, {"status": "entering", "side": "LONG",
                             "stop": mark * 0.98, "qty_planned": 10.0,
                             "opened_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                         time.gmtime())})
    rc_fly = reconcile.run_cycle(True, False, SYM)
    check(rc_fly == 0, f"rc={rc_fly}")
    check(len([p for p in c.positions(SYM) if float(p.get("positionAmt", 0))]) == 1,
          "la posicion en vuelo sigue abierta")
    check((state_store.load().get(SYM) or {}).get("status") == "entering",
          "estado intacto (no lo borro ni lo cerro)")

    report("limpieza")
    rc_flat = flatten.run(SYM, True)
    check(rc_flat == 0, f"flatten rc={rc_flat}")

    report("RESULTADO")
    if fails:
        print(f"{len(fails)} FALLOS:")
        for f in fails:
            print("  -", f)
        return 1
    print("TODO OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
