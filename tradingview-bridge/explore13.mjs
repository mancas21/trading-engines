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

  // Check hlines structure for MPS
  console.log('=== MPS hlines ===');
  const hl = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      if((s.metaInfo().description||'').indexOf('Master Pattern Scanner')>=0){
        var coll = s._graphics && s._graphics._primitivesCollection;
        if(!coll) return 'no coll';
        var hl = coll.hlines;
        if(!hl) return 'no hlines';
        var keys = Object.keys(hl).join(',');
        var items = hl._items;
        if(items && items.length) {
          var itemKeys = Object.keys(items[0]).join(',');
          return JSON.stringify({keys: keys.substring(0,100), itemCount: items.length, itemKeys: itemKeys.substring(0,100), firstPrice: items[0]._price});
        }
        return JSON.stringify({keys: keys.substring(0,100), noItems: true});
      }
    }
    return 'not found';
  })()`);
  console.log('hlines:', hl);

  // Check dwgtables structure for MPS
  console.log('\n=== MPS dwgtables ===');
  const tbl = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<sources.length;i++){
      var s = sources[i];
      if((s.metaInfo().description||'').indexOf('Master Pattern Scanner')>=0){
        var coll = s._graphics && s._graphics._primitivesCollection;
        if(!coll) return 'no coll';
        var t = coll.dwgtables;
        if(!t) return 'no dwgtables';
        var keys = Object.keys(t).join(',');
        var items = t._items;
        if(items && items.length) {
          var item = items[0];
          var itemKeys = Object.keys(item).join(',');
          var cells = item._cells;
          var cellInfo = '';
          if(cells && cells.length) {
            var c0 = cells[0];
            var c0keys = Object.keys(c0).join(',');
            cellInfo = 'cells:'+cells.length+' firstKeys:'+c0keys.substring(0,100)+' firstText:'+(c0._text||'')+' firstTag:'+(c0._tag||'');
          } else {
            cellInfo = 'no cells';
          }
          return JSON.stringify({keys: keys.substring(0,100), itemCount: items.length, itemType: item._type, cellInfo: cellInfo});
        }
        return JSON.stringify({keys: keys.substring(0,100), noItems: true});
      }
    }
    return 'not found';
  })()`);
  console.log('dwgtables:', tbl);

  // Check if there are any primitives in the old location 
  console.log('\n=== Any hlines from any study? ===');
  const anyHL = await e(`(function(){
    var sources = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    var out = {};
    for(var i=3;i<sources.length;i++){
      try {
        var name = (sources[i].metaInfo().description||sources[i].metaInfo().shortName||'src-'+i).substring(0,40);
        var coll = sources[i]._graphics && sources[i]._graphics._primitivesCollection;
        if(!coll) continue;
        for(var key of ['hlines','dwglines','lines','dwgtables','dwglabels']) {
          var c = coll[key];
          if(c && c._items && c._items.length) {
            out[name+'/'+key] = c._items.length + ' items';
          }
        }
      } catch(e) {}
    }
    return JSON.stringify(out);
  })()`);
  console.log('Any drawings:', anyHL);

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
