using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Drawing;
using System.Globalization;
using System.Linq;
using ATAS.Indicators;
using OFT.Rendering.Context;
using OFT.Rendering.Tools;

namespace AtasExporter;

/// <summary>
/// LiveOrderFlow — draws the footprint's value area, point of control and
/// per-bar delta straight on the price chart.
///
/// Everything here is drawn with custom rendering rather than data series, on
/// purpose: a data series shares one vertical scale with everything else in
/// the panel, and a price (POC, value area) and a volume (delta) cannot live
/// on the same scale. Custom drawing keeps the price geometry exact and lets
/// the delta ride along as text.
///
/// It is strictly a VIEW. It writes no files, talks to no network and never
/// affects the LiveExporter's data feed.
/// </summary>
[DisplayName("Live Order Flow")]
[Category("Export")]
public class LiveOrderFlowIndicator : Indicator
{
    // ── Cached per-bar geometry (filled in OnCalculate, read in OnRender) ──
    private decimal[] _poc = Array.Empty<decimal>();
    private decimal[] _vah = Array.Empty<decimal>();
    private decimal[] _val = Array.Empty<decimal>();
    private decimal[] _delta = Array.Empty<decimal>();
    private decimal[] _high = Array.Empty<decimal>();
    private decimal[] _low = Array.Empty<decimal>();

    private List<FootprintMath.Level> _liveLevels = new();
    private readonly ValueDataSeries _pocSeries;

    private readonly RenderFont _font = new("Arial", 9);
    private readonly RenderFont _fontSmall = new("Arial", 8);
    private readonly RenderStringFormat _format = new()
    {
        Alignment = StringAlignment.Center,
        LineAlignment = StringAlignment.Center
    };
    private readonly RenderStringFormat _formatLeft = new()
    {
        Alignment = StringAlignment.Near,
        LineAlignment = StringAlignment.Near
    };

    // ── Settings ───────────────────────────────────────────────────────────
    [Display(Name = "Show value area", GroupName = "Visual", Description = "Fill the value area band (value area high → low) behind the candles.")]
    public bool ShowValueArea { get; set; } = true;

    [Display(Name = "Show POC line", GroupName = "Visual", Description = "Draw the point-of-control line (highest volume price of each bar).")]
    public bool ShowPocLine
    {
        get => _showPocLine;
        set
        {
            _showPocLine = value;
            if (_pocSeries != null)
                _pocSeries.VisualType = value ? VisualMode.Line : VisualMode.Hide;
        }
    }
    private bool _showPocLine = true;

    [Display(Name = "Show delta labels", GroupName = "Visual", Description = "Print the per-bar delta above/below each candle.")]
    public bool ShowDeltaLabels { get; set; } = true;

    [Display(Name = "Show imbalance markers", GroupName = "Visual", Description = "Mark the stacked-imbalance levels of the live bar.")]
    public bool ShowImbalanceMarkers { get; set; } = true;

    [Display(Name = "Show info box", GroupName = "Visual", Description = "Print a live summary of the active bar in the corner of the chart.")]
    public bool ShowInfoBox { get; set; } = true;

    [Display(Name = "POC color", GroupName = "Colors")]
    public Color PocColor
    {
        get => _pocSeries?.Color ?? Color.Orange;
        set { if (_pocSeries != null) _pocSeries.Color = value; }
    }

    [Display(Name = "POC width", GroupName = "Colors")]
    public int PocWidth
    {
        get => _pocSeries?.Width ?? 1;
        set { if (_pocSeries != null) _pocSeries.Width = value; }
    }

    [Display(Name = "Value area color", GroupName = "Colors", Description = "Fill colour of the value area band.")]
    public Color ValueAreaColor { get; set; } = Color.FromArgb(45, 70, 130, 255);

    [Display(Name = "Positive delta color", GroupName = "Colors")]
    public Color PositiveDeltaColor { get; set; } = Color.FromArgb(0, 160, 90);

    [Display(Name = "Negative delta color", GroupName = "Colors")]
    public Color NegativeDeltaColor { get; set; } = Color.FromArgb(200, 60, 60);

    [Display(Name = "Imbalance buy color", GroupName = "Colors", Description = "Colour of stacked BUY imbalances (aggressive buyers).")]
    public Color StackedBuyColor { get; set; } = Color.FromArgb(0, 200, 120);

