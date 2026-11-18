"""backtest_verify.py — ¿este artefacto sigue siendo un resultado?

REGLA 0 dice "ningun numero sin artefacto". Falta la otra mitad, que es la
que muerde en produccion: **un artefacto sin verificar tampoco es un
resultado**. Un JSON en `results/runs/` puede ser de un motor que ya cambio,
de datos que se editaron despues del corte, o de parametros que ya no son los
que se van a operar.

El backtest-system YA tiene la autoridad para responder eso:

    python code/vrun.py verify <run_id>
      0 OK        reproduce bit-for-bit
      2 REFUSED   drift (motor, datos o parametros cambiaron)
      3 STALE     datos extendidos/editados dentro del corte

Este modulo NO reimplementa esa comprobacion: la INVOCA, guarda el veredicto
y lo deja consultable. Reimplementarla seria crear una segunda autoridad
-estadistica, ademas- que puede discrepar de la primera. Aqui solo se
transporta el veredicto.

Escritura: SOLO en trading-engines (`scanner-engine/state/backtest_verify.json`).
El backtest-system se lee; no se escribe ni se modifica.

Uso:
    python scanner-engine/backtest_verify.py --candidates   # verifica los admitidos
    python scanner-engine/backtest_verify.py <run_id> [...]
    python scanner-engine/backtest_verify.py --status       # solo el ledger
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "state" / "backtest_verify.json"

BACKTEST_ROOT = Path(os.environ.get(
    "BACKTEST_SYSTEM_ROOT",
    r"C:\Users\Lenovo\Documents\backtest-system",
))
RUNS = BACKTEST_ROOT / "results" / "runs"

# veredicto -> codigo de salida de vrun.py verify
_RC_STATUS = {0: "OK", 2: "REFUSED", 3: "STALE"}
TTL_S = float(os.environ.get("BACKTEST_VERIFY_TTL_S", str(24 * 3600)))
TIMEOUT_S = float(os.environ.get("BACKTEST_VERIFY_TIMEOUT_S", "180"))


def _load() -> dict:
    try:
        d = json.loads(LEDGER.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        tmp = LEDGER.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(LEDGER)
    except OSError:
        pass


def artifact_path(run_id: str) -> Path:
    return RUNS / f"{run_id}.json"


def _artifact_stamp(run_id: str) -> float:
    try:
        return artifact_path(run_id).stat().st_mtime
    except OSError:
        return 0.0


def cached_status(run_id: str) -> str:
    """Veredicto guardado, o 'not_checked'. NUNCA 'OK' por defecto."""
    rec = _load().get(run_id) or {}
    return str(rec.get("status") or "not_checked")


def verify(run_id: str, force: bool = False) -> dict:
    """Ejecuta `vrun.py verify` y cachea el veredicto.

    Fail-closed: si no se puede ejecutar (sin python, timeout, excepcion) el
    estado es 'ERROR', no 'OK'. Un artefacto ausente es 'MISSING'.
    """
    rec = _load()
    prev = rec.get(run_id) or {}
    stamp = _artifact_stamp(run_id)
    if (not force and prev
            and float(prev.get("artifact_mtime") or 0) == stamp
            and (time.time() - float(prev.get("checked_ts") or 0)) < TTL_S):
        return prev

    out = {"run_id": run_id, "status": "ERROR", "rc": None, "line": "",
           "checked_ts": time.time(), "artifact_mtime": stamp, "elapsed_s": 0.0}
    art = artifact_path(run_id)
    if not art.is_file():
        out.update(status="MISSING",
                   line="no existe el artefacto (REGLA 0: un numero sin "
                        "artefacto no es un resultado)")
        rec[run_id] = out
        _save(rec)
        return out

    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, "code/vrun.py", "verify", run_id],
            cwd=str(BACKTEST_ROOT), capture_output=True, text=True,
            timeout=TIMEOUT_S,
            # sin consola parpadeando; el texto va a stdout/stderr capturados
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rc = p.returncode
        line = (p.stdout or p.stderr or "").strip().splitlines()
        out.update(rc=rc, status=_RC_STATUS.get(rc, "ERROR"),
                   line=(line[0] if line else "")[:300])
    except (OSError, subprocess.SubprocessError) as e:
        out.update(status="ERROR", line=f"{type(e).__name__}: {e}"[:300])
    out["elapsed_s"] = round(time.time() - t0, 2)
    rec[run_id] = out
    _save(rec)
    return out


def verify_many(run_ids, force: bool = False) -> dict:
    return {rid: verify(rid, force=force) for rid in run_ids}


def artifact_sha256(run_id: str) -> str | None:
    try:
        return hashlib.sha256(artifact_path(run_id).read_bytes()).hexdigest()
    except OSError:
        return None


def _candidates() -> list[str]:
    sys.path.insert(0, str(HERE))
    import backtest_link  # noqa: PLC0415 — import tardio: evita ciclo

    data = backtest_link.cached_scan(ttl_s=0)
    ids: list[str] = []
    for lst in data.get("candidates", {}).values():
        for c in lst:
            rid = c.get("run_id")
            if rid and rid not in ids:
                ids.append(rid)
    return ids


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--status":
        rec = _load()
        if not rec:
            print("ledger vacio: ningun artefacto verificado todavia")
            return 0
        for rid, r in sorted(rec.items(), key=lambda kv: kv[1].get("checked_ts", 0)):
            age_h = (time.time() - float(r.get("checked_ts") or 0)) / 3600
            print(f"  {rid} {r.get('status'):9s} rc={r.get('rc')} "
                  f"hace {age_h:.1f}h  {str(r.get('line'))[:70]}")
        return 0
    force = "--force" in args
    args = [a for a in args if not a.startswith("--")]
    ids = _candidates() if (not args or args == ["candidates"]) else args
    if not ids:
        print("sin candidatos admitidos que verificar")
        return 0
    bad = 0
    for rid in ids:
        r = verify(rid, force=force)
        if r["status"] != "OK":
            bad += 1
        print(f"  {rid} {r['status']:9s} rc={r.get('rc')} "
              f"{r.get('elapsed_s')}s  {str(r.get('line'))[:90]}")
    print(f"\n{len(ids) - bad}/{len(ids)} OK")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
