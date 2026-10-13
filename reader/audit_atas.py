"""Auditor de expectativa para el ledger ATAS (reader.db) — el mismo estándar
que scanner-engine/expectancy.py, aplicado a las reglas 6/7 del L2 real.

POR QUÉ EXISTE
==============
El L2 de ATAS es el único dato del sistema que ningún otro motor puede ver, y
es el MENOS medido: las reglas 6/7 llevan meses disparando sin que nadie haya
computado su expectativa neta de costo con corrección de solape. La auditoría
del 2026-10-11 encontró que el scanner medía DROP en sus tres reglas; este
módulo pone la pata ATAS bajo la misma vara ANTES de que alguien proponga
operar con ella.

LA POBLACIÓN
============
Una fila de reader.db entra al cálculo cuando:
  * resolved = 1                  -> el retorno a horizonte ya ocurrió
  * forward_return IS NOT NULL    -> hay medida (no fue retirada)
  * bias IN ('BULLISH','BEARISH') -> se afirmó una dirección
El retorno viene RAW del resolver (fill en el OPEN de la barra siguiente, exit
al close de la barra horizonte); el signo lo aplica aquí el sesgo del
veredicto, nunca se muta en la base. Es la misma disciplina que
DIRECTED_RETURN_SQL del scanner: la dirección se deriva en el auditor para que
siga re-derivable.

LAS CELDAS
==========
  * regla 6  -> filas con r6_met = 1 ( distribución confirmada )
  * regla 7  -> filas con r7_met = 1 ( acumulación confirmada )
Dos celdas -> Bonferroni sobre 2. La regla 6 se mide aunque su enforcement esté
"disabled" en el bridge: la cuarentena prohíbe ACTUAR con ella, no medirla —
y desactivar la medición de la regla en cuarentena sería borrar la evidencia
que decide si algún día vuelve a "hard".

LOS COSTOS
==========
El mismo modelo del scanner: 5bps fee + 0.5bps slippage por lado, x2, + 1bps de
spread por defecto (reader.db no graba el spread del fill; es una aproximación
declarada, no una medición). Un spread real cambiaría el número en ~1bps.

Run:
    python audit_atas.py            # informe completo
    python audit_atas.py tick       # emite solo si cambió (para el cron)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scanner-engine"))

import expectancy as E                      # noqa: E402  (estadística compartida)

DB = HERE / "reader.db"
STATE = HERE / "audit_atas_state.json"

# La única versión de medición de este ledger. Bump cuando cambie la
# población o la estadística: el tick retrajo lo anterior en lugar de
# compararlo contra números que ya no significan lo mismo.
# v2 (2026-10-11) - Fix 2-A (docs/FIXES-gate-DOM.md): la condicion (c) de las
# reglas AT-6/AT-7 cambio de fuente (EMA del libro Binance WS -> consenso
# multi-venue del scanner con persistencia). Las reglas que el ledger v1 midio
# NO son las reglas que corre el codigo nuevo; mezclar sus filas en una celda
# es medir dos reglas distintas con un mismo nombre.
MEASUREMENT_VERSION = 2

MIN_N = E.DEFAULT_MIN_N
ALPHA = E.ALPHA

_ROWS_SQL = """
    SELECT id, ts, symbol, bias, r6_met, r7_met, forward_return, score
      FROM verdicts
     WHERE resolved = 1
       AND forward_return IS NOT NULL
       AND bias IN ('BULLISH', 'BEARISH')
     ORDER BY ts
