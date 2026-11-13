# 50 Crypto Scalping and Day-Trading Backtest Prompts

Copy any numbered prompt into your backtester or strategy assistant.

## Pair and backtest rules

- The requested crypto symbols are BTC, ETH, SOL, HYPE, SUI, JUP, and SPCX. Prompts use their USDT-quoted forms (for example, `BTCUSDT`). Verify that the selected venue actually lists each exact market. If a market is unavailable, report that instead of silently substituting another asset. `SPCX` is kept exactly as supplied and may need ticker clarification.
- NVDA is a stock, not a cryptocurrency, so it is excluded from this crypto-only list. Use it only in a separate equity strategy request, or provide the exact crypto-native tokenized NVDA market if that is what you meant.
- Unless a prompt names one pair, run it on each supported pair among `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `HYPEUSDT`, `SUIUSDT`, `JUPUSDT`, and `SPCXUSDT`. Use 1x notional for perpetuals; include trading fees, slippage, and funding where applicable. If shorting or a required data feed is unavailable, report the limitation.
- In every backtest, use the full available history for the chosen pair and timeframe, avoid lookahead, and execute signals no earlier than the next bar. Do not use a trailing stop. Report net return after costs, maximum drawdown, profit factor, trade count, and buy-and-hold return over the same dates. State the date range and market/data limitations.

## Scalping (1–15 minute charts)

1. **Supertrend direction scalp** — Build a trend-following strategy on `BTCUSDT` 5-minute candles. Use Supertrend (10, 3) for direction; enter on a candle close that changes direction and exit on the opposite signal or a 1.5 ATR stop / 2 ATR take profit, whichever occurs first. Apply the pair and backtest rules above.

2. **VWAP reclaim** — Build a `ETHUSDT` 5-minute scalp. Go long when price closes back above session VWAP after trading below it, with RSI(14) above 50; go short on the reverse with RSI below 50. Set a 1 ATR stop and 1.5 ATR target. Apply the pair and backtest rules above.

3. **UTC opening-range breakout** — Build a `SOLUSDT` 5-minute strategy using the first 30 minutes after 00:00 UTC as the range. Enter on a close outside the range with above-average volume; stop at the opposite range edge and target 1.5 times the risk. Close any open trade at 23:55 UTC. Apply the pair and backtest rules above.

4. **Fast EMA pullback** — Build a `HYPEUSDT` 3-minute trend scalp. Trade only when EMA(9) is above EMA(21) for longs or below for shorts; enter on a pullback to EMA(9) followed by a close back in trend direction. Use a 1 ATR stop and 1.5 ATR target. Apply the pair and backtest rules above.

5. **Micro Donchian breakout** — Build a `SUIUSDT` 5-minute scalp. Enter long on a close above the prior 20-bar high or short below the prior 20-bar low, only when volume exceeds its 20-bar average. Use a 1 ATR stop and 2 ATR target. Apply the pair and backtest rules above.

6. **Bollinger mean reversion** — Build a `JUPUSDT` 5-minute range scalp. Enter long when price closes below the lower 20-period, 2-standard-deviation Bollinger Band and RSI(2) is below 10; exit at the middle band. Reverse the rules for shorts. Use a 1.25 ATR protective stop. Apply the pair and backtest rules above.

7. **RSI divergence reversal** — Build a `BTCUSDT` 5-minute scalp using confirmed swing pivots and RSI(14). Enter on bullish price/RSI divergence followed by a close above the previous candle high, or bearish divergence followed by a close below the previous low. Use a 1 ATR stop and 1.5 ATR target. Apply the pair and backtest rules above.

8. **Stochastic pullback in trend** — Build an `ETHUSDT` 3-minute scalp. Use EMA(50) to define trend; in an uptrend enter when Stochastic RSI crosses up from below 20, and in a downtrend enter when it crosses down from above 80. Use a 1 ATR stop and 1.5 ATR target. Apply the pair and backtest rules above.

9. **Keltner squeeze breakout** — Build a `SOLUSDT` 5-minute volatility scalp. Identify a squeeze when Bollinger Bands fit inside Keltner Channels for at least 5 bars, then enter on the close beyond the Keltner Channel in the direction of above-average volume. Use a 1.25 ATR stop and 2 ATR target. Apply the pair and backtest rules above.

10. **Bollinger band trend ride** — Build a `HYPEUSDT` 5-minute momentum scalp. Enter long after two consecutive closes above the upper 20-period Bollinger Band while EMA(20) rises; enter short on the mirrored condition. Exit on a close back inside the bands or at a 1.5 ATR stop. Apply the pair and backtest rules above.

11. **VWAP deviation fade** — Build a `SUIUSDT` 1-minute mean-reversion scalp. When price is at least 2.5 standard deviations from session VWAP, enter toward VWAP only if the next candle closes back toward it. Stop 1 ATR beyond the signal extreme and take profit at VWAP. Apply the pair and backtest rules above.

12. **Order-book imbalance** — Build a `BTCUSDT` 1-minute scalp using historical level-2 data. Enter long when top-five-level bid depth is at least 1.5 times ask depth and price confirms with a higher close; reverse for shorts. Exit after 5 bars or at a 0.5 ATR stop / 0.75 ATR target. If historical depth data is unavailable, report that no valid backtest can be run. Apply the pair and backtest rules above.

13. **Liquidity-sweep reversal** — Build an `ETHUSDT` 5-minute scalp. Mark the prior 12-bar high and low; enter short when price wicks above the high but closes back below it, or long when it sweeps the low and closes back above. Stop beyond the sweep wick and target the range midpoint or 1.5R. Apply the pair and backtest rules above.

14. **Intraday pivot rejection** — Build a `SOLUSDT` 5-minute scalp using daily pivot levels calculated from the previous UTC day. Enter on a confirmed rejection candle at S1/R1 or the central pivot, with RSI confirmation. Stop 1 ATR beyond the level and target the next pivot. Apply the pair and backtest rules above.

15. **MACD zero-line scalp** — Build a `HYPEUSDT` 5-minute momentum strategy. Enter long when MACD(8, 21, 5) crosses above zero with rising volume, or short when it crosses below zero. Exit on the opposite cross or a 1 ATR stop / 1.5 ATR target. Apply the pair and backtest rules above.

16. **ADX directional burst** — Build a `SUIUSDT` 3-minute scalp. Enter long when +DI crosses above -DI and ADX(14) rises through 20; reverse for shorts. Use a 1 ATR stop and 1.75 ATR target, and exit after 12 bars if neither is reached. Apply the pair and backtest rules above.

17. **Tenkan/Kijun cross** — Build a `JUPUSDT` 5-minute scalp using Ichimoku (9, 26, 52). Enter when Tenkan crosses Kijun in the direction of price relative to the cloud; close on an opposite cross or use a 1 ATR stop and 1.5 ATR target. Apply the pair and backtest rules above.

18. **Fair-value-gap retrace** — Build a `BTCUSDT` 5-minute scalp with an objective three-candle fair-value-gap definition. After an impulsive move, enter on the first retrace into the gap only if price closes back in the impulse direction. Stop beyond the gap and target 1.5R. Apply the pair and backtest rules above.

19. **Range support/resistance retest** — Build an `ETHUSDT` 5-minute scalp. Define support and resistance from confirmed 30-bar swing pivots; enter after a level breaks and is retested from the other side, with candle-close confirmation. Stop 1 ATR beyond the retest and target 2R. Apply the pair and backtest rules above.

20. **ATR expansion breakout** — Build a `SOLUSDT` 1-minute scalp. Enter in the direction of a candle whose range exceeds 1.8 times ATR(14) and whose volume is above its 20-bar average. Stop 1 ATR from entry and target 1.5 ATR; skip signals after an equally large opposing candle. Apply the pair and backtest rules above.

21. **Fast EMA crossover** — Build a `HYPEUSDT` 1-minute scalp using EMA(5) and EMA(13), filtered by price relative to EMA(50). Enter only on a crossover in the EMA(50) direction; exit on the reverse cross or at a 0.75 ATR stop / 1 ATR target. Apply the pair and backtest rules above.

22. **CCI extreme reversal** — Build a `SUIUSDT` 5-minute scalp. Enter long when CCI(20) crosses back above -200 and price closes above the prior candle high; enter short when CCI crosses below +200 and price closes below the prior low. Use a 1 ATR stop and 1.5 ATR target. Apply the pair and backtest rules above.

23. **OBV confirmation breakout** — Build a `JUPUSDT` 5-minute scalp. Enter on a close beyond the prior 15-bar high/low only when On-Balance Volume also breaks its corresponding 15-bar extreme. Use a 1 ATR stop and 2 ATR target; exit after 15 bars if neither is reached. Apply the pair and backtest rules above.

24. **Volume-profile point-of-control reversion** — Build a `BTCUSDT` 5-minute scalp using historical volume-at-price data. Fade a move 1.5 ATR away from the current session point of control after a rejection close; take profit at the point of control and place a 1 ATR stop beyond the rejection extreme. If the required data is unavailable, report the limitation. Apply the pair and backtest rules above.

25. **Liquidation-spike reversal** — Build an `ETHUSDT` 1-minute scalp using historical liquidation data. After a liquidation spike above the rolling 95th percentile, enter against the spike only after a reversal candle confirms; use a 0.75 ATR stop and 1.25 ATR target. If liquidation history is unavailable, report that instead of using a proxy. Apply the pair and backtest rules above.

## Day trading (15-minute to 4-hour charts; close by UTC day-end)

26. **Hourly Donchian trend breakout** — Build a `BTCUSDT` 1-hour day-trading strategy. Enter on a close above the prior 20-bar high or below the prior 20-bar low, with ADX(14) above 20. Use a 2 ATR stop and 3 ATR target; close all trades at 23:55 UTC. Apply the pair and backtest rules above.

27. **Thirty-minute opening range** — Build an `ETHUSDT` 15-minute strategy using the 00:00–01:00 UTC range. Enter on a candle close outside the range with volume above its 20-bar average; stop at the range midpoint and target 2R. Close positions by 23:55 UTC. Apply the pair and backtest rules above.

28. **Daily VWAP trend day** — Build a `SOLUSDT` 15-minute day strategy. Go long only when price stays above daily VWAP and EMA(20) is above EMA(50); enter on a VWAP retest and bullish close. Mirror for shorts. Use a 1.5 ATR stop and 2.5 ATR target; flatten by 23:55 UTC. Apply the pair and backtest rules above.

29. **Previous-day high/low breakout** — Build a `HYPEUSDT` 30-minute strategy. Enter on a close above the previous UTC day's high or below its low, with volume confirmation. Stop at the broken level and target 2R; exit at UTC day-end if still open. Apply the pair and backtest rules above.

30. **Supertrend with ATR exits** — Build a `SUIUSDT` 1-hour trend-following strategy. Use Supertrend (10, 3) for direction, a fixed 2 ATR stop, and a fixed 3 ATR take profit. Exit on the opposite Supertrend signal or either fixed exit; do not trail the stop. Apply the pair and backtest rules above.

31. **EMA trend pullback** — Build a `JUPUSDT` 30-minute day strategy. Trade only when EMA(20) and EMA(50) are aligned; enter after a pullback to EMA(20) and a close back in the trend direction. Use a 1.5 ATR stop and 2.5 ATR target; flatten by 23:55 UTC. Apply the pair and backtest rules above.

32. **Range-bound RSI reversal** — Build a `BTCUSDT` 1-hour mean-reversion strategy, active only when ADX(14) is below 18. Enter long when RSI(14) crosses above 30 and short when it crosses below 70; exit at RSI 50 or a 1.5 ATR stop. Close all positions by UTC day-end. Apply the pair and backtest rules above.

33. **Daily Bollinger squeeze expansion** — Build an `ETHUSDT` 1-hour strategy. Detect Bollinger Band width in the lowest 10% of its trailing 100 bars, then enter on a close outside the bands with above-average volume. Use a 2 ATR stop and 3 ATR target; close by 23:55 UTC. Apply the pair and backtest rules above.

34. **MACD histogram momentum** — Build a `SOLUSDT` 1-hour day strategy. Enter long when the MACD(12, 26, 9) histogram crosses above zero and price is above EMA(50); reverse for shorts. Use a 1.5 ATR stop and 2.5 ATR target; flatten at UTC day-end. Apply the pair and backtest rules above.

35. **Ichimoku cloud breakout** — Build a `HYPEUSDT` 1-hour strategy using Ichimoku (9, 26, 52). Enter when price closes outside the cloud with Tenkan above Kijun for longs or below for shorts. Use a fixed 2 ATR stop and 3 ATR target; close at UTC day-end. Apply the pair and backtest rules above.

36. **Daily pivot breakout and retest** — Build a `SUIUSDT` 15-minute strategy using pivot levels from the previous UTC day. Enter after price closes beyond R1/S1 and retests that level with confirmation. Stop beyond the retest swing and target the next pivot or 2R, whichever comes first. Flatten by 23:55 UTC. Apply the pair and backtest rules above.

37. **Volatility-regime breakout** — Build a `JUPUSDT` 1-hour day strategy. Trade a 20-bar high/low breakout only when ATR(14) is above its 50-bar median and volume exceeds its 20-bar average. Use a 2 ATR stop and 3 ATR target; close at UTC day-end. Apply the pair and backtest rules above.

38. **Volume-confirmed range break** — Build an `SPCXUSDT` 30-minute strategy. Define a range from the prior 24 bars; enter on a close outside it when volume is at least 1.5 times its 20-bar average. Stop inside the range and target 2R. First verify that `SPCXUSDT` exists on the selected venue; otherwise report unavailable. Apply the pair and backtest rules above.

39. **UTC daily-open anchored VWAP** — Build a `BTCUSDT` 15-minute day strategy. Anchor VWAP at 00:00 UTC; enter on a retest and rejection of VWAP when the 1-hour EMA(50) agrees with direction. Use a 1.5 ATR stop and 2R target, and flatten by 23:55 UTC. Apply the pair and backtest rules above.

40. **Higher-timeframe trend, lower-timeframe entry** — Build an `ETHUSDT` strategy with a 4-hour EMA(50/200) direction filter and 15-minute pullback entries to EMA(20). Use a 2 ATR stop and 2.5 ATR target, allow no more than one position per direction, and close by UTC day-end. Apply the pair and backtest rules above.

41. **Stochastic range rotation** — Build a `SOLUSDT` 1-hour strategy, active only when ADX(14) is below 20. Enter long when Stochastic(14, 3, 3) crosses up below 20 and short when it crosses down above 80; exit at the midpoint or a 1.5 ATR stop. Flatten by UTC day-end. Apply the pair and backtest rules above.

42. **ADX plus directional-index trend** — Build a `HYPEUSDT` 30-minute day strategy. Enter when ADX(14) rises through 25 and +DI/-DI confirms direction; exit when ADX falls below 20 or at a fixed 2 ATR stop / 3 ATR target. Close open trades by 23:55 UTC. Apply the pair and backtest rules above.

43. **Multi-timeframe Supertrend** — Build a `SUIUSDT` 15-minute strategy. Require 4-hour Supertrend (10, 3) to set direction, then enter on a 15-minute Supertrend (7, 2) flip in the same direction. Use a 1.5 ATR stop and 2.5 ATR target; flatten by UTC day-end. Apply the pair and backtest rules above.

44. **UTC session overlap breakout** — Build a `JUPUSDT` 15-minute day strategy. Define the 12:00–16:00 UTC high/low; enter on a close beyond that range with volume confirmation. Use a 1.5 ATR stop and 2R target, and close all positions at 23:55 UTC. Apply the pair and backtest rules above.

45. **Supertrend plus RSI confirmation** — Build a `BTCUSDT` 1-hour day strategy. Use Supertrend (10, 3) for direction and require RSI(14) above 55 for longs or below 45 for shorts. Enter after a pullback candle closes back in trend direction; use a 2 ATR stop and 3 ATR target. Flatten by UTC day-end. Apply the pair and backtest rules above.

46. **Donchian breakout with time exit** — Build an `ETHUSDT` 30-minute day strategy. Enter on a close beyond the prior 30-bar high/low; use a 2 ATR stop and 2.5 ATR target. If neither exit is hit, close after 8 bars or at 23:55 UTC, whichever comes first. Apply the pair and backtest rules above.

47. **Funding-rate extreme reversion** — Build a `SOLUSDT` 1-hour perpetual strategy using historical funding rates. When funding is in the top or bottom 5% of its trailing 90-day distribution, enter against crowded positioning only after price confirms a reversal; use a 2 ATR stop and 2.5 ATR target. Include funding payments. If historical funding is unavailable, report the limitation. Apply the pair and backtest rules above.

48. **Realized-volatility regime filter** — Build an `SPCXUSDT` 1-hour strategy. Trade a 20/50 EMA crossover only when 24-hour realized volatility is above its trailing 30-day median; use a 2 ATR stop and 3 ATR target, and flatten by UTC day-end. Verify the exact `SPCXUSDT` market first and report it unavailable if not listed. Apply the pair and backtest rules above.

49. **Prior-day liquidity sweep and reversal** — Build a `BTCUSDT` 15-minute day strategy. Enter short when price sweeps the previous UTC day's high and closes back below it, or long when it sweeps the low and closes back above. Stop beyond the sweep extreme and target the daily open or 2R. Flatten by 23:55 UTC. Apply the pair and backtest rules above.

50. **Rate-of-change with relative strength** — Build a `JUPUSDT` 1-hour day strategy, benchmarked against `BTCUSDT`. Enter long when the asset's 12-bar rate of change is positive and its 24-bar return exceeds BTC's; enter short on the mirrored condition. Use a 1.5 ATR stop and 2.5 ATR target, and flatten by UTC day-end. Report results for both legs and the combined strategy. Apply the pair and backtest rules above.
