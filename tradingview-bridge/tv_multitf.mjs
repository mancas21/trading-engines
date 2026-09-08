import CDP from 'chrome-remote-interface';

async function main() {
  const targets = await CDP.List({host:'localhost',port:9222});
  const chart = targets.find(t => t.url && t.url.includes('tradingview.com/chart')) || targets[0];
  const client = await CDP({host:'localhost',port:9222,target:chart.id});
  await client.Runtime.enable();
  const e = async (expr) => {
    const r = await client.Runtime.evaluate({expression:expr, returnByValue:true, timeout:8000});
    if (r.result.exceptionDetails) return 'ERR: '+r.result.exceptionDetails.text;
    return r.result.value;
  };

  // Pine graphics levels from indicators
  const pineLevels = await e(`(function(){var wv=window.TradingViewApi._activeChartWidgetWV;var ds=window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();var out={};for(var i=3;i<ds.length;i++){try{var s=ds[i];var name=(s.metaInfo().description||s.metaInfo().shortName||"").substring(0,40);var coll=s._graphics&&s._graphics._primitivesCollection;if(!coll)continue;var hlines=coll.hlines;if(hlines&&hlines._items){var prices=hlines._items.map(function(h){return Math.round(h._price*10000)/10000;}).filter(function(p){return!isNaN(p);});if(prices.length)out[name+"_hlines"]=prices.slice(0,8);var labels=coll.dwglabels;if(labels&&labels._items){var texts=labels._items.map(function(l){return l._text||"";}).filter(function(t){return t;});if(texts.length)out[name+"_labels"]=texts.slice(0,10);}}catch(e){}}return JSON.stringify(out);})()`);
  console.log("Pine Levels:", pineLevels);

  for (const tf of ['30', '60', '240']) {
    await client.Runtime.evaluate({expression:`window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.chart().setResolution("${tf}",function(){}`, awaitPromise:false});
    await new Promise(r=>setTimeout(r,3500));
    const bars = await e(`(function(){var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();var items=ms.bars()._items,l=items.length,o=[];for(var i=Math.max(0,l-80);i<l;i++){var item=items[i];if(item&&item.value)o.push({o:item.value[1],h:item.value[2],l:item.value[3],c:item.value[4],v:item.value[5]})}return o;})()`);
    const d = bars.result.value;
    if (!d || d.length < 10) { console.log(`${tf}m: insufficient data`); continue; }
    const c = d.map(b=>b.c), h = d.map(b=>b.h), lo = d.map(b=>b.l), v = d.map(b=>b.v);
    const last = c[c.length-1];
    function ema(arr,p){const k=2/(p+1);let e=arr.slice(0,p).reduce((a,b)=>a+b,0)/p;for(let i=p;i<arr.length;i++)e=arr[i]*k+e*(1-k);return e;}
    function rsi(arr,p){let g=0,ls=0;for(let i=arr.length-p;i<arr.length;i++){const dv=arr[i]-arr[i-1];if(dv>=0)g+=dv;else ls+=-dv;}const ag=g/p,al=ls/p;return al===0?100:100-100/(1+ag/al);}
    const e9=ema(c,9), e21=ema(c,21), e50=c.length>=50?ema(c,50):ema(c,Math.min(30,c.length));
    const r14=rsi(c,14);
    const avgV=v.slice(-20).reduce((a,b)=>a+b,0)/Math.min(20,v.length);
    const vr=(v[v.length-1]/avgV).toFixed(1);
    const cross = e9>e21?'9>21':'9<21';
    let sh=null, sl=null;
    const last20=d.slice(-20);
    for(let i=1;i<last20.length-1;i++){if(last20[i].h>last20[i-1].h&&last20[i].h>last20[i+1].h)sh=last20[i].h;if(last20[i].l<last20[i-1].l&&last20[i].l<last20[i+1].l)sl=last20[i].l;}
    let bias='NEUTRAL';
    if(last>e9&&e9>e21)bias='BULLISH';else if(last<e9&&e9<e21)bias='BEARISH';else if(last<e9&&e9>e21)bias='PULLBACK';else if(last>e9&&e9<e21)bias='RECOVERY';
    console.log(`${tf}m | P:${last.toFixed(4)} | EMA9:${e9.toFixed(4)} EMA21:${e21.toFixed(4)} EMA50:${e50?.toFixed(4)} | RSI:${r14.toFixed(1)} | Cross:${cross} | Vol:${vr}x | Range:${(Math.max(...h)-Math.min(...lo)).toFixed(4)} | Bias:${bias} | SH:${sh?.toFixed(4)} SL:${sl?.toFixed(4)} | bars:${d.length}`);
  }
  client.close();
  console.log('DONE');
}
main().catch(e=>{console.log('FATAL:',e.message);});
