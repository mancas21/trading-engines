"""risk_guard.py — topes de cartera y matafuegos, EN el paso previo a la orden.

El audit (docs/AUDIT-QUANT-REPORT.md §2.2) encontro que el sizing vive en el
router, el gating en el engine y NO HABIA nada que limite la exposicion
agregada ni pare la cuenta. Este modulo es ese "ultimo paso antes de la orden":

  * KILL_SWITCH: si execution/state/KILL_SWITCH existe, no se abre ni se
    revierte NINGUNA posicion (cerrar si se permite: cerrar reduce riesgo).
    El fichero se puede escribir a mano o lo escribe el propio guard cuando
    el limite de perdida diaria salta.
  * PERDIDA DIARIA: guarda el equity al empezar el dia UTC; si el equity cae
    mas de DAILY_LOSS_PCT, dispara el kill switch y bloquea nuevas entradas.
  * EXPOSICION: tope de nocional bruto (suma |amt|*mark) y neto (|suma firmada|)
    en la cuenta. Pasarse bloquea solo las entradas nuevas.

Todo se puede afinar por entorno: QAE_DAILY_LOSS_PCT, QAE_MAX_GROSS_USD,
QAE_MAX_NET_USD.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
KILL_FILE = STATE / "KILL_SWITCH"
DAILY_FILE = STATE / "daily_guard.json"

DAILY_LOSS_PCT = float(os.environ.get("QAE_DAILY_LOSS_PCT", "2.0"))
MAX_GROSS_USD = float(os.environ.get("QAE_MAX_GROSS_USD", "600"))
MAX_NET_USD = float(os.environ.get("QAE_MAX_NET_USD", "400"))


class RiskBlocked(RuntimeError):
    """Un tope de cartera impide asumir riesgo nuevo (no es un error de red)."""


# ─────────────────────────────────────────────────────────── kill switch ──
def kill_active() -> bool:
    return KILL_FILE.is_file()


def trip(reason: str) -> None:
    STATE.mkdir(exist_ok=True)
    KILL_FILE.write_text(
        json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "reason": reason}, ensure_ascii=False),
        encoding="utf-8")


def clear() -> None:
    try:
        KILL_FILE.unlink()
    except OSError:
        pass


def kill_reason() -> str:
    try:
        return KILL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def require_can_open() -> None:
    """Lanzar RiskBlocked si el kill switch esta activo."""
    if kill_active():
        raise RiskBlocked(f"KILL_SWITCH activo: {kill_reason()[:200]}")


# ───────────────────────────────────────────────────────── perdida diaria ──
def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _load_daily() -> dict:
    try:
        return json.loads(DAILY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def daily_check(equity: float) -> dict:
    """Actualiza el ancla diaria y decide si la perdida diaria salta.

    Devuelve {"tripped": bool, "why": str, "loss_pct": float}. Si salta, deja
    el kill switch puesto (hay que levantarlo a mano: decision humana).
    """
    d = _load_daily()
    today = _today()
    if d.get("date") != today or not isinstance(d.get("equity_start"), (int, float)):
        d = {"date": today, "equity_start": equity}
        try:
            STATE.mkdir(exist_ok=True)
            DAILY_FILE.write_text(json.dumps(d), encoding="utf-8")
        except OSError:
            pass
    start = float(d["equity_start"])
    if start <= 0 or equity <= 0:
        return {"tripped": False, "why": "sin equity legible", "loss_pct": 0.0}
    loss_pct = (start - equity) / start * 100.0
    if loss_pct >= DAILY_LOSS_PCT:
        if not kill_active():
            trip(f"perdida diaria {loss_pct:.2f}% >= {DAILY_LOSS_PCT}% "
                 f"(equity {equity:.2f} vs ancla {start:.2f})")
        return {"tripped": True, "why": f"perdida diaria {loss_pct:.2f}%",
                "loss_pct": loss_pct}
    return {"tripped": False, "why": f"perdida diaria {loss_pct:.2f}%",
            "loss_pct": loss_pct}


# ───────────────────────────────────────────────────────────── exposicion ──
def exposure_check(client) -> dict:
    """Nocional bruto y neto de las posiciones vivas, contra los topes.

    `client` necesita .positions() -> [{"symbol","positionAmt",...}] y
    .mark_price(sym). Devuelve {"gross": f, "net": f, "ok": bool, "why": str}.
    """
    gross = 0.0
    net = 0.0
    for p in client.positions():
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue
        sym = p["symbol"]
        try:
            mark = client.mark_price(sym)
        except Exception:  # noqa: BLE001 — sin mark no puedo valorar: cuento amt signado a precio 0
            mark = 0.0
        notional = abs(amt) * mark
        gross += notional
        net += amt * mark
    ok = gross <= MAX_GROSS_USD and abs(net) <= MAX_NET_USD
    why = "" if ok else (
        f"exposicion gross {gross:.0f} USD (tope {MAX_GROSS_USD:.0f}) / "
        f"net {net:.0f} USD (tope {MAX_NET_USD:.0f})")
    return {"gross": gross, "net": net, "ok": ok, "why": why}
