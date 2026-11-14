"""synthetic_flow.py — flujo tipo-ATAS computado desde el stream @trade de Binance.

DECISIÓN DE ARQUITECTURA (2026-11-14, usuario): ATAS y TradingView solo cubren
el símbolo del chart activo. Este módulo da a TODOS los pares del scanner el
mismo set de métricas (delta por barra, tape, footprint-delta por nivel,
VWAP, session value area) computado por nosotros, etiquetado
source="synthetic_binance". ATAS sigue siendo el certificado del par
graficado; aquí el footprint es DELTA por nivel (comprador vs vendedor
agresor), no bid/ask por nivel — eso solo lo tiene el L2 de ATAS.

Alimentación: 1 trade por llamada a on_trade() desde el handler @trade de
_dom_feeds (mismo event loop, sin lock: una sola corrutina escribe por
símbolo). Sin backfill: tras un reinicio del bridge las historias se llenan
en vivo (~20 barras de 1m).

Clave de nivel: el STRING de precio crudo de Binance — los trades solo
ocurren en precios de tick válidos, así que no hace falta exchangeInfo.
"""
from __future__ import annotations

import time
from collections import deque

VA_TARGET = 0.70          # value area = 70% del volumen desde el POC
SESSION_ANCHOR_S = 86400  # VWAP/VA de sesión: ancla 00:00 UTC


class _Bar:
    __slots__ = ("ts", "open", "buy", "sell", "count", "hi", "lo", "buckets")

    def __init__(self, ts_ms: int):
        self.ts = ts_ms
        self.open: float | None = None
        self.buy = 0.0
        self.sell = 0.0
        self.count = 0
        self.hi = 0.0
        self.lo = 0.0
        self.buckets: dict[str, list[float]] = {}   # price_str -> [buy, sell, vol]


def _va_from_levels(levels: dict[str, list[float]]) -> tuple[float | None, float | None, float | None]:
    """(poc, vah, val) por expansión 70% desde el POC — mismo algoritmo que
    features.py::_value_location pero sobre niveles de trade reales."""
    if not levels:
        return None, None, None
    items = sorted(((float(p), b + s) for p, (b, s) in levels.items()), key=lambda x: x[0])
    prices = [p for p, _ in items]
    vols = [v for _, v in items]
    total = sum(vols)
    if total <= 0:
        return None, None, None
    poc_i = max(range(len(vols)), key=lambda i: vols[i])
    target = total * VA_TARGET
    acc = vols[poc_i]
    lo = hi = poc_i
    while acc < target and (lo > 0 or hi < len(vols) - 1):
        if lo == 0:
            hi += 1
        elif hi == len(vols) - 1:
            lo -= 1
        elif vols[lo - 1] > vols[hi + 1]:
            lo -= 1
        else:
            hi += 1
        acc = sum(vols[lo:hi + 1])
    return prices[poc_i], prices[hi], prices[lo]


