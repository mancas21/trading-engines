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
  const r = await client.Runtime.evaluate({expression:expr, returnByValue: true});
  if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.text);
  return r.result.value;
}

async function wait(ms) { return new Promise(r => setTimeout(r, ms)); }

async function main() {
  await connect();
  console.log('Connected');

  // Get symbol and resolution
  const sym = await e('window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().symbol()');
  const res = await e('window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().interval().value()');
  console.log(`Symbol: ${sym}  Resolution: ${res}`);

  // Get last bar
  const lastBar = await e(`(function(){
    var ms = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries();
    var bars = ms.bars();
    if (!bars || !bars.length) return null;
    var last = bars[bars.length - 1];
    return JSON.stringify({t:last.time_v, o:last.value[1], h:last.value[2], l:last.value[3], c:last.value[4], v:last.value[5]});
  })()`);
  if (lastBar) {
    const b = JSON.parse(lastBar);
    console.log(`\n--- Last Bar (${new Date(b.t*1000).toLocaleTimeString()}) ---`);
    console.log(`O: ${b.o}  H: ${b.h}  L: ${b.l}  C: ${b.c}  V: ${b.v}`);
  }

  // Get recent bars for levels
  const range = await e(`(function(){
    var ms = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries();
    var bars = ms.bars().slice(-100);
    if (!bars.length) return null;
    var o=bars[0].value[1], c=bars[bars.length-1].value[4];
    var h=Math.max(...bars.map(b=>b.value[2])), l=Math.min(...bars.map(b=>b.value[3]));
    var v=bars.map(b=>b.value[5]).reduce((a,b)=>a+b,0)/bars.length;
    var maxV = Math.max(...bars.map(b=>b.value[5]));
    // EMA
    var closes = bars.map(b=>b.value[4]);
    var ema = function(arr, p) { var k=2/(p+1); var r=arr[0]; for(var i=1;i<arr.length;i++) r=arr[i]*k+r*(1-k); return r; };
    var e9 = ema(closes.slice(-50),9);
    var e21 = ema(closes.slice(-50),21);
    var e50 = ema(closes.slice(-50),50);
    // RSI
    var gains=[], losses=[];
    for(var i=1;i<closes.length;i++){var d=closes[i]-closes[i-1]; gains.push(d>0?d:0); losses.push(d<0?-d:0);}
    var avg=function(arr,n){return arr.slice(-n).reduce((s,v)=>s+v,0)/Math.min(n,arr.length);};
    var ag=avg(gains,14), al=avg(losses,14);
    var rsi = al===0?100:100-100/(1+ag/al);
    return JSON.stringify({high:h, low:l, avgVol:+v.toFixed(1), maxVol:maxV, ema9:+e9.toFixed(2), ema21:+e21.toFixed(2), ema50:+e50.toFixed(2), rsi14:+rsi.toFixed(1)});
  })()`);
  if (range) {
    const r = JSON.parse(range);
    console.log(`\n--- Range (100 bars) ---`);
    console.log(`H: ${r.h}  L: ${r.l}  Range: ${(r.h-r.l).toFixed(2)}`);
    console.log(`EMA9: ${r.ema9}  EMA21: ${r.ema21}  EMA50: ${r.ema50}`);
    console.log(`RSI14: ${r.rsi14}`);
    console.log(`Avg Vol: ${r.avgVol}k  Max Vol: ${r.maxVol}k`);
  }

  // Get Master Pattern Scanner
  console.log(`\n--- Master Pattern Scanner ---`);
  try {
    const mps = await e(`(function(){
      var s = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
      for(var i=0;i<s.length;i++){
        var m = s[i].metaInfo();
        if(m && m.description && m.description.indexOf("Master Pattern Scanner")>=0){
          var p = s[i]._graphics && s[i]._graphics._primitivesCollection;
          if(!p || !p._primitives) return "NODATA";
          for(var j=0;j<p._primitives.length;j++){
            var prim = p._primitives[j];
            if(prim._type==="table"){
              var r={};
              for(var k=0;k<prim._rows;k++){
                var c0=prim._cells&&prim._cells[k]&&prim._cells[k][0];
                var c1=prim._cells&&prim._cells[k]&&prim._cells[k][1];
                if(c0&&c0._text&&c1&&c1._text) r[c0._text]=c1._text;
              }
              return JSON.stringify(r);
            }
          }
        }
      }
      return "NODATA";
    })()`);
    if (mps && mps !== 'NODATA') {
      const d = JSON.parse(mps);
      for (const [k, v] of Object.entries(d)) console.log(`  ${k}: ${v}`);
    } else {
      console.log('  (no table)');
    }
  } catch(err) { console.log('  Error:', err.message); }

  // Get Orderflow Pro
  console.log(`\n--- Orderflow Pro Strategy ---`);
  try {
    const ofp = await e(`(function(){
      var s = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
      for(var i=0;i<s.length;i++){
        var m = s[i].metaInfo();
        if(m && m.description && m.description.indexOf("Orderflow Pro")>=0){
          var p = s[i]._graphics && s[i]._graphics._primitivesCollection;
          if(!p || !p._primitives) return "NODATA";
          for(var j=0;j<p._primitives.length;j++){
            var prim = p._primitives[j];
            if(prim._type==="table"){
              var r={};
              for(var k=0;k<prim._rows;k++){
                var c0=prim._cells&&prim._cells[k]&&prim._cells[k][0];
                var c1=prim._cells&&prim._cells[k]&&prim._cells[k][1];
                if(c0&&c0._text&&c1&&c1._text) r[c0._text]=c1._text;
              }
              return JSON.stringify(r);
            }
          }
        }
      }
      return "NODATA";
    })()`);
    if (ofp && ofp !== 'NODATA') {
      const d = JSON.parse(ofp);
      for (const [k, v] of Object.entries(d)) console.log(`  ${k}: ${v}`);
    } else {
      console.log('  (no table)');
    }
  } catch(err) { console.log('  Error:', err.message); }

  // Get BookFlow Pro
  console.log(`\n--- BookFlow Pro ---`);
  try {
    const bf = await e(`(function(){
      var s = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
      for(var i=0;i<s.length;i++){
        var m = s[i].metaInfo();
        if(m && m.description && m.description.indexOf("BookFlow")>=0){
          var p = s[i]._graphics && s[i]._graphics._primitivesCollection;
          if(!p || !p._primitives) return "NODATA";
          for(var j=0;j<p._primitives.length;j++){
            var prim = p._primitives[j];
            if(prim._type==="table"){
              var r={};
              for(var k=0;k<prim._rows;k++){
                var c0=prim._cells&&prim._cells[k]&&prim._cells[k][0];
                var c1=prim._cells&&prim._cells[k]&&prim._cells[k][1];
                if(c0&&c0._text&&c1&&c1._text) r[c0._text]=c1._text;
              }
              return JSON.stringify(r);
            }
          }
        }
      }
      return "NODATA";
    })()`);
    if (bf && bf !== 'NODATA') {
      const d = JSON.parse(bf);
      for (const [k, v] of Object.entries(d)) console.log(`  ${k}: ${v}`);
    } else {
      console.log('  (no table)');
    }
  } catch(err) { console.log('  Error:', err.message); }

  // Get Order Flow Sniper
  console.log(`\n--- Order Flow Sniper ---`);
  try {
    const sniper = await e(`(function(){
      var s = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
      for(var i=0;i<s.length;i++){
        var m = s[i].metaInfo();
        if(m && m.description && m.description.indexOf("Order Flow Sniper")>=0){
          var p = s[i]._graphics && s[i]._graphics._primitivesCollection;
          if(!p || !p._primitives) return "NODATA";
          for(var j=0;j<p._primitives.length;j++){
            var prim = p._primitives[j];
            if(prim._type==="table"){
              var r={};
              for(var k=0;k<prim._rows;k++){
                var c0=prim._cells&&prim._cells[k]&&prim._cells[k][0];
                var c1=prim._cells&&prim._cells[k]&&prim._cells[k][1];
                if(c0&&c0._text&&c1&&c1._text) r[c0._text]=c1._text;
              }
              return JSON.stringify(r);
            }
          }
        }
      }
      return "NODATA";
    })()`);
    if (sniper && sniper !== 'NODATA') {
      const d = JSON.parse(sniper);
      for (const [k, v] of Object.entries(d)) console.log(`  ${k}: ${v}`);
    } else {
      console.log('  (no table)');
    }
  } catch(err) { console.log('  Error:', err.message); }

  // Get visible levels/lines from studies
  console.log(`\n--- Support/Resistance Levels ---`);
  try {
    const lines = await e(`(function(){
      var s = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
      var levels = {};
      for(var i=0;i<s.length;i++){
        var m = s[i].metaInfo();
        if(!m || !m.description) continue;
        var p = s[i]._graphics && s[i]._graphics._primitivesCollection;
        if(!p || !p._primitives) continue;
        var desc = m.description;
        for(var j=0;j<p._primitives.length;j++){
          var prim = p._primitives[j];
          if(prim._type === "hline" && prim._price != null){
            if(!levels[desc]) levels[desc]=[];
            levels[desc].push(+prim._price.toFixed(2));
          }
        }
      }
      return JSON.stringify(levels);
    })()`);
    if (lines && lines !== '{}') {
      const d = JSON.parse(lines);
      for (const [k, v] of Object.entries(d)) {
        const uniq = [...new Set(v)].sort((a,b)=>a-b);
        console.log(`  ${k.substring(0,40)}: ${uniq.join(', ')}`);
      }
    } else {
      console.log('  (no levels found)');
    }
  } catch(err) { console.log('  Error:', err.message); }

  client.close();
  console.log('\nDone');
}

main().catch(e => console.log('FATAL:', e.message));
