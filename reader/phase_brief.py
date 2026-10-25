"""Brief de FASE multi-par a Telegram — sustituye al digest de 15 min.

POR QUÉ EXISTE
==============
Sustituye el digest ejecutivo de `verdict_notify.py --digest` (2026-10-24,
peticion del usuario): el digest era una plantilla de un solo simbolo que se
enviaba cada 15 min y el usuario dejo de quererlo. Lo que pide es el analisis
de FASE de todos los pares activos del scanner ("en que momento estamos:
rango, pullback, bounce, trend, reversal") en lenguaje de trader.

DOS PRODUCTORES, UN ESTADO
==========================
La version de CALIDAD (la que escribe el modelo con sintesis) la produce la
automatizacion del agente (cron de ZCode) mientras la app esta abierta. Este
script es el SUELO siempre-activo que corre bajo el supervisor del reader.
Ambos escriben el mismo marcador `phase_state.json`; el que envia primero
deja al otro en silencio (stale > PHASE_EVERY_S). Asi no hay duplicados.

QUE ES Y QUE NO ES
==================
Clasificacion mecanica: posicion en el rango 24h, minimos/mx crecientes
(1h, 12v vs 12v previas), profundidad de corrida. Es estructura, no senal:
ninguna linea de aqui decide un sesgo (eso sigue siendo verdict.py, Reglas
6/7). El scanner (:8090) aporta el veredicto multi-venue por par, que hoy
se publica tal cual (neutral/score).

Uso:
  python phase_brief.py --once --dry-run   # ver el brief sin enviar
  python phase_brief.py --once             # enviar si toca por tiempo
  python phase_brief.py --force            # enviar ignorando el marcador
  python phase_brief.py --status           # estado del marcador
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "phase_state.json"

PHASE_EVERY_S = 3600.0        # 1 h: la frecuencia que reemplaza al 15-min
STALE_GRACE_S = 1800.0        # el cron del agente refresca el marcador; si
                              # lleva >30 min sin poder escribirse, el suelo
                              # asume que la app esta cerrada y toma el turno.
SCANNER = "http://127.0.0.1:8090"
FAPI = "https://fapi.binance.com"

# Pares del scanner (los mismos que /rank). Si el scanner responde, el score
# y la direccion se leen de ahi; esta lista es el fallback y el orden del
# sweep de Binance.
PAIRS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "HYPE": "HYPEUSDT", "SOL": "SOLUSDT",
    "XAU": "XAUUSDT", "XRP": "XRPUSDT", "JUP": "JUPUSDT", "NVDA": "NVDAUSDT",
    "SPCX": "SPCXUSDT", "SUI": "SUIUSDT",
}


def _get(url: str, timeout: float = 8.0, retries: int = 1):
    # Binance fapi tolera llamadas sueltas pero banea por ráfaga ~1 min
    # (medido 2026-10-24: 20 llamadas en 4s -> todo 4xx un minuto, y la
    # llamada suelta siguiente OK). Retry con backoff + pacing en sweep().
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            if i < retries:
                time.sleep(1.5)
    return None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def sweep() -> list[dict]:
    """Una fila por par: metricas de estructura 24h + veredicto scanner."""
    rank = {}
    r = _get(f"{SCANNER}/rank")
    for row in (r or {}).get("rank", []):
        rank[row["pair"].split("/")[0]] = row
    rows = []
    for name, sym in PAIRS.items():
        t = _get(f"{FAPI}/fapi/v1/ticker/24hr?symbol={sym}", retries=2)
        time.sleep(0.4)
        k = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=1h&limit=24", retries=2)
        time.sleep(0.4)
        if not t or not k or len(k) < 24:
            continue
        last = float(t["lastPrice"]); hi = float(t["highPrice"]); lo = float(t["lowPrice"])
        closes = [float(b[4]) for b in k]
        pos = (last - lo) / (hi - lo) if hi > lo else 0.5
        c12, cprev = closes[-12:], closes[-24:-12]
        hl = min(c12) > min(cprev)      # minimos crecientes
        lh = max(c12) < max(cprev)      # maximos decrecientes
        sc = rank.get(name) or {}
        rows.append({
            "name": name, "last": last, "pos": pos, "hl": hl, "lh": lh,
            "from_lo": (last / lo - 1) * 100 if lo else 0.0,
            "chg": float(t["priceChangePercent"]),
            "dir": sc.get("direction", "?"), "score": sc.get("score"),
        })
    return rows


def classify(r: dict) -> tuple[str, str]:
    """(grupo, etiqueta) mecanicos. Estructura, no senal."""
    p, hl, lh = r["pos"], r["hl"], r["lh"]
    if hl and lh:
        return "compresion", "rango comprimido (decision pendiente)"
    if p >= 0.75 and hl:
        return "alza", "tendencia alcista en extension (pegado al maximo)"
    if p >= 0.60 and hl:
        return "alza", "tendencia alcista"
    if p <= 0.25 and lh:
        return "debiles", "tendencia bajista (pegado al minimo)"
    if p <= 0.40 and lh:
        return "debiles", "rango con sesgo bajista"
    if hl:
        return "recuperacion", "recuperacion / sesgo alcista suave"
    return "rango", "rango sin direccion"


GROUP_ORDER = [
    ("alza", "EN TENDENCIA AL ALZA"),
    ("recuperacion", "EN RECUPERACION"),
    ("compresion", "EN RANGO COMPRIMIDO"),
    ("rango", "EN RANGO SIN DIRECCION"),
    ("debiles", "LOS DEBILES"),
]


def compose(rows: list[dict]) -> str:
    groups: dict[str, list[str]] = {k: [] for k, _ in GROUP_ORDER}
    for r in rows:
        g, label = classify(r)
        signo = "+" if r["chg"] >= 0 else ""
        groups[g].append(
            f"{r['name']} {r['last']:g} — {label} "
            f"({signo}{r['chg']:.1f}% 24h, a {r['from_lo']:.1f}% del minimo)")
    L = [f"FASES — {datetime.now():%H:%M}", ""]
    for key, title in GROUP_ORDER:
        if groups[key]:
            L.append(title)
            L.extend(groups[key])
            L.append("")
    live = [r for r in rows if r["score"] is not None]
    if live and all(r["dir"] == "neutral" for r in live):
        L.append("Scanner multi-venue: neutral en todos los pares — sin "
                 "consenso de flujo direccional.")
    elif live:
        con_lado = [f"{r['name']} {r['dir']}/{r['score']}" for r in live if r["dir"] != "neutral"]
        L.append("Scanner con lado: " + (", ".join(con_lado) if con_lado else "ninguno"))
    L.append("(estructura 24h en barras 1h; clasificacion mecanica, "
             "no sesgo de Reglas 6/7)")
    return "\n".join(L)


def should_send(force: bool) -> bool:
    if force:
        return True
    st = load_state()
    last = float(st.get("last_sent") or 0.0)
    return (time.time() - last) > max(PHASE_EVERY_S, STALE_GRACE_S)


def send(text: str) -> tuple[bool, str]:
    # Reusa credenciales y metodo del notificador del reader: una sola
    # implementacion del envio, sin copiar el token a otro sitio.
    import verdict_notify as N
    return N.send(text)


def do_once(dry: bool = False, force: bool = False) -> int:
    if not should_send(force):
        st = load_state()
        age = (time.time() - float(st.get("last_sent") or 0)) / 60
        print(f"  phase: todavia no toca ({age:.0f} min desde el ultimo; "
              f"por {st.get('by', '?')})")
        return 0
    rows = sweep()
    if not rows:
        print("  phase: sin datos (ni Binance ni scanner respondieron)")
        return 1
    text = compose(rows)
    print("  PHASE BRIEF — " + "-" * 46)
    print("  " + text.replace("\n", "\n  "))
    print("  " + "-" * 46)
    if dry:
        print("  (dry-run: no enviado)")
        return 0
    ok, msg = send(text)
    print(f"  telegram: {'OK' if ok else 'FALLO — ' + msg}")
    if ok:
        save_state({"last_sent": time.time(),
                    "sent_iso": datetime.now().isoformat(),
                    "by": "supervisor"})
    return 0 if ok else 1


def do_status() -> int:
    st = load_state()
    if not st:
        print("sin envios registrados")
        return 0
    age = (time.time() - float(st.get("last_sent") or 0)) / 60
    print(f"ultimo: {st.get('sent_iso')} por {st.get('by')} hace {age:.0f} min")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        return do_status()
    return do_once(dry=a.dry_run, force=a.force)


if __name__ == "__main__":
    raise SystemExit(main())
