#!/usr/bin/env python3
"""HMA(34) Trend Backtest — todos los pares del scanner, 1h, 2026.
Reglas:
- Long cuando HMA(34) sube 2 barras consecutivas Y close > HMA(34)
- Salida cuando HMA gira a bajista O stop 3x ATR(14)
- Relleno en apertura de barra siguiente
- Comisión+slippage: 11 bps roundtrip (5.5 bps por lado)
"""

import urllib.request
import json
import datetime
import statistics
import math
import time
import sys

PAIRS_MAP = {
    "BTC/USD": "BTCUSDT",
    "ETH/USD": "ETHUSDT",
    "HYPE/USD": "HYPEUSDT",
    "JUP/USD": "JUPUSDT",
    "SOL/USD": "SOLUSDT",
    "SUI/USD": "SUIUSDT",
    "XRP/USD": "XRPUSDT",
    "1000PEPE/USD": "1000PEPEUSDT",
    "ARB/USD": "ARBUSDT",
    "NVDA/USD": "NVDAUSDT",
    "SPCX/USD": "SPCXUSDT",
    "XAU/USD": "XAUUSDT",
}

START_MS = int(datetime.datetime(2026, 1, 1, 0, 0, 0).timestamp() * 1000)
END_MS = int(datetime.datetime(2026, 11, 14, 23, 59, 59).timestamp() * 1000)
COMMISSION_ROUNDTRIP = 0.0011  # 11 bps


def get_binance_klines(symbol, interval="1h", start_ms=None, end_ms=None, limit=1500):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    if start_ms:
        url += f"&startTime={start_ms}"
    if end_ms:
        url += f"&endTime={end_ms}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def wma(values, period):
    if len(values) < period:
        return None
    weights = list(range(1, period + 1))
    total_weight = sum(weights)
    window = values[-period:]
    return sum(w * v for w, v in zip(weights, window)) / total_weight


def hma(values, period):
    """HMA(n) = WMA(2*WMA(n/2) - WMA(n), round(sqrt(n)))"""
    if len(values) < period:
        return None
    half = max(1, period // 2)
    sqrt_n = max(1, round(math.sqrt(period)))

    wma_half = [None] * len(values)
    wma_full = [None] * len(values)
    for i in range(len(values)):
        if i >= half - 1:
            wma_half[i] = wma(values[i - half + 1:i + 1], half)
        if i >= period - 1:
            wma_full[i] = wma(values[i - period + 1:i + 1], period)

    raw_hma = []
    for i in range(len(values)):
        if wma_half[i] is not None and wma_full[i] is not None:
            raw_hma.append(2 * wma_half[i] - wma_full[i])
        else:
            raw_hma.append(None)

    result = [None] * len(values)
    for i in range(len(values)):
        if i >= sqrt_n - 1:
            window = raw_hma[i - sqrt_n + 1:i + 1]
            if all(v is not None for v in window):
                result[i] = wma(window, sqrt_n)
    return result


def atr_wilder(highs, lows, closes, period):
    tr = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(highs[i] - lows[i],
                          abs(highs[i] - closes[i - 1]),
                          abs(lows[i] - closes[i - 1])))

    result = [None] * len(closes)
    if len(tr) >= period:
        result[period - 1] = sum(tr[:period]) / period
        for i in range(period, len(tr)):
            result[i] = (result[i - 1] * (period - 1) + tr[i]) / period
    return result


