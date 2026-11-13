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
    if (r.result.exceptionDetails) return 'ERR:' + (r.result.exceptionDetails.text || 'exc');
    return r.result.value;
  };
  const tf = async () => e(`(function(){try{var i=${MAIN}.interval();return i&&(i.value?i.value():String(i));}catch(x){return -1;}})()`);
  const active = async () => e(`(function(){var a=document.activeElement;return a?(a.tagName||''):'none';})()`);

  // canvas mas grande = pane de precio
  const rect = await e(`(function(){
    var cs=document.querySelectorAll('canvas'), best=null, ba=0;
    for(var i=0;i<cs.length;i++){var r=cs[i].getBoundingClientRect();var a=r.width*r.height;if(a>ba){ba=a;best=r;}}
    if(!best) return 'null';
    return JSON.stringify({x:Math.round(best.left+best.width*0.5),y:Math.round(best.top+best.height*0.5),w:Math.round(best.width),h:Math.round(best.height)});
  })()`);
  console.log('canvas pane:', rect);

  // desenfocar inputs y hacer click en el canvas
  await e(`(function(){try{document.activeElement&&document.activeElement.blur&&document.activeElement.blur();}catch(x){}return 1;})()`);
  const r = JSON.parse(rect);
  await client.Input.dispatchMouseEvent({ type: 'mousePressed', x: r.x, y: r.y, button: 'left', clickCount: 1 });
  await client.Input.dispatchMouseEvent({ type: 'mouseReleased', x: r.x, y: r.y, button: 'left', clickCount: 1 });
  await new Promise(res => setTimeout(res, 700));
  console.log('activeElement tras click en canvas:', await active(), '| tf:', await tf());

  const sendKey = async (ch) => {
    const vk = ch.charCodeAt(0);
    await client.Input.dispatchKeyEvent({ type: 'keyDown', key: ch, code: 'Digit' + ch, text: ch, unmodifiedText: ch, keyCode: vk, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    await client.Input.dispatchKeyEvent({ type: 'keyUp', key: ch, code: 'Digit' + ch, keyCode: vk, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
  };

  for (const [ch, want] of [['2', '5'], ['4', '30'], ['5', '60']]) {
    await sendKey(ch);
    let cur = null;
    for (let i = 0; i < 10; i++) {
      await new Promise(res => setTimeout(res, 1200));
      cur = await tf();
      if (String(cur) === want) break;
    }
    await new Promise(res => setTimeout(res, 2000));
    console.log(`tecla '${ch}' -> tf=${await tf()} (querido ${want}) ${String(cur) === want ? 'OK' : 'NO'}`);
  }
}
main().catch(e => console.log('FATAL:', e.message)).finally(() => { try { client && client.close(); } catch { } process.exit(0); });
