import CDP from 'chrome-remote-interface';

// Multi-TF TradingView (5m / 30m / 1h) via clicks DOM en la barra de intervalos.
// setResolution() no cambia el TF en esta build de TV; el boton del DOM si.
// Al terminar restaura el TF original (5m).
const MAIN = 'window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries()';
const TFS = (process.argv[2] ? process.argv[2].split(',') : ['5m', '30m', '1h']);
const ENUM = { '1m': 1, '3m': 2, '5m': 3, '15m': 4, '30m': 5, '1h': 7, '4h': 10, '1D': 11 };
let client;

async function main() {
  const targets = await CDP.List({ host: 'localhost', port: 9222 });
  const t = targets.find(x => x.url && x.url.includes('tradingview.com/chart'));
  client = await CDP({ host: 'localhost', port: 9222, target: t.id });
  await client.Runtime.enable();
  await client.Page.enable();
  const e = async (expr) => {
    const r = await client.Runtime.evaluate({ expression: expr, returnByValue: true, timeout: 20000 });
    if (r.result.exceptionDetails) return null;
    return r.result.value;
  };
  const tfRaw = async () => e(`(function(){try{var i=${MAIN}.interval();return i&&(i.value?i.value():String(i));}catch(x){return -1;}})()`);
  const tfMatches = async (label) => {
    const v = await tfRaw();
    if (v === null) return false;
    const s = String(v);
    return s === label || s === label.replace('m', '') || s === String(ENUM[label]);
  };
  const clickTf = async (label) => e(`(function(){
    var ns=document.querySelectorAll('button[aria-label]');
    for(var i=0;i<ns.length;i++){
      var al=ns[i].getAttribute('aria-label')||'';
      if(al===${JSON.stringify(label === '5m' ? '5 minutos' : label === '30m' ? '30 minutos' : label === '1h' ? '1 hora' : label === '15m' ? '15 minutos' : '4 horas')}){ns[i].click();return 'clicked '+al;}
    }
    return 'not-found';})()`);
  const readBars = async () => {
    const raw = await e(`(function(){var items=${MAIN}.bars()._items;var out=[];for(var i=Math.max(0,items.length-300);i<items.length;i++){var v=items[i].value||[];out.push([v[0]||null,v[1],v[2],v[3],v[4],v[5]]);}return JSON.stringify(out);})()`);
    try { return JSON.parse(raw); } catch (x) { return null; }
  };

  const sym = await e(`${MAIN}.symbol()`);
  console.log(`SIMBOLO: ${sym}`);
  console.log('='.repeat(104));

  for (const tf of TFS) {
    const cl = await clickTf(tf);
    let ok = false;
    for (let i = 0; i < 14; i++) {
      await new Promise(r => setTimeout(r, 1200));
      if (await tfMatches(tf)) { ok = true; break; }
    }
    await new Promise(r => setTimeout(r, 3000));
    const bars = await readBars();
    if (!bars || bars.length < 30) { console.log(`\n--- ${tf} --- sin datos (${bars ? bars.length : 0}) | ${cl}`); continue; }

    const cl_ = bars.map(b => b[4]).filter(x => typeof x === 'number');
    const hi_ = bars.map(b => b[2]).filter(x => typeof x === 'number');
    const lo_ = bars.map(b => b[3]).filter(x => typeof x === 'number');
    const vo_ = bars.map(b => b[5]).filter(x => typeof x === 'number');
    const px = cl_[cl_.length - 1];

    const ema = (a, p) => { const k = 2 / (p + 1); let x = a[0]; for (let i = 1; i < a.length; i++) x = a[i] * k + x * (1 - k); return x; };
    const rsi = (a, p) => {
      if (a.length < p + 1) return null;
      let g = 0, l = 0;
      for (let i = 1; i <= p; i++) { const d = a[i] - a[i - 1]; if (d > 0) g += d; else l -= d; }
      let ag = g / p, al = l / p;
      for (let i = p + 1; i < a.length; i++) { const d = a[i] - a[i - 1]; ag = (ag * (p - 1) + (d > 0 ? d : 0)) / p; al = (al * (p - 1) + (d < 0 ? -d : 0)) / p; }
      return al === 0 ? 100 : 100 - 100 / (1 + ag / al);
    };
    const atr = (b, p = 14) => { let s = 0; for (let i = b.length - p; i < b.length; i++) s += Math.max(b[i][2] - b[i][3], Math.abs(b[i][4] - b[i - 1][4])); return s / p; };
    const pct = (a, b) => b ? ((a - b) / b * 100) : 0;

    const e9 = ema(cl_, 9), e21 = ema(cl_, 21), e50 = ema(cl_, 50), e200 = cl_.length >= 200 ? ema(cl_, 200) : null;
    const e9p = ema(cl_.slice(0, -1), 9), e21p = ema(cl_.slice(0, -1), 21);
    const r14 = rsi(cl_, 14), r6 = rsi(cl_, 6), a14 = atr(bars);
    const vAvg20 = vo_.slice(-21, -1).reduce((s, v) => s + v, 0) / Math.min(20, vo_.length - 1);
    const vr = (vo_[vo_.length - 1] / vAvg20).toFixed(2);
    const vAvg50 = vo_.slice(-51, -1).reduce((s, v) => s + v, 0) / Math.min(50, vo_.length - 1);
    const vr5 = (vo_.slice(-5).reduce((s, v) => s + v, 0) / 5 / vAvg50).toFixed(2);

    const sh = [], sl = [];
    for (let i = 2; i < bars.length - 2; i++) {
      if (bars[i][2] > bars[i - 1][2] && bars[i][2] > bars[i - 2][2] && bars[i][2] >= bars[i + 1][2] && bars[i][2] >= bars[i + 2][2]) sh.push(bars[i][2]);
      if (bars[i][3] < bars[i - 1][3] && bars[i][3] < bars[i - 2][3] && bars[i][3] <= bars[i + 1][3] && bars[i][3] <= bars[i + 2][3]) sl.push(bars[i][3]);
    }
    const lastSh = sh[sh.length - 1], prevSh = sh[sh.length - 2];
    const lastSl = sl[sl.length - 1], prevSl = sl[sl.length - 2];
    let struct = 'INDETERMINADA';
    if (lastSh && lastSl && prevSh && prevSl) {
      if (lastSh > prevSh && lastSl > prevSl) struct = 'ALCISTA (HH + HL)';
      else if (lastSh < prevSh && lastSl < prevSl) struct = 'BAJISTA (LH + LL)';
      else if (lastSh > prevSh && lastSl < prevSl) struct = 'AMBIGUA (HH + LL)';
      else struct = 'COMPRESION (LH + HL)';
    }
    const bias = px > e9 && e9 > e21 ? 'ALCISTA' : px < e9 && e9 < e21 ? 'BAJISTA' : px < e9 && e9 > e21 ? 'PULLBACK' : px > e9 && e9 < e21 ? 'RECUPERACION' : 'NEUTRAL';
    const hi20 = Math.max(...hi_.slice(-20)), lo20 = Math.min(...lo_.slice(-20));
    const posR = ((px - lo20) / (hi20 - lo20) * 100).toFixed(0);
    const lastT = new Date(bars[bars.length - 1][0] * 1000).toISOString().slice(0, 16).replace('T', ' ');

    console.log(`\n--- ${tf}  (${cl}, tf=${await tfRaw()}, barra ${lastT} UTC) ---`);
    console.log(`  Precio ${px.toFixed(6)}  |  20b ${pct(px, cl_[Math.max(0, cl_.length - 21)]).toFixed(2)}%  |  50b ${pct(px, cl_[Math.max(0, cl_.length - 51)]).toFixed(2)}%`);
    console.log(`  EMA9 ${e9.toFixed(6)} (${pct(e9, e9p) >= 0 ? '+' : ''}${pct(e9, e9p).toFixed(3)}%)  |  EMA21 ${e21.toFixed(6)} (${pct(e21, e21p) >= 0 ? '+' : ''}${pct(e21, e21p).toFixed(3)}%)  |  stack ${e9 > e21 ? 'BULL' : 'BEAR'}`);
    console.log(`  EMA50 ${e50.toFixed(6)}  |  EMA200 ${e200 ? e200.toFixed(6) : '-'}  |  px vs EMA50 ${pct(px, e50) >= 0 ? '+' : ''}${pct(px, e50).toFixed(2)}%`);
    console.log(`  RSI14 ${r14.toFixed(1)}  |  RSI6 ${r6.toFixed(1)}  |  ATR14 ${a14.toFixed(6)} (${(a14 / px * 100).toFixed(2)}%)`);
    console.log(`  Vol: barra ${vr}x vs20  |  5 vs 50: ${vr5}x  |  rango20 ${(hi20 - lo20).toFixed(6)}  |  pos en rango ${posR}%`);
    console.log(`  Estructura: ${struct}  |  SwingH ${lastSh ? lastSh.toFixed(6) : '-'}  SwingL ${lastSl ? lastSl.toFixed(6) : '-'}`);
    console.log(`  Soporte ${Math.min(...lo_.slice(-10)).toFixed(6)}  |  Resistencia ${Math.max(...hi_.slice(-10)).toFixed(6)}`);
    console.log(`  BIAS: ${bias}`);
  }

  // restaurar TF original
  await clickTf('5m');
  await new Promise(r => setTimeout(r, 3000));
  console.log(`\n[restaurado] TF ahora: ${await tfRaw()}`);
  console.log('DONE');
}
main().catch(e => { console.log('FATAL:', e.message); process.exit(1); })
  .finally(() => { try { client && client.close(); } catch { } process.exit(0); });
