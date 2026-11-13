import CDP from 'chrome-remote-interface';

async function main() {
  const targets = await CDP.List({ host: 'localhost', port: 9222 });
  const chart = targets.find(t => t.url && t.url.includes('tradingview.com/chart')) || targets[0];
  const client = await CDP({ host: 'localhost', port: 9222, target: chart.id });
  await client.Runtime.enable();
  const e = async (expr) => {
    const r = await client.Runtime.evaluate({ expression: expr, returnByValue: true, timeout: 20000 });
    if (r.result.exceptionDetails) return 'ERR: ' + r.result.exceptionDetails.text;
    return r.result.value;
  };

  const readState = () => e(`(function(){
    var wv=window.TradingViewApi._activeChartWidgetWV.value();
    var iv=wv.interval();
    var ms=wv._chartWidget.model().mainSeries();
    var items=ms.bars()._items;
    return JSON.stringify({
      interval: iv && (iv.value?iv.value():String(iv)),
      bars: items.length,
      lastBarT: items.length? items[items.length-1].value[0] : null
    });
  })()`);

  console.log('ANTES:', await readState());
  for (const tf of ['5', '30', '60']) {
    await client.Runtime.evaluate({
      expression: `window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.chart().setResolution("${tf}",function(){})`,
      awaitPromise: false,
    });
    let state = null;
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 1000));
      const s = await readState();
      try { state = JSON.parse(s); } catch (e) { state = { raw: s }; }
      if (state.interval === tf) break;
    }
    console.log(`tras setResolution("${tf}"):`, JSON.stringify(state));
  }
  client.close();
  console.log('DONE');
}
main().catch(e => console.log('FATAL:', e.message));
