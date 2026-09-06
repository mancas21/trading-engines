import CDP from 'chrome-remote-interface';
import dotenv from 'dotenv';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
const __dirname = dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: resolve(__dirname, '.env') });

const TG_TOKEN = process.env.TELEGRAM_TOKEN;
const TG_CHAT = process.env.TELEGRAM_CHAT_ID;

const INTERVAL = Number(process.argv[2]) || 8;

// Thresholds for SPCXUSDT.P
const LEVELS = {
  resistance: 160.00,
  support: 152.00,
  majorSupport: 148.00
};

// === Periodic analysis Telegram delivery ===
const ANALYSIS_INTERVAL_MS = 5 * 60 * 1000; // 5 min entre análisis completos
let lastAnalysisTime = 0; // 0 = enviar en el primer ciclo

let client, lastPrice = 0;
let triggeredRes = false, triggeredSup = false, triggeredMajor = false;
let lastTgTime = 0;
let priceHist = [], rsiHist = [];
let sentRsOverbought = false, sentRsOversold = false;
let sentBearDiv = false, sentBullDiv = false;
let sentEmaCross = null;
// === Rebound/breakdown confirmation tracking ===
let testingSupport = false;
let supportTestPrice = 0;
let lastBounceAlert = 0;
let lastBreakdownAlert = 0;
// === Reversal / Continuation tracking ===
let trendState = 'NEUTRAL'; // BEARISH or NEUTRAL
let lowestDuringTrend = Infinity;
let sentReversalAlert = 0;
let sentContinuationAlert = 0;

async function findChartTarget() {
  const targets = await CDP.List({ host: '127.0.0.1', port: 9222 });
  return targets.find(t => t.url && (t.url.includes('tradingview.com/chart') || t.title.includes('TradingView'))) || targets[0];
}

async function connect() {
  const target = await findChartTarget();
  if (!target) throw new Error('No TV target');
  client = await CDP({ host: '127.0.0.1', port: 9222, target: target.id });
  await client.Runtime.enable();
}

