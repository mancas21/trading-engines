import CDP from 'chrome-remote-interface';

const UP = 60.90, DOWN = 60.58;
let client;

async function connect() {
  const t = await CDP.List({ host: 'localhost', port: 9222 });
  const target = t.find(x => x.url && x.url.includes('tradingview.com/chart')) || t[0];
  if (!target) throw new Error('No TV target found');
  client = await CDP({ host: 'localhost', port: 9222, target: target.id });
  await client.Runtime.enable();
  const p = await getPrice();
  console.log('[ALERT] Watching HYPE/USDT 1m @ $' + p.toFixed(2) + ' | UP>$' + UP + ' DOWN<$' + DOWN);
}

async function getPrice() {
  const { result } = await client.Runtime.evaluate({
    expression: '(function(){var w=window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries();var l=w.bars().last();return l.value[4]})()',
    returnByValue: true
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.value;
}

let triggered = { up: false, down: false };

async function check() {
  while (true) {
    try {
      const p = await getPrice();
      if (!triggered.up && p >= UP) {
        console.log(`\n*** 🚀 BREAKOUT UP: $${p.toFixed(3)} > $${UP} ***\n`);
        triggered.up = true;
      }
      if (!triggered.down && p <= DOWN) {
        console.log(`\n*** 💥 BREAKDOWN: $${p.toFixed(3)} < $${DOWN} ***\n`);
        triggered.down = true;
      }
      if (triggered.up && triggered.down) break;
    } catch (e) {
      // reconnect next loop
    }
    await new Promise(r => setTimeout(r, 3000));
  }
}

check().catch(e => { console.error(e.message); process.exit(1); });
