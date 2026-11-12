"""Capa de VIGILANCIA TEMPRANA — banderas de pre-posicionamiento, no señales.

POR QUÉ EXISTE (petición del usuario 2026-11-12)
================================================
El gate formal (Reglas 6/7) es un motor de CONFIRMACIÓN: por diseño llega
tarde. El usuario lo demostró con PEPE: el 0.00428 era entrada viable y el
engine siguió en NEUTRAL hasta que el movimiento se agotaba. El marcador que
sevió temprano no era el precio — era el FUNDING en extremo con el precio
estabilizando (cortos apiñados pagando −0.017% mientras el tape dejaba de
caer). Esta capa vigila exactamente ese patrón.

QUE ES Y QUE NO ES
==================
Una bandera AMARILLA de "PREPARARSE": nunca es sesgo, nunca es señal, nunca
entra al gate. El sesgo sigue saliendo exclusivamente de verdict.py (Reglas
6/7). Máximo UNA bandera por par y lado cada 24h.

CRITERIOS PRE-REGISTRADOS v2 (2026-11-12, revisión de la misma noche)
=======================================================================
  A. funding_extreme : |lastFundingRate| >= 0.00015 (1.5x el baseline 0.01%)
                       el SIGNO define el lado (negativo -> LONG, positivo -> SHORT)
  B. stabilized      : sin nuevo extremo de 24h en las últimas 2h, medido en
                       barras de 15m (LONG: min(low15[-8:]) > min(low15[-104:-8]))
                       v1 medía esto en 1h (8h de latencia mínima): simulado
                       sobre el propio PEPE, disparaba a +5.4% del low — tarde
                       estructural. 15m corta la latencia 4x (2.2h, +0.9%).
                       El riesgo cuchillo que esto añade lo cuantifica MAE.
  C. first_higher_low: swing fractal en 1h (3 barras por lado) — el último
                       swing low es mayor que el anterior (LONG; SHORT espejo).
Flag = A y B y C, en el mismo lado.

DISCIPLINA DE MEDICIÓN (lo que separa esto del motor de ruido viejo)
====================================================================
Cada bandera se graba en watch_flags.jsonl con precio y funding al momento.
`--measure` rellena r24h/r72h/mfe24h/mae24h cuando cumplen plazo. `--report`
imprime hit-rate y medianas. Nada de esto promueve la bandera a señal: la
medición existe para decidir, con datos, si la capa vive o muere como las
reglas 1/2/4 del scanner.

Uso:
  python watch_early.py --once --dry-run   # ver qué dispararía, sin enviar
  python watch_early.py --once             # chequear (auto-throttle 5 min) y avisar
  python watch_early.py --once --force     # ignorar el throttle
  python watch_early.py --measure          # rellenar resultados de banderas viejas
  python watch_early.py --report           # estadísticas de la capa
  python watch_early.py --status           # estado actual
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# veredicto_notify vive en este dir; con python -I el script dir no entra al
# path, así que lo garantizamos aquí (inocuo en ejecución normal).
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
STATE = HERE / "watch_state.json"
FLAGS = HERE / "watch_flags.jsonl"

CHECK_EVERY_S = 300.0          # el supervisor llama cada 30s; el script se
                               # auto-limita a un chequeo real cada 5 min
FLAG_COOLDOWN_S = 86400.0      # una bandera por par y lado cada 24h
FUNDING_EXTREME = 0.00015      # 1.5x baseline de 0.01%/intervalo
SCAN = "http://127.0.0.1:8090"
FAPI = "https://fapi.binance.com"

DEFAULT_PAIRS = ["BTC", "ETH", "HYPE", "SOL", "XAU", "XRP", "JUP", "NVDA",
                 "SPCX", "SUI", "1000PEPE", "ARB"]


def _get(url: str, timeout: float = 8.0, retries: int = 1):
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            if i < retries:
                time.sleep(1.2)
    return None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def live_pairs() -> list[str]:
    """Pares desde el scanner (fuente de verdad); fallback a la lista fija."""
    r = _get(f"{SCAN}/rank")
    if r and r.get("rank"):
        return [row["pair"].split("/")[0] for row in r["rank"]]
    return DEFAULT_PAIRS


def swing_extremes(h: list[float], l: list[float]) -> tuple[list[float], list[float]]:
    """Swing highs/lows fractales (3 barras por lado) sobre la serie dada."""
    ph, pl = [], []
    n = len(h)
    for i in range(3, n - 3):
        if h[i] == max(h[i - 3:i + 4]):
            ph.append(h[i])
        if l[i] == min(l[i - 3:i + 4]):
            pl.append(l[i])
    # dedupe consecutivos iguales (mesetas)
    ph = [v for i, v in enumerate(ph) if i == 0 or v != ph[i - 1]]
    pl = [v for i, v in enumerate(pl) if i == 0 or v != pl[i - 1]]
    return ph, pl


def evaluate_pair(name: str) -> dict | None:
    """Devuelve el dict de bandera si A y B y C se cumplen; None si no."""
    sym = f"{name}USDT"
    p = _get(f"{FAPI}/fapi/v1/premiumIndex?symbol={sym}")
    time.sleep(0.2)
    if not p:
        return None
    fr = float(p.get("lastFundingRate") or 0.0)
    if abs(fr) < FUNDING_EXTREME:
        return None
    side = "LONG" if fr < 0 else "SHORT"
    # B en 15m (latencia 4x menor que la v1 en 1h): 2h sin nuevo extremo
    # contra un telón de 24h.
    k15 = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=15m&limit=120")
    time.sleep(0.2)
    if not k15 or len(k15) < 104:
        return None
    l15 = [float(b[3]) for b in k15]
    h15 = [float(b[2]) for b in k15]
    # C en 1h: la estructura (swings) necesita el marco grande.
    k = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=1h&limit=64")
    time.sleep(0.2)
    if not k or len(k) < 64:
        return None
    h = [float(b[2]) for b in k]
    l = [float(b[3]) for b in k]
    c = [float(b[4]) for b in k]
    price = c[-1]
    if side == "LONG":
        stabilized = min(l15[-8:]) > min(l15[-104:-8])
        ph, pl = swing_extremes(h, l)
        higher = len(pl) >= 2 and pl[-1] > pl[-2]
        ref = (pl[-1], pl[-2]) if len(pl) >= 2 else (None, None)
    else:
        stabilized = max(h15[-8:]) < max(h15[-104:-8])
        ph, pl = swing_extremes(h, l)
        higher = len(ph) >= 2 and ph[-1] < ph[-2]
        ref = (ph[-1], ph[-2]) if len(ph) >= 2 else (None, None)
    if not (stabilized and higher):
        return None
    return {"pair": name, "side": side, "price": price, "funding": fr,
            "crit": {"funding_extreme": True, "stabilized": True,
                     "swing": higher},
            "ref_prev": ref[1], "ref_now": ref[0]}


def sweep() -> list[dict]:
    out = []
    for name in live_pairs():
        try:
            f = evaluate_pair(name)
        except Exception:
            f = None
        if f:
            out.append(f)
    return out


def compose(f: dict) -> str:
    sgn = f"{f['funding']*100:.3f}%"
    prev, now = f["ref_prev"], f["ref_now"]
    rl = ("mínimos crecientes" if f["side"] == "LONG" else "máximos decrecientes")
    return (f"🟡 VIGILANCIA TEMPRANA — {f['side']} {f['pair']} @ {f['price']:g}\n\n"
            f"Los {'cortos' if f['side']=='LONG' else 'largos'} están apiñados "
            f"pagando {sgn} de funding, el precio lleva 2h sin hacer "
            f"{'nuevos mínimos' if f['side']=='LONG' else 'nuevos máximos'} y "
            f"acaba de marcar {rl} ({now:g} vs {prev:g} previo). "
            f"Es la receta de {'squeeze' if f['side']=='LONG' else 'distribución'} "
            f"que venimos viendo — bandera de PREPARACIÓN, no señal: el sesgo "
            f"formal sigue saliendo del gate (Reglas 6/7).\n"
            f"(una bandera por par y lado cada 24h; el resultado se mide a "
            f"24h/72h con --measure)")


def cooldown_ok(st: dict, f: dict) -> bool:
    key = f"{f['pair']}:{f['side']}"
    last = float((st.get("last_flag") or {}).get(key) or 0.0)
    return (time.time() - last) >= FLAG_COOLDOWN_S


def append_flag(f: dict) -> None:
    rec = {"ts": time.time(), "ts_iso": datetime.now(timezone.utc).isoformat(),
           **f, "r24h": None, "r72h": None, "mfe24h": None, "mae24h": None}
    with FLAGS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def do_once(dry: bool = False, force: bool = False) -> int:
    st = load_state()
    last_check = float(st.get("last_check") or 0.0)
    if not force and (time.time() - last_check) < CHECK_EVERY_S:
        return 0
    st["last_check"] = time.time()
    st["last_check_iso"] = datetime.now(timezone.utc).isoformat()
    fired = []
    for f in sweep():
        if not cooldown_ok(st, f):
            continue
        if not dry:
            # en dry-run nada se graba: los tests no contaminan la medición
            append_flag(f)
            key = f"{f['pair']}:{f['side']}"
            st.setdefault("last_flag", {})[key] = time.time()
            st.setdefault("active", []).append(
                {"ts": time.time(), "pair": f["pair"], "side": f["side"],
                 "price": f["price"], "funding": f["funding"]})
        fired.append(f)
    st["active"] = [a for a in st.get("active", [])
                    if time.time() - a["ts"] < 86400.0][-20:]
    save_state(st)
    for f in fired:
        text = compose(f)
        print("  WATCH — " + "-" * 46)
        print("  " + text.replace("\n", "\n  "))
        print("  " + "-" * 46)
        if dry:
            print("  (dry-run: no enviado)")
            continue
        import verdict_notify as N
        ok, msg = N.send(text)
        print(f"  telegram: {'OK' if ok else 'FALLO — ' + msg}")
    if not fired:
        print(f"  watch: sin banderas nuevas "
              f"({datetime.now(timezone.utc):%H:%M} UTC)")
    return 0


def do_measure() -> int:
    """Rellena r24h/r72h/mfe24h/mae24h de las banderas que ya cumplieron plazo."""
    if not FLAGS.exists():
        print("  measure: sin banderas grabadas")
        return 0
    lines = [json.loads(x) for x in FLAGS.read_text(encoding="utf-8").splitlines() if x.strip()]
    now = time.time()
    changed = 0
    for rec in lines:
        age = now - rec["ts"]
        if rec.get("r72h") is None and age >= 72 * 3600:
            sym = f"{rec['pair']}USDT"
            k = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=1h&limit=80")
            time.sleep(0.2)
            if not k:
                continue
            t0 = rec["ts"] * 1000
            fwd = [b for b in k if b[0] >= t0]
            if len(fwd) < 24:
                continue
            entry = rec["price"]
            m = 1 if rec["side"] == "LONG" else -1
            closes = [float(b[4]) for b in fwd[:24]]
            highs = [float(b[2]) for b in fwd[:24]]
            lows = [float(b[3]) for b in fwd[:24]]
            rec["r24h"] = round((closes[-1] / entry - 1) * 100 * m, 3)
            rec["mfe24h"] = round((max(highs) / entry - 1) * 100 * m, 3)
            rec["mae24h"] = round((min(lows) / entry - 1) * 100 * m, 3)
        if rec.get("r72h") is None and age >= 72 * 3600:
            sym = f"{rec['pair']}USDT"
            k = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=1h&limit=200")
            time.sleep(0.2)
            if not k:
                continue
            t0 = rec["ts"] * 1000
            fwd = [b for b in k if b[0] >= t0]
            if len(fwd) >= 72:
                rec["r72h"] = round((float(fwd[71][4]) / rec["price"] - 1) * 100
                                    * (1 if rec["side"] == "LONG" else -1), 3)
            changed += 1
    if changed:
        FLAGS.write_text("\n".join(json.dumps(r) for r in lines) + "\n",
                         encoding="utf-8")
        print(f"  measure: {changed} banderas actualizadas")
    else:
        print("  measure: nada que rellenar todavía")
    return 0


def do_report() -> int:
    if not FLAGS.exists():
        print("  report: sin banderas grabadas")
        return 0
    recs = [json.loads(x) for x in FLAGS.read_text(encoding="utf-8").splitlines() if x.strip()]
    done = [r for r in recs if r.get("r24h") is not None]
    print(f"banderas totales: {len(recs)} | con resultado 24h: {len(done)}")
    if not done:
        return 0
    wins = [r for r in done if r["r24h"] > 0]
    mfe = sorted(r["mfe24h"] for r in done if r["mfe24h"] is not None)
    mae = sorted(r["mae24h"] for r in done if r["mae24h"] is not None)
    med = lambda xs: xs[len(xs)//2] if xs else None
    print(f"hit-rate 24h (favorable): {len(wins)}/{len(done)} "
          f"= {len(wins)/len(done)*100:.0f}%")
    print(f"mediana r24h: {med([r['r24h'] for r in done])}% | "
          f"mediana MFE: {med(mfe)}% | mediana MAE: {med(mae)}%")
    for r in done[-5:]:
        print(f"  {r['ts_iso'][:16]} {r['side']} {r['pair']} @ {r['price']:g} "
              f"-> r24h {r['r24h']}%")
    return 0


def do_status() -> int:
    st = load_state()
    active = st.get("active", [])
    if not active:
        print("sin banderas activas (<24h)")
    for a in active:
        age = (time.time() - a["ts"]) / 3600
        print(f"activa: {a['side']} {a['pair']} @ {a['price']:g} "
              f"(funding {a['funding']*100:.3f}%) hace {age:.1f}h")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        return do_status()
    if a.measure:
        return do_measure()
    if a.report:
        return do_report()
    return do_once(dry=a.dry_run, force=a.force)


if __name__ == "__main__":
    raise SystemExit(main())
