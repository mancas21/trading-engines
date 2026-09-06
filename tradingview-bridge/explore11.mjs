import CDP from 'chrome-remote-interface';

async function main() {
  const targets = await CDP.List({host:'localhost',port:9222});
  const t = targets.find(x => x.title && x.title.includes('TradingView'));
  const c = await CDP({host:'localhost',port:9222,target:t.id});
  await c.Runtime.enable();
  const e = async (x) => {
    const r = await c.Runtime.evaluate({expression:x, returnByValue:true, timeout:8000});
    if (r.result.exceptionDetails) return 'ERR: ' + r.result.exceptionDetails.text;
    return r.result.value;
  };

  // 1. Get bars via _items
  console.log('=== Bar Data (last 10) ===');
  const bars = await e(`(function(){
    var ms = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries();
    var b = ms.bars();
    var items = b._items;
    if(!items || !items.length) return 'no items';
    var count = items.length;
    var recent = [];
    for(var i=Math.max(0,count-10);i<count;i++){
      var v = items[i];
      recent.push({
        t: v.time_v || v.t,
        o: items[i][1],
        h: items[i][2],
        l: items[i][3],
        c: items[i][4],
        v: items[i][5]
      });
    }
    return JSON.stringify({count:count, recent:recent});
  })()`);
  console.log('Bars:', bars);

  // 2. Get drawings from the chart
  console.log('\n=== Chart drawings ===');
  const drawings = await e(`(function(){
    try {
      var wv = window.TradingViewApi._activeChartWidgetWV.value();
      var drawings = wv._chartWidget.model().mainSeries()._graphics;
      if(!drawings) return 'no graphics';
      var keys = Object.keys(drawings).join(',').substring(0,200);
      return JSON.stringify({keys: keys});
    } catch(e) { return e.message; }
  })()`);
  console.log('Graphics:', drawings);

  // 3. Get indicator values via plot API
  console.log('\n=== Indicator Values ===');
  const vals = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV.value();
    var sources = wv._chartWidget.model().model().dataSources();
    var out = {};
    for(var i=3;i<9;i++){ // skip first 3 non-indicators
      var s = sources[i];
      try {
        var name = (s.metaInfo().description || s.metaInfo().shortName || 'src-'+i).substring(0,50);
        // Check what properties are on the source
        var keys = Object.keys(s).filter(k=>k[0]!=='_').join(',');
        out[name] = {keys: keys.substring(0,100)};
      } catch(e) { out['src-'+i] = e.message.substring(0,40); }
    }
    return JSON.stringify(out);
  })()`);
  console.log('Source keys:', vals);

  // 4. Try to access study values via the chart widget API
  console.log('\n=== Chart widget studies ===');
  const studies = await e(`(function(){
    try {
      var wv = window.TradingViewApi._activeChartWidgetWV.value();
      var chart = wv._chartWidget.chart();
      var allStudies = chart.getAllStudies();
      if(!allStudies || !allStudies.length) return 'no studies via chart()';
      var out = [];
      for(var i=0;i<allStudies.length;i++){
        var st = allStudies[i];
        var keys = Object.keys(st).filter(k=>k[0]!=='_').join(',');
        out.push({id:st.id, name:st.name, keys:keys.substring(0,100)});
      }
      return JSON.stringify(out);
    } catch(e) { return e.message; }
  })()`);
  console.log('Chart studies:', studies);

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
