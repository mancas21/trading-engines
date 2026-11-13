using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using ATAS.Indicators;
using Newtonsoft.Json;

namespace AtasExporter;

/// <summary>
/// LiveExporter — streams the ACTIVE ATAS chart into a JSON file the Python
/// bridge reads, plus BigTrade / DOM-wall alerts pushed straight to the bridge.
///
/// Design notes that matter (see ATAS_NOTES.md for the platform behaviour):
///  * Only the LIVE bar is exported. OnCalculate replays every historical bar on
///    chart load; writing those would replay stale deltas into the CVD tracker.
///  * The payload is ADDITIVE across versions. `schema_version` tells the reader
///    what it is looking at; no existing key is ever removed or repurposed.
///  * The tape keeps BOTH a 30-print window (the historical `tape.*` fields, kept
///    byte-compatible so old track-record rows stay comparable) and TRUE per-bar
///    totals (the new `tape.bar_*` fields, accumulated from every print).
/// </summary>
[DisplayName("LiveExporter")]
[Category("Export")]
public class LiveExporter : Indicator
{
    // ── Output paths ─────────────────────────────────────────────────────────
    private string GetExportFilePath()
    {
        var symbol = InstrumentInfo?.Instrument?.Replace("/", "").Replace(":", "").ToUpper() ?? "DEFAULT";
        // Output dir: env override first, else the bridge's current dir.
        // (was hardcoded C:\atas-bridge, which no longer exists after the
        // repo moved to trading-engines\atas-bridge).
        var dir = Environment.GetEnvironmentVariable("ATAS_BRIDGE_DIR");
        if (string.IsNullOrWhiteSpace(dir))
            dir = @"C:\Users\Lenovo\trading-engines\atas-bridge";
        return $@"{dir}\_live_indicators_{symbol}.json";
    }

    private string _filePath => GetExportFilePath();
    private readonly string _logPath = @"C:\Users\Lenovo\trading-engines\atas-bridge\_exporter_debug.log";

    // ── Atomic publish that survives a reader holding the destination ───────
    // File.Move(.., overwrite:true) is MoveFileEx(MOVEFILE_REPLACE_EXISTING),
    // which needs DELETE access on the destination. On Windows ANY process with
    // an open handle lacking FILE_SHARE_DELETE (CPython's builtin open() does
    // not grant it) makes that rename fail with WinError 5 — the snapshot then
    // silently stops updating for that symbol while its siblings keep streaming.
    // Verified: a single held read handle blocks the rename indefinitely, and a
    // rapid open/close reader fails ~60% of renames under load.
    //
    // Strategy, in order:
    //   1. Retry the atomic rename with backoff (measured: 0/300 failures vs
    //      114-187/300 with a single attempt, under an active reader).
    //   2. Fall back to a direct overwrite, which only needs write access and
    //      succeeds even while a reader holds a read handle.
    // Direct overwrite is NOT atomic — a reader can catch a torn file — but the
    // bridge treats unparseable/empty content as "skip this poll" and keeps the
    // previous snapshot, so a rare torn read degrades rather than corrupts.
    private static void PublishSnapshot(string tempPath, string destPath, string logPath)
    {
        const int maxAttempts = 8;
        for (var attempt = 1; attempt <= maxAttempts; attempt++)
        {
            try
            {
                File.Move(tempPath, destPath, true);
                return;
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }

            Thread.Sleep(15 * attempt);
        }

        // Last resort: direct overwrite. Keeps the feed alive when some
        // process holds a delete-hostile handle for the whole retry budget.
        try
        {
            File.Copy(tempPath, destPath, true);
            File.Delete(tempPath);
        }
        catch (Exception ex)
        {
            try { File.AppendAllText(logPath, $"[{DateTime.Now:HH:mm:ss}] PUBLISH FAILED: {ex.Message}\n"); } catch { }
        }
    }

    // ── Shared lock ─────────────────────────────────────────────────────────
    // One critical section for the tape counters, the DOM cache and the alert
    // queue. Nothing that can block (file I/O, HTTP) is allowed to hold it.
    private readonly object _lock = new();
    private DateTime _lastWrite = DateTime.MinValue;

