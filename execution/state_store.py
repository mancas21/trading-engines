"""Estado de las posiciones abiertas por este engine (JSON atomico).

El router escribe aqui lo que abrio; el reconciliador lo lee para recrear el
stop si desaparece. Si el proceso muere entre la entrada y el stop, el estado
ya existe (se escribe la INTENCION antes de enviar) y el reconciliador puede
actuar.

    state/positions.json
    { "XRPUSDT": { "side": "LONG", "qty": 76.9, "entry": 1.2998,
                   "stop": 1.276, "tp": 1.335, "stop_algo_id": 123,
                   "tp_algo_id": 456, "candidate_id": "S10",
                   "run_id": "...", "strategy": "S10", "opened_utc": "...",
                   "risk_usdt": 1.84, "status": "open" } }
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
STATE_FILE = STATE_DIR / "positions.json"


def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save(data: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def upsert(symbol: str, rec: dict) -> None:
    d = load()
    rec = dict(rec)
    rec.setdefault("updated_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    d[symbol.upper()] = rec
    save(d)


def patch(symbol: str, **kw) -> None:
    d = load()
    sym = symbol.upper()
    if sym in d:
        d[sym].update(kw)
        d[sym]["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save(d)


def remove(symbol: str) -> dict | None:
    d = load()
    rec = d.pop(symbol.upper(), None)
    save(d)
    return rec
