# Quant Alert Engine v2.5 - Windows 11 Python Port

A production-ready Python application that replicates the TradingView Pine Script indicator for real-time crypto trading alerts with quantitative edge tracking, walk-forward validation, and a PyQt6 desktop dashboard.

---

## Features
✅ **Multi-Symbol Support** - Monitor multiple crypto pairs simultaneously
✅ **Real-Time Alerts** - Windows 10/11 notifications + customizable sound alerts
✅ **8 Indicator Families**:
* Volume (Spikes, Climactic, Dry-up, OBV Breaks)
* CVD (Cumulative Volume Delta - tick-rule proxy)
* RSI (Overbought/Oversold, Midline, Divergences)
* MACD (Signal Cross, Zero-Line, Histogram, Divergences)
* Stochastic (%K/%D Cross, OB/OS)
* Bollinger Bands (Squeeze, Breakouts)
* ATR (Volatility Surge Detection)
✅ **Live Data** - Direct integration with Binance via CCXT
✅ **Divergence Detection** - Regular & Hidden divergences for RSI, MACD, CVD
✅ **Configurable Parameters** - JSON-based configuration (no code edits needed)
✅ **Logging** - Full trading session logs with timestamps
✅ **Low Latency** - Efficient polling with configurable scan intervals

---

## System Requirements
* **OS**: Windows 10/11 (64-bit recommended)
* **Python**: 3.8+ (3.10+ recommended)
* **RAM**: 2GB minimum, 4GB+ recommended
* **Internet**: Stable connection for real-time data
* **Binance Account**: Free (for public data feeds)

---

## Installation Guide

### Step 1: Install Python
1. Go to https://www.python.org/downloads/
2. Download **Python 3.10+** for Windows (64-bit)
3. Run the installer
4. **CRITICAL**: Check the box "Add Python to PATH"
5. Click "Install Now"
6. Verify installation:
```
   python --version
```

### Step 2: Download & Extract the Engine
1. Extract all files to a folder (e.g., `C:\\Users\\YourUsername\\quant-alert-engine`)
2. Open Command Prompt / PowerShell in that folder
3. Right-click in folder → "Open PowerShell window here" (or Command Prompt)

### Step 3: Run the Automated Setup
**Option A - Easiest (Recommended)**:
```bash
setup\_windows.bat
```
This will:
* Create a Python virtual environment
* Install all required packages
* Verify everything works
* Create necessary directories

**Option B - Manual Setup**:
```bash
python -m venv venv
venv\Scripts\activate.bat
pip install --upgrade pip
pip install pandas numpy ccxt playsound win10toast
```

### Step 4: Configure the Engine
Edit `config.json` in your editor (Notepad, VSCode, etc.):
```json
{
  "symbols": ["SOL/USDT", "XRP/USDT", "SUI/USDT"],
  "timeframes": ["5m"],
  "general": {
    "check\_interval": 5
  }
}
```

---

## Running the Engine

### Quick Start:
```bash
run\_engine.bat
```
This will automatically:
1. Activate the virtual environment
2. Load your configuration
3. Connect to Binance
4. Start monitoring symbols
5. Log all alerts to `quant\_alert\_engine.log`

### Manual Start:
```bash
venv\Scripts\activate.bat
python quant\_alert\_engine.py
```

**Expected Output**:
```
========================================
QUANT ALERT ENGINE v1.0 - Starting
Symbols: ['SOL/USDT', 'XRP/USDT', 'SUI/USDT']
Timeframes: ['5m']
Check interval: 5s
========================================
[INFO] Analyzing SOL/USDT...
[INFO] Analyzing XRP/USDT...
[INFO] Analyzing SUI/USDT...
```
Stop the engine anytime with **CTRL+C**.

---

## Configuration Guide

