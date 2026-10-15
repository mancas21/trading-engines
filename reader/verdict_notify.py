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
    # 2026-10-13: el scorecard se RETIRO (commit a93147f, medido sin poder
    # predictivo: pearson +0.0175, y el tercil alto perdia MAS que el bajo).
    # El grabador dejo de guardar 'score', pero este modulo seguia leyendolo
    # con r["score"] -- KeyError en cuanto el sesgo fuera direccional, que es
    # justo cuando el mensaje importa. Se lee con .get: la ausencia de score
    # ya no es un dato que este modulo necesite.
    score = r.get("score")
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
    # El scorecard se retiro (a93147f): no se publica score. Publicarlo seria
    # dar precision aparente sobre un numero que se demostro no predecir.
    L.append(f"{sym}  px {v.get('price_last')}")
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
        st[sym] = {"bias": r["bias"],
                   "sent_at": time.time(),
                   "sent_iso": datetime.now().isoformat(),
                   "prev": dec["prev"]}
        save_state(st)
    return 0 if ok else 1



# ══════════════════════════════════════════════════════════════════════════════
# DIGEST PERIODICO — formato EJECUTIVO (2026-10-15)
# ══════════════════════════════════════════════════════════════════════════════
#
# El modelo anterior notificaba SOLO al cambiar el sesgo. Ese sigue vivo y es
# el correcto para no spamear: un veredicto se puede leer 50 veces al dia y
# cambiar dos.
#
# Pero el usuario pidio explicitamente recibir "este tipo de analisis cada 15
# minutos": el formato ejecutivo (decision, niveles, invalidacion), no el aviso
# de cambio. Son dos productos distintos y los dos se envian:
#
#   cambio de sesgo  -> mensaje corto, inmediato, cuando el motor gira
#   digest           -> lectura ejecutiva periodica, aunque el sesgo no cambie
#
# El digest NO depende de que el chat este abierto: lo dispara el supervisor del
# reader (ya un servicio), que llama a `--once --digest` en cada ciclo y este
# modulo decide si toca por tiempo.
DIGEST_EVERY_S = 900.0          # 15 min
_DIGEST_KEY = "_digest"


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def compose_digest(r: dict) -> str:
    """Lectura EJECUTIVA para decision. No un informe de lo que se leyo.

    Estructura fija (acordada con el usuario): DECISION / POR QUE / NIVELES /
    INVALIDACION / CONTEXTO. Si una fuente falta, se dice en una linea; no se
    rellena con lo que haya.
    """
    v = (r["atas"].get("verdict") or {})
    tv = r.get("tradingview") or {}
    bias = r.get("bias")
    sym = v.get("symbol") or "?"
    px = _num(v.get("price_last"))
    now = datetime.now()

    e9, e21 = _num(tv.get("ema9")), _num(tv.get("ema21"))
    e50, e200 = _num(tv.get("ema50")), _num(tv.get("ema200"))
    rsi6, rsi14 = _num(tv.get("rsi6")), _num(tv.get("rsi14"))
    atr = _num(tv.get("atr14"))
    c = v.get("cvd") or {}
    fp = v.get("footprint") or {}

    # ── Sensores del gate normalizado (la condicion (c) que se arreglo) ──
    sensores = []
    try:
        import verdict as V
        g = V.dom_vac_eval(sym, observe=False)
        cal = V._load_vac_calibration()
        raw = g.get("raw") or {}
        zs = {}
        for k, val in raw.items():
            st = (cal.get("venues") or {}).get(k) or {}
            m, s = st.get("mean"), st.get("std")
            if m is not None and s:
                zs[k] = (val - m) / s
        nb = sum(1 for z in zs.values() if z >= V.RULE7["vac_z"])
        ns = sum(1 for z in zs.values() if z <= -V.RULE6["vac_z"])
        top = max(zs.items(), key=lambda kv: kv[1]) if zs else None
        if g["buy"]["state"] == V._SPLIT or g["sell"]["state"] == V._SPLIT:
            libro = f"PARTIDO {nb}C/{ns}V (piden 3)"
        elif nb >= 3 or ns >= 3:
            libro = f"CONSENSO {nb}C/{ns}V"
        else:
            libro = f"{nb}C/{ns}V, sin consenso"
        if top and top[1] >= 1:
            libro += f" — max {top[0]} z={top[1]:+.1f}"
        sensores.append(libro)
    except Exception as e:                                  # noqa: BLE001
        sensores.append(f"libro multi-venue no evaluable ({type(e).__name__})")

    sell_h = fp.get("sell_heavy")
    buy_h = fp.get("buy_heavy")
    sensores.append(f"footprint {buy_h} compra / {sell_h} venta >65%")

    if px and e9 and e21:
        stack = "alcista" if px > e9 > e21 else ("bajista" if px < e9 < e21 else "mixto")
        sensores.append(f"estructura {stack}: px vs EMA9/21")
    if rsi6 is not None and rsi14 is not None:
        etq = "SOBRECOMPRA" if rsi6 >= 70 else ("SOBREVENTA" if rsi6 <= 30 else "neutral")
        sensores.append(f"RSI6 {rsi6:.0f} ({etq}) / RSI14 {rsi14:.0f}")
    up, dn = c.get("streak_up") or 0, c.get("streak_down") or 0
    if up or dn:
        sensores.append(f"CVD racha +{up}/-{dn}")

    # ── Direccion por mayoria de sensores (declarada como tal, no como sesgo) ──
    alc = baj = 0
    if buy_h and (not sell_h or buy_h > sell_h): alc += 1
    elif sell_h and (not buy_h or sell_h > buy_h): baj += 1
    if px and e9 and e21:
        if px > e9 > e21: alc += 1
        elif px < e9 < e21: baj += 1
    if up and up > dn: alc += 1
    elif dn and dn > up: baj += 1
    if rsi6 is not None and rsi6 >= 70 and rsi14 and rsi14 >= 60: baj += 1
    if bias in ("BULLISH", "BEARISH"):
        (alc if bias == "BULLISH" else baj).__class__  # no-op, claridad
    if bias == "BULLISH": alc += 1
    elif bias == "BEARISH": baj += 1
    lean = "alcista" if alc > baj else ("bajista" if baj > alc else "sin direccion")

    # ── La DECISION la da el gate, no la mayoria de sensores ──
    # La primera version ponia la mayoria de sensores en la cabecera, asi que un
    # mercado con 4 sensores alcistas pero libro PARTIDO salia titulado
    # "ALCISTA" y tres lineas mas abajo decia "sin setup". El lector se queda
    # con el titular. La cabecera es lo que el motor puede sostener; el sesgo de
    # los sensores es contexto y va en el cuerpo.
    if bias in ("BULLISH", "BEARISH"):
        head = f"{bias} — MOTOR CONFIRMA"
    elif bias == "UNAVAILABLE":
        head = "SIN LECTURA"
    else:
        head = "SIN POSICION — motor NEUTRAL"

    # ── Niveles ──
    if bias == "BULLISH":
        entra, para, obj = e21, (e50 or e9), e200
    elif bias == "BEARISH":
        entra, para, obj = e21, (e9 or e50), e200
    else:
        # Sin sesgo, los niveles son de referencia: el rango del dia.
        entra = para = obj = None

    L = [f"{sym} — {head}   ({now:%H:%M})", ""]
    L.append(f"Motor: {bias}   sensores inclinados al {lean}"
             f" ({alc} alc / {baj} baj)   probability: unvalidated")
    if r.get("scope_ok") is False:
        L.append(f"ALCANCE: {r['atas'].get('warning')}")
    L.append("")
    L.append("POR QUE")
    for s in sensores:
        L.append(f"  {s}")
    if entra is not None:
        L.append("")
        L.append("NIVELES (motor direccional)")
        L.append(f"  entrar sobre/ bajo {entra:.6g}   parar {para:.6g}   objetivo {obj:.6g}")
    elif px and e9 and e21:
        L.append("")
        L.append("NIVELES DE REFERENCIA (sin sesgo: solo rango)")
        L.append(f"  EMA9 {e9:.6g}  EMA21 {e21:.6g}  EMA50 {e50:.6g}  EMA200 {e200:.6g}")
        L.append(f"  px {px:.6g}  ATR {atr:.6g}" if atr else f"  px {px:.6g}")
    L.append("")
    L.append("INVALIDACION")
    if bias == "BULLISH":
        L.append(f"  perder {para:.6g} (EMA50/9) rompe el stack alcista")
        L.append(f"  CVD girando a negativo tras racha de {up} barras")
    elif bias == "BEARISH":
        L.append(f"  recuperar {para:.6g} invalida la distribucion")
        L.append(f"  CVD girando al alza tras racha de {dn} barras")
    else:
        L.append("  sin setup: cualquier direccion exige consenso del libro (3 venues)")
        L.append("  el mercado partido NO es senal, es falta de acuerdo")
    # contexto 24 h
    px24 = (r.get("prices_24h") or {}).get("prices") or {}
    cg = (r.get("prices_24h") or {}).get("cg_id")
    if px24 and cg and px24.get(cg, {}).get("usd_24h_change") is not None:
        ch = px24[cg]["usd_24h_change"]
        btc = (px24.get("bitcoin") or {}).get("usd_24h_change")
        base = f"  24h {ch:+.2f}%"
        if btc is not None:
            base += f" vs BTC {btc:+.2f}%"
        base += (" — caida propia del activo" if btc is not None and ch < btc - 1
                 else " — moviendo con el mercado" if btc is not None and abs(ch - btc) <= 1
                 else " — superando al mercado")
        L.append("")
        L.append("CONTEXTO")
        L.append(base)
    faltan = r.get("sources_missing") or []
    if faltan:
        L.append("")
        L.append(f"fuentes ausentes: {', '.join(faltan)}")
    L.append("")
    L.append(f"digest automatico cada {DIGEST_EVERY_S/60:.0f} min — "
             f"cambio de sesgo se avisa aparte")
    return "\n".join(L)


