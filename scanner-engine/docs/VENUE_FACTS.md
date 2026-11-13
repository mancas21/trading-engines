# scanner-engine — venue & endpoint facts (measured, not remembered)

All rows below were produced by calling the live public endpoints on 2026-10-08
from `C:\Users\Lenovo`. Anything not listed here was **not** measured.

## Endpoint facts

| Fact | Detail |
|---|---|
| Binance `takerlongshortRatio` | **No 1m period** — returns `[]`. Periods 5m/15m/30m/1h/4h resolve. Never use it for a 1m/5m scanner. |
| Binance per-bar CVD source | `/fapi/v1/klines` idx 5 = base volume, idx 9 = `takerBuyBaseVolume`. `delta = takerBuy − (vol − takerBuy)`. |
| Binance aggTrades `m` flag | `m=True` means **the buyer was the maker**, so the aggressor **SOLD**. Inverted reading = perfectly inverted CVD. |
| **JUP listings** | Added 2026-10-14. All four venues list it and all four answer depth/trades: Binance `JUPUSDT` (PERPETUAL, not TRADIFI), OKX `JUP-USDT-SWAP`, Bybit `JUPUSDT` (LinearPerpetual), Kraken `JUPUSD`. |
| **JUP liquidity** | Measured over 60 x 1m bars: Binance **6,792 USD/min** (median 4,935, max 56,126), OKX 1,372, Bybit 3,013, Kraken **811 (median 5)**. Thinnest pair in the registry -> `bar=5m`. |
| **JUP tape vs the flow floor** | In a 60s window: Binance 23 trades, OKX 8, Bybit 10, Kraken 0. The rules' `trade_floor` is 50, so **no venue contributes `flow_by_venue`** for JUP. `book_imb_5` is unaffected (it reads the book, not the tape), so rule 7(c) stays evaluable. Do NOT lower the floor to make JUP produce flow -- an 8-trade delta-ratio is noise. |
| **Kraken `fetch_closed_bar`** | Was dead for EVERY pair: it indexed `fetch_ohlc`'s dicts positionally (`c[0]`, `c[4]`) raising `KeyError: 0`. Hidden because the resolver only consults a pair's REFERENCE venue (Binance). Fixed 2026-10-14. |
| **Bybit / Kraken `import time`** | Both were missing it, so `fetch_closed_bar(..., at_ms=None)` raised `NameError`. Latent for the same reason. Fixed 2026-10-14. |
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
| XAU/USD | `XAUTUSDT` | `XAU-USDT-SWAP` | `XAUTUSD` | `XAUUSDT` |
| SPCX/USD | `SPCXUSDT` | `SPCX-USDT-SWAP` | **absent** | `SPCXUSDT` |
| NVDA/USD | `NVDAUSDT` | `NVDA-USDT-SWAP` | **absent** | `NVDAUSDT` |
| ARB/USD | `ARBUSDT` | `ARB-USDT-SWAP` | `ARBUSD` | `ARBUSDT` |
| 1000PEPE/USD | `1000PEPEUSDT` | `PEPE-USDT-SWAP` | `PEPEUSD` | `1000PEPEUSDT` |

Kraken lists gold under BOTH `XAUTUSD` (ZUSD-quoted) and `XAUTUSDT`
(USDT-quoted); both answer on the Depth endpoint. This engine trades
`XAUTUSD` and the registry records that, because the registry did not
exist as an authority until 2026-11-12 and had recorded the other one.

Kraken rejects SPCX/NVDA with `EQuery:Unknown asset pair` — genuinely absent,
not a discovery bug. XAU maps to **`XAUT`** on Binance (`underlyingSubType` includes `RWA`).

### XAU/USD — two gold contracts, and this pair mixes them

Measured live 2026-11-12 (touches, same instant):

| Venue | Symbol | Touch | Cluster |
|---|---|---|---|
| Binance | `XAUUSDT` | 3870.38 | XAU |
| Binance | `XAUTUSDT` | 3860.04 | XAUT |
| Bybit | `XAUUSDT` | 3870.71 | XAU |
| Bybit | `XAUTUSDT` | 3859.24 | XAUT |
| OKX | `XAU-USDT-SWAP` | 3871.1 | XAU (the only one it lists) |
| Kraken | `XAUTUSD` | 3858.6 | XAUT (the only one it lists) |
| Kraken | `XAUTUSDT` | 3863.7 | XAUT |

