"""Chequeo de conexion contra Alpaca (acciones US). SOLO LECTURA.

    python execution/alpaca_check.py --symbol AAPL

No envia ordenes: como maximo construye el bracket que se enviaria y lo imprime
(`place_bracket(..., dry_run=True)`), que escribe una linea en
logs/alpaca_orders.jsonl con result="not_sent".

La autenticacion la gestiona el CLI (`alpaca profile login`); aqui no hay
claves que leer ni enmascarar. Si el CLI no esta autenticado, las llamadas
privadas fallan con "authentication required".

Salidas:
  0 = conectado, cuenta legible, mercado y simbolo revisados
  2 = no encuentro el binario del CLI
  3 = CLI presente pero sin autenticar
  4 = el simbolo no existe / no tradable / datos de mercado fallan
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpaca_broker import (AlpacaError, AlpacaClient, SafetyError,  # noqa: E402
                           fmt, stop_side_check)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--qty", type=float, default=1.0,
                    help="shares del bracket de prueba en seco")
    ap.add_argument("--stop", type=float, default=None,
                    help="stop de la preview (por defecto ref -1%)")
    ap.add_argument("--take-profit", dest="take_profit", type=float, default=None)
    a = ap.parse_args()
    sym = a.symbol.upper()

    try:
        c = AlpacaClient(dry_run=True)
    except SafetyError as e:
        print(f"CLI: {e}")
        print("Instalacion: binario en ~/go/bin/alpaca.exe o exporta ALPACA_CLI.")
        return 2

    print(f"cli             : {c.cli}")
    print(f"simbolo         : {sym}")
    print(f"allow_symbols   : {', '.join(c.allow_symbols)}")
    print(f"max_order_usd   : {c.max_notional:g}")
    print(f"orders_enabled  : {c.orders_enabled}  "
          f"(ALLOW_ALPACA_ORDERS={'1' if c.orders_enabled else '0'})")

    # ── horario (publico) ──────────────────────────────────────────────
    try:
        clk = c.clock()
    except AlpacaError as e:
        print(f"\nCLOCK: {e}")
        return 3
    print(f"mercado         : {'ABIERTO' if clk.get('is_open') else 'CERRADO'}  "
          f"next_open {clk.get('next_open')}  next_close {clk.get('next_close')}")

    # ── simbolo y mercado ──────────────────────────────────────────────
    try:
        asr = c.asset(sym)
        ref = c.last_trade(sym)
        q = c.quote(sym)
    except AlpacaError as e:
        print(f"\nSIMBOLO/MERCADO: {e}")
        return 4
    if not asr.get("tradable") or asr.get("status") != "active":
        print(f"\nSIMBOLO: {sym} tradable={asr.get('tradable')} "
              f"status={asr.get('status')}")
        return 4
    mid = (q["bid"] + q["ask"]) / 2 if q["ask"] else ref
    spread = f"  spread {(q['ask']-q['bid'])/mid*1e4:.2f} bps" if q["ask"] else ""
    print(f"asset           : {asr.get('name')}  [{asr.get('exchange')}]  "
          f"fractionable={asr.get('fractionable')} shortable={asr.get('shortable')} "
          f"marginable={asr.get('marginable')}")
    print(f"mercado         : last {ref:g}  bid {q['bid']:g} / ask {q['ask']:g}{spread}")

    if sym not in c.allow_symbols:
        print(f"\nAVISO: {sym} no esta en ALLOW_ALPACA_SYMBOLS; una orden real "
              "la bloquearia.")

    # ── cuenta (privada) ───────────────────────────────────────────────
    try:
        acc = c.account()
    except AlpacaError as e:
        print(f"\nCUENTA: {e}")
        if "authentication" in str(e):
            print("  -> CLI sin autenticar: corre `alpaca profile login` una vez.")
        return 3

    print(f"\ncuenta          : status={acc.get('status')}  "
          f"numero={acc.get('account_number')}  "
          f"equity {fmt(float(acc.get('equity', 0)))}")
    print(f"caja            : cash {fmt(float(acc.get('cash', 0)))}  "
          f"buying_power {fmt(float(acc.get('buying_power', 0)))}  "
          f"mult {acc.get('multiplier')}x")
    print(f"opciones        : level {acc.get('options_trading_level')}  "
          f"crypto {acc.get('crypto_status')}")

    pos = c.positions()
    if pos:
        for p in pos:
            print(f"  posicion {p.get('symbol')}: qty={p.get('qty')} "
                  f"avg={p.get('avg_entry_price')} "
                  f"upnl={fmt(float(p.get('unrealized_pl', 0) or 0))}")
    else:
        print("posiciones      : ninguna")

    oo = c.open_orders()
    print(f"ordenes abiertas: {len(oo)}")
    for o in oo[:5]:
        print(f"  {o.get('id')} {o.get('side')} {o.get('type')} "
              f"qty={o.get('qty')} status={o.get('status')}")

    # ── bracket en seco ────────────────────────────────────────────────
    stop = a.stop if a.stop is not None else round(ref * 0.99, 2)
    ok, why = stop_side_check("LONG", stop, ref, a.take_profit)
    if not ok:
        print(f"\nbracket en seco : BLOQUEADO -> {why}")
    else:
        plan = c.place_bracket(symbol=sym, side="LONG", qty=a.qty,
                               stop=stop, take_profit=a.take_profit)
        args = plan["would_send"]
        print(f"\nbracket en seco : BUY {sym} qty={a.qty:g} ~"
              f"{plan['notional']:.2f} USD  stop {stop:g}"
              + (f"  tp {a.take_profit:g}" if a.take_profit else ""))
        print(f"  ({args[0]} {args[1]} ... -> {plan['reason']})")
        print("  (no se ha enviado nada; queda en logs/alpaca_orders.jsonl como not_sent)")

    print("\nOK: CLI, cuenta y mercado legibles.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SafetyError as e:
        print(f"BLOQUEADO por seguridad: {e}")
        raise SystemExit(2)