### Adding/Removing Symbols
Edit `config.json`:
```json
"symbols": [
  "SOL/USDT",
  "XRP/USDT",
  "SUI/USDT",
  "ETH/USDT",          // Add more
  "BTC/USDT"           // Add more
]
```
**Available symbols**: Any trading pair supported by Binance (e.g., SOL/USDT, BTC/USDT, etc.)

### Changing Check Interval
```json
"general": {
  "check\_interval": 5   // Scan every 5 seconds (min recommended: 3s)
}
```
⚠️ Lower intervals = faster alerts but more API calls (may hit rate limits)

### Indicator Parameters

#### RSI Settings
```json
"rsi": {
  "enabled": true,
  "length": 14,           // Period (default: 14)
  "overbought": 70,       // OB level (default: 70)
  "oversold": 30,         // OS level (default: 30)
  "pivot\_len": 5          // Divergence detection sensitivity
}
```

#### MACD Settings
```json
"macd": {
  "enabled": true,
  "fast": 12,             // Fast EMA period
  "slow": 26,             // Slow EMA period
  "signal": 9,            // Signal line period
  "pivot\_len": 5          // Divergence detection sensitivity
}
```

#### Bollinger Bands
```json
"volatility": {
  "enabled": true,
  "bb\_len": 20,           // Period
  "bb\_mult": 2.0,         // Standard deviations
  "bb\_squeeze\_pct": 20.0, // Squeeze threshold (%ile)
  "atr\_len": 14,
  "atr\_expansion\_mult": 1.8
}
```

#### Volume Settings
```json
"volume": {
  "enabled": true,
  "avg\_len": 20,          // Volume SMA period
  "spike\_mult": 2.0,      // Spike = 2x average
  "climax\_mult": 3.5,     // Climax = 3.5x average
  "dryup\_mult": 0.5       // Dry-up = 0.5x average
}
```

---

## Alert Types

### 🔴 Volume Alerts
* **Volume Spike**: Volume > 2x average
* **Volume Climax**: Volume > 3.5x average (potential reversal)
* **Volume Dry-Up**: Volume < 0.5x average (consolidation/coil)
* **OBV Bullish/Bearish Break**: OBV trendline crosses

### 🔵 CVD Alerts (Tick-Rule Proxy)
* **CVD Bullish/Bearish Flip**: Direction reversal

### 🟣 RSI Alerts
* **RSI Overbought** (>70) / **Oversold** (<30)
* **RSI Midline Cross** (above/below 50)
* **RSI Divergences**: Regular & Hidden (Bullish/Bearish)

### 🟠 MACD Alerts
* **MACD Bullish/Bearish Cross** (line crosses signal)
* **MACD Zero-Line Cross** (line crosses 0)
* **MACD Histogram Momentum Flip**
* **MACD Divergences**: Regular & Hidden

### 🔘 Stochastic Alerts
* **Stoch %K/%D Bullish/Bearish Cross**
* **Stoch Overbought** (>80) / **Oversold** (<20)

### 📊 Volatility Alerts
* **Bollinger Band Breakout**: Up/Down
* **BB Squeeze**: Low volatility consolidation
* **ATR Expansion**: Volatility surge detected

---

## Sound Alerts (Optional)

### Add Custom Alert Sound:
1. Find or download a `.wav` sound file (e.g., alert.wav)
2. Place it in the application folder
3. Edit `quant\_alert\_engine.py`, find this line (around line 155):
```python
   self.sound\_file = sound\_file or 'alert.wav'
```
4. Change `'alert.wav'` to your sound filename
5. Restart the engine

### Disable Sounds:
Edit `config.json`:
```json
"general": {
  "enable\_sound": false
}
```

---

## Troubleshooting

### "Python is not recognized"
* Ensure Python is in your PATH
* Restart Command Prompt after installing Python
* Or use the full path: `C:\\Python310\\python.exe quant\_alert\_engine.py`

### "ModuleNotFoundError: No module named 'ccxt'"
* Activate the virtual environment first:
```
  venv\Scripts\activate.bat
```
* Then try again

