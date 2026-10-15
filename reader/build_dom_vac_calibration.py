"""build_dom_vac_calibration.py — calibracion por venue de book_imb_5.

POR QUE EXISTE
==============
La condicion (c) de las reglas 6/7 compara `book_imb_5` de 4 venues contra un
umbral ABSOLUTO (0.25). Medido sobre 329 observaciones de reader.db, eso no mide
acuerdo: mide "quevenue vive fuera de su propia banda":

    venue    media     std     >+0.25%   <-0.25%
    binance  +0.080    0.214      21.1       8.9
    okx      -0.199    0.145       1.5      38.2
    kraken   +0.129    0.499      41.9      25.1
    bybit    +0.081    0.155      17.4       1.5

OKX cruza +0.25 el 1,5% de las veces: por estructura jamas puede votar "compra",
asi que exigir 3 venues equivalia a exigir los otros 3 a la vez. Kraken, con std
3,4x la de bybit, cruza el umbral el 42% solo con ruido.

El arreglo es normalizar cada venue contra SU propia distribucion: un voto pasa a
significar "este venue esta a 1 sigma de su media", que es la unica forma de que
los 4 numerios sean comparables.

Selectividad conservada: el umbral absoluto (0.25 / 3 de 4) disparaba en el 6,0%
de 331 ciclos; el normalizado (z>=1.0 / 3 de 4) en el 5,7%. No se ha vuelto mas
permisivo; se ha vuelto comparable.

Uso:
    python build_dom_vac_calibration.py            # regenera el JSON
    python build_dom_vac_calibration.py --check     # solo lo ensena

NO VALIDADO COMO EDGE: la selectividad esta medida; que los disparos GANEN no.
El JSON que genera lo consume verdict.dom_vac_eval para normalizar, nada mas.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "reader.db"
OUT = HERE / "dom_vac_calibration.json"

# Muestras minimas por venue para publicar estadisticos. Con menos, el venue se
# marca no calibrado y verdict.py no lo usa (mejora UNEVALUABLE que un numero
# inventado).
MIN_N = 30


def collect() -> dict[str, list[float]]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT dom_vac_raw FROM verdicts WHERE dom_vac_raw IS NOT NULL"
        ).fetchall()
    finally:
        con.close()
    out: dict[str, list[float]] = {}
    for (txt,) in rows:
        try:
            raw = (json.loads(txt) or {}).get("raw") or {}
        except Exception:
            continue
        for k, v in raw.items():
            if isinstance(v, (int, float)):
                out.setdefault(str(k), []).append(float(v))
    return out


def build() -> dict:
    samples = collect()
    venues, skipped = {}, {}
    for venue, vals in sorted(samples.items()):
        if len(vals) < MIN_N:
            skipped[venue] = len(vals)
            continue
        venues[venue] = {
            "n": len(vals),
            "mean": round(statistics.mean(vals), 6),
            "std": round(statistics.pstdev(vals), 6),
            # percentiles para poder diagnosticar sin recalcular
            "p10": round(sorted(vals)[int(0.10 * (len(vals) - 1))], 6),
            "p50": round(sorted(vals)[int(0.50 * (len(vals) - 1))], 6),
            "p90": round(sorted(vals)[int(0.90 * (len(vals) - 1))], 6),
        }
    total = sum(len(v) for v in samples.values())
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "reader.db:verdicts.dom_vac_raw",
        "method": (
            "media/std por venue sobre book_imb_5 historico. verdict.dom_vac_eval "
            "normaliza (x-mean)/std antes de comparar, para que venues con sesgo y "
            "escala distintos (okx media -0.199, kraken std 0.499) sean comparables."
        ),
        "min_n": MIN_N,
        "samples_total": total,
        "venues": venues,
        "skipped_venues": skipped,
        "note": (
            "Selectividad conservada frente al umbral absoluto que sustituye "
            "(6.0% -> 5.7% de ciclos con consenso). NO validado como edge."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="solo mostrar")
    a = ap.parse_args()
    cal = build()
    if a.check:
        print(json.dumps(cal, indent=2, ensure_ascii=False))
        return 0
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(cal, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)
    print(f"escrito {OUT.name}: {len(cal['venues'])} venues, "
          f"{cal['samples_total']} muestras")
    for v, s in cal["venues"].items():
        print(f"  {v:<8} n={s['n']:<5} media={s['mean']:+.3f} std={s['std']:.3f}")
    if cal["skipped_venues"]:
        print(f"  omitidos por n<{MIN_N}: {cal['skipped_venues']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
