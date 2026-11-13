import CDP from 'chrome-remote-interface';

let client;
async function main() {
  const targets = await CDP.List({ host: 'localhost', port: 9222 });
  console.log('TARGETS CDP:');
  targets.forEach((t, i) => {
    const u = (t.url || '').slice(0, 110);
    console.log(`  [${i}] id=${t.id.slice(0, 8)} type=${t.type} title="${(t.title || '').slice(0, 60)}"`);
    console.log(`       url=${u}`);
  });

  // probar cada target que parezca chart: leer symbol + interval + bars
  for (const t of targets) {
    if (!/tradingview/.test(t.url || '') && !/TradingView/.test(t.title || '')) continue;
    try {
      const c = await CDP({ host: 'localhost', port: 9222, target: t.id });
      await c.Runtime.enable();
      const e = async (expr) => {
        const r = await c.Runtime.evaluate({ expression: expr, returnByValue: true, timeout: 8000 });
        if (r.result.exceptionDetails) return null;
        return r.result.value;
      };
      const sym = await e(`(function(){try{var s=window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().symbol();return s&&(s.value?s.value():String(s));}catch(x){return 'NO_API';}})()`);
      const iv = await e(`(function(){try{var i=window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().interval();return i&&(i.value?i.value():String(i));}catch(x){return 'ERR';}})()`);
      const nb = await e(`(function(){try{return window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().bars()._items.length;}catch(x){return -1;}})()`);
      console.log(`  -> id=${t.id.slice(0, 8)} symbol=${sym} intervalEnum=${iv} bars=${nb}`);
      c.close();
    } catch (err) {
      console.log(`  -> id=${t.id.slice(0, 8)} ERROR ${err.message}`);
    }
  }
}
main().catch(e => console.log('FATAL:', e.message)).finally(() => { try { client && client.close(); } catch { } process.exit(0); });