    [Display(Name = "Imbalance sell color", GroupName = "Colors", Description = "Colour of stacked SELL imbalances (aggressive sellers).")]
    public Color StackedSellColor { get; set; } = Color.FromArgb(230, 70, 70);

    [Display(Name = "Text color", GroupName = "Colors")]
    public Color TextColor { get; set; } = Color.Gray;

    [Display(Name = "Stacked imbalance levels", GroupName = "Calculation", Description = "Consecutive price levels needed to flag a stacked imbalance.")]
    [Range(2, 10)]
    public int StackLevels { get; set; } = 3;

    [Display(Name = "Stacked imbalance ratio", GroupName = "Calculation", Description = "Side volume must exceed the other by this factor to qualify as an imbalanced level.")]
    [Range(1.0, 10.0)]
    public double StackRatio { get; set; } = 2.0;

    [Display(Name = "Min bar width for delta labels", GroupName = "Calculation", Description = "Below this bar width (px) the per-bar delta labels are hidden — below it they overlap and are unreadable.")]
    [Range(0, 60)]
    public int MinBarWidthForDeltaLabels { get; set; } = 8;

    [Display(Name = "Info box offset (px)", GroupName = "Visual", Description = "Vertical offset of the info box from the top of the chart, to clear the OHLC legend.")]
    [Range(0, 300)]
    public int InfoBoxOffset { get; set; } = 46;

    public LiveOrderFlowIndicator()
        : base(true)
    {
        // The POC lives in a REAL data series, not in OnRender. Two reasons:
        //  1. ATAS renders and scales it natively — the price-to-pixel mapping
        //     is the platform's, not mine.
        //  2. A series left at 0 (the default) POISONS the price-chart
        //     auto-scale: the chart stretched to ~26x the real price range
        //     because a phantom 0 was inside the scale. Feeding the series
        //     real prices fixes the scale AND draws the line.
        _pocSeries = new ValueDataSeries("poc");
        _pocSeries.Color = Color.Orange;
        _pocSeries.Width = 1;
        _pocSeries.VisualType = VisualMode.Line;
        DataSeries[0] = _pocSeries;

        EnableCustomDrawing = true;
        SubscribeToDrawingEvents(DrawingLayouts.Final);
    }

    private static readonly string _debugLog = @"C:\Users\Lenovo\trading-engines\atas-bridge\_orderflow_debug.log";

    /// <summary>
    /// Diagnostics go to a file, never to the chart. The indicator must never
    /// be able to take the platform down because of a logging failure.
    /// </summary>
    private void Diag(string msg)
    {
        try
        {
            File.AppendAllText(_debugLog, $"[{DateTime.Now:HH:mm:ss.fff}] {msg}\n");
        }
        catch { }
    }

    protected override void OnInitialize()
    {
        base.OnInitialize();
        Diag($"OnInitialize instrument={InstrumentInfo?.Instrument} panel='{Panel}'");
    }

    protected override void OnApplyDefaultColors()
    {
        base.OnApplyDefaultColors();

        if (ChartInfo == null)
            return;

        TextColor = ChartInfo.ColorsStore.AxisTextColor;
    }