    // ── Tape ────────────────────────────────────────────────────────────────
    // Two views of the same bar:
    //   _tapeBuffer  — the last MAX_TAPE_TRADES prints, kept for the `trades[]`
    //                   detail array. This is what the historical `tape.*`
    //                   fields describe and they keep that exact meaning.
    //   _barBuyVol / _barSellVol / _barBuyCount / _barSellCount — TRUE totals
    //                   for the current bar, accumulated from EVERY print with
    //                   no cap. Published as the new `tape.bar_*` fields.
    private readonly List<(double price, double vol, string side)> _tapeBuffer = new();
    private const int MAX_TAPE_TRADES = 30;
    private double _barBuyVol;
    private double _barSellVol;
    private int _barBuyCount;
    private int _barSellCount;

    // Bar-scoping: tracks which bar the tape belongs to. When the live bar
    // rolls over, the buffer and the totals are cleared so the snapshot never
    // mixes trades from previous bars (audit finding 2026-08-05).
    private long _tapeBarIndex = -1;

    // ── Closed-bar footprint cache ─────────────────────────────────────────
    // A closed bar never changes, so its footprint is computed once per bar
    // rollover and reused until the next rollover. Recomputing 20 closed bars
    // on every publish (3.3 Hz) would be pure waste.
    private readonly List<ClosedBarFootprint> _closedBars = new();
    private int _closedBarsBarIndex = -1;

    // ── Bridge alert push (BigTrade + DOM walls → POST /alerta-atas) ────────
    // Replaces the manual ATAS-UI webhook config: the indicator itself detects
    // events and pushes them to the bridge in the exact message format the
    // bridge parser expects ("[date] [TICKER]: Ask BigTrade volume greater
    // than X at PRICE (Buy)" / "Best Bid X lots > Y at PRICE").
    [Display(Name = "Enable bridge alerts", GroupName = "Alerts", Description = "POST BigTrade / DOM-wall alerts to the ATAS bridge (/alerta-atas).")]
    public bool EnableAlerts { get; set; } = true;

    [Display(Name = "Bridge URL", GroupName = "Alerts", Description = "Base URL of the ATAS bridge, e.g. http://127.0.0.1:8000")]
    public string BridgeUrl { get; set; } = "http://127.0.0.1:8000";

    [Display(Name = "BigTrade min lots", GroupName = "Alerts", Description = "Single-print volume (base units) that triggers a BigTrade alert.")]
    public decimal BigTradeMinLots { get; set; } = 2000;

    [Display(Name = "DOM wall min lots", GroupName = "Alerts", Description = "Single DOM level volume (base units) that triggers a wall alert.")]
    public decimal WallMinLots { get; set; } = 40000;

    [Display(Name = "Alert cooldown (s)", GroupName = "Alerts", Description = "Minimum seconds before re-alerting the same price level. A persistent wall only re-fires if it grows 25%+.")]
    public int AlertCooldownSeconds { get; set; } = 300;

    // ── Export switches ────────────────────────────────────────────────────
    [Display(Name = "Show price line on chart", GroupName = "Display", Description = "Draw the close-price line on the chart. Off by default: the exporter is a data feed, not a visual.")]
    public bool ShowPriceLine { get; set; }

    [Display(Name = "Publish DOM snapshot", GroupName = "Export", Description = "Include the Smart DOM snapshot (best bid/ask, cumulative volume, 20-level stacks).")]
    public bool PublishDom { get; set; } = true;

    [Display(Name = "Publish tape snapshot", GroupName = "Export", Description = "Include the Smart Tape snapshot (print window + true per-bar totals).")]
    public bool PublishTape { get; set; } = true;

    [Display(Name = "Publish closed-bar footprint", GroupName = "Export", Description = "Include the footprint of the last closed bars. The bridge otherwise only ever sees the FORMING bar.")]
    public bool PublishClosedBars { get; set; } = true;

    [Display(Name = "Closed bars to publish", GroupName = "Export", Description = "How many closed bars of footprint to publish (0-200).")]
    [Range(0, 200)]
    public int ClosedBarsToPublish { get; set; } = 20;

    [Display(Name = "Publish closed-bar levels", GroupName = "Export", Description = "Include the full per-level footprint of each closed bar. OFF by default: it multiplies the snapshot size by ~10x and the snapshot is rewritten 3x/second, even though closed-bar data only changes once per bar rollover.")]
    public bool PublishClosedBarLevels { get; set; }

