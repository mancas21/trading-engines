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

  const listButtons = async () => {
    const raw = await e(`(function(){
      var out=[];var ns=document.querySelectorAll('button,[role=button]');
      for(var i=0;i<ns.length;i++){var n=ns[i];var tx=(n.textContent||'').trim();
        if(tx.length>0&&tx.length<=8&&n.offsetParent!==null){out.push(tx+'|'+n.getAttribute('aria-label')+'');}}
      return JSON.stringify(out);})()`);
    try { return JSON.parse(raw); } catch (x) { return []; }
  };

  const before = await listButtons();
  console.log('botones visibles (antes):');
  before.forEach(b => console.log('   ' + b));

  // click en el boton que muestra el TF actual ("240")
  const cur = await tf();
  const opened = await e(`(function(){
    var ns=document.querySelectorAll('button,[role=button]');
    for(var i=0;i<ns.length;i++){var tx=(ns[i].textContent||'').trim();
      if(tx===${JSON.stringify(String(cur))}||tx===${JSON.stringify(String(cur) + 'm')}){ns[i].click();return 'clicked:'+tx;}}
    return 'not-found';})()`);
  console.log('boton TF click:', opened);
  await new Promise(r => setTimeout(r, 1500));
  const after = await listButtons();
  console.log('botones visibles (despues de abrir menu):');
  after.forEach(b => console.log('   ' + b));
}
main().catch(e => console.log('FATAL:', e.message)).finally(() => { try { client && client.close(); } catch { } process.exit(0); });
