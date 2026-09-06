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

  // Get symbol & resolution via monitor method
  console.log('=== Basic info ===');
  const basic = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    var cw = wv.value()._chartWidget;
    var ms = cw.model().mainSeries();
    return JSON.stringify({
      sym: ms.symbol(),
      res: ms.interval().value(),
      lastClose: ms.bars().last()[4]
    });
  })()`);
  console.log('Basic:', basic);

  // Get last bar
  console.log('\n=== Last bar ===');
  const bar = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    var ms = wv.value()._chartWidget.model().mainSeries();
    var last = ms.bars().last();
    return JSON.stringify({
      t: last.time_v, o: last[1], h: last[2], l: last[3], c: last[4], v: last[5]
    });
  })()`);
  console.log('Bar:', bar);

  // Get a few recent bars
  console.log('\n=== Recent 5 bars ===');
  const bars = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    var ms = wv.value()._chartWidget.model().mainSeries();
    var bars = ms.bars();
    var count = bars.length;
    var recent = [];
    for(var i=Math.max(0,count-5);i<count;i++){
      var b = bars[i];
      recent.push({t:b.time_v, o:b[1], h:b[2], l:b[3], c:b[4], v:b[5]});
    }
    return JSON.stringify({count: count, recent: recent});
  })()`);
  console.log('Bars:', bars);

  // Get studies count and names
  console.log('\n=== Studies ===');
  const studies = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    var sources = wv.value()._chartWidget.model().model().dataSources();
    var out = [];
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      // Try different ways to get name
      try {
        var name = '';
        try { name = s.metaInfo().description || ''; } catch(e2) {}
        if(!name) try { name = s._metaInfo.description || ''; } catch(e2) {}
        if(!name) try { name = s.metaInfo.shortName || ''; } catch(e2) {}
        if(!name) try { name = s.metaInfo.name || ''; } catch(e2) {}
        out.push({idx:i, id: s.id_ || s.id || '', name: (name||'').substring(0,50), hasPlot: !!s._plots});
      } catch(e) { out.push({idx:i, name: 'ERR: '+e.message}); }
    }
    return JSON.stringify(out);
  })()`);
  console.log('Studies:', studies);

  // Get MPS table data
  console.log('\n=== MPS Table ===');
  const mps = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    var sources = wv.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      try {
        var m = s.metaInfo();
        if(m && m.description && m.description.indexOf('Master Pattern Scanner') >= 0){
          var prims = s._graphics && s._graphics._primitivesCollection;
          if(!prims || !prims._primitives) return 'no primitives';
          for(var j=0;j<prims._primitives.length;j++){
            var p = prims._primitives[j];
            if(p._type === 'table'){
              var r = {};
              for(var k=0;k<p._rows;k++){
                var c0 = p._cells && p._cells[k] && p._cells[k][0];
                var c1 = p._cells && p._cells[k] && p._cells[k][1];
                if(c0 && c0._text && c1 && c1._text) r[c0._text] = c1._text;
              }
              return JSON.stringify(r);
            }
          }
          return 'no table found in primitives(' + prims._primitives.length + ')';
        }
      } catch(e) {}
    }
    return 'MPS study not found';
  })()`);
  console.log('MPS:', mps);

  // Also try BookFlow
  console.log('\n=== BookFlow Table ===');
  const bf = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV;
    var sources = wv.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      try {
        var m = s.metaInfo();
        if(m && m.description && m.description.indexOf('BookFlow') >= 0){
          var prims = s._graphics && s._graphics._primitivesCollection;
          if(!prims || !prims._primitives) return 'no primitives';
          for(var j=0;j<prims._primitives.length;j++){
            var p = prims._primitives[j];
            if(p._type === 'table'){
              var r = {};
              for(var k=0;k<p._rows;k++){
                var c0 = p._cells && p._cells[k] && p._cells[k][0];
                var c1 = p._cells && p._cells[k] && p._cells[k][1];
                if(c0 && c0._text && c1 && c1._text) r[c0._text] = c1._text;
              }
              return JSON.stringify(r);
            }
          }
          return 'no table (' + prims._primitives.length + ' primitives)';
        }
      } catch(e) {}
    }
    return 'BookFlow not found';
  })()`);
  console.log('BookFlow:', bf);

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
