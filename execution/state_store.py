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

audit 2.7: un estado corrupto NO se lee como "sin posiciones". JSONDecodeError
levanta StateCorrupt — el llamador debe parar y avisar, no seguir como si el
estado estuviera vacio. save() hace fsync del fichero antes del rename para que
un corte de luz no deje un fichero truncado que dispare exactamente eso.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
STATE_FILE = STATE_DIR / "positions.json"


class StateCorrupt(RuntimeError):
    """positions.json existe pero no se puede parsear."""


def load() -> dict:
    try:
        raw = STATE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise StateCorrupt(f"no se pudo leer {STATE_FILE}: {e}") from None
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StateCorrupt(f"{STATE_FILE}: {e}") from None
    if not isinstance(d, dict):
        raise StateCorrupt(f"{STATE_FILE}: el contenido no es un objeto JSON")
    return d


def save(data: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
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
