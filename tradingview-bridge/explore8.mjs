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

  // Explore the activeChartWidget object
  console.log('=== activeChartWidget object ===');
  const obj = await e(`(function(){
    var w = window._exposed_chartWidgetCollection.activeChartWidget;
    if(!w) return 'null';
    var keys = [];
    for(var k in w) { if(typeof w[k] !== 'function' && k[0] !== '_') keys.push(k); }
    return JSON.stringify(keys.join(','));
  })()`);
  console.log('Props:', obj);

  // Try calling w as function
  console.log('\nCall as function:', await e('typeof window._exposed_chartWidgetCollection.activeChartWidget()'));

  // Try _activeChartWidgetModel
  console.log('\n=== _activeChartWidgetModel ===');
  const mdl = await e(`(function(){
    var m = window._exposed_chartWidgetCollection._activeChartWidgetModel;
    if(!m) return 'null';
    if(typeof m === 'object') return JSON.stringify(Object.keys(m).filter(k=>k[0]!=='_').join(',').substring(0,200));
    return typeof m;
  })()`);
  console.log('Model:', mdl);

  // Try getting chart data from the exposed model
  console.log('\n=== Layout ===');
  const layout = await e(`(function(){
    var w = window._exposed_chartWidgetCollection.layout;
    if(!w) return 'no layout';
    var keys = Object.keys(w).join(',').substring(0,200);
    return JSON.stringify({keys:keys, type:typeof w});
  })()`);
  console.log('Layout:', layout);

  // Try the monitor's approach again - maybe it needs a fresh property access
  console.log('\n=== Monitor method ===');
  const m2 = await e(`(function(){
    try {
      var wv = window.TradingViewApi._activeChartWidgetWV;
      return JSON.stringify({type:typeof wv, hasValue: typeof wv.value, hasValueFn: typeof wv.value()});
    } catch(e) { return e.message; }
  })()`);
  console.log('WV:', m2);

  // Try the _owner approach
  // The error said Cannot read properties of undefined (reading '_owner')
  // This means wv.value() returned undefined
  // Maybe the value is a getter that returns undefined on second access
  console.log('\n=== Direct chart access ===');
  const direct = await e(`(function(){
    try {
      // Try looking at the iframe content
      var frames = document.querySelectorAll('iframe');
      var results = [];
      for(var i=0;i<frames.length;i++){
        try {
          var f = frames[i];
          var src = f.src || '';
          var id = f.id || '';
          results.push({idx:i, id:id, src:src.substring(0,60)});
        } catch(e) {}
      }
      return JSON.stringify(results);
    } catch(e) { return e.message; }
  })()`);
  console.log('Frames:', direct);

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
