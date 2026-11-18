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
  * Tope de inventario por simbolo (240 USD) y total (720 USD): al llegar,
    se bloquea el lado que AUMENTA la exposicion y se cancelan sus slots.
    La cobertura del lado contrario siempre se permite.
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
import math
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
NOTIONAL_PER_SIDE_USDT = 60     # USDT por nivel (era 100)
MAX_ORDER_NOTIONAL_USD = 650
POLL_SECONDS = 20

# 2026-11-17 — el 17-nov una subida de 0,5-2% en los 9 pares a la vez lleno
# 12 niveles en 13 minutos y dejo el libro en 1.212 USDT (tope 1.200). El
# tope funciono, pero el libro era demasiado grande para un movimiento
# correlacionado. Con 60/nivel el peor caso es 720 USDT (15% de la equity) y
# un stop-out completo ~29 USDT (0,6%), dentro de la guardia diaria del 2%.
#
# LA POLITICA VIVE EN risk_guard (un solo sitio desde 2026-11-17): aqui solo
# se importa. Antes este fichero llevaba sus propios 400/1.200 mientras
# risk_guard decia 600/400 — dos cerebros, dos numeros, y ningun consumidor
# podia saber cual describia la cuenta.
MAX_INVENTORY_USD_PER_SYMBOL = risk_guard.MAX_INVENTORY_USD_PER_SYMBOL
MAX_INVENTORY_USD_TOTAL = risk_guard.MAX_INVENTORY_USD_TOTAL
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


class StateCorrupt(RuntimeError):
    """El estado existe pero no se puede leer. NO se arranca con estado vacio.

    2026-11-17 — `load_state()` devolvia un estado FRESCO ante cualquier
    OSError o JSONDecodeError. Con grid_state.json corrupto (o bloqueado por
    otro proceso) el bot arrancaba creyendo que no tenia NINGUNA orden puesta
    y colocaba un grid completo encima del que ya estaba vivo en el exchange:
    duplicaba cada nivel. El estado de posiciones de `state_store.py` ya habia
    aprendido esta leccion (StateCorrupt); este fichero no. La ausencia del
    fichero si es "sin estado" (primer arranque); un fichero ilegible no.
    """


def load_state() -> dict:
    if not STATE.exists():
        return _fresh_state()          # primer arranque: no hay estado, correcto
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise StateCorrupt(f"{STATE}: {e}") from e
    if not isinstance(d, dict) or "symbols" not in d:
        # estado v1 (slots que cambiaban de lado): se tira a proposito, esta
        # documentado en el historial. No es corrupcion.
        return _fresh_state()
    d.setdefault("cooldown", {})
    return d