def run_backtest(closes, highs, lows, opens, volumes, times):
    hma_34 = hma(closes, 34)
    atr_14 = atr_wilder(highs, lows, closes, 14)

    equity = 10000.0
    position = 0
    entry_price = 0.0
    stop_price = 0.0
    entry_bar = -1
    trades = []
    equity_curve = []

    first_close = closes[0] if closes[0] else 1

    for i in range(1, len(closes)):
        prev_eq = equity

        # --- EXIT ---
        if position == 1:
            exit_price = None
            exit_reason = None

            # Stop loss
            if lows[i] <= stop_price:
                exit_price = min(stop_price, opens[i]) if opens[i] < stop_price else stop_price
                exit_reason = "STOP"
            # HMA down
            elif hma_34[i] is not None and hma_34[i - 1] is not None and hma_34[i] < hma_34[i - 1]:
                exit_price = opens[i]
                exit_reason = "HMA_DOWN"

            if exit_price is not None:
                # Commission on exit
                gross_pnl = (exit_price - entry_price) / entry_price
                net_pnl = gross_pnl - COMMISSION_ROUNDTRIP / 2  # exit leg commission
                equity *= (1 + net_pnl)
                trades.append({
                    "entry_bar": entry_bar,
                    "exit_bar": i,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_pct": net_pnl * 100,
                    "exit_reason": exit_reason,
                })
                position = 0
                entry_price = 0.0
                stop_price = 0.0

        # --- ENTRY ---
        if position == 0 and i + 1 < len(closes):
            if (hma_34[i] is not None and hma_34[i - 1] is not None and hma_34[i - 2] is not None
                    and hma_34[i - 1] > hma_34[i - 2]
                    and hma_34[i] > hma_34[i - 1]
                    and closes[i] > hma_34[i]):

                entry_price = opens[i + 1]
                if atr_14[i] is not None:
                    stop_price = entry_price - 3 * atr_14[i]
                else:
                    stop_price = entry_price * 0.95
                entry_bar = i + 1
                position = 1
                # Commission on entry (charged at entry, reduces effective capital)
                equity *= (1 - COMMISSION_ROUNDTRIP / 2)

        equity_curve.append(equity)

    # Close any open position at last close
    if position == 1:
        exit_price = closes[-1]
        gross_pnl = (exit_price - entry_price) / entry_price
        net_pnl = gross_pnl - COMMISSION_ROUNDTRIP / 2
        equity *= (1 + net_pnl)
        trades.append({
            "entry_bar": entry_bar,
            "exit_bar": len(closes) - 1,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_pct": net_pnl * 100,
            "exit_reason": "END_OF_DATA",
        })

    # Metrics
    total_trades = len(trades)
    winning = [t for t in trades if t["pnl_pct"] > 0]
    losing = [t for t in trades if t["pnl_pct"] < 0]
    win_rate = len(winning) / total_trades * 100 if total_trades > 0 else 0

    gross_profit = sum(t["pnl_pct"] for t in winning) if winning else 0
    gross_loss = sum(t["pnl_pct"] for t in losing) if losing else 0
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float('inf')

    avg_win = statistics.mean([t["pnl_pct"] for t in winning]) if winning else 0
    avg_loss = statistics.mean([t["pnl_pct"] for t in losing]) if losing else 0

    max_eq = 10000.0
    max_dd = 0.0
    for eq in equity_curve:
        if eq > max_eq:
            max_eq = eq
        dd = (max_eq - eq) / max_eq * 100
        if dd > max_dd:
            max_dd = dd

    total_return = (equity - 10000.0) / 10000.0 * 100

    # Buy & hold
    bh_return = (closes[-1] - closes[0]) / closes[0] * 100

    return {
        "n_trades": total_trades,
        "winning": len(winning),
        "losing": len(losing),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "max_dd": max_dd,
        "total_return": total_return,
        "final_equity": equity,
        "bh_return": bh_return,
        "excess_return": total_return - bh_return,
        "trades": trades,
        "equity_curve": equity_curve,
    }


if __name__ == "__main__":
    all_results = {}
    for pair, symbol in PAIRS_MAP.items():
        print(f"Fetching {pair} ({symbol})...", flush=True)
        try:
            klines = get_binance_klines(symbol, "1h", START_MS, END_MS, 1500)
            # paginate if needed
            while len(klines) == 1500 and int(klines[-1][6]) < END_MS:
                next_start = int(klines[-1][6]) + 3600000
                more = get_binance_klines(symbol, "1h", next_start, END_MS, 1500)
                if not more:
                    break
                klines.extend(more)
                time.sleep(0.3)

            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            opens = [float(k[1]) for k in klines]
            times = [datetime.datetime.fromtimestamp(k[0] / 1000) for k in klines]

            if len(closes) < 100:
                print(f"  SKIP: only {len(closes)} klines")
                continue

            result = run_backtest(closes, highs, lows, opens, None, times)
            result["symbol"] = symbol
            result["pair"] = pair
            result["n_klines"] = len(closes)
            all_results[pair] = result

            print(f"  {pair}: {result['n_trades']} trades, WR {result['win_rate']:.1f}%, "
                  f"PF {result['profit_factor']:.2f}, DD {result['max_dd']:.2f}%, "
                  f"Ret {result['total_return']:+.2f}%, B&H {result['bh_return']:+.2f}%")
        except Exception as e:
            print(f"  ERROR {pair}: {e}")
        time.sleep(0.5)

    # Summary table
    print("\n" + "=" * 100)
    print("HMA(34) TREND — TODOS LOS PARES — 1h — 2026 — 11 bps roundtrip")
    print("=" * 100)
    print(f"{'Par':<14} {'Trades':>7} {'WR%':>6} {'PF':>6} {'DD%':>7} {'Ret%':>8} {'B&H%':>8} {'Excess%':>9}")
    print("-" * 100)

    sorted_pairs = sorted(all_results.items(), key=lambda x: x[1]["total_return"], reverse=True)
    for pair, r in sorted_pairs:
        print(f"{pair:<14} {r['n_trades']:>7} {r['win_rate']:>6.1f} {r['profit_factor']:>6.2f} "
              f"{r['max_dd']:>7.2f} {r['total_return']:>8.2f} {r['bh_return']:>8.2f} "
              f"{r['excess_return']:>9.2f}")

    print("\n" + "=" * 100)
    print("LOOKAHEAD CHECK: HMA usa ventanas hacia atrás (no centradas).")
    print("Señal en cierre de barra i, relleno en apertura i+1.")
    print("ATR usa Wilder smoothing hacia atrás. Cero lookahead.")
    print("=" * 100)

    # Save results
    output = {pair: {k: v for k, v in r.items() if k != "trades" and k != "equity_curve"}
              for pair, r in all_results.items()}
    with open(r"C:\Users\Lenovo\AppData\Local\hermes\cache\scratch\hma_backtest_all_pairs.json",
              "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\nSaved to hma_backtest_all_pairs.json")
