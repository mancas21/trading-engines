"""Router: de una senal a una posicion protegida en el exchange.

    # plan (no envia nada)
    python execution/router.py --symbol XRPUSDT --side LONG --stop 1.2760

    # ejecutar (ademas exige ALLOW_ORDERS=1)
    $env:ALLOW_ORDERS="1"
    python execution/router.py --symbol XRPUSDT --side LONG --stop 1.2760 --execute

    # o desde un fichero
    python execution/router.py --signal senal.json --execute

Esquema de la senal (json):
    {"symbol": "XRPUSDT", "side": "LONG", "stop": 1.2760,
     "take_profit": 1.3350, "candidate_id": "S10", "run_id": "..."}

DECISIONES DEL OPERADOR (2026-10-25)
====================================
1. UNA posicion por simbolo. Si el simbolo ya tiene posicion u ordenes vivas,
   el router aborta: no promedia, no suma, no reemplaza.
2. Tamano por RIESGO: risk_pct (0.5% por defecto) del equity hasta el stop, con
   tope de nocional (MAX_ORDER_NOTIONAL_USD) y tope de margen inicial. Se queda
   con el minimo de los tres y redondea hacia abajo al stepSize. Si el
   redondeo deja el riesgo por debajo del 10% del objetivo, avisa.
3. STOP EN EL EXCHANGE: STOP_MARKET con closePosition=true y workingType
   MARK_PRICE. Si el motor se cae, el stop sigue vivo. Si la colocacion del
   stop falla, el router CIERRA la posicion a mercado en el acto y lo registra.

El stop se valida contra la banda PERCENT_PRICE del simbolo ANTES de entrar:
si el stop no se puede representar (p.ej. XRP solo admite +-5% del mark), no se
entra. Nunca se abre una posicion que no se pueda proteger.

Coste asumido: taker 5 bps por lado (modelo del repo, 11 bps roundtrip con
spread). En testnet el exchange cobra 4 bps: los numeros de coste que imprime
este script son los de PRODUCCION, no los del testnet.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from binance_testnet import (BinanceError, SafetyError, TestnetClient,  # noqa: E402
                             fmt, mask)
import state_store  # noqa: E402

SCANNER = HERE.parent / "scanner-engine"

RISK_PCT_DEFAULT = 0.5
RISK_PCT_MAX = 1.0
LEVERAGE_DEFAULT = 5
LEVERAGE_MAX = 20
MARGIN_PCT_DEFAULT = 25.0
PROD_TAKER_BPS = 5.0


def load_signal(path: str | None, args) -> dict:
    if path:
        j = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        j = {"symbol": args.symbol, "side": args.side, "stop": args.stop,
             "take_profit": args.take_profit, "candidate_id": args.candidate_id,
             "run_id": args.run_id}
    for k in ("symbol", "side", "stop"):
        if j.get(k) in (None, ""):
            raise SafetyError(f"senal incompleta: falta {k}")
    j["side"] = str(j["side"]).upper()
    if j["side"] in ("BUY", "SELL"):
        j["side"] = "LONG" if j["side"] == "BUY" else "SHORT"
    if j["side"] not in ("LONG", "SHORT"):
        raise SafetyError(f"side invalido: {j['side']}")
    j["symbol"] = str(j["symbol"]).upper()
    j["stop"] = float(j["stop"])
    if j.get("take_profit") is not None:
        j["take_profit"] = float(j["take_profit"])
    return j


def gate_context(symbol: str) -> dict | None:
    """Candidatos admitidos del backtest-system para este simbolo (solo lectura)."""
    if not SCANNER.is_dir():
        return None
    if str(SCANNER) not in sys.path:
        sys.path.insert(0, str(SCANNER))
    try:
        import backtest_link  # noqa: PLC0415

        pairs = {v: k for k, v in backtest_link._PAIR_ALIAS.items()}
        return backtest_link.for_pair(pairs.get(symbol, symbol))
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "has_candidate": None}


def band_check(r: dict, mark: float, stop: float, side: str) -> tuple[bool, str]:
    down, up = r["percent_price"]["down"], r["percent_price"]["up"]
    if not (down and up):
        return True, "sin PERCENT_PRICE"
    lo, hi = mark * down, mark * up
    if not (lo <= stop <= hi):
        return False, (f"stop {stop:g} fuera de la banda del simbolo "
                       f"[{lo:.6g}, {hi:.6g}] (mark {mark:.6g}, "
                       f"-{(1-down)*100:.1f}%/+{(up-1)*100:.1f}%)")
    if side == "LONG" and stop >= mark:
        return False, f"LONG con stop {stop:g} >= mark {mark:g}"
    if side == "SHORT" and stop <= mark:
        return False, f"SHORT con stop {stop:g} <= mark {mark:g}"
    return True, "ok"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--signal", help="json con symbol/side/stop/take_profit")
    ap.add_argument("--symbol")
    ap.add_argument("--side", choices=("LONG", "SHORT", "long", "short", "BUY", "SELL"))
    ap.add_argument("--stop", type=float)
    ap.add_argument("--take-profit", dest="take_profit", type=float)
    ap.add_argument("--candidate-id")
    ap.add_argument("--run-id")
    ap.add_argument("--strategy", default=None, help="id de estrategia (S10, S02...)")
    ap.add_argument("--risk-pct", type=float, default=RISK_PCT_DEFAULT)
    ap.add_argument("--leverage", type=int, default=LEVERAGE_DEFAULT)
    ap.add_argument("--max-notional", type=float, default=None,
                    help="tope de nocional (por defecto MAX_ORDER_NOTIONAL_USD del .env)")
    ap.add_argument("--margin-pct", type=float, default=MARGIN_PCT_DEFAULT,
                    help="%% del balance disponible que puede usar de margen inicial")
    ap.add_argument("--require-candidate", action="store_true",
                    help="aborta si el backtest-system no tiene candidato admitido")
    ap.add_argument("--execute", action="store_true",
                    help="enviar de verdad (ademas exige ALLOW_ORDERS=1)")
    return ap


def main() -> int:
    a = build_parser().parse_args()
    return run(a)


def run(a: argparse.Namespace) -> int:
    sig = load_signal(a.signal, a)
    sym = sig["symbol"]
    side = sig["side"]

    if a.risk_pct <= 0 or a.risk_pct > RISK_PCT_MAX:
        raise SafetyError(f"risk_pct {a.risk_pct} fuera de (0, {RISK_PCT_MAX}]")
    if not 1 <= a.leverage <= LEVERAGE_MAX:
        raise SafetyError(f"leverage {a.leverage} fuera de 1..{LEVERAGE_MAX}")
    if not 0 < a.margin_pct <= 100:
        raise SafetyError(f"margin_pct {a.margin_pct} fuera de (0, 100]")

    c = TestnetClient(dry_run=not a.execute,
                      max_notional_usd=a.max_notional)
    off = c.sync_time()
    r = c.rules(sym)
    mark = c.mark_price(sym)
    d = c.depth(sym, 5)
    bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
    spread_bps = (ask - bid) / mark * 1e4

    print(f"host            : {c.base}")
    print(f"senal           : {sym} {side} stop {sig['stop']:g}"
          + (f" tp {sig['take_profit']:g}" if sig.get("take_profit") else ""))
    if sig.get("candidate_id") or sig.get("run_id"):
        print(f"origen          : candidate={sig.get('candidate_id')} run={sig.get('run_id')}")
    print(f"mercado         : bid {bid:g} / ask {ask:g}  mark {mark:g}  "
          f"spread {spread_bps:.2f} bps  offset {off:+d} ms")
    print(f"reglas          : tick {r['tick']:g} step {r['step']:g} "
          f"minQty {r['min_qty']:g} minNotional {r['min_notional']:g} "
          f"banda [{r['percent_price']['down']:g}, {r['percent_price']['up']:g}]x")
    print(f"claves          : key {mask(c.key)}  secret {mask(c.secret)}")

    ok, why = band_check(r, mark, sig["stop"], side)
    if not ok:
        print(f"\nBLOQUEADO: {why}")
        print("No se abre una posicion que no se pueda proteger.")
        return 4

    if sig.get("take_profit"):
        tp_ok, tp_why = band_check(r, mark, sig["take_profit"], "SHORT" if side == "LONG" else "LONG")
        if not tp_ok:
            print(f"\nAVISO tp: {tp_why} -> se ignora el TP")

    if a.require_candidate:
        ctx = gate_context(sym)
        if ctx is None:
            print("\nBLOQUEADO: no puedo leer el backtest-system para verificar el candidato")
            return 2
        if not ctx.get("has_candidate"):
            print(f"\nBLOQUEADO: sin candidato admitido para {sym}: {ctx}")
            return 2
        print(f"\ncandidato       : {len(ctx['candidates'])} admitido(s), "
              f"mejor PF {max(float(x['pf']) for x in ctx['candidates']):.2f}")

    if not c.has_keys():
        print("\nBLOQUEADO: faltan claves en el .env")
        return 2

    acc = c.account()
    equity = float(acc.get("totalWalletBalance") or acc.get("totalMarginBalance") or 0)
    available = float(acc.get("availableBalance") or 0)
    if c.dual_side():
        print("\nBLOQUEADO: cuenta en hedge mode; closePosition necesita one-way")
        return 3

    pos = [p for p in c.positions(sym) if float(p.get("positionAmt", 0)) != 0]
    oo = c.open_orders(sym)
    print(f"\ncuenta          : equity {fmt(equity)}  disponible {fmt(available)}")
    if pos:
        for p in pos:
            print(f"  posicion VIVA {sym}: amt={p['positionAmt']} entry={p['entryPrice']} "
                  f"upnl={float(p['unRealizedProfit']):+.2f}")
    if oo:
        for o in oo:
            print(f"  orden VIVA {sym}: {o['orderId']} {o['side']} {o['type']} "
                  f"{o['origQty']} @ {o.get('price')} stop={o.get('stopPrice')}")
    if pos or oo:
        print("\nBLOQUEADO: una posicion por simbolo. No promedio ni reemplazo.")
        return 3

    # ── tamano ────────────────────────────────────────────────────────
    dist = abs(mark - sig["stop"])
    risk_usd = equity * a.risk_pct / 100.0
    qty_risk = risk_usd / dist
    qty_notional = c.max_notional / mark
    qty_margin = (available * a.margin_pct / 100.0) * a.leverage / mark
    binding, raw = min((("riesgo", qty_risk), ("nocional", qty_notional),
                        ("margen", qty_margin)), key=lambda kv: kv[1])
    qty = c.round_qty(sym, raw)
    if qty < r["min_qty"]:
        print(f"\nBLOQUEADO: qty {qty:g} < minQty {r['min_qty']:g} ({binding} manda)")
        return 4
    notional = qty * mark
    if notional < r["min_notional"]:
        print(f"\nBLOQUEADO: nocional {notional:.2f} < minNotional {r['min_notional']:g}")
        return 4

    riesgo_real = qty * dist
    riesgo_pct = riesgo_real / equity * 100 if equity else 0
    margen = notional / a.leverage
    stop_bps = dist / mark * 1e4
    entry_fee = notional * PROD_TAKER_BPS / 1e4
    exit_fee = notional * PROD_TAKER_BPS / 1e4

    print(f"\nPLAN")
    print(f"  entrada         : MARKET {('BUY' if side == 'LONG' else 'SELL')} {qty:g} {sym} "
          f"~{notional:.2f} USDT")
    print(f"  stop            : {sig['stop']:g}  ({stop_bps:.0f} bps, "
          f"{'<' if side == 'LONG' else '>'} mark)")
    print(f"  riesgo          : {fmt(riesgo_real)} USDT = {riesgo_pct:.2f}% del equity "
          f"(objetivo {a.risk_pct:g}%)")
    print(f"  margen          : {fmt(margen)} USDT a {a.leverage}x "
          f"({margen/available*100 if available else 0:.1f}% del disponible)")
    print(f"  restriccion     : {binding}")
    if sig.get("take_profit"):
        rr = abs(sig["take_profit"] - mark) / dist
        print(f"  take profit     : {sig['take_profit']:g}  R:R {rr:.2f}")
    print(f"  coste ida+ida   : {PROD_TAKER_BPS*2:.0f} bps fees + {spread_bps:.2f} bps spread "
          f"= {PROD_TAKER_BPS*2+spread_bps:.2f} bps (produccion; testnet cobra 4 pb/lado)")
    print(f"  fees estimados  : entrada {entry_fee:.4f} + salida {exit_fee:.4f} USDT")

    if riesgo_real < risk_usd * 0.10:
        print(f"  AVISO: el redondeo/tope deja el riesgo en {riesgo_pct:.2f}%, "
              f"muy por debajo del objetivo {a.risk_pct:g}%")

    if not a.execute:
        print("\nDRY-RUN: no se ha enviado nada. Repite con --execute y ALLOW_ORDERS=1.")
        return 0

    if not c.orders_enabled:
        print("\nBLOQUEADO: --execute sin ALLOW_ORDERS=1")
        return 2

    # ── ejecucion ─────────────────────────────────────────────────────
    # Intencion ANTES de enviar: si el proceso muere entre la entrada y el
    # stop, el reconciliador sabe que stop corresponde. El sello de tiempo se
    # conserva hasta el estado final: marca el inicio de la posicion, y el
    # reconciliador lo usa para reconstruir TODOS los trades (entrada incluida).
    opened_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state_store.upsert(sym, {
        "status": "entering", "side": side, "stop": sig["stop"],
        "take_profit": sig.get("take_profit"), "strategy": a.strategy,
        "candidate_id": sig.get("candidate_id"), "run_id": sig.get("run_id"),
        "qty_planned": qty, "risk_usdt": round(riesgo_real, 6),
        "opened_utc": opened_utc,
    })
    try:
        lev = c.set_leverage(sym, a.leverage)
        print(f"\n1) leverage {a.leverage}x  {lev}")
    except (BinanceError, SafetyError) as e:
        print(f"\nBLOQUEADO: no pude fijar el leverage ({e}).")
        state_store.remove(sym)
        return 7
    entry_side = "BUY" if side == "LONG" else "SELL"
    t0 = time.time()
    try:
        resp = c.place_order(symbol=sym, side=entry_side, qty=qty,
                             order_type="MARKET", client_id=f"qae-ent-{int(t0*1000)}")
    except (BinanceError, SafetyError) as e:
        # No llego a haber posicion: la intencion no debe quedarse en el estado.
        print(f"\nBLOQUEADO: la entrada fue rechazada ({e}). No hay posicion.")
        state_store.remove(sym)
        return 7
    oid = resp.get("orderId")
    print(f"2) entrada orderId={oid} status={resp.get('status')} exec={resp.get('executedQty')}")

    fills = c.user_trades(sym, order_id=oid) if oid else []
    filled = sum(float(t["qty"]) for t in fills) if fills else float(resp.get("executedQty", 0) or 0)
    avg = (sum(float(t["price"]) * float(t["qty"]) for t in fills) / filled) if fills else mark
    print(f"   fill {filled:g} @ {avg:g}  (slippage {(avg-mark)/mark*1e4:+.2f} bps vs mark)")

    if filled <= 0:
        print("\nCRITICO: la entrada no lleno nada; no hay posicion que proteger.")
        state_store.remove(sym)
        return 7

    # ── proteccion ────────────────────────────────────────────────────
    close_side = "SELL" if side == "LONG" else "BUY"
    stop_px = c.round_price(sym, sig["stop"])
    try:
        sresp = c.protective_order(kind="STOP_MARKET", symbol=sym, side=close_side,
                                   stop_price=stop_px, close_position=True)
        sid = sresp.get("algoId") or sresp.get("orderId")
        # el exchange manda: se guarda el trigger REAL, no el ideal
        stop_px = float(sresp.get("triggerPrice") or stop_px)
        print(f"3) STOP_MARKET {close_side} trigger={stop_px:g} closePosition=true "
              f"algoId={sid} status={sresp.get('algoStatus') or sresp.get('status')}")
    except (BinanceError, SafetyError) as e:
        print(f"\nCRITICO: no pude colocar el stop ({e}). Cierro a mercado AHORA.")
        try:
            cerr = c.close_position_market(sym, close_side, filled)
            print(f"   cierre de emergencia: orderId={cerr.get('orderId')} "
                  f"status={cerr.get('status')} exec={cerr.get('executedQty')}")
            state_store.remove(sym)
        except (BinanceError, SafetyError) as e2:
            print(f"   FALLO TAMBIEN EL CIERRE: {e2}")
            print("   >>> POSICION DESCUBIERTA: cierrala a mano en el testnet. <<<")
            state_store.patch(sym, status="unprotected", stop_algo_id=None)
            return 6
        return 5

    # ── take profit (opcional) ────────────────────────────────────────
    tid = None
    tp = sig.get("take_profit")
    if tp:
        try:
            tresp = c.protective_order(kind="TAKE_PROFIT_MARKET", symbol=sym,
                                       side=close_side, stop_price=tp,
                                       close_position=True)
            tid = tresp.get("algoId") or tresp.get("orderId")
            print(f"4) TAKE_PROFIT_MARKET {close_side} trigger={tp:g} algoId={tid} "
                  f"status={tresp.get('algoStatus') or tresp.get('status')}")
        except (BinanceError, SafetyError) as e:
            print(f"4) AVISO: TP no colocado ({e}); el stop sigue vivo")

    # ── verificacion ──────────────────────────────────────────────────
    time.sleep(1)
    pos = [p for p in c.positions(sym) if float(p.get("positionAmt", 0)) != 0]
    oo = c.open_orders(sym)
    algo = c.open_algo_orders(sym)
    stops = [o for o in algo if o.get("orderType") == "STOP_MARKET"]
    tps = [o for o in algo if o.get("orderType") == "TAKE_PROFIT_MARKET"]
    print(f"\nverificacion    : posicion={'si' if pos else 'NO'} "
          f"({pos[0]['positionAmt'] if pos else 0})  "
          f"ordenes={len(oo)}  algo={len(algo)}  stops={len(stops)}  tps={len(tps)}")

    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "symbol": sym, "side": side, "qty": filled, "avg": avg,
           "mark": mark, "stop": stop_px, "tp": tp,
           "entry_order": oid, "stop_order": sid, "tp_order": tid,
           "risk_usdt": round(riesgo_real, 6), "risk_pct": round(riesgo_pct, 4),
           "equity": equity, "leverage": a.leverage, "binding": binding,
           "candidate_id": sig.get("candidate_id"), "run_id": sig.get("run_id"),
           "protected": bool(stops), "host": c.base}
    try:
        (HERE / "logs").mkdir(exist_ok=True)
        with (HERE / "logs" / "router.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass

    if not stops:
        print("\nCRITICO: hay posicion pero no veo el stop. Revisa la cuenta AHORA.")
        state_store.patch(sym, status="unprotected", stop_algo_id=None)
        return 6

    state_store.upsert(sym, {
        "status": "open", "side": side, "qty": filled, "entry": avg,
        "stop": stop_px, "take_profit": tp, "strategy": a.strategy,
        "candidate_id": sig.get("candidate_id"), "run_id": sig.get("run_id"),
        "stop_algo_id": sid, "tp_algo_id": tid, "leverage": a.leverage,
        "risk_usdt": round(riesgo_real, 6), "risk_pct": round(riesgo_pct, 4),
        "equity": equity, "binding": binding,
        "opened_utc": opened_utc,
    })
    print("\nOK: posicion abierta y protegida en el exchange.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafetyError as e:
        print(f"BLOQUEADO por seguridad: {e}")
        raise SystemExit(2)
    except BinanceError as e:
        print(f"BINANCE: {e}")
        raise SystemExit(7)
