"""
Bot NEUTRAL (grid delta-neutral) para Binance USDT-M testnet.
Opera TODOS los pares sin sesgo direccional. No usa estrategias S02-S10.

MECANICA DEL GRID NEUTRAL
=========================
Por cada símbolo, el bot mantiene N niveles de compra (por debajo del mark) y
N niveles de venta (por encima del mark), con el MISMO notional por nivel en
cada lado.

Cuando se llena una compra en el nivel i, el bot coloca una VENTA un nivel mas
arriba (mark + i*spacing). Esa venta es el take-profit: bloquea la ganancia del
nivel y devuelve la posición a plano. Simetricamente, cuando se llena una venta,
coloca una COMPRA un nivel mas abajo.

Resultado: la exposición neta tiende a cero, el profit viene del spread del
grid, y el bot gana con volatilidad, no con dirección. Sin esta regla de
emparejamiento, un grid plano acumularía posición en la tendencia (no seria
neutral).

ESTADO
======
Se guarda en state/grid_state.json:
  { "SYM": { "level": 0, "side": "BUY", "order_id": 123, "price": 1.23, "qty": 4 } }
Un registro por nivel pendiente de llenarse. Cuando el nivel se llena, se
coloca la orden contraria y se libera el registro.

Uso:
  python execution/neutral_bot.py              # dry-run (no envia)
  python execution/neutral_bot.py --execute    # envia ordenes reales a testnet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from binance_testnet import BinanceError, SafetyError, TestnetClient
import proclock

LOG = HERE / "logs" / "neutral_bot.jsonl"
STATE = HERE / "state" / "grid_state.json"
LOCK = "neutral_bot"

# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------
SYMBOLS = [
    "HYPEUSDT", "SUIUSDT", "ETHUSDT", "SOLUSDT",
    "1000PEPEUSDT", "BTCUSDT", "ARBUSDT", "XRPUSDT", "JUPUSDT",
]

GRID_LEVELS = 4             # niveles por lado
GRID_SPACING_PCT = 0.005    # 0.5% entre niveles
NOTIONAL_PER_SIDE_USDT = 100  # USDT por nivel y por lado
MAX_ORDER_NOTIONAL_USD = 650
POLL_SECONDS = 20           # cada cuanto se revisa si se llenaron niveles

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def log(rec: dict) -> None:
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(d: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class NeutralGridBot:
    def __init__(
        self,
        client: TestnetClient,
        symbols: list[str],
        grid_levels: int = GRID_LEVELS,
        grid_spacing_pct: float = GRID_SPACING_PCT,
        notional_per_side_usdt: float = NOTIONAL_PER_SIDE_USDT,
        dry_run: bool = True,
    ):
        self.client = client
        self.symbols = symbols
        self.grid_levels = grid_levels
        self.grid_spacing = grid_spacing_pct
        self.notional_per_side = notional_per_side_usdt
        self.dry_run = dry_run
        self.allow_symbols = set(client.allow_symbols)
        self.state = load_state()

    # ---- exchange ---------------------------------------------------------
    def place_limit(self, symbol: str, side: str, price: float, qty: float) -> dict | None:
        if self.dry_run:
            log({"symbol": symbol, "action": f"dry-run {side}", "price": price, "qty": qty})
            return None
        try:
            return self.client.place_order(
                symbol=symbol, side=side, qty=qty, order_type="LIMIT",
                price=price, time_in_force="GTC",
            )
        except (BinanceError, SafetyError) as e:
            log({"symbol": symbol, "action": f"order_error {side}",
                 "price": price, "qty": qty, "error": str(e)[:200]})
            return None

    def order_status(self, symbol: str, order_id: int) -> str | None:
        try:
            return self.client.query_order(symbol, order_id=order_id).get("status")
        except (BinanceError, SafetyError):
            return None

    # ---- estado por nivel -------------------------------------------------
    def levels_for(self, symbol: str) -> dict:
        return self.state.setdefault(symbol, {})

    def check_fills(self, symbol: str) -> None:
        """Revisa si algun nivel pendiente se llenoio; si si, coloca la orden
        contraria (take-profit) y libera el nivel."""
        lv = self.levels_for(symbol)
        for key in list(lv.keys()):
            rec = lv[key]
            if not rec.get("order_id"):
                continue
            status = self.order_status(symbol, rec["order_id"])
            if status not in ("FILLED",):
                if status in ("CANCELED", "REJECTED", "EXPIRED"):
                    log({"symbol": symbol, "action": "level_lost",
                         "level": key, "status": status})
                    lv.pop(key, None)
                continue

            # Llenado: colocar la orden contraria un nivel mas lejos del mark
            side = rec["side"]
            price = float(rec["price"])
            qty = float(rec["qty"])
            level = int(rec["level"])
            tp_price = self.client.round_price(
                symbol, price * (1 + self.grid_spacing) if side == "BUY"
                else price * (1 - self.grid_spacing))
            tp_side = "SELL" if side == "BUY" else "BUY"
            log({"symbol": symbol, "action": "fill", "level": level, "filled_side": side,
                 "fill_price": price, "qty": qty, "tp_side": tp_side, "tp_price": tp_price})
            r = self.place_limit(symbol, tp_side, tp_price, qty)
            if r is not None:
                lv[key] = {"level": level, "side": tp_side, "order_id": r.get("orderId"),
                           "price": tp_price, "qty": qty}
            else:
                lv.pop(key, None)
        save_state(self.state)

    def ensure_grid(self, symbol: str) -> None:
        """Coloca los niveles que falten alrededor del mark actual."""
        try:
            mark = float(self.client.mark_price(symbol))
            rules = self.client.rules(symbol)
        except (BinanceError, SafetyError) as e:
            log({"symbol": symbol, "action": "error", "error": str(e)[:200]})
            return

        lv = self.levels_for(symbol)
        qty = self.client.round_qty(symbol, self.notional_per_side / mark)
        if qty <= 0 or qty * mark < rules.get("min_notional", 0):
            log({"symbol": symbol, "action": "skip",
                 "why": f"qty {qty} no cumple min_notional {rules.get('min_notional')}"})
            return

        placed = 0
        for i in range(1, self.grid_levels + 1):
            for side, sign in (("BUY", -1), ("SELL", +1)):
                key = f"{side}{i}"
                if key in lv:
                    continue  # ya hay una orden viva en este nivel
                px = self.client.round_price(symbol, mark * (1 + sign * self.grid_spacing * i))
                r = self.place_limit(symbol, side, px, qty)
                if r is not None:
                    lv[key] = {"level": i, "side": side, "order_id": r.get("orderId"),
                               "price": px, "qty": qty}
                    placed += 1
        if placed:
            save_state(self.state)
            log({"symbol": symbol, "action": "grid_placed", "mark": mark,
                 "new_levels": placed, "levels": self.grid_levels,
                 "spacing_pct": self.grid_spacing, "qty_per_level": qty,
                 "notional_per_side": self.notional_per_side})

    # ---- ciclo ------------------------------------------------------------
    def run_cycle(self) -> None:
        for symbol in self.symbols:
            if symbol not in self.allow_symbols:
                log({"symbol": symbol, "action": "skip", "why": "fuera de ALLOW_SYMBOLS"})
                continue
            self.check_fills(symbol)
            self.ensure_grid(symbol)

    def net_exposure(self) -> dict:
        """Exposicion neta por simbolo (debe tender a 0 = neutral)."""
        out = {}
        try:
            for p in self.client.positions():
                a = float(p.get("positionAmt", 0))
                if a != 0:
                    out[p["symbol"]] = a
        except (BinanceError, SafetyError):
            pass
        return out

    def run(self, loop: bool = False, interval: int = POLL_SECONDS) -> None:
        if not proclock.acquire(LOCK):
            print(f"ya hay un neutral_bot vivo (pid {proclock.holder(LOCK)})")
            return
        try:
            while True:
                self.run_cycle()
                if not loop:
                    break
                time.sleep(max(10, interval))
        except KeyboardInterrupt:
            pass
        finally:
            proclock.release(LOCK)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Bot neutral grid para Binance testnet")
    ap.add_argument("--execute", action="store_true", help="envia ordenes reales (no dry-run)")
    ap.add_argument("--loop", action="store_true", help="bucle continuo")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS,
                    help="segundos entre ciclos en loop")
    ap.add_argument("--symbols", type=str, default=",".join(SYMBOLS))
    ap.add_argument("--levels", type=int, default=GRID_LEVELS)
    ap.add_argument("--spacing", type=float, default=float(GRID_SPACING_PCT),
                    help="espaciado en fraccion (0.005 = 0.5%)")
    ap.add_argument("--notional", type=float, default=NOTIONAL_PER_SIDE_USDT)
    ap.add_argument("--status", action="store_true", help="solo muestra el estado y sale")
    a = ap.parse_args()

    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    client = TestnetClient(dry_run=not a.execute, max_notional_usd=MAX_ORDER_NOTIONAL_USD)
    client.sync_time()

    bot = NeutralGridBot(
        client=client, symbols=symbols, grid_levels=a.levels,
        grid_spacing_pct=a.spacing, notional_per_side_usdt=a.notional,
        dry_run=not a.execute,
    )

    if a.status:
        exp = bot.net_exposure()
        print(f"exposicion neta: {exp if exp else 'PLANO (todo en 0)'}")
        for sym in symbols:
            lv = bot.levels_for(sym)
            print(f"  {sym:14s} niveles_vivos={len(lv)} "
                  f"open_orders={len(client.open_orders(sym))}")
        return 0

    print(
        f"Neutral grid bot | simbolos={len(symbols)} | niveles/lado={a.levels} | "
        f"espaciado={a.spacing*100:.2f}% | notional/lado={a.notional} USDT | "
        f"{'EJECUTANDO EN TESTNET' if a.execute else 'DRY-RUN'}",
        flush=True,
    )
    bot.run(loop=a.loop, interval=a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
