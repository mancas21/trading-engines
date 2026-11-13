"""Cliente firmado para el testnet de Binance USDⓈ-M Futures.

Solo stdlib: no depende de `requests` ni de `ccxt`.

Reglas de seguridad, por orden de importancia:
  1. DRY-RUN por defecto. Enviar una orden exige `ALLOW_ORDERS=1` en el entorno
     Y `dry_run=False` explicito en la llamada.
  2. Lista blanca de simbolos (`ALLOW_SYMBOLS`, por defecto XRPUSDT).
  3. Tope de nocional por orden (`MAX_ORDER_NOTIONAL_USD`, por defecto 100).
  4. Las claves NUNCA se imprimen: `mask()` deja solo los ultimos 4 caracteres.
  5. Cada intento se escribe en `logs/orders.jsonl` sin secretos.

El host es configurable (`BINANCE_TESTNET_BASE`) para poder apuntar al demo de
produccion (`https://demo-fapi.binance.com`) sin tocar codigo.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
LOG_DIR = HERE / "logs"

DEFAULT_BASE = "https://testnet.binancefuture.com"
RECV_WINDOW_MS = 5000


# ─────────────────────────────────────────────────────────────── entorno ──
def load_env(path: Path | None = None) -> None:
    """Carga KEY=VALUE del .env local sin pisar variables ya definidas."""
    p = path or ENV_FILE
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def mask(s: str | None) -> str:
    if not s:
        return "(vacia)"
    if len(s) <= 8:
        return "*" * len(s)
    return f"{'*' * 8}...{s[-4:]} (len {len(s)})"


# ──────────────────────────────────────────────────────────────── errores ──
class BinanceError(RuntimeError):
    def __init__(self, status: int, code, msg: str):
        super().__init__(f"HTTP {status} code={code} msg={msg}")
        self.status = status
        self.code = code
        self.msg = msg


class SafetyError(RuntimeError):
    """Una regla local ha bloqueado la orden antes de salir a la red."""


# ──────────────────────────────────────────────────────────────── cliente ──
class TestnetClient:
    def __init__(self, *, base: str | None = None, dry_run: bool = True,
                 max_notional_usd: float | None = None,
                 allow_symbols: list[str] | None = None):
        load_env()
        self.base = (base or os.environ.get("BINANCE_TESTNET_BASE") or DEFAULT_BASE).rstrip("/")
        self.key = os.environ.get("BINANCE_TESTNET_KEY", "").strip()
        self.secret = os.environ.get("BINANCE_TESTNET_SECRET", "").strip()
        self.dry_run = dry_run
        env_allow = os.environ.get("ALLOW_ORDERS", "0") == "1"
        self.orders_enabled = env_allow and not dry_run
        self.max_notional = float(max_notional_usd or os.environ.get(
            "MAX_ORDER_NOTIONAL_USD", "100"))
        syms = os.environ.get("ALLOW_SYMBOLS", "XRPUSDT")
        self.allow_symbols = allow_symbols or [s.strip().upper() for s in syms.split(",") if s.strip()]
        self._time_offset_ms = 0
        self._filters: dict[str, dict] = {}

    # ── infraestructura ────────────────────────────────────────────────
    def has_keys(self) -> bool:
        return bool(self.key and self.secret)

    def _request(self, method: str, path: str, params: dict | None = None,
                 *, signed: bool = False) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            if not self.has_keys():
                raise SafetyError(
                    "faltan BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET "
                    f"(ponlas en {ENV_FILE})")
            params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            params["recvWindow"] = RECV_WINDOW_MS
            qs = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
            qs = f"{qs}&signature={sig}"
        else:
            qs = urllib.parse.urlencode(params)

        url = f"{self.base}{path}"
        data = None
        if method.upper() in ("POST", "PUT", "DELETE"):
            data = qs.encode()
        elif qs:
            url = f"{url}?{qs}"

        req = urllib.request.Request(url, data=data, method=method.upper())
        if self.has_keys():
            req.add_header("X-MBX-APIKEY", self.key)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", "qae-testnet/1.0")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                j = json.loads(body)
                raise BinanceError(e.code, j.get("code"), j.get("msg", body)) from None
            except json.JSONDecodeError:
                raise BinanceError(e.code, None, body) from None

    def sync_time(self) -> int:
        t0 = time.time() * 1000
        srv = self._request("GET", "/fapi/v1/time")["serverTime"]
        t1 = time.time() * 1000
        self._time_offset_ms = int(srv - (t0 + t1) / 2)
        return self._time_offset_ms

    # ── lectura publica ────────────────────────────────────────────────
    def exchange_info(self) -> dict:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def rules(self, symbol: str) -> dict:
        """tickSize, stepSize, minQty, minNotional del simbolo."""
        symbol = symbol.upper()
        if symbol in self._filters:
            return self._filters[symbol]
        info = self.exchange_info()
        for s in info["symbols"]:
            if s["symbol"] != symbol:
                continue
            f = {x["filterType"]: x for x in s["filters"]}
            out = {
                "symbol": symbol,
                "status": s["status"],
                "tick": float(f["PRICE_FILTER"]["tickSize"]),
                "step": float(f["LOT_SIZE"]["stepSize"]),
                "min_qty": float(f["LOT_SIZE"]["minQty"]),
                "max_qty": float(f["LOT_SIZE"]["maxQty"]),
                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 0)),
                "qty_precision": s["quantityPrecision"],
                "price_precision": s["pricePrecision"],
                "percent_price": {
                    "down": float(f.get("PERCENT_PRICE", {}).get("multiplierDown", 0) or 0),
                    "up": float(f.get("PERCENT_PRICE", {}).get("multiplierUp", 0) or 0),
                },
            }
            self._filters[symbol] = out
            return out
        raise SafetyError(f"{symbol} no existe en {self.base}")

    def depth(self, symbol: str, limit: int = 5) -> dict:
        return self._request("GET", "/fapi/v1/depth",
                             {"symbol": symbol.upper(), "limit": limit})

    def mark_price(self, symbol: str) -> float:
        j = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol.upper()})
        return float(j["markPrice"])

    # ── lectura privada ────────────────────────────────────────────────
    def account(self) -> dict:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def positions(self, symbol: str | None = None) -> list[dict]:
        p = {"symbol": symbol.upper()} if symbol else None
        return self._request("GET", "/fapi/v2/positionRisk", p, signed=True)

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        p = {"symbol": symbol.upper()} if symbol else None
        return self._request("GET", "/fapi/v1/openOrders", p, signed=True)

    def query_order(self, symbol: str, order_id: int | None = None,
                    client_id: str | None = None) -> dict:
        if not (order_id or client_id):
            raise SafetyError("query_order necesita order_id o client_id")
        p: dict = {"symbol": symbol.upper()}
        if order_id:
            p["orderId"] = order_id
        if client_id:
            p["origClientOrderId"] = client_id
        return self._request("GET", "/fapi/v1/order", p, signed=True)

    def user_trades(self, symbol: str, order_id: int | None = None,
                    limit: int = 50) -> list[dict]:
        p: dict = {"symbol": symbol.upper(), "limit": int(limit)}
        if order_id:
            p["orderId"] = order_id
        return self._request("GET", "/fapi/v1/userTrades", p, signed=True)

    def commission_rate(self, symbol: str) -> dict:
        """maker/taker que el exchange aplica de verdad a esta cuenta."""
        return self._request("GET", "/fapi/v1/commissionRate",
                             {"symbol": symbol.upper()}, signed=True)

    def dual_side(self) -> bool:
        j = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return bool(j.get("dualSidePosition"))

    def listen_key(self) -> str:
        return self._request("POST", "/fapi/v1/listenKey", signed=True)["listenKey"]

    # ── redondeo a filtros ─────────────────────────────────────────────
    def round_qty(self, symbol: str, qty: float) -> float:
        r = self.rules(symbol)
        step = Decimal(str(r["step"]))
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return float(q)

    def round_price(self, symbol: str, price: float) -> float:
        r = self.rules(symbol)
        tick = Decimal(str(r["tick"]))
        p = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return float(p)

    def check_order(self, symbol: str, side: str, qty: float,
                    price: float | None = None, reduce_only: bool = False) -> dict:
        """Valida contra filtros y topes locales. Devuelve qty/precio limpios.

        `reduce_only=True` exime de los topes de nocional: el tope existe para
        limitar exposicion NUEVA, no para impedir cerrar. Sin esto, una posicion
        mayor que MAX_ORDER_NOTIONAL_USD no se podria aplanar.
        """
        symbol, side = symbol.upper(), side.upper()
        if side not in ("BUY", "SELL"):
            raise SafetyError(f"side invalido: {side}")
        if symbol not in self.allow_symbols:
            raise SafetyError(f"{symbol} no esta en ALLOW_SYMBOLS={self.allow_symbols}")
        r = self.rules(symbol)
        if r["status"] != "TRADING":
            raise SafetyError(f"{symbol} status={r['status']}")
        q = self.round_qty(symbol, qty)
        if q < r["min_qty"]:
            raise SafetyError(f"qty {q} < minQty {r['min_qty']} ({symbol})")
        if q > r["max_qty"]:
            raise SafetyError(f"qty {q} > maxQty {r['max_qty']} ({symbol})")
        px = self.round_price(symbol, price) if price else self.mark_price(symbol)
        if price and px <= 0:
            raise SafetyError(f"precio invalido: {price}")
        notional = q * px
        if not reduce_only:
            if notional < r["min_notional"]:
                raise SafetyError(f"nocional {notional:.2f} < MIN_NOTIONAL {r['min_notional']}")
            if notional > self.max_notional:
                raise SafetyError(
                    f"nocional {notional:.2f} > MAX_ORDER_NOTIONAL_USD {self.max_notional:.2f}")
        return {"symbol": symbol, "side": side, "qty": q, "price": px,
                "notional": notional, "rules": r}

    # ── escritura ──────────────────────────────────────────────────────
    def place_order(self, *, symbol: str, side: str, qty: float,
                    order_type: str = "MARKET", price: float | None = None,
                    reduce_only: bool = False, position_side: str | None = None,
                    time_in_force: str = "GTC", client_id: str | None = None,
                    dry_run: bool | None = None) -> dict:
        dry = self.dry_run if dry_run is None else dry_run
        chk = self.check_order(symbol, side, qty, price, reduce_only=reduce_only)
        params = {
            "symbol": chk["symbol"],
            "side": chk["side"],
            "type": order_type.upper(),
            "quantity": f"{chk['qty']:.{chk['rules']['qty_precision']}f}",
            "newClientOrderId": client_id or f"qae-{int(time.time()*1000)}",
            "newOrderRespType": "RESULT",
        }
        if order_type.upper() == "LIMIT":
            params["price"] = f"{chk['price']:.{chk['rules']['price_precision']}f}"
            params["timeInForce"] = time_in_force
        if reduce_only:
            params["reduceOnly"] = "true"
        if position_side:
            params["positionSide"] = position_side.upper()
        return self._submit(params, notional=chk["notional"], dry=dry,
                            tag="exit" if reduce_only else "entry")

    def _submit(self, params: dict, *, notional: float,
                dry: bool | None = None, tag: str = "",
                path: str = "/fapi/v1/order") -> dict:
        """Envia (o simula) una orden ya validada y la registra."""
        dry = self.dry_run if dry is None else dry
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "mode": "DRY-RUN" if dry or not self.orders_enabled else "SEND",
               "path": path, "tag": tag, "params": params}
        if dry or not self.orders_enabled:
            rec["result"] = "not_sent"
            self._log(rec)
            return {"dry_run": True, "would_send": params, "notional": notional,
                    "reason": "dry_run=True" if dry else "ALLOW_ORDERS!=1"}
        resp = self._request("POST", path, params, signed=True)
        rec["result"] = {k: resp.get(k) for k in
                         ("orderId", "algoId", "status", "algoStatus", "executedQty",
                          "avgPrice", "clientOrderId", "clientAlgoId")}
        rec["result"]["code"] = resp.get("code")
        self._log(rec)
        return resp

    def open_algo_orders(self, symbol: str | None = None) -> list[dict]:
        p = {"symbol": symbol.upper()} if symbol else None
        r = self._request("GET", "/fapi/v1/openAlgoOrders", p, signed=True)
        return r if isinstance(r, list) else [r]

    def cancel_algo(self, algo_id: int | str, symbol: str | None = None) -> dict:
        p: dict = {"algoid": algo_id}
        if symbol:
            p["symbol"] = symbol.upper()
        return self._request("DELETE", "/fapi/v1/algoOrder", p, signed=True)

    def protective_order(self, *, kind: str, symbol: str, side: str,
                         stop_price: float, qty: float | None = None,
                         close_position: bool = True,
                         working_type: str = "MARK_PRICE",
                         dry_run: bool | None = None) -> dict:
        """STOP_MARKET / TAKE_PROFIT_MARKET via Algo Order API, cerrando la posicion.

        Desde 2026 el endpoint clasico `/fapi/v1/order` rechaza estos tipos con
        -4120 ("use the Algo Order API endpoints instead"). Aqui se usa
        POST /fapi/v1/algoOrder con algoType=CONDITIONAL y triggerPrice.

        `closePosition=true` no admite quantity ni reduceOnly, y el exchange
        exige que exista una posicion abierta (-4509). Se valida contra tick y
        contra la banda PERCENT_PRICE, que es la que rechaza stops lejanos.
        """
        kind = kind.upper()
        if kind not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            raise SafetyError(f"tipo de proteccion invalido: {kind}")
        sym, side = symbol.upper(), side.upper()
        if sym not in self.allow_symbols:
            raise SafetyError(f"{sym} no esta en ALLOW_SYMBOLS={self.allow_symbols}")
        if side not in ("BUY", "SELL"):
            raise SafetyError(f"side invalido: {side}")
        r = self.rules(sym)
        if r["status"] != "TRADING":
            raise SafetyError(f"{sym} status={r['status']}")
        mark = self.mark_price(sym)
        sp = self.round_price(sym, stop_price)
        down, up = r["percent_price"]["down"], r["percent_price"]["up"]
        if down and up and not (mark * down <= sp <= mark * up):
            raise SafetyError(
                f"{kind} {sp} fuera de la banda PERCENT_PRICE del simbolo "
                f"[{mark*down:.6g}, {mark*up:.6g}] (mark {mark:.6g})")
        if kind == "STOP_MARKET":
            if side == "SELL" and sp >= mark:
                raise SafetyError(f"SELL {kind} {sp} >= mark {mark}: se dispararia ya")
            if side == "BUY" and sp <= mark:
                raise SafetyError(f"BUY {kind} {sp} <= mark {mark}: se dispararia ya")
        else:  # TAKE_PROFIT_MARKET va al otro lado del precio
            if side == "SELL" and sp <= mark:
                raise SafetyError(f"SELL {kind} {sp} <= mark {mark}: se dispararia ya")
            if side == "BUY" and sp >= mark:
                raise SafetyError(f"BUY {kind} {sp} >= mark {mark}: se dispararia ya")

        params = {
            "algoType": "CONDITIONAL",
            "symbol": sym, "side": side, "type": kind,
            "triggerPrice": f"{sp:.{r['price_precision']}f}",
            "workingType": working_type.upper(),
            "clientAlgoId": f"qae-{kind[:4].lower()}-{int(time.time()*1000)}",
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            if not qty or qty <= 0:
                raise SafetyError("sin closePosition hace falta qty")
            q = self.round_qty(sym, qty)
            params["quantity"] = f"{q:.{r['qty_precision']}f}"
            params["reduceOnly"] = "true"
        notional = (qty or 0) * sp
        return self._submit(params, notional=notional, dry=dry_run,
                            tag=kind.lower(), path="/fapi/v1/algoOrder")

    def close_position_market(self, symbol: str, side: str, qty: float,
                              dry_run: bool | None = None) -> dict:
        """Cierre de emergencia a mercado con reduceOnly."""
        return self.place_order(symbol=symbol, side=side, qty=qty,
                                order_type="MARKET", reduce_only=True,
                                dry_run=dry_run, client_id=f"qae-emg-{int(time.time()*1000)}")

    def cancel(self, symbol: str, order_id: int | None = None,
               client_id: str | None = None) -> dict:
        if not (order_id or client_id):
            raise SafetyError("cancel necesita order_id o client_id")
        p = {"symbol": symbol.upper()}
        if order_id:
            p["orderId"] = order_id
        if client_id:
            p["origClientOrderId"] = client_id
        return self._request("DELETE", "/fapi/v1/order", p, signed=True)

    def cancel_all(self, symbol: str) -> dict:
        return self._request("DELETE", "/fapi/v1/allOpenOrders",
                             {"symbol": symbol.upper()}, signed=True)

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        if not 1 <= int(leverage) <= 20:
            raise SafetyError("leverage fuera de 1..20 (tope local)")
        return self._request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol.upper(), "leverage": int(leverage)},
                             signed=True)

    @staticmethod
    def _log(rec: dict) -> None:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            with (LOG_DIR / "orders.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


def fmt(v: float, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:,.{nd}f}"
