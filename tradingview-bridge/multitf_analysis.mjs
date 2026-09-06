import CDP from 'chrome-remote-interface';
import https from 'https';
const TG_TOKEN = '8890755562:AAHdMKjIhXETxJ-tgoyxxwrNoxcZzCBh3o4';
const TG_CHAT = '6369690844';

function tgSend(text) {
  return new Promise((ok, fail) => {
    const t = encodeURIComponent(text);
    https.get(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage?chat_id=${TG_CHAT}&text=${t}&parse_mode=Markdown`,
      r => { let d=''; r.on('data',c=>d+=c); r.on('end',()=>ok(d)); }
    ).on('error', fail);
  });
}

function ema(p, per) {
  if (p.length < per) return null;
  const k = 2/(per+1); let e = p.slice(0,per).reduce((a,b)=>a+b,0)/per;
  for (let i=per; i<p.length; i++) e = p[i]*k + e*(1-k);
  return e;
}
function rsi(p, per) {
  if (p.length < per+1) return null;
  let g=0,l=0;
  for (let i=p.length-per;i<p.length;i++){const d=p[i]-p[i-1];if(d>=0)g+=d;else l-=d;}
  const ag=g/per,al=l/per;
  return al===0?100:100-100/(1+ag/al);
}

async function getData(client, tf, waitMs=4000) {
  // Set timeframe
  await client.Runtime.evaluate({
    expression: `window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.chart().setResolution("${tf}",function(){})`,
    awaitPromise: false
  });
  await new Promise(r => setTimeout(r, waitMs));

  // Get state
  let r = await client.Runtime.evaluate({
    expression: '(function(){var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();var last=ms.bars().last();return{sym:ms.symbol(),close:last.value[4],high:last.value[2],low:last.value[3]}})()',
    returnByValue: true, awaitPromise: false
  });
  if (r.result.exceptionDetails) return null;
  const state = r.result.value;

  // Get bars
  r = await client.Runtime.evaluate({
    expression: '(function(){var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();var items=ms.bars()._items,l=items.length,o=[];for(var i=Math.max(0,l-60);i<l;i++){if(items[i])o.push({o:items[i].value[1],h:items[i].value[2],l:items[i].value[3],c:items[i].value[4],v:items[i].value[5]})}return o})()',
    returnByValue: true, awaitPromise: false
  });
  const bars = r.result.value;
  if (!Array.isArray(bars) || bars.length < 10) return null;

  const c = bars.map(b=>b.c), h = bars.map(b=>b.h), lo = bars.map(b=>b.l), v = bars.map(b=>b.v);
  const sHigh = Math.max(...h), sLow = Math.min(...lo);
  const e9 = ema(c,9), e21 = ema(c,21), e50 = c.length>=50 ? ema(c,50) : ema(c,Math.min(30,c.length));
  const r14 = rsi(c,14), r6 = rsi(c,6);
  const avgV = v.reduce((a,b)=>a+b,0)/v.length;
  const lastV = v[v.length-1];
  const vr = (lastV/avgV).toFixed(1);

  // Swings
  const last20 = bars.slice(-20);
  let sh=[], sl=[];
  for (let i=1;i<last20.length-1;i++){
    if(last20[i].h>last20[i-1].h&&last20[i].h>last20[i+1].h) sh.push(last20[i].h);
    if(last20[i].l<last20[i-1].l&&last20[i].l<last20[i+1].l) sl.push(last20[i].l);
  }

  const vsEma9 = state.close > e9 ? 'above' : 'below';
  let bias = 'NEUTRAL';
  if (state.close > e9 && e9 > e21) bias = 'BULLISH';
  else if (state.close < e9 && e9 < e21) bias = 'BEARISH';
  else if (state.close < e9 && e9 > e21) bias = 'PULLBACK';
  else if (state.close > e9 && e9 < e21) bias = 'RECOVERY';

  const cross = e9 > e21 ? '9>21' : '9<21';
  const rsiZone = r14 >= 70 ? 'OB' : r14 <= 30 ? 'OS' : r14 > 55 ? '↑' : r14 < 45 ? '↓' : '∼';

  return {
    tf, price: state.close, high: state.high, low: state.low,
    e9: e9?.toFixed(4), e21: e21?.toFixed(4), e50: e50?.toFixed(4),
    r14: r14?.toFixed(1), r6: r6?.toFixed(1), rsiZone,
    cross, vsEma9, vr, avgV: Math.round(avgV), lastV: Math.round(lastV),
    sHigh: sHigh.toFixed(4), sLow: sLow.toFixed(4),
    range: (sHigh-sLow).toFixed(4),
    microRes: sh.length>0?Math.max(...sh).toFixed(4):sHigh.toFixed(4),
    microSup: sl.length>0?Math.min(...sl).toFixed(4):sLow.toFixed(4),
    bias, barCount: bars.length
  };
}

async function main() {
  const targets = await CDP.List({ host:'localhost', port:9222 });
  const chart = targets.find(t => t.url && t.url.includes('tradingview.com/chart')) || targets[0];
  if (!chart) { console.log('NO_CHART'); process.exit(1); }
  const client = await CDP({ host:'localhost', port:9222, target:chart.id });
  await client.Runtime.enable();

  // Get 1m data
  const d1 = await getData(client, '1', 4000);
  if (!d1) { console.log('ERR 1m'); process.exit(1); }

  // Get 5m data
  const d5 = await getData(client, '5', 4000);
  if (!d5) { console.log('ERR 5m'); process.exit(1); }

  // Get 15m data
  const d15 = await getData(client, '15', 4000);
  if (!d15) { console.log('ERR 15m'); process.exit(1); }

  // Get 1h data
  const d1h = await getData(client, '60', 4000);
  if (!d1h) { console.log('ERR 1h'); process.exit(1); }

  const sym = d1.sym || d5.sym || d15.sym || d1h.sym || 'SUIUSDT.P';
  const price = d1h.price;

  // Build console output
  const l = (s) => console.log(s);
  l('');
  l('╔══════════════════════════════════════════════════════╗');
  l('║        ' + sym + '  —  MULTI-TIMEFRAME ANALYSIS       ║');
  l('╚══════════════════════════════════════════════════════╝');
  l('');
  l(sym + '  $' + price.toFixed(4) + '  |  ' + new Date().toLocaleString());
  l('');

  const rows = [
    ['TF', 'Price', 'EMA9', 'EMA21', 'EMA50', 'Cross', 'RSI14', 'RSI6', 'Vol', 'Range', 'Bias'],
    ['──', '─────', '─────', '──────', '──────', '─────', '──────', '─────', '───', '─────', '────'],
    ['1m', '$'+d1.price.toFixed(4), d1.e9, d1.e21, d1.e50, d1.cross, d1.r14+'('+d1.rsiZone+')', d1.r6, d1.vr+'x', '$'+d1.range, d1.bias],
    ['5m', '$'+d5.price.toFixed(4), d5.e9, d5.e21, d5.e50, d5.cross, d5.r14+'('+d5.rsiZone+')', d5.r6, d5.vr+'x', '$'+d5.range, d5.bias],
    ['15m','$'+d15.price.toFixed(4),d15.e9,d15.e21,d15.e50,d15.cross,d15.r14+'('+d15.rsiZone+')',d15.r6,d15.vr+'x','$'+d15.range,d15.bias],
    ['1h','$'+d1h.price.toFixed(4),d1h.e9,d1h.e21,d1h.e50,d1h.cross,d1h.r14+'('+d1h.rsiZone+')',d1h.r6,d1h.vr+'x','$'+d1h.range,d1h.bias],
  ];
  rows.forEach(r => l(r.join(' | ')));

  l('');
  l('─── Micro Estructura ───');
  l('1m  → Res: $' + d1.microRes + ' | Sup: $' + d1.microSup);
  l('5m  → Res: $' + d5.microRes + ' | Sup: $' + d5.microSup);
  l('15m → Res: $' + d15.microRes + ' | Sup: $' + d15.microSup);
  l('1h  → Res: $' + d1h.microRes + ' | Sup: $' + d1h.microSup);
  l('');

  // Overall bias
  const biases = [d1.bias, d5.bias, d15.bias, d1h.bias];
  const bullish = biases.filter(b=>b==='BULLISH').length;
  const bearish = biases.filter(b=>b==='BEARISH').length;
  const overall = bullish >= 2 ? 'BULLISH' : bearish >= 2 ? 'BEARISH' : biases.includes('PULLBACK') ? 'PULLBACK' : 'NEUTRAL';
  l('🎯 Overall Bias: ' + overall + ' (1m:'+d1.bias+' 5m:'+d5.bias+' 15m:'+d15.bias+')');

  // Alignments
  const allBull = d1.cross==='9>21'&&d5.cross==='9>21'&&d15.cross==='9>21';
  const allBear = d1.cross==='9<21'&&d5.cross==='9<21'&&d15.cross==='9<21'&&d1h.cross==='9<21';
  if (allBull) l('✅ ALL TIMEFRAMES BULLISH ALIGNMENT (9>21)');
  if (allBear) l('🔴 ALL TIMEFRAMES BEARISH ALIGNMENT (9<21)');
  if (!allBull && !allBear) l('⚠️ Mixed EMA alignment across TFs');
  l('');

  // Build Telegram message
  const tgMsg = '📊 *' + sym + ' — MULTI-TF (1-5-15-60m)*\n\n' +
    '💵 $' + price.toFixed(4) + ' | ' + new Date().toLocaleTimeString() + '\n\n' +
    '```\n' +
    'TF   Price     EMA9    EMA21   EMA50  Cross RSI14    RSI6   Vol   Bias\n' +
    '1m   $' + d1.price.toFixed(4) + ' ' + d1.e9 + ' ' + d1.e21 + ' ' + d1.e50 + ' ' + d1.cross + ' ' + d1.r14 + '(' + d1.rsiZone + ') ' + d1.r6 + '  ' + d1.vr + 'x ' + d1.bias + '\n' +
    '5m   $' + d5.price.toFixed(4) + ' ' + d5.e9 + ' ' + d5.e21 + ' ' + d5.e50 + ' ' + d5.cross + ' ' + d5.r14 + '(' + d5.rsiZone + ') ' + d5.r6 + '  ' + d5.vr + 'x ' + d5.bias + '\n' +
    '15m  $' + d15.price.toFixed(4) + ' ' + d15.e9 + ' ' + d15.e21 + ' ' + d15.e50 + ' ' + d15.cross + ' ' + d15.r14 + '(' + d15.rsiZone + ') ' + d15.r6 + '  ' + d15.vr + 'x ' + d15.bias + '\n' +
    '1h   $' + d1h.price.toFixed(4) + ' ' + d1h.e9 + ' ' + d1h.e21 + ' ' + d1h.e50 + ' ' + d1h.cross + ' ' + d1h.r14 + '(' + d1h.rsiZone + ') ' + d1h.r6 + '  ' + d1h.vr + 'x ' + d1h.bias + '\n' +
    '```\n\n' +
    '📊 Micro Estructura:\n' +
    '• 1m:  Res $' + d1.microRes + ' | Sup $' + d1.microSup + '\n' +
    '• 5m:  Res $' + d5.microRes + ' | Sup $' + d5.microSup + '\n' +
    '• 15m: Res $' + d15.microRes + ' | Sup $' + d15.microSup + '\n' +
    '• 1h:  Res $' + d1h.microRes + ' | Sup $' + d1h.microSup + '\n\n' +
    '🎯 *Overall: ' + overall + '*\n' +
    (allBull ? '✅ All TFs bullish\n' : '') +
    (allBear ? '🔴 All TFs bearish\n' : '') +
    (!allBull&&!allBear ? '⚠️ Mixed signals\n' : '') +
    '1m:' + d1.bias + ' | 5m:' + d5.bias + ' | 15m:' + d15.bias + ' | 1h:' + d1h.bias;

  const tgR = await tgSend(tgMsg);
  console.log('✅ Telegram sent (msg ' + JSON.parse(tgR).result.message_id + ')');
  console.log('DONE');

  client.close();
}

main().catch(e => { console.log('FATAL:', e.message); process.exit(1); });