### "Failed to connect to Binance"
* Check your internet connection
* Binance may be blocking rapid requests (use longer `check\_interval`)
* Wait 1-2 minutes before retrying

### No alerts showing
1. Check `config.json` - ensure indicators are `"enabled": true`
2. Check `quant\_alert\_engine.log` for errors
3. Verify symbols exist: `SOL/USDT`, not `SOLUSDT` (requires the slash!)
4. Wait for data: Need ~50 candles for indicators to calculate

### Windows notifications not showing
* Check Windows notification settings (Settings → System → Notifications)
* Enable notifications for Command Prompt / Python
* Or disable in `config.json`: `"enable\_notifications": false`

### High CPU usage
* Increase `check\_interval` (e.g., 30 seconds)
* Reduce number of symbols
* Close unnecessary applications

---

## Log Files
Logs are saved to `quant\_alert\_engine.log` with entries like:
```
2025-01-15 14:23:45,123 - quant\_alert\_engine - INFO - 🚨 [VOLUME] SOL/USDT - Volume Spike @ 141.2340
2025-01-15 14:24:10,456 - quant\_alert\_engine - INFO - 🚨 [RSI] XRP/USDT - RSI Overbought @ 0.5678
2025-01-15 14:25:33,789 - quant\_alert\_engine - INFO - 🚨 [MACD] SUI/USDT - MACD Bullish Cross @ 0.8912
```

---

## Advanced Usage

### API Keys (If Using Private Binance Endpoints)
Currently, the engine uses **public data only** (no private keys needed).
To use your own API keys for premium features:
1. Edit `quant\_alert\_engine.py`, find the `\_init\_exchange()` method
2. Add your keys:
```python
   exchange\_class = getattr(ccxt, exchange\_id)({
       'apiKey': 'your\_api\_key',
       'secret': 'your\_api\_secret',
       'enableRateLimit': True
   })
```

### Custom Indicators
To add custom indicators:
1. Add the calculation method to the `TechnicalIndicators` class
2. Create a check method in `SignalAnalyzer` (e.g., `\_check\_custom\_signals`)
3. Call it from `analyze\_symbol()`
Example:
```python
def \_check\_custom\_signals(self, symbol: str, price: float, close: np.ndarray):
    your\_indicator = YourCustomIndicator(close)
    if your\_indicator[-1] > threshold:
        self.alert\_mgr.emit\_alert("Custom Signal", symbol, price)
```

---

## Performance Tips
1. **Optimize Symbols**: Monitor only active trading pairs
2. **Increase Interval**: 5-30s depending on your needs
3. **Disable Unused Indicators**: Set `"enabled": false` in config
4. **Use Dedicated Machine**: Laptop or old PC works fine
5. **Run in Background**: Minimize the window or use Task Scheduler

---

## Support & Resources
* **TradingView Pine Script Original**: Check the .pine file for indicator logic
* **CCXT Documentation**: https://docs.ccxt.com/
* **Pandas/NumPy**: https://pandas.pydata.org/, https://numpy.org/
* **Report Issues**: Check `quant\_alert\_engine.log` for error details

---

## Quant Audit Fixes & Strategy Suite (v2.5)

Applied from comprehensive quant audits — all configurable via `config.json` and `.env`:

### Divergence gating (no more spam)
Each divergence (RSI/MACD/CVD, regular + hidden) fires **once per episode** instead of every bar. Configurable spacing between compared pivots (`min_gap` in `rsi`/`macd`/`cvd` sections, default 3) and strict fractal pivots (flat tops no longer fabricate divergences). Indicator extrema are sampled within a +/-2 bar window around price pivots.

### Honest CVD labeling
The "CVD" series is a **bar-signed volume index** (tick-rule approximation), not taker-side CVD: each bar's entire volume is assigned by close direction, and the cumulative sum restarts every fetch. Use it for shapes/crosses only — absolute levels are meaningless. True taker-side CVD (aggTrade WS) is a roadmap item.

