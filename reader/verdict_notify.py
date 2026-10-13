"""Fase 4 — notificar SÓLO cambios de estado. Esto mata el spam original.

POR QUÉ CAMBIA EL MODELO
========================
El defecto original del motor no era generar señales: era NOTIFICARLAS. El
audit lo cuantificó — 19,9 señales por símbolo-día antes de VOL-1, con la
puerta de resultado emitiendo en 33 de cada 40. El usuario recibía ruido y
aprendió a desconfiar del sistema entero.

Un veredicto no es una señal: se puede leer 50 veces al día y cambiar sólo dos.
Por eso la unidad de notificación es el CAMBIO DE SESGO, no la lectura.

REGLAS DE LA NOTIFICACIÓN
========================
1. Se notifica al CAMBIAR el sesgo (NEUTRAL→BEARISH, BEARISH→NEUTRAL, ...).
   Repetir el mismo sesgo no notifica nunca.
2. NUNCA dos mensajes seguidos con el mismo sesgo, aunque el score se mueva.
   El score va en el mensaje; no es motivo de envío.
3. Un cambio a NEUTRAL tras una dirección es la parte que más importa (alguien
   estaba actuándose y ya no), así que se marca explícitamente.
4. Si el objetivo ATAS no coincide con el charted, NO se notifica: el veredicto
   no describe el activo pedido.
5. El estado persiste en disco. Si el proceso muere y vuelve, no re-notifica el
   cambio que ya notificó.
6. `--dry-run` no envía nada pero imprime lo que enviaría — para poder ver el
   formato sin escribir en tu Telegram.

Uso:
  python verdict_notify.py --once --dry-run     # ver el mensaje, no enviar
  python verdict_notify.py --once               # enviar si cambió el sesgo
  python verdict_notify.py --loop --interval 60
  python verdict_notify.py --status             # estado actual + último envío
  python verdict_notify.py --test               # enviar un mensaje de prueba
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = HERE / "notify_state.json"

# 2026-10-11 — DESACOPLADO de quant-alert-engine. Antes las credenciales de
# Telegram se leian de QAE/.env, lo que ataba el notificador a esa carpeta. El
# puente tiene su propio .env con TELEGRAM_TOKEN/TELEGRAM_CHAT_ID, asi que se
# lee ahi. `load_env` acepta ambas grafias (con y sin doble underscore) para
# que siga funcionando si alguien reintroduce una de las dos.
_ENV_CANDIDATES = (
    ROOT / "atas-bridge" / ".env",      # preferido: el puente es quien notifica
    HERE / ".env",
    ROOT / "quant-alert-engine" / ".env",  # ultimo recurso
)


def load_env() -> dict:
    """Lee el primer .env disponible. NUNCA lo escribe ni lo imprime."""
    out: dict = {}
    for env_path in _ENV_CANDIDATES:
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
        if out:
            break
    return out

# Sesgos que son "direccionales". NEUTRAL es la ausencia de dirección, y un
# retorno a NEUTRAL cuenta como evento porque cambia lo que debe hacer alguien.
DIRECTIONAL = ("BULLISH", "BEARISH")


def telegram_creds():
    env = load_env()
    tok = env.get("TELEGRAM__TOKEN") or env.get("TELEGRAM_TOKEN") or ""
    cid = env.get("TELEGRAM__CHAT_ID") or env.get("TELEGRAM_CHAT_ID") or ""
    return tok, cid


def send(text: str) -> tuple[bool, str]:
    tok, cid = telegram_creds()
    if not tok or not cid:
        return False, "sin TELEGRAM__TOKEN/CHAT_ID en .env"
    # Metodo documentado: endpoint /bot<token>/sendMessage + cuerpo JSON.
    # Se evita deliberadamente el helper del bridge, que envoyaba
    # form-encoded; heredar ese camino seria arrastrar un defecto conocido.
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    body = json.dumps({"chat_id": cid, "text": text,
                       "disable_web_page_preview": True}).encode()
    try:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode())
        return bool(j.get("ok")), (j.get("description") or "ok")
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")


def compose(r: dict) -> str:
    """Mensaje corto. Un veredicto que necesita un párrafo no es accionable."""
    v = (r["atas"].get("verdict") or {})
    bias = r["bias"]
    score = r["score"]
    d = v.get("dom") or {}
    c = v.get("cvd") or {}
    fp = v.get("footprint") or {}
    sym = v.get("symbol")

    prev = r.get("_prev_bias")
    if bias == "NEUTRAL":
        head = (f"NEUTRAL — se salio de {prev}, sin setup direccional"
                if prev in DIRECTIONAL else "NEUTRAL — sin setup direccional")
    else:
        arrow = "AL ALZA" if bias == "BULLISH" else "A LA BAJA"
        head = f"{bias} {arrow}"

    L = [head, ""]
    L.append(f"{sym}  px {v.get('price_last')}  score {score}/100")
    L.append(f"EMA {d.get('imbalance_pct_ema')}% (n={d.get('ema_n')}) "
             f"vs instantaneo {d.get('imbalance_pct')}%")
    L.append(f"footprint {fp.get('sell_heavy')} venta>70% / "
             f"{fp.get('buy_heavy')} compra>70%   POC {v.get('poc')}")
    L.append(f"CVD rachas +{c.get('streak_up')}/-{c.get('streak_down')}   "
             f"tape compra {v.get('tape_buy_pct')}%")

    which = "Regla 6" if bias == "BEARISH" else "Regla 7"
    L.append("")
    L.append(f"{which} satisfecha: las 3 condiciones.")
    if v.get("rule2", {}).get("applicable"):
        L.append(f"Regla 2: {v['rule2']['note']}")
    if not v.get("cvd_clean", True):
        L.append(f"aviso: CVD con hueco (missing {c.get('missing_bars')}) "
                 f"— la serie no es continua.")
    if not r.get("complete", True):
        L.append(f"fuentes ausentes: {', '.join(r.get('sources_missing', []))}")
    L.append("")
    L.append(f"probability: unvalidated  ({datetime.now():%H:%M})")
    return "\n".join(L)


def evaluate(r: dict) -> dict:
    """Decide si esto amerita un mensaje. Devuelve la decisión y el texto."""
    st = load_state()
    sym = (r["atas"].get("verdict") or {}).get("symbol") or "?"
    prev = st.get(sym, {}).get("bias")

    if r["bias"] == "UNAVAILABLE":
        return {"send": False, "why": "sin datos (UNAVAILABLE)", "prev": prev}
    if not r.get("scope_ok", True):
        return {"send": False, "why": "el objetivo ATAS no coincide con el charted",
                "prev": prev}
    if r["bias"] == prev:
        return {"send": False, "why": f"sesgo sin cambio ({prev})", "prev": prev}
    # Primer arranque: no "cambia" de nada. Se registra sin notificar, salvo que
    # ya sea direccional — en cuyo que SÍ es información que no tenías.
    if prev is None and r["bias"] not in DIRECTIONAL:
        return {"send": False, "why": "primer registro, sesgo neutral: se registra sin avisar",
                "prev": prev}
    r["_prev_bias"] = prev
    return {"send": True, "why": f"cambio de sesgo {prev or '—'} -> {r['bias']}",
            "prev": prev, "text": compose(r)}


def do_once(dry: bool = False) -> int:
    import live_reader as L
    r = L.read_all()
    dec = evaluate(r)
    if not dec["send"]:
        print(f"  no enviar: {dec['why']}")
        return 0
    print(f"  ENVIAR: {dec['why']}")
    print("  " + "-" * 52)
    print("  " + dec["text"].replace("\n", "\n  "))
    print("  " + "-" * 52)
    if dry:
        print("  (dry-run: no enviado)")
        return 0
    ok, msg = send(dec["text"])
    print(f"  telegram: {'OK' if ok else 'FALLO — ' + msg}")
    if ok:
        st = load_state()
        sym = (r["atas"].get("verdict") or {}).get("symbol") or "?"
        st[sym] = {"bias": r["bias"], "score": r["score"],
                   "sent_at": time.time(),
                   "sent_iso": datetime.now().isoformat(),
                   "prev": dec["prev"]}
        save_state(st)
    return 0 if ok else 1


def do_status() -> int:
    st = load_state()
    print(f"  state file: {STATE}  ({'existe' if STATE.exists() else 'aun no'})")
    if not st:
        print("  ningun veredicto enviado todavia")
        return 0
    for sym, d in st.items():
        age = time.time() - d.get("sent_at", 0)
        print(f"  {sym:10s} ultimo enviado: {d.get('bias')} "
              f"score {d.get('score')}  hace {age/60:.0f} min  "
              f"(previo: {d.get('prev')})")
    return 0


def do_test() -> int:
    ok, msg = send("prueba del lector de veredictos — canal configurado OK")
    print(f"  {'OK' if ok else 'FALLO — ' + msg}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--interval", type=float, default=60.0)
    a = ap.parse_args()
    if a.status:
        return do_status()
    if a.test:
        return do_test()
    if a.loop:
        print(f"  loop cada {a.interval:.0f}s (Ctrl-C para parar)")
        while True:
            try:
                do_once(dry=a.dry_run)
            except KeyboardInterrupt:
                print("\n  parado")
                return 0
            except Exception as e:
                print(f"  error: {type(e).__name__}: {str(e)[:90]}")
            time.sleep(a.interval)
    return do_once(dry=a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())