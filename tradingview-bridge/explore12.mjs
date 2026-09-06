import CDP from 'chrome-remote-interface';

async function main() {
  const targets = await CDP.List({host:'localhost',port:9222});
  const t = targets.find(x => x.title && x.title.includes('TradingView'));
  const c = await CDP({host:'localhost',port:9222,target:t.id});
  await c.Runtime.enable();
  const e = async (x) => {
    const r = await c.Runtime.evaluate({expression:x, returnByValue:true, timeout:8000});
    if (r.result.exceptionDetails) return 'ERR: ' + r.result.exceptionDetails.text;
    return r.result.value;
  };

  // Understand bar item structure
  console.log('=== Bar item structure ===');
  const barStruct = await e(`(function(){
    var ms = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries();
    var items = ms.bars()._items;
    if(!items || !items.length) return 'no items';
    var last = items[items.length-1];
    var keys = [];
    for(var k in last) keys.push(k);
    // Also check if array indexing works
    var val1 = items[0];
    return JSON.stringify({
      keys: keys.join(','),
      type: typeof last,
      isArray: Array.isArray(last),
      val0: typeof items[items.length-1][0],
      val1: typeof val1[1],
      hasClose: last.close !== undefined,
      hasValue: last.value !== undefined,
      has_v: last.v !== undefined
    });
  })()`);
  console.log('Bar struct:', barStruct);

  // Try one more - print the last item's actual values
  console.log('\n=== Last bar values ===');
  const lastBar = await e(`(function(){
    var ms = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries();
    var items = ms.bars()._items;
    var last = items[items.length-1];
    var vals = {};
    for(var k in last) {
      if(typeof last[k] !== 'function' && typeof last[k] !== 'object') vals[k] = last[k];
      else if(Array.isArray(last[k])) vals[k] = 'array['+last[k].length+']';
    }
    return JSON.stringify(vals);
  })()`);
  console.log('Last bar:', lastBar);

  // Now try source - check internal properties
  console.log('\n=== Source internal props ===');
  const srcProps = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV.value();
    var sources = wv._chartWidget.model().model().dataSources();
    var s = sources[6]; // Master Pattern Scanner
    var keys = [];
    for(var k in s) keys.push(k);
    return JSON.stringify(keys.join(','));
  })()`);
  console.log('MPS keys:', srcProps);

  // Check what's in _graphics for MPS
  console.log('\n=== MPS graphics ===');
  const mpsGfx = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV.value();
    var sources = wv._chartWidget.model().model().dataSources();
    var s = sources[6]; // Master Pattern Scanner
    var g = s._graphics;
    if(!g) return 'no graphics';
    var gKeys = Object.keys(g).join(',');
    var coll = g._primitivesCollection;
    if(!coll) return 'no collection, keys: '+gKeys;
    var collKeys = Object.keys(coll).join(',');
    // Check primitives
    if(coll._primitives) {
      return 'coll keys: '+collKeys+' primitives: '+coll._primitives.length;
    }
    // maybe _items?
    if(coll._items) {
      return 'coll keys: '+collKeys+' items: '+coll._items.length;
    }
    return 'coll keys: '+collKeys+' no primitives';
  })()`);
  console.log('MPS graphics:', mpsGfx);

  // Check for all sources _graphics primitives
  console.log('\n=== All graphics ===');
  const allGfx = await e(`(function(){
    var wv = window.TradingViewApi._activeChartWidgetWV.value();
    var sources = wv._chartWidget.model().model().dataSources();
    var out = {};
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      var g = s._graphics;
      if(!g) continue;
      try {
        var name = (s.metaInfo().description || s.metaInfo().shortName || 'src-'+i).substring(0,50);
        var coll = g._primitivesCollection;
        if(!coll) { out[name] = 'no coll'; continue; }
        var prims = coll._primitives;
        if(prims) { out[name] = prims.length+' primitives'; continue; }
        var items = coll._items;
        if(items) { out[name] = items.length+' items'; continue; }
        out[name] = 'has coll, no prims/items, keys:'+Object.keys(coll).join(',');
      } catch(e) { out['src-'+i] = e.message.substring(0,30); }
    }
    return JSON.stringify(out);
  })()`);
  console.log('All graphics:', allGfx);

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
