#!/usr/bin/env python3
"""Exporta veredictos del lector a CSV/JSON para análisis externo.

Lee reader.db y escribe los veredictos de un día (o rango) a un archivo.

Uso:
    python export_daily_verdicts.py
    python export_daily_verdicts.py --date 2026-11-14
    python export_daily_verdicts.py --format csv --out export.csv
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta


DB_PATH = r"C:\Users\Lenovo\trading-engines\reader\reader.db"


def main():
    parser = argparse.ArgumentParser(description="Exporta veredictos del lector")
    parser.add_argument("--date", type=str, help="Fecha YYYY-MM-DD (por defecto hoy)")
    parser.add_argument("--format", choices=["csv", "json"], default="csv")
    parser.add_argument("--out", type=str, help="Ruta de salida (por defecto autogenerada)")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Ruta a reader.db")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: no existe {args.db}")
        return 1

    # Determinar rango de fechas
    if args.date:
        try:
            target = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print("ERROR: fecha debe ser YYYY-MM-DD")
            return 1
    else:
        target = datetime.now()

    start_ts = datetime(target.year, target.month, target.day).timestamp()
    end_ts = datetime(target.year, target.month, target.day, 23, 59, 59).timestamp()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM verdicts
        WHERE ts >= ? AND ts <= ?
        ORDER BY ts ASC
        """,
        (start_ts, end_ts),
    )
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    conn.close()

    if not rows:
        print(f"No hay veredictos para {args.date or 'hoy'}")
        return 0

    # Nombre de salida
    if args.out:
        out_path = args.out
    else:
        suffix = args.date or datetime.now().strftime("%Y%m%d")
        out_path = os.path.join(
            r"C:\Users\Lenovo\trading-engines\automation\exports",
            f"verdicts_{suffix}.{args.format}",
        )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    if args.format == "json":
        data = [dict(zip(cols, r)) for r in rows]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    else:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(rows)

    print(f"Exportados {len(rows)} veredictos a {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())