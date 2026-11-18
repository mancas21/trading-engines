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
    en la cuenta. Pasarse bloquea solo las entradas nuevas. Un mark ilegible
    deja la exposicion como DESCONOCIDA (fail-closed), nunca como cero.

`exposure_snapshot()` es la UNICA fuente de verdad de exposicion del repo: la
usan el auto_router, el bot neutral y cualquier panel. Los topes de inventario
del grid viven aqui y el bot los importa; no hay un segundo juego de numeros.

Todo se puede afinar por entorno: QAE_DAILY_LOSS_PCT, QAE_MAX_INV_SYMBOL,
QAE_MAX_INV_TOTAL, QAE_MAX_GROSS_USD, QAE_MAX_NET_USD.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
KILL_FILE = STATE / "KILL_SWITCH"
DAILY_FILE = STATE / "daily_guard.json"

# ---------------------------------------------------------------------------
# POLITICA DE RIESGO — UN SOLO SITIO
# ---------------------------------------------------------------------------
# 2026-11-17 — habia DOS cerebros de riesgo con numeros distintos: este modulo
# decia 600 bruto / 400 neto (heredado del auto_router, retirado el 12-nov) y
# neutral_bot.py llevaba sus propios 400/1.200 inline. Cualquier consumidor que
# preguntara aqui -un panel, un control plane, el propio bot- recibia una
# respuesta que no describia la cuenta. Ahora la politica vive aqui y el bot
# IMPORTA estas constantes: no hay un segundo numero que pueda derivar.
#
# El envelope lo fijo el bot el 17-nov midiendo el peor caso: 9 pares x 4
# niveles x 60 USDT = 720 de inventario bruto (15% de la equity de 4.700), y un
# stop-out completo de los 9 a la vez ~29 USDT (0,6%), dentro de la guardia
# diaria del 2%. Ver neutral_bot.py para el razonamiento completo.
MAX_INVENTORY_USD_PER_SYMBOL = float(
    os.environ.get("QAE_MAX_INV_SYMBOL", "240"))
MAX_INVENTORY_USD_TOTAL = float(os.environ.get("QAE_MAX_INV_TOTAL", "720"))

DAILY_LOSS_PCT = float(os.environ.get("QAE_DAILY_LOSS_PCT", "2.0"))
# El bruto de la cuenta ES el inventario total: un solo numero, no dos.
MAX_GROSS_USD = float(os.environ.get("QAE_MAX_GROSS_USD",
                                     MAX_INVENTORY_USD_TOTAL))
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
def _finite(x) -> float | None:
    """float finito o None. `None` NUNCA se convierte en 0."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _positive(x) -> float | None:
    v = _finite(x)
    return v if (v is not None and v > 0) else None


def exposure_snapshot(client) -> dict:
    """LA fuente unica de exposicion de la cuenta. Fail-closed por diseno.

    Devuelve:
        {gross, net, positions, unpriced, verifiable, ok, why, caps}

    Regla que este modulo aprendio a golpes (audit P0): si el mark de una
    posicion no se puede leer, la exposicion es DESCONOCIDA, no cero. El
    codigo viejo hacia `except: mark = 0.0`, el bruto se encogia y
    `gross <= MAX_GROSS_USD` daba True: autorizaba riesgo nuevo justo cuando
    no sabia cuanto riesgo habia. Aqui un precio ilegible deja
    `verifiable=False` y `ok=False`.

    Puede lanzar si `client.positions()` falla: el llamador debe tratar la
    excepcion como "exposicion desconocida" (fail-closed), no como "plana".
    """
    out: dict = {
        "gross": 0.0, "net": 0.0,
        "positions": {}, "unpriced": [],
        "verifiable": True, "ok": True, "why": "",
        "caps": {"per_symbol": MAX_INVENTORY_USD_PER_SYMBOL,
                 "total": MAX_INVENTORY_USD_TOTAL,
                 "gross": MAX_GROSS_USD, "net": MAX_NET_USD},
    }
    raw = client.positions()
    if not isinstance(raw, list):
        out.update(verifiable=False, ok=False,
                   why=f"positions() devolvio {type(raw).__name__}, no una lista")
        return out

    for p in raw:
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol")
        amt = _finite(p.get("positionAmt"))
        if not sym or amt is None:
            out["unpriced"].append(str(sym) if sym else "(sin symbol)")
            continue
        if amt == 0:
            continue
        mark = _positive(p.get("markPrice"))
        if mark is None:
            # positionRisk trae markPrice; si falta se pregunta una vez.
            try:
                mark = _positive(client.mark_price(sym))
            except Exception:  # noqa: BLE001 — red/HTTP: exposicion desconocida
                mark = None
        if mark is None:
            out["unpriced"].append(sym)
            continue
        notional = abs(amt) * mark
        out["positions"][sym] = {
            "amt": amt, "mark": mark, "notional": notional,
            "upnl": _finite(p.get("unRealizedProfit")) or 0.0,
        }
        out["gross"] += notional
        out["net"] += amt * mark

    if out["unpriced"]:
        out["verifiable"] = False
    gross_ok = out["gross"] <= MAX_GROSS_USD
    net_ok = abs(out["net"]) <= MAX_NET_USD
    out["ok"] = bool(out["verifiable"] and gross_ok and net_ok)

    reasons = []
    if not out["verifiable"]:
        reasons.append("precio ilegible en " + ", ".join(out["unpriced"])
                       + " -> exposicion DESCONOCIDA")
    if not gross_ok:
        reasons.append(f"gross {out['gross']:.0f} USD > tope {MAX_GROSS_USD:.0f}")
    if not net_ok:
        reasons.append(f"net {out['net']:.0f} USD > tope {MAX_NET_USD:.0f}")
    out["why"] = "; ".join(reasons)
    return out


def exposure_check(client) -> dict:
    """Compatibilidad con el auto_router: dict {gross, net, ok, why}."""
    s = exposure_snapshot(client)
    return {"gross": s["gross"], "net": s["net"], "ok": s["ok"],
            "why": s["why"], "verifiable": s["verifiable"],
            "positions": s["positions"], "unpriced": s["unpriced"]}


def require_exposure_ok(client) -> dict:
    """Fail-closed de verdad: lanza RiskBlocked si no se puede verificar."""
    s = exposure_snapshot(client)
    if not s["ok"]:
        raise RiskBlocked(s["why"] or "exposicion fuera de tope o no verificable")
    return s
