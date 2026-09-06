using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;
using ATAS.Indicators;
using Newtonsoft.Json;

namespace AtasExporter;

[DisplayName("LiveExporter")]
[Category("Export")]
public class LiveExporter : Indicator
{
    private string GetExportFilePath()
    {
        var symbol = InstrumentInfo?.Instrument?.Replace("/", "").Replace(":", "").ToUpper() ?? "DEFAULT";
        return $@"C:\atas-bridge\_live_indicators_{symbol}.json";
    }
    private string _filePath => GetExportFilePath();
    private readonly string _logPath = @"C:\atas-bridge\_exporter_debug.log";
    private readonly object _lock = new();
    private DateTime _lastWrite = DateTime.MinValue;

    // Smart Tape buffer
    private readonly List<(double price, double vol, string side)> _tapeBuffer = new();
    private const int MAX_TAPE_TRADES = 30;
    // Bar-scoping: tracks which bar the tape buffer belongs to. When the live
    // bar rolls over, the buffer is cleared so the snapshot never mixes trades
    // from previous bars (audit finding 2026-08-05).
    private long _tapeBarIndex = -1;

    // ── Bridge alert push (BigTrade + DOM walls → POST /alerta-atas) ──
    // Replaces the manual ATAS-UI webhook config: the indicator itself detects
    // events and pushes them to the bridge in the exact message format the
    // bridge parser expects ("[date] [TICKER]: Ask BigTrade volume greater
    // than X at PRICE (Buy)" / "Best Bid X lots > Y at PRICE").
    [DisplayName("Enable bridge alerts")]
    [Description("POST BigTrade / DOM-wall alerts to the ATAS bridge (/alerta-atas).")]
    [Category("Alerts")]
    public bool EnableAlerts { get; set; } = true;

    [DisplayName("Bridge URL")]
    [Description("Base URL of the ATAS bridge, e.g. http://127.0.0.1:8000")]
    [Category("Alerts")]
    public string BridgeUrl { get; set; } = "http://127.0.0.1:8000";

    [DisplayName("BigTrade min lots")]
    [Description("Single-print volume (base units) that triggers a BigTrade alert.")]
    [Category("Alerts")]
    public decimal BigTradeMinLots { get; set; } = 2000;

    [DisplayName("DOM wall min lots")]
    [Description("Single DOM level volume (base units) that triggers a wall alert.")]
    [Category("Alerts")]
    public decimal WallMinLots { get; set; } = 40000;

    [DisplayName("Alert cooldown (s)")]
    [Description("Minimum seconds before re-alerting the same price level. A persistent wall only re-fires if it grows 25%+.")]
    [Category("Alerts")]
    public int AlertCooldownSeconds { get; set; } = 300;

    private static readonly HttpClient _http = new HttpClient { Timeout = TimeSpan.FromSeconds(4) };
    private readonly List<string> _pendingAlerts = new();
    private readonly Dictionary<string, (DateTime at, double vol)> _alertCooldowns = new();
    private DateTime _warmupUntil = DateTime.UtcNow.AddSeconds(30);
    private DateTime _lastFlush = DateTime.MinValue;
    private DateTime _lastErrLog = DateTime.MinValue;

    private string NormSymbol()
    {
        return (InstrumentInfo?.Instrument ?? "?").Replace("/", "").Replace(":", "").ToUpper();
    }

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
        List<string> batch = null;
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

    public LiveExporter()
        : base(true)
    {
        DataSeries[0] = new ValueDataSeries("vals");
        try
        {
            File.WriteAllText(_logPath, $"[{DateTime.Now:HH:mm:ss}] LiveExporter initialized\n");
        }
        catch { }
    }

