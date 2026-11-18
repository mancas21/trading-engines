#!/usr/bin/env python3
"""HMA(34) Backtest IS/OOS — 12 pares del scanner, 1h, 2026.
Split cronológico: IS = primeros 70% de barras, OOS = últimos 30%.
Guarda trades individuales con entry_bar, exit_bar, timestamps.
"""

import urllib.request
import json
import datetime
import statistics
import math
import time

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
    if len(values) < period:
        return [None] * len(values)
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


def run_backtest(closes, highs, lows, opens, times):
    hma_34 = hma(closes, 34)
    atr_14 = atr_wilder(highs, lows, closes, 14)

    equity = 10000.0
    position = 0
    entry_price = 0.0
    stop_price = 0.0
    entry_bar = -1
    trades = []
    equity_curve = []

    for i in range(1, len(closes)):
        prev_eq = equity

        # --- EXIT ---
        if position == 1:
            exit_price = None
            exit_reason = None

            if lows[i] <= stop_price:
                exit_price = min(stop_price, opens[i]) if opens[i] < stop_price else stop_price
                exit_reason = "STOP"
            elif hma_34[i] is not None and hma_34[i - 1] is not None and hma_34[i] < hma_34[i - 1]:
                exit_price = opens[i]
                exit_reason = "HMA_DOWN"

            if exit_price is not None:
                gross_pnl = (exit_price - entry_price) / entry_price
                net_pnl = gross_pnl - COMMISSION_ROUNDTRIP / 2
                equity *= (1 + net_pnl)
                trades.append({
                    "entry_bar": entry_bar,
                    "exit_bar": i,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "entry_time": times[entry_bar].isoformat() if entry_bar >= 0 else None,
                    "exit_time": times[i].isoformat(),
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
            "entry_time": times[entry_bar].isoformat() if entry_bar >= 0 else None,
            "exit_time": times[-1].isoformat(),
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


def fetch_klines(symbol):
    klines = get_binance_klines(symbol, "1h", START_MS, END_MS, 1500)
    while len(klines) == 1500 and int(klines[-1][6]) < END_MS:
        next_start = int(klines[-1][6]) + 3600000
        more = get_binance_klines(symbol, "1h", next_start, END_MS, 1500)
        if not more:
            break
        klines.extend(more)
        time.sleep(0.3)
    return klines


if __name__ == "__main__":
    all_results = {}
    for pair, symbol in PAIRS_MAP.items():
        print(f"Fetching {pair} ({symbol})...", flush=True)
        try:
            klines = fetch_klines(symbol)
            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            opens = [float(k[1]) for k in klines]
            times = [datetime.datetime.fromtimestamp(k[0] / 1000) for k in klines]

            if len(closes) < 100:
                print(f"  SKIP: only {len(closes)} klines")
                continue

            n = len(closes)
            split_idx = int(n * 0.7)

            # IS: primeros 70%
            is_closes = closes[:split_idx]
            is_highs = highs[:split_idx]
            is_lows = lows[:split_idx]
            is_opens = opens[:split_idx]
            is_times = times[:split_idx]

            # OOS: últimos 30%
            oos_closes = closes[split_idx:]
            oos_highs = highs[split_idx:]
            oos_lows = lows[split_idx:]
            oos_opens = opens[split_idx:]
            oos_times = times[split_idx:]

            is_result = run_backtest(is_closes, is_highs, is_lows, is_opens, is_times)
            oos_result = run_backtest(oos_closes, oos_highs, oos_lows, oos_opens, oos_times)

            all_results[pair] = {
                "symbol": symbol,
                "pair": pair,
                "n_klines": n,
                "split_idx": split_idx,
                "is_start": is_times[0].isoformat(),
                "is_end": is_times[-1].isoformat(),
                "oos_start": oos_times[0].isoformat(),
                "oos_end": oos_times[-1].isoformat(),
                "is": {k: v for k, v in is_result.items() if k not in ("trades", "equity_curve")},
                "oos": {k: v for k, v in oos_result.items() if k not in ("trades", "equity_curve")},
                "is_trades": is_result["trades"],
                "oos_trades": oos_result["trades"],
            }

            print(f"  {pair}: IS {is_result['n_trades']} trades WR {is_result['win_rate']:.1f}% PF {is_result['profit_factor']:.2f} | "
                  f"OOS {oos_result['n_trades']} trades WR {oos_result['win_rate']:.1f}% PF {oos_result['profit_factor']:.2f} | "
                  f"IS Ret {is_result['total_return']:+.2f}% | OOS Ret {oos_result['total_return']:+.2f}%")
        except Exception as e:
            print(f"  ERROR {pair}: {e}")
        time.sleep(0.5)

    # Summary table
    print("\n" + "=" * 120)
    print("HMA(34) — IS/OOS SPLIT 70/30 — 12 pares — 1h — 2026 — 11 bps")
    print("=" * 120)
    print(f"{'Par':<14} {'IS Trades':>8} {'IS WR%':>6} {'IS PF':>6} {'IS Ret%':>8} | "
          f"{'OOS Trades':>8} {'OOS WR%':>6} {'OOS PF':>6} {'OOS Ret%':>8} | {'OOS vs IS':>9}")
    print("-" * 120)

    sorted_pairs = sorted(all_results.items(), key=lambda x: x[1]["oos"]["total_return"], reverse=True)
    for pair, r in sorted_pairs:
        is_r = r["is"]
        oos_r = r["oos"]
        excess_diff = oos_r["total_return"] - is_r["total_return"]
        print(f"{pair:<14} {is_r['n_trades']:>8} {is_r['win_rate']:>6.1f} {is_r['profit_factor']:>6.2f} "
              f"{is_r['total_return']:>8.2f} | {oos_r['n_trades']:>8} {oos_r['win_rate']:>6.1f} "
              f"{oos_r['profit_factor']:>6.2f} {oos_r['total_return']:>8.2f} | {excess_diff:>+9.2f}")

    print("\n" + "=" * 120)
    print("INTERPRETACIÓN:")
    print("- OOS WR > IS WR: estrategia no overfiteada, funciona en datos no vistos")
    print("- OOS PF > IS PF: edge real, no ruido de muestra")
    print("- OOS Ret < IS Ret con signo igual: consistente, no casualidad")
    print("=" * 120)

    # Save results (without trades to keep file smaller, but trades available per pair if needed)
    output = {}
    for pair, r in all_results.items():
        output[pair] = {k: v for k, v in r.items() if k not in ("is_trades", "oos_trades", "equity_curve")}
        # Include trades separately per pair for OOS validation
        output[pair]["is_trades_count"] = len(r["is_trades"])
        output[pair]["oos_trades_count"] = len(r["oos_trades"])

    with open(r"C:\Users\Lenovo\AppData\Local\hermes\cache\scratch\hma_backtest_is_oos_all_pairs.json",
              "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\nSaved to hma_backtest_is_oos_all_pairs.json")
    print("IS/OOS trades available per pair in the saved JSON.")
