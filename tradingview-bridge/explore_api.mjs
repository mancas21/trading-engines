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

  // Resolution
  console.log('TF via interval:', await e('window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().interval().value()'));
  console.log('TF via chart:', await e('try{return window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.chart().resolution();}catch(e){return e.message}'));

  // List data sources
  const ds = await e(`(function(){
    var s = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    var out = [];
    for(var i=0;i<s.length;i++){
      var m = s[i].metaInfo();
      out.push({idx:i, id:s[i].id(), desc:(m?m.description:''), short:(m?m.shortName:''), type:(m?m.originalFormat:'')});
    }
    return JSON.stringify(out);
  })()`);
  console.log('DATA SOURCES:');
  if (ds) JSON.parse(ds).forEach(d => console.log(`  [${d.idx}] ${d.id} | ${(d.desc||'').substring(0,50)} | ${d.short}`));

  // Try to get study values via data window API
  console.log('\nSTUDY VALUES:');
  const sv = await e(`(function(){
    try {
      var wv = window.TradingViewApi._activeChartWidgetWV.value();
      var chart = wv._chartWidget;
      // Try getting last bar study values
      var all = chart.model().mainSeries().bars();
      var last = all[all.length-1];
      // Get study plots
      var s = chart.model().model().dataSources();
      var out = {};
      for(var i=0;i<s.length;i++){
        var m = s[i].metaInfo();
        if(!m || !m.shortName) continue;
        var name = m.shortName || m.description;
        try {
          var plots = s[i]._plots || [];
          var vals = {};
          for(var j=0;j<plots.length;j++){
            try {
              var v = plots[j].plot(last.time_v);
              if(v !== undefined && v !== null) vals[plots[j]._id || j] = +v.toFixed(2);
            }catch(e){}
          }
          if(Object.keys(vals).length) out[name] = vals;
        } catch(e) {}
      }
      return JSON.stringify(out);
    }catch(e){return e.message}
  })()`);
  if (sv && sv.startsWith('{')) {
    const d = JSON.parse(sv);
    for (const [k,v] of Object.entries(d)) console.log(`  ${k.substring(0,40)}:`, JSON.stringify(v));
  } else {
    console.log('  ', sv);
  }

  // Try getting lines/labels from chart
  console.log('\nLINES/LABELS:');
  const lines = await e(`(function(){
    try {
      var prims = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries()._graphics._primitivesCollection._primitives;
      var out = [];
      for(var i=0;i<prims.length;i++){
        var p = prims[i];
        if(p._type === 'hline' || p._type === 'line' || p._type === 'label' || p._type === 'table') {
          out.push({type:p._type, price:p._price, text:p._text, rows:p._rows});
        }
      }
      return JSON.stringify(out);
    }catch(e){return e.message}
  })()`);
  if (lines && lines.startsWith('[')) {
    JSON.parse(lines).forEach(l => console.log(`  ${l.type}: price=${l.price} text=${l.text||''}`));
  } else {
    console.log('  ', lines);
  }

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