    protected override void OnNewTrade(MarketDataArg arg)
    {
        var vol = (double)arg.Volume;
        var price = (double)arg.Price;
        var dir = arg.Direction;
        var isBuy = dir == TradeDirection.Buy;
        var isSell = dir == TradeDirection.Sell;
        var side = isBuy ? "buy" : (isSell ? "sell" : "?");

        lock (_tapeBuffer)
        {
            _tapeBuffer.Add((price, vol, side));
            while (_tapeBuffer.Count > MAX_TAPE_TRADES)
                _tapeBuffer.RemoveAt(0);
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

    protected override void OnCalculate(int bar, decimal value)
    {
        if (bar < 0)
            return;

        // Only export the CURRENT (live) bar. OnCalculate is invoked for every
        // historical bar during chart load/reload — writing those would replay
        // stale historical deltas into the bridge's CVD tracker.
        if (bar != CurrentBar - 1)
            return;

        // Bar-scoped tape: clear the buffer when the live bar rolls over so the
        // tape snapshot reflects ONLY the current bar (audit fix 2026-08-05).
        if (_tapeBarIndex != bar)
        {
            lock (_tapeBuffer)
            {
                _tapeBuffer.Clear();
            }
            _tapeBarIndex = bar;
        }

        var now = DateTime.UtcNow;
        if ((now - _lastWrite).TotalMilliseconds < 300)
            return;

        try
        {
            var candle = GetCandle(bar);
            if (candle == null)
                return;

            DataSeries[0][bar] = candle.Close;

            // ── Footprint ──
            object footprint = null;
            try
            {
                var allLevels = candle.GetAllPriceLevels()?.ToList();
                if (allLevels != null && allLevels.Count > 0)
                {
                    var sortedByVol = allLevels.OrderByDescending(l => l.Volume).Take(10).ToList();
                    // Tail preservation: ensure extreme wick levels are not discarded
                    var extremes = new[] { allLevels.OrderBy(l => l.Price).First(), allLevels.OrderByDescending(l => l.Price).First() };
                    foreach (var ex in extremes)
                        if (!sortedByVol.Any(x => x.Price == ex.Price))
                            sortedByVol.Add(ex);
                    footprint = new
                    {
                        poc = sortedByVol.FirstOrDefault()?.Price ?? 0,
                        value_area_high = candle.ValueArea?.ValueAreaHigh ?? 0,
                        value_area_low = candle.ValueArea?.ValueAreaLow ?? 0,
                        top_levels = sortedByVol.Select(l => new
                        {
                            price = l.Price,
                            volume = l.Volume,
                            bid = l.Bid,
                            ask = l.Ask,
                            delta = l.Ask - l.Bid,
                            ticks = l.Ticks
                        }).ToList(),
                        max_bid_level = candle.MaxBidPriceInfo?.Price ?? 0,
                        max_ask_level = candle.MaxAskPriceInfo?.Price ?? 0
                    };
                }
            }
            catch { }

            // ── DOM snapshot (Smart DOM) ──
            object domSnapshot = null;
            try
            {
                var depthProvider = DataProvider?.MarketDepthInfoProvider;
                if (depthProvider != null)
                {
                    var depthEntries = depthProvider.GetMarketDepthSnapshot()?.ToList();
                    if (depthEntries != null && depthEntries.Count > 0)
                    {
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

                        domSnapshot = new
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
                    }
                }
            }
            catch { }

            // ── Smart Tape snapshot (computed from current buffer) ──
            object tapeSnapshot = null;
            List<(double price, double vol, string side)> tapeCopy;
            lock (_tapeBuffer)
            {
                tapeCopy = new List<(double price, double vol, string side)>(_tapeBuffer);
            }
            if (tapeCopy.Count > 0)
            {
                var buyVol = tapeCopy.Where(t => t.side == "buy").Sum(t => t.vol);
                var sellVol = tapeCopy.Where(t => t.side == "sell").Sum(t => t.vol);
                var buyCnt = tapeCopy.Count(t => t.side == "buy");
                var sellCnt = tapeCopy.Count(t => t.side == "sell");
                var totalVol = buyVol + sellVol;
                var tradeCount = buyCnt + sellCnt;

                var largest = tapeCopy.OrderByDescending(t => t.vol).First();
                tapeSnapshot = new
                {
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
                    sell_count_pct = tradeCount > 0 ? Math.Round((double)sellCnt / tradeCount * 100, 0) : 0
                };
            }

            // ── Serialize ──
            var data = new
            {
                timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                ticker = InstrumentInfo?.Instrument ?? "?",
                bar_index = bar,
                price = new { last = candle.Close, open = candle.Open, high = candle.High, low = candle.Low },
                volume = new { total = candle.Volume, ask = candle.Ask, bid = candle.Bid },
                delta = (double)candle.Delta,
                time = candle.Time.ToString("HH:mm:ss"),
                footprint = footprint,
                dom = domSnapshot,
                tape = tapeSnapshot
            };

            lock (_lock)
            {
                var json = JsonConvert.SerializeObject(data, Formatting.Indented);
                var tempPath = _filePath + ".tmp";
                File.WriteAllText(tempPath, json);
                File.Move(tempPath, _filePath, true);  // atomic on Windows
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
}
