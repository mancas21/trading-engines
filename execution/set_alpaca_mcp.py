"""Guarda las claves API de Alpaca en execution/.env y activa el MCP server.

    python execution/set_alpaca_mcp.py

Igual que set_keys.py pero para la pata Alpaca: pide key y secret con
`getpass` (no se ven al escribir), valida el formato (paper=PK..., live=AK...),
las escribe en execution/.env y pone enabled=true en el servidor `alpaca` de
~/.zcode/cli/config.json.

Las claves NO pasan por el chat ni por la config de ZCode: el MCP server las
lee del .env via --env-file. Se necesitan claves del dashboard
(https://app.alpaca.markets -> la cuenta paper -> API keys), no las del login
OAuth del CLI (ese token no sirve para la API REST).

Despues: reiniciar ZCode y mirar Settings -> MCP.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
CONFIG = Path.home() / ".zcode" / "cli" / "config.json"

KEYS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")


def mask(s: str) -> str:
    return f"{'*' * 8}...{s[-4:]} (len {len(s)})" if len(s) > 8 else "*" * len(s)


def ask(name: str) -> str:
    while True:
        v = getpass.getpass(f"{name} (no se muestra al escribir): ").strip()
        if not v:
            print("  vacia, prueba otra vez.")
            continue
        if v.lower().startswith(("http", "browse ")):
            print("  eso parece una URL o un comando, no una clave.")
            continue
        if not v.startswith(("PK", "AK")):
            print("  una clave de Alpaca empieza por PK (paper) o AK (live).")
            if input("  usarla igualmente? [s/N] ").strip().lower() != "s":
                continue
        return v


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


def enable_server() -> str:
    """enabled=true en mcp.servers.alpaca. Escritura atomica."""
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    srv = data.get("mcp", {}).get("servers", {}).get("alpaca")
    if srv is None:
        return "sin entrada 'alpaca' en config.json (no se toca nada)"
    if srv.get("enabled") is True:
        return "server ya estaba enabled"
    srv["enabled"] = True
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CONFIG)
    return "server 'alpaca' enabled=true"


def main() -> int:
    if not CONFIG.is_file():
        print(f"no existe {CONFIG}; configura el MCP server primero (README §6).")
        return 2
    print(f"fichero de claves: {ENV}")
    print("Genera las claves en https://app.alpaca.markets -> cuenta PAPER ->")
    print("API keys. Las del login OAuth del CLI no sirven para la API REST.\n")

    key = ask(KEYS[0])
    secret = ask(KEYS[1])
    if key.startswith("AK"):
        print("\nAVISO: clave live (AK...) con ALPACA_PAPER_TRADE=true no "
              "autentica contra paper. Usa las claves de la cuenta paper (PK...).")

    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.is_file() else []
    lines = put(lines, KEYS[0], key)
    lines = put(lines, KEYS[1], secret)
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nenv escrito. key={mask(key)}  secret={mask(secret)}")

    print(enable_server())
    print("\nAhora reinicia ZCode y comprueba Settings -> MCP.")
    print("Prueba rapida del motor (sin MCP):  python execution/alpaca_check.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