### Directional Outcome Tagging (Edge Tracking)
Every fired signal is recorded with its price and direction. After `general.outcome_forward_bars` (default 12) bars, the directional forward return (short returns properly inverted) is resolved in SQLite.

### Selectivity (All Opt-in)
```json
"general": {
  "confluence_required": 2,       // require N directional confirmations (RSI/MACD/STOCH/BB/Volume/Trend)
  "regime_filter_enabled": true,  // true: suppress counter-trend signals
  "regime_trend_len": 50,         // EMA length for regime
  "tf_consolidation_enabled": true, // suppress lower-TF duplicates within a higher-TF candle
  "blackout_enabled": true,       // true: throttle alerts by wall-clock time
  "blackout_seconds": 300,        // min seconds between ANY alert for the same symbol
  "signal_cooldown_seconds": 600  // min seconds between repeats of the SAME signal on a symbol
}
```

`blackout_enabled` is a **recency throttle**, independent of the condition filters above:
even if a fresh cross occurs, the engine stays silent for `blackout_seconds` since the
last alert on that symbol, and for `signal_cooldown_seconds` since the last time that
exact signal fired. The health endpoint reports real counters via `analyzer.blackout_health()`.

### Level-2 Validator (ATAS bridge order-flow gate)
Before a **directional** (bull/bear) alert fires, the engine can validate it against the
ATAS bridge's live order flow: DOM imbalance, CVD trend, and the bridge's own multi-TF
quant consensus. It scores each signal and passes only when the score reaches `min_score`.
```json
"level2": {
  "enabled": true,             // gate directional alerts by bridge order-flow
  "base_url": "http://127.0.0.1:8000",
  "timeout_seconds": 1.5,
  "min_score": 2.0,            // pass when weighted score >= 2.0 (DOM+CVD+quant, 0..3)
  "require_dom": true,         // DOM imbalance vs direction
  "dom_imbalance_min": 5.0,    // |imbalance %| to count as strong DOM confirmation
  "require_cvd": true,         // CVD current-bar delta vs direction
  "require_quant": true,       // bridge quant multi-TF consensus vs direction
  "quant_min_timeframes": 1,   // min distinct TFs for a strong quant vote
  "fail_open": true,           // if bridge unreachable, allow alert instead of blocking
  "cache_ttl_seconds": 4.0     // cache bridge reads per symbol to limit load
}
```
The validator only applies to directional signals; informational/yellow alerts pass through.
Health is exposed via `analyzer.level2_health()`.

### Backtesting and Validation Tools

The repository includes two complementary quantitative validation tools:

#### 1. Signal Replay Evaluator (`quant_alert_engine.py`)
Replays historical bars directly through the real `_evaluate` signal engine to evaluate theoretical forward edge:
```bash
python quant_alert_engine.py --validate SOL/USDT:USDT 5m --bars 2000
python quant_alert_engine.py --walkforward SOL/USDT:USDT 5m --bars 2000
```

#### 2. Position Execution Simulator (`strategy_backtester.py`)
Simulates actual portfolio trading with ATR-based stops/targets, position sizing, realistic fill slippage impact, and multi-fold walk-forward:
```bash
python strategy_backtester.py SOL/USDT:USDT 5m --walkforward --folds 5
```

## Version History**v1.0** (Current)* ✅ Full port of Pine Script indicator* ✅ All 8 indicator families* ✅ Divergence detection* ✅ CVD tick-rule proxy* ✅ Windows 10/11 notifications* ✅ Sound alerts* ✅ Real-time data via CCXT/Binance\---## LicenseThis Python port maintains compatibility with the original Pine Script project structure.\---## Next Steps1. ✅ Run `setup\_windows.bat`2. ✅ Edit `config.json` with your symbols3. ✅ Run `run\_engine.bat`4. ✅ Monitor `quant\_alert\_engine.log`5. ✅ Adjust parameters based on your trading strategy**Happy Trading!** 🚀