    [Display(Name = "Stacked imbalance levels", GroupName = "Export", Description = "Consecutive price levels needed to flag a stacked imbalance.")]
    [Range(2, 10)]
    public int StackLevels { get; set; } = 3;

    [Display(Name = "Stacked imbalance ratio", GroupName = "Export", Description = "Side volume must exceed the other by this factor to qualify as an imbalanced level.")]
    [Range(1.0, 10.0)]
    public double StackRatio { get; set; } = 2.0;

    [Display(Name = "Session value area %", GroupName = "Export", Description = "Volume percentage the session value area must cover (ATAS's own ValueAreaPercent setting is deprecated in this build).")]
    [Range(10, 100)]
    public int SessionValueAreaPercent { get; set; } = 70;

    private static readonly HttpClient _http = new HttpClient { Timeout = TimeSpan.FromSeconds(4) };
    private readonly List<string> _pendingAlerts = new();
    private readonly Dictionary<string, (DateTime at, double vol)> _alertCooldowns = new();
    private DateTime _warmupUntil = DateTime.UtcNow.AddSeconds(30);
    private DateTime _lastFlush = DateTime.MinValue;
    private DateTime _lastErrLog = DateTime.MinValue;

    // The default DataSeries[0] carries the close price. Kept as a field so the
    // ShowPriceLine toggle can switch the visual mode without a reload.
    private readonly ValueDataSeries _priceSeries;

    public LiveExporter()
        : base(true)
    {
        _priceSeries = new ValueDataSeries("vals");
        _priceSeries.Color = Color.Orange;
        _priceSeries.VisualType = VisualMode.Hide;
        _priceSeries.IsHidden = true;
        DataSeries[0] = _priceSeries;
    }

    protected override void OnInitialize()
    {
        base.OnInitialize();

        try
        {
            File.WriteAllText(_logPath, $"[{DateTime.Now:HH:mm:ss}] LiveExporter initialized ({InstrumentInfo?.Instrument ?? "?"})\n");
        }
        catch { }
    }

    private string NormSymbol()
    {
        return (InstrumentInfo?.Instrument ?? "?").Replace("/", "").Replace(":", "").ToUpper();
    }

    // ── Settings that must take effect immediately ─────────────────────────
    private void ApplyPriceSeriesVisual()
    {
        _priceSeries.VisualType = ShowPriceLine ? VisualMode.Line : VisualMode.Hide;
    }

    // ── Alerts ─────────────────────────────────────────────────────────────
    private void TryQueueAlert(string key, double vol, string msg, double growthFactor = 1.0)
    {
        var now = DateTime.UtcNow;
        lock (_lock)
        {
            if (_alertCooldowns.TryGetValue(key, out var prev))
            {
                if ((now - prev.at).TotalSeconds < AlertCooldownSeconds)
                    return;
                if (vol < prev.vol * growthFactor)
                    return; // same event re-observed without growth → stay silent
            }
            if (_alertCooldowns.Count > 500)
            {
                var cut = now.AddHours(-1);
                foreach (var k in _alertCooldowns.Where(kv => kv.Value.at < cut).Select(kv => kv.Key).ToList())
                    _alertCooldowns.Remove(k);
            }
            _alertCooldowns[key] = (now, vol);
            _pendingAlerts.Add(msg);
        }
    }

    private void FlushAlerts()
    {
        List<string>? batch = null;
        var now = DateTime.UtcNow;
        lock (_lock)
        {
            if (!EnableAlerts || _pendingAlerts.Count == 0 || (now - _lastFlush).TotalSeconds < 5)
                return;
            batch = new List<string>(_pendingAlerts);
            _pendingAlerts.Clear();
            _lastFlush = now;
        }
        var url = (BridgeUrl ?? "").TrimEnd('/') + "/alerta-atas";
        var payload = JsonConvert.SerializeObject(new { messages = batch });
        Task.Run(async () =>
        {
            try
            {
                using var content = new StringContent(payload, Encoding.UTF8, "application/json");
                var resp = await _http.PostAsync(url, content).ConfigureAwait(false);
                resp.EnsureSuccessStatusCode();
            }
            catch (Exception ex)
            {
                try
                {
                    if ((DateTime.UtcNow - _lastErrLog).TotalSeconds > 60)
                    {
                        _lastErrLog = DateTime.UtcNow;
                        File.AppendAllText(_logPath, $"[{DateTime.Now:HH:mm:ss}] ALERT POST ERROR: {ex.Message}\n");
                    }
                }
                catch { }
            }
        });
    }

