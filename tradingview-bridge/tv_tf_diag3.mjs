import CDP from 'chrome-remote-interface';

const MAIN = 'window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries()';
let client;

async function main() {
  const targets = await CDP.List({ host: 'localhost', port: 9222 });
  const t = targets.find(x => x.url && x.url.includes('tradingview.com/chart'));
  client = await CDP({ host: 'localhost', port: 9222, target: t.id });
  await client.Runtime.enable();
  await client.Page.enable();
  const e = async (expr) => {
    const r = await client.Runtime.evaluate({ expression: expr, returnByValue: true, timeout: 20000 });
    if (r.result.exceptionDetails) return 'ERR';
    return r.result.value;
  };
  const state = async () => {
    const iv = await e(`(function(){try{var i=${MAIN}.interval();return i&&(i.value?i.value():String(i));}catch(x){return 'ERR';}})()`);
    const nb = await e(`(function(){try{return ${MAIN}.bars()._items.length;}catch(x){return -1;}})()`);
    const lt = await e(`(function(){try{var it=${MAIN}.bars()._items;var v=it[it.length-1].value;return v[0];}catch(x){return -1;}})()`);
    return `tf=${iv} bars=${nb} lastBarTs=${lt}`;
  };

  console.log('ANTES: ' + await state());
  console.log('-> Page.bringToFront');
  try { await client.Page.bringToFront(); } catch (err) { console.log('  bringToFront err ' + err.message); }
  await new Promise(r => setTimeout(r, 2000));
  console.log('POST-BRING: ' + await state());

  for (const tf of ['30', '60', '5']) {
    const expr = `window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.chart().setResolution("${tf}",function(){})`;
    const ret = await e(expr);
    console.log(`setResolution("${tf}") ret=${ret}`);
    for (let i = 0; i < 12; i++) {
      await new Promise(r => setTimeout(r, 1500));
      const s = await state();
      console.log(`   +${(i + 1) * 1.5}s ${s}`);
      if (s.includes('tf=' + ({ 30: 5, 60: 7, 5: 3 })[tf])) break;
    }
  }
}
main().catch(e => console.log('FATAL:', e.message)).finally(() => { try { client && client.close(); } catch { } process.exit(0); });
