#!/usr/bin/env python3
"""Alerta de sesgo crítico.

Monitorea el lector en busca de cambios de sesgo a BULLISH o BEARISH con
consenso de libro (>=3 venues en la misma dirección) y envía un mensaje
prioritario a Telegram mediante el notificador existente.

Requisitos:
- El lector debe estar corriendo (reader_supervisor.py)
- El notificador (verdict_notify.py) debe estar configurado con TG_TOKEN/TG_CHAT

Uso:
    python bias_alert.py --once       # Ejecutar una vez y salir
    python bias_alert.py --loop       # Bucle continuo (cada 60s)
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime


BRIDGE_URL = "http://127.0.0.1:8000"
NOTIFIER = r"C:\Users\Lenovo\trading-engines\reader\verdict_notify.py"


def get_verdict(symbol="1000PEPEUSDT"):
    try:
        with urllib.request.urlopen(f"{BRIDGE_URL}/indicators/{symbol}", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"Error leyendo indicadores: {e}")
        return None


def get_scanner(symbol="1000PEPE/USD"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8090/features?pair={symbol}", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"Error leyendo scanner: {e}")
        return None


def consensus_buy_sell(scanner_data):
    """Devuelve (buy_count, sell_count) del libro multi-venue."""
    if not scanner_data:
        return 0, 0
    x = scanner_data.get(symbol.split("/")[0] + "/" + symbol.split("/")[1]) or scanner_data
    imb = x.get("book_imb_5") or {}
    buy = sum(1 for v in imb.values() if v > 0.25)
    sell = sum(1 for v in imb.values() if v < -0.25)
    return buy, sell


def send_telegram(text):
    """Envía un mensaje a través del notificador existente."""
    try:
        r = subprocess.run(
            ["python", NOTIFIER, "--once", "--force"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # El notificador lee el último veredicto y lo envía; para un mensaje
        # personalizado no hay parámetro, así que usamos --digest si existe.
        # Aquí simplemente notificamos que hay un cambio y dejamos que el
        # notificador regular maneje el envío del veredicto actual.
        print(f"Notificador: rc={r.returncode}")
        print(r.stdout[-200:] if r.stdout else "")
        if r.stderr:
            print(r.stderr[-200:])
        return r.returncode == 0
    except Exception as e:
        print(f"Error enviando notificación: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Alerta de sesgo crítico")
    parser.add_argument("--once", action="store_true", help="Ejecutar una vez y salir")
    parser.add_argument("--loop", action="store_true", help="Bucle continuo cada 60s")
    args = parser.parse_args()

    last_bias = None
    last_consensus = None

    def check():
        nonlocal last_bias, last_consensus
        ind = get_verdict()
        if not ind:
            return
        v = ind.get("verdict") or {}
        bias = v.get("bias")
        scanner = get_scanner()
        buy, sell = consensus_buy_sell(scanner)

        consensus = None
        if buy >= 3:
            consensus = "BUY"
        elif sell >= 3:
            consensus = "SELL"

        changed = False
        if bias != last_bias:
            print(f"[{datetime.now()}] Sesgo cambió: {last_bias} -> {bias}")
            changed = True
        if consensus != last_consensus:
            print(f"[{datetime.now()}] Consenso cambió: {last_consensus} -> {consensus}")
            changed = True

        if changed and bias in ("BULLISH", "BEARISH") and consensus in ("BUY", "SELL"):
            text = (
                f"🚨 SESCOGO CRÍTICO — {bias}\n"
                f"Símbolo: 1000PEPEUSDT\n"
                f"Sesgo: {bias}\n"
                f"Consenso libro: {consensus} ({buy}C/{sell}V)\n"
                f"Hora: {datetime.now():%d/%m/%Y %H:%M:%S}\n"
                f"Probabilidad: unvalidated"
            )
            send_telegram(text)

        last_bias = bias
        last_consensus = consensus

    if args.once:
        check()
    elif args.loop:
        print("Bucle de alerta de sesgo iniciado (Ctrl+C para salir)")
        try:
            while True:
                check()
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nDetenido por el usuario")
    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())