    // ── Trades ─────────────────────────────────────────────────────────────
    protected override void OnNewTrade(MarketDataArg arg)
    {
        var vol = (double)arg.Volume;
        var price = (double)arg.Price;
        var dir = arg.Direction;
        var isBuy = dir == TradeDirection.Buy;
        var isSell = dir == TradeDirection.Sell;
        var side = isBuy ? "buy" : (isSell ? "sell" : "?");

        // Bar scoping: reset the window AND the true totals when the live bar
        // rolls over, so neither ever mixes bars.
        var bar = CurrentBar - 1;
        lock (_lock)
        {
            if (_tapeBarIndex != bar)
            {
                _tapeBuffer.Clear();
                _barBuyVol = 0;
                _barSellVol = 0;
                _barBuyCount = 0;
                _barSellCount = 0;
                _tapeBarIndex = bar;
            }

            _tapeBuffer.Add((price, vol, side));
            while (_tapeBuffer.Count > MAX_TAPE_TRADES)
                _tapeBuffer.RemoveAt(0);

            // TRUE per-bar totals — every print counts, no 30-print cap.
            if (isBuy)
            {
                _barBuyVol += vol;
                _barBuyCount++;
            }
            else if (isSell)
            {
                _barSellVol += vol;
                _barSellCount++;
            }
        }

        // ── BigTrade alert: single aggressive print >= threshold ──
        if (EnableAlerts && (isBuy || isSell) && DateTime.UtcNow >= _warmupUntil
            && vol >= (double)BigTradeMinLots)
        {
            var sym = NormSymbol();
            var sideWord = isBuy ? "Ask" : "Bid";
            var dirWord = isBuy ? "Buy" : "Sell";
            var priceStr = price.ToString("0.####", CultureInfo.InvariantCulture);
            var volStr = Math.Round(vol, 1).ToString(CultureInfo.InvariantCulture);
            var key = $"BT|{sym}|{sideWord}|{Math.Round(price, 4).ToString(CultureInfo.InvariantCulture)}";
            var stamp = DateTime.Now.ToString("dd/MM/yyyy HH:mm:ss");
            TryQueueAlert(key, vol,
                $"[{stamp}] [{sym}]: {sideWord} BigTrade volume greater than {volStr} at {priceStr} ({dirWord})");
        }
    }

