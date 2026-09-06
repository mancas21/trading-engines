import CDP from 'chrome-remote-interface';

async function main() {
  const targets = await CDP.List({host:'localhost',port:9222});
  // Try different targets
  for (const t of targets) {
    if (!t.url || t.url.includes('doubleclick') || t.url.includes('recaptcha') || t.url.includes('blob')) continue;
    try {
      const c = await CDP({host:'localhost',port:9222,target:t.id});
      await c.Runtime.enable();

      const e = async (x) => {
        const r = await c.Runtime.evaluate({expression:x, returnByValue:true, timeout:3000});
        if (r.result.exceptionDetails) return 'ERR: ' + r.result.exceptionDetails.text;
        return r.result.value;
      };

      const hasWidget = await e('typeof window.TradingViewApi !== "undefined"');
      if (hasWidget === true) {
        console.log(`TARGET: ${t.title || t.url?.substring(0,60)}`);
        console.log(`  URL: ${(t.url||'').substring(0,80)}`);

        // Try accessing the widget
        const wv = await e('typeof window.TradingViewApi._activeChartWidgetWV');
        console.log(`  _activeChartWidgetWV: ${wv}`);

        // Try the old way
        const wvVal = await e('try{return typeof window.TradingViewApi._activeChartWidgetWV.value;}catch(e){return e.message}');
        console.log(`  .value: ${wvVal}`);

        // Try to evaluate a simple expression
        const sym = await e(`(function(){
          try {
            var wv = window.TradingViewApi._activeChartWidgetWV;
            if (!wv) return 'no wv';
            var v = wv.value;
            if (typeof v === 'function') v = v();
            if (!v) return 'no value';
            var cw = v._chartWidget;
            if (!cw) return 'no chartWidget';
            return cw.chart().symbol();
          } catch(e) { return e.message; }
        })()`);
        console.log(`  Symbol: ${sym}`);

        c.close();
      } else {
        c.close();
      }
    } catch(e) {
      // skip
    }
  }
  console.log('Done');
}

main().catch(e => console.log('FATAL:', e.message));
