import CDP from 'chrome-remote-interface';

// Multi-TF via UI: enfoca el chart y usa los atajos de intervalo de TradingView
//  1=1m  2=5m  3=15m  4=30m  5=1h  6=4h  7=1D
// SetResolution() no cambia el TF en esta build: el atajo de teclado si.
const MAIN = 'window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries()';
const KEY = { '5': '2', '30': '4', '60': '5', '15': '3', '240': '6' };
const ENUM = { '1': 1, '5': 3, '15': 4, '30': 5, '60': 7, '240': 10, '1D': 11 };
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
  const tfEnum = async () => e(`(function(){try{var i=${MAIN}.interval();return i&&(i.value?i.value():String(i));}catch(x){return -1;}})()`);
  const bars = async () => {
    const raw = await e(`(function(){var items=${MAIN}.bars()._items;var out=[];for(var i=Math.max(0,items.length-300);i<items.length;i++){var v=items[i].value||[];out.push([v[0]||null,v[1],v[2],v[3],v[4],v[5]]);}return JSON.stringify(out);})()`);
    try { return JSON.parse(raw); } catch (x) { return null; }
  };

  // Enfocar el chart: bringToFront + click en el lienzo (area de precio, no la barra de herramientas)
  await client.Page.bringToFront();
  await new Promise(r => setTimeout(r, 1200));
  const vp = e(`(function(){return JSON.stringify({w:window.innerWidth,h:window.innerHeight});})()`);
  console.log('viewport:', await vp);
  const x = 300, y = 250;
  await client.Input.dispatchMouseEvent({ type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
  await client.Input.dispatchMouseEvent({ type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
  await new Promise(r => setTimeout(r, 800));
  console.log('focus tras click:', await e(`document.hasFocus()`), '| tf actual:', await tfEnum());

  const TFS = process.argv[2] ? process.argv[2].split(',') : ['5', '30', '60'];
  for (const tf of TFS) {
    const key = KEY[tf];
    if (!key) { console.log(`sin atajo para ${tf}`); continue; }
    await client.Input.dispatchKeyEvent({ type: 'keyDown', key, text: key, windowsVirtualKeyCode: key.charCodeAt(0) });
    await client.Input.dispatchKeyEvent({ type: 'keyUp', key, windowsVirtualKeyCode: key.charCodeAt(0) });
    // esperar a que el TF cambie
    let ok = false;
    for (let i = 0; i < 14; i++) {
      await new Promise(r => setTimeout(r, 1500));
      const cur = await tfEnum();
      if (String(cur) === String(ENUM[tf])) { ok = true; break; }
    }
    await new Promise(r => setTimeout(r, 3000));
    const b = await bars();
    console.log(`TF ${tf}m -> enum=${await tfEnum()} (esperado ${ENUM[tf]}) ${ok ? 'OK' : 'NO-CAMBIO'} | barras=${b ? b.length : 0}`);
  }
}
main().catch(e => console.log('FATAL:', e.message)).finally(() => { try { client && client.close(); } catch { } process.exit(0); });
