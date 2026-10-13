"""
build_scorecard_calibration.py — genera scorecard_calibration.json

POR QUE
=======
El scorecard v3.4.1 es ANTI-PREDICTIVO, medido sobre sus propios 1.205
registros: score alto gana el 34,0%, score bajo gana el 38,0%. Spread -4,0.
Puntua al reves. Eso es daño medido, y quitar daño medido no requiere que
la alternativa tenga edge probado.

QUE HACE
========
1. Calcula la distribucion empirica de cada componente disponible en la
   ruta live (lo que /indicators devuelve de verdad).
2. Exporta percentiles a JSON para que verdict.py puntue por rank y no por
   linealidad.
3. Solo incluye componentes con spread positivo medido.

LIMITACION HONESTA (leer antes de confiar)
==========================================
- La muestra es 1 simbolo, 25 h, un unico regimen (sube 33,5%). Un
  split-half dentro de esa ventana da 2 dias vs 2 dias del MISMO dia: no es
  out-of-sample real. Los componentes por separado fallan out-of-sample
  (-8,0 y -13,3); solo la combinacion aguanta (+6,7). Eso es la firma clasica
  de overfitting.
- La validacion de verdad es hacia adelante: este scorecard se recalcula
  cada barra y verdict.py registra sus componentes. Hay que medir el spread
  sobre datos NUEVOS antes de llamarlo edge.
"""
import sqlite3, os, json, bisect, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "reader.db")
OUT = os.path.join(HERE, "scorecard_calibration.json")

sys.path.insert(0, HERE)
import verdict  # noqa: E402  (la UNICA implementacion del scorecard viejo)

# Componentes que verdict.py puede calcular en vivo desde /indicators.
COMPONENTS = {
    "dom_ema":            "imbalance_pct_ema",      # spread util +27.3
    "footprint_sell_heavy": "footprint_sell_heavy",  # spread util  +4.7
    "footprint_buy_heavy_inv": "footprint_buy_heavy",  # spread util -17.3 -> se invierte
}

WEIGHTS = {
    "dom_ema": 50.0,
    "footprint_sell_heavy": 25.0,
    "footprint_buy_heavy_inv": 25.0,
}

PCTS = [5, 10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 95]

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute(
    "SELECT imbalance_pct, imbalance_pct_ema, tape_buy_pct, volume_ratio,"
    " footprint_buy_heavy, footprint_sell_heavy, levels, score, bias,"
    " forward_return, resolved "
    "FROM verdicts WHERE resolved=1 AND forward_return IS NOT NULL").fetchall()
n = len(rows)
print(f"muestra: {n}")
if n < 300:
    raise SystemExit(f"muestra insuficiente ({n}); no se genera calibracion")

def col(name):
    return sorted(r[name] for r in rows if r[name] is not None)

