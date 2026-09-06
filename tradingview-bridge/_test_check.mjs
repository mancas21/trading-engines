import CDP from 'chrome-remote-interface';

const targets = await CDP.List({ host: 'localhost', port: 9222 });
console.log(JSON.stringify(targets.map(t => ({ id: t.id.substring(0,8), url: (t.url || '').substring(0,80), title: (t.title || '').substring(0,60) })), null, 2));

const chartTarget = targets.find(t => (t.url || '').includes('tradingview.com/chart'));
if (!chartTarget) { console.log('NO CHART TARGET'); process.exit(1); }

const client = await CDP({ host: 'localhost', port: 9222, target: chartTarget.id });
await client.Runtime.enable();

const { result } = await client.Runtime.evaluate({
  expression: '(function(){try{var wv=window.TradingViewApi._activeChartWidgetWV;return wv?"WV_OK":"WV_NULL"}catch(e){return "ERR:"+e.message}})()',
  returnByValue: true,
  awaitPromise: false,
});
console.log('WV check:', result.value);

const { result: r2 } = await client.Runtime.evaluate({
  expression: '(function(){try{var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();return ms.symbol()}catch(e){return "ERR:"+e.message}})()',
  returnByValue: true,
  awaitPromise: false,
});
console.log('Symbol:', r2.value);

const { result: r3 } = await client.Runtime.evaluate({
  expression: '(function(){try{var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();var last=ms.bars().last();return({sym:ms.symbol(),close:last.value[4]})}catch(e){return({error:e.message})}})()',
  returnByValue: true,
  awaitPromise: false,
});
console.log('Quote:', r3.value);

client.close();
process.exit(0);
