# Quant & Engineering Audit — trading-engines

**Repo:** github.com/mancas21/trading-engines · **Commit audited:** `ee2cd17302af70c42ca5ab4983cc3d469db462db` (shallow clone, LFS stubs present, no pushes made)
**Scope:** signal logic, statistics, backtest/validation math, execution layer, architecture, secrets hygiene, test health.
**Method:** full code reads of each subsystem; executable reproductions where possible (repro outside the repo at `/home/claude/scratch_repro/repro.py`); a null-hypothesis simulation for the overlap correction; test-suite runs per subsystem.

Severity: **C** = critical (money, credentials, or a broken safety path), **H** = high (materially wrong numbers or decisions), **M** = medium, **L** = low / hygiene. "Verified" means reproduced or executed; "Read" means found by code inspection only.

---

## 0. Executive summary

1. **Live credentials are committed to a public repository.** Two Telegram bot tokens are hard-coded in tradingview-bridge scripts and a third sits in a committed config backup. Treat them as compromised. Rotate before anything else.
2. **The live execution path crashes on its own error handling.** `execution/auto_router.py` references `SafetyError` without importing it. Whenever a Binance error is raised inside those `except` clauses, Python raises `NameError` instead. Reproduced.
3. **The statistics that drive "is this edge real" are wrong in several places.** Sharpe and annualized return are annualized with the wrong horizon (Sharpe 5.90 reported where the correct figure is 0.83; annual return of 1.19 million percent). Effective-N can exceed N. The "Bonferroni-adjusted" label is not applied the way the label says.
4. **The backtester cannot run its first signal.** A signature mismatch in the capture lambda raises `TypeError` on the first emitted signal. Reproduced. Funding costs are zero for 1h horizons because the default bar length is wrong at two call sites.
5. **The signal engine has a self-referential feedback loop.** Alerts are forwarded to the bridge and read back as the "Level 2" input, and outcome statistics are computed only for signals that already passed the gates. The system partly validates itself.
6. **Several volume and confluence components are either dead code or directionally wrong.** Persistence gates never emit; fallback thresholds are compared against the wrong scale (constant volume reads as "dry-up"); a confluence factor adds to the bearish count when volume is merely average.
7. **The Windows process-liveness check is probably dangerous.** `proclock.pid_alive` uses `os.kill(pid, 0)` on Windows. In CPython, that call on Windows can terminate the target process. Needs confirmation on a Windows box, but if confirmed this is a critical bug in a lock that guards live trading.
8. **The validation pipeline is honest about its limits but still over-selects.** About 100 strategies are screened on the same data, then the survivors are ranked. Only 5 hold up out of sample, and one of those has one OOS trade. Multiple-testing control is weaker than the labels suggest.
9. **Tests are not green anywhere.** Reader: 9 failures. Execution: 22 pass plus one collection error. quant-alert-engine: collection aborts because a test module calls `sys.exit` at import. Scanner: 249 pass. ATAS bridge: 1 environment-dependent failure.

---

## 1. Secrets and repository hygiene

### 1.1 [C] Telegram bot tokens committed to a public repo — Read, confirmed by grep
- `tradingview-bridge/indicator_analysis.mjs` and `tradingview-bridge/multitf_analysis.mjs`: bot token and chat ID hard-coded. Token prefix `8890755562:…` (redacted here).
- `quant-alert-engine/config.json.pre-edgecut-20261006-180205`: committed token with prefix `8844332826:…` (redacted here).

**Impact:** anyone can read the bot's messages, send messages as the bot, and reach the chat ID. If the chat is used for alerts, alert content and timing are visible to strangers.
**Fix:** revoke both tokens in BotFather now, rotate, then remove the literals and load from environment. Purge the files from git history (`git filter-repo`) if the repo will stay public. Add a pre-commit secret scan.

### 1.2 [M] Destructive default in TradingView health tool
- `tradingview-bridge/src/core/health.js` L216–217 runs `taskkill /F /IM TradingView.exe` (or `pkill -f TradingView`) when `killFirst` is set.
- `tradingview-bridge/src/tools/health.js` L23 sets `kill_existing` to `true` by default.

Any health check can force-kill the user's trading terminal and any unsaved state in it. Default should be `false`, and the kill should require an explicit argument.

