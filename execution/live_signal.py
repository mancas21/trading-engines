"""Estado vivo de una estrategia validada, calculado con el ENGINE AUDITADO.

No reimplementa las reglas: importa `cbpack_funding_engine` del backtest-system
(sha256 fijado en los artefactos) y le pasa las velas de producción. Asi la
senal viva es, por construccion, la misma que la del backtest.

    python execution/live_signal.py --symbol XRPUSDT --strategy S10
    python execution/live_signal.py --all

Lo que devuelve `compute()`:
    direction   1 long, -1 short, 0 plano
    entry       precio de entrada del backtest (con slip)
    stop        entry - dir * sl_dist  (el SL que usa el engine)
    bars_held   barras desde la entrada
    trusted     False si la posicion pudo abrirse antes de la ventana

El engine cierra la posicion abierta al final con reason="end_of_data"; ese
trade final ES la posicion viva, no una salida real.

LIMITE: el engine cobra funding del fichero historico. Aqui se ejecuta con
charge_funding=False porque el funding lo cobra el exchange en vivo; la
direccion y el stop no dependen de el.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE_DIR = Path(r"C:\Users\Lenovo\Documents\backtest-system\code")
BASE = "https://fapi.binance.com"
WARMUP_BARS = 300          # S10 necesita 168+96; S02 148. 300 deja margen.
MAX_BARS = 3000            # ~125 dias de 1h

if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import cbpack_funding_engine as engine  # noqa: E402
import pandas as pd  # noqa: E402


def _get(url: str) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "qae-live-signal/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def fetch_klines(symbol: str, interval: str = "1h", bars: int = MAX_BARS) -> pd.DataFrame:
    """Velas CERRADAS de produccion, paginando hacia atras."""
    rows: list[list] = []
    end = None
    remaining = bars
    while remaining > 0:
        limit = min(1500, remaining)
        url = (f"{BASE}/fapi/v1/klines?symbol={symbol.upper()}"
               f"&interval={interval}&limit={limit}")
        if end is not None:
            url += f"&endTime={end}"
        chunk = _get(url)
        if not isinstance(chunk, list) or not chunk:
            break
        rows = chunk + rows
        end = int(chunk[0][0]) - 1
        remaining -= len(chunk)
        if len(chunk) < limit:
            break

    now_ms = int(time.time() * 1000)
    rows = [r for r in rows if int(r[6]) < now_ms]           # fuera la vela en curso
    if not rows:
        raise RuntimeError(f"{symbol}: sin velas cerradas")

    df = pd.DataFrame({
        "open_ts": [int(r[0]) for r in rows],
        "open": [float(r[1]) for r in rows],
        "high": [float(r[2]) for r in rows],
        "low": [float(r[3]) for r in rows],
        "close": [float(r[4]) for r in rows],
        "volume": [float(r[5]) for r in rows],
        "n_trades": [int(r[8]) for r in rows],
        "taker_buy_base": [float(r[9]) for r in rows],
    })
    df.index = pd.to_datetime(df.pop("open_ts"), unit="ms", utc=True)
    df = engine.prepare(df)
    return df[~df.index.duplicated(keep="last")].sort_index()


def compute(symbol: str, strategy: str = "S10", bars: int = MAX_BARS,
            interval: str = "1h") -> dict:
    df = fetch_klines(symbol, interval, bars)
    sig = engine.make_signals(df, strategy)
    cfg = engine.BacktestConfig(fee_bps=5.0, slippage_bps=0.5,
                                charge_funding=False, initial_capital=10_000.0)
    res = engine.backtest(df, sig, cfg)
    tr = res.trades
    out = {
        "symbol": symbol.upper(), "strategy": strategy, "interval": interval,
        "bars": len(df), "last_bar_utc": df.index[-1].isoformat(),
        "last_close": float(df["close"].iloc[-1]),
        "direction": 0, "entry": None, "stop": None, "bars_held": None,
        "sl_dist_last": float(sig["sl_dist"].iloc[-1]) if len(sig) else None,
        "stop_for_new": None, "trusted": True,
    }
    if tr is None or not len(tr) or str(tr.iloc[-1]["reason"]) != "end_of_data":
        return out

    t = tr.iloc[-1]
    entry_time = t["entry_time"]
    entry_i = int(df.index.get_loc(entry_time))
    sl_d = float(sig.loc[entry_time, "sl_dist"])
    d = int(t["dir"])
    entry = float(t["entry"])
    last_close = float(df["close"].iloc[-1])
    out.update({
        "direction": d,
        "entry": entry,
        "stop": entry - d * sl_d,
        "entry_utc": pd.Timestamp(entry_time).isoformat(),
        "bars_held": len(df) - 1 - entry_i,
        "sl_dist": sl_d,
        "sl_dist_bps": sl_d / entry * 1e4,
        "trusted": entry_i >= WARMUP_BARS,
        "entry_i": entry_i,
        "open_pnl_pct": (last_close / entry - 1) * 100 * d,
        # stop que corresponde a una entrada NUEVA ahora (ATR de la ultima vela)
        "stop_for_new": last_close - d * float(sig["sl_dist"].iloc[-1]),
    })
    return out


def candidates() -> dict:
    """Simbolo -> mejor candidato admitido (por PF), desde el gate del scanner."""
    scanner = HERE.parent / "scanner-engine"
    if str(scanner) not in sys.path:
        sys.path.insert(0, str(scanner))
    import backtest_link  # noqa: PLC0415

    data = backtest_link.scan()
    best: dict = {}
    for key, lst in data.get("candidates", {}).items():
        sym = key.partition("@")[0]
        for c in lst:
            cur = best.get(sym)
            if cur is None or float(c["pf"]) > float(cur["pf"]):
                best[sym] = c
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol")
    ap.add_argument("--strategy", default="S10")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--bars", type=int, default=MAX_BARS)
    a = ap.parse_args()

    if a.all:
        seen = set()
        for sym, c in sorted(candidates().items()):
            sid = c["id"]
            if (sym, sid) in seen:
                continue
            seen.add((sym, sid))
            try:
                r = compute(sym, sid, a.bars)
                print(json.dumps(r, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                print(json.dumps({"symbol": sym, "strategy": sid,
                                  "error": f"{type(e).__name__}: {e}"}))
        return 0

    if not a.symbol:
        print("hace falta --symbol o --all")
        return 2
    r = compute(a.symbol, a.strategy, a.bars)
    print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