def do_digest(dry: bool = False, every: float = DIGEST_EVERY_S,
              force: bool = False) -> int:
    """Envia la lectura ejecutiva si paso `every` desde el ultimo digest."""
    import live_reader as L
    st = load_state()
    sym_hint = None
    last = (st.get(_DIGEST_KEY) or {})
    last_ts = float(last.get("sent_at") or 0.0)
    if not force and last_ts and (time.time() - last_ts) < every:
        print(f"  digest: todavia no toca ({((time.time()-last_ts)/60):.0f} de "
              f"{every/60:.0f} min)")
        return 0
    r = L.read_all()
    sym = (r["atas"].get("verdict") or {}).get("symbol") or "?"
    sym_hint = sym
    if r["bias"] == "UNAVAILABLE":
        # Sin veredicto NO se inventa uno. Se envia un aviso corto y honesto:
        # distinguir "no mire" de "mire y no hay nada" era justo el defecto.
        text = (f"{sym} — SIN LECTURA\n\n"
                f"El motor no pudo emitir veredicto: "
                f"{r['atas'].get('error') or 'fuentes insuficientes'}.\n"
                f"No hay analisis que entregar. No es NEUTRAL: es ausencia de dato.\n"
                f"{datetime.now():%H:%M}")
    else:
        text = compose_digest(r)
    print("  DIGEST — " + "-" * 50)
    print("  " + text.replace("\n", "\n  "))
    print("  " + "-" * 50)
    if dry:
        print("  (dry-run: no enviado)")
        return 0
    ok, msg = send(text)
    print(f"  telegram: {'OK' if ok else 'FALLO — ' + msg}")
    if ok:
        st[_DIGEST_KEY] = {"sent_at": time.time(),
                           "sent_iso": datetime.now().isoformat(),
                           "symbol": sym_hint}
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
    ap.add_argument("--digest", action="store_true",
                    help="lectura ejecutiva periodica, no solo avisos de cambio")
    ap.add_argument("--every", type=float, default=DIGEST_EVERY_S,
                    help="minutos entre digests (por defecto 900s = 15 min)")
    ap.add_argument("--force", action="store_true",
                    help="enviar el digest aunque no haya pasado el intervalo")
    a = ap.parse_args()
    if a.digest:
        return do_digest(dry=a.dry_run, every=a.every, force=a.force)
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