### 1.3 [L] Duplicate and conflicting artifacts
- `COMPLETE_SUMMARY.txt`, `QUICK_REFERENCE.txt`, and the ATAS audit exist in two directories with identical content. They will drift.
- `features.py` exists in both `quant-alert-engine` and `scanner-engine` with different content. Any import that resolves by basename will silently pick the wrong one.

### 1.4 [L] Hard-coded local paths
- `execution/live_signal.py`: `ENGINE_DIR = Path(r"C:\Users\Lenovo\Documents\backtest-system\code")`. Machine-specific, and it points outside the repo.

---

## 2. Execution layer (highest consequence)

### 2.1 [C] `NameError` on the error path of the live router — Verified
- `execution/auto_router.py` imports only `BinanceError` from `binance_testnet`. The `except (BinanceError, SafetyError)` clauses at L172 and L206 reference `SafetyError`, which is never imported or defined in that module.
- Result: whenever a `BinanceError` is raised and the exception matcher is evaluated, Python raises `NameError` (with `SafetyError` undefined). The original error is lost and the router's recovery logic never runs.
- Reproduced in `/home/claude/scratch_repro/repro.py`.

**Fix:** import `SafetyError` where it is defined, or define it in one shared module. Add a test that forces each exception branch.

### 2.2 [C] No aggregate exposure limit or kill switch — Read
- `auto_router.py` sizes each trade (RISK_PCT 0.5, CONC_HAIRCUT 0.5, LEVERAGE 5, MAX_LAG 24) but there is no portfolio-level cap on gross or net exposure, no daily-loss stop, and no global halt.
- `sig.get("trusted", True)` defaults to `True`. A signal missing the field is treated as trusted. Combined with 2.1, a malformed signal can reach order placement.

**Fix:** add a global kill switch file, daily loss limit, and gross/net exposure caps enforced in the router, not in the signal producer. Default `trusted` to `False`.

### 2.3 [H] Mark price read before flatten on reversal — Read
- `auto_router.py` L150 reads the mark price before closing the existing position on a reverse signal. Price can move between the read and the close, so the reversal is sized and stopped against a stale price.

### 2.4 [H] Stop orders and rounding — Read
- `execution/router.py` sizes as `qty_risk = risk_usd / dist`, then takes the minimum of risk, notional, and margin limits. Sound in principle, but `dist` can be near zero on very tight stops and the result is not capped.
- Stops are placed as `STOP_MARKET` with `closePosition` through the Algo Order endpoint (`/fapi/v1/algoOrder`). If stop placement fails, the code falls back to a market close. That is a reasonable failsafe, but it is untested under exchange rejection.
- `binance_testnet.py`: `round_qty` and `round_price` use `Decimal` with `ROUND_DOWN`. Rounding a stop price down moves a short's stop closer to market and a long's stop further away. The direction should be chosen per side.

### 2.5 [H] Signal source is not in the repo — Read
- `execution/live_signal.py` calls `cbpack_funding_engine`, which does not exist in this repository. Execution tests fail at collection on this import.
- `compute()` uses `fee_bps=5.0`, `slippage_bps=0.5`, `charge_funding=False`. Live signal scoring therefore ignores funding, and the external engine cannot be audited from this repo.
- `WARMUP_BARS = 300`, and `trusted = entry_i >= WARMUP_BARS`. This is a reasonable gate, but it cannot be verified without the external engine.

### 2.6 [H] Probable process-kill on Windows in the lock check — Read, needs Windows confirmation
- `execution/proclock.py`: `pid_alive` uses only `os.kill(pid, 0)`, with no Windows branch.
- In CPython on Windows, `os.kill` does not send a signal for anything other than the console-control events. For any other value it calls `TerminateProcess` on the target. Signal `0` therefore becomes a terminate request with exit code 0, which would kill the process the lock is checking.
- `execution/trigger_entry.py` has a correct `ctypes OpenProcess` branch for `nt`. `proclock.py` does not, so the two disagree.
- The summary notes a "WinError 87" seen in one path, consistent with this area being fragile.

**Status:** this follows from CPython's `os.kill` implementation. It has not been executed on Windows here. **Confirm on the target machine** with a throwaway child process before relying on the lock. Fix regardless: use `OpenProcess` plus `GetExitCodeProcess` on Windows, as `trigger_entry.py` already does.

