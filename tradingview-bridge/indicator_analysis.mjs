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
function sma(p, per) { return p.length < per ? null : p.slice(-per).reduce((a,b)=>a+b,0)/per; }
function rsi(p, per) {
  if (p.length < per+1) return null;
  let g=0,l=0;
  for (let i=p.length-per;i<p.length;i++){const d=p[i]-p[i-1];if(d>=0)g+=d;else l-=d;}
  return l===0?100:100-100/(1+(g/per)/(l/per));
}

async function main() {
  const targets = await CDP.List({ host: 'localhost', port: 9222 });
  const chart = targets.find(t => t.url && t.url.includes('tradingview.com/chart'));
  if (!chart) { console.log('NO_CHART'); process.exit(1); }
  const client = await CDP({ host: 'localhost', port: 9222, target: chart.id });
  await client.Runtime.enable();

  async function ex(js) {
    const r = await client.Runtime.evaluate({ expression: js, returnByValue: true, awaitPromise: false });
    if (r.result.exceptionDetails) return { _err: r.result.exceptionDetails.text };
    return r.result.value;
  }

  // Wait for data
  await new Promise(r => setTimeout(r, 3000));

  // Get current
  let r = await ex('(function(){var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();var last=ms.bars().last();return{sym:ms.symbol(),close:last.value[4],high:last.value[2],low:last.value[3],vol:last.value[5]}})()');
  if (r._err) { console.log('ERR:', r._err); client.close(); process.exit(1); }
  const close = r.close, high = r.high, low = r.low;
  console.log('STATE:', r.sym, r.close);

  // Get 60 bars 5m
  r = await ex('(function(){var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();var items=ms.bars()._items,l=items.length,o=[];for(var i=Math.max(0,l-60);i<l;i++){if(items[i])o.push({o:items[i].value[1],h:items[i].value[2],l:items[i].value[3],c:items[i].value[4],v:items[i].value[5]})}return o})()');
  const bars = r && Array.isArray(r) ? r : [];
  console.log('BARS:', bars.length);

  const c = bars.map(b=>b.c), h = bars.map(b=>b.h), lo = bars.map(b=>b.l), v = bars.map(b=>b.v);
  const sHigh = Math.max(...h), sLow = Math.min(...lo);
  const e9 = ema(c,9), e21 = ema(c,21), e50 = c.length>=50 ? ema(c,50) : sma(c,Math.min(30,c.length));
  const r14 = rsi(c,14), r6 = rsi(c,6);
  const avgV = v.reduce((a,b)=>a+b,0)/v.length;
  const lastV = v[v.length-1];
  const vr = (lastV/avgV).toFixed(1);

  // Volume profile
  const step = 0.05;
  const vc = {};
  bars.forEach(b => { const k = Math.round(b.c/step)*step; vc[k] = (vc[k]||0)+b.v; });
  const sorted = Object.entries(vc).sort((a,b)=>b[1]-a[1]);
  const poc = sorted.length > 0 ? parseFloat(sorted[0][0]) : close;
  const vahLvl = sorted.length > 1 ? parseFloat(sorted[Math.floor(sorted.length*0.15)][0]) : poc;
  const valLvl = sorted.length > 1 ? parseFloat(sorted[Math.floor(sorted.length*0.85)][0]) : poc;

  // Swings
  const last20 = bars.slice(-20);
  let sh = [], sl = [];
  for (let i = 1; i < last20.length-1; i++) {
    if (last20[i].h > last20[i-1].h && last20[i].h > last20[i+1].h) sh.push(last20[i].h);
    if (last20[i].l < last20[i-1].l && last20[i].l < last20[i+1].l) sl.push(last20[i].l);
  }
  const microRes = sh.length > 0 ? Math.max(...sh) : sHigh;
  const microSup = sl.length > 0 ? Math.min(...sl) : sLow;

  // Patterns
  const l3 = bars.slice(-3);
  const doji = l3[l3.length-1] && Math.abs(l3[l3.length-1].c-l3[l3.length-1].o) < 0.02;
  const ib = l3.length >= 2 && l3[l3.length-1].h <= l3[l3.length-2].h && l3[l3.length-1].l >= l3[l3.length-2].l;

  // Volume spikes
  const spikes = v.slice(-5).filter(x => x > avgV * 2.5).length;

  // Trend in last 10
  const c10 = c.slice(-10);
  const slope10 = ((c10[c10.length-1] - c10[0]) / c10[0] * 100).toFixed(2);

  // Bias
  const vsEma9 = close > e9 ? 'above' : 'below';
  const rsiZone = r14 >= 70 ? 'OB' : r14 <= 30 ? 'OS' : r14 > 55 ? 'high' : r14 < 45 ? 'low' : 'mid';
  const crossState = e9 > e21 ? 'EMA9>EMA21' : 'EMA9<EMA21';
  let bias = 'NEUTRAL';
  if (close > e9 && e9 > e21 && slope10 > 0) bias = 'BULLISH';
  else if (close < e9 && e9 < e21 && slope10 < 0) bias = 'BEARISH';
  else if (close < e9 && e9 > e21) bias = 'PULLBACK';
  else if (close > e9 && e9 < e21) bias = 'RECOVERY';

  // Retracement level (fib rough)
  const range = sHigh - sLow;
  const fib618 = sHigh - range * 0.618;
  const fib50 = sHigh - range * 0.5;
  const fib382 = sHigh - range * 0.382;

  const txt =
    `📊 *${r.sym} — 5m*\n\n` +
    `💵 $${close.toFixed(2)} | H:$${high.toFixed(2)} L:$${low.toFixed(2)}\n\n` +
    `📈 Indicadores:\n` +
    `• EMA9: $${e9?.toFixed(2) || '?'} | EMA21: $${e21?.toFixed(2) || '?'} | EMA50: $${e50?.toFixed(2) || '?'}\n` +
    `• ${crossState} | Precio: ${vsEma9} EMA9\n` +
    `• RSI14: ${r14?.toFixed(1) || '?'} (${rsiZone}) | RSI6: ${r6?.toFixed(1) || '?'}\n` +
    `• Vol: ${vr}x (${Math.round(lastV)}) | Avg: ${Math.round(avgV)}\n` +
    `• Tendencia 10v: ${slope10}%\n\n` +
    `📊 Value Area (60v):\n` +
    `• Rango: $${sLow.toFixed(2)} → $${sHigh.toFixed(2)} ($${(sHigh-sLow).toFixed(2)})\n` +
    `• POC: $${poc.toFixed(2)} | VAH: $${vahLvl.toFixed(2)} | VAL: $${valLvl.toFixed(2)}\n` +
    `• MicroRes: $${microRes.toFixed(2)} | MicroSup: $${microSup.toFixed(2)}\n\n` +
    `🎯 Fib levels:\n` +
    `• 0.618 ($${fib618.toFixed(2)}) | 0.5 ($${fib50.toFixed(2)}) | 0.382 ($${fib382.toFixed(2)})\n\n` +
    (doji ? '⚠️ Doji — indecisión\n' : '') +
    (ib ? '⚠️ Inside bar — compresión\n' : '') +
    (spikes > 0 ? `⚠️ ${spikes} volume spike(s)\n` : '') +
    `🎯 Bias: *${bias}*`;

  console.log(txt);

  const tgR = await tgSend(txt);
  console.log('TG:', tgR.slice(0,100));
  console.log('DONE');
  client.close();
}

main().catch(e => { console.log('FATAL:', e.message); process.exit(1); });
