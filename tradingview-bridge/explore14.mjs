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

  // Deep inspect MPS primitives collection
  console.log('=== MPS primitives collection keys ===');
  const info = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      if((sources[i].metaInfo().description||'').indexOf('Master Pattern Scanner')>=0){
        var coll = sources[i]._graphics && sources[i]._graphics._primitivesCollection;
        if(!coll) return 'no coll';
        var keys = [];
        for(var k in coll) keys.push(k);
        return JSON.stringify({keys: keys.join(','), hlinesType: typeof coll.hlines, hlinesKeys: coll.hlines?Object.keys(coll.hlines).join(','):'null'});
      }
    }
    return 'not found';
  })()`);
  console.log('Coll info:', info);

  // Check if hlines is a getter that returns items directly
  console.log('\n=== hlines evaluated ===');
  const hlEval = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      if((sources[i].metaInfo().description||'').indexOf('Master Pattern Scanner')>=0){
        var coll = sources[i]._graphics && sources[i]._graphics._primitivesCollection;
        if(!coll) return 'no coll';
        var hl = coll.hlines;
        if(!hl) return 'hlines is null';
        // Check if it's an array directly
        if(Array.isArray(hl)) return 'hlines is array('+hl.length+')';
        // Check what's on it
        var hlKeys = [];
        for(var k in hl) hlKeys.push(k);
        var hasItems = 'hasItems:'+('_items' in hl);
        var hasLength = 'hasLength:'+('length' in hl);
        // Try _items
        var itemsInfo = '';
        if(hl._items !== undefined) {
          itemsInfo = '_items exists, type='+(typeof hl._items)+' len='+(hl._items.length||'no length')+ ' json='+JSON.stringify(hl._items);
        }
        // Try .items
        if(hl.items !== undefined) {
          itemsInfo += ' .items exists';
        }
        return JSON.stringify({hlKeys:hlKeys.join(','), hasItems:hasItems, hasLength:hasLength, itemsInfo:itemsInfo});
      }
    }
    return 'not found';
  })()`);
  console.log('hlines:', hlEval);

  // Also check Volume study - might have simpler structure
  console.log('\n=== Volume hlines ===');
  const volHL = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      if((sources[i].metaInfo().description||'').indexOf('Volume')>=0){
        var coll = sources[i]._graphics && sources[i]._graphics._primitivesCollection;
        if(!coll) return 'no coll';
        var hl = coll.hlines;
        if(!hl) return 'hlines is null';
        var hlKeys = [];
        for(var k in hl) hlKeys.push(k);
        return JSON.stringify({hlinesKeys: hlKeys.join(','), typeof:_typeof(hl), isArray:Array.isArray(hl)});
      }
    }
    return 'not found';
  })()`);
  console.log('Volume hlines:', volHL);

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
