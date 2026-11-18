#!/usr/bin/env python3
"""Reporte diario de rendimiento del lector.

Lee reader.db y calcula:
- Total de veredictos por sesgo
- Win rate de forward_return resueltos
- Profit factor bruto
- Drawdown máximo de la curva de equity (equity acumulada de forward_return)
- Trades ganadores/perdedores

Uso:
    python daily_performance_report.py
    python daily_performance_report.py --days 7
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta


DB_PATH = r"C:\Users\Lenovo\trading-engines\reader\reader.db"


def main():
    parser = argparse.ArgumentParser(description="Reporte diario de rendimiento")
    parser.add_argument("--days", type=int, default=1, help="Días hacia atrás a analizar")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Ruta a reader.db")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: no existe {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    # Verificar columnas
    cur.execute("PRAGMA table_info(verdicts)")
    cols = [c[1] for c in cur.fetchall()]
    if "forward_return" not in cols:
        print("ERROR: la tabla verdicts no tiene columna forward_return")
        conn.close()
        return 1

    since = None
    if args.days > 0:
        since = (datetime.now() - timedelta(days=args.days)).timestamp()
        cur.execute(
            "SELECT COUNT(*) FROM verdicts WHERE symbol = '1000PEPEUSDT' AND ts >= ?",
            (since,),
        )
        count = cur.fetchone()[0]
        print(f"Analizando {args.días if False else args.days} día(s) desde {datetime.fromtimestamp(since)}")
        print(f"Veredictos en periodo: {count}")

    # Sesgos
    cur.execute("SELECT bias, COUNT(*) FROM verdicts WHERE ts >= ? GROUP BY bias" if since else "SELECT bias, COUNT(*) FROM verdicts GROUP BY bias", (since,) if since else ())
    rows = cur.fetchall()
    print("\nVeredictos por sesgo:")
    for bias, cnt in rows:
        print(f"  {bias}: {cnt}")

    # Forward returns resueltos
    cur.execute(
        "SELECT forward_return FROM verdicts WHERE forward_return IS NOT NULL" + (
            " AND ts >= ?" if since else ""
        ),
        (since,) if since else (),
    )
    rets = [r[0] for r in cur.fetchall() if r[0] is not None]

    if not rets:
        print("\nNo hay forward returns resueltos en el periodo.")
        conn.close()
        return 0

    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    win_rate = len(wins) / len(rets) * 100 if rets else 0

    gross_profit = sum(w for w in wins) if wins else 0
    gross_loss = sum(l for l in losses) if losses else 0
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0

    # Drawdown de la curva de equity (equity acumulada de forward_return en bps)
    equity = 1.0
    max_equity = 1.0
    max_dd = 0.0
    for r in rets:
        equity *= (1 + r)
        if equity > max_equity:
            max_equity = equity
        dd = (max_equity - equity) / max_equity
        if dd > max_dd:
            max_dd = dd

    total_return = (equity - 1.0) * 100

    print("\n--- Métricas de forward_return ---")
    print(f"Operaciones resueltas: {len(rets)}")
    print(f"Ganadoras: {len(wins)} | Perdedoras: {len(losses)}")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Ganancia media: {avg_win*100:+.2f}% | Pérdida media: {avg_loss*100:+.2f}%")
    print(f"Ganancia bruta: {gross_profit*100:+.2f}% | Pérdida bruta: {gross_loss*100:+.2f}%")
    print(f"Profit factor: {profit_factor:.2f}")
    print(f"Drawdown máximo (equity curva): {max_dd*100:.2f}%")
    print(f"Retorno acumulado (equity): {total_return:+.2f}%")

    conn.close()
    return 0


if __name__ == "__main__":
    import os
    sys.exit(main())