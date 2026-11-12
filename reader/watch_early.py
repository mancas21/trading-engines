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

CRITERIOS PRE-REGISTRADOS v3 (2026-11-12 — retune por backtest, PLAN §10.2)
============================================================================
BACKTEST (event study, 11 pares, 1h+15m+funding históricos, coste 11bps RT,
n=1,842 banderas v2): la capa COMPLETA no tiene edge medio (hit neto 47%,
mediana r24 negativa) y las banderas SHORT no lo tienen en ninguna celda.
El edge vive SOLO en LONG con funding profundo — gradiente monótono:
  LONG funding 1.5-3x  (n=418): hit 45.5%, med r24 -0.41%  -> ruido
  LONG funding 3-10x   (n=123): hit 48.0%, med r24 -0.15%  -> ruido
  LONG funding >10x    (n=32):  hit 56.2%, med r24 +1.0%, MFE p75 +11.2%
v1 vs v2 (8h vs 2h de estabilización): idénticos (46.8 vs 47.0) — el debate
de la ventana B fue inmaterial para el resultado; se conserva v2 (menor
latencia, misma calidad).

  A. funding_extreme : LONG-only, lastFundingRate <= -0.001 (10x baseline
                       0.01%). Las banderas SHORT quedan DESACTIVADAS
                       (sin edge medido en ninguna profundidad).
  B. stabilized      : 15m — 2h sin nuevo extremo de 24h (igual v2)
  C. first_higher_low: swing fractal 1h 3+3, último low > anterior (igual)
Flag = A y B y C. Con el umbral a 10x, la bandera será RARA (~5-6/año en el
universo de 12 pares) — que es exactamente el punto: menos ruido, más
significado. PEPE (funding -0.017% = 1.7x) NO habría disparado bajo v3: su
evento vive en la celda sin edge medido — la disciplina manda sobre la
anécdota.

ESTADO: HIPOTESIS, NO EDGE (T7, auditoría 2026-11-12)
=====================================================
El 56.2% (n=32) del backtest NO es edge validado: depende de 2 regimenes
(manía dic-20/ene-21 y FTX 2022-11-10), 11 episodios, p=0.21 por permutación,
SIN meseta en el umbral (54.5/57.5/56.2/68.0/52.9 para 6x/8x/10x/12x/15x) y
CERO banderas en 2024 y 2026. validated: false. La potencia para distinguir
56% de 50% exige ~544 eventos independientes. Las banderas v3 se siguen
grabando en vivo con su resultado — la medición en vivo es el árbitro.

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
FUNDING_EXTREME = 0.001        # v3: 10x baseline 0.01% — celda con edge medido
ENABLE_SHORT_FLAGS = False     # v3: sin edge medido en ninguna profundidad
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
    # v3: solo LONG con funding profundo (<= -10x baseline); SHORT sin edge
    # medido -> desactivados.
    if fr > -FUNDING_EXTREME:
        return None
    side = "LONG"
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
