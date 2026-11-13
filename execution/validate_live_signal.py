"""Valida que la senal viva sale del mismo engine y que truncar no la cambia.

Dos comprobaciones por candidato:

1. REPRODUCE EL ARTEFACTO. Corre el engine sobre el CSV del backtest con
   charge_funding=False y compara `trades`, `pf_no_funding` y
   `net_no_funding_pct` con la fila del artefacto. Si no cuadra, el mapeo de
   datos o la configuracion estan mal y la senal viva no vale.

2. TRUNCAR NO CAMBIA EL ESTADO. Corre el engine sobre la historia completa y
   sobre las ultimas N velas; compara direccion/entrada/stop/antiguedad. Si la
   posicion viva es mas vieja que la ventana, lo dice.

    python execution/validate_live_signal.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scanner-engine"))
sys.path.insert(0, r"C:\Users\Lenovo\Documents\backtest-system\code")

import backtest_link  # noqa: E402
import cbpack_funding_engine as engine  # noqa: E402
import pandas as pd  # noqa: E402

WARMUP = 300
CUTS = (2000, 2500, 3000)


def load_csv(path: str) -> pd.DataFrame:
    """El CSV del repo usa `open_ts` (ms), que load_ohlcv no reconoce."""
    df = pd.read_csv(path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    tcol = "open_ts" if "open_ts" in df.columns else "open_time"
    df.index = pd.to_datetime(df[tcol], unit="ms", utc=True)
    df = df.drop(columns=[c for c in ("open_ts", "open_time") if c in df.columns])
    return engine.prepare(df)


def state_from(df: pd.DataFrame, sid: str) -> dict:
    sig = engine.make_signals(df, sid)
    cfg = engine.BacktestConfig(fee_bps=5.0, slippage_bps=0.5,
                                charge_funding=False, initial_capital=10_000.0)
    tr = engine.backtest(df, sig, cfg).trades
    if tr is None or not len(tr) or str(tr.iloc[-1]["reason"]) != "end_of_data":
        return {"direction": 0}
    t = tr.iloc[-1]
    i = int(df.index.get_loc(t["entry_time"]))
    d = int(t["dir"])
    entry = float(t["entry"])
    sl_d = float(sig.loc[t["entry_time"], "sl_dist"])
    return {"direction": d, "entry": round(entry, 10),
            "stop": round(entry - d * sl_d, 10), "bars_held": len(df) - 1 - i,
            "entry_i": i, "trusted": i >= WARMUP}


def main() -> int:
    cands = {}
    for key, lst in backtest_link.scan().get("candidates", {}).items():
        sym, _, tf = key.partition("@")
        for c in lst:
            cands.setdefault((sym, tf, c["id"]), c)

    fails = 0
    for (sym, tf, sid), c in sorted(cands.items()):
        art = json.load(open(c["artifact"], encoding="utf-8"))
        row = next((r for r in art["tables"] if r.get("id") == sid and
                    abs(float(r.get("PF", 0)) - float(c["pf"])) < 1e-9), None)
        src = next((i["path"] for i in art["inputs"] if i["path"].endswith(".csv")), None)
        print(f"\n== {sym} {sid}  {src}")
        if not src or not Path(src).is_file():
            print("  FALTA EL CSV")
            fails += 1
            continue

        df = load_csv(src)
        sig = engine.make_signals(df, sid)
        res = engine.backtest(df, sig, engine.BacktestConfig(
            fee_bps=5.0, slippage_bps=0.5, charge_funding=False))
        met = res.metrics
        got = {
            "trades": float(met["n_trades"]),
            "pf_no_funding": float(met["profit_factor"]),
            "net_no_funding_pct": float(met["net_return_pct"]),
        }
        want = {
            "trades": float(row.get("trades")),
            "pf_no_funding": float(row.get("pf_no_funding")),
            "net_no_funding_pct": float(row.get("net_no_funding_pct")),
        }
        ok = True
        for k in got:
            same = abs(got[k] - want[k]) <= max(1e-6, abs(want[k]) * 1e-9)
            ok &= same
            print(f"  {k:22s} engine {got[k]:>12.6f}  artefacto {want[k]:>12.6f}  "
                  f"{'OK' if same else 'DIF'}")
        if not ok:
            fails += 1

        full = state_from(df, sid)
        print(f"  estado historico: {full}")
        for cut in CUTS:
            if len(df) <= cut:
                continue
            s = state_from(df.iloc[-cut:], sid)
            s.pop("entry_i", None)
            f = dict(full)
            f.pop("entry_i", None)
            same = (s.get("direction") == f.get("direction")
                    and (s.get("entry") is None or abs(s["entry"] - f["entry"]) < 1e-9)
                    and (s.get("stop") is None or abs(s["stop"] - f["stop"]) < 1e-9))
            print(f"  corte {cut}: {s}  {'OK' if same else 'DIF'}")
            if not same:
                fails += 1

    print(f"\n{'TODO OK' if fails == 0 else f'{fails} FALLOS'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