### 2.7 [H] Silent state reset on corrupt state file — Read
- `execution/state_store.py`: `load()` returns `{}` on `JSONDecodeError`. A corrupt state file therefore looks like "no open positions" to the router.
- `save()` uses `write_text` followed by `os.replace`, with no `fsync`. A crash between write and rename can leave an empty or truncated file, which then triggers the silent reset above.

**Fix:** on decode failure, halt and alert rather than returning empty state. Write to a temp file, `fsync`, then `os.replace`.

### 2.8 [M] Neutral grid bot — Read
- `execution/neutral_bot.py`: `GRID_LEVELS=4`, `GRID_SPACING_PCT=0.005`, `NOTIONAL_PER_SIDE_USDT=100`, 9 symbols. There is no inventory cap, so a trending market accumulates one-sided exposure without limit.
- `check_fills` removes a grid level when placing the take-profit order fails. The level is lost and the position is orphaned.
- A `CANCELED` status after a partial fill drops the level, even though the filled quantity is still open.
- `net_exposure` is display-only; it does not gate any order.

**Fix:** cap inventory per symbol and in aggregate, restore levels on TP failure, and treat `CANCELED` with `executedQty > 0` as a partial fill to reconcile.

---

## 3. Signal engine (quant-alert-engine)

### 3.1 [H] Backtester raises `TypeError` on the first signal — Verified
- `Backtester.run` (≈L2681–2786) defines a capture lambda `(name, sym, price, signal_type="", color="info", bar_key="", timeframe="")`.
- The call at ≈L2302 is `emit_alert(name, ..., market_data=..., direction=...)`. Neither `market_data` nor `direction` is in the lambda signature.
- Result: the backtester fails on its first emitted signal. Any backtest output produced by this code path is from a different version or was never produced.

### 3.2 [H] Funding costs are zero for 1h horizons — Verified
- `CostModel.funding_cost(price, bars_held, qty, bar_seconds=300)` defaults to 5-minute bars.
- Backtester funding at ≈L2744 and `WalkForward` at ≈L2871 do not pass `bar_seconds`. For 1h bars, funding is computed on the wrong horizon and comes out at zero within the test window.
- Every backtest that uses funding on 1h data overstates net returns.

### 3.3 [H] Annualization is wrong — Verified
- `compute_metrics` (≈L463–561):
  - `sharpe = avg_ret / std_ret * (bars_per_year ** 0.5)`. The per-trade Sharpe is annualized with the bar count per year, not the trade count per year. Reproduced: reports 5.90 where the trade-frequency-correct figure is 0.83.
  - `annual_ret = (1 + total_ret) ** (bars_per_year / n_trades) - 1`. The exponent uses bars per year divided by trades, which compounds a per-trade return as if each trade were a bar. Reproduced: 1.19 million percent annual return on a plausible sample.
- Consequence: any ranking or gate that uses Sharpe or annual return is ordering on inflated numbers.

**Fix:** annualize per-trade Sharpe by `sqrt(trades_per_year)`, or compute Sharpe on a fixed-interval return series (daily equity returns is the standard). Annualize CAGR from the calendar span, not from trade count.

### 3.4 [H] Effective-N exceeds N and the "Bonferroni-adjusted" label is wrong — Read
- Effective sample size is `n / (1 + 2·rho)` and is not clamped. With negative lag-1 autocorrelation, `rho < 0`, the result exceeds `n`.
- Output labelled "Bonferroni-adjusted" does not divide the significance level by the number of tests anywhere in `compute_metrics`. The label should be removed or the correction implemented.

### 3.5 [H] Gross returns in outcome resolution — Read
- `_resolve_outcomes` (≈L2334–2379) computes returns before costs. Outcome caches therefore overstate the edge relative to what `CostModel` would charge.

### 3.6 [H] Outcome cache statistics — Read
- `_refresh_outcome_cache` (≈L1750–1826) computes `t = mean / sd · √n` using the population standard deviation (`ddof=0`) and treats overlapping forward returns as independent observations. Both inflate t.
- Muted cells stop producing new data. A cell that looks bad gets muted, and then its record freezes. That is a survivorship mechanism: only currently-good cells keep being updated.

**Fix:** use sample sd (`ddof=1`), correct for overlap (non-overlapping sampling or the lag-based effective-N with a clamp), and keep muted cells reporting, just not alerting.

