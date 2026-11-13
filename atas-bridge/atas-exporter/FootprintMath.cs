using System;
using System.Collections.Generic;
using System.Linq;
using ATAS.Indicators;

namespace AtasExporter;

/// <summary>
/// Footprint maths shared by the LiveExporter (which publishes it) and the
/// LiveOrderFlow indicator (which draws it).
///
/// Deliberately dependency-free and side-effect free so it can be reasoned
/// about — and unit-tested — without an ATAS instance.
/// </summary>
internal static class FootprintMath
{
    /// <summary>One price level of a candle's footprint.</summary>
    internal sealed class Level
    {
        public decimal Price;
        public decimal Volume;
        public decimal Bid;
        public decimal Ask;
        public int Ticks;

        /// <summary>(bid - ask) / (bid + ask). Negative ⇒ aggressive sellers.</summary>
        public double ImbalanceRatio;
        public bool StackedBuy;
        public bool StackedSell;

        public decimal Delta => Ask - Bid;
    }

    /// <summary>Value area computed from a set of footprint levels.</summary>
    internal sealed class ValueAreaResult
    {
        public decimal Poc;
        public decimal ValueAreaHigh;
        public decimal ValueAreaLow;
        public decimal TotalVolume;
        public double CoveredPercent;
        public int LevelCount;
    }

    /// <summary>
    /// Read the footprint of one candle.
    ///
    /// GetAllPriceLevels() can legitimately return nothing (chart not in
    /// footprint mode, history trimmed, candle without trades) and can throw on
    /// odd states — neither is an error, it just means there is nothing to
    /// publish for that bar. An empty list is the answer, never an exception.
    /// </summary>
    internal static List<Level> BuildLevels(IndicatorCandle candle, int stackLevels, double stackRatio)
    {
        var levels = new List<Level>();
        if (candle == null)
            return levels;

        try
        {
            var all = candle.GetAllPriceLevels()?.ToList();
            if (all == null)
                return levels;

            foreach (var l in all)
            {
                var bid = l.Bid;
                var ask = l.Ask;
                var total = bid + ask;
                levels.Add(new Level
                {
                    Price = l.Price,
                    Volume = l.Volume,
                    Bid = bid,
                    Ask = ask,
                    Ticks = l.Ticks,
                    ImbalanceRatio = total > 0 ? (double)((bid - ask) / total) : 0
                });
            }

            MarkStackedImbalance(levels, stackLevels, stackRatio);
        }
        catch
        {
            levels.Clear();
        }

        return levels;
    }

    /// <summary>
    /// Flag stacked imbalance: N consecutive price levels where the same side
    /// dominates by at least <paramref name="stackRatio"/>.
    ///   • Ask volume dominant (aggressive buyers)  → stacked_buy
    ///   • Bid volume dominant (aggressive sellers) → stacked_sell
    /// Levels are sorted by price so "consecutive" means adjacent in price.
    /// </summary>
    internal static void MarkStackedImbalance(List<Level> levels, int stackLevels, double stackRatio)
    {
        if (levels == null || levels.Count == 0)
            return;

        var sorted = levels.OrderBy(l => l.Price).ToList();
        var need = Math.Max(2, stackLevels);
        var ratio = Math.Max(1.0, stackRatio);

        var buyRun = 0;
        var sellRun = 0;

        for (var i = 0; i < sorted.Count; i++)
        {
            var l = sorted[i];
            var buyQualifies = l.Ask > 0 && l.Bid > 0 && (double)(l.Ask / l.Bid) >= ratio;
            var sellQualifies = l.Bid > 0 && l.Ask > 0 && (double)(l.Bid / l.Ask) >= ratio;

            buyRun = buyQualifies ? buyRun + 1 : 0;
            sellRun = sellQualifies ? sellRun + 1 : 0;

            if (buyRun >= need)
            {
                // Mark the whole run, not just its last element.
                for (var j = i - need + 1; j <= i; j++)
                    sorted[j].StackedBuy = true;
            }
            if (sellRun >= need)
            {
                for (var j = i - need + 1; j <= i; j++)
                    sorted[j].StackedSell = true;
            }
        }
    }

    /// <summary>
    /// Value area from a set of levels (already summed per price).
    /// POC is the highest-volume level; the area grows from the POC by taking
    /// the more voluminous neighbour until <paramref name="percent"/> of the
    /// traded volume is covered.
    /// </summary>
    internal static ValueAreaResult ComputeValueArea(IReadOnlyCollection<Level> levels, int percent)
    {
        var result = new ValueAreaResult();
        if (levels == null || levels.Count == 0)
            return result;

        var byPrice = new SortedDictionary<decimal, decimal>();
        foreach (var l in levels)
        {
            byPrice.TryGetValue(l.Price, out var cur);
            byPrice[l.Price] = cur + l.Volume;
        }

        if (byPrice.Count == 0)
            return result;

        var prices = byPrice.Keys.ToList();
        var totalVol = byPrice.Values.Sum();

        var pocIdx = 0;
        var bestVol = decimal.MinValue;
        for (var i = 0; i < prices.Count; i++)
        {
            if (byPrice[prices[i]] > bestVol)
            {
                bestVol = byPrice[prices[i]];
                pocIdx = i;
            }
        }

        var targetPct = percent > 0 ? percent : 70;
        var target = totalVol * targetPct / 100m;
        var acc = byPrice[prices[pocIdx]];
        var lo = pocIdx;
        var hi = pocIdx;

        while (acc < target && (lo > 0 || hi < prices.Count - 1))
        {
            var volBelow = lo > 0 ? byPrice[prices[lo - 1]] : decimal.MinValue;
            var volAbove = hi < prices.Count - 1 ? byPrice[prices[hi + 1]] : decimal.MinValue;

            if (volAbove >= volBelow)
            {
                hi++;
                acc += volAbove;
            }
            else
            {
                lo--;
                acc += volBelow;
            }
        }

        result.Poc = prices[pocIdx];
        result.ValueAreaHigh = prices[hi];
        result.ValueAreaLow = prices[lo];
        result.TotalVolume = totalVol;
        result.CoveredPercent = totalVol > 0 ? Math.Round((double)(acc / totalVol) * 100, 1) : 0;
        result.LevelCount = byPrice.Count;
        return result;
    }
}
