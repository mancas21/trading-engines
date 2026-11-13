"""Auto-router: mantiene la cuenta sincronizada con la senal viva validada.

Cada ciclo, para cada simbolo con candidato admitido:

  1. calcula la direccion que el ENGINE quiere (live_signal, mismo codigo que
     el backtest, validado contra el artefacto);
  2. lee la posicion real;
  3. decide: abrir, cerrar, mantener o esperar;
  4. si hay que operar, delega en router.py (tamano por riesgo + stop en el
     exchange) o en flatten.py (cierre).

    python execution/auto_router.py                 # plan, no envia
    python execution/auto_router.py --execute       # un ciclo
    python execution/auto_router.py --loop --execute

Politica de entrada tardia: si el engine lleva mas de --max-entry-lag-bars
barras en la posicion, NO se persigue. Un motor que arranca a mitad de una
tendencia de 186 horas no esta replicando el backtest, esta copiando su
resultado. Por defecto 24 barras (1 dia). Si la posicion viva es la contraria,
se cierra igual (el riesgo no se discute), pero no se abre la pata nueva.

Concentracion: si el candidato no pasa `conc_passes` (sus 5 mejores trades son
mas del 100% del PnL), el riesgo se multiplica por 0.5. No se veta: el propio
engine advierte que recortar la cola derecha penaliza a S02/S10 por construccion.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import flatten  # noqa: E402
import live_signal  # noqa: E402
import proclock  # noqa: E402
import router  # noqa: E402
import state_store  # noqa: E402
from binance_testnet import BinanceError, TestnetClient  # noqa: E402

LOG = HERE / "logs" / "auto_router.jsonl"
LOCK = "auto_router"

RISK_PCT = 0.5
CONC_HAIRCUT = 0.5
LEVERAGE = 5
MAX_LAG_DEFAULT = 24


def log(rec: dict) -> None:
    try:
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def risk_for(cand: dict) -> tuple[float, str]:
    conc = cand.get("conc_passes")
    if conc is True:
        return RISK_PCT, "conc_passes=true"
    if conc is False:
        return RISK_PCT * CONC_HAIRCUT, "conc_passes=false -> riesgo x0.5"
    return RISK_PCT * CONC_HAIRCUT, "conc_passes ausente -> riesgo x0.5"


def decision(*, sig: dict, pos_amt: float, max_lag: int) -> dict:
    want = int(sig.get("direction") or 0)
    have = 0 if abs(pos_amt) < 1e-12 else (1 if pos_amt > 0 else -1)
    age = sig.get("bars_held")
    base = {"want": want, "have": have, "age": age}

    if not sig.get("trusted", True):
        return {**base, "action": "skip", "why": "ventana corta: estado no fiable"}
    if want == 0:
        return ({**base, "action": "close", "why": "senal plana"}
                if have != 0 else {**base, "action": "wait", "why": "plano y sin senal"})
    if have == want:
        return {**base, "action": "keep", "why": "posicion alineada"}
    if have == -want:
        # Cerrar es obligatorio; ABRIR la pata contraria es una entrada nueva.
        # Si el engine lleva mucho en esa direccion, no se persigue: se cierra
        # y se queda plano (misma politica que una entrada tardia desde cero).
        if age is not None and age > max_lag:
            return {**base, "action": "close",
                    "why": f"senal contraria pero entrada tardia ({age} barras): "
                           f"cierro sin perseguir"}
        return {**base, "action": "reverse", "why": "senal contraria"}
    if age is not None and age > max_lag:
        return {**base, "action": "skip",
                "why": f"entrada tardia: el engine lleva {age} barras (max {max_lag})"}
    return {**base, "action": "open", "why": "senal nueva"}


def entry_stop(sig: dict, mark: float, action: str) -> tuple[float | None, str]:
    """Nivel de stop para la entrada, y por que no vale si no vale.

    - `open`: se usa el stop DEL ENGINE (`sig["stop"]`). La estrategia sale en
      ese nivel, no a una distancia movil del precio actual; reanclar el stop al
      mark cambia la invalidacion y, en una entrada tardia, el riesgo real.
    - `reverse`: el `sig["stop"]` pertenece a la posicion contraria, no a la
      pata nueva. Se usa la distancia ATR del engine desde el mark.
    """
    d = int(sig.get("direction") or 0)
    if d not in (-1, 1):
        return None, "direccion invalida"
    if action == "open" and sig.get("stop"):
        stop = float(sig["stop"])
        if (mark - stop) * d <= 0:
            return None, (f"el precio {mark:g} ya cruzo el stop del engine "
                          f"{stop:g}; la entrada llega tarde y no se abre")
        return stop, ""
    sl = float(sig.get("sl_dist") or sig.get("sl_dist_last") or 0)
    if not (sl > 0) or sl != sl:            # 0 o NaN
        return None, f"sin ATR utilizable (sl_dist={sig.get('sl_dist')})"
    return mark - d * sl, ""


def run_cycle(execute: bool, max_lag: int, leverage: int,
              max_notional: float | None) -> int:
    cands = live_signal.candidates()
    c = TestnetClient(dry_run=not execute, max_notional_usd=max_notional)
    c.sync_time()
    marks = {}
    positions = {p["symbol"]: float(p["positionAmt"])
                 for p in c.positions() if float(p.get("positionAmt", 0)) != 0}

    rows = []
    rc = 0
    for sym, cand in sorted(cands.items()):
        if sym not in c.allow_symbols:
            rows.append({"symbol": sym, "action": "skip",
                         "why": f"fuera de ALLOW_SYMBOLS={c.allow_symbols}"})
            continue
        try:
            sig = live_signal.compute(sym, cand["id"])
        except Exception as e:  # noqa: BLE001
            rows.append({"symbol": sym, "action": "error",
                         "why": f"{type(e).__name__}: {e}"})
            rc = max(rc, 1)
            continue

        pos_amt = positions.get(sym, 0.0)
        d = decision(sig=sig, pos_amt=pos_amt, max_lag=max_lag)
        risk, why_risk = risk_for(cand)
        mark = c.mark_price(sym) if d["action"] in ("open", "reverse") else None
        if mark:
            marks[sym] = mark

        row = {"symbol": sym, "strategy": cand["id"], "pf": cand["pf"],
               "run_id": cand["run_id"], "conc_passes": cand.get("conc_passes"),
               "risk_pct": risk, **d, "last_close": sig.get("last_close"),
               "bars_held": sig.get("bars_held"), "ts": time.strftime(
                   "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        rows.append(row)

        if d["action"] == "keep":
            continue
        if d["action"] in ("wait", "skip", "error"):
            continue

        if d["action"] in ("close", "reverse"):
            if not execute:
                print(f"{sym}: CERRAR ({d['why']})")
            else:
                try:
                    r = flatten.run(sym, True, quiet=False)
                except (BinanceError, SafetyError) as e:
                    row["close_error"] = f"{type(e).__name__}: {e}"
                    print(f"{sym}: ERROR al cerrar: {e}")
                    rc = max(rc, 1)
                    continue
                row["close_rc"] = r
                if r not in (0, 2):
                    rc = max(rc, 1)
            if d["action"] == "close":
                continue

        # open (o la pata nueva de un reverse)
        if mark is None:
            continue
        stop, why_stop = entry_stop(sig, mark, d["action"])
        if stop is None:
            print(f"{sym}: NO ABRO: {why_stop}")
            row["action"] = "skip"
            row["why"] = why_stop
            continue
        side = "LONG" if sig["direction"] > 0 else "SHORT"
        ns = argparse.Namespace(
            signal=None, symbol=sym, side=side, stop=float(stop),
            take_profit=None, candidate_id=cand["id"], run_id=cand["run_id"],
            strategy=cand["id"], risk_pct=risk, leverage=leverage,
            max_notional=max_notional, margin_pct=25.0,
            require_candidate=True, execute=execute,
        )
        if not execute:
            print(f"{sym}: ABRIR {side} stop {stop:.6g} "
                  f"(riesgo {risk:g}%, {why_risk})  mark {mark:g}")
            continue
        try:
            r = router.run(ns)
        except (BinanceError, SafetyError) as e:
            # router.run ya limpia su intencion si la entrada no llego a existir;
            # aqui solo se evita que un simbolo tumbe el ciclo entero.
            row["open_error"] = f"{type(e).__name__}: {e}"
            print(f"{sym}: ERROR al abrir: {e}")
            r = 1
        row["open_rc"] = r
        if r != 0:
            rc = max(rc, 1)
            log(row)
            continue
        state_store.patch(sym, strategy=cand["id"], candidate_id=cand["id"],
                          run_id=cand["run_id"], conc_passes=cand.get("conc_passes"),
                          auto=True)

    for row in rows:
        log(row)
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=300,
                    help="segundos entre ciclos en --loop")
    ap.add_argument("--max-entry-lag-bars", type=int, default=MAX_LAG_DEFAULT)
    ap.add_argument("--leverage", type=int, default=LEVERAGE)
    ap.add_argument("--max-notional", type=float, default=None)
    a = ap.parse_args()

    if not proclock.acquire(LOCK):
        print(f"ya hay un auto_router vivo (pid {proclock.holder(LOCK)})")
        return 3
    try:
        while True:
            rc = run_cycle(a.execute, a.max_entry_lag_bars, a.leverage, a.max_notional)
            if not a.loop:
                return rc
            time.sleep(max(30, a.interval))
    except KeyboardInterrupt:
        return 0
    finally:
        proclock.release(LOCK)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BinanceError as e:
        print(f"BINANCE: {e}")
        raise SystemExit(7)
