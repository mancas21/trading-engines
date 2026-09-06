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

  // Get symbol
  const sym = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    return wv.value()._chartWidget.chart().symbol();
  })()`);
  console.log('Symbol:', sym);

  // Get resolution
  const res = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    var ms = wv.value()._chartWidget.model().mainSeries();
    return ms.interval().value();
  })()`);
  console.log('Resolution:', res);

  // Get bars - try .slice
  console.log('\n=== Bars count ===');
  const barsInfo = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    var ms = wv.value()._chartWidget.model().mainSeries();
    var bars = ms.bars();
    if(typeof bars === 'object' && bars !== null) {
      var keys = Object.keys(bars).join(',').substring(0,100);
      var len = bars.length || bars.size || 'no length';
      var hasLast = typeof bars.last;
      return JSON.stringify({keys: keys, length: len, hasLast: hasLast});
    }
    return typeof bars;
  })()`);
  console.log('Bars info:', barsInfo);

  // Try getting study plot values
  console.log('\n=== Study values ===');
  const studyVals = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    var sources = wv.value()._chartWidget.model().model().dataSources();
    var out = {};
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      try {
        var name = s.metaInfo().description || s.metaInfo().shortName || '';
        if(!name) name = s.metaInfo().name || 'unnamed-'+i;
        name = name.substring(0,40);
        // Try to get plot values for the last bar
        if(s._plots && s._plots.length) {
          var plotVals = {};
          for(var pi=0;pi<s._plots.length;pi++){
            var plot = s._plots[pi];
            if(plot._id && plot.plot) {
              try {
                plotVals[plot._id] = plot.plot();
              } catch(e) {}
            }
          }
          if(Object.keys(plotVals).length) out[name] = plotVals;
        } else {
          out[name] = '(no plots)';
        }
      } catch(e) { out['src-'+i] = 'ERR: '+e.message.substring(0,30); }
    }
    return JSON.stringify(out);
  })()`);
  console.log('Study values:', studyVals);

  // Now try primitives - check if any table/label exists
  console.log('\n=== Primitives ===');
  const prims = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    var sources = wv.value()._chartWidget.model().model().dataSources();
    var out = {};
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      try {
        var name = (s.metaInfo().description || s.metaInfo().shortName || 'src-'+i).substring(0,40);
        var g = s._graphics;
        if(!g) { out[name] = 'no graphics'; continue; }
        var coll = g._primitivesCollection;
        if(!coll) { out[name] = 'no collection'; continue; }
        var prims = coll._primitives;
        if(!prims) { out[name] = 'no primitives list'; continue; }
        var types = {};
        for(var j=0;j<prims.length;j++){
          var t = prims[j]._type || 'unknown';
          types[t] = (types[t]||0) + 1;
        }
        out[name] = JSON.stringify(types);
      } catch(e) { out['src-'+i] = 'ERR'; }
    }
    return JSON.stringify(out);
  })()`);
  console.log('Primitives:', prims);

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
