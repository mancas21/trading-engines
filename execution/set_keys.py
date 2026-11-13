"""Guarda las claves del testnet en execution/.env sin mostrarlas.

    python execution/set_keys.py

Pide key y secret con `getpass` (no se ven al escribir), valida que parezcan
claves de Binance y reescribe solo esas dos lineas del .env. No imprime nada
mas alla de la mascara (ultimos 4 caracteres) para poder comprobar que se
pegaron bien.

Si el fichero no existe, lo crea a partir de .env.example.
"""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
EXAMPLE = HERE / ".env.example"

KEYS = ("BINANCE_TESTNET_KEY", "BINANCE_TESTNET_SECRET")


def mask(s: str) -> str:
    return f"{'*' * 8}...{s[-4:]} (len {len(s)})" if len(s) > 8 else "*" * len(s)


def read_lines() -> list[str]:
    src = ENV if ENV.is_file() else EXAMPLE
    return src.read_text(encoding="utf-8").splitlines()


def put(lines: list[str], key: str, val: str) -> list[str]:
    out, done = [], False
    for ln in lines:
        if re.match(rf"^\s*{key}\s*=", ln):
            out.append(f"{key}={val}")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={val}")
    return out


def ask(name: str) -> str:
    while True:
        v = getpass.getpass(f"{name} (no se muestra al escribir): ").strip()
        if not v:
            print("  vacia, prueba otra vez.")
            continue
        if v.lower().startswith(("browse ", "http")):
            print("  eso parece un comando o una URL, no una clave.")
            continue
        if len(v) < 20:
            print(f"  demasiado corta ({len(v)}). Una clave de Binance ronda 64 caracteres.")
            if input("  usarla igualmente? [s/N] ").strip().lower() != "s":
                continue
        return v


def main() -> int:
    print(f"fichero: {ENV}\n")
    print("Genera las claves en https://testnet.binancefuture.com -> API Key")
    print("Permisos: Reading + Futures. Sin retiros.\n")

    key = ask("BINANCE_TESTNET_KEY")
    secret = ask("BINANCE_TESTNET_SECRET")

    lines = read_lines()
    lines = put(lines, KEYS[0], key)
    lines = put(lines, KEYS[1], secret)
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nescrito. key={mask(key)}  secret={mask(secret)}")
    print("Ahora:  python execution/check_testnet.py --symbol XRPUSDT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