    // ── Main export ────────────────────────────────────────────────────────
    protected override void OnCalculate(int bar, decimal value)
    {
        if (bar < 0)
            return;

        // Keep the price series a VALID price series for EVERY bar. It lives
        // on the price chart, and a series that is 0 for the whole history
        // poisons the chart's auto-scale — the platform stretches the price
        // axis to include the phantom zero. One GetCandle per bar is cheap.
        if (bar < CurrentBar)
        {
            try
            {
                var priceCandle = GetCandle(bar);
                if (priceCandle != null)
                    _priceSeries[bar] = priceCandle.Close;
            }
            catch { /* a bad index must never break the export */ }
        }

        // Only export the CURRENT (live) bar. OnCalculate is invoked for every
        // historical bar during chart load/reload — writing those would replay
        // stale historical deltas into the bridge's CVD tracker.
        if (bar != CurrentBar - 1)
            return;

        var now = DateTime.UtcNow;
        if ((now - _lastWrite).TotalMilliseconds < 300)
            return;

        try
        {
            var candle = GetCandle(bar);
            if (candle == null)
                return;

            // ── Footprint (live bar) ──
            object? footprint = null;
            var liveLevels = FootprintMath.BuildLevels(candle, StackLevels, StackRatio);
            if (liveLevels.Count > 0)
            {
                footprint = new
                {
                    poc = liveLevels.OrderByDescending(l => l.Volume).First().Price,
                    value_area_high = candle.ValueArea?.ValueAreaHigh ?? 0,
                    value_area_low = candle.ValueArea?.ValueAreaLow ?? 0,
                    top_levels = liveLevels
                        .OrderByDescending(l => l.Volume).Take(10)
                        .Select(LevelToAnon)
                        .ToList(),
                    all_level_count = liveLevels.Count,
                    imbalance_ratio = liveLevels.Sum(l => (double)l.Volume) > 0
                        ? (double)(liveLevels.Sum(l => l.Bid) - liveLevels.Sum(l => l.Ask))
                          / (double)(liveLevels.Sum(l => l.Bid) + liveLevels.Sum(l => l.Ask)) * 100
                        : 0
                };
            }

            // ── DOM snapshot (Smart DOM) ──
            object? domSnapshot = null;
            if (PublishDom)
                domSnapshot = BuildDomSnapshot(candle.Close);

            // ── Smart Tape snapshot ──
            object? tapeSnapshot = null;
            if (PublishTape)
                tapeSnapshot = BuildTapeSnapshot();

            // ── Closed-bar footprint + session value area ──
            object? closedBarsPayload = null;
            object? sessionVa = null;
            if (PublishClosedBars && ClosedBarsToPublish > 0)
            {
                RefreshClosedBars(bar);
                if (_closedBars.Count > 0)
                {
                    // Compact by default. The full per-level footprint of 20
                    // closed bars is ~120 KB of JSON, and this whole file is
                    // rewritten every 300 ms — that is ~750 KB/s of disk writes
                    // and 5 Hz of JSON parsing in the bridge, for data that only
                    // changes once per bar rollover. The per-bar summary below
                    // carries everything the rules read; the raw levels stay
                    // one setting away.
                    closedBarsPayload = _closedBars.Select(b => new
                    {
                        bar_index = b.BarIndex,
                        time = b.Time.ToString("HH:mm:ss"),
                        volume = b.Volume,
                        delta = b.Delta,
                        poc = b.Poc,
                        value_area_high = b.ValueAreaHigh,
                        value_area_low = b.ValueAreaLow,
                        level_count = b.Levels.Count,
                        imbalance_pct = b.ImbalancePercent,
                        stacked_buy_levels = b.Levels.Count(l => l.StackedBuy),
                        stacked_sell_levels = b.Levels.Count(l => l.StackedSell),
                        levels = PublishClosedBarLevels
                            ? b.Levels.Select(LevelToAnon).ToList()
                            : null
                    }).ToList();

                    sessionVa = BuildSessionValueArea();
                }
            }

            // ── Volume context: last closed bar + 20-bar average ──
            // Computed here rather than in the bridge because only the indicator
            // can see historical candles; GetCandle(index) is the ATAS API for it.
            double? prevVol = null;
            double? avgVol20 = null;
            double? volRatio = null;
            int volLookback = 0;
            try
            {
                const int Lookback = 20;
                double sum = 0;
                int n = 0;
                // bar-1 is the last CLOSED bar; walk back further for the average.
                for (int k = 1; k <= Lookback; k++)
                {
                    // GetCandle THROWS on an invalid index — guard instead of
                    // relying on the exception (audit: silent nulls on early bars).
                    if (bar - k < 0)
                        break;

                    var pc = GetCandle(bar - k);
                    if (pc == null)
                        continue;
                    var v = (double)pc.Volume;
                    if (k == 1)
                        prevVol = v;
                    sum += v;
                    n++;
                }
                volLookback = n;
                if (n > 0)
                {
                    avgVol20 = sum / n;
                    if (avgVol20.Value > 0)
                        volRatio = (double)candle.Volume / avgVol20.Value;
                }
            }
            catch { /* volume context is optional - never block the publish */ }

            // ── Serialize ──
            var data = new
            {
                schema_version = 2,
                timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                ticker = InstrumentInfo?.Instrument ?? "?",
                bar_index = bar,
                price = new { last = candle.Close, open = candle.Open, high = candle.High, low = candle.Low },
                volume = new
                {
                    total = candle.Volume,
                    ask = candle.Ask,
                    bid = candle.Bid,
                    // Volume context (added 2026-10-06). The exporter previously
                    // published only the FORMING bar's volume, so rules.json's
                    // volume_climax component could never be scored: it needs a
                    // ratio against recent bars, and a single bar has no baseline.
                    // Read the last CLOSED bars via the same GetCandle(int) the
                    // indicator already uses. Lookback is bounded and every
                    // missing bar is skipped rather than treated as zero.
                    prev_bar_volume = prevVol,
                    avg_volume_20 = avgVol20,
                    volume_ratio = volRatio,
                    lookback_bars = volLookback,
                    delta = (double)candle.Delta
                },
                delta = (double)candle.Delta,
                time = candle.Time.ToString("HH:mm:ss"),

                // ── NEW in schema 2: per-bar extras ──
                open_interest = (double)candle.OI,
                vwap = (double)candle.VWAP,
                max_delta = (double)candle.MaxDelta,
                min_delta = (double)candle.MinDelta,
                max_volume_level = candle.MaxVolumePriceInfo == null
                    ? null
                    : new { price = (double)candle.MaxVolumePriceInfo.Price, volume = (double)candle.MaxVolumePriceInfo.Volume },
                max_tick_level = candle.MaxTickPriceInfo == null
                    ? null
                    : new { price = (double)candle.MaxTickPriceInfo.Price, ticks = candle.MaxTickPriceInfo.Ticks },

                footprint = footprint,
                closed_bars = closedBarsPayload,
                session_value_area = sessionVa,
                dom = domSnapshot,
                tape = tapeSnapshot
            };

            lock (_lock)
            {
                var json = JsonConvert.SerializeObject(data, Formatting.Indented);
                var tempPath = _filePath + ".tmp";
                File.WriteAllText(tempPath, json);
                PublishSnapshot(tempPath, _filePath, _logPath);
            }
            _lastWrite = now;

            // Ship any queued BigTrade / DOM-wall alerts to the bridge (batched, ≤1 POST / 5s).
            FlushAlerts();
        }
        catch (Exception ex)
        {
            try { File.AppendAllText(_logPath, $"[{DateTime.Now:HH:mm:ss}] ERROR: {ex.Message}\n"); } catch { }
        }
    }

