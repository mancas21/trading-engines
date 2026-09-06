import CDP from 'chrome-remote-interface';

let client;

async function main() {
  const targets = await CDP.List({host:'localhost',port:9222});
  const t = targets.find(x => x.title && x.title.includes('TradingView'));
  client = await CDP({host:'localhost',port:9222,target:t.id});
  await client.Runtime.enable();
  const e = async (x) => {
    const r = await client.Runtime.evaluate({expression:x, returnByValue:true, timeout:5000});
    if (r.result.exceptionDetails) return 'ERR: ' + r.result.exceptionDetails.text;
    return r.result.value;
  };

  // Try activeChartWidget
  console.log('=== Trying activeChartWidget ===');
  const r1 = await e(`(function(){
    var c = window._exposed_chartWidgetCollection;
    if(!c) return 'no collection';
    var w = c.activeChartWidget();
    if(!w) return 'no active widget';
    try {
      return JSON.stringify({
        sym: w.symbol(),
        res: w.resolution(),
        tf: w.chart().resolution()
      });
    } catch(err) { return err.message; }
  })()`);
  console.log('Result:', r1);

  // Try getting bars
  console.log('\n=== Bars ===');
  const r2 = await e(`(function(){
    var w = window._exposed_chartWidgetCollection.activeChartWidget();
    return JSON.stringify(w.chart().symbol());
  })()`);
  console.log('Symbol:', r2);

  // Try to get visible bars
  const r3 = await e(`(function(){
    try {
      var w = window._exposed_chartWidgetCollection.activeChartWidget();
      var s = w.activeChart();
      var bars = s._visibleBars;
      if(!bars) return 'no bars';
      return 'bars found: length=' + bars.length;
    } catch(e) { return e.message; }
  })()`);
  console.log('Visible bars:', r3);

  // Try mainSeries
  const r4 = await e(`(function(){
    try {
      var c = window._exposed_chartWidgetCollection;
      var w = c.activeChartWidget();
      var ch = w.chart();
      return JSON.stringify({
        sym: ch.symbol(),
        tf: ch.resolution(),
        hasMainSeries: !!ch._mainSeries
      });
    } catch(e) { return e.message; }
  })()`);
  console.log('Chart:', r4);

  // Try getting studies
  console.log('\n=== Studies ===');
  const r5 = await e(`(function(){
    try {
      var c = window._exposed_chartWidgetCollection;
      var w = c.activeChartWidget();
      var ch = w.chart();
      var studies = ch.getAllStudies();
      if(!studies || !studies.length) return 'no studies';
      return JSON.stringify(studies.map(s => s.name || s.id));
    } catch(e) { return e.message; }
  })()`);
  console.log('Studies:', r5);

  client.close();
}

main().catch(e => console.log('FATAL:', e.message));
