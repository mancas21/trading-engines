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

  // Symbol & TF
  const sym = await e('window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().symbol()');
  console.log(`SYMBOL: ${sym}`);

  // Last bar
  const lb = JSON.parse(await e('(function(){var v=window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().bars()._items;var l=v[v.length-1].value;return JSON.stringify({t:l[0],o:+l[1].toFixed(2),h:+l[2].toFixed(2),l:+l[3].toFixed(2),c:+l[4].toFixed(2),v:+l[5].toFixed(0)});})()'));
  console.log(`\nÚLTIMA VELA [${new Date(lb.t*1000).toLocaleTimeString()}]`);
  console.log(`O: $${lb.o}  H: $${lb.h}  L: $${lb.l}  C: $${lb.c}  Vol: ${(lb.v/1000).toFixed(1)}k`);

  // Extract all bar data and compute EMAs, RSI, levels
  const d = JSON.parse(await e(`(function(){
    var items = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().bars()._items;
    var closes=[],highs=[],lows=[],vols=[];
    for(var i=0;i<items.length;i++){var v=items[i].value;closes.push(v[4]);highs.push(v[2]);lows.push(v[3]);vols.push(v[5]);}
    var len=closes.length;
    // EMAs
    var ema=function(arr,p){var k=2/(p+1);var r=arr[0];for(var i=1;i<arr.length;i++)r=arr[i]*k+r*(1-k);return r;};
    var e9=ema(closes.slice(-50),9), e21=ema(closes.slice(-50),21), e50=ema(closes.slice(-50),50);
    // RSI
    var g=[],ls=[];
    for(var i=1;i<closes.length;i++){var d=closes[i]-closes[i-1];g.push(d>0?d:0);ls.push(d<0?-d:0);}
    var avg=function(a,n){return a.slice(-n).reduce(function(s,v){return s+v;},0)/Math.min(n,a.length);};
    var r14=function(){var ag=avg(g,14),al=avg(ls,14);return al===0?100:100-100/(1+ag/al);}();
    var r6=function(){var ag=avg(g,6),al=avg(ls,6);return al===0?100:100-100/(1+ag/al);}();
    var prevClose=closes.length>1?closes[closes.length-2]:null;
    var prevE9=closes.length>51?ema(closes.slice(-51,-1),9):null;
    var prevE21=closes.length>51?ema(closes.slice(-51,-1),21):null;
    // Volume
    var avg30=vols.slice(-30).reduce(function(s,v){return s+v;},0)/Math.min(30,vols.length);
    var max100=Math.max.apply(null,vols.slice(-100));
    var prevVol=vols.length>1?vols[vols.length-2]:0;
    // Levels
    var h100=Math.max.apply(null,highs.slice(-100)), l100=Math.min.apply(null,lows.slice(-100));
    var h20=Math.max.apply(null,highs.slice(-20)), l20=Math.min.apply(null,lows.slice(-20));
    // Swing high/low (fractal)
    var sh=null, sl=null;
    for(var i=Math.max(5,len-60);i<len;i++){
      if(highs[i]>highs[i-1]&&highs[i]>highs[i+1]&&highs[i]>highs[i-2]&&highs[i]>highs[i+2]){if(!sh||highs[i]>sh)sh=highs[i];}
      if(lows[i]<lows[i-1]&&lows[i]<lows[i+1]&&lows[i]<lows[i-2]&&lows[i]<lows[i+2]){if(!sl||lows[i]<sl)sl=lows[i];}
    }
    // Price position vs EMAs
    var cur = closes[closes.length-1];
    return JSON.stringify({
      ema9:+e9.toFixed(2), ema21:+e21.toFixed(2), ema50:+e50.toFixed(2),
      rsi14:+r14.toFixed(1), rsi6:+r6.toFixed(1),
      volRatio:+(cur/avg30).toFixed(1), avgVol30:+avg30.toFixed(0), maxVol100:+max100.toFixed(0), prevVol:+prevVol.toFixed(0),
      h20:+h20.toFixed(2), l20:+l20.toFixed(2), h100:+h100.toFixed(2), l100:+l100.toFixed(2),
      sh:sh?+sh.toFixed(2):null, sl:sl?+sl.toFixed(2):null,
      prevClose:+prevClose.toFixed(2), prevE9:prevE9?+prevE9.toFixed(2):null, prevE21:prevE21?+prevE21.toFixed(2):null,
      cross: e9>e21?'bull':'bear', curClose:+cur.toFixed(2)
    });
  })()`));

  // Print analysis
  console.log(`\n📐 TÉCNICO`);
  console.log(`EMA9: $${d.ema9}  |  EMA21: $${d.ema21}  |  EMA50: $${d.ema50}`);
  const crossStr = d.cross === 'bull' ? '9>21 🟢' : '9<21 🔴';
  const gap = Math.abs(d.ema21 - d.ema9);
  console.log(`Cross: ${crossStr} (gap: $${gap.toFixed(2)})`);

  // Cross change detection
  if (d.prevE9 && d.prevE21) {
    if (d.prevE9 < d.prevE21 && d.ema9 > d.ema21) console.log(`🆕 CRUCE BULLISH 9>21`);
    else if (d.prevE9 > d.prevE21 && d.ema9 < d.ema21) console.log(`🆕 CRUCE BEARISH 9<21`);
  }

  console.log(`RSI14: ${d.rsi14}  |  RSI6: ${d.rsi6}`);
  if (d.rsi14 > 70) console.log(`⚠️ RSI14 SOBRECOMPRA`);
  else if (d.rsi14 < 30) console.log(`⚠️ RSI14 SOBREVENDIDO`);

  console.log(`\n📊 VOLUMEN`);
  console.log(`Actual: ${(d.curClose/1000).toFixed(0)}k  |  Prev: ${(d.prevVol/1000).toFixed(0)}k  |  Prom30: ${(d.avgVol30/1000).toFixed(0)}k`);
  console.log(`Ratio: ${d.volRatio}x  |  Max100: ${(d.maxVol100/1000).toFixed(0)}k`);
  if (d.volRatio > 3) console.log(`⚡ SPIKES DE VOLUMEN`);

  console.log(`\n📏 NIVELES`);
  console.log(`20b: $${d.l20} - $${d.h20}  ($${(d.h20-d.l20).toFixed(2)})`);
  console.log(`100b: $${d.l100} - $${d.h100}  ($${(d.h100-d.l100).toFixed(2)})`);
  if (d.sh) console.log(`Swing High: $${d.sh}`);
  if (d.sl) console.log(`Swing Low: $${d.sl}`);

  console.log(`\n🎯 BIAS: ${d.cross === 'bull' ? 'BULLISH' : 'BEARISH'}`);
  console.log(`Precio vs EMAs: ${d.curClose > d.ema9 ? '🟢 sobre EMA9' : '🔴 bajo EMA9'} | ${d.curClose > d.ema21 ? '🟢 sobre EMA21' : '🔴 bajo EMA21'}`);

  if (d.rsi14 < 30 && d.cross === 'bear') console.log(`→ Oversold en downtrend: posible rebote técnico, confirmar con vela de reversal`);
  else if (d.rsi14 > 70 && d.cross === 'bull') console.log(`→ Overbought en uptrend: posible agotamiento, buscar señales de distribución`);
  else if (d.cross === 'bear') console.log(`→ Tendencia bajista: rebotes a EMAs son shorts. Esperar precio a EMA21/50`);
  else console.log(`→ Tendencia alcista: retrocesos a EMAs son longs`);

  // Micro patterns
  if (d.sl && Math.abs(d.curClose - d.sl) < (d.h100 - d.l100) * 0.02) console.log(`→ Precio testeando swing low $${d.sl}`);
  if (d.sh && Math.abs(d.curClose - d.sh) < (d.h100 - d.l100) * 0.02) console.log(`→ Precio testeando swing high $${d.sh}`);

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
