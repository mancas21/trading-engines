"""Puente al backtest-system (`C:\\Users\\Lenovo\\Documents\\backtest-system`).

POR QUE EXISTE
=============
`trading-engines` es la capa LIVE (scanner/reader/gate/alertas). El
backtest-system es la capa de VALIDACION (harness vrun.py, artefactos con
run_id, kill list documentada). Este modulo es el UNICO punto donde las dos se
tocan.

REGLAS INHEREDADAS (no negociables, ver backtest-system/AGENTS.md)
====================================================================
REGLA 0 - ningun numero sin artefacto. Aqui eso significa:
  - cada metrica que sale de este modulo viene de un artefacto de
    `results/runs/*.json` y lleva su `run_id` y su ruta.
  - si no hay artefacto, no hay numero. Se declara la ausencia.
  - NO se recalcula ni se reinterpreta una metrica: se copia.

CRITERIOS DE ADMISION
=====================
Se aplican los del propio repo (AGENTS.md "Validation criteria"):
  - PF ex-cost > 1.2
  - n >= 30 trades cerrados
  - net > 0
  - costes del repo: direccional 5 bps fee + 0.5 bps slip por lado => 11 bps
    roundtrip (medido 2026-10-25 en libro vivo; 6+5 = 22 bps solo sensibilidad)
  - ademas se exige `excess_%` (neto frente a buy & hold) porque un PF > 1 que
    solo gana "perdiendo menos que un mercado que se hunde" no es alpha.
    Desde 2026-10-25 esto se APLICA (antes solo se reportaba: un candidato con
    excess -279pp pasaba el gate).

Los artefactos con `slice_from`/`slice_to` son cortes diagnosticos de 8-9 meses,
no resultados validados: se cuentan en `slice_artifacts` y NUNCA son candidatos
(podian ser mas recientes que el artefacto de ventana completa y ganar el dedupe,
sustituyendo un resultado real por el de un trozo).

Un artefacto con OTRA asuncion de costes (14 bps, zero-cost control, modelos
propios de cvrmd/sax) NO se usa para admitir un candidato: se contabiliza aparte
como "costes distintos" y nunca se mezcla con los admitted.

Solo LECTURA. Nunca se escribe en el backtest-system: `code/`, `data/`,
`results/` y `backups/` son registros append-only de ese repo.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BACKTEST_ROOT = Path(os.environ.get(
    "BACKTEST_SYSTEM_ROOT",
    r"C:\Users\Lenovo\Documents\backtest-system",
))

# costes del repo (backtest-system/AGENTS.md desde 2026-10-25): direccional
# 5 bps fee + 0.5 bps slip por lado => 11 bps roundtrip, MEDIDO en libro vivo
# (Binance/OKX/Bybit/Kraken: spread 0.77 bps, impacto $10k 0.4-2 bps, fee tier0
# 5 bps). El modelo viejo (6+5 => 22 bps) queda solo como sensibilidad.
# Cualquier otra asuncion no admite candidatos.
REPO_FEE_BPS = 5.0
REPO_SLIP_BPS = 0.5
REPO_COST_RT_BPS = 11.0

PF_MIN = 1.2
TRADES_MIN = 30

# pares del engine -> simbolos del backtest-system (los perps de Binance
# tienen el mismo ticker en los dos repos, pero el engine los llama X/USDT)
_PAIR_ALIAS = {
    "ETH/USD": "ETHUSDT",
    "SOL/USD": "SOLUSDT",
    "XRP/USD": "XRPUSDT",
    "HYPE/USD": "HYPEUSDT",
    "SUI/USD": "SUIUSDT",
    "XAU/USD": "XAUUSDT",
    "SPCX/USD": "SPCXUSDT",
    "NVDA/USD": "NVDAUSDT",
    "JUP/USD": "JUPUSDT",
    "ARB/USD": "ARBUSDT",
    "BTC/USD": "BTCUSDT",
}


def _artifacts() -> list[Path]:
    d = BACKTEST_ROOT / "results" / "runs"
    if not d.is_dir():
        return []
    return sorted(d.glob("*.json"))


def _load(p: Path) -> dict | None:
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return j if isinstance(j, dict) else None


def _repo_costs(j: dict) -> bool:
    m = j.get("metrics") or {}
    try:
        fee = float(m.get("fee_bps_per_side"))
        slip = float(m.get("slippage_bps_per_side"))
        rt = float(m.get("cost_bp_roundtrip"))
    except (TypeError, ValueError):
        return False
    return (abs(fee - REPO_FEE_BPS) < 1e-6
            and abs(slip - REPO_SLIP_BPS) < 1e-6
            and abs(rt - REPO_COST_RT_BPS) < 1e-6)


def _passes(row: dict) -> tuple[bool, str]:
    """Gates del repo sobre una fila de `tables`. Devuelve (pasa, motivo)."""
    if str(row.get("status")) != "ok":
        return False, f"status={row.get('status')}"
    try:
        pf = float(row.get("PF"))
        trades = float(row.get("trades"))
        net = float(row.get("net_%"))
        excess = float(row.get("excess_%"))
    except (TypeError, ValueError):
        return False, "metricas ausentes (PF/net/trades/excess)"
    if trades < TRADES_MIN:
        return False, f"n={trades:.0f} < {TRADES_MIN}"
    if pf <= PF_MIN:
        return False, f"PF {pf:.2f} <= {PF_MIN}"
    if net <= 0:
        return False, f"net {net:.2f}% <= 0"
    # AGENTS.md: "a strategy that only wins by losing less than a collapsing
    # market is not alpha (compare vs buy & hold)". The module always claimed
    # this was enforced; until 2026-10-25 it was only reported, so a row with
    # excess_% = -279 was admitted as a candidate.
    if excess <= 0:
        return False, f"excess {excess:+.1f}pp <= 0 vs buy & hold"
    return True, "ok"


def scan() -> dict:
    """Lee los artefactos y devuelve candidatos admitidos + conteos.

    No muta nada. Devuelve un dict serializable, apto para /health.
    """
    arts = _artifacts()
    out: dict = {
        "root": str(BACKTEST_ROOT),
        "root_exists": BACKTEST_ROOT.is_dir(),
        "artifacts": len(arts),
        "repo_costs": {"fee_bps": REPO_FEE_BPS, "slippage_bps": REPO_SLIP_BPS,
                       "roundtrip_bps": REPO_COST_RT_BPS},
        "gates": {"pf_min": PF_MIN, "trades_min": TRADES_MIN},
        "candidates": {},          # symbol -> [ {id,name,run_id,artifact,...} ]
        "scored": {},              # symbol -> {rows_scored, rows_passed}
        "other_cost_artifacts": 0,
        "slice_artifacts": 0,      # diagnostic slices (slice_from/slice_to): never candidates
        "unreadable": 0,
        "latest_artifact_utc": None,
        "scanned_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not arts:
        out["error"] = "sin directorio results/runs"
        return out

    latest = 0.0
    for p in arts:
        j = _load(p)
        if j is None:
            out["unreadable"] += 1
            continue
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            pass
        m = j.get("metrics") or {}
        sym = j.get("symbol")
        tf = j.get("timeframe")
        if not sym or not tf:
            continue
        # A slice artifact (`slice_from`/`slice_to`) is a diagnostic of the
        # gates run, not a validated result: its window is 8-9 months, it can
        # be more recent than the full-window artifact and therefore WIN the
        # dedupe below, overwriting a real candidate with a slice number.
        params = j.get("params") or {}
        if isinstance(params, dict) and ("slice_from" in params or "slice_to" in params):
            out["slice_artifacts"] += 1
            continue
        if not _repo_costs(j):
            out["other_cost_artifacts"] += 1
            continue
        rows = j.get("tables")
        if not isinstance(rows, list) or not rows:
            continue
        key = f"{sym}@{tf}"
        sc = out["scored"].setdefault(key, {"rows_scored": 0, "rows_passed": 0})
        for row in rows:
            if not isinstance(row, dict):
                continue
            sc["rows_scored"] += 1
            ok, _why = _passes(row)
            if not ok:
                continue
            sc["rows_passed"] += 1
            cand = {
                "id": row.get("id"),
                "name": row.get("name"),
                "symbol": sym,
                "timeframe": tf,
                "strategy": j.get("strategy"),
                "run_id": j.get("run_id"),
                "artifact": str(p),
                "created_utc": j.get("created_utc"),
                "pf": row.get("PF"),
                "net_pct": row.get("net_%"),
                "gross_pct": row.get("gross_%"),
                "excess_pct": row.get("excess_%"),
                "trades": row.get("trades"),
                "win_pct": row.get("win_%"),
                "sharpe": row.get("sharpe"),
                "maxdd_pct": row.get("maxDD_%"),
                "buy_hold_pct": row.get("buy_hold_%"),
                "exposure_pct": row.get("exposure_%"),
                # Concentracion: el auto-router recorta riesgo si los 5 mejores
                # trades son >100% del PnL. El gate NO lo exige (dejaria la
                # lista vacia: los 4 candidatos lo incumplen), pero el dato
                # viaja para que la politica de tamano sea explicita.
                "conc_passes": row.get("conc_passes"),
                "conc_top5_share_pct": row.get("conc_top5_share_pct"),
                "conc_passes_decile": row.get("conc_passes_decile"),
                "funding_drag_pp": row.get("funding_drag_pp"),
            }
            # DEDUPE: la misma estrategia puede tener varios artefactos
            # (el backtest-system se ha re-ejecutado con datos frescos). Se
            # queda el MAS RECIENTE y se descartan los anteriores: si no, el
            # endpoint devuelve la misma estrategia 3 veces con metricas
            # distintas y contradictorias (excess +0.3% vs -4.9%).
            bucket = out["candidates"].setdefault(key, [])
            for existing in bucket:
                if existing["id"] == cand["id"]:
                    if str(cand.get("created_utc") or "") > str(existing.get("created_utc") or ""):
                        existing.update(cand)
                    break
            else:
                bucket.append(cand)
    out["latest_artifact_utc"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(latest)) if latest else None
    for v in out["candidates"].values():
        v.sort(key=lambda c: -(c.get("pf") or 0))
    return out


def for_pair(pair: str) -> dict:
    """Candidatos admitidos para un par del engine ('XRP/USD' -> 'XRPUSDT')."""
    data = cached_scan()
    sym = _PAIR_ALIAS.get(pair, pair.replace("/", "").upper())
    hits = []
    for key, lst in data.get("candidates", {}).items():
        s, _, tf = key.partition("@")
        if s != sym:
            continue
        for c in lst:
            c = dict(c)
            c["engine_pair"] = pair
            c["backtest_tf"] = tf
            hits.append(c)
    hits.sort(key=lambda c: (c.get("backtest_tf", ""), -(c.get("pf") or 0)))
    return {
        "pair": pair,
        "symbol": sym,
        "has_candidate": bool(hits),
        "candidates": hits,
        "artifacts_scanned": data.get("artifacts"),
        "root_exists": data.get("root_exists"),
    }


# ── cache ──────────────────────────────────────────────────────────────────
# /health se llama a menudo y leer 297 artefactos en cada request es caro.
_CACHE: dict = {"at": 0.0, "data": None}
_CACHE_TTL_S = 300.0


def cached_scan(ttl_s: float = _CACHE_TTL_S) -> dict:
    now = time.time()
    if _CACHE["data"] is not None and (now - _CACHE["at"]) < ttl_s:
        return _CACHE["data"]
    data = scan()
    _CACHE["at"] = now
    _CACHE["data"] = data
    return data


def link_summary() -> dict:
    """Resumen compacto para /health: un par -> tiene candidato o no.

    NO lleva metricas: /health se lee mucho y las metricas viven en el
    artefacto. Los numeros se piden a `for_pair()` / al endpoint de detalle.
    """
    data = cached_scan()
    by_pair: dict = {}
    for pair in _PAIR_ALIAS:
        sym = _PAIR_ALIAS[pair]
        keys = [k for k in data.get("scored", {}) if k.startswith(sym + "@")]
        n_cand = sum(len(data.get("candidates", {}).get(k, [])) for k in keys)
        by_pair[pair] = {
            "has_candidate": n_cand > 0,
            "candidates": n_cand,
            "evaluated_tfs": sorted(k.partition("@")[2] for k in keys),
        }
    return {
        "root": data.get("root"),
        "root_exists": data.get("root_exists"),
        "artifacts": data.get("artifacts"),
        "latest_artifact_utc": data.get("latest_artifact_utc"),
        "scanned_utc": data.get("scanned_utc"),
        "repo_costs": data.get("repo_costs"),
        "gates": data.get("gates"),
        "pairs": by_pair,
        "pairs_with_candidate": sorted(
            p for p, v in by_pair.items() if v["has_candidate"]),
    }


def _main() -> int:
    data = scan()
    print(f"root: {data['root']} (existe={data['root_exists']})")
    print(f"artefactos: {data['artifacts']} | con costes del repo: "
          f"{data['artifacts'] - data['other_cost_artifacts'] - data['unreadable']}"
          f" | otros costes: {data['other_cost_artifacts']}")
    print(f"ultimo artefacto: {data['latest_artifact_utc']}")
    print()
    if not data["scored"]:
        print("sin artefactos con costes del repo (gates: PF>1.2, n>=30, net>0, 11bps)")
        return 0
    for key in sorted(data["scored"]):
        sc = data["scored"][key]
        lst = data["candidates"].get(key, [])
        if not lst:
            print(f"{key}: 0 admitidos de {sc['rows_scored']} filas evaluadas "
                  f"(ninguno pasa los gates)")
            continue
        print(f"{key}: {len(lst)} admitido(s) de {sc['rows_scored']} filas")
        for c in lst:
            print(f"   {c['id']} {c['name']} | PF {c['pf']:.2f} | net {c['net_pct']:+.2f}% "
                  f"| excess {c['excess_pct']:+.2f}% | n {c['trades']:.0f} | "
                  f"DD {c['maxdd_pct']:.1f}% | run_id {c['run_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