# ── spread medido de CADA componente (evidencia para incluirlo) ──
print("\nspread medido (a favor):")
evidence = {}
for comp, src in COMPONENTS.items():
    v = [r for r in rows if r[src] is not None]
    v.sort(key=lambda r: r[src])
    k = max(15, len(v)//8)
    f = lambda g: sum(1 for r in g if r["forward_return"] > 0)/len(g)*100
    hi, lo = f(v[-k:]), f(v[:k])
    raw = hi - lo
    inv = comp.endswith("_inv")
    evidence[comp] = {
        "source_col": src,
        "raw_spread": round(raw, 1),
        "inverted": inv,
        "useful_spread": round(-raw if inv else raw, 1),
        "n_top_bottom": [k, k],
        "top_win_pct": round(hi, 1),
        "bottom_win_pct": round(lo, 1),
    }
    print(f"  {comp:<26} TOP {hi:5.1f}%  BOT {lo:5.1f}%  raw {raw:+6.1f}  "
          f"util {evidence[comp]['useful_spread']:+6.1f}  {'(invertido)' if inv else ''}")

# ── distribucion para puntuar por percentil ──
print("\ngenerando distribuciones...")
cal = {"version": "1.0",
       "generated_at": datetime.now(timezone.utc).isoformat(),
       "source": "reader.db verdicts (resueltos)",
       "sample_size": n,
       "sample_span_days": round((max(r["forward_return"] and 0 or 0 for r in rows), 2)[0] or 0, 2),
       "note": ("Percentiles empiricos. El score es un promedio de ranks, no de "
                "lineas: por construccion, si el mercado esta en la mediana el "
                "saliente es ~50. Componentes con spread negativo medido estan "
                "INVERTIDOS o excluidos. NO es un modelo validado: ver la "
                "limitacion en el docstring."),
       "evidence": evidence,
       "weights": WEIGHTS,
       "distributions": {},
       "bands": {"bullish_min": 60, "bearish_max": 40}}

for comp, src in COMPONENTS.items():
    v = col(src)
    if len(v) < 100:
        print(f"  {comp}: muestra baja ({len(v)}), se omite")
        continue
    cal["distributions"][comp] = [round(v[min(len(v)-1, int(p/100*(len(v)-1)))], 4) for p in PCTS]
    print(f"  {comp:<26} p10 {cal['distributions'][comp][1]:+8.3f}  "
          f"p50 {cal['distributions'][comp][6]:+8.3f}  p90 {cal['distributions'][comp][11]:+8.3f}")

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(cal, f, indent=2, ensure_ascii=False)
print(f"\nescrito: {OUT}")

# ── verificacion: el nuevo scorecard contra el actual, sobre los mismos datos ──
print("\n=== verificacion (in-sample, sobre los mismos n) ===")
def rank(comp, x):
    v = cal["distributions"].get(comp)
    if not v or x is None:
        return 50.0
    return bisect.bisect_left(v, x)/len(v)*100.0

def score_new(r):
    src_map = {c: s for c, s in COMPONENTS.items()}
    tot = wsum = 0.0
    for comp, w in WEIGHTS.items():
        x = r[src_map[comp]]
        pr = rank(comp, x)
        if comp.endswith("_inv"):
            pr = 100.0 - pr
        tot += pr*w; wsum += w
    return tot/wsum

def score_old(r):
    # v3.4.1 — UNA SOLA implementacion, la de verdict.py._scorecard_v341.
    #
    # Antes esto era una COPIA local de la formula. Sumada a la de verdict.py y
    # a la que tenia verdict_recorder.py, eran TRES, y una divergio: el
    # grabador guardaba el v341 mientras rules.json declaraba por escrito que no
    # se usa. Aqui solo se MIDE el viejo para compararlo; no se reimplementa, o
    # volvemos a tener dos verdades sobre la misma formula.
    s, _parts, _notes = verdict._scorecard_v341(
        [None] * int(r["levels"] or 0),        # _scorecard_v341 solo usa len()
        r["imbalance_pct"], None,
        r["tape_buy_pct"], r["volume_ratio"],
        r["footprint_sell_heavy"], r["footprint_buy_heavy"])
    return s

for label, fn in (("VIEJO v3.4.1 (retirado)", score_old), ("NUEVO percentile", score_new)):
    pairs = sorted((fn(r), r["forward_return"]) for r in rows)
    k = max(15, len(pairs)//8)
    f = lambda g: sum(1 for x in g if x[1]>0)/len(g)*100
    s = sorted(pairs)
    b40 = sum(1 for x in s if x[0] <= 40); b60 = sum(1 for x in s if x[0] >= 60)
    print(f"  {label:<20} mediana {s[len(s)//2][0]:5.1f}  <=40 {b40/len(s)*100:5.1f}%  "
          f">=60 {b60/len(s)*100:5.1f}%  |  alto {f(pairs[-k:]):5.1f}%  bajo {f(pairs[:k]):5.1f}%  "
          f"SPREAD {f(pairs[-k:])-f(pairs[:k]):+6.1f}")
    print(f"  {'':<20} nivel absoluto del quintil alto: {f(pairs[-k:]):.1f}% a favor "
          f"(necesita >52% para superar costes)")