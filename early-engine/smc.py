"""smc.py — estructura de mercado SMC: CHOCH y BOS sobre velas CERRADAS.

Son HECHOS de estructura, no predicciones: un BOS es "el cierre rompió el
último swing en dirección de la tendencia", un CHOCH es "el cierre rompió el
último swing EN CONTRA de la tendencia" (primer aviso de reversión). Por eso
pueden notificarse en vivo sin protocolo de validación — no afirman edge,
afirman qué pasó.

Reglas (sin lookahead):
  * Swing high/low: pivote con `strength` velas más bajas/altas a CADA lado.
    Se CONFIRMA `strength` velas después del extremo — nunca antes.
  * Tendencia: None al inicio; BOS la fija; CHOCH la invierte.
  * BOS (continuación): cierre > último swing high con tendencia bull
    (o cierre < último swing low con tendencia bear).
  * CHOCH (reversión): cierre < último swing low con tendencia bull,
    o cierre > último swing high con tendencia bear.
  * El nivel roto se consume: no re-dispara hasta que un pivote NUEVO lo
    reemplace. El chequeo anti-tendencia (CHOCH) va primero.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class StructureEvent:
    kind: str            # "BOS" | "CHOCH"
    side: str            # "bull" | "bear"
    tf: str
    ts: float            # apertura de la vela que rompió (cerrada)
    price: float         # cierre rompiente
    level: float         # swing roto
    trend_before: str

    @property
    def name(self) -> str:
        return f"SMC_{self.kind}"


class StructureTracker:
    """Estado de estructura por (símbolo, timeframe). update() una vela CERRADA
    por llamada, en orden; devuelve StructureEvent o None."""

    def __init__(self, symbol: str, tf: str, strength: int = 2):
        if strength < 1:
            raise ValueError("strength >= 1")
        self.symbol = symbol
        self.tf = tf
        self.k = strength
        self.trend: Optional[str] = None          # "bull" | "bear" | None
        self.last_sh: Optional[float] = None      # último swing high confirmado
        self.last_sl: Optional[float] = None
        self.sh_consumed = False                  # su ruptura ya disparó
        self.sl_consumed = False
        self._window: list[dict] = []             # velas sin pivot-check completo
        self._last_ts: Optional[float] = None

    # -- pivotes ----------------------------------------------------------
    def _confirm_pivots(self) -> None:
        """Revisa la ventana y confirma pivotes cuyo lado derecho ya completo.
        La ventana desliza una vela por update(): cada candidato se evalúa
        exactamente una vez, así que no hay doble confirmación."""
        w = self._window
        k = self.k
        while len(w) >= 2 * k + 1:
            cand = w[k]                            # k velas confirmadas a cada lado
            if all(cand["high"] > w[j]["high"] for j in range(len(w)) if j != k):
                self.last_sh = float(cand["high"])
                self.sh_consumed = False
            if all(cand["low"] < w[j]["low"] for j in range(len(w)) if j != k):
                self.last_sl = float(cand["low"])
                self.sl_consumed = False
            w.pop(0)

    def update(self, bar: dict) -> Optional[StructureEvent]:
        """bar: vela CERRADA {ts, open, high, low, close}. En orden."""
        ts = float(bar["ts"])
        if self._last_ts is not None and ts <= self._last_ts:
            return None                            # vela vieja/repetida
        self._last_ts = ts
        self._window.append(dict(bar))
        self._confirm_pivots()

        close = float(bar["close"])
        ev: Optional[StructureEvent] = None

        # CHOCH primero: la ruptura CONTRA la tendencia manda
        if self.trend == "bull" and self.last_sl is not None \
                and not self.sl_consumed and close < self.last_sl:
            ev = StructureEvent("CHOCH", "bear", self.tf, ts, close,
                                self.last_sl, self.trend)
            self.trend = "bear"
            self.sl_consumed = True
        elif self.trend == "bear" and self.last_sh is not None \
                and not self.sh_consumed and close > self.last_sh:
            ev = StructureEvent("CHOCH", "bull", self.tf, ts, close,
                                self.last_sh, self.trend)
            self.trend = "bull"
            self.sh_consumed = True
        elif self.trend == "bull" and self.last_sh is not None \
                and not self.sh_consumed and close > self.last_sh:
            ev = StructureEvent("BOS", "bull", self.tf, ts, close,
                                self.last_sh, self.trend)
            self.sh_consumed = True
        elif self.trend == "bear" and self.last_sl is not None \
                and not self.sl_consumed and close < self.last_sl:
            ev = StructureEvent("BOS", "bear", self.tf, ts, close,
                                self.last_sl, self.trend)
            self.sl_consumed = True
        elif self.trend is None:
            # estructura inicial: la primera ruptura fija la tendencia
            if self.last_sh is not None and not self.sh_consumed \
                    and close > self.last_sh:
                ev = StructureEvent("BOS", "bull", self.tf, ts, close,
                                    self.last_sh, "none")
                self.trend = "bull"
                self.sh_consumed = True
            elif self.last_sl is not None and not self.sl_consumed \
                    and close < self.last_sl:
                ev = StructureEvent("BOS", "bear", self.tf, ts, close,
                                    self.last_sl, "none")
                self.trend = "bear"
                self.sl_consumed = True
        return ev
