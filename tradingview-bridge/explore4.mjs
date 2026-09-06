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

  // Check if it's a different version - look for chart widget
  console.log('ActiveWidget:', await e(`(function(){
    var api = window.TradingViewApi;
    if(!api) return 'no api';
    var keys = Object.keys(api).filter(k=>k[0]!=='_').join(',');
    var hasWidget = !!api._activeChartWidgetWV;
    var hasWidget2 = !!api._widgets;
    return JSON.stringify({keys: keys.substring(0,100), hasWidget: hasWidget, hasWidgets: hasWidget2, type: typeof api._activeChartWidgetWV});
  })()`));

  // Try accessing via widget
  console.log('Widgets:', await e(`(function(){
    var api = window.TradingViewApi;
    if(api._widgets) {
      var w = api._widgets;
      var keys = Object.keys(w);
      return JSON.stringify({count: keys.length, first: keys[0], type: typeof w[keys[0]]});
    }
    return 'no widgets';
  })()`));

  // Try to find chart widget differently
  console.log('Window keys:', await e(`(function(){
    var keys = [];
    for(var k in window) {
      if(k.indexOf('Chart')>=0 || k.indexOf('Widget')>=0 || k.indexOf('widget')>=0 || k.indexOf('chart')>=0 || k.indexOf('TradingView')>=0) keys.push(k);
    }
    return keys.join(', ');
  })()`));

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
