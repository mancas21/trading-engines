import CDP from 'chrome-remote-interface';

const MAIN = 'window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries()';
let client;

async function main() {
  const targets = await CDP.List({ host: 'localhost', port: 9222 });
  const chart = targets.find(t => t.url && t.url.includes('tradingview.com/chart')) || targets[0];
  client = await CDP({ host: 'localhost', port: 9222, target: chart.id });
  await client.Runtime.enable();
  const e = async (expr) => {
    const r = await client.Runtime.evaluate({ expression: expr, returnByValue: true, timeout: 20000 });
    if (r.result.exceptionDetails) return 'ERR: ' + (r.result.exceptionDetails.text || 'exception');
    return r.result.value;
  };
  const tf = async () => e(`(function(){try{return ${MAIN}.interval().value();}catch(x){return 'ERR';}})()`);
  const bars = async () => e(`(function(){try{var i=${MAIN}.bars()._items;return i.length;}catch(x){return 'ERR';}})()`);

  const approaches = [
    ['A chart().setResolution("30",cb)', `window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.chart().setResolution("30",function(){})`],
    ['B chart().setResolution(5,cb) enum', `window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.chart().setResolution(5,function(){})`],
    ['C chart().setResolution("1h",cb)', `window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.chart().setResolution("1h",function(){})`],
    ['D activeChart().changeInterval("30")', `window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.chart().changeInterval("30")`],
    ['E model().setResolution("30")', `window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().setResolution("30")`],
    ['F mainSeries().setResolution("30")', `${MAIN}.setResolution("30")`],
  ];

  console.log('inicial tf=' + await tf() + ' bars=' + await bars());
  for (const [name, expr] of approaches) {
    try {
      const r = await e(expr);
      await new Promise(x => setTimeout(x, 5000));
      console.log(`${name} -> ret=${JSON.stringify(r)} | tf=${await tf()} bars=${await bars()}`);
    } catch (err) {
      console.log(`${name} -> EXC ${err.message}`);
    }
  }
}
main().catch(e => console.log('FATAL:', e.message)).finally(() => { try { client && client.close(); } catch { } process.exit(0); });