### 3.7 [H] Feedback loop between alert gating and outcome statistics — Read
- `_emit` (≈L2191–2314) applies gates in this order: outcome → time → blackout → L2 → ML. Then `emit_alert` is called, and `tag_signal` records outcomes only for signals that passed.
- Outcome statistics therefore describe only the signals the gates let through. The gates cannot be evaluated against signals they blocked.
- Blackout uses `time.time()` while `time_gating_utc_offset` is hard-coded to `-5`. This is wrong for half the year under daylight saving time, and it is inconsistent with UTC.

### 3.8 [H] Circular Level 2 confirmation — Read
- The engine forwards every alert to the bridge at `/quant-alert` (≈L862–895). The bridge stores it in `quant_buffers`.
- `_l2_quant_score` (≈L2057–2081) reads back `/quant/{symbol}` from the bridge (`atas-bridge/bridge_server.py` ≈L2938–2954). That read has no age filter.
- So an alert can be "confirmed" by the engine's own earlier alert. Level 2 is not independent evidence.
- `Level2Config` defaults to `enabled: False` and `fail_open: True`, but `config.json.example` enables it. Users who copy the example get the circular path active.

**Fix:** drop the self-read, or filter by age and by source. Make L2 fail-closed or fail-open explicitly and document which.

### 3.9 [H] Volume components — Verified / Read
- Persistence gates (`volume.state_persistence_bars = 2`, `obv_break_bars = 2`) never emit in the tested configuration. The OBV break-persistence helper (`_obv_break_persisted`) and the state gate do not agree on the bar count, so the condition is never met.
- `_check_volume` (≈L2465–2546): fallback thresholds `e_hi = climax_mult * vol_avg` are compared against `ratio = cur_vol / vol_avg`. The first is a volume, the second is a dimensionless ratio. Constant volume produces `ratio = 1`, and the dry-up threshold fires. Verified.
- `_vol_baseline` pulls 15m klines with `limit = 1500` from fapi. Adequate for a rolling baseline, but the window is not documented.

### 3.10 [M] Confluence volume factor is not directional — Read
- `_confluence` (≈L2141): `if v[-1] > vol_avg: bull += 1 else bear += 1`. Volume above average is credited to bulls, and volume at or below average is credited to bears. A quiet bar is therefore evidence for a short, which is not a property of volume.

### 3.11 [M] Trend regime differs between two call sites — Read
- `_trend_regime(c)` is used in confluence without an ATR band, and in the main regime with a band of `EMA ± 0.25·ATR`. The same bar can be "up" in one component and "flat" in the other.

### 3.12 [M] Forming-candle guard is conditional — Read
- `_evaluate` (≈L1450–1639): `if is_forming and len(df) > 51`. With 51 or fewer bars, the forming candle is used and the signal can repaint as the bar closes.

### 3.13 [M] `features.py` mislabels and duplicates — Read
- `ema200 = self.ind.ema(co, min(200, max(60, w // 2)))` is labelled EMA-200 but its period is dynamic, between 60 and 200.
- `cvd = np.cumsum(signed)` and `obv = np.cumsum(signed)` are the same expression. OBV is not computed from volume direction on close.
- `delta_pct_vol` is clamped to ±100 by direction, which hides magnitude.
- Value-area band is symmetric around POC, which is not how a value area is defined (it should be 70% of volume, expanded from POC by volume).

### 3.14 [L] Config
- `Level2Config`: `enabled=False`, `fail_open=True` by default.
- `blackout_enabled=True` by default; `config.json.example` also enables level2 and blackout, so the example differs from the defaults in two places.

---

## 4. ATAS bridge (atas-bridge)

### 4.1 [H] Authentication is a no-op when the token is unset — Read
- `_require_auth` (≈L1523–1529) returns immediately when `BRIDGE_TOKEN` is empty. Mitigated by the default `HOST = 127.0.0.1`, but any deployment that binds `0.0.0.0` without setting the token is open.
- **Fix:** fail closed when the token is empty and the host is not loopback.

### 4.2 [M] DOM imbalance definition — Read
- Imbalance is computed over the top 15 levels using base-quantity ratios (≈L311–313). Base quantity weights a cheap far level the same as a costly near level. Quote-notional weighting is the usual choice for imbalance across symbols with very different prices.

### 4.3 [M] Depth stream payload semantics — Unverified
- Depth stream `@depth20@100ms` with U/u/pu continuity check (≈L272–293). The continuity logic follows Binance's documented procedure for diff streams, but this endpoint is a partial-book snapshot stream, where the continuity fields behave differently. **Verify against a live payload** before relying on the check.

