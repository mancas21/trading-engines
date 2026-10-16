#!/usr/bin/env python3
"""Pull a full live snapshot from the running engine (atas-bridge :8000) plus raw
Binance futures klines, and persist everything to ./raw for offline analysis.

Read-only: no exchange keys, no order endpoints. Public market data only.
"""
import json
import os
import time
import urllib.error
import urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw")
os.makedirs(OUT, exist_ok=True)

BRIDGE = "http://127.0.0.1:8000"
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "SUIUSDT", "HYPEUSDT",
        "SPCXUSDT", "NVDAUSDT", "XAUUSDT"]
SHORT = {s: s.replace("USDT", "") for s in SYMS}


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "live-audit/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # keep the status AND the body: the bridge answers 503 {"detail":"no_klines"}
        # and a bare exception string loses that reason.
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:  # noqa: BLE001
            pass
        return {"__error__": f"HTTP {e.code} {e.reason}", "__status__": e.code, "__body__": body}
    except Exception as e:  # noqa: BLE001
        return {"__error__": f"{type(e).__name__}: {e}", "__status__": None}


snap = {"pulled_at": time.time(), "pulled_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "bridge": {}, "binance": {}}

# Lista de sondas: SOLO endpoints vivos del bridge.
#
# 2026-10-16: se retiraron tres sondas que no podian moverse nunca mas. Medido en
# vivo — de las tres entradas que la tarjeta listaba en "FEED DEGRADED", dos no
# eran feeds:
#   - `calls` y `calls/stats`: el subsistema /calls se borro en la Fase 6
#     (orderflow_calls.py, la ruta perdedora) y el bridge contesta 404. Sondeados
#     en cada pull, metian dos lineas fijas en el `degraded` de live_read.json.
#     Es el mismo defecto que watchdog.py corrigio el 2026-10-15 para su propio
#     checker de /calls: un control retirado que se sigue anunciando.
#   - `quant`: el buffer que llena el QAE con POST /quant-alert. Con el QAE
#     cancelado no puede recibir nada: sondeado solo podia devolver {} para
#     siempre, y la tarjeta imprimia "freshest alerts: none" en cada lectura.
for ep in ["health", "health/detailed", "dom", "cvd", "taker-cvd",
           "derivatives", "indicators", "blackout/status"]:
    snap["bridge"][ep] = get(f"{BRIDGE}/{ep}")

# El simbolo PRIMARIO es el que tiene ATAS en el grafico y NO tiene por que estar en la
# cesta de 9. MEDIDO 2026-10-16: con ATAS en JUP/USDT (instrumento elegido, atas_target
# match=True) la cesta no contenia su ticker y la tarjeta moria con KeyError: 'JUPUSDT';
# solo funcionaba mientras el grafico estuviera en un simbolo de la cesta (en HEAD:
# SUIUSDT). Se descargan tambien sus datos; la cesta sigue siendo los 9.
_ind = snap["bridge"].get("indicators")
_primary = ((_ind.get("ticker") or "") if isinstance(_ind, dict)
            and "__error__" not in _ind else "").upper()
SYMS_ALL = SYMS + ([_primary] if _primary and _primary not in SYMS else [])

# per-symbol context (value area / vol regime / expected move) — hits Binance klines
# NOTE: /state/{sym} is fetched by nothing in this toolchain and 1d klines are unused;
# dropping both cut the pull from ~110s to a few seconds.
snap["bridge"]["context"] = {}
for s in SYMS_ALL:
    snap["bridge"]["context"][s] = get(f"{BRIDGE}/context/{s}")

# raw klines straight from Binance futures (public)
FAPI = "https://fapi.binance.com/fapi/v1/klines"
for s in SYMS_ALL:
    snap["binance"][s] = {}
    for iv, lim in (("15m", 1000), ("1h", 1000), ("4h", 500)):
        k = get(f"{FAPI}?symbol={s}&interval={iv}&limit={lim}", timeout=30)
        snap["binance"][s][iv] = k
        time.sleep(0.05)

with open(os.path.join(OUT, "live_snapshot.json"), "w", encoding="utf-8") as f:
    json.dump(snap, f, indent=1)

# quick sanity report
def ok(v):
    return "ERROR" if isinstance(v, dict) and "__error__" in v else "ok"

print("pulled_at", snap["pulled_at_iso"])
for ep, v in snap["bridge"].items():
    if ep in ("context", "state_by_symbol"):
        continue
    print(f"  bridge/{ep:16s} {ok(v)}")
print("  context:", {s: ok(v) for s, v in snap["bridge"]["context"].items()})
print("  binance klines:", {s: {iv: (len(k) if isinstance(k, list) else "ERR")
                                for iv, k in d.items()} for s, d in snap["binance"].items()})
