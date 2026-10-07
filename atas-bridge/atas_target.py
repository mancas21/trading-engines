"""Fase 1 — selector de símbolo ATAS. La cobertura es UN símbolo, así que elegirlo ES el diseño.

POR QUÉ
=======
ATAS lleva UN gráfico con Smart Tape + DOM por símbolo (restricción confirmada
por el usuario; TradingView igual). Antes del rediseño el símbolo abierto era
XRP por CASUALIDAD — el que estiveras mirando cuando se cargó el exportador.
Con cobertura = 1, "cuál símbolo tiene L2 real" es la decisión de arquitectura
más importante del sistema, y no puede seguir siendo un accidente.

QUÉ HACE
========
  - declara un objetivo (símbolo + razón) en atas_target.json
  - lee el gráfico REAL desde el log de ATAS y lo compara
  - avisa si no coinciden, para que el operador lo arregle manualmente
    (no se puede cambiar el gráfico de ATAS por API de forma fiable)

Uso:
  python atas_target.py set SOL "mejor liquidez spot, CVD más profundo"
  python atas_target.py show
  python atas_target.py show --json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "atas_target.json"
ATAS_LOG = Path(os.path.expandvars(r"%APPDATA%/ATAS X/Logs"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(sym: str) -> str:
    """'SOL/USDT:USDT' -> 'SOLUSDT'. ATAS y Binance no usan el mismo formato."""
    s = str(sym or "").strip().upper()
    s = s.split(":")[0]
    s = s.replace("/", "").replace("-", "").replace("_", "")
    if s.endswith("USDT") or s.endswith("USDC"):
        return s
    return s + "USDT"


def load() -> dict:
    if TARGET.exists():
        try:
            return json.loads(TARGET.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save(d: dict) -> None:
    d["updated_at"] = now_iso()
    TARGET.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")


def charted_symbol() -> tuple[str | None, str | None]:
    """El símbolo REALMENTE chartado, leído del log de ATAS.

    El log registra 'Unsubscribing from market depth X' seguido de
    'Subscribing to market depth Y' cuando ATAS cambia de gráfico: el último
    subscribe es el gráfico vivo. Un fichero de exportador congelado NO indica
    el gráfico actual, que es justo la confusión que-causó el bug anterior.
    """
    logs = sorted(glob.glob(str(ATAS_LOG / "app_*.log")))
    if not logs:
        return None, "no se encontró ningún log de ATAS"
    latest = logs[-1]
    try:
        lines = Path(latest).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return None, f"no se pudo leer {latest}: {e}"
    pat = re.compile(r"Subscribing to market depth (\w+)")
    sym = None
    for line in lines[-4000:]:
        m = pat.search(line)
        if m:
            sym = m.group(1).upper()
    return sym, (f"{os.path.basename(latest)} (edad "
                 f"{time.time() - os.path.getmtime(latest):.0f}s)")


def bridge_live() -> dict:
    """Qué símbolo cree el puente que está vivo."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=10) as r:
            h = json.loads(r.read().decode())
        return {"live_symbols": h.get("live_symbols"),
                "frozen_exporter_files": h.get("frozen_exporter_files")}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:60]}"}


def cmd_set(a) -> int:
    sym = normalize(a.symbol)
    d = load()
    prev = d.get("symbol")
    d = {
        "symbol": sym,
        "reason": a.reason,
        "set_at": now_iso(),
        "previous_symbol": prev,
        "note": "ATAS no se cambia por API de forma fiable: abre este símbolo "
                "manualmente y verifica con `show`.",
    }
    save(d)
    print(f"  objetivo -> {sym}")
    if a.reason:
        print(f"  razón    -> {a.reason}")
    if prev and prev != sym:
        print(f"  (antes: {prev})")
    charted, src = charted_symbol()
    print(f"  gráfico ATAS ahora: {charted or 'desconocido'}  [{src}]")
    if charted and charted != sym:
        print(f"  ATENCIÓN: no coincide. Abre {sym} en ATAS.")
    else:
        print("  coincide.")
    return 0


def cmd_show(a) -> int:
    d = load()
    charted, src = charted_symbol()
    bl = bridge_live()
    info = {
        "target": d.get("symbol"),
        "target_reason": d.get("reason"),
        "target_set_at": d.get("set_at"),
        "atas_chart": charted,
        "atas_chart_source": src,
        "match": (d.get("symbol") == charted) if (d.get("symbol") and charted) else None,
        "bridge": bl,
    }
    if a.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0
    print(f"  objetivo      : {info['target'] or '(sin objetivo declarado)'}")
    if info["target_reason"]:
        print(f"  razón         : {info['target_reason']}")
    if info["target_set_at"]:
        print(f"  fijado        : {info['target_set_at']}")
    print(f"  gráfico ATAS  : {charted or 'desconocido'}  [{src}]")
    m = info["match"]
    print(f"  coincide      : " + ("SÍ" if m else ("NO" if m is False else "?")))
    if not info["match"]:
        print("  → el L2 real está en otro símbolo. Todo lo que se lea de ATAS")
        print("    describe otro activo. Abre el objetivo en ATAS o cambia el objetivo.")
    live = (bl.get("live_symbols") or {})
    if live:
        print(f"  puente vivo   : {list(live.keys())}")
    frozen = bl.get("frozen_exporter_files") or {}
    if frozen:
        print(f"  congelados    : {list(frozen.keys())}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("set"); p1.add_argument("symbol"); p1.add_argument("reason", nargs="?", default="")
    p2 = sub.add_parser("show"); p2.add_argument("--json", action="store_true")
    a = ap.parse_args()
    return cmd_set(a) if a.cmd == "set" else cmd_show(a)


if __name__ == "__main__":
    raise SystemExit(main())