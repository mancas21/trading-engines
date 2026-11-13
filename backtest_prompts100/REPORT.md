# Criba rápida — 100 prompts de estrategia (Binance USDT-M perps)

**Fecha:** 2026-10-29 · **Motor:** `backtest_prompts100/` (trading-engines) · **Datos:** backtest-system (read-only) + rellenos JUP 1h/15m y cola HYPE 1h por API fapi

## Convenciones

- Señal en barra cerrada, fill en la apertura siguiente; el stop gana empates.
- Riesgo 1% de equity por trade desde la distancia del stop (ATR); compuesto.
- Costes: **11 bps roundtrip** (modelo del repo: 5 fee + 0.5 slip por lado) y **22 bps** como sensibilidad.
- Split **70/30 IS/OOS** por nº de barras. Datos: 1h desde listing (BTC 2019, SOL 2020, SUI 2023, HYPE 2025-05, JUP 2024-01), 15m/5m desde 2019-2024 según par, hasta 2026-10-15/27.
- Gates del repo aplicados como criba: PF ex-cost > 1.2 · n ≥ 30 · net > 0 · exceso vs buy & hold reportado.

## Veredicto global

**Coste oficial del repo: 11 bps roundtrip** (5 bps fee + 0.5 bps slip por lado, medido en libro vivo 2026-10-25; 22 bps es solo el preset viejo, se reporta como sensibilidad y NO decide el veredicto).

De **100 estrategias**, con el coste oficial de 11 bps: **14 pasan el gate completo** (PF>1.2, n≥30, net>0 en ventana completa) y **5 lo mantienen en OOS** (PF>1.2, n≥30, net>0): [28, 44, 60, 71, 73]. De esos, 4 también sobreviven a la sensibilidad de 22 bps: [44, 60, 71, 73].

Mediana de OOS PF por grupo: mean_reversion 0.65 (8% con OOS positivo), pairs 0.18, funding 0.53, breakout 0.58, trend 0.69, session 0.78, cross_sectional 1.22. **La reversión a la media a estos timeframes y con estos stops está muerta** — consistente con el veredicto v3 del repo (edge bruto cero, coste-motivado).

## Estrategias que sobreviven (gate completo + OOS, coste oficial 11 bps)

| # | Estrategia | Par | TF | Net | PF | n | DD | OOS PF | OOS n | OOS net | Exceso OOS vs B&H | ¿Sobrevive a 22 bps? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 28 | Donchian breakout | BTCUSDT | 1h | 113.7% | 1.37 | 430 | -21.4% | 1.30 | 138 | 19.2% | -24.6pp | no |
| 44 | 168-bar breakout | BTCUSDT | 1h | 368.0% | 2.54 | 161 | -21.4% | 1.52 | 52 | 18.7% | -25.1pp | sí |
| 60 | Keltner con RSI | HYPEUSDT | 1h | 40.3% | 1.60 | 128 | -9.9% | 1.61 | 42 | 12.2% | -40.5pp | sí |
| 71 | Hour-of-day filter | JUPUSDT | 1h | 15.8% | 1.26 | 97 | -15.3% | 1.38 | 30 | 12.5% | -15.3pp | sí |
| 73 | Sunday-to-Monday reversion | ETHUSDT | 1h | 37.5% | 1.45 | 128 | -20.1% | 1.45 | 42 | 10.0% | +22.1pp | sí |

Cerca pero sin gate OOS (por n o PF): **83 Weekly basket rotation** (OOS PF 1.56, n=22<30), **95 Price-funding divergence SUI** (OOS PF 1.17, exceso +65pp), **26 EMA cross ETH** (OOS PF 1.09, exceso +28pp, PF decae a 1.00 a 22 bps).

## Advertencias

