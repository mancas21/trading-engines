#!/usr/bin/env python3
"""Backup automático de bases de datos del stack trading-engines.

Copia reader.db y scanner.db a una carpeta de respaldo con timestamp.
Mantiene los últimos N backups y borra los más antiguos.

Uso:
    python backup_dbs.py
    python backup_dbs.py --keep 10
"""

import argparse
import os
import shutil
import sys
from datetime import datetime


SRC_FILES = [
    (r"C:\Users\Lenovo\trading-engines\reader\reader.db", "reader"),
    (r"C:\Users\Lenovo\trading-engines\scanner-engine\data\scanner.db", "scanner"),
]

BACKUP_DIR = r"C:\Users\Lenovo\trading-engines\automation\backups"


def main():
    parser = argparse.ArgumentParser(description="Backup de DBs del stack")
    parser.add_argument("--keep", type=int, default=10, help="Máximo backups a conservar")
    args = parser.parse_args()

    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    copied = []

    for src, name in SRC_FILES:
        if not os.path.exists(src):
            print(f"ADVERTENCIA: no existe {src}")
            continue
        dst = os.path.join(BACKUP_DIR, f"{name}_{timestamp}.db")
        try:
            shutil.copy2(src, dst)
            copied.append(dst)
            print(f"OK: {dst}")
        except Exception as e:
            print(f"ERROR copiando {src}: {e}")

    # Rotar: mantener solo los últimos N por archivo base
    for name in ["reader", "scanner"]:
        files = sorted(
            [f for f in os.listdir(BACKUP_DIR) if f.startswith(name + "_") and f.endswith(".db")],
        )
        if len(files) > args.keep:
            for old in files[: len(files) - args.keep]:
                try:
                    os.remove(os.path.join(BACKUP_DIR, old))
                    print(f"Rotado (borrado): {old}")
                except Exception as e:
                    print(f"ERROR borrando {old}: {e}")

    print(f"\nBackups realizados: {len(copied)}")
    print(f"Directorio: {BACKUP_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())