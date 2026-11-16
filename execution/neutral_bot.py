"""
Bot NEUTRAL (grid delta-neutral) para Binance USDT-M testnet.
Opera TODOS los pares sin sesgo direccional. No usa estrategias S02-S10.

MODELO (v2, 2026-11-15)
=======================
El grid mantiene SIEMPRE 4 compras por debajo del ancla y 4 ventas por encima,
al mismo notional. El take-profit NO es una orden aparte: es el nivel opuesto
del propio grid.

  ancla A, espaciado s=0.5%:
      BUY i  ->  A * (1 - i*s)      SELL i -> A * (1 + i*s)

Cuando se llena una compra en BUY1, se vuelve a colocar BUY1 en el mismo
precio: el grid nunca pierde un lado. La venta que cierra esa compra ya esta
en SELL1 (o en SELL2, etc.), a +1.0% del precio de compra. Esa es la ganancia
bruta del ciclo; el par se cierra solo cuando el precio vuelve a subir.

Por que NO se coloca un TP a fill+0.5% (modelo v1, retirado):
  * un TP a fill*1.005 cae practicamente en el mark (fill = mark*0.995),
    asi que se ejecuta al instante contra el bid: el ciclo pierde solo las
    comisiones y genera churn (3.398 ejecuciones en 3 dias);
  * si el precio se mueve, el TP queda fuera de la banda PERCENT_PRICE (+-5%)
    y el exchange lo rechaza: 1.163 ordenes rechazadas, 1.158 reintentos;
  * en modo one-way el exchange netea, pero el bot contaba lotes por nivel:
    el estado decia vender 791.9 ARB cuando la posicion real era 733.1.

SEGURIDAD
=========
  * Tope de inventario por simbolo (400 USD) y total (1.200 USD): al llegar,
    se dejan de reponer compras (las ventas siempre se permiten).
  * Stop por simbolo: si el uPnL de la posicion cae -4% de su nocional, se
    cancelan sus niveles, se cierra a mercado y el simbolo entra en cuarentena
    60 min.
  * Kill switch / perdida diaria de risk_guard: si salta, se cancelan las
    compras y no se abre riesgo nuevo.
  * Re-anclaje: solo con la posicion plana y solo si el mark se alejo >1% del
    ancla. Con inventario abierto NO se persigue el precio: eso realizaria
    perdidas y es justo lo que el stop debe decidir.

ESTADO (state/grid_state.json)
==============================
  {"symbols": {"SYM": {"anchor": 1.23,
                        "slots": {"BUY1": {"level":1,"price":1.22,
                                            "qty":4,"order_id":123}}}},
   "cooldown": {"SYM": 1763300000}}

Uso:
  python execution/neutral_bot.py              # dry-run (no envia)
  python execution/neutral_bot.py --execute    # envia ordenes reales a testnet
  python execution/neutral_bot.py --status     # estado y sale
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
import risk_guard

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

GRID_LEVELS = 4                 # niveles por lado
GRID_SPACING_PCT = 0.005        # 0.5% entre niveles
NOTIONAL_PER_SIDE_USDT = 100    # USDT por nivel
MAX_ORDER_NOTIONAL_USD = 650
POLL_SECONDS = 20

MAX_INVENTORY_USD_PER_SYMBOL = 400   # 4 niveles llenos
MAX_INVENTORY_USD_TOTAL = 1200       # correlacion: ~3 simbolos llenos
STOP_LOSS_PCT = 0.04                 # -4% del nocional de la posicion
COOLDOWN_MIN = 60                    # cuarentena tras stop
FLAT_USD = 5.0                       # menos de esto se considera plano
REANCHOR_FLAT_PCT = 0.01             # re-ancla solo plano y >1% de deriva
MIN_EDGE_PCT = 0.0005                # 5 bps: no colocar precios pegados al mark

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


def _fresh_state() -> dict:
    return {"version": 2, "symbols": {}, "cooldown": {}}


def load_state() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _fresh_state()
    if not isinstance(d, dict) or "symbols" not in d:
        return _fresh_state()   # estado v1 (slots que cambiaban de lado): se tira
    d.setdefault("cooldown", {})
    return d


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
        self._rules: dict[str, dict] = {}
        self._halted = False     # kill switch / perdida diaria

    # ---- helpers ----------------------------------------------------------
    def sym_state(self, symbol: str) -> dict:
        return self.state["symbols"].setdefault(symbol, {"anchor": None, "slots": {}})

    def rules(self, symbol: str) -> dict:
        if symbol not in self._rules:
            self._rules[symbol] = self.client.rules(symbol)
        return self._rules[symbol]

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
            log({"symbol": symbol, "action": "order_error", "side": side,
                 "price": price, "qty": qty, "error": str(e)[:200]})
            return None

    # ---- slots ------------------------------------------------------------
    def cancel_slot(self, symbol: str, st: dict, key: str) -> None:
        rec = st["slots"].get(key)
        if not rec:
            return
        oid = rec.get("order_id")
        if oid and not self.dry_run:
            try:
                self.client.cancel(symbol, order_id=oid)
            except (BinanceError, SafetyError):
                # 2026-11-16 — BUG: el slot se quitaba del estado AUNQUE la
                # cancelacion fallara. La orden se queda viva en el exchange y
                # el estado la olvida: huérfana que el bot no gestiona pero que
                # el exchange sigue ejecutando. Si el cancel falla, el slot se
                # queda: es la informacion honesta.
                log({"symbol": symbol, "action": "cancel_failed",
                     "level": key, "order_id": oid})
                return
        st["slots"].pop(key, None)

    def cancel_all_slots(self, symbol: str, st: dict) -> None:
        for key in list(st.get("slots", {})):
            self.cancel_slot(symbol, st, key)

    def sync_slots(self, symbol: str, st: dict) -> int:
        """Detecta fills y ordenes muertas. Devuelve el numero de fills."""
        if self.dry_run or not st.get("slots"):
            return 0
        try:
            live = {int(o["orderId"]) for o in self.client.open_orders(symbol)}
        except (BinanceError, SafetyError) as e:
            log({"symbol": symbol, "action": "error", "error": f"open_orders: {e}"[:200]})
            return 0

        fills = 0
        for key in list(st["slots"]):
            rec = st["slots"][key]
            oid = int(rec.get("order_id") or 0)
            if not oid or oid in live:
                continue
            try:
                q = self.client.query_order(symbol, order_id=oid)
            except (BinanceError, SafetyError):
                continue
            status = q.get("status")
            executed = float(q.get("executedQty", 0) or 0)
            if status == "FILLED":
                px = float(q.get("avgPrice") or rec.get("price") or 0)
                log({"symbol": symbol, "action": "fill", "level": rec.get("level"),
                     "side": key[:3], "price": px, "qty": executed or rec.get("qty"),
                     "order_id": oid})
                fills += 1
                st["slots"].pop(key, None)
            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                log({"symbol": symbol, "action": "lost_slot", "level": key,
                     "status": status, "executed_qty": executed})
                st["slots"].pop(key, None)
        return fills

    # ---- grid -------------------------------------------------------------
    @staticmethod
    def increases_exposure(side: str, amt: float) -> bool:
        """True si esta orden AUMENTA la exposicion neta de la posicion.

        Con la posicion en corto (amt<0) comprar la REDUCE y vender la
        aumenta; con la posicion en largo es al reves. En plano las dos
        lados abren posicion, asi que los dos aumentan.

        2026-11-16 — este matiz es el que faltaba. El codigo bloqueaba la
        compra cuando el inventario estaba por encima del tope
        (`if side == "BUY" and blocked: continue`). En un corto eso bloquea
        justamente la cobertura: el bot podia AUMENTAR el corto (vender) sin
        limite y nunca podia taparlo (comprar). MEDIDO: el 12-nov el libro
        estaba en 13.569 USD con un tope total de 1.200 USD (11x), 6 de los
        9 simbolos en corto, y HYPE con 3.539 USD de corto y CERO slots:
        su ancla (66,946) estaba 2,2% por debajo del mark (68,412), asi que
        sus niveles de venta caian al lado equivocado del mark (se omitian)
        y los de compra estaban bloqueados por el tope. Posicion viva que
        el bot no podia ampliar, tapar ni re-anclar.
        """
        if amt == 0:
            return True
        if side == "BUY":
            return amt > 0       # comprar con un largo abierto lo aumenta
        return amt < 0           # vender con un corto abierto lo aumenta

    def reanchor(self, symbol: str, st: dict, mark: float) -> None:
        had = len(st.get("slots", {}))
        self.cancel_all_slots(symbol, st)
        st["anchor"] = mark
        st["slots"] = {}
        if had:
            log({"symbol": symbol, "action": "reanchor", "mark": mark,
                 "cancelled_slots": had})

    def ensure_grid(self, symbol: str, st: dict, mark: float,
                    blocked: bool, amt: float = 0.0,
                    halted: bool = False) -> None:
        """Rellena los 8 niveles que falten. No persigue el precio: si un nivel
        cae del lado equivocado del mark, se omite hasta que el precio vuelva.

        `blocked` = inventario por encima del tope. El lado que AUMENTA la
        exposicion queda bloqueado (y se cancelan sus slots vivos: si no, el
        tope no existe); el lado que la REDUCE se permite siempre, porque
        bloquear la cobertura convierte el grid en una apuesta direccional.
        `halted` = kill switch o guardia diaria: no se coloca NADA, ni se
        cancela lo que ya esta puesto.
        """
        anchor = float(st["anchor"])
        r = self.rules(symbol)
        band = r.get("percent_price") or {}
        lo = mark * float(band.get("down") or 1.0)
        hi = mark * float(band.get("up") or 1.0)

        qty = self.client.round_qty(symbol, self.notional_per_side / mark)
        if qty <= 0 or qty * mark < r.get("min_notional", 0):
            log({"symbol": symbol, "action": "skip",
                 "why": f"qty {qty} no cumple min_notional {r.get('min_notional')}"})
            return

        # Tope de inventario: se retiran los slots vivos del lado que aumenta
        # la exposicion. Sin esto el tope solo impedia colocar, no absorber:
        # el bot seguia agrandando la posicion con las ordenes que ya estaban
        # puestas en el libro.
        if blocked and not halted:
            for key in list(st.get("slots", {})):
                if self.increases_exposure(key[:3], amt):
                    self.cancel_slot(symbol, st, key)

        placed = 0
        for i in range(1, self.grid_levels + 1):
            for side, sign in (("BUY", -1), ("SELL", +1)):
                key = f"{side}{i}"
                if key in st["slots"]:
                    continue
                if halted:
                    continue
                if blocked and self.increases_exposure(side, amt):
                    continue
                px = self.client.round_price(
                    symbol, anchor * (1 + sign * self.grid_spacing * i))
                # el precio debe quedar del lado correcto del mark y en banda
                if side == "BUY":
                    if not (lo < px < mark * (1 - MIN_EDGE_PCT)):
                        continue
                else:
                    if not (mark * (1 + MIN_EDGE_PCT) < px < hi):
                        continue
                res = self.place_limit(symbol, side, px, qty)
                if res is not None and res.get("orderId"):
                    st["slots"][key] = {"level": i, "price": px, "qty": qty,
                                       "order_id": res.get("orderId")}
                    placed += 1
        if placed:
            save_state(self.state)
            log({"symbol": symbol, "action": "grid_placed", "mark": mark,
                 "anchor": anchor, "new_levels": placed,
                 "total_slots": len(st["slots"]),
                 "levels": self.grid_levels, "spacing_pct": self.grid_spacing,
                 "qty_per_level": qty, "notional_per_level": self.notional_per_side})

    # ---- riesgo -----------------------------------------------------------
    def check_stop(self, symbol: str, st: dict, pos: dict | None) -> bool:
        if not pos:
            return False
        amt = float(pos.get("positionAmt", 0))
        mark = float(pos.get("markPrice", 0) or 0)
        notional = abs(amt) * mark
        if notional < FLAT_USD:
            return False
        upnl = float(pos.get("unRealizedProfit", 0) or 0)
        if upnl > -STOP_LOSS_PCT * notional:
            return False
        side = "SELL" if amt > 0 else "BUY"
        log({"symbol": symbol, "action": "stop_loss", "amt": amt,
             "notional": round(notional, 2), "upnl": round(upnl, 2),
             "pct": round(upnl / notional * 100, 2)})
        self.cancel_all_slots(symbol, st)
        if not self.dry_run:
            try:
                r = self.client.close_position_market(symbol, side, abs(amt))
                log({"symbol": symbol, "action": "stop_closed", "side": side,
                     "qty": abs(amt), "status": r.get("status"),
                     "executed": r.get("executedQty")})
            except (BinanceError, SafetyError) as e:
                log({"symbol": symbol, "action": "stop_close_error",
                     "error": str(e)[:200]})
        st["anchor"] = None
        st["slots"] = {}
        self.state["cooldown"][symbol] = time.time() + COOLDOWN_MIN * 60
        save_state(self.state)
        return True

    # ---- ciclo ------------------------------------------------------------
    def run_cycle(self) -> None:
        # 1) equity + guardia diaria
        equity = 0.0
        try:
            equity = float(self.client.account().get("totalMarginBalance", 0) or 0)
        except (BinanceError, SafetyError):
            pass
        if equity > 0 and not self.dry_run:
            daily = risk_guard.daily_check(equity)
            if daily.get("tripped") and not self._halted:
                log({"symbol": "-", "action": "daily_guard",
                     "why": daily.get("why"), "equity": round(equity, 2)})
            if daily.get("tripped"):
                self._halted = True
        if risk_guard.kill_active():
            self._halted = True

        # 2) posiciones y marks
        positions: dict[str, dict] = {}
        try:
            positions = {p["symbol"]: p for p in self.client.positions()
                         if float(p.get("positionAmt", 0) or 0) != 0}
        except (BinanceError, SafetyError):
            pass
        total_inv = sum(abs(float(p["positionAmt"])) * float(p.get("markPrice", 0) or 0)
                        for p in positions.values())

        now = time.time()
        cooldown = self.state["cooldown"]

        for symbol in self.symbols:
            if symbol not in self.allow_symbols:
                log({"symbol": symbol, "action": "skip", "why": "fuera de ALLOW_SYMBOLS"})
                continue
            st = self.sym_state(symbol)

            # fills / ordenes muertas
            self.sync_slots(symbol, st)

            pos = positions.get(symbol)
            amt = float(pos["positionAmt"]) if pos else 0.0
            mark = float(pos["markPrice"]) if pos else 0.0
            if pos and mark <= 0:
                try:
                    mark = self.client.mark_price(symbol)
                except (BinanceError, SafetyError):
                    continue
            if not pos:
                try:
                    mark = self.client.mark_price(symbol)
                except (BinanceError, SafetyError):
                    continue

            # stop por simbolo
            if self.check_stop(symbol, st, pos):
                continue

            # cuarentena
            until = float(cooldown.get(symbol, 0) or 0)
            if until > now:
                continue
            if until:
                cooldown.pop(symbol, None)
                save_state(self.state)

            # ancla y re-anclaje (solo plano)
            flat = abs(amt) * mark < FLAT_USD
            anchor = st.get("anchor")
            if anchor is None:
                st["anchor"] = mark
                anchor = mark
                log({"symbol": symbol, "action": "anchor_set", "mark": mark})
            elif flat and abs(mark / float(anchor) - 1) > REANCHOR_FLAT_PCT:
                self.reanchor(symbol, st, mark)
                anchor = mark

            # topes de inventario
            inv_sym = abs(amt) * mark
            capped = (inv_sym >= MAX_INVENTORY_USD_PER_SYMBOL
                      or total_inv >= MAX_INVENTORY_USD_TOTAL)
            blocked = (self._halted or capped)
            if capped and not self._halted:
                log({"symbol": symbol, "action": "inventory_cap",
                     "inventory_sym_usd": round(inv_sym, 2),
                     "inventory_total_usd": round(total_inv, 2),
                     "side_blocked": ("SELL" if amt < 0 else "BUY") if amt else "ambos",
                     "note": "se bloquea el lado que AUMENTA la exposicion; "
                             "la cobertura sigue permitida"})

            self.ensure_grid(symbol, st, mark, blocked, amt=amt,
                             halted=self._halted)

            # posicion viva que el grid no puede gestionar: los 8 niveles
            # estan al lado equivocado del mark (el precio se fue del ancla)
            # y no hay ninguno colocado. Sin este aviso el bot la lleva
            # colgada sin que nadie lo note.
            if amt and not st.get("slots"):
                log({"symbol": symbol, "action": "unmanaged_position",
                     "amt": amt, "notional": round(inv_sym, 2),
                     "anchor": st.get("anchor"), "mark": mark,
                     "drift_pct": round((mark / float(st["anchor"]) - 1) * 100, 2)
                     if st.get("anchor") else None})

        save_state(self.state)

    def net_exposure(self) -> dict:
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
                    help="espaciado en fraccion (0.005 = 0.5%%)")
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
        pos = {p["symbol"]: p for p in client.positions()
               if float(p.get("positionAmt", 0)) != 0}
        for sym in symbols:
            st = bot.sym_state(sym)
            n = len(st.get("slots", {}))
            try:
                live = len(client.open_orders(sym))
            except BinanceError:
                live = -1
            p = pos.get(sym)
            inv = abs(float(p["positionAmt"])) * float(p["markPrice"]) if p else 0.0
            print(f"  {sym:14s} ancla={st.get('anchor')} slots={n} abiertas={live} "
                  f"inventario={inv:.0f} USDT")
        return 0

    print(
        f"Neutral grid bot v2 | simbolos={len(symbols)} | niveles/lado={a.levels} | "
        f"espaciado={a.spacing*100:.2f}% | notional/nivel={a.notional} USDT | "
        f"topes={MAX_INVENTORY_USD_PER_SYMBOL}/simbolo {MAX_INVENTORY_USD_TOTAL} total | "
        f"stop={STOP_LOSS_PCT*100:.0f}% | "
        f"{'EJECUTANDO EN TESTNET' if a.execute else 'DRY-RUN'}",
        flush=True,
    )
    bot.run(loop=a.loop, interval=a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