async function getQuote() {
  const { result } = await client.Runtime.evaluate({
    expression: '(function(){var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();var last=ms.bars().last();return{sym:ms.symbol(),close:last.value[4],open:last.value[1],high:last.value[2],low:last.value[3],vol:last.value[5]}})()',
    returnByValue: true, awaitPromise: false
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.value;
}

async function getMps() {
  const { result } = await client.Runtime.evaluate({
    expression: '(function(){var s=window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();for(var i=0;i<s.length;i++){var m=s[i].metaInfo();if(m&&m.description&&m.description.indexOf("Master Pattern Scanner")>=0){var p=s[i]._graphics&&s[i]._graphics._primitivesCollection;if(!p||!p._primitives)return null;for(var j=0;j<p._primitives.length;j++){var prim=p._primitives[j];if(prim._type==="table"){var r={};for(var k=0;k<prim._rows;k++){var c0=prim._cells&&prim._cells[k]&&prim._cells[k][0];var c1=prim._cells&&prim._cells[k]&&prim._cells[k][1];if(c0&&c0._text&&c1&&c1._text)r[c0._text]=c1._text}return r}}}}return null}})()',
    returnByValue: true, awaitPromise: false
  });
  if (result.exceptionDetails) return null;
  return result.value;
}

// Calculate RSI from price history
function calcRsi(bars, period = 14) {
  if (bars.length < period + 1) return null;
  let gains = 0, losses = 0;
  for (let i = bars.length - period; i < bars.length; i++) {
    const diff = bars[i].c - bars[i - 1].c;
    if (diff >= 0) gains += diff;
    else losses -= diff;
  }
  const avgGain = gains / period;
  const avgLoss = losses / period;
  if (avgLoss === 0) return 100;
  const rs = avgGain / avgLoss;
  return 100 - (100 / (1 + rs));
}

// Detect divergence: compare price highs/lows vs RSI highs/lows
function detectDivergence(priceHist, rsiHist) {
  if (priceHist.length < 10 || rsiHist.length < 10) return null;
  const lookback = 8;
  const p = priceHist.slice(-lookback);
  const r = rsiHist.slice(-lookback);

  // Find local highs/lows
  let pHighIdx = 0, pLowIdx = 0, rHighIdx = 0, rLowIdx = 0;
  for (let i = 1; i < p.length; i++) {
    if (p[i] > p[pHighIdx]) pHighIdx = i;
    if (p[i] < p[pLowIdx]) pLowIdx = i;
    if (r[i] > r[rHighIdx]) rHighIdx = i;
    if (r[i] < r[rLowIdx]) rLowIdx = i;
  }

  // Bearish divergence: price makes higher high, RSI makes lower high
  if (pHighIdx > 0 && rHighIdx > 0 && pHighIdx !== rHighIdx) {
    if (p[pHighIdx] > p[pHighIdx - 1] && r[rHighIdx] < r[rHighIdx - 1]) {
      return 'bearish';
    }
  }

  // Bullish divergence: price makes lower low, RSI makes higher low
  if (pLowIdx > 0 && rLowIdx > 0 && pLowIdx !== rLowIdx) {
    if (p[pLowIdx] < p[pLowIdx - 1] && r[rLowIdx] > r[rLowIdx - 1]) {
      return 'bullish';
    }
  }

  return null;
}

async function ensureConnected() {
  if (!client || (client && client._ws && client._ws.readyState !== 1)) {
    if (client) try { client.close(); } catch (_) {}
    console.log('[MONITOR] Reconnecting...');
    await connect();
    console.log('[MONITOR] Reconnected');
    lastPrice = 0;
  }
}

async function sendTg(msg) {
  if (!TG_TOKEN || !TG_CHAT) return;
  const now = Date.now();
  if (now - lastTgTime < 120000) return; // 2 min cooldown entre cualquier alerta
  lastTgTime = now;
  try {
    const https = await import('https');
    const body = JSON.stringify({ chat_id: TG_CHAT, text: msg });
    const req = https.request({
      hostname: 'api.telegram.org',
      path: '/bot' + TG_TOKEN + '/sendMessage',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    req.write(body);
    req.end();
    console.log('  >> [TG] Alerta enviada');
  } catch (_) { console.log('  >> [TG] Error send'); }
}

// Send full analysis (bypasses signal cooldown, uses its own timer)
async function sendTgAnalysis(msg) {
  if (!TG_TOKEN || !TG_CHAT) return;
  try {
    const https = await import('https');
    const body = JSON.stringify({ chat_id: TG_CHAT, text: msg, parse_mode: 'Markdown' });
    const req = https.request({
      hostname: 'api.telegram.org',
      path: '/bot' + TG_TOKEN + '/sendMessage',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    req.write(body);
    req.end();
    console.log('  >> [TG] Análisis completo enviado');
  } catch (e) { console.log('  >> [TG] Error:', e.message); }
}

// Get 60 bars for analysis
async function getBars() {
  const { result } = await client.Runtime.evaluate({
    expression: '(function(){var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();var items=ms.bars()._items,l=items.length,o=[];for(var i=Math.max(0,l-60);i<l;i++){if(items[i])o.push({o:items[i].value[1],h:items[i].value[2],l:items[i].value[3],c:items[i].value[4],v:items[i].value[5]})}return o})()',
    returnByValue: true, awaitPromise: false
  });
  if (result.exceptionDetails) return null;
  return result.value;
}

function calcEma(p, per) {
  if (p.length < per) return null;
  const k = 2/(per+1); let e = p.slice(0,per).reduce((a,b)=>a+b,0)/per;
  for (let i=per; i<p.length; i++) e = p[i]*k + e*(1-k);
  return e;
}

async function sendFullAnalysis(sym, currentPrice) {
  const bars = await getBars();
  if (!bars || !Array.isArray(bars) || bars.length < 10) return;
  const c = bars.map(b=>b.c), h = bars.map(b=>b.h), lo = bars.map(b=>b.l), v = bars.map(b=>b.v);
  const e9 = calcEma(c,9), e21 = calcEma(c,21), e50 = c.length>=50 ? calcEma(c,50) : null;
  const r14 = calcRsi(c.map(x=>({c:x})), 14);
  const r6 = calcRsi(c.map(x=>({c:x})), 6);
  const avgV = v.reduce((a,b)=>a+b,0)/v.length;
  const lastV = v[v.length-1];
  const vr = (lastV/avgV).toFixed(1);
  const sHigh = Math.max(...h), sLow = Math.min(...lo);

  // Swings
  const last20 = bars.slice(-20);
  let sh = [], sl = [];
  for (let i = 1; i < last20.length-1; i++) {
    if (last20[i].h > last20[i-1].h && last20[i].h > last20[i+1].h) sh.push(last20[i].h);
    if (last20[i].l < last20[i-1].l && last20[i].l < last20[i+1].l) sl.push(last20[i].l);
  }
  const microRes = sh.length > 0 ? Math.max(...sh) : sHigh;
  const microSup = sl.length > 0 ? Math.min(...sl) : sLow;

  // Volume profile
  const step = 0.05;
  const vc = {};
  bars.forEach(b => { const k = Math.round(b.c/step)*step; vc[k] = (vc[k]||0)+b.v; });
  const sorted = Object.entries(vc).sort((a,b)=>b[1]-a[1]);
  const poc = sorted.length > 0 ? parseFloat(sorted[0][0]) : currentPrice;

  const vsEma9 = currentPrice > e9 ? '✅ above' : '🔻 below';
  const rsiZone = r14 >= 70 ? 'OB' : r14 <= 30 ? 'OS' : r14 > 55 ? 'high' : r14 < 45 ? 'low' : 'mid';
  const crossState = e9 > e21 ? 'EMA9>EMA21 bullish' : 'EMA9<EMA21 bearish';
  let bias = 'NEUTRAL';
  if (currentPrice > e9 && e9 > e21) bias = 'BULLISH';
  else if (currentPrice < e9 && e9 < e21) bias = 'BEARISH';
  else if (currentPrice < e9 && e9 > e21) bias = 'PULLBACK';
  else if (currentPrice > e9 && e9 < e21) bias = 'RECOVERY';

  const txt = `📊 *${sym} — LIVE*\n\n` +
    `💵 $${currentPrice.toFixed(2)} | H:$${Math.max(...h).toFixed(2)} L:${Math.min(...lo).toFixed(2)}\n\n` +
    `📈 Indicadores:\n` +
    `• EMA9: $${e9?.toFixed(2)||'?'} | EMA21: $${e21?.toFixed(2)||'?'} | EMA50: $${e50?.toFixed(2)||'?'}\n` +
    `• ${crossState} | Precio: ${vsEma9} EMA9\n` +
    `• RSI14: ${r14?.toFixed(1)||'?'} (${rsiZone}) | RSI6: ${r6?.toFixed(1)||'?'}\n` +
    `• Vol: ${vr}x | POC: $${poc.toFixed(2)}\n\n` +
    `📊 Estructura:\n` +
    `• Rango: $${sLow.toFixed(2)} → $${sHigh.toFixed(2)} ($${(sHigh-sLow).toFixed(2)})\n` +
    `• MicroRes: $${microRes.toFixed(2)} | MicroSup: $${microSup.toFixed(2)}\n\n` +
    `🎯 Bias: *${bias}*`;

  await sendTgAnalysis(txt);
}

async function main() {
  console.log('[MONITOR] Connecting...');
  try { await connect(); } catch (e) { console.error('CDP fail:', e.message); process.exit(1); }
  console.log('[MONITOR] Connected. Interval: ' + INTERVAL + 's\n');

  let volSum = 0, volCount = 0;
  let sym = '';

  while (true) {
    try {
      await ensureConnected();
      const q = await getQuote();
      const mps = await getMps();
      sym = q.sym;

      // Store price history for RSI calc
      priceHist.push(q.close);
      if (priceHist.length > 30) priceHist.shift();

      // Calc RSI every bar
      const rsi = calcRsi(priceHist.map(c => ({ c })), 14);
      if (rsi !== null) {
        rsiHist.push(rsi);
        if (rsiHist.length > 30) rsiHist.shift();
      }

      if (lastPrice && lastPrice !== q.close) {
        const change = q.close - lastPrice;
        const volAvg = volCount > 0 ? volSum / volCount : 0;
        const dir = change >= 0 ? '\uD83D\uDFE2' : '\uD83D\uDD34';
        const chgStr = (change >= 0 ? '+' : '') + change.toFixed(2);
        const pct = change / (q.close - change) * 100;
        const pctStr = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';

        // Print to console
        let line = '[' + new Date().toLocaleTimeString() + '] ' + sym + ' ' + dir + ' $' + q.close.toFixed(2) + ' | ' + chgStr + ' (' + pctStr + ') | Vol ' + (q.vol / 1000).toFixed(1) + 'k';
        if (rsi !== null) line += ' | RSI: ' + rsi.toFixed(1);
        if (volAvg > 0 && q.vol > volAvg * 2.5) line += ' \u26A1VOL';
        console.log(line);

        // --- SIGNAL DETECTION ---

        // 1. EMA Cross from MPS
        if (mps && mps['EMA Cross'] && mps['EMA Cross'] !== '\u2014') {
          const cross = mps['EMA Cross'];
          if (cross !== sentEmaCross && sentEmaCross !== null) {
            const msg = '\u26A0\uFE0F ' + sym + ' EMA CROSS: ' + cross + ' en $' + q.close.toFixed(2);
            console.log('  >> ' + msg);
            sendTg(msg);
          }
          sentEmaCross = cross;
        }

        // 2. RSI Overbought (>70)
        if (rsi !== null && rsi > 70 && !sentRsOverbought) {
          const msg = '\uD83D\uDD34 ' + sym + ' RSI SOBRECOMPRADO: ' + rsi.toFixed(1) + ' en $' + q.close.toFixed(2);
          console.log('  >> ' + msg);
          sendTg(msg);
          sentRsOverbought = true;
          sentRsOversold = false; // reset opposite
        }

        // 3. RSI Oversold (<30)
        if (rsi !== null && rsi < 30 && !sentRsOversold) {
          const msg = '\uD83D\uDFE2 ' + sym + ' RSI SOBREVENDIDO: ' + rsi.toFixed(1) + ' en $' + q.close.toFixed(2);
          console.log('  >> ' + msg);
          sendTg(msg);
          sentRsOversold = true;
          sentRsOverbought = false;
        }

        // Reset RSI alerts when back to neutral
        if (rsi !== null && rsi > 30 && rsi < 70) {
          sentRsOverbought = false;
          sentRsOversold = false;
        }

        // 4. Volume Spike (from MPS or direct)
        if (volAvg > 0 && q.vol > volAvg * 3) {
          const mult = (q.vol / volAvg).toFixed(1);
          const msg = '\u26A1 ' + sym + ' VOL SPIKE: ' + mult + 'x promedio en $' + q.close.toFixed(2) + ' (' + (q.vol / 1000).toFixed(0) + 'k)';
          console.log('  >> ' + msg);
          sendTg(msg);
        }

        // 5. Divergence detection
        if (rsiHist.length >= 10 && priceHist.length >= 10) {
          const divergence = detectDivergence(priceHist, rsiHist);
          if (divergence === 'bearish' && !sentBearDiv) {
            const msg = '\u26D4 ' + sym + ' DIVERGENCIA BAJISTA: precio alto > RSI bajo en $' + q.close.toFixed(2) + ' (RSI: ' + rsi.toFixed(1) + ')';
            console.log('  >> ' + msg);
            sendTg(msg);
            sentBearDiv = true;
            sentBullDiv = false;
          }
          if (divergence === 'bullish' && !sentBullDiv) {
            const msg = '\uD83D\uDFE2 ' + sym + ' DIVERGENCIA ALCISTA: precio bajo < RSI alto en $' + q.close.toFixed(2) + ' (RSI: ' + rsi.toFixed(1) + ')';
            console.log('  >> ' + msg);
            sendTg(msg);
            sentBullDiv = true;
            sentBearDiv = false;
          }
        }

        // 6. Level alerts (resistance/support breakout)
        if (!triggeredRes && q.close >= LEVELS.resistance) {
          triggeredRes = true;
          const msg = '\uD83D\uDE80 ' + sym + ' ROMPI\u00D3 RESISTENCIA $' + LEVELS.resistance + ' en $' + q.close.toFixed(2) + ' — nuevos m\u00E1ximos';
          console.log('  >> ' + msg);
          sendTg(msg);
        }
        if (!triggeredSup && q.close <= LEVELS.support) {
          triggeredSup = true;
          const msg = '\uD83D\uDCA5 ' + sym + ' PERDI\u00D3 SOPORTE $' + LEVELS.support + ' en $' + q.close.toFixed(2) + ' — presi\u00F3n bajista';
          console.log('  >> ' + msg);
          sendTg(msg);
        }
        if (!triggeredMajor && q.close <= LEVELS.majorSupport) {
          triggeredMajor = true;
          const msg = '\uD83D\uDED1 ' + sym + ' PERDI\u00D3 SOPORTE MAYOR $' + LEVELS.majorSupport + ' — tendencia bajista confirmada';
          console.log('  >> ' + msg);
          sendTg(msg);
        }

        // === 7. Rebound / Breakdown confirmation ===
        const nowMs = Date.now();
        if (!testingSupport && q.close <= LEVELS.support * 1.005) {
          // Price entered support zone (within 0.5% above support)
          testingSupport = true;
          supportTestPrice = q.close;
          console.log('  >> Probando soporte $' + LEVELS.support.toFixed(2) + ' en $' + q.close.toFixed(2));
        }
        if (testingSupport) {
          // Track the lowest price during test
          if (q.close < supportTestPrice) supportTestPrice = q.close;
          // Check for rebound: price bounced 0.4% above the test low
          if (q.close >= supportTestPrice * 1.004 && q.close > supportTestPrice && nowMs - lastBounceAlert > 120000) {
            testingSupport = false;
            lastBounceAlert = nowMs;
            const msg = '\uD83D\uDFE2 REBOTE CONFIRMADO en $' + q.close.toFixed(2) + ' (probó $' + supportTestPrice.toFixed(2) + ') — soporte $' + LEVELS.support.toFixed(2) + ' sostiene';
            console.log('  >> ' + msg);
            sendTg(msg);
          }
          // Check for breakdown: price broke 0.2% below support
          if (q.close < LEVELS.support * 0.998 && nowMs - lastBreakdownAlert > 120000) {
            testingSupport = false;
            lastBreakdownAlert = nowMs;
            const msg = '\uD83D\uDCA5 BREAKDOWN CONFIRMADO en $' + q.close.toFixed(2) + ' — soporte $' + LEVELS.support.toFixed(2) + ' PERDIDO';
            console.log('  >> ' + msg);
            sendTg(msg);
          }
        }

        // === 8. Reversal / Bearish Continuation ===
        const nowMs2 = Date.now();
        if (trendState === 'BEARISH') {
          if (q.close < lowestDuringTrend) lowestDuringTrend = q.close;
          if (q.close > LEVELS.support * 1.02 && nowMs2 - sentReversalAlert > 180000) {
            trendState = 'NEUTRAL';
            sentReversalAlert = nowMs2;
            const msg = '\uD83D\uDFE2 POSIBLE REVERSIÓN ALCISTA en $' + q.close.toFixed(2) + ' — rompió resistencia de soporte';
            console.log('  >> ' + msg);
            sendTg(msg);
          }
        } else {
          if (q.close < LEVELS.support && q.close < lowestDuringTrend - 0.01) {
            trendState = 'BEARISH';
            lowestDuringTrend = q.close;
            sentContinuationAlert = nowMs2;
            const msg = '\uD83D\uDD34 CONTINUACIÓN BAJISTA en $' + q.close.toFixed(2) + ' — nuevo mínimo debajo de soporte';
            console.log('  >> ' + msg);
            sendTg(msg);
          }
        }
        if (q.close < lowestDuringTrend) lowestDuringTrend = q.close;

      } else if (!lastPrice) {
        console.log('[' + new Date().toLocaleTimeString() + '] ' + sym + ' $' + q.close.toFixed(2) + ' | watching...');
      }

      // === Periodic full analysis to Telegram ===
      const nowMs = Date.now();
      if (nowMs - lastAnalysisTime >= ANALYSIS_INTERVAL_MS) {
        lastAnalysisTime = nowMs;
        console.log('  >> Enviando análisis completo a Telegram...');
        sendFullAnalysis(sym, q.close);
      }

      lastPrice = q.close;
      volSum += q.vol;
      volCount++;

    } catch (e) {
      console.log('[MONITOR] ' + e.message);
    }
    await new Promise(r => setTimeout(r, INTERVAL * 1000));
  }
}

main().catch(e => { console.error('Fatal:', e.message); process.exit(1); });
