import CDP from 'chrome-remote-interface';

async function main() {
  const targets = await CDP.List({host:'localhost',port:9222});
  const t = targets.find(x => x.title && x.title.includes('TradingView'));
  const c = await CDP({host:'localhost',port:9222,target:t.id});
  await c.Runtime.enable();
  const e = async (x) => {
    const r = await c.Runtime.evaluate({expression:x, returnByValue:true, timeout:5000});
    if (r.result.exceptionDetails) return 'ERR: ' + r.result.exceptionDetails.text;
    return r.result.value;
  };

  // Try _exposed_chartWidgetCollection
  console.log('Collection:', await e(`(function(){
    var coll = window._exposed_chartWidgetCollection;
    if(!coll) return 'no coll';
    var keys = Object.keys(coll).join(',');
    return JSON.stringify({keys:keys.substring(0,200), type:typeof coll, count:coll.length});
  })()`));

  // Try widgetbar
  console.log('WidgetBar:', await e(`(function(){
    var wb = window.widgetbar;
    if(!wb) return 'no widgetbar';
    return JSON.stringify({keys:Object.keys(wb).join(',').substring(0,100)});
  })()`));

  // Try TradingView global
  console.log('TradingView:', await e(`(function(){
    var tv = window.TradingView;
    if(!tv) return 'no tv';
    var keys = Object.keys(tv).join(',').substring(0,200);
    return JSON.stringify({keys:keys, type:typeof tv});
  })()`));

  // Try ChartApiInstance
  console.log('ChartApi:', await e(`(function(){
    var ca = window.ChartApiInstance;
    if(!ca) return 'no ChartApiInstance';
    return JSON.stringify({keys:Object.keys(ca).join(',').substring(0,200)});
  })()`));

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
