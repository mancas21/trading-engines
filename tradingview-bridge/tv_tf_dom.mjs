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

  // 1) desenfocar cualquier input (mi click anterior enfoco el search box)
  await e(`(function(){try{if(document.activeElement&&document.activeElement.blur)document.activeElement.blur();}catch(x){}return 1;})()`);

  // 2) enumerar botones candidatos de intervalo
  const cands = await e(`(function(){
    var out=[];
    var nodes=document.querySelectorAll('button,[role=button],a');
    for(var i=0;i<nodes.length;i++){
      var n=nodes[i];
      var txt=(n.textContent||'').trim();
      var al=n.getAttribute('aria-label')||'';
      var ti=n.getAttribute('title')||'';
      var dn=n.getAttribute('data-name')||'';
      var key=(txt+'|'+al+'|'+ti+'|'+dn);
      if(/^(5|15|30|60|240|D|W)$/.test(txt)||/^(\d+ ?m|1h|4h|1D|1W)$/i.test(txt)){
        out.push({tag:n.tagName,txt:txt,al:al,ti:ti,dn:dn,vis:n.offsetParent!==null});
      }
    }
    return JSON.stringify(out);
  })()`);
  let list = [];
  try { list = JSON.parse(cands); } catch (x) { }
  console.log('botones de intervalo encontrados:', list.length);
  list.slice(0, 40).forEach(b => console.log(`   "${b.txt}" al="${b.al}" ti="${b.ti}" dn="${b.dn}" vis=${b.vis}`));

  // 3) mapa texto->enum TV (1=1m 3=5m 4=15m 5=30m 7=1h 10=4h 11=1D)
  const WANT = { '5': 3, '30': 5, '60': 7, '15': 4, '240': 10 };
  for (const [label, wantEnum] of Object.entries(WANT)) {
    const target = ['5', '15', '30', '1h', '4h'].find(x => x === label || (label === '60' && x === '1h') || (label === '240' && x === '4h'));
    const clicked = await e(`(function(){
      var nodes=document.querySelectorAll('button,[role=button]');
      for(var i=0;i<nodes.length;i++){
        var txt=(nodes[i].textContent||'').trim();
        if(txt===${JSON.stringify(target)}){ nodes[i].click(); return 'clicked:'+txt; }
      }
      return 'not-found';
    })()`);
    let cur = null;
    for (let i = 0; i < 12; i++) {
      await new Promise(r => setTimeout(r, 1500));
      cur = await tf();
      if (String(cur) === String(wantEnum)) break;
    }
    await new Promise(r => setTimeout(r, 2500));
    console.log(`-> ${target} (${clicked}) => tf=${await tf()} esperado=${wantEnum} ${String(cur) === String(wantEnum) ? 'OK' : 'NO'}`);
  }
  console.log('tf final:', await tf());
}
main().catch(e => console.log('FATAL:', e.message)).finally(() => { try { client && client.close(); } catch { } process.exit(0); });
