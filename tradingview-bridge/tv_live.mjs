// Live TradingView read for XRP: exact TF, precise EMAs, RSI, multi-TF context.
// Run: node tv_live.mjs
import CDP from 'chrome-remote-interface';

let client;
async function connect() {
  const targets = await CDP.List({ host: 'localhost', port: 9222 });
  const t = targets.find(x => x.title && x.title.includes('TradingView'));
  if (!t) throw new Error('TV target not found');
  client = await CDP({ host: 'localhost', port: 9222, target: t.id });
  await client.Runtime.enable();
}
async function e(expr) {
  const r = await client.Runtime.evaluate({ expression: expr, returnByValue: true, timeout: 15000 });
  if (r.result.exceptionDetails) return 'ERR: ' + (r.result.exceptionDetails.text || 'exception');
  return r.result.value;
}

const MAIN = 'window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries()';

async function main() {
  await connect();
  console.log('CONNECTED');

  const sym = await e(`${MAIN}.symbol()`);
  const tf = await e(`(function(){var s=${MAIN}.interval(); return s && (s.value?s.value():String(s));})()`);
  console.log(`SYMBOL: ${sym} | TF: ${tf}`);

  // Pull the raw bar series and compute EVERYTHING locally so nothing is rounded.
  const raw = await e(`(function(){
    var items = ${MAIN}.bars()._items;
    var out = [];
    for (var i = Math.max(0, items.length - 300); i < items.length; i++) {
      var it = items[i];
      var v = it.value || [];
      out.push([v[0]||null, v[1], v[2], v[3], v[4], v[5]]);
    }
    return JSON.stringify(out);
  })()`);

  const bars = JSON.parse(raw);
  console.log(`BARS: ${bars.length} (last ${new Date(bars[bars.length-1][0]*1000).toLocaleTimeString()})`);

  const closes = bars.map(b => b[4]).filter(v => typeof v === 'number');
  const highs = bars.map(b => b[2]).filter(v => typeof v === 'number');
  const lows = bars.map(b => b[3]).filter(v => typeof v === 'number');
  const vols = bars.map(b => b[5]).filter(v => typeof v === 'number');
  const last = bars[bars.length - 1];

  function ema(arr, p) {
    const k = 2 / (p + 1); let e = arr[0];
    for (let i = 1; i < arr.length; i++) e = arr[i] * k + e * (1 - k);
    return e;
  }
  function rsi(arr, p) {
    // 2026-10-07: esta funcion TENIA EL MISMO BUG que monitor.mjs. Sumaba solo
    // los ultimos `p` cambios sin suavizado, asi que NO era el RSI14 estandar:
    // daba 22.09 / 7.31 donde Wilder sobre la misma serie da 28.78 / 15.16.
    // RSI6 7.31 se lee como "sobreventa extrema" cuando el valor real es 15.16.
    // He estado llamando "real" a este numero y era el que estaba mal.
    //
    // Ahora: semilla media simple + suavizado de Wilder sobre toda la serie, que
    // es lo que usan TradingView y cualquier plataforma. Requiere p+1 barras
    // minimo y converge; con 300 barras el resultado es estable.
    if (arr.length < p + 1) return null;
    let g = 0, l = 0;
    for (let i = 1; i <= p; i++) {
      const d = arr[i] - arr[i - 1];
      if (d > 0) g += d; else l -= d;
    }
    let ag = g / p, al = l / p;
    for (let i = p + 1; i < arr.length; i++) {
      const d = arr[i] - arr[i - 1];
      ag = (ag * (p - 1) + (d > 0 ? d : 0)) / p;
      al = (al * (p - 1) + (d < 0 ? -d : 0)) / p;
    }
    return al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  }
  function atr(b, p = 14) {
    if (b.length < p + 1) return null;
    let s = 0;
    for (let i = b.length - p; i < b.length; i++)
      s += Math.max(b[i][2] - b[i][3], Math.abs(b[i][4] - b[i-1][4]));
    return s / p;
  }

  const e9 = ema(closes, 9), e21 = ema(closes, 21), e50 = ema(closes, 50), e200 = closes.length >= 200 ? ema(closes, 200) : null;
  const px = last[4];
  const a = atr(bars);
  console.log('\n=== PRECISE LEVELS ===');
  console.log(`close        ${px}`);
  console.log(`EMA9         ${e9.toFixed(6)}  ${px > e9 ? 'ABOVE' : 'BELOW'} (+${((px-e9)/e9*100).toFixed(3)}%)`);
  console.log(`EMA21        ${e21.toFixed(6)}  ${px > e21 ? 'ABOVE' : 'BELOW'} (+${((px-e21)/e21*100).toFixed(3)}%)`);
  console.log(`EMA50        ${e50.toFixed(6)}  ${px > e50 ? 'ABOVE' : 'BELOW'} (+${((px-e50)/e50*100).toFixed(3)}%)`);
  if (e200) console.log(`EMA200       ${e200.toFixed(6)}  ${px > e200 ? 'ABOVE' : 'BELOW'}`);
  console.log(`RSI14        ${rsi(closes,14).toFixed(2)}   RSI6 ${rsi(closes,6).toFixed(2)}`);
  console.log(`ATR14        ${a ? a.toFixed(6) : '?'}  (${a ? (a/px*100).toFixed(3) : '?'}% of price)`);
  console.log(`EMA9 vs 21   ${e9 > e21 ? 'BULLISH stack' : 'BEARISH stack'} (${((e9-e21)/e21*100).toFixed(3)}%)`);

  const lastV = last[5];
  const vAvg20 = vols.slice(-21, -1).reduce((s, v) => s + v, 0) / Math.min(20, vols.length - 1);
  console.log(`\nvolume last ${lastV ? Math.round(lastV) : '?'} | avg20 ${Math.round(vAvg20)} | ratio ${(lastV/vAvg20).toFixed(2)}x`);

  const recent = bars.slice(-24);
  const hi = Math.max(...recent.map(b => b[2]));
  const lo = Math.min(...recent.map(b => b[3]));
  console.log(`last 24 bars: high ${hi} low ${lo} range ${(hi-lo).toFixed(6)} (${((hi-lo)/px*100).toFixed(3)}%)`);
  console.log(`position in range: ${(((px-lo)/(hi-lo))*100).toFixed(1)}% (0=low,100=high)`);
}
main()
  .catch(e => { console.error('FAIL', e.message); process.exit(1); })
  .finally(() => {
    // The CDP websocket keeps the event loop alive, so the process hangs after
    // printing. That forced every caller to wrap this in a timeout and kill it.
    // Close the connection and exit explicitly. NOTE: a bare close() is not
    // enough if other sockets linger, hence the hard exit.
    try { client && client.close && client.close(); } catch {}
    process.exit(0);
  });