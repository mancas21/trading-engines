# scanner-engine — venue & endpoint facts (measured, not remembered)

All rows below were produced by calling the live public endpoints on 2026-10-08
from `C:\Users\Lenovo`. Anything not listed here was **not** measured.

## Endpoint facts

| Fact | Detail |
|---|---|
| Binance `takerlongshortRatio` | **No 1m period** — returns `[]`. Periods 5m/15m/30m/1h/4h resolve. Never use it for a 1m/5m scanner. |
| Binance per-bar CVD source | `/fapi/v1/klines` idx 5 = base volume, idx 9 = `takerBuyBaseVolume`. `delta = takerBuy − (vol − takerBuy)`. |
| Binance aggTrades `m` flag | `m=True` means **the buyer was the maker**, so the aggressor **SOLD**. Inverted reading = perfectly inverted CVD. |
| OKX `market/tickers` (plural) | HTTP 400 `{"code":"50014","msg":"Parameter instType can not be empty."}`. Use singular `/market/ticker`. |
| OKX `market/candles` | 9 fields, **no taker-buy volume** → per-bar CVD is impossible on OKX. Trade-side only. |
| OKX trade side | `side` field on `/market/trades` is the taker side. |
| Kraken pair keys | Mangled inconsistently: `XBTUSD`→`XXBTZUSD`, `XRPUSD`→`XXRPZUSD`, `ETHUSD`→`XETHZUSD`, but `SUIUSD`/`SOLUSD`/`HYPEUSD`/`XAUTUSDT` are **unmangled**. No rule predicts it → always discover from the response, return `None` on ambiguity. |
| Kraken trades order | **Ascending** by time. `trades[0]` is the OLDEST. Naive `trades[0]` age overstated staleness by 1508s in a live check. Use `max(ts)`. |
| Kraken OHLC | `interval=1` works, returns 721 candles. |
| Bybit orderbook `limit` | Only `{1,50,200,500}` accepted; arbitrary values rejected. Check `retCode == 0`. |
| Bybit trade side | `side` = taker side ("Sell"/"Buy"). |

## Contract types — the trap that produced a false coverage table

Binance lists tokenized equities (`SPCXUSDT`, `NVDAUSDT`) as
**`contractType: TRADIFI_PERPETUAL`**, *not* `PERPETUAL`.

Filtering `exchangeInfo` on `contractType == "PERPETUAL"` reports SPCX/NVDA as
**unlisted when they are live and tradeable**. This is what produced a wrong
"available on only 2 of 4 venues" table in this repo. Fixed in
`venues/binance.py` via `TRADABLE_CONTRACT_TYPES`.

Correct rule:

```python
TRADABLE_CONTRACT_TYPES = frozenset({"PERPETUAL", "TRADIFI_PERPETUAL"})
```

## Symbol map (verified tradeable on each venue)

| Pair | Binance | OKX | Kraken | Bybit |
|---|---|---|---|---|
| SUI/USD | `SUIUSDT` | `SUI-USDT-SWAP` | `SUIUSD` | `SUIUSDT` |
| XRP/USD | `XRPUSDT` | `XRP-USDT-SWAP` | `XRPUSD` | `XRPUSDT` |
| SOL/USD | `SOLUSDT` | `SOL-USDT-SWAP` | `SOLUSD` | `SOLUSDT` |
| HYPE/USD | `HYPEUSDT` | `HYPE-USDT-SWAP` | `HYPEUSD` | `HYPEUSDT` |
| ETH/USD | `ETHUSDT` | `ETH-USDT-SWAP` | `ETHUSD` | `ETHUSDT` |
| XAU/USD | `XAUTUSDT` | `XAU-USDT-SWAP` | `XAUTUSDT` | `XAUUSDT` |
| SPCX/USD | `SPCXUSDT` | `SPCX-USDT-SWAP` | **absent** | `SPCXUSDT` |
| NVDA/USD | `NVDAUSDT` | `NVDA-USDT-SWAP` | **absent** | `NVDAUSDT` |

Kraken rejects SPCX/NVDA with `EQuery:Unknown asset pair` — genuinely absent,
not a discovery bug. XAU maps to **`XAUT`** on Binance (`underlyingSubType` includes `RWA`).

## Rate-limit budget

Binance `exchangeInfo` → `REQUEST_WEIGHT MINUTE=2400` (this instance; the
commonly-quoted 1200 is not what the API reports).

8 pairs × 1 weight (`/klines`) = 8 weight/cycle. Sequential fetch of all 8
measured **2.256s, 0 failures** (~282ms/pair). Ceiling 2400/8 = 300 cycles/min.

**`poll_interval_s: 15` is validated** — 4 cycles/min, 1/75 of headroom.

## 1m liquidity (avg 1m notional, Binance)

```
ETH  $24.8M   SOL $3.6M   XRP  $969K
HYPE   $435K  SUI  $345K  XAU  $126K
SPCX  ~$10K   NVDA ~$18K     <- 1m not viable
```

1m is viable for ETH/SOL/XRP, marginal for HYPE/SUI/XAU, not viable for
SPCX/NVDA. Decide the bar from **measured notional**, not a hand-written list.

## In-progress bars

The last row of any kline series is the current, partially-formed bar. It must
be excluded from features *and* outcome resolution. `venues/binance.py` derives
`closed` from `closeTime <= now` rather than blindly dropping `bars[-1]`,
because the last row is only the current bar when the request lands inside the
same minute.