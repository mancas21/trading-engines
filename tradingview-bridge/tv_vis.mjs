import CDP from 'chrome-remote-interface';
let client;
async function main() {
  const targets = await CDP.List({ host: 'localhost', port: 9222 });
  const t = targets.find(x => x.url && x.url.includes('tradingview.com/chart'));
  client = await CDP({ host: 'localhost', port: 9222, target: t.id });
  await client.Runtime.enable();
  await client.Page.enable();
  const e = async (expr) => {
    const r = await client.Runtime.evaluate({ expression: expr, returnByValue: true, timeout: 10000 });
    if (r.result.exceptionDetails) return 'ERR';
    return r.result.value;
  };
  const vis = await e(`document.visibilityState`);
  const hidden = await e(`document.hidden`);
  const focus = await e(`document.hasFocus()`);
  const w = await e(`(function(){return JSON.stringify({w:window.innerWidth,h:window.innerHeight,ts:Date.now()});})()`);
  let win = null;
  try {
    const r = await client.Browser.getWindowForTarget({ targetId: t.id });
    win = r;
  } catch (err) { win = 'ERR ' + err.message; }
  console.log('visibilityState:', vis, '| document.hidden:', hidden, '| hasFocus:', focus);
  console.log('window inner:', w);
  console.log('Browser.getWindowForTarget:', JSON.stringify(win));
}
main().catch(e => console.log('FATAL:', e.message)).finally(() => { try { client && client.close(); } catch { } process.exit(0); });
