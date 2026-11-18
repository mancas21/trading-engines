#!/usr/bin/env python3
#!/usr/bin/env python3
"""Health check del stack trading-engines.

Verifica:
- ATAS bridge :8000 (/health)
- Scanner VPS :8090 (/health)
- Procesos clave (ATAS bridge, reader supervisor)

Uso:
    python health_check.py
    python health_check.py --json
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from datetime import datetime


def check_http(url, timeout=8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return {"ok": True, "status": r.status, "error": None}
    except Exception as e:
        return {"ok": False, "status": None, "error": str(e)[:120]}


def check_process(pattern):
    """Devuelve True si hay algún proceso cuyo CommandLine contenga pattern."""
    try:
        cmd = (
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*"
            + pattern
            + "*' } | Select-Object -First 1 ProcessId, Name"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Health check del stack trading-engines")
    parser.add_argument("--json", action="store_true", help="Salida en JSON")
    args = parser.parse_args()

    results = {}

    # ATAS bridge
    atas_health = check_http("http://127.0.0.1:8000/health")
    atas_proc = check_process("bridge_server.py")
    results["atas_bridge"] = {
        "http": atas_health,
        "process": atas_proc,
        "ok": atas_health["ok"] and atas_proc,
    }

    # Scanner VPS
    scanner_health = check_http("http://127.0.0.1:8090/health")
    results["scanner_vps"] = {
        "http": scanner_health,
        "process": None,
        "ok": scanner_health["ok"],
    }

    # Reader supervisor
    reader_proc = check_process("reader_supervisor.py")
    results["reader_supervisor"] = {
        "http": None,
        "process": reader_proc,
        "ok": reader_proc,
    }

    all_ok = all(v["ok"] for v in results.values())

    if args.json:
        print(
            json.dumps(
                {
                    "ok": all_ok,
                    "timestamp": __import__("datetime").datetime.now().isoformat(),
                    "checks": results,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print("=" * 60)
        print("HEALTH CHECK — trading-engines")
        print("=" * 60)
        for name, data in results.items():
            status = "OK" if data["ok"] else "FALLO"
            print(f"{name:<20} {status}")
            if data["http"]:
                h = data["http"]
                print(f"  HTTP : {'ok' if h['ok'] else 'FALLO'} "
                      f"({h['status'] if h['status'] else h['error']})")
            if data["process"] is not None:
                print(f"  Proceso: {'ok' if data['process'] else 'NO ENCONTRADO'}")
        print()
        print("RESULTADO:", "TODO OK" if all_ok else "HA FALLOS")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())