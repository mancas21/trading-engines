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

  // Show TV version
  console.log('TV Ver:', await e('window.TradingViewApi && window.TradingViewApi.version()'));
  console.log('TV API:', await e('typeof window.TradingViewApi'));

  // Show the dataSources structure
  const info = await e(`(function(){
    var s = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    var out = [];
    for(var i=0;i<s.length;i++){
      var keys = Object.keys(s[i]).filter(k => k[0] !== '_').join(',');
      var m = s[i].metaInfo;
      var mt = typeof m;
      var m2 = s[i]._metaInfo;
      var desc = '';
      if(m && typeof m === 'object') desc = (m.description || m.shortName || '') + '';
      if(!desc && m2) desc = (m2.description || '') + '';
      out.push({idx:i, id:s[i].id_ || s[i].id, keys:keys.substring(0,60), metaType:mt, desc:desc.substring(0,50), hasPlots:!!s[i]._plots});
    }
    return JSON.stringify(out);
  })()`);
  console.log('DATA SOURCES:');
  if (info) JSON.parse(info).forEach(d => console.log(`  [${d.idx}] ${d.id} | ${d.keys} | ${d.desc}`));

  // Get plot values from main series
  console.log('\nVALUE AREA:');
  const va = await e(`(function(){
    var s = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<s.length;i++){
      var m = s[i].metaInfo;
      if(m && m.description && m.description.indexOf('Master Pattern Scanner') >= 0){
        return 'FOUND MPS at idx ' + i + ' type=' + (typeof m);
      }
      if(m && m.shortName && m.shortName.indexOf('Master Pattern Scanner') >= 0){
        return 'FOUND MPS SHORT at idx ' + i;
      }
    }
    return 'NOT found';
  })()`);
  console.log('MPS Search:', va);

  // Show full info for MPS
  const mps = await e(`(function(){
    var s = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();
    for(var i=0;i<s.length;i++){
      var src = s[i];
      var info = {idx:i, id:src.id_||src.id};
      // Get all props
      var allKeys = [];
      for(var k in src) allKeys.push(k);
      info.keys = allKeys.join(',');
      // Try metaInfo as property
      var m = src.metaInfo;
      if(m && typeof m === 'object') {
        info.metaKeys = Object.keys(m).join(',');
        info.desc = m.description;
        info.short = m.shortName;
        return JSON.stringify(info);
      }
    }
    return 'NOT FOUND';
  })()`);
  console.log('\nMPS DETAIL:', mps);

  c.close();
}

main().catch(e => console.log('FATAL:', e.message));