### 4.4 [M] CVD closed-bar delta — Read
- `closed_delta = prev_live_delta` (≈L1000–1030): the closed bar's delta is taken as the last live delta before the bar advanced. Trades arriving in the same millisecond as the rollover are assigned to the wrong bar. Small effect, but systematic on busy symbols.

### 4.5 [OK] Trade side mapping
- `"sell" if m` is correct for Binance's `m` (buyer is maker) convention.

---

## 5. Scanner and statistics (scanner-engine)

### 5.1 [M] Round-trip cost model — Read
- `expectancy.py`: `round_trip_bps = 2 * (FEE + SLIP) + spread`, base 11 bps. Matches the repo's stated convention. Sensitivity to 22 bps is reported separately. Fine, but spread is not measured per symbol.

### 5.2 [OK] Overlap correction — Verified by simulation
- Lag-1 effective-N correction was tested under an i.i.d. null. False-positive rate was 3–4.5% at the nominal 5%. Adequate for this purpose.
- *Retraction:* an earlier note in this audit said the lag-1 correction understated dependence. The simulation contradicted that claim, and it is withdrawn.

### 5.3 [M] Multiple-testing control is across cells only — Read
- `_bonferroni_t` applies correction across cells within one expectancy table. Strategies in other runs (see 5.5) are not included in that family.
- `promotion.py`: `audit` returns `min_t` with the inherited Bonferroni threshold. `DEFAULT_MIN_T = -1.65` is a one-sided bare threshold used by direct calls, with no correction. Direct calls therefore use an uncorrected threshold.

### 5.4 [OK] Candidate gate thresholds
- `backtest_link.py` gate: PF > 1.2, n ≥ 30, net > 0, excess vs buy-and-hold > 0, 11 bps. Concentration is flagged but not vetoed. Reasonable as a screen.

### 5.5 [H] Search-family selection bias in the strategy screen — Read
- `backtest_prompts100/REPORT.md`: 100 prompt-generated strategies, screened on the full window, then the top candidates are checked on OOS. The screen is run on the same data that chooses the candidates.
- 14 pass the full-window gate; 5 hold on OOS. Under a true null, 100 independent tests at 5% would yield roughly 5 false positives by chance alone, so 5 OOS survivors is about the chance baseline, not evidence of edge.
- One of the five (strategy 91) has **one** OOS trade. It is not evidence.
- The report itself says "candidate for formal validation, not validated". That is the right framing. The numbers in the summary table should not be quoted without that caveat.

**Fix:** pre-register the candidate set, use a holdout the search never touched, and apply a multiple-testing correction across the full 100-run family (Bonferroni or Holm on t-stats, or a deflated Sharpe ratio).

### 5.6 [M] Gate uses profit factor, not a significance test
- The candidate gate is PF-based. PF has no sampling distribution used here, so the gate does not control for chance. Pair it with a t-test or bootstrap on net per-trade returns.

---

## 6. Reader (verdict and rules)

### 6.1 [OK] Honest labelling
- `reader/verdict.py` retired a scorecard after finding Pearson +0.0175 and a DOM EMA with rho = 0.995, which amounts to about one independent observation. `v["probability"] = "unvalidated"` is the right output. Keep it.

### 6.2 [M] Rule 6 disabled with n_eff = 2.7
- Rule 6 is disabled with a note that effective N is 2.7. Disabling it is the right call; the note should stay in the rule file so it does not get re-enabled silently.

### 6.3 [H] Reader test failures — Verified
- 9 failures: supervisor timeout regression (return code 1 where 124 is expected), rule 6 `KeyError`, contract JSON `StopIteration`.
- The `KeyError` on rule 6 is consistent with 6.2 being disabled while the test still expects the rule to be present.

---

## 7. Process supervision (quant-alert-engine/qae_supervisor.py)

Read in full this session. The design is sound: clean exit stops the loop, a dedicated exit code (3) for "already running", a crash-loop guard, a stop file, and a single-instance lock.

### 7.1 [L] `_pid_alive` on Windows uses substring matching — Read
- `tasklist /FI "PID eq N"` output is checked with `str(pid) in out.stdout`. A PID of `12` matches a line containing `1234`, so a dead PID can read as alive. Parse the CSV output and compare the PID column exactly, or use `OpenProcess`.
- Consequence: a stale lock from a dead supervisor can block a new one from starting. The stale-lock path is otherwise handled.