    // ── Footprint helpers ──────────────────────────────────────────────────
    private sealed class ClosedBarFootprint
    {
        public int BarIndex;
        public DateTime Time;
        public decimal Volume;
        public decimal Delta;
        public decimal Poc;
        public decimal ValueAreaHigh;
        public decimal ValueAreaLow;
        public List<FootprintMath.Level> Levels = new();

        /// <summary>(bid - ask) / (bid + ask) over the whole bar, in percent.</summary>
        public double ImbalancePercent
        {
            get
            {
                var bid = Levels.Sum(l => l.Bid);
                var ask = Levels.Sum(l => l.Ask);
                var total = bid + ask;
                return total > 0 ? Math.Round((double)((bid - ask) / total) * 100, 3) : 0;
            }
        }
    }

    /// <summary>Projection of one footprint level into the published JSON.</summary>
    private static object LevelToAnon(FootprintMath.Level l) => new
    {
        price = l.Price,
        volume = l.Volume,
        bid = l.Bid,
        ask = l.Ask,
        delta = l.Ask - l.Bid,
        ticks = l.Ticks,
        imbalance_ratio = Math.Round(l.ImbalanceRatio, 4),
        stacked_buy = l.StackedBuy,
        stacked_sell = l.StackedSell
    };

    /// <summary>
    /// Closed bars are frozen, so their footprint is computed once per bar
    /// rollover and reused until the next one.
    /// </summary>
    private void RefreshClosedBars(int liveBar)
    {
        if (_closedBarsBarIndex == liveBar)
            return;

        _closedBars.Clear();
        _closedBarsBarIndex = liveBar;

        var count = Math.Min(Math.Max(ClosedBarsToPublish, 0), 200);
        for (var k = 1; k <= count; k++)
        {
            var b = liveBar - k;
            if (b < 0)
                break;

            IndicatorCandle c;
            try { c = GetCandle(b); }
            catch { break; }
            if (c == null)
                continue;

            var levels = FootprintMath.BuildLevels(c, StackLevels, StackRatio);
            _closedBars.Add(new ClosedBarFootprint
            {
                BarIndex = b,
                Time = c.Time,
                Volume = c.Volume,
                Delta = c.Delta,
                Poc = levels.Count > 0 ? levels.OrderByDescending(l => l.Volume).First().Price : 0,
                ValueAreaHigh = c.ValueArea?.ValueAreaHigh ?? 0,
                ValueAreaLow = c.ValueArea?.ValueAreaLow ?? 0,
                Levels = levels
            });
        }
    }