    // ── Calculation ────────────────────────────────────────────────────────
    protected override void OnCalculate(int bar, decimal value)
    {
        // GetCandle throws on an invalid index, so guard instead of relying on
        // the exception (see ATAS_NOTES.md).
        if (bar < 0 || bar >= CurrentBar)
            return;

        EnsureCapacity(CurrentBar);

        IndicatorCandle candle;
        try { candle = GetCandle(bar); }
        catch { return; }
        if (candle == null)
            return;

        _delta[bar] = candle.Delta;
        _high[bar] = candle.High;
        _low[bar] = candle.Low;

        // ATAS precomputes both of these per candle, so this is O(1) — no
        // footprint iteration needed for the historical series.
        _poc[bar] = candle.MaxVolumePriceInfo?.Price ?? 0;
        _vah[bar] = candle.ValueArea?.ValueAreaHigh ?? 0;
        _val[bar] = candle.ValueArea?.ValueAreaLow ?? 0;

        // BUT those precomputed fields are NOT populated for every historical
        // bar: some come back with a null MaxVolumePriceInfo / ValueArea, and
        // a zero there silently breaks the polylines (the POC line stopped
        // mid-chart). Fall back to deriving them from the footprint levels —
        // the same thing the exporter does for the closed bars.
        if (_poc[bar] <= 0 || _vah[bar] <= 0)
        {
            var levels = FootprintMath.BuildLevels(candle, StackLevels, StackRatio);
            if (levels.Count > 0)
            {
                if (_poc[bar] <= 0)
                {
                    var candidate = levels.OrderByDescending(l => l.Volume).First().Price;
                    // SANITY GUARD. Some historical bars carry a corrupt
                    // footprint level (a price ~0.25 on an instrument trading
                    // at ~0.0044). Feeding that into a price series stretched
                    // the chart's auto-scale by 60x — the candles became an
                    // invisible sliver. A POC outside its own bar's range is
                    // garbage; refuse it.
                    _poc[bar] = IsSanePrice(candidate, candle) ? candidate : candle.Close;
                }

                if (_vah[bar] <= 0)
                {
                    var va = FootprintMath.ComputeValueArea(levels, 70);
                    if (IsSanePrice(va.ValueAreaHigh, candle) && IsSanePrice(va.ValueAreaLow, candle))
                    {
                        _vah[bar] = va.ValueAreaHigh;
                        _val[bar] = va.ValueAreaLow;
                    }
                    else
                    {
                        _vah[bar] = candle.High;
                        _val[bar] = candle.Low;
                    }
                }
            }
        }

        // Same guard for the precomputed values: ATAS' own MaxVolumePriceInfo
        // can be nonsense on a corrupt bar too.
        if (!IsSanePrice(_poc[bar], candle))
            _poc[bar] = candle.Close;
        if (!IsSanePrice(_vah[bar], candle) || !IsSanePrice(_val[bar], candle))
        {
            _vah[bar] = candle.High;
            _val[bar] = candle.Low;
        }

        // Feed the series a real price: the POC when there is one, the close
        // otherwise. A 0 here would drag the price auto-scale down.
        _pocSeries[bar] = _poc[bar] > 0 ? _poc[bar] : candle.Close;

        // The live bar's levels are needed for the imbalance markers.
        if (bar == CurrentBar - 1)
            _liveLevels = FootprintMath.BuildLevels(candle, StackLevels, StackRatio);
    }

    /// <summary>
    /// A price that could belong to this bar. Rejects the corrupt footprint
    /// levels that show up on some historical bars and would otherwise wreck
    /// the chart's auto-scale.
    /// </summary>
    private static bool IsSanePrice(decimal price, IndicatorCandle candle)
    {
        if (price <= 0 || candle == null)
            return false;

        var low = candle.Low;
        var high = candle.High;

        // Allow a tick of slack: the POC can sit exactly on a level ATAS
        // rounds differently from the candle's high/low.
        var slack = (high - low) * 0.0001m + 0.00000001m;

        return price >= low - slack && price <= high + slack;
    }

    protected override void OnFinishRecalculate()
    {
        base.OnFinishRecalculate();
    }

    private void EnsureCapacity(int size)
    {
        if (_poc.Length >= size)
            return;

        var n = Math.Max(size, 256);
        Array.Resize(ref _poc, n);
        Array.Resize(ref _vah, n);
        Array.Resize(ref _val, n);
        Array.Resize(ref _delta, n);
        Array.Resize(ref _high, n);
        Array.Resize(ref _low, n);
    }