### 7.2 [L] Lock acquisition race
- `_acquire_supervisor_lock` retries 5 times and removes a stale lock with `os.remove` followed by a 50 ms sleep. Two supervisors starting at the same moment can both remove the same stale file. Low probability; the `O_EXCL` retry loop covers most cases.

### 7.3 [OK] Exit-code handling
- Clean exit (0), already-running (3), and crash (other) are distinct and documented, and the crash-loop guard is bounded. The "already running" fix addresses a real incident recorded in the file header.

---

## 8. Test health

| Subsystem | Result | Notes |
|---|---|---|
| scanner-engine | 249 pass | Healthy |
| reader | 9 failures | See 6.3 |
| atas-bridge | 1 failure | Environment-dependent |
| execution | 22 pass, 1 collection error | `cbpack_funding_engine` missing (2.5) |
| quant-alert-engine | Collection aborts | `test_supervisor_loop.py` calls `sys.exit(1)` at module import, which stops pytest for the whole directory. Run suites separately until fixed. |

**Fix:** move `sys.exit` under `if __name__ == "__main__"` in the supervisor test. Mark the execution test that needs the external engine as skipped when the module is absent.

---

## 9. Architecture observations

- **The bridge is in the decision path.** The engine reads its own alerts back through the bridge (3.8). Decision inputs should come from market data, not from the engine's outputs.
- **Duplicated logic across subsystems.** Two `features.py`, two regime definitions (3.11), two cost models (`CostModel` in the engine, `expectancy.py` in the scanner, `live_signal.py` at 0 funding). Pick one source of truth for cost and regime.
- **No single place enforces risk.** Sizing lives in the router, gating in the engine, and the kill switch does not exist (2.2). Risk limits belong at the last step before an order is sent.
- **Config drift.** The example config and the code defaults disagree (3.14). Generate the example from the schema.

---

## 10. Prioritized actions

1. **Today:** revoke and rotate the Telegram tokens (1.1). Decide whether to make the repo private or scrub history.
2. **Before any live order flows:** fix the `NameError` (2.1), add a kill switch and exposure caps (2.2), and make `trusted` default to `False`.
3. **Before trusting any backtest number:** fix the TypeError (3.1), funding horizon (3.2), and annualization (3.3). Re-run the 100-strategy screen with a multiple-testing correction (5.5).
4. **Before relying on the process lock on Windows:** confirm 2.6 on a Windows machine and replace `os.kill(pid, 0)`.
5. **Remove the self-confirmation path:** drop the L2 read-back of the engine's own alerts (3.8).
6. **Repair dead volume components:** persistence gates (3.9), fallback scale (3.9), confluence direction (3.10).
7. **Harden state persistence:** no silent reset on corrupt state; `fsync` before rename (2.7).
8. **Clean up:** test collection (8), duplicated artifacts (1.3), machine-specific paths (1.4), destructive default (1.2).

---

## 11. Open questions (cannot be settled from this repo)

- **External engine:** `cbpack_funding_engine` is not in the repo. Live signal correctness and its funding treatment cannot be audited here.
- **Windows `os.kill` behavior (2.6):** needs a live test on the target OS.
- **Depth stream payload (4.3):** needs a captured live payload to confirm continuity logic.
- **Strategy edge:** nothing in the repo establishes a validated edge. The best candidate has one OOS trade (5.5). Any claim of profitability needs a pre-registered, untouched holdout.

---

## Appendix A — Reproduction notes

- `NameError`: `execution/auto_router.py` `except` clauses referencing undefined `SafetyError`. Repro at `/home/claude/scratch_repro/repro.py` (outside repo).
- Annualization: `compute_metrics` sharpe 5.90 vs trade-frequency-correct 0.83; annual return 1.19M% from the exponent `bars_per_year / n_trades`.
- Backtester: `TypeError` from keyword arguments `market_data` and `direction` not accepted by the capture lambda.
- Volume: constant volume yields `ratio = 1`, which triggers dry-up through the mis-scaled fallback threshold.
- Overlap correction: i.i.d. null simulation, false-positive rate 3–4.5% at nominal 5%.

## Appendix B — Redaction

All secrets are shown as prefixes only. Full values appear in the files listed in 1.1 and must be revoked, not copied elsewhere.