**No gold contract exists on all four venues.** OKX only lists the XAU
quote and Kraken only the XAUT quote, so the XAU pair's cross-venue
basis is structurally ~23-32bps of *instrument difference*, not a
dislocation. Read that pair's basis as noise, never as rule-1 evidence.

The registry now records exactly what the adapters traded before it
became authoritative: binance `XAUTUSDT`, okx `XAU-USDT-SWAP`, kraken
`XAUTUSD`, bybit `XAUUSDT`. It had recorded kraken=`XAUTUSDT` and
bybit=`XAUTUSDT`; neither was ever read, and both would have silently
changed the pair on the day the registry took over.

### 1000PEPE/USD (added 2026-11-12)
### 1000PEPE/USD (added 2026-11-12) — THE MULTIPLIER IS NOT UNIFORM

Measured live with `_probe_pepe_venues.py`, `_probe_pepe_notional.py`,
`_probe_pepe_liquidity.py`; verified through the engine's own machinery with
`_verify_pepe_pair.py`.

| Fact | Detail |
|---|---|
| Why this pair exists | It is the instrument the ATAS chart was showing (`1000PEPEUSDT`). Without it the ATAS reader's rule AT-6/AT-7 condition (c) — the multi-venue book consensus — was **UNEVALUABLE** for this symbol, because the scanner did not track it. |
| Binance listing | fapi lists **`1000PEPEUSDT` only**. `PEPEUSDT` (1x) is NOT listed on the futures API — only on spot. This adapter is fapi-only. |
| Bybit listing | `1000PEPEUSDT` and `1000PEPEPERP` (both 1000x, `qtyStep=100`, `priceScale=7`). |
| OKX listing | `PEPE-USDT-SWAP` only — **1x price**, and `ctVal=10,000,000` PEPE per contract. There is no 1000x OKX instrument. |
| Kraken listing | `PEPEUSD` — **1x spot** (also PEPECOINUSD). Kraken has no 1000x pair and no PEPE perp. |
| Consequence | A naive "derive the symbol from the pair name" adapter asks OKX for `1000PEPE-USDT-SWAP` and Kraken for `1000PEPEUSD`; both answer `does not exist`. The registry's per-venue `symbols` is now authoritative (`pairs.venue_symbol`), and the adapters consult it. |
| Price scale | The reference scale is the **1000x** contract (Binance/Bybit quote per 1000 PEPE). OKX and Kraken quote per PEPE → `price_mult` 1000 on both. |
| Size unit | Binance/Bybit quote size in 1000PEPE (`size_mult` 1). OKX quotes size in **CONTRACTS** → 1 contract = 10,000,000 PEPE = 10,000 units of 1000PEPE (`size_mult` 10000). Kraken quotes in PEPE → `size_mult` 0.001. |
| Notional preserved | Measured OKX touch: 17 contracts x 10,000,000 PEPE at 4.3818e-06 = **$744.9**; on the reference scale 17 x 10,000 = 170,000 units of 1000PEPE x 0.0043818 = **$744.9**. Same money — the product `price_mult * size_mult` is the token count per price unit. |
| Why the scale has to be fixed | `book_imb_5` is a RATIO and survives untouched; `basis_bps` compares PRICES and does not. Unscaled, the cross-venue mid becomes a blend of 0.00435 and 4.38e-06 and every venue's basis is off by 1000x. Measured after the fix: binance +1.87bps, bybit +0.15, kraken −2.15, okx +0.26 — single-digit bps, which is a cross-venue spread, not a scale error. |
| 60 x 1m notional USD | binance **399,924** (median 345,129) · okx 270,271 (median 237,693). |
| Bar interval | `5m`. 1m is not defensible at ~400K USD/min — the same standard that puts HYPE (435K) on 5m. |
| Tape in a 60s window | binance ~975 trades · okx ~489 (11s span) · bybit ~620 · **kraken ~12**. Kraken fails the rules' `trade_floor` (50) and contributes **no** `flow_by_venue` — the same situation as ARB and JUP. Its `book_imb_5` is unaffected (that reads the book, not the tape). Do NOT lower the floor to make PEPE produce flow. |
| Spreads | binance/okx 0.23bps · bybit 2.31bps · **kraken 2.3–4.6bps**. Kraken is a thin spot book, so any cross-venue basis must be read against each venue's OWN spread. |
| Kraken is spot | Not a perp. It is still a valid book to read for the top-5 imbalance; it is NOT a valid fill venue, which is why `ref_venue` is `binance`. |
| Condition (c) outcome | Live on the reader after the pair went in service: `2C/2V venues por lado (4 vivos)` → state **SPLIT**, i.e. the consensus is genuinely split rather than absent. Rule 6/7 no longer report `UNEVALUABLE` for this symbol. |

