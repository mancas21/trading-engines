import CDP from 'chrome-remote-interface';

let client;

async function connect() {
  const targets = await CDP.List({host:'localhost',port:9222});
  const t = targets.find(x => x.title && x.title.includes('TradingView'));
  if (!t) throw new Error('TV target not found');
  client = await CDP({host:'localhost',port:9222,target:t.id});
  await client.Runtime.enable();
}

async function e(expr) {
  const r = await client.Runtime.evaluate({expression:expr, returnByValue:true, timeout:8000});
  if (r.result.exceptionDetails) return 'ERR: ' + r.result.exceptionDetails.text;
  return r.result.value;
}

async function main() {
  await connect();
  console.log('CONNECTED');

  // === SYMBOL & TF ===
  const sym = await e('window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().symbol()');
  const tf = await e('window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().interval().value()');
  console.log(`\n${sym} | TF: ${tf}`);

  // === LAST BAR ===
  const bar = await e(`(function(){
    var items = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().bars()._items;
    var last = items[items.length-1];
    var v = last.value;
    return JSON.stringify({t: v[0]||last.index, o: v[1], h: v[2], l: v[3], c: v[4], vol: v[5]});
  })()`);
  const b = JSON.parse(bar);
  console.log(`\n📊 ÚLTIMA VELA [${new Date(b.t*1000).toLocaleTimeString()}]`);
  console.log(`O: ${b.o}  H: ${b.h}  L: ${b.l}  C: ${b.c}  Vol: ${b.vol}`);

  // === COMPUTE EMAs & RSI from bars ===
  const calc = await e(`(function(){
    var items = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().bars()._items;
    var closes = [], highs = [], lows = [], vols = [];
    for(var i=0;i<items.length;i++){
      var v = items[i].value;
      closes.push(v[4]);
      highs.push(v[2]);
      lows.push(v[3]);
      vols.push(v[5]);
    }
    var len = closes.length;
    // EMAs
    var ema = function(arr,p){var k=2/(p+1);var r=arr[0];for(var i=1;i<arr.length;i++)r=arr[i]*k+r*(1-k);return r;};
    var e9 = ema(closes.slice(-50),9);
    var e21 = ema(closes.slice(-50),21);
    var e50 = ema(closes.slice(-50),50);
    // RSI
    var gains=[],losses=[];
    for(var i=1;i<closes.length;i++){var d=closes[i]-closes[i-1];gains.push(d>0?d:0);losses.push(d<0?-d:0);}
    var avg=function(arr,n){return arr.slice(-n).reduce((s,v)=>s+v,0)/Math.min(n,arr.length);};
    var rsi14 = (function(){var ag=avg(gains,14),al=avg(losses,14);return al===0?100:100-100/(1+ag/al);})();
    var rsi6 = (function(){var ag=avg(gains,6),al=avg(losses,6);return al===0?100:100-100/(1+ag/al);})();
    // Levels
    var hh = Math.max(...highs.slice(-100));
    var ll = Math.min(...lows.slice(-100));
    var avgVol = vols.slice(-30).reduce((s,v)=>s+v,0)/Math.min(30,vols.length);
    var maxVol = Math.max(...vols.slice(-100));
    var lastVol = vols[vols.length-1];
    // Cross
    var cross = e9 > e21 ? '9>21' : (e9 < e21 ? '9<21' : '9=21');
    var prevE9 = ema(closes.slice(-51,-1),9);
    var prevE21 = ema(closes.slice(-51,-1),21);
    var crossDir = '';
    if(prevE9 < prevE21 && e9 > e21) crossDir = ' 🟢 BULL CROSS';
    else if(prevE9 > prevE21 && e9 < e21) crossDir = ' 🔴 BEAR CROSS';
    return JSON.stringify({
      ema9: +e9.toFixed(2), ema21: +e21.toFixed(2), ema50: +e50.toFixed(2),
      rsi14: +rsi14.toFixed(1), rsi6: +rsi6.toFixed(1),
      high: hh, low: ll, range: +(hh-ll).toFixed(2),
      avgVol: +avgVol.toFixed(0), maxVol: maxVol, lastVol: lastVol,
      cross: cross, crossDir: crossDir,
      bias: e9 > e21 ? 'BULLISH' : 'BEARISH'
    });
  })()`);
  const c = JSON.parse(calc);
  console.log(`\n📐 TÉCNICO`);
  console.log(`EMA9: ${c.ema9}  EMA21: ${c.ema21}  EMA50: ${c.ema50}`);
  console.log(`Cross: ${c.cross}${c.crossDir}`);
  console.log(`RSI14: ${c.rsi14}  RSI6: ${c.rsi6}`);
  console.log(`Rango 100b: $${c.low} - $${c.high} ($${c.range})`);
  console.log(`Vol: ${c.lastVol} | Prom: ${c.avgVol} | Max: ${c.maxVol} | Ratio: ${(c.lastVol/(c.avgVol||1)).toFixed(1)}x`);

  // === MPS TABLE ===
  console.log(`\n📋 MASTER PATTERN SCANNER`);
  const mps = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      try {
        if((s.metaInfo().description||'').indexOf('Master Pattern Scanner')>=0){
          var coll = s._graphics && s._graphics._primitivesCollection;
          if(!coll) return 'no coll';
          var t = coll.dwgtables;
          if(!t || !t._items || !t._items.length) return 'no tables';
          var out = {};
          for(var ti=0;ti<t._items.length;ti++){
            var tbl = t._items[ti];
            if(tbl._type==='dwgtable' && tbl._cells){
              for(var ci=0;ci<tbl._cells.length;ci++){
                var cell = tbl._cells[ci];
                var txt = cell._text || '';
                var tag = cell._tag || '';
                if(txt) out[tag||'cell'+ci] = txt;
              }
            }
          }
          return JSON.stringify(out);
        }
      }catch(e){}
    }
    return 'MPS not found';
  })()`);
  if (mps && !mps.startsWith('ERR') && mps !== 'MPS not found' && mps !== 'no coll' && mps !== 'no tables') {
    try {
      const d = JSON.parse(mps);
      for (const [k, v] of Object.entries(d)) console.log(`  ${k}: ${v}`);
    } catch(e) { console.log('  Raw:', mps.substring(0,200)); }
  } else {
    console.log('  (no table data)');
  }

  // === BOOKFLOW ===
  console.log(`\n📋 BOOKFLOW PRO`);
  const bf = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      try {
        if((s.metaInfo().description||'').indexOf('BookFlow')>=0){
          var coll = s._graphics && s._graphics._primitivesCollection;
          if(!coll) return 'no coll';
          var t = coll.dwgtables;
          if(!t || !t._items || !t._items.length) return 'no tables';
          var out = {};
          for(var ti=0;ti<t._items.length;ti++){
            var tbl = t._items[ti];
            if(tbl._type==='dwgtable' && tbl._cells){
              for(var ci=0;ci<tbl._cells.length;ci++){
                var cell = tbl._cells[ci];
                var txt = cell._text || '';
                var tag = cell._tag || '';
                if(txt) out[tag||'cell'+ci] = txt;
              }
            }
          }
          return JSON.stringify(out);
        }
      }catch(e){}
    }
    return 'BookFlow not found';
  })()`);
  if (bf && !bf.startsWith('ERR') && bf !== 'BookFlow not found' && bf !== 'no coll' && bf !== 'no tables') {
    try {
      const d = JSON.parse(bf);
      for (const [k, v] of Object.entries(d)) console.log(`  ${k}: ${v}`);
    } catch(e) { console.log('  Raw:', bf.substring(0,200)); }
  } else {
    console.log('  (no table data)');
  }

  // === ORDER FLOW SNIPER ===
  console.log(`\n📋 ORDER FLOW SNIPER`);
  const sniper = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      try {
        if((s.metaInfo().description||'').indexOf('Order Flow Sniper')>=0){
          var coll = s._graphics && s._graphics._primitivesCollection;
          if(!coll) return 'no coll';
          var t = coll.dwgtables;
          if(!t || !t._items || !t._items.length) return 'no tables';
          var out = {};
          for(var ti=0;ti<t._items.length;ti++){
            var tbl = t._items[ti];
            if(tbl._type==='dwgtable' && tbl._cells){
              for(var ci=0;ci<tbl._cells.length;ci++){
                var cell = tbl._cells[ci];
                var txt = cell._text || '';
                var tag = cell._tag || '';
                if(txt) out[tag||'cell'+ci] = txt;
              }
            }
          }
          return JSON.stringify(out);
        }
      }catch(e){}
    }
    return 'OFS not found';
  })()`);
  if (sniper && !sniper.startsWith('ERR') && sniper !== 'OFS not found' && sniper !== 'no coll' && sniper !== 'no tables') {
    try {
      const d = JSON.parse(sniper);
      for (const [k, v] of Object.entries(d)) console.log(`  ${k}: ${v}`);
    } catch(e) { console.log('  Raw:', sniper.substring(0,200)); }
  } else {
    console.log('  (no table data)');
  }

  // === ORDERFLOW PRO STRATEGY ===
  console.log(`\n📋 ORDERFLOW PRO STRATEGY`);
  const ofp = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      try {
        if((s.metaInfo().description||'').indexOf('Orderflow Pro')>=0){
          var coll = s._graphics && s._graphics._primitivesCollection;
          if(!coll) return 'no coll';
          var t = coll.dwgtables;
          if(!t || !t._items || !t._items.length) return 'no tables';
          var out = {};
          for(var ti=0;ti<t._items.length;ti++){
            var tbl = t._items[ti];
            if(tbl._type==='dwgtable' && tbl._cells){
              for(var ci=0;ci<tbl._cells.length;ci++){
                var cell = tbl._cells[ci];
                var txt = cell._text || '';
                var tag = cell._tag || '';
                if(txt) out[tag||'cell'+ci] = txt;
              }
            }
          }
          return JSON.stringify(out);
        }
      }catch(e){}
    }
    return 'OFP not found';
  })()`);
  if (ofp && !ofp.startsWith('ERR') && ofp !== 'OFP not found' && ofp !== 'no coll' && ofp !== 'no tables') {
    try {
      const d = JSON.parse(ofp);
      for (const [k, v] of Object.entries(d)) console.log(`  ${k}: ${v}`);
    } catch(e) { console.log('  Raw:', ofp.substring(0,200)); }
  } else {
    console.log('  (no table data)');
  }

  // === DRAWING LABELS & LINES from indicators ===
  console.log(`\n📏 NIVELES (indicadores)`);
  const levels = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    var out = {};
    for(var i=3;i<sources.length;i++){
      var s = sources[i];
      try {
        var name = s.metaInfo().description || s.metaInfo().shortName || '';
        name = name.substring(0,40);
        var coll = s._graphics && s._graphics._primitivesCollection;
        if(!coll) continue;
        var hlines = coll.hlines;
        if(hlines && hlines._items && hlines._items.length) {
          var prices = hlines._items.map(function(h) { return h._price; }).filter(function(p) { return p != null; });
          if(prices.length) out[name+' hlines'] = prices.map(function(p){return +p.toFixed(2)}).slice(0,5);
        }
        var labels = coll.dwglabels;
        if(labels && labels._items && labels._items.length) {
          var ltexts = labels._items.map(function(l){ return l._text || ''; }).filter(function(t){return t;});
          if(ltexts.length) out[name+' labels'] = ltexts.slice(0,5);
        }
        var lines = coll.dwglines;
        if(lines && lines._items && lines._items.length) {
          out[name+' lines'] = lines._items.length + ' lines';
        }
      } catch(e) {}
    }
    return JSON.stringify(out);
  })()`);
  if (levels && levels !== '{}') {
    const d = JSON.parse(levels);
    for (const [k, v] of Object.entries(d)) {
      if (Array.isArray(v)) console.log(`  ${k}: ${v.join(', ')}`);
      else console.log(`  ${k}: ${v}`);
    }
  } else {
    console.log('  (no levels found)');
  }

  client.close();
  console.log('\nDONE');
}

main().catch(e => console.log('FATAL:', e.message));
