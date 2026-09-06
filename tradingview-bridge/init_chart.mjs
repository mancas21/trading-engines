import CDP from 'chrome-remote-interface';

async function main() {
  const targets = await CDP.List({host:'localhost',port:9222});
  const t = targets.find(x => x.title && x.title.includes('TradingView'));
  const c = await CDP({host:'localhost',port:9222,target:t.id});
  await c.Runtime.enable();
  const e = async (x) => {
    const r = await c.Runtime.evaluate({expression:x, returnByValue:true, timeout:5000});
    if (r.result.exceptionDetails) return 'ERR: ' + r.result.exceptionDetails.text;
    return r.result.value;
  };

  // Set symbol
  console.log('Setting symbol to SPCXUSDT.P...');
  await e(`new Promise(function(res,rej){
    window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().setSymbol("BINANCE:SPCXUSDT.P", function(e){if(e)rej(e);else res();});
  })`);
  console.log('Symbol set. Waiting for chart...');
  await new Promise(r => setTimeout(r, 5000));

  // Verify
  const sym = await e('window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().symbol()');
  console.log('Current symbol:', sym);

  // Get last price for thresholds
  const items = await e('window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().bars()._items');
  const lastVal = items[items.length-1].value;
  console.log('Last price:', lastVal[4]);
  console.log('Last close:', lastVal[4]);
  console.log('High 100:', Math.max(...items.slice(-100).map(i=>i.value[2])));
  console.log('Low 100:', Math.min(...items.slice(-100).map(i=>i.value[3])));

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