def save_state(d: dict) -> bool:
    """Escribe el estado de forma atomica. True si quedo en disco.

    Si devuelve False, el llamador NO puede seguir colocando: las ordenes ya
    enviadas no estarian registradas y el siguiente ciclo las duplicaria.
    """
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE)
        return True
    except OSError as e:
        log({"symbol": "-", "action": "state_save_failed", "error": str(e)[:200]})
        return False


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
        self._log_seen: dict[str, float] = {}

    # ---- helpers ----------------------------------------------------------
    def sym_state(self, symbol: str) -> dict:
        return self.state["symbols"].setdefault(symbol, {"anchor": None, "slots": {}})

    def rules(self, symbol: str) -> dict:
        if symbol not in self._rules:
            self._rules[symbol] = self.client.rules(symbol)
        return self._rules[symbol]

    def _log_throttled(self, key: str, rec: dict, every: float = 600.0) -> None:
        """Evita escribir el mismo aviso cada 20 s (3,7 MB de log en 2 dias)."""
        now = time.time()
        if now - self._log_seen.get(key, 0.0) < every:
            return
        self._log_seen[key] = now
        log(rec)

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

    def sync_slots(self, symbol: str, st: dict) -> dict:
        """Detecta fills, ordenes muertas y ordenes que el estado NO conoce.

        Devuelve {"fills": n, "orphans": [ids], "known": bool}.

        `known=False`: no se pudo leer el libro de ordenes. El llamador no
        puede colocar nada en ese simbolo: no sabria si duplica.

        `orphans`: ordenes vivas en el exchange que no estan registradas en
        state/grid_state.json. Antes se leian solo las ordenes muertas; una
        orden viva SIN registro pasaba desapercibida y el ciclo siguiente
        colocaba otra vez el mismo nivel: dos ordenes al mismo precio, es
        decir el doble de exposicion en ese nivel. MEDIDO el 17-nov: la
        cuenta tenia 63 ordenes abiertas y el estado 28 slots. El estado ya
        no es la unica entrada: si el libro tiene algo que el estado no
        conoce, el bot no coloca y avisa.
        """
        out = {"fills": 0, "orphans": [], "known": True}
        if self.dry_run:
            return out
        try:
            live = {int(o["orderId"]) for o in self.client.open_orders(symbol)}
        except (BinanceError, SafetyError) as e:
            log({"symbol": symbol, "action": "error", "error": f"open_orders: {e}"[:200]})
            out["known"] = False
            return out

        slots = st.get("slots") or {}
        for key in list(slots):
            rec = slots[key]
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
                out["fills"] += 1
                slots.pop(key, None)
            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                log({"symbol": symbol, "action": "lost_slot", "level": key,
                     "status": status, "executed_qty": executed})
                slots.pop(key, None)

        registered = {int(r.get("order_id") or 0)
                      for r in (st.get("slots") or {}).values()}
        out["orphans"] = sorted(i for i in live if i not in registered)
        return out

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
                    total_inv: float = 0.0, halted: bool = False) -> None:
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

        # Un step puede valer mas que el nocional objetivo (BTC: 0.001 paso
        # x 64.800 = 64,8 USDT). Sin este minimo el redondeo da 0 y el
        # simbolo se queda sin grid para siempre.
        qty = self.client.round_qty(
            symbol, max(self.notional_per_side / mark, float(r.get("step") or 0)))
        if qty <= 0 or qty * mark < r.get("min_notional", 0):
            log({"symbol": symbol, "action": "skip",
                 "why": f"qty {qty} no cumple min_notional {r.get('min_notional')}"})
            return

        # Tope de inventario: se retiran los slots vivos del lado que aumenta
        # la exposicion. Sin esto el tope solo impedia colocar, no absorber:
        # el bot seguia agrandando la posicion con las ordenes que ya estaban
        # puestas en el libro. Cancelar reduce riesgo: se hace tambien con el
        # kill switch activo (lo que no se hace es COLOCAR nada nuevo).
        if blocked:
            for key in list(st.get("slots", {})):
                if self.increases_exposure(key[:3], amt):
                    self.cancel_slot(symbol, st, key)

        inv_sym = abs(amt) * mark
        placed = 0
        for i in range(1, self.grid_levels + 1):
            for side, sign in (("BUY", -1), ("SELL", +1)):
                key = f"{side}{i}"
                if key in st["slots"]:
                    continue
                if halted:
                    continue
                inc = self.increases_exposure(side, amt)
                if blocked and inc:
                    continue
                # tope prospectivo: no colocar la orden que romperia el tope
                if inc and (inv_sym + self.notional_per_side > MAX_INVENTORY_USD_PER_SYMBOL
                            or total_inv + self.notional_per_side > MAX_INVENTORY_USD_TOTAL):
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
        try:
            mark = float(pos.get("markPrice"))
        except (TypeError, ValueError):
            mark = 0.0
        if not (math.isfinite(mark) and mark > 0):
            # 2026-11-17 — antes `or 0` dejaba notional=0 < FLAT_USD y el stop
            # se creia "plano": el unico freno por perdida quedaba desactivado
            # en silencio. Sin precio no se mide el dano; el ciclo ya bloquea
            # colocaciones por exposicion no verificable.
            self._log_throttled(f"{symbol}:stopnoprice", {
                "symbol": symbol, "action": "stop_unverifiable",
                "note": "mark ilegible: el stop NO se evalua"})
            return False
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
        account_ok = True
        try:
            equity = float(self.client.account().get("totalMarginBalance", 0) or 0)
        except (BinanceError, SafetyError) as e:
            # 2026-11-17 — antes `pass`: equity quedaba en 0, `if equity > 0`
            # se saltaba la guardia diaria EN SILENCIO y el bot seguia
            # operando sin saber si el dia ya estaba roto.
            account_ok = False
            self._log_throttled("account:fail", {
                "symbol": "-", "action": "account_unavailable",
                "error": str(e)[:200],
                "note": "guardia diaria NO evaluada; no se coloca riesgo nuevo"})
        if account_ok and equity > 0 and not self.dry_run:
            daily = risk_guard.daily_check(equity)
            if daily.get("tripped") and not self._halted:
                log({"symbol": "-", "action": "daily_guard",
                     "why": daily.get("why"), "equity": round(equity, 2)})
            if daily.get("tripped"):
                self._halted = True
        if risk_guard.kill_active():
            self._halted = True

        # 2) exposicion: UNA fuente (risk_guard.exposure_snapshot), fail-closed
        snap = None
        try:
            snap = risk_guard.exposure_snapshot(self.client)
        except (BinanceError, SafetyError) as e:
            # 2026-11-17 — antes `except: pass` dejaba positions={} y
            # total_inv=0: los topes de inventario DESAPARECIAN justo cuando
            # no se podia leer la cuenta, y el bot colocaba sin limite.
            self._log_throttled("positions:fail", {
                "symbol": "-", "action": "positions_unavailable",
                "error": str(e)[:200],
                "note": "exposicion desconocida: NO se coloca riesgo nuevo"})
        positions: dict[str, dict] = {}
        total_inv = 0.0
        exposure_ok = snap is not None and bool(snap.get("verifiable"))
        if snap is not None:
            total_inv = float(snap["gross"])
            for sym, v in snap["positions"].items():
                positions[sym] = {"symbol": sym, "positionAmt": v["amt"],
                                  "markPrice": v["mark"],
                                  "unRealizedProfit": v["upnl"]}
            unpriced = list(snap.get("unpriced") or [])
            if unpriced:
                self._log_throttled("exposure:unpriced", {
                    "symbol": "-", "action": "exposure_unverifiable",
                    "unpriced": unpriced,
                    "note": "precio ilegible: la exposicion es DESCONOCIDA, "
                            "no cero; no se coloca riesgo nuevo"})

        now = time.time()
        cooldown = self.state["cooldown"]
        halt_cycle = bool(self._halted or not exposure_ok or not account_ok)

        for symbol in self.symbols:
            if symbol not in self.allow_symbols:
                log({"symbol": symbol, "action": "skip", "why": "fuera de ALLOW_SYMBOLS"})
                continue
            st = self.sym_state(symbol)

            # fills / ordenes muertas / ordenes que el estado no conoce
            sync = self.sync_slots(symbol, st)
            desync = bool(sync["orphans"]) or not sync["known"]
            if sync["orphans"]:
                self._log_throttled(f"{symbol}:desync", {
                    "symbol": symbol, "action": "state_desync",
                    "orphans": len(sync["orphans"]),
                    "order_ids": sync["orphans"][:10],
                    "slots": len(st.get("slots") or {}),
                    "note": "ordenes vivas que el estado NO conoce: no se coloca "
                            "en este simbolo hasta reconciliar "
                            "(python execution/audit_sync.py --repair)"})
            elif not sync["known"]:
                self._log_throttled(f"{symbol}:nobook", {
                    "symbol": symbol, "action": "orderbook_unavailable",
                    "note": "no se pudo leer el libro: no se coloca en este simbolo"})

            if snap is not None and symbol in (snap.get("unpriced") or []):
                continue          # sin precio no hay stop, ni tope, ni grid

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
                if not save_state(self.state):
                    halt_cycle = True

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
            if capped and not self._halted:
                self._log_throttled(f"{symbol}:cap", {
                    "symbol": symbol, "action": "inventory_cap",
                    "inventory_sym_usd": round(inv_sym, 2),
                    "inventory_total_usd": round(total_inv, 2),
                    "side_blocked": ("SELL" if amt < 0 else "BUY") if amt else "ambos",
                    "note": "se bloquea el lado que AUMENTA la exposicion; "
                            "la cobertura sigue permitida"})

            # 2026-11-17: `desync` NO cancela (un libro que no se pudo leer no
            # autoriza acciones) pero SI impide colocar. `halted` = no colocar;
            # `blocked` = retirar el lado que aumenta.
            self.ensure_grid(symbol, st, mark,
                             blocked=bool(capped or self._halted),
                             amt=amt, total_inv=total_inv,
                             halted=bool(halt_cycle or desync))

            # posicion viva que el grid no puede gestionar: los 8 niveles
            # estan al lado equivocado del mark (el precio se fue del ancla)
            # y no hay ninguno colocado. Sin este aviso el bot la lleva
            # colgada sin que nadie lo note.
            if amt and not st.get("slots"):
                self._log_throttled(f"{symbol}:unmanaged", {
                    "symbol": symbol, "action": "unmanaged_position",
                    "amt": amt, "notional": round(inv_sym, 2),
                    "anchor": st.get("anchor"), "mark": mark,
                    "drift_pct": round((mark / float(st["anchor"]) - 1) * 100, 2)
                    if st.get("anchor") else None})

        if not save_state(self.state):
            # las ordenes de este ciclo no quedaron registradas: el proximo
            # sync las vera como huerfanas y el gate de desync parara el
            # simbolo. Se avisa ahora para que no sorprenda.
            log({"symbol": "-", "action": "state_save_failed_cycle",
                 "note": "estado no persistido: el proximo ciclo parara los "
                         "simbolos con ordenes no registradas"})

    def net_exposure(self) -> dict | None:
        """Posiciones netas. None = no se pudo leer (NUNCA {} = 'plano')."""
        out = {}
        try:
            for p in self.client.positions():
                a = float(p.get("positionAmt", 0))
                if a != 0:
                    out[p["symbol"]] = a
        except (BinanceError, SafetyError):
            return None
        return out

    def consistency(self) -> dict:
        """Estado vs exchange, SOLO LECTURA (no poda slots ni escribe nada).

        Es la precondicion de arranque: si `ok` es False el bot no coloca en
        los simbolos afectados hasta reconciliar. `unregistered_state` son
        slots del estado que ya no estan vivos en el libro (fills sin procesar
        o ordenes muertas); no invalidan por si solos, se informan.
        """
        snap = risk_guard.exposure_snapshot(self.client)
        out = {"ok": True, "gross": snap["gross"], "net": snap["net"],
               "verifiable": snap["verifiable"],
               "unpriced": list(snap.get("unpriced") or []),
               "caps": snap["caps"], "symbols": {}}
        if not snap["verifiable"]:
            out["ok"] = False
        for symbol in self.symbols:
            if symbol not in self.allow_symbols:
                continue
            st = (self.state.get("symbols") or {}).get(symbol) or {}
            registered = {int(r.get("order_id") or 0)
                          for r in (st.get("slots") or {}).values()}
            try:
                live = {int(o["orderId"]) for o in self.client.open_orders(symbol)}
            except (BinanceError, SafetyError) as e:
                out["symbols"][symbol] = {"error": str(e)[:120], "known": False}
                out["ok"] = False
                continue
            orphans = sorted(live - registered)
            if orphans:
                out["ok"] = False
            out["symbols"][symbol] = {
                "known": True,
                "slots": len(registered),
                "live": len(live),
                "orphans": orphans,
                "unregistered_state": sorted(registered - live),
            }
        return out

    def run(self, loop: bool = False, interval: int = POLL_SECONDS) -> None:
        if not proclock.acquire(LOCK):
            print(f"ya hay un neutral_bot vivo (pid {proclock.holder(LOCK)})")
            return
        try:
            while True:
                try:
                    self.run_cycle()
                except Exception as e:  # noqa: BLE001
                    # 2026-11-17: un TimeoutError de SSL mataba el proceso (~1
                    # muerte/dia). En loop se registra y se sigue; en one-shot
                    # se relanza para que el fallo sea visible.
                    log({"symbol": "-", "action": "cycle_error",
                         "error": f"{type(e).__name__}: {e}"[:200]})
                    if not loop:
                        raise
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
    ap.add_argument("--check", action="store_true",
                    help="precondicion: estado vs exchange, sin tocar nada (rc=1 si descuadra)")
    a = ap.parse_args()

    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    client = TestnetClient(dry_run=not a.execute, max_notional_usd=MAX_ORDER_NOTIONAL_USD)
    client.sync_time()

    try:
        bot = NeutralGridBot(
            client=client, symbols=symbols, grid_levels=a.levels,
            grid_spacing_pct=a.spacing, notional_per_side_usdt=a.notional,
            dry_run=not a.execute,
        )
    except StateCorrupt as e:
        # 2026-11-17: arrancar con estado vacio significaba colocar un grid
        # completo encima del que ya estaba vivo. Mejor no arrancar.
        print(f"CRITICO: grid_state.json ilegible; NO se arranca con estado vacio.\n  {e}")
        print("  Inspecciona execution/state/grid_state.json y reconcilia con")
        print("  python execution/audit_sync.py --repair")
        return 6

    if a.check:
        c = bot.consistency()
        print(f"exposicion bruta={c['gross']:.2f} neta={c['net']:+.2f} "
              f"verificable={c['verifiable']} topes={c['caps']['per_symbol']:.0f}/"
              f"{c['caps']['total']:.0f}")
        if c["unpriced"]:
            print(f"  SIN PRECIO: {', '.join(c['unpriced'])}")
        for sym, r in c["symbols"].items():
            if not r.get("known"):
                print(f"  {sym:14s} LIBRO ILEGIBLE: {r.get('error')}")
            elif r["orphans"]:
                print(f"  {sym:14s} DESINCRONIZADO slots={r['slots']} vivas={r['live']} "
                      f"huerfanas={r['orphans']}")
            elif r["unregistered_state"]:
                print(f"  {sym:14s} ok (slots del estado ya no vivos: "
                      f"{r['unregistered_state']})")
        print("OK" if c["ok"] else "DESINCRONIZADO")
        return 0 if c["ok"] else 1

    if a.status:
        exp = bot.net_exposure()
        if exp is None:
            print("exposicion neta: DESCONOCIDA (no se pudo leer la cuenta)")
        else:
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
    log({"symbol": "-", "action": "startup", "symbols": len(symbols),
         "levels": a.levels, "notional": a.notional,
         "execute": a.execute, "interval": a.interval})
    bot.run(loop=a.loop, interval=a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