    /// <summary>
    /// Session value area computed from the closed-bar footprint union.
    /// POC is the highest-volume level; the value area grows from the POC by
    /// repeatedly taking the more voluminous neighbour until SessionValueAreaPercent
    /// of the traded volume is covered.
    /// </summary>
    private object? BuildSessionValueArea()
    {
        try
        {
            var union = new List<FootprintMath.Level>();
            foreach (var b in _closedBars)
                union.AddRange(b.Levels);

            var va = FootprintMath.ComputeValueArea(union, SessionValueAreaPercent);
            if (va.LevelCount == 0)
                return null;

            return new
            {
                poc = va.Poc,
                value_area_high = va.ValueAreaHigh,
                value_area_low = va.ValueAreaLow,
                bars_used = _closedBars.Count,
                total_volume = va.TotalVolume,
                covered_pct = va.CoveredPercent,
                target_pct = SessionValueAreaPercent > 0 ? SessionValueAreaPercent : 70,
                levels = va.LevelCount
            };
        }
        catch
        {
            return null;
        }
    }

    // ── DOM snapshot ───────────────────────────────────────────────────────
    private object? BuildDomSnapshot(decimal lastPrice)
    {
        try
        {
            var depthProvider = DataProvider?.MarketDepthInfoProvider;
            if (depthProvider == null)
                return null;

            var depthEntries = depthProvider.GetMarketDepthSnapshot()?.ToList();
            if (depthEntries == null || depthEntries.Count == 0)
                return null;

            var bids = depthEntries
                .Where(d => d.IsBid)
                .GroupBy(d => d.Price)
                .Select(g => new
                {
                    price = g.Key,
                    volume = (double)Math.Round(g.Sum(d => d.Volume), 1)
                })
                .OrderByDescending(b => b.price)
                .Take(20)
                .ToList();

            var asks = depthEntries
                .Where(d => d.IsAsk)
                .GroupBy(d => d.Price)
                .Select(g => new
                {
                    price = g.Key,
                    volume = (double)Math.Round(g.Sum(d => d.Volume), 1)
                })
                .OrderBy(a => a.price)
                .Take(20)
                .ToList();

            var bidCumSum = 0.0;
            var bidStack = bids.Select(b =>
            {
                bidCumSum += b.volume;
                return new { b.price, b.volume, cum_vol = Math.Round(bidCumSum, 1) };
            }).ToList();

            var askCumSum = 0.0;
            var askStack = asks.Select(a =>
            {
                askCumSum += a.volume;
                return new { a.price, a.volume, cum_vol = Math.Round(askCumSum, 1) };
            }).ToList();

            var totalBid = depthProvider.CumulativeDomBids;
            var totalAsk = depthProvider.CumulativeDomAsks;
            var cumBid = (double)Math.Round(totalBid, 1);
            var cumAsk = (double)Math.Round(totalAsk, 1);

            // ── DOM wall alerts: single level >= threshold ──
            if (EnableAlerts && DateTime.UtcNow >= _warmupUntil)
            {
                var wsym = NormSymbol();
                var wstamp = DateTime.Now.ToString("dd/MM/yyyy HH:mm:ss");
                double wthr = (double)WallMinLots;
                var wthrStr = wthr.ToString("0.#", CultureInfo.InvariantCulture);
                foreach (var b in bids)
                {
                    if (b.volume >= wthr)
                    {
                        var px = Convert.ToDecimal(b.price).ToString("0.####", CultureInfo.InvariantCulture);
                        var lots = b.volume.ToString("0.#", CultureInfo.InvariantCulture);
                        TryQueueAlert($"WALL|{wsym}|B|{Convert.ToDecimal(b.price).ToString(CultureInfo.InvariantCulture)}",
                            b.volume,
                            $"[{wstamp}] [{wsym}]: Best Bid {lots} lots > {wthrStr} at {px}", 1.25);
                    }
                }
                foreach (var a in asks)
                {
                    if (a.volume >= wthr)
                    {
                        var px = Convert.ToDecimal(a.price).ToString("0.####", CultureInfo.InvariantCulture);
                        var lots = a.volume.ToString("0.#", CultureInfo.InvariantCulture);
                        TryQueueAlert($"WALL|{wsym}|A|{Convert.ToDecimal(a.price).ToString(CultureInfo.InvariantCulture)}",
                            a.volume,
                            $"[{wstamp}] [{wsym}]: Best Ask {lots} lots > {wthrStr} at {px}", 1.25);
                    }
                }
            }

            return new
            {
                best_bid = bids.FirstOrDefault()?.price ?? 0,
                best_ask = asks.FirstOrDefault()?.price ?? 0,
                spread = (double)((asks.FirstOrDefault()?.price ?? 0) - (bids.FirstOrDefault()?.price ?? 0)),
                cumulative_bid_vol = cumBid,
                cumulative_ask_vol = cumAsk,
                imbalance_pct = (cumBid + cumAsk) > 0 ? Math.Round((cumBid - cumAsk) / (cumBid + cumAsk) * 100, 3) : 0,
                bid_ask_ratio = cumAsk > 0 ? Math.Round(cumBid / cumAsk, 2) : 0,
                bid_stack = bidStack,
                ask_stack = askStack
            };
        }
        catch
        {
            return null;
        }
    }

