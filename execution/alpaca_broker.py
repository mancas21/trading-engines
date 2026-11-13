"""Cliente del broker Alpaca (acciones US) orquestando su CLI oficial.

No usa la API REST ni guarda claves: la autenticacion vive en el perfil del CLI
(`alpaca profile login`, lo hace el operador una vez) y este modulo solo invoca
el binario. El binario se localiza con `ALPACA_CLI`, luego en PATH, y por ultimo
en `%USERPROFILE%\\go\\bin\\alpaca.exe`.

Reglas de seguridad, por orden de importancia (espejo de binance_testnet.py):
  1. DRY-RUN por defecto. Enviar una orden exige `ALLOW_ALPACA_ORDERS=1` en el
     entorno Y `dry_run=False` explicito en la llamada.
  2. Lista blanca de simbolos (`ALLOW_ALPACA_SYMBOLS`, por defecto AAPL,SPY).
  3. Tope de nocional por orden (`MAX_ALPACA_NOTIONAL_USD`, por defecto 1000).
     Los cierres (position close) no lo usan: el tope limita exposicion NUEVA,
     no impide salir.
  4. Este cliente no toca claves: no hay secretos que enmascarar ni en el .env
     ni en los logs. Cada intento se registra en `logs/alpaca_orders.jsonl`.
  5. Mercado cerrado no entra (las acciones no son 24/7): el guard de horario
     es decision del router, aqui solo expongo `clock()`.

Diferencias con la pata crypto que hay que conocer:
  * El bracket de Alpaca es ATOMICO: entrada + stop + TP se aceptan o se
    rechazan juntos. Si el entry llena, el stop ya vive en el exchange.
  * Qty ENTERA para entradas: los brackets fraccionales no son un camino que el
    repo quiera pisar. El sizing redondea a shares enteras y avisa si el
    redondeo deja el riesgo por debajo del 10% del objetivo.
  * La comision de acciones es 0: el coste real es spread (y en live, fees
    regulatorias; en paper no existen).
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from binance_testnet import load_env  # mismo .env, mismas convenciones

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
LOG_FILE = LOG_DIR / "alpaca_orders.jsonl"

DEFAULT_CLI = Path.home() / "go" / "bin" / "alpaca.exe"

RISK_PCT_DEFAULT = 0.5
RISK_PCT_MAX = 1.0


# ──────────────────────────────────────────────────────────────── errores ──
class AlpacaError(RuntimeError):
    """El CLI fallo (exit != 0, envelope de error, o salida no-JSON)."""


class SafetyError(RuntimeError):
    """Una regla local ha bloqueado la orden antes de salir a la red."""


# ─────────────────────────────────────────────────────────────────── cli ──
def find_cli() -> Path:
    """ALPACA_CLI > PATH > ~/go/bin/alpaca.exe. SafetyError si no esta."""
    env = os.environ.get("ALPACA_CLI", "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise SafetyError(f"ALPACA_CLI={env} no existe")
    w = shutil.which("alpaca")
    if w:
        return Path(w)
    if DEFAULT_CLI.is_file():
        return DEFAULT_CLI
    raise SafetyError(
        "no encuentro el CLI de Alpaca: instala el binario o exporta ALPACA_CLI")


def parse_cli_output(text: str, returncode: int) -> dict | list:
    """Salida del CLI -> JSON. El CLI mete los errores en stdout como
    {"code":0,"error":"...","status":0} con exit=1; tambien se revisa el exit."""
    if returncode != 0:
        # el envelope de error viaja en stdout; intenta extraerlo para el mensaje
        try:
            j = json.loads(text)
            if isinstance(j, dict) and j.get("error"):
                raise AlpacaError(f"CLI: {j['error']}") from None
        except json.JSONDecodeError:
            pass
        raise AlpacaError(f"CLI exit={returncode}: {text.strip()[:300]}")
    try:
        j = json.loads(text)
    except json.JSONDecodeError:
        raise AlpacaError(f"CLI devolvio no-JSON: {text.strip()[:300]}") from None
    if isinstance(j, dict) and j.get("error"):
        raise AlpacaError(f"CLI: {j['error']}")
    return j


# ─────────────────────────────────────────────────────── logica pura ──
def round_qty(qty: float, fractionable: bool) -> float:
    """Fraccional: 4 decimales hacia abajo. Entero: shares completas."""
    if fractionable:
        step = Decimal("0.0001")
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return float(q)
    return float(int(qty))


def size_order(*, equity: float, cash: float, price: float, stop: float,
               risk_pct: float, max_notional: float) -> tuple[float, str]:
    """Tamano por RIESGO (misma decision que router.py): minimo entre
    riesgo/stop, tope de nocional y caja disponible. Devuelve (qty, restriccion).

    La caja usa `cash` (sin apalancamiento) aunque la cuenta sea margin: el
    router no usa leverage, y en cash account es lo que hay.
    """
    if price <= 0:
        raise SafetyError(f"precio invalido: {price}")
    dist = abs(price - stop)
    if dist <= 0:
        raise SafetyError("stop == precio de referencia: distancia de riesgo nula")
    qty_risk = equity * risk_pct / 100.0 / dist
    qty_notional = max_notional / price
    qty_cash = cash / price
    binding, qty = min((("riesgo", qty_risk), ("nocional", qty_notional),
                        ("caja", qty_cash)), key=lambda kv: kv[1])
    return qty, binding


def entry_guard(clock: dict, *, allow_closed: bool = False) -> tuple[bool, str]:
    """Mercado cerrado = no entra. Las ordenes day en cola llenarian en la
    apertura del dia siguiente con un stop pensado para el precio de hoy."""
    if clock.get("is_open"):
        return True, "abierto"
    if allow_closed:
        return True, "cerrado pero allow_closed"
    return False, (f"mercado cerrado (now {clock.get('timestamp')}, "
                   f"next_open {clock.get('next_open')})")


def stop_side_check(side: str, stop: float, ref: float,
                    take_profit: float | None = None) -> tuple[bool, str]:
    """El stop y el TP de un bracket deben quedar del lado correcto del precio
    de referencia (ultimo trade). Si no, el bracket se rechazaria (o peor: se
    dispararia al instante)."""
    if side == "LONG":
        if stop >= ref:
            return False, f"LONG con stop {stop:g} >= ref {ref:g}"
        if take_profit is not None and take_profit <= ref:
            return False, f"LONG con tp {take_profit:g} <= ref {ref:g}"
    elif side == "SHORT":
        if stop <= ref:
            return False, f"SHORT con stop {stop:g} <= ref {ref:g}"
        if take_profit is not None and take_profit >= ref:
            return False, f"SHORT con tp {take_profit:g} >= ref {ref:g}"
    else:
        return False, f"side invalido: {side}"
    return True, "ok"


def bracket_args(*, symbol: str, qty: float, side: str, stop: float,
                 take_profit: float | None = None,
                 client_id: str | None = None,
                 time_in_force: str = "day") -> list[str]:
    """Argumentos de `alpaca order submit` para un bracket de mercado.

    LONG -> side buy; SHORT -> side sell. El stop/TP van en las patas del
    bracket: Alpaca invierte el lado de las patas solo, no se les pasa side.
    """
    if side not in ("LONG", "SHORT"):
        raise SafetyError(f"side invalido: {side}")
    if qty <= 0:
        raise SafetyError(f"qty invalido: {qty}")
    args = ["order", "submit",
            "--symbol", symbol,
            "--qty", f"{qty:g}",
            "--side", "buy" if side == "LONG" else "sell",
            "--type", "market",
            "--order-class", "bracket",
            "--time-in-force", time_in_force,
            "--stop-loss", json.dumps({"stop_price": stop}),
            "--client-order-id", client_id or f"qae-ent-{int(time.time()*1000)}"]
    if take_profit is not None:
        args += ["--take-profit", json.dumps({"limit_price": take_profit})]
    return args


# ──────────────────────────────────────────────────────────────── cliente ──
class AlpacaClient:
    def __init__(self, *, dry_run: bool = True,
                 max_notional_usd: float | None = None,
                 allow_symbols: list[str] | None = None,
                 cli: str | Path | None = None):
        load_env()
        self.cli = Path(cli or os.environ.get("ALPACA_CLI") or find_cli())
        self.dry_run = dry_run
        env_allow = os.environ.get("ALLOW_ALPACA_ORDERS", "0") == "1"
        self.orders_enabled = env_allow and not dry_run
        self.max_notional = float(max_notional_usd or os.environ.get(
            "MAX_ALPACA_NOTIONAL_USD", "1000"))
        syms = os.environ.get("ALLOW_ALPACA_SYMBOLS", "AAPL,SPY")
        self.allow_symbols = allow_symbols or [s.strip().upper() for s in syms.split(",") if s.strip()]
        self._assets: dict[str, dict] = {}

    # ── infraestructura ────────────────────────────────────────────────
    def run(self, args: list[str], timeout: int = 30) -> dict | list:
        proc = subprocess.run([str(self.cli), *args, "-q"],
                              capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout)
        return parse_cli_output(proc.stdout, proc.returncode)

    # ── lectura ────────────────────────────────────────────────────────
    def account(self) -> dict:
        return self.run(["account", "get"])

    def clock(self) -> dict:
        return self.run(["clock"])

    def positions(self) -> list:
        p = self.run(["position", "list"])
        return p if isinstance(p, list) else [p]

    def position(self, symbol: str) -> dict | None:
        for p in self.positions():
            if str(p.get("symbol", "")).upper() == symbol.upper():
                return p
        return None

    def open_orders(self, symbol: str | None = None) -> list:
        args = ["order", "list", "--status", "open", "--limit", "100"]
        if symbol:
            args += ["--symbols", symbol.upper()]
        o = self.run(args)
        return o if isinstance(o, list) else [o]

    def order(self, order_id: str) -> dict:
        return self.run(["order", "get", "--order-id", str(order_id)])

    def asset(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if symbol not in self._assets:
            self._assets[symbol] = self.run(
                ["asset", "get", "--symbol-or-asset-id", symbol])
        return self._assets[symbol]

    def last_trade(self, symbol: str) -> float:
        j = self.run(["data", "latest-trade", "--symbol", symbol.upper()])
        return float(j["trade"]["p"])

    def quote(self, symbol: str) -> dict:
        j = self.run(["data", "latest-quote", "--symbol", symbol.upper()])
        qt = j.get("quote", j) if isinstance(j, dict) else {}
        return {"bid": float(qt.get("bp", 0)), "ask": float(qt.get("ap", 0))}

    # ── validacion ─────────────────────────────────────────────────────
    def check_order(self, symbol: str, side: str, qty: float,
                    ref_price: float | None = None) -> dict:
        """Valida contra la lista blanca, el asset y los topes locales.

        A diferencia de Binance, aqui no hay filtros de tick/step que leer: la
        regla local es qty entera para brackets, whitelist y tope de nocional.
        `ref_price` es el ultimo trade; si falta, se pide.
        """
        symbol, side = symbol.upper(), side.upper()
        if side not in ("BUY", "SELL"):
            raise SafetyError(f"side invalido: {side}")
        if symbol not in self.allow_symbols:
            raise SafetyError(
                f"{symbol} no esta en ALLOW_ALPACA_SYMBOLS={self.allow_symbols}")
        a = self.asset(symbol)
        if not a.get("tradable"):
            raise SafetyError(f"{symbol} tradable=false")
        if a.get("status") != "active":
            raise SafetyError(f"{symbol} status={a.get('status')}")
        q = round_qty(qty, fractionable=False)  # brackets: qty entera
        if q < 1:
            raise SafetyError(
                f"qty {qty:g} redondea a 0 shares (entera por bracket); "
                "sube el riesgo o el tope de nocional")
        if ref_price is None:
            ref_price = self.last_trade(symbol)
        notional = q * ref_price
        if notional > self.max_notional:
            raise SafetyError(
                f"nocional {notional:.2f} > MAX_ALPACA_NOTIONAL_USD "
                f"{self.max_notional:.2f}")
        return {"symbol": symbol, "side": side, "qty": q, "price": ref_price,
                "notional": notional, "asset": a}

    # ── escritura ──────────────────────────────────────────────────────
    def place_bracket(self, *, symbol: str, side: str, qty: float,
                      stop: float, take_profit: float | None = None,
                      dry_run: bool | None = None) -> dict:
        """Bracket ATOMICO de mercado: entrada + stop + TP en una submission.

        `stop`/`take_profit` son precios absolutos; la coherencia de lados
        (stop < ref para LONG, etc.) la valida el router con stop_side_check.
        Si la submission se acepta, el stop ya vive en el exchange aunque este
        proceso muera.
        """
        dry = self.dry_run if dry_run is None else dry_run
        chk = self.check_order(symbol, "BUY" if side == "LONG" else "SELL", qty)
        args = bracket_args(symbol=chk["symbol"], qty=chk["qty"], side=side,
                            stop=stop, take_profit=take_profit)
        return self._submit(args, notional=chk["notional"], dry=dry, tag="entry")

    def close_position(self, symbol: str, dry_run: bool | None = None) -> dict:
        """Cierre de emergencia via position close (exento del tope de nocional)."""
        dry = self.dry_run if dry_run is None else dry_run
        symbol = symbol.upper()
        if symbol not in self.allow_symbols:
            raise SafetyError(
                f"{symbol} no esta en ALLOW_ALPACA_SYMBOLS={self.allow_symbols}")
        args = ["position", "close", "--symbol-or-asset-id", symbol]
        return self._submit(args, notional=0.0, dry=dry, tag="close")

    def cancel(self, order_id: str) -> dict:
        """Cancela una orden abierta. HTTP 204 (sin cuerpo) si va bien."""
        if not self.orders_enabled:
            return {"dry_run": True, "would_send": ["order", "cancel", "--order-id", str(order_id)],
                    "reason": "ALLOW_ALPACA_ORDERS!=1"}
        resp = self.run(["order", "cancel", "--order-id", str(order_id)])
        self._log({"ts": _utc(), "mode": "SEND", "tag": "cancel",
                   "params": ["order", "cancel", "--order-id", str(order_id)],
                   "result": resp if resp else "204"})
        return resp

    def _submit(self, args: list[str], *, notional: float, dry: bool,
                tag: str) -> dict:
        explicit_dry = dry
        gated = not self.orders_enabled
        dry = explicit_dry or gated
        rec = {"ts": _utc(), "mode": "DRY-RUN" if dry else "SEND",
               "cli": str(self.cli), "tag": tag, "params": args,
               "notional": notional}
        if dry:
            rec["result"] = "not_sent"
            self._log(rec)
            return {"dry_run": True, "would_send": args, "notional": notional,
                    "reason": "dry_run=True" if explicit_dry
                    else "ALLOW_ALPACA_ORDERS!=1"}
        resp = self.run(args)
        rec["result"] = resp.get("id") if isinstance(resp, dict) else resp
        rec["result_status"] = resp.get("status") if isinstance(resp, dict) else None
        self._log(rec)
        return resp

    @staticmethod
    def _log(rec: dict) -> None:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fmt(v: float | None, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:,.{nd}f}"
