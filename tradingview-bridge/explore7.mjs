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

  console.log('Type of activeChartWidget:', await e('typeof window._exposed_chartWidgetCollection.activeChartWidget'));
  console.log('Is function:', await e('typeof window._exposed_chartWidgetCollection.activeChartWidget === "function"'));
  console.log('Keys:', await e('Object.keys(window._exposed_chartWidgetCollection).join(",")'));

  // Try accessing as property
  const sym = await e(`(function(){
    try {
      var coll = window._exposed_chartWidgetCollection;
      var w = coll.activeChartWidget;
      if(!w) return 'widget is null';
      if(typeof w === 'object') {
        return JSON.stringify(Object.keys(w).filter(k=>k[0]!=='_').join(',').substring(0,200));
      }
      return 'type: ' + typeof w;
    } catch(e) { return e.message; }
  })()`);
  console.log('ActiveChartWidget prop:', sym);

  // Try through TradingView.onWidget
  console.log('\nTradingView.onWidget:', await e('typeof window.TradingView.onWidget'));

  const widgetApi = await e(`(function(){
    try {
      // TradingView.onWidget adds a callback when widget is ready
      var api;
      window.TradingView.onWidget(function(w) { api = w; });
      if(!api) return 'no callback yet';
      return JSON.stringify(Object.keys(api).join(',').substring(0,200));
    } catch(e) { return e.message; }
  })()`);
  console.log('onWidget API:', widgetApi);

  // Try ChartApiInstance
  console.log('\nChartApiInstance keys:', await e('Object.keys(window.ChartApiInstance).join(",").substring(0,200)'));

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
