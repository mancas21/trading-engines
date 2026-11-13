"""Router Alpaca: de una senal de acciones a una posicion protegida.

    # plan (no envia nada)
    python execution/alpaca_router.py --symbol AAPL --side LONG --stop 335.50

    # ejecutar (ademas exige ALLOW_ALPACA_ORDERS=1 y mercado abierto)
    $env:ALLOW_ALPACA_ORDERS="1"
    python execution/alpaca_router.py --symbol AAPL --side LONG --stop 335.50 --execute

    # o desde un fichero
    python execution/alpaca_router.py --signal senal.json --execute

Esquema de la senal (json):
    {"symbol": "AAPL", "side": "LONG", "stop": 335.50,
     "take_profit": 370.0, "strategy": "dt-01"}

DECISIONES (espejo de router.py, 2026-10-25, con las de Alpaca del 2026-10-29)
==============================================================================
1. UNA posicion por simbolo. Posicion u ordenes vivas -> aborta. No promedia.
2. Tamano por RIESGO: risk_pct (0.5% por defecto) del equity hasta el stop,
   con tope de nocional (MAX_ALPACA_NOTIONAL_USD) y tope de caja (cash, SIN
   apalancamiento aunque la cuenta sea margin). Minimo de los tres, shares
   enteras (los brackets fraccionales no se pisan). Si el redondeo deja el
   riesgo por debajo del 10% del objetivo, avisa.
3. STOP EN EL BROKER: bracket ATOMICO de Alpaca (entrada + stop + TP se
   aceptan o se rechazan juntos). Si la entrada llena, el stop ya vive en el
   exchange aunque este proceso muera. Aun asi se verifica al final: posicion
   viva sin orden protectora = CRITICO (exit 6).
4. MERCADO CERRADO NO ENTRA: las ordenes day en cola llenarian en la apertura
   siguiente con un stop pensado para el precio de hoy. El plan (sin
   --execute) si se puede preparar fuera de horario.

Diferencias con la pata crypto, declaradas:
  * Sin `--require-candidate`: no hay gate de backtest para acciones US en el
    repo. La senal la trae el operador (o los skills trader-*). Quien ejecute
    con --execute responde de la senal.
  * Sin state/positions.json: el estado crypto lo lee el reconciliador de
    Binance; mezclar AAPL ahi lo confundiria. La verdad de Alpaca es
    consultable en el broker; la trazabilidad queda en logs/alpaca_router.jsonl.
  * Comision 0 en acciones: el coste real es el spread impreso en el plan.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from alpaca_broker import (AlpacaError, AlpacaClient, SafetyError,  # noqa: E402
                           entry_guard, fmt, round_qty, size_order, stop_side_check)

RISK_PCT_DEFAULT = 0.5
RISK_PCT_MAX = 1.0


def load_signal(path: str | None, args) -> dict:
    if path:
        j = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        j = {"symbol": args.symbol, "side": args.side, "stop": args.stop,
             "take_profit": args.take_profit, "strategy": args.strategy}
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--signal", help="json con symbol/side/stop/take_profit")
    ap.add_argument("--symbol")
    ap.add_argument("--side", choices=("LONG", "SHORT", "long", "short", "BUY", "SELL"))
    ap.add_argument("--stop", type=float)
    ap.add_argument("--take-profit", dest="take_profit", type=float)
    ap.add_argument("--strategy", default=None, help="id de estrategia o skill origen")
    ap.add_argument("--risk-pct", type=float, default=RISK_PCT_DEFAULT)
    ap.add_argument("--max-notional", type=float, default=None,
                    help="tope de nocional (por defecto MAX_ALPACA_NOTIONAL_USD del .env)")
    ap.add_argument("--close", action="store_true",
                    help="cierra la posicion del simbolo (emergencia)")
    ap.add_argument("--execute", action="store_true",
                    help="enviar de verdad (ademas exige ALLOW_ALPACA_ORDERS=1)")
    return ap


def wait_fill(c: AlpacaClient, order_id: str, tries: int = 5) -> dict:
    """Sondea la orden hasta salir de pending/new o agotar intentos."""
    o = {"status": "pending_new"}
    for _ in range(tries):
        time.sleep(1)
        o = c.order(order_id)
        st = str(o.get("status", "")).lower()
        if st not in ("pending_new", "new", "accepted"):
            break
    return o


def main() -> int:
    a = build_parser().parse_args()
    return run(a)


def run(a: argparse.Namespace) -> int:
    sig = load_signal(a.signal, a)
    sym, side = sig["symbol"], sig["side"]

    if a.risk_pct <= 0 or a.risk_pct > RISK_PCT_MAX:
        raise SafetyError(f"risk_pct {a.risk_pct} fuera de (0, {RISK_PCT_MAX}]")

    c = AlpacaClient(dry_run=not a.execute, max_notional_usd=a.max_notional)
    print(f"cli             : {c.cli}")
    print(f"senal           : {sym} {side} stop {sig['stop']:g}"
          + (f" tp {sig['take_profit']:g}" if sig.get("take_profit") else "")
          + (f"  [{sig['strategy']}]" if sig.get("strategy") else ""))
    print(f"allow_symbols   : {', '.join(c.allow_symbols)}")
    print(f"max_order_usd   : {c.max_notional:g}  orders_enabled={c.orders_enabled}")

    # ── cierre de emergencia ───────────────────────────────────────────
    if a.close:
        plan = c.close_position(sym)
        if plan.get("dry_run"):
            print(f"\nCIERRE en seco  : {plan['reason']} -> position close {sym}")
            return 0
        print(f"\nCIERRE enviado  : {json.dumps(plan)[:200]}")
        return 0 if not c.position(sym) else 7

    clk = c.clock()
    open_now, why = entry_guard(clk)
    print(f"mercado         : {'ABIERTO' if open_now else 'CERRADO'}  "
          f"next_open {clk.get('next_open')}")
    if not open_now and a.execute:
        print(f"\nBLOQUEADO: {why}")
        print("El plan de arriba no se ejecuta fuera de horario (ordenes day en "
              "cola llenarian con el stop de hoy).")
        return 4

    asr = c.asset(sym)
    if sym not in c.allow_symbols:
        print(f"\nBLOQUEADO: {sym} no esta en ALLOW_ALPACA_SYMBOLS")
        return 2
    if not asr.get("tradable") or asr.get("status") != "active":
        print(f"\nBLOQUEADO: {sym} tradable={asr.get('tradable')} "
              f"status={asr.get('status')}")
        return 4

    ref = c.last_trade(sym)
    q = c.quote(sym)
    spread_bps = ((q["ask"] - q["bid"]) / ref * 1e4) if (q["ask"] and q["bid"]) else 0.0
    print(f"referencia      : last {ref:g}  bid {q['bid']:g} / ask {q['ask']:g}  "
          f"spread {spread_bps:.2f} bps  ({asr.get('name')})")

    ok, why = stop_side_check(side, sig["stop"], ref, sig.get("take_profit"))
    if not ok:
        print(f"\nBLOQUEADO: {why}")
        print("No se abre una posicion cuyo bracket se rechazaria o dispararia al instante.")
        return 4

    acc = c.account()
    equity = float(acc.get("equity", 0) or 0)
    cash = float(acc.get("cash", 0) or 0)
    print(f"cuenta          : equity {fmt(equity)}  cash {fmt(cash)}  "
          f"status {acc.get('status')}")

    pos = c.position(sym)
    oo = c.open_orders(sym)
    if pos and float(pos.get("qty", 0) or 0) != 0:
        print(f"  posicion VIVA {sym}: qty={pos.get('qty')} avg={pos.get('avg_entry_price')} "
              f"upnl={fmt(float(pos.get('unrealized_pl', 0) or 0))}")
    for o in oo:
        print(f"  orden VIVA {sym}: {str(o.get('id'))[:8]} {o.get('side')} {o.get('type')} "
              f"qty={o.get('qty')} status={o.get('status')}")
    if (pos and float(pos.get("qty", 0) or 0) != 0) or oo:
        print("\nBLOQUEADO: una posicion por simbolo. No promedio ni reemplazo.")
        return 3

    # ── tamano ─────────────────────────────────────────────────────────
    raw, binding = size_order(equity=equity, cash=cash, price=ref,
                              stop=sig["stop"], risk_pct=a.risk_pct,
                              max_notional=c.max_notional)
    qty = round_qty(raw, fractionable=False)
    if qty < 1:
        print(f"\nBLOQUEADO: {raw:.4f} shares redondea a 0 ({binding} manda). "
              "Sube el riesgo o el tope.")
        return 4
    notional = qty * ref
    dist = abs(ref - sig["stop"])
    riesgo_real = qty * dist
    riesgo_pct = riesgo_real / equity * 100 if equity else 0

    print(f"\nPLAN")
    print(f"  entrada         : bracket MARKET "
          f"{'BUY' if side == 'LONG' else 'SELL'} {qty:g} {sym} ~{notional:.2f} USD")
    print(f"  stop            : {sig['stop']:g}  ({dist/ref*1e4:.0f} bps, "
          f"{'<' if side == 'LONG' else '>'} last)")
    print(f"  riesgo          : {fmt(riesgo_real)} USD = {riesgo_pct:.2f}% del equity "
          f"(objetivo {a.risk_pct:g}%)")
    print(f"  restriccion     : {binding}")
    if sig.get("take_profit"):
        rr = abs(sig["take_profit"] - ref) / dist
        print(f"  take profit     : {sig['take_profit']:g}  R:R {rr:.2f}")
    print(f"  coste           : comision 0 + {spread_bps:.2f} bps spread x2")
    if riesgo_real < equity * a.risk_pct / 100.0 * 0.10:
        print(f"  AVISO: shares enteras dejan el riesgo en {riesgo_pct:.2f}%, "
              f"muy por debajo del objetivo {a.risk_pct:g}%")

    if not a.execute:
        print("\nDRY-RUN: no se ha enviado nada. Repite con --execute y ALLOW_ALPACA_ORDERS=1.")
        return 0

    if not c.orders_enabled:
        print("\nBLOQUEADO: --execute sin ALLOW_ALPACA_ORDERS=1")
        return 2
    if not open_now:
        print(f"\nBLOQUEADO: {why}")
        return 4

    # ── ejecucion: bracket atomico ─────────────────────────────────────
    try:
        resp = c.place_bracket(symbol=sym, side=side, qty=qty, stop=sig["stop"],
                               take_profit=sig.get("take_profit"), dry_run=False)
    except (AlpacaError, SafetyError) as e:
        print(f"\nCRITICO: la submission del bracket fue rechazada ({e}). "
              "No hay posicion ni stop parcial: es atomica.")
        return 7
    oid = resp.get("id")
    print(f"\n1) bracket enviado id={oid} status={resp.get('status')}")

    o = wait_fill(c, str(oid))
    st = str(o.get("status", "")).lower()
    filled = float(o.get("filled_qty", 0) or 0)
    avg = float(o.get("filled_avg_price", 0) or 0) or ref
    print(f"2) entrada status={st} fill {filled:g} @ {avg:g} "
          f"(slippage {(avg-ref)/ref*1e4:+.2f} bps vs last)")
    if st in ("rejected", "canceled", "expired"):
        print(f"   rechazada: {o.get('reject_reason') or 'sin detalle'}")
        return 7
    if filled <= 0:
        print("   CRITICO: sin fill (o sin confirmar); revisa la cuenta AHORA.")
        return 7

    # ── verificacion: posicion viva y proteccion viva ──────────────────
    time.sleep(1)
    pos = c.position(sym)
    oo = c.open_orders(sym)
    legs = [o for o in oo if str(o.get("type", "")).lower()
            in ("stop", "stop_limit", "limit")]
    print(f"verificacion    : posicion={'si' if pos else 'NO'} "
          f"({pos.get('qty') if pos else 0})  ordenes={len(oo)}  "
          f"protectoras={len(legs)}")
    for o in oo:
        print(f"  {str(o.get('id'))[:8]} {o.get('side')} {o.get('type')} "
              f"qty={o.get('qty')} stop={o.get('stop_price')} "
              f"limit={o.get('limit_price')}")

    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "symbol": sym, "side": side, "qty": filled, "avg": avg,
           "ref": ref, "stop": sig["stop"], "tp": sig.get("take_profit"),
           "order_id": oid, "risk_usd": round(riesgo_real, 6),
           "risk_pct": round(riesgo_pct, 4), "equity": equity,
           "binding": binding, "strategy": sig.get("strategy"),
           "protected": bool(legs) or (pos and not oo)}
    try:
        (HERE / "logs").mkdir(exist_ok=True)
        with (HERE / "logs" / "alpaca_router.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass

    if pos and not legs:
        print("\nCRITICO: posicion viva sin orden protectora visible. "
              "Revisa la cuenta AHORA (en el peor caso: position close).")
        return 6

    print("\nOK: posicion abierta y protegida en el broker.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafetyError as e:
        print(f"BLOQUEADO por seguridad: {e}")
        raise SystemExit(2)
    except AlpacaError as e:
        print(f"ALPACA: {e}")
        raise SystemExit(7)
