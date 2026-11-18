#!/usr/bin/env python3
"""Reinicio automático del bridge de ATAS.

Si :8000 no responde, mata y relanza bridge_server.py.

Uso:
    python restart_bridge.py --once   # Intenta una vez
    python restart_bridge.py --loop   # Bucle de vigilancia cada 60s
"""

import subprocess
import sys
import time
import urllib.request


BRIDGE_URL = "http://127.0.0.1:8000/health"
BRIDGE_SCRIPT = r"C:\Users\Lenovo\trading-engines\atas-bridge\bridge_server.py"
BRIDGE_DIR = r"C:\Users\Lenovo\trading-engines\atas-bridge"


def bridge_alive():
    try:
        with urllib.request.urlopen(BRIDGE_URL, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def restart_bridge():
    print(f"[{__import__('datetime').datetime.now():%d/%m/%Y %H:%M:%S}] Bridge no responde. Relanzando...")
    try:
        # Matar procesos existentes de bridge_server.py
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bridge_server.py' } | Stop-Process -Force -ErrorAction SilentlyContinue",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        import time as _t
        _t.sleep(2)

        # Lanzar nuevo puente
        proc = subprocess.Popen(
            ["python", "bridge_server.py"],
            cwd=BRIDGE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        print(f"  Bridge lanzado con PID {proc.pid}")
        return True
    except Exception as e:
        print(f"  ERROR relanzando bridge: {e}")
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Watchdog del bridge de ATAS")
    parser.add_argument("--once", action="store_true", help="Verificar una vez y salir")
    parser.add_argument("--loop", action="store_true", help="Bucle cada 60s")
    args = parser.parse_args()

    if args.once:
        if bridge_alive():
            print("Bridge :8000 responde OK")
            return 0
        else:
            print("Bridge :8000 NO responde. Intentando reinicio...")
            return 0 if restart_bridge() else 1
    elif args.loop:
        print("Watchdog del bridge iniciado (Ctrl+C para salir)")
        try:
            while True:
                if not bridge_alive():
                    restart_bridge()
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nDetenido por el usuario")
        return 0
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    import datetime
    sys.exit(main())