    // ── Rendering ──────────────────────────────────────────────────────────
    protected override void OnRender(RenderContext context, DrawingLayouts layout)
    {
        if (ChartInfo == null || context == null)
            return;

        var pc = ChartInfo.PriceChartContainer;
        if (pc == null)
            return;

        var first = Math.Max(FirstVisibleBarNumber, 0);
        var last = Math.Min(LastVisibleBarNumber, CurrentBar - 1);
        if (last < first)
            return;

        try
        {
            if (ShowValueArea)
                DrawValueArea(context, pc, first, last);

            if (ShowImbalanceMarkers)
                DrawImbalanceMarkers(context, pc, last);

            if (ShowDeltaLabels)
                DrawDeltaLabels(context, pc, first, last);

            if (ShowInfoBox)
                DrawInfoBox(context);
        }
        catch (Exception ex)
        {
            // A rendering failure must never take the chart down with it.
            Diag($"OnRender EXCEPTION: {ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>Fill the value-area band as a polygon: VAH forward, VAL back.</summary>
    private void DrawValueArea(RenderContext context, IChartContainer pc, int first, int last)
    {
        var top = new List<Point>();
        var bottom = new List<Point>();

        for (var bar = first; bar <= last; bar++)
        {
            var vah = _vah[bar];
            var val = _val[bar];

            if (vah <= 0 || val <= 0)
            {
                FillBand(context, top, bottom);
                top.Clear();
                bottom.Clear();
                continue;
            }

            var x = pc.GetXByBar(bar, false);
            top.Add(new Point(x, pc.GetYByPrice(vah, false)));
            bottom.Add(new Point(x, pc.GetYByPrice(val, false)));
        }

        FillBand(context, top, bottom);
    }

    private void FillBand(RenderContext context, List<Point> top, List<Point> bottom)
    {
        if (top.Count < 2 || bottom.Count < 2)
            return;

        var poly = new List<Point>(top);
        for (var i = bottom.Count - 1; i >= 0; i--)
            poly.Add(bottom[i]);
        poly.Add(top[0]); // close the polygon

        context.FillPolygon(ValueAreaColor, poly.ToArray());
    }

    /// <summary>Short horizontal ticks at the stacked-imbalance levels of the live bar.</summary>

    /// <summary>Short horizontal ticks at the stacked-imbalance levels of the live bar.</summary>
    private void DrawImbalanceMarkers(RenderContext context, IChartContainer pc, int liveBar)
    {
        if (_liveLevels == null || _liveLevels.Count == 0 || liveBar < 0)
            return;

        var x = pc.GetXByBar(liveBar, false);
        var barWidth = (int)Math.Max(2, (double)pc.BarsWidth);

        foreach (var l in _liveLevels)
        {
            if (!l.StackedBuy && !l.StackedSell)
                continue;

            var color = l.StackedBuy ? StackedBuyColor : StackedSellColor;
            var pen = new RenderPen(color, 2);
            var y = pc.GetYByPrice(l.Price, false);
            context.DrawLine(pen, x - barWidth, y, x + barWidth, y);
        }
    }

    private void DrawDeltaLabels(RenderContext context, IChartContainer pc, int first, int last)
    {
        if (ChartInfo == null)
            return;

        var barWidth = (double)pc.BarsWidth;
        if (barWidth < MinBarWidthForDeltaLabels)
            return; // too dense to be readable

        for (var bar = first; bar <= last; bar++)
        {
            var delta = _delta[bar];
            if (delta == 0)
                continue;

            var text = ChartInfo.TryGetMinimizedVolumeString(delta);
            if (string.IsNullOrEmpty(text))
                continue;

            var size = context.MeasureString(text, _fontSmall);
            var x = pc.GetXByBar(bar, false) - size.Width / 2;

            var y = delta > 0
                ? pc.GetYByPrice(_high[bar], false) - size.Height - 2
                : pc.GetYByPrice(_low[bar], false) + 2;

            var color = delta > 0 ? PositiveDeltaColor : NegativeDeltaColor;
            context.DrawString(text, _fontSmall, color, x, y, _format);
        }
    }

    private void DrawInfoBox(RenderContext context)
    {
        var bar = CurrentBar - 1;
        if (bar < 0 || ChartInfo == null)
            return;

        var lines = new List<string>
        {
            $"{InstrumentInfo?.Instrument ?? "?"}  {ChartInfo.TimeFrame}",
            $"Δ {ChartInfo.TryGetMinimizedVolumeString(_delta[bar])}"
        };

        if (_poc[bar] > 0)
            lines.Add($"POC {ChartInfo.GetPriceString(_poc[bar])}");

        if (_vah[bar] > 0 && _val[bar] > 0)
            lines.Add($"VA  {ChartInfo.GetPriceString(_val[bar])} - {ChartInfo.GetPriceString(_vah[bar])}");

        var stackedBuy = _liveLevels.Count(l => l.StackedBuy);
        var stackedSell = _liveLevels.Count(l => l.StackedSell);
        if (stackedBuy + stackedSell > 0)
            lines.Add($"Stacked  buy {stackedBuy} / sell {stackedSell}");

        var area = ChartArea;
        var y = area.Top + InfoBoxOffset;
        var x = area.Left + 8;

        foreach (var line in lines)
        {
            context.DrawString(line, _font, TextColor, x, y, _formatLeft);
            y += context.MeasureString(line, _font).Height + 1;
        }
    }
}
