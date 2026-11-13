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
    const r = await client.Runtime.evaluate({ expression: expr, returnByValue: true, timeout: 15000 });
    if (r.result.exceptionDetails) return null;
    return r.result.value;
  };
  const tf = async () => e(`(function(){try{var i=${MAIN}.interval();return i&&(i.value?i.value():String(i));}catch(x){return -1;}})()`);
  const focused = async () => e(`(function(){var a=document.activeElement;return a?(a.tagName||'')+'/'+(a.className||'').toString().slice(0,40):'none';})()`);

  console.log('tf ahora:', await tf(), '| activeElement:', await focused());

  // Teclado: enviar keyDown/keyUp con key+code+keyCode+windowsVirtualKeyCode
  const sendKey = async (ch) => {
    const code = 'Digit' + ch;
    const vk = ch.charCodeAt(0);
    await client.Input.dispatchKeyEvent({ type: 'keyDown', key: ch, code, text: ch, unmodifiedText: ch, keyCode: vk, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    await client.Input.dispatchKeyEvent({ type: 'keyUp', key: ch, code, keyCode: vk, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
  };

  for (const ch of ['2']) {
    await sendKey(ch);
    for (let i = 0; i < 8; i++) {
      await new Promise(r => setTimeout(r, 1200));
      const c = await tf();
      console.log(`  tecla '${ch}' +${((i + 1) * 1.2).toFixed(1)}s -> tf=${c}`);
      if (c === 3) break;
    }
  }
  console.log('tf final:', await tf());
}
main().catch(e => console.log('FATAL:', e.message)).finally(() => { try { client && client.close(); } catch { } process.exit(0); });