"""


def directed_rows(db: sqlite3.Connection) -> list[dict]:
    """Filas dirigidas con costo, en orden temporal (el lag-1 lo exige)."""
    cur = db.cursor()
    cur.row_factory = sqlite3.Row
    cur.execute(_ROWS_SQL)
    out = []
    for r in cur.fetchall():
        d = dict(r)
        sign = 1.0 if d["bias"] == "BULLISH" else -1.0
        d["dr"] = sign * float(d["forward_return"])
        d["cost"] = E.round_trip_bps(None)          # sin spread grabado: 1bps default
        d["net"] = d["dr"] - d["cost"] / 10_000.0
        out.append(d)
    return out


def _cell(rows: list[dict]) -> dict:
    """Clasificación de una celda — mismos buckets que el auditor del scanner."""
    net = [r["net"] for r in rows]
    gross = [r["dr"] for r in rows]
    mean, sd, n = E._stats(net)
    mean_raw, _, _ = E._stats(gross)
    n_eff, r1 = E.effective_n(net)
    t_eff = E._tstat(mean, sd, n_eff)
    threshold = E._bonferroni_t(2)                  # regla 6 + regla 7
    rec = {"n": n, "n_eff": round(n_eff, 1), "autocorr": round(r1, 3),
           "mean": mean, "mean_gross": mean_raw, "stdev": sd,
           "t": t_eff, "t_naive": E._tstat(mean, sd, n),
           "hit_net": (sum(1 for v in net if v > 0) / n) if n else None,
           "hit_gross": (sum(1 for v in gross if v > 0) / len(gross)) if gross else None,
           "threshold": threshold}
    if n_eff < MIN_N:
        rec["bucket"] = "insufficient"
        rec["why"] = f"n_eff={n_eff:.1f} < min_n={MIN_N} (n={n}, autocorr={r1:+.2f})"
    elif t_eff is not None and mean < 0 and t_eff <= -threshold:
        rec["bucket"] = "drop"
        rec["why"] = f"net {mean:+.6f} < 0 y t={t_eff:.2f} <= -{threshold:.2f}"
    elif t_eff is not None and mean > 0 and t_eff >= threshold:
        rec["bucket"] = "keep"
        rec["why"] = f"net {mean:+.6f} > 0 y t={t_eff:.2f} >= {threshold:.2f}"
    else:
        rec["bucket"] = "inconclusive"
        rec["why"] = (f"net {mean:+.6f}, t="
                      f"{t_eff if t_eff is None else round(t_eff, 2)} "
                      f"vs {threshold:.2f}")
    return rec


def audit(db: sqlite3.Connection) -> dict:
    rows = directed_rows(db)
    counts = {
        "rows_total": db.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0],
        "rows_resolved": db.execute(
            "SELECT COUNT(*) FROM verdicts WHERE resolved=1").fetchone()[0],
        "rows_directed_resolved": len(rows),
        "rule6_fired_resolved": sum(1 for r in rows if r["r6_met"]),
        "rule7_fired_resolved": sum(1 for r in rows if r["r7_met"]),
    }
    if not rows:
        return {"verdict": "INSUFFICIENT DATA",
                "reason": "no hay filas dirigidas resueltas en reader.db",
                "counts": counts, "rules": {}}
    r6 = _cell([r for r in rows if r["r6_met"]])
    r7 = _cell([r for r in rows if r["r7_met"]])
    return {"verdict": "MEASURED", "counts": counts,
            "cost_bps_rt": E.round_trip_bps(None),
            "n_tests": 2, "alpha": ALPHA, "min_n": MIN_N,
            "rules": {"6": r6, "7": r7}}


def render(a: dict) -> str:
    L = ["=" * 74, "AUDIT ATAS — REGLAS 6/7 sobre reader.db (NET de costo, "
         "overlap-corrected)", "=" * 74,
         "  reglas de atas-bridge/rules.json (namespace: atas) — NO confundir "
         "con scanner-engine/rules.json"]
    c = a.get("counts", {})
    L.append(f"  verdicts        {c.get('rows_total', 0)}")
    L.append(f"  resueltos       {c.get('rows_resolved', 0)}")
    L.append(f"  dirigidos       {c.get('rows_directed_resolved', 0)}   "
             f"<- BULLISH/BEARISH con retorno")
    L.append(f"  regla 6 disparó {c.get('rule6_fired_resolved', 0)}   "
             f"regla 7 disparó {c.get('rule7_fired_resolved', 0)}")
    if a["verdict"] == "INSUFFICIENT DATA":
        L.append("  reglas de atas-bridge/rules.json (namespace: atas)")
        L.append("")
        L.append(f"  {a['reason']} — la respuesta día-uno correcta. Seguir grabando.")
        return "\n".join(L)
    L.append(f"  costo ida-vuelta {a['cost_bps_rt']:.2f} bps  |  2 celdas -> "
             f"Bonferroni t={a['rules']['6']['threshold']:.2f}")
    for rid in ("6", "7"):
        r = a["rules"][rid]
        t = f"{r['t']:.2f}" if r["t"] is not None else "n/a"
        hn = f"{r['hit_net']:.1%}" if r["hit_net"] is not None else "n/a"
        hg = f"{r['hit_gross']:.1%}" if r["hit_gross"] is not None else "n/a"
        L.append(f"  regla {rid}: {r['bucket']:13} n={r['n']:>4} n_eff={r['n_eff']:>6} "
                 f"NET {r['mean']:+.6f} GROSS {r['mean_gross']:+.6f} t={t:>7} "
                 f"hit_N {hn} hit_G {hg}  [{r['why']}]")
    return "\n".join(L)


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:                               # noqa: BLE001
        return {}


def _save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def tick() -> list[str]:
    """Modo cron: devuelve la lista de cambios (vacía = nada nuevo).

    Devuelve las líneas en vez de imprimirlas para que el tick del scanner —
    que corre en la MISMA tarea programada — pueda entregarlas por Telegram
    junto con las suyas. El __main__ imprime para la corrida manual.
    """
    if not DB.exists():
        return []                                   # el grabador nunca corrió
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    a = audit(db)
    db.close()
    prev = _load_state()
    fp = {rid: {"bucket": r["bucket"], "n_eff": r["n_eff"], "t": r["t"],
                "mean": r["mean"]}
          for rid, r in a.get("rules", {}).items()}
    changes = []
    if prev.get("measurement_version") != MEASUREMENT_VERSION:
        if prev:
            changes.append(f"MEASUREMENT BASIS CHANGED v{prev.get('measurement_version')}"
                           f" -> v{MEASUREMENT_VERSION}: veredictos previos retractados.")
        prev_fp = {}
    else:
        prev_fp = prev.get("fingerprint", {})
    for rid, cur in fp.items():
        old = prev_fp.get(rid)
        if old is None:
            changes.append(f"regla {rid}: primera medición — {cur['bucket']} "
                           f"n_eff={cur['n_eff']} t={cur['t']}")
        elif cur["bucket"] != old["bucket"]:
            changes.append(f"regla {rid}: {old['bucket']} -> {cur['bucket']} "
                           f"(n_eff={cur['n_eff']}, t={cur['t']})")
    if a.get("counts", {}).get("rows_directed_resolved", 0) == 0 and \
            a.get("counts", {}).get("rows_total", 0) > 100:
        changes.append("veredictos grabados pero ninguno dirigido resuelto — "
                       "la puerta no dispara o el resolver no corre")
    _save_state({"fingerprint": fp, "measurement_version": MEASUREMENT_VERSION,
                 "ts": time.time()})
    return changes


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Auditor ATAS (reader.db)")
    ap.add_argument("cmd", nargs="?", default="audit", choices=["audit", "tick"])
    args = ap.parse_args()
    if args.cmd == "tick":
        _changes = tick()
        if _changes:
            print("reader/atas: audit update")
            print("\n".join(f"  * {c}" for c in _changes))
        raise SystemExit(0)
    if not DB.exists():
        print(f"no hay base en {DB} — el verdict_recorder nunca corrió.")
        raise SystemExit(0)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    print(render(audit(conn)))