class SyntheticFlow:
    """Acumulador de flujo por símbolo. on_trade() en cada print del WS."""

    def __init__(self, symbol: str, tape_n: int = 30, va_bars: int = 20):
        self.symbol = symbol.upper()
        self.tape = deque(maxlen=tape_n)
        self.closed: deque = deque(maxlen=va_bars)   # resúmenes de barras cerradas
        self.bar: _Bar | None = None
        self.session_levels: dict[str, list[float]] = {}
        self.session_anchor = 0
        self.vwap_num = 0.0
        self.vwap_den = 0.0
        self.last_price = 0.0
        self.max_delta = 0.0
        self.min_delta = 0.0
        self.closed_count = 0

    # -- alimentación -----------------------------------------------------
    def on_trade(self, price: float, qty: float, side: str, ts_ms: int,
                 price_str: str | None = None) -> None:
        day = int(ts_ms // 1000 // SESSION_ANCHOR_S)
        if day != self.session_anchor:
            self.session_anchor = day
            self.session_levels = {}
            self.vwap_num = 0.0
            self.vwap_den = 0.0

        bts = ts_ms // 60000 * 60000
        if self.bar is None or self.bar.ts != bts:
            if self.bar is not None:
                self._close_bar()
            self.bar = _Bar(bts)
        lvl = price_str or f"{price:.8f}"
        delta = qty if side == "buy" else -qty

        if self.bar.open is None:
            self.bar.open = price
        self.bar.buy += qty if side == "buy" else 0.0
        self.bar.sell += qty if side == "sell" else 0.0
        self.bar.count += 1
        self.bar.hi = max(self.bar.hi, price) if self.bar.hi else price
        self.bar.lo = min(self.bar.lo, price) if self.bar.lo else price
        b = self.bar.buckets.setdefault(lvl, [0.0, 0.0])
        b[0 if side == "buy" else 1] += qty

        self.tape.append({"p": price, "q": qty, "s": side, "t": ts_ms / 1000.0})
        sb = self.session_levels.setdefault(lvl, [0.0, 0.0])
        sb[0 if side == "buy" else 1] += qty
        self.vwap_num += price * qty
        self.vwap_den += qty
        self.last_price = price

    def _close_bar(self) -> None:
        b = self.bar
        delta = b.buy - b.sell
        self.max_delta = max(self.max_delta, delta)
        self.min_delta = min(self.min_delta, delta)
        poc, vah, val = _va_from_levels(b.buckets)
        self.closed.append({
            "volume": round(b.buy + b.sell, 4), "delta": round(delta, 4),
            "poc": poc, "value_area_high": vah, "value_area_low": val,
        })
        self.closed_count += 1
        self.bar = None

    # -- lectura ----------------------------------------------------------
    def snapshot(self, dom: dict | None = None,
                 open_interest: float | None = None) -> dict:
        b = self.bar
        vol_total = (b.buy + b.sell) if b else 0.0
        avg20 = (sum(c["volume"] for c in self.closed) / len(self.closed)) if self.closed else 0.0
        poc, vah, val = _va_from_levels(b.buckets if b else {})
        s_poc, s_vah, s_val = _va_from_levels(self.session_levels)
        tape = list(self.tape)
        tb = sum(t["q"] for t in tape if t["s"] == "buy")
        ts_ = sum(t["q"] for t in tape if t["s"] == "sell")
        tb_c = sum(1 for t in tape if t["s"] == "buy")
        ts_c = len(tape) - tb_c
        gross = (b.buy + b.sell) if b else 0.0
        net = (b.buy - b.sell) if b else 0.0
        return {
            "schema_version": 2,
            "source": "synthetic_binance",
            "ticker": self.symbol,
            "timestamp": int(time.time() * 1000),
            "bar_index": self.closed_count,
            "time": time.strftime("%H:%M:%S", time.gmtime()),
            "price": {"last": self.last_price,
                      "open": b.open if b else None,
                      "high": b.hi if b else 0.0, "low": b.lo if b else 0.0},
            "volume": {"total": vol_total, "delta": net,
                       "avg_volume_20": round(avg20, 4),
                       "volume_ratio": (vol_total / avg20) if avg20 else 0.0,
                       "lookback_bars": len(self.closed)},
            "delta": net,
            "max_delta": self.max_delta, "min_delta": self.min_delta,
            "open_interest": open_interest,
            "vwap": (self.vwap_num / self.vwap_den) if self.vwap_den else None,
            "tape": {"buy_vol": tb, "sell_vol": ts_, "net_vol": tb - ts_,
                     "buy_count": tb_c, "sell_count": ts_c,
                     "buy_count_pct": (tb_c / len(tape) * 100.0) if tape else 0.0,
                     "sell_count_pct": (ts_c / len(tape) * 100.0) if tape else 0.0,
                     "total_vol": tb + ts_,
                     "avg_trade_size": ((tb + ts_) / len(tape)) if tape else 0.0,
                     "largest_trade": max((t["q"] for t in tape), default=0.0),
                     "largest_trade_side": (max(tape, key=lambda t: t["q"])["s"] if tape else None),
                     "largest_trade_price": (max(tape, key=lambda t: t["q"])["p"] if tape else None),
                     "prints_sampled": len(tape),
                     "sample_window": "last_30_trades_of_current_session"},
            "footprint": {"poc": poc, "value_area_high": vah,
                          "value_area_low": val,
                          "all_level_count": len(b.buckets) if b else 0,
                          "imbalance_ratio": (net / gross * 100.0) if gross else 0.0,
                          "note": "delta por nivel (agresor); sin bid/ask por nivel — eso es ATAS"},
            "closed_bars": list(self.closed),
            "session_value_area": {"poc": s_poc, "value_area_high": s_vah,
                                   "value_area_low": s_val,
                                   "levels": len(self.session_levels)},
            "dom": dom or {},
        }


def empty_flow_payload(symbol: str, note: str) -> dict:
    """Payload mínimo para un símbolo sin datos aún (warm-up)."""
    return {"schema_version": 2, "source": "synthetic_binance", "ticker": symbol.upper(),
            "timestamp": int(time.time() * 1000), "warming_up": True, "note": note}