    // ── Tape snapshot ──────────────────────────────────────────────────────
    private object? BuildTapeSnapshot()
    {
        List<(double price, double vol, string side)> tapeCopy;
        double barBuyVol, barSellVol;
        int barBuyCount, barSellCount;
        lock (_lock)
        {
            tapeCopy = new List<(double price, double vol, string side)>(_tapeBuffer);
            barBuyVol = _barBuyVol;
            barSellVol = _barSellVol;
            barBuyCount = _barBuyCount;
            barSellCount = _barSellCount;
        }

        if (tapeCopy.Count == 0 && barBuyCount == 0 && barSellCount == 0)
            return null;

        var buyVol = tapeCopy.Where(t => t.side == "buy").Sum(t => t.vol);
        var sellVol = tapeCopy.Where(t => t.side == "sell").Sum(t => t.vol);
        var buyCnt = tapeCopy.Count(t => t.side == "buy");
        var sellCnt = tapeCopy.Count(t => t.side == "sell");
        var totalVol = buyVol + sellVol;
        var tradeCount = buyCnt + sellCnt;

        var largest = tapeCopy.OrderByDescending(t => t.vol).First();

        return new
        {
            // ── Historical window (last MAX_TAPE_TRADES prints of this bar).
            //    Kept exactly as it was so old track-record rows stay comparable.
            trades = tapeCopy.Select(t => (object)new { p = t.price, q = Math.Round(t.vol, 1), s = t.side }).ToList(),
            buy_vol = Math.Round(buyVol, 1),
            sell_vol = Math.Round(sellVol, 1),
            buy_count = buyCnt,
            sell_count = sellCnt,
            total_vol = Math.Round(totalVol, 1),
            net_vol = Math.Round(buyVol - sellVol, 1),
            avg_trade_size = tradeCount > 0 ? Math.Round(totalVol / (double)tradeCount, 1) : 0,
            largest_trade = Math.Round(largest.vol, 1),
            largest_trade_side = largest.side,
            largest_trade_price = largest.price,
            buy_count_pct = tradeCount > 0 ? Math.Round((double)buyCnt / tradeCount * 100, 0) : 0,
            sell_count_pct = tradeCount > 0 ? Math.Round((double)sellCnt / tradeCount * 100, 0) : 0,

            // ── NEW in schema 2: transparency about the window + TRUE bar totals.
            prints_sampled = tapeCopy.Count,
            sample_window = $"last_{MAX_TAPE_TRADES}_trades_of_current_bar",
            bar_buy_vol = Math.Round(barBuyVol, 1),
            bar_sell_vol = Math.Round(barSellVol, 1),
            bar_net_vol = Math.Round(barBuyVol - barSellVol, 1),
            bar_trade_count = barBuyCount + barSellCount,
            bar_buy_count = barBuyCount,
            bar_sell_count = barSellCount
        };
    }
}