- Criba con convenciones homogéneas, **no** artefactos del harness `vrun.py` (sin walk-forward, sin period gates, sin neighbourhood). Un superviviente aquí es **candidato a validación formal** en backtest-system, no un resultado validado.
- #91 Funding extreme fade pasa el gate completo pero su OOS tiene 1 trade: sin evidencia fuera de muestra.
- #85 (Kalman hedge) quedó **invalidada durante la corrida**: el Kalman escalar se congelaba y un bug de signo del coste en spreads con precio negativo inflaba el resultado (+9342% falso). Con beta OLS rodante es −81%. Los 4 bugs encontrados y corregidos: signo del coste en el motor de pares, lookahead en el motor de flips (posición cobraba el retorno de su propia barra), churn de re-entrada en rupturas diarias (47/55) y bloque 4h sin cerrar en #98.
- Los TF de 5m/15m están sistemáticamente por debajo de 1h: el coste de 11-22 bps come cualquier edge bruto en 23 de 25 mean-reversion.
- JUP 1h/15m vienen de la API (listing 2024-01); HYPE 1h completada hasta hoy. XS/pares usan la intersección de historiales (HYPE limita a 2025-05).

## Tabla completa (11 bps, ordenada por OOS PF)

| # | Estrategia | Par | TF | Grupo | Net | PF | n | DD | WR | Sharpe | OOS net | OOS PF | OOS n | OOS DD | Gate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 91 | Funding extreme fade | BTCUSDT | 1h | funding | 30.3% | 1.26 | 398 | -10.7% | 49.5% | 0.62 | 0.6% | inf | 1 | -0.5% | ✅ |
| 78 | Session VWAP band | BTCUSDT | 5m | session | 4.7% | 1.39 | 26 | -59.1% | 3.8% | 0.29 | 26.3% | 9.75 | 5 | -22.5% |  |
| 35 | 480-bar breakout | HYPEUSDT | 1h | trend | 8.2% | 1.70 | 20 | -13.7% | 35.0% | 0.44 | 13.8% | 8.77 | 4 | -7.8% |  |
| 49 | ATR expansion | SUIUSDT | 1h | breakout | 1.8% | 1.14 | 31 | -9.6% | 32.3% | 0.15 | 5.9% | 2.35 | 14 | -2.7% |  |
| 16 | RSI divergence | HYPEUSDT | 5m | mean_reversion | -0.6% | 0.87 | 4 | -3.8% | 25.0% | -0.14 | 1.7% | 2.30 | 2 | -1.4% |  |
| 90 | Volatility-scaled momentum | 6 perps | 1h | cross_sectional | 11.6% | 1.17 | 74 | -30.9% | 59.5% | 0.40 | 16.5% | 1.68 | 22 | -13.3% |  |
| 60 | Keltner con RSI | HYPEUSDT | 1h | breakout | 40.3% | 1.60 | 128 | -9.9% | 36.7% | 1.52 | 12.2% | 1.61 | 42 | -8.4% | ✅ |
| 83 | Weekly basket rotation | 6 perps | 1h | cross_sectional | 27.3% | 1.28 | 74 | -31.8% | 40.5% | 0.66 | 15.0% | 1.56 | 22 | -21.4% | ✅ |
| 44 | 168-bar breakout | BTCUSDT | 1h | trend | 368.0% | 2.54 | 161 | -21.4% | 28.0% | 1.29 | 18.7% | 1.52 | 52 | -21.4% | ✅ |
| 21 | Trend-filtered RSI | SOLUSDT | 1h | mean_reversion | 0.6% | 1.04 | 37 | -9.0% | 29.7% | 0.04 | 4.3% | 1.47 | 14 | -7.1% |  |
| 73 | Sunday-to-Monday reversion | ETHUSDT | 1h | session | 37.5% | 1.45 | 128 | -20.1% | 40.6% | 0.65 | 10.0% | 1.45 | 42 | -9.8% | ✅ |
| 71 | Hour-of-day filter | JUPUSDT | 1h | session | 15.8% | 1.26 | 97 | -15.3% | 22.7% | 0.44 | 12.5% | 1.38 | 30 | -12.5% | ✅ |
| 28 | Donchian breakout | BTCUSDT | 1h | trend | 113.7% | 1.37 | 430 | -21.4% | 33.3% | 0.79 | 19.2% | 1.30 | 138 | -21.4% | ✅ |
| 56 | Donchian chandelier | JUPUSDT | 1h | breakout | -1.6% | 1.01 | 299 | -17.0% | 34.8% | 0.02 | 11.7% | 1.27 | 89 | -10.1% |  |
| 75 | Month-end drift | HYPEUSDT | 1h | session | -12.5% | 0.63 | 54 | -16.3% | 31.5% | -1.08 | 1.6% | 1.20 | 14 | -7.8% |  |
| 95 | Price-funding divergence | SUIUSDT | 1h | funding | 21.0% | 1.25 | 153 | -12.0% | 43.1% | 0.55 | 1.4% | 1.17 | 36 | -5.0% | ✅ |
| 26 | EMA cross | ETHUSDT | 1h | trend | 444.7% | 1.36 | 1134 | -29.1% | 27.1% | 1.07 | 15.6% | 1.09 | 385 | -24.8% | ✅ |
| 34 | Regression slope | SOLUSDT | 1h | trend | 355.0% | 1.26 | 1096 | -26.5% | 29.3% | 0.95 | 4.8% | 1.06 | 332 | -26.4% | ✅ |
| 59 | Range expansion | SOLUSDT | 1h | breakout | 40.9% | 1.25 | 533 | -17.3% | 39.4% | 0.69 | 0.9% | 1.03 | 155 | -8.3% | ✅ |
| 50 | Volatility breakout | JUPUSDT | 15m | breakout | 71.2% | 1.08 | 3774 | -95.2% | 14.7% | 0.80 | -64.2% | 0.97 | 1150 | -93.2% |  |
| 37 | Parabolic SAR | JUPUSDT | 1h | trend | -51.1% | 0.94 | 2113 | -62.4% | 36.5% | -0.67 | -13.3% | 0.96 | 637 | -24.9% |  |
| 69 | US cash open fade | HYPEUSDT | 15m | session | -8.0% | 0.95 | 185 | -28.3% | 38.4% | -0.28 | -2.3% | 0.96 | 61 | -11.5% |  |
| 22 | Band fade con time stop | HYPEUSDT | 1h | mean_reversion | -11.2% | 0.91 | 336 | -21.9% | 53.6% | -0.57 | -2.4% | 0.95 | 103 | -13.2% |  |
| 68 | Weekend effect | SOLUSDT | 1h | session | 50.0% | 1.26 | 319 | -22.1% | 42.3% | 0.61 | -4.3% | 0.94 | 96 | -18.6% | ✅ |
| 4 | Stochastic cross | HYPEUSDT | 1h | mean_reversion | -18.4% | 0.90 | 379 | -29.3% | 52.5% | -0.77 | -4.6% | 0.93 | 116 | -12.7% |  |
| 11 | EMA deviation | SUIUSDT | 1h | mean_reversion | -19.8% | 0.80 | 276 | -25.5% | 56.2% | -0.81 | -1.5% | 0.93 | 87 | -10.2% |  |
| 92 | Funding z-score | ETHUSDT | 1h | funding | -36.7% | 0.86 | 597 | -54.0% | 41.2% | -0.54 | -7.2% | 0.93 | 189 | -18.5% |  |
| 45 | Time-series momentum | ETHUSDT | 1h | trend | 6.4% | 1.06 | 467 | -47.7% | 37.3% | 0.19 | -32.3% | 0.89 | 143 | -42.6% |  |
| 84 | 24-bar relative strength | 6 perps | 1h | cross_sectional | -58.4% | 0.79 | 516 | -62.3% | 46.1% | -1.54 | -13.4% | 0.88 | 155 | -28.1% |  |
| 10 | Down-streak bounce | HYPEUSDT | 15m | mean_reversion | -55.4% | 0.74 | 959 | -56.7% | 60.9% | -2.95 | -12.2% | 0.85 | 279 | -17.7% |  |
| 43 | HTF trend filter | JUPUSDT | 5m | trend | -99.9% | 0.75 | 2721 | -100.0% | 20.0% | -2.55 | -85.5% | 0.84 | 808 | -90.6% |  |
| 29 | MACD zero-cross | HYPEUSDT | 15m | trend | -52.1% | 0.85 | 981 | -57.8% | 30.3% | -1.50 | -24.0% | 0.82 | 297 | -34.3% |  |
| 24 | Failed breakdown reclaim | JUPUSDT | 1h | mean_reversion | -70.3% | 0.82 | 1054 | -72.1% | 41.7% | -1.67 | -30.5% | 0.81 | 302 | -34.7% |  |
| 30 | ADX DI cross | SUIUSDT | 1h | trend | -19.6% | 0.85 | 476 | -30.9% | 27.5% | -0.51 | -9.4% | 0.80 | 126 | -22.6% |  |
| 72 | Monday persistence | BTCUSDT | 1h | session | 17.9% | 1.09 | 372 | -27.3% | 38.2% | 0.27 | -15.2% | 0.79 | 112 | -27.3% |  |
| 74 | NY close momentum | SOLUSDT | 15m | session | -93.0% | 0.76 | 2225 | -94.0% | 39.7% | -1.93 | -52.2% | 0.78 | 665 | -55.0% |  |
| 70 | Funding-window fade | SUIUSDT | 15m | session | -95.5% | 0.65 | 1461 | -95.9% | 38.9% | -3.52 | -46.4% | 0.75 | 435 | -47.7% |  |
| 7 | Connors RSI(2) | BTCUSDT | 1h | mean_reversion | -73.0% | 0.79 | 1313 | -74.7% | 54.5% | -1.35 | -38.4% | 0.74 | 397 | -47.0% |  |
| 53 | Volume breakout | SOLUSDT | 5m | breakout | -99.4% | 0.67 | 1374 | -99.5% | 16.7% | -3.09 | -77.9% | 0.74 | 424 | -79.5% |  |
| 57 | Volatility-percentile breakout | ETHUSDT | 15m | breakout | -67.2% | 0.89 | 655 | -88.5% | 17.6% | -0.41 | -48.6% | 0.71 | 187 | -59.7% |  |
| 1 | RSI mean reversion | SOLUSDT | 1h | mean_reversion | -87.1% | 0.68 | 746 | -89.3% | 20.5% | -1.74 | -47.5% | 0.70 | 247 | -55.7% |  |
| 17 | Ultimate Oscillator | SUIUSDT | 5m | mean_reversion | -98.9% | 0.71 | 1409 | -99.1% | 15.8% | -3.20 | -71.5% | 0.67 | 322 | -78.5% |  |
| 12 | Donchian fade | JUPUSDT | 15m | mean_reversion | -100.0% | 0.65 | 5403 | -100.0% | 42.8% | -8.31 | -96.6% | 0.66 | 1570 | -96.7% |  |
| 97 | Funding-filtered reversion | BTCUSDT | 1h | funding | -13.6% | 0.93 | 272 | -30.6% | 37.1% | -0.20 | -24.6% | 0.65 | 100 | -24.6% |  |
| 23 | Connors RSI(3,2,100) | SUIUSDT | 15m | mean_reversion | -93.4% | 0.74 | 2531 | -93.8% | 57.1% | -3.34 | -69.9% | 0.65 | 739 | -70.5% |  |
| 18 | MFI reversal | JUPUSDT | 5m | mean_reversion | -100.0% | 0.64 | 4628 | -100.0% | 24.7% | -8.88 | -99.1% | 0.65 | 1446 | -99.2% |  |
| 62 | Volume spike breakout | JUPUSDT | 5m | breakout | -98.2% | 0.74 | 1827 | -98.6% | 35.5% | -4.41 | -84.9% | 0.65 | 622 | -85.3% |  |
| 47 | Opening range breakout | BTCUSDT | 5m | breakout | -98.7% | 0.73 | 1216 | -99.0% | 18.2% | -2.55 | -82.7% | 0.64 | 369 | -85.6% |  |
| 5 | Williams %R | SUIUSDT | 15m | mean_reversion | -100.0% | 0.64 | 5352 | -100.0% | 40.6% | -7.64 | -97.5% | 0.64 | 1517 | -97.8% |  |
| 15 | Bollinger %B | SOLUSDT | 15m | mean_reversion | -100.0% | 0.67 | 7768 | -100.0% | 33.5% | -6.35 | -99.9% | 0.62 | 2317 | -99.9% |  |
| 87 | Low-volatility rotation | 6 perps | 1h | cross_sectional | -38.9% | 0.71 | 74 | -56.3% | 48.6% | -0.85 | -14.3% | 0.62 | 22 | -25.0% |  |
| 25 | VWAP band | BTCUSDT | 1h | mean_reversion | -99.1% | 0.55 | 1606 | -99.1% | 39.4% | -3.80 | -73.1% | 0.60 | 496 | -73.9% |  |
| 54 | Failed breakout fade | HYPEUSDT | 5m | breakout | -100.0% | 0.65 | 3748 | -100.0% | 39.4% | -9.33 | -96.8% | 0.60 | 1126 | -97.2% |  |
| 98 | Funding vs 4h RSI | ETHUSDT | 1h | funding | -62.9% | 0.58 | 310 | -64.9% | 27.1% | -1.42 | -23.7% | 0.59 | 87 | -28.4% |  |
| 42 | KAMA slope | SUIUSDT | 15m | trend | -100.0% | 0.65 | 13054 | -100.0% | 22.0% | -8.59 | -99.7% | 0.57 | 3945 | -99.7% |  |
| 65 | Stop-hunt reclaim | SOLUSDT | 15m | breakout | -100.0% | 0.65 | 6144 | -100.0% | 46.9% | -5.48 | -99.6% | 0.56 | 1812 | -99.7% |  |
| 14 | 50-EMA stretch | ETHUSDT | 1h | mean_reversion | -95.1% | 0.60 | 1074 | -95.5% | 33.7% | -2.73 | -62.7% | 0.56 | 302 | -64.7% |  |
| 6 | CCI reversal | JUPUSDT | 1h | mean_reversion | -54.6% | 0.58 | 381 | -55.2% | 47.0% | -2.75 | -21.3% | 0.54 | 109 | -22.2% |  |
| 80 | Opening-bar momentum | SOLUSDT | 5m | session | -94.1% | 0.51 | 718 | -94.2% | 30.9% | -4.80 | -55.5% | 0.54 | 213 | -60.9% |  |
| 46 | Bollinger squeeze | SOLUSDT | 15m | breakout | -54.6% | 0.87 | 762 | -71.8% | 27.6% | -0.51 | -53.9% | 0.53 | 210 | -56.7% |  |
| 20 | Multi-timeframe RSI | ETHUSDT | 5m | mean_reversion | -99.9% | 0.51 | 1576 | -100.0% | 22.5% | -8.03 | -87.9% | 0.53 | 442 | -88.3% |  |
| 2 | Bollinger + RSI | BTCUSDT | 15m | mean_reversion | -100.0% | 0.54 | 5805 | -100.0% | 37.2% | -7.68 | -99.9% | 0.53 | 1743 | -99.9% |  |
| 8 | Keltner fade | ETHUSDT | 15m | mean_reversion | -100.0% | 0.52 | 9486 | -100.0% | 37.0% | -10.11 | -100.0% | 0.52 | 2988 | -100.0% |  |
| 63 | Bollinger breakout con ADX | BTCUSDT | 15m | breakout | -100.0% | 0.59 | 9850 | -100.0% | 28.8% | -6.85 | -100.0% | 0.52 | 2934 | -100.0% |  |
| 39 | Aroon | ETHUSDT | 15m | trend | -96.7% | 0.51 | 1910 | -96.8% | 39.2% | -3.77 | -64.4% | 0.51 | 577 | -65.3% |  |
| 89 | ETH/BTC ratio breakout | ETH/BTC | 15m | pairs | -87.4% | 0.60 | 800 | -88.1% | 23.6% | -2.18 | -58.8% | 0.49 | 243 | -60.2% |  |
| 9 | Fast RSI | SOLUSDT | 5m | mean_reversion | -100.0% | 0.52 | 4305 | -100.0% | 30.6% | -12.92 | -99.7% | 0.49 | 1253 | -99.8% |  |
| 40 | VWMA cross | SOLUSDT | 5m | trend | -100.0% | 0.59 | 11003 | -100.0% | 26.5% | -12.04 | -100.0% | 0.49 | 3429 | -100.0% |  |
| 100 | Funding harvest | HYPEUSDT | 15m | funding | -20.1% | 0.48 | 66 | -24.0% | 30.3% | -2.14 | -5.6% | 0.48 | 18 | -6.0% |  |
| 36 | EMA(9/21) con ADX | SUIUSDT | 5m | trend | -100.0% | 0.72 | 3733 | -100.0% | 22.4% | -4.10 | -99.5% | 0.47 | 1194 | -99.6% |  |
| 13 | Volume-confirmed RSI | BTCUSDT | 5m | mean_reversion | -99.9% | 0.50 | 1249 | -99.9% | 27.1% | -7.61 | -90.6% | 0.46 | 384 | -91.0% |  |
| 33 | Ichimoku | BTCUSDT | 15m | trend | -100.0% | 0.52 | 13854 | -100.0% | 20.6% | -7.60 | -100.0% | 0.45 | 4114 | -100.0% |  |
| 3 | VWAP z-score | ETHUSDT | 5m | mean_reversion | -100.0% | 0.43 | 3719 | -100.0% | 25.8% | -14.43 | -99.4% | 0.44 | 997 | -99.5% |  |
| 51 | TTM squeeze | ETHUSDT | 5m | breakout | -100.0% | 0.49 | 4846 | -100.0% | 20.4% | -11.27 | -100.0% | 0.43 | 1450 | -100.0% |  |
| 66 | London open range | BTCUSDT | 15m | session | -100.0% | 0.42 | 5098 | -100.0% | 22.5% | -7.82 | -100.0% | 0.42 | 1481 | -100.0% |  |
| 55 | Pivot breakout | SUIUSDT | 15m | breakout | -100.0% | 0.42 | 978 | -100.0% | 11.5% | -2.98 | -95.8% | 0.42 | 288 | -95.8% |  |
| 79 | Impulse fade | ETHUSDT | 5m | session | -93.9% | 0.46 | 1190 | -94.0% | 36.2% | -6.57 | -66.0% | 0.40 | 396 | -66.7% |  |
| 61 | Opening drive | SUIUSDT | 5m | breakout | -96.1% | 0.50 | 710 | -96.2% | 28.9% | -4.44 | -73.7% | 0.37 | 209 | -74.2% |  |
| 31 | Triple EMA pullback | JUPUSDT | 15m | trend | -100.0% | 0.47 | 2634 | -100.0% | 14.9% | -4.48 | -99.9% | 0.37 | 750 | -100.0% |  |
| 38 | Keltner breakout | BTCUSDT | 5m | trend | -100.0% | 0.39 | 2892 | -100.0% | 18.7% | -11.74 | -100.0% | 0.37 | 903 | -100.0% |  |
| 41 | Efficiency ratio filter | HYPEUSDT | 5m | trend | -98.3% | 0.36 | 1255 | -98.3% | 22.9% | -9.15 | -69.4% | 0.36 | 373 | -71.5% |  |
| 93 | Negative-funding long | SOLUSDT | 1h | funding | 18.4% | 1.23 | 148 | -41.1% | 10.8% | 0.24 | -21.2% | 0.36 | 33 | -41.1% | ✅ |
| 32 | HMA slope | ETHUSDT | 5m | trend | -100.0% | 0.36 | 13137 | -100.0% | 20.0% | -26.42 | -100.0% | 0.32 | 3932 | -100.0% |  |
| 19 | Range fade | BTCUSDT | 15m | mean_reversion | -100.0% | 0.25 | 77949 | -100.0% | 31.6% | -42.53 | -100.0% | 0.23 | 23005 | -100.0% |  |
| 85 | HYPE/SOL Kalman hedge | HYPE/SOL | 1h | pairs | -81.4% | 0.12 | 247 | -81.4% | 19.4% | -25.77 | -36.1% | 0.15 | 70 | -36.1% |  |
| 81 | SOL/ETH z-score | SOL/ETH | 1h | pairs | -100.0% | 0.19 | 1820 | -100.0% | 20.1% | -10.70 | -99.7% | 0.13 | 652 | -99.7% |  |
| 82 | BTC/ETH z-score | BTC/ETH | 1h | pairs | -100.0% | 0.07 | 3558 | -100.0% | 9.8% | -17.84 | -100.0% | 0.10 | 981 | -100.0% |  |
| 86 | SUI/JUP cointegration | SUI/JUP | 1h | pairs | -74.8% | 0.35 | 366 | -75.6% | 38.0% | -6.49 | -50.8% | 0.09 | 102 | -51.2% |  |
| 27 | Supertrend | SOLUSDT | 15m | trend | 0.0% | 0.00 | 0 | 0.0% | 0.0% | 0.00 | 0.0% | 0.00 | 0 | 0.0% |  |
| 48 | NR7 breakout | HYPEUSDT | 15m | breakout | -31.7% | 0.79 | 83 | -68.5% | 1.2% | 0.09 | 32.0% | 0.00 | 0 | -31.4% |  |
| 58 | Gap continuation | BTCUSDT | 1h | breakout | -0.5% | 0.00 | 1 | -0.7% | 0.0% | -0.18 | 0.0% | 0.00 | 0 | 0.0% |  |
| 52 | Range compression | BTCUSDT | 15m | breakout | 0.0% | 0.00 | 0 | 0.0% | 0.0% | 0.00 | 0.0% | 0.00 | 0 | 0.0% |  |
| 67 | Asian range fade | ETHUSDT | 15m | session | -36.9% | 0.15 | 22 | -41.7% | 4.5% | -0.51 | -1.1% | 0.00 | 0 | -10.6% |  |
| 64 | Consolidation breakout | ETHUSDT | 1h | breakout | 0.0% | 0.00 | 0 | 0.0% | 0.0% | 0.00 | 0.0% | 0.00 | 0 | 0.0% |  |
| 77 | Asia gap fade | JUPUSDT | 15m | session | 0.0% | 0.00 | 0 | 0.0% | 0.0% | 0.00 | 0.0% | 0.00 | 0 | 0.0% |  |
| 76 | Pre-US compression | SUIUSDT | 5m | session | 0.0% | 0.00 | 0 | 0.0% | 0.0% | 0.00 | 0.0% | 0.00 | 0 | 0.0% |  |
| 88 | Alt basket vs BTC | basket/BTC | 1h | pairs | 0.0% | 0.00 | 0 | 0.0% | 0.0% | 0.00 | 0.0% | 0.00 | 0 | 0.0% |  |
| 94 | Funding persistence | HYPEUSDT | 1h | funding | -2.1% | 0.19 | 6 | -2.4% | 33.3% | -0.88 | 0.0% | 0.00 | 0 | 0.0% |  |
| 96 | Funding con trend filter | JUPUSDT | 1h | funding | 0.0% | 0.00 | 0 | 0.0% | 0.0% | 0.00 | 0.0% | 0.00 | 0 | 0.0% |  |
| 99 | Funding momentum | SOLUSDT | 1h | funding | 235.5% | 5.89 | 38 | -29.1% | 13.2% | 0.90 | -2.1% | 0.00 | 2 | -3.8% | ✅ |

Charts: `charts/top_survivors.png`, `charts/oos_map.png`. Datos crudos: `results_100.csv` (11 y 22 bps).