### ARB (added 2026-11-10)

Measured live with `_probe_arb.py`, `_probe_arb_venues.py`, `_probe_arb_tape.py`.

| Fact | Detail |
|---|---|
| Binance contract type | `PERPETUAL` (`underlyingSubType` = `['Layer-2','Crypto']`), tick 0.000010, onboard 2022-03-23. Not TRADIFI. |
| OKX | `ARB-USDT-SWAP`, `state=live`, ctVal 10, tick 0.00001, lot 0.1. |
| Bybit | `ARBUSDT`, `LinearPerpetual`, tick 0.00001. |
| Kraken | `ARBUSD` — listed and answers, and the key comes back **unmangled** (like `SUIUSD`/`JUPUSD`). |
| 60 x 1m notional USD | binance **37,209** (median 32,606) · okx 21,115 (median 17,917) · bybit 17,959 (median 12,377) · kraken 1,043 (median 57). |
| Bar interval | `5m`. 1m is only defensible for ETH/SOL/XRP; ARB's own 1m book is ~37K USD/min. |
| Tape in a 60s window | binance 133 trades (86 taker-buy) · okx 100 (61 buy) · bybit 60 (50 buy) · **kraken 2**. Kraken therefore fails the rules' `trade_floor` (50) and contributes **no** `flow_by_venue` for ARB — same situation as JUP. Its `book_imb_5` is unaffected (that reads the book, not the tape). Do NOT lower the floor to make ARB produce flow. |
| Spreads | binance/okx/bybit 0.63bps · **kraken 6.32bps**. Kraken is a thin spot book, so any cross-venue basis must be read against each venue's OWN spread. |
| Kraken depth | top5 bid 721,852 ARB vs ask 145,186 — large but thin in turnover; the imbalance is real but low-conviction. |


## Rate-limit budget

Binance `exchangeInfo` → `REQUEST_WEIGHT MINUTE=2400` (this instance; the
commonly-quoted 1200 is not what the API reports).

8 pairs × 1 weight (`/klines`) = 8 weight/cycle. Sequential fetch of all 8
measured **2.256s, 0 failures** (~282ms/pair). Ceiling 2400/8 = 300 cycles/min.

**`poll_interval_s: 15` is validated** — 4 cycles/min, 1/75 of headroom.

**After 1000PEPE was added (2026-11-12):** 9 pairs × 1 weight = 9 weight/cycle,
so the ceiling is 2400/9 = 266 cycles/min and the same 4 cycles/min is 1/66 of
headroom. The headroom did not change materially and the interval is still
validated.

## 1m liquidity (avg 1m notional, Binance)

```
ETH  $24.8M   SOL $3.6M   XRP  $969K
HYPE   $435K  SUI  $345K  XAU  $126K
1000PEPE $400K
ARB   $37.2K  NVDA ~$18K  SPCX ~$10K     <- 1m not viable
```

1m is viable for ETH/SOL/XRP, marginal for HYPE/SUI/XAU/1000PEPE, not viable
for ARB/NVDA/SPCX. Decide the bar from **measured notional**, not a hand-written list.

## In-progress bars

The last row of any kline series is the current, partially-formed bar. It must
be excluded from features *and* outcome resolution. `venues/binance.py` derives
`closed` from `closeTime <= now` rather than blindly dropping `bars[-1]`,
because the last row is only the current bar when the request lands inside the
same minute.
