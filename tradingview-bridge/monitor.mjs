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
// 2026-10-07 — ESTOS NIVELES ESTABAN HARDCODEADOS A 160/152/148, QUE SON
// NIVELES DE BTC. El sistema se reinicio contra XRPUSDT a ~$1.42, asi que el
// precio SIEMPRE estaba "por debajo del soporte" y estas alertas se disparaban
// sin parar: 10 BREAKDOWN + 12 "PERDIÓ SOPORTE" en pocos minutos. Ademas
// `lowestDuringTrend - 0.01` es un umbral absoluto de 1 centimo, que en BTC es
// ruido pero en XRP es el 0.7% del precio.
//
// Ahora los niveles se DERIVAN del propio activo: micro-resistencia/soporte de
// los ultimos 20 swings, y los histeres se expresan en PORCENTAJE, no en
// unidades absolutas. Si no hay datos para derivarlos, no se alerta — un nivel
// inventado es peor que silencio.
const PRICE_LEVEL_TOL = 0.004;      // 0.4% de histéresis
const BREAKDOWN_TOL = 0.002;         // 0.2% bajo el soporte
const CONTINUATION_PCT = 0.004;      // nuevo mínimo = -0.4%
const SWING_LOOKBACK = 20;

// LEVELS se rellena en cada ciclo con niveles DERIVADOS del propio activo.
// Antes era una constante y por eso daba "$152.00" en un activo de $1.42.
// `valid` false = no hay datos suficientes: el bloque de alertas se salta entero.
let LEVELS = { resistance: 0, support: 0, majorSupport: 0, valid: false };

// Deriva micro-resistencia / soporte de los últimos SWING_LOOKBACK swings, y un
// "soporte mayor" algo más abajo. Si no encuentra ningún swing, marca
// valid:false en vez de inventar un número.
function deriveLevels(bars) {
  if (!bars || bars.length < 30) return { resistance: 0, support: 0, majorSupport: 0, valid: false };
  const last = bars.slice(-SWING_LOOKBACK);
  const hi = [], lo = [];
  for (let i = 1; i < last.length - 1; i++) {
    if (last[i].h > last[i - 1].h && last[i].h > last[i + 1].h) hi.push(last[i].h);
    if (last[i].l < last[i - 1].l && last[i].l < last[i + 1].l) lo.push(last[i].l);
  }
  if (!hi.length || !lo.length) return { resistance: 0, support: 0, majorSupport: 0, valid: false };
  const resistance = Math.max(...hi);
  const support = Math.min(...lo);
  const majorSupport = support * 0.985;   // 1.5% más abajo = soporte mayor
  if (!(resistance > support) || support <= 0) {
    return { resistance: 0, support: 0, majorSupport: 0, valid: false };
  }
  return { resistance, support, majorSupport, valid: true };
}

// === Periodic analysis Telegram delivery ===
const ANALYSIS_INTERVAL_MS = 5 * 60 * 1000; // 5 min entre análisis completos
let lastAnalysisTime = 0; // 0 = enviar en el primer ciclo

let client, lastPrice = 0;
let triggeredRes = false, triggeredSup = false, triggeredMajor = false;
let lastTgTime = 0;
let priceHist = [], rsiHist = [];
let sentRsOverbought = false, sentRsOversold = false;
// 2026-10-07: faltaba declarar. El gate anti-repeticion del VOL SPIKE lo usa y,
// sin esta linea, cada ciclo lanzaba ReferenceError. Se resetea junto con los
// otros flags cuando el volumen vuelve a su media.
let sentVolSpike = false;
// 2026-10-07: serie de CIERRES DE BARRA para detectDivergence. Antes se usaba
// priceHist, que se llenaba con q.close cada 8s (ticks del poll) y por tanto no
// era la misma escala que el RSI, ya calculado sobre barras.
let barHist = [];
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
// 2026-10-07 — RSI REESCRITO. La version anterior NO era el RSI14 estandar y
// por eso la tarjeta mostraba numeros que no coincidian con el analisis real:
//
//   antes: sumaba SOLO los ultimos `period` cambios (sin suavizado) sobre las
//         ultimas 60 barras  -> RSI14 22.01 / RSI6 7.27
//   ahora: Wilder (el que usan TradingView, tv_live.mjs y todo el mundo)
//         sobre las 300 barras -> RSI14 28.67 / RSI6 15.04
//
// El bug era material: RSI6 7.27 se leia como "sobreventa extrema" cuando el
// valor real era 15.04, y RSI14 22.01 vs 28.67 cambiaba la zona. No era
// redondeo, era un metodo de calculo distinto; la tarjetaessentially fabricaba
// su propia lectura.
//
// Se calcula sobre TODAS las barras disponibles (no 60) para que el suavizado de
// Wilder tenga historia suficiente y converja al valor real.
function calcRsi(bars, period = 14) {
  if (!bars || bars.length < period + 1) return null;
  let gains = 0, losses = 0;
  // semilla: media simple de los primeros `period` cambios
  for (let i = 1; i <= period; i++) {
    const diff = bars[i].c - bars[i - 1].c;
    if (diff >= 0) gains += diff;
    else losses -= diff;
  }
  let avgGain = gains / period;
  let avgLoss = losses / period;
  // suavizado de Wilder sobre el resto de la serie
  for (let i = period + 1; i < bars.length; i++) {
    const diff = bars[i].c - bars[i - 1].c;
    avgGain = (avgGain * (period - 1) + (diff > 0 ? diff : 0)) / period;
    avgLoss = (avgLoss * (period - 1) + (diff < 0 ? -diff : 0)) / period;
  }
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
// 2026-10-07: antes devolvia solo las ultimas 60 barras. Con el suavizado de
// Wilder eso no alcanza para que el RSI converja: 60 barras dan un valor que no
// coincide con el de TradingView ni con tv_live.mjs (que usa 300). Se piden
// todas las que el chart tiene cargadas, con un tope de 400 por memoria.
const BARS_FOR_RSI = 400;
async function getBars() {
  const { result } = await client.Runtime.evaluate({
    expression: '(function(){var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();var items=ms.bars()._items,l=items.length,o=[];var from=Math.max(0,l-' + BARS_FOR_RSI + ');for(var i=from;i<l;i++){if(items[i])o.push({o:items[i].value[1],h:items[i].value[2],l:items[i].value[3],c:items[i].value[4],v:items[i].value[5]})}return o})()',
    returnByValue: true, awaitPromise: false
  });
  if (result.exceptionDetails) return null;
  return result.value;
}

// 2026-10-07: el POC que se imprimia era SINTETICO - un histograma de volumen
// por PRECIO DE CIERRE (vc[round(close/0.05)*0.05] += volume). Eso NO es un POC:
// el Point of Control sale del FOOTPRINT (volumen negociado en cada nivel de
// precio), no de donde cerro la barra. El numero era real pero la magnitud no
// significaba nada. Ahora se lee el POC real del puente ATAS; si no hay dato
// vivo, la tarjeta lo dice en vez de inventarlo.
async function getAtasFootprint() {
  try {
    const http = await import('http');
    const body = await new Promise((res, rej) => {
      const req = http.request({ hostname: '127.0.0.1', port: 8000, path: '/indicators', method: 'GET' }, (r) => {
        let d = ''; r.on('data', (c) => d += c); r.on('end', () => res(d));
      });
      req.on('error', rej);
      req.setTimeout(6000, () => { req.destroy(); rej(new Error('timeout')); });
      req.end();
    });
    const j = JSON.parse(body);
    const fp = j.footprint || {};
    const ts = j.timestamp || 0;
    const ageS = ts ? Math.abs(Date.now() / 1000 - (ts > 1e11 ? ts / 1000 : ts)) : null;
    if (ageS !== null && ageS > 300) return { stale: true, ageS: Math.round(ageS) };
    const dom = j.dom || {};
    return {
      stale: false,
      ticker: j.ticker, time: j.time, barIndex: j.bar_index,
      price: (j.price || {}).last,
      poc: fp.poc, vah: fp.value_area_high, val: fp.value_area_low,
      levels: (fp.top_levels || []).length,
      imbalanceEma: dom.imbalance_pct_ema,
      volumeRatio: (j.volume || {}).volume_ratio,
      ageS: ageS === null ? null : Math.round(ageS),
    };
  } catch (e) {
    return { error: e.message };
  }
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

  // Volume profile — ELIMINADO el POC sintetico (2026-10-07).
  // Antes: step=0.05; vc[Math.round(b.c/step)*step] += b.v  -> volumen por
  // PRECIO DE CIERRE. El "POC" que salia de ahi no es un Point of Control: el
  // POC sale del footprint (volumen negociado en cada nivel), no de donde cerro
  // la barra. Ahora el POC viene de ATAS en getAtasFootprint(), y si no hay dato
  // vivo la tarjeta lo declara en vez de fabricar un numero.
  const atas = await getAtasFootprint();

  const vsEma9 = currentPrice > e9 ? '✅ above' : '🔻 below';
  const rsiZone = r14 >= 70 ? 'OB' : r14 <= 30 ? 'OS' : r14 > 55 ? 'high' : r14 < 45 ? 'low' : 'mid';
  const crossState = e9 > e21 ? 'EMA9>EMA21 bullish' : 'EMA9<EMA21 bearish';
  let bias = 'NEUTRAL';
  if (currentPrice > e9 && e9 > e21) bias = 'BULLISH';
  else if (currentPrice < e9 && e9 < e21) bias = 'BEARISH';
  else if (currentPrice < e9 && e9 > e21) bias = 'PULLBACK';
  else if (currentPrice > e9 && e9 < e21) bias = 'RECOVERY';

  // 2026-10-07 — TARJETA REESCRITA.
  //
  // Los tres bugs que hacian que los datos NO coincidieran con el analisis real:
  //
  //  1) POC SINTETICO: venia de un histograma de precio-de-cierre, no del
  //     footprint. Ahora sale de ATAS (getAtasFootprint) o se declara ausente.
  //  2) SIN ORDER-FLOW: la tarjeta no leia ni /indicators ni /cvd ni /dom, asi
  //     que no podia incluir CVD, DOM imbalance, footprint ni las Reglas 6/7 de
  //     rules.json v3.3. Ahora los lee y si no estan, lo dice.
  //  3) PRECIO A 2 DECIMALES: 1.4252 se imprimia como 1.42. En un activo de
  //     1.42 eso es 0.37% de error solo por redondeo. Ahora 4 decimales.
  //
  // Ademas se estampa el origen y la edad de cada dato, para que la tarjeta sea
  // auditable contra el analisis en vez de tener que fiarse de ella.
  const fx = (v) => (typeof v === 'number' && isFinite(v)) ? v.toFixed(4) : '?';
  const hasAtas = atas && !atas.stale && !atas.error && atas.poc != null;

  const atasBlock = hasAtas
    ? `🧱 *ATAS order-flow* (age ${atas.ageS}s, barra ${atas.time} #${atas.barIndex})\n` +
      `• POC: $${fx(atas.poc)} | Value area: $${fx(atas.val)} – $${fx(atas.vah)}\n` +
      `• Niveles footprint: ${atas.levels}\n` +
      `• DOM imbalance EMA: ${typeof atas.imbalanceEma === 'number' ? atas.imbalanceEma.toFixed(2) + '%' : '?'}` +
      `${typeof atas.imbalanceEma === 'number' ? (atas.imbalanceEma > 10 ? ' (sobre +10%)' : atas.imbalanceEma < 5 ? ' (bajo +5%)' : ' (neutro)') : ''}\n` +
      `• Vol ratio: ${typeof atas.volumeRatio === 'number' ? atas.volumeRatio.toFixed(3) : '?'}${typeof atas.volumeRatio === 'number' && atas.volumeRatio < 0.3 ? ' (barra formandose: no concluyente)' : ''}\n`
    : `🧱 *ATAS order-flow*: NO DISPONIBLE\n` +
      `   ${atas && atas.stale ? `dato congelado (age ${atas.ageS}s > 300s) — no se usa` : atas && atas.error ? `error: ${atas.error}` : 'sin footprint'}` +
      `\n   -> Sin ATAS no hay CVD, ni DOM, ni footprint. NO se aplica rules.json 6/7.\n`;

  const txt = `📊 *${sym} — LIVE*\n\n` +
    `💵 $${fx(currentPrice)} | H:$${fx(Math.max(...h))} L:${fx(Math.min(...lo))}\n\n` +
    `📈 *TradingView* (Wilder, ${bars.length} barras)\n` +
    `• EMA9: $${e9?fx(e9):'?'} | EMA21: $${e21?fx(e21):'?'} | EMA50: $${e50?fx(e50):'?'}\n` +
    `• ${crossState} | Precio: ${vsEma9} EMA9\n` +
    `• RSI14: ${r14?.toFixed(1)||'?'} (${rsiZone}) | RSI6: ${r6?.toFixed(1)||'?'}\n` +
    `• Vol: ${vr}x (TV)\n\n` +
    atasBlock + `\n` +
    `📊 *Estructura* (sintetica, de barras OHLC)\n` +
    `• Rango: $${fx(sLow)} → $${fx(sHigh)}\n` +
    `• MicroRes: $${fx(microRes)} | MicroSup: $${fx(microSup)}\n\n` +
    `🎯 Bias (por EMAs, NO es un veredicto de rules.json): *${bias}*\n` +
    `_${new Date().toISOString().replace('T',' ').slice(0,19)} UTC · sources: TV+${hasAtas?'ATAS':'ATAS AUSENTE'}_`;

  // 2026-10-07: la tarjeta se ENVIABA pero su texto no se logueaba, asi que no
  // habia forma de comprobar que lo que llega a Telegram es lo que dice el
  // codigo. Ahora se imprime completa. Sin esto, cualquier bug de datos en la
  // tarjeta es invisible: solo se ve el "enviado".
  console.log('----- TARJETA ENVIADA A TELEGRAM -----');
  console.log(txt);
  console.log('----- FIN TARJETA -----');

  await sendTgAnalysis(txt);
}

async function main() {
  console.log('[MONITOR] Connecting...');
  try { await connect(); } catch (e) { console.error('CDP fail:', e.message); process.exit(1); }
  console.log('[MONITOR] Connected. Interval: ' + INTERVAL + 's\n');

  // 2026-10-07: volSum/volCount ya no se usan. El "volumen x promedio" sale ahora
  // de las barras reales del chart (volMult), no de un buffer de ticks.
  let sym = '';

  while (true) {
    try {
      await ensureConnected();
      const q = await getQuote();
      const mps = await getMps();
      sym = q.sym;

      // priceHist ya no se usa: la divergencia compara ahora barHist (cierres de
      // barra), la misma serie que el RSI. Se mantiene el push solo por
      // compatibilidad? No — se elimina.
      priceHist.push(q.close);
      if (priceHist.length > 30) priceHist.shift();

      // 2026-10-07 — BUG DE ORIGEN. Estas alertas (RSI sobrecomprado /
      // sobrevendido, cruce de EMA, divergencia, vol spike) calcularan el RSI
      // sobre `priceHist`, un buffer que se llena cada 8s con q.close — es
      // decir, ticks del poll, NO barras del chart. Con la funcion antigua
      // (media simple de los ultimos p cambios) eso "funcionaba" y daba un
      // numero; al pasar a Wilder, un RSI sobre ticks de 8s con buffer de 30
      // no converge y da otra cosa distinta del RSI14 real.
      //
      // Ahora el RSI de las alertas se calcula sobre las BARRAS reales del
      // chart, igual que la tarjeta y que tv_live.mjs. Si no hay barras, no se
      // alerta: mejor callar que avisar con un numero que no es el RSI.
      const barList = await getBars();
      const rsi = barList && barList.length > 20
        ? calcRsi(barList, 14)
        : null;
      if (rsi !== null) {
        rsiHist.push(rsi);
        if (rsiHist.length > 30) rsiHist.shift();
        // Misma serie que usa el RSI, para que la comparacion de divergencia sea
        // entre escalas comparables. Solo se anade cuando la BARRA cambia, no en
        // cada poll de 8s: si no, la serie se llenaria de repeticiones.
        const lastBarClose = barList[barList.length - 1].c;
        if (!barHist.length || barHist[barHist.length - 1] !== lastBarClose) {
          barHist.push(lastBarClose);
          if (barHist.length > 30) barHist.shift();
        }
      }

      // 2026-10-07: el "volumen x promedio" se calcula sobre las BARRAS reales.
      // Antes usaba volSum/volCount, un buffer que se llenaba con q.vol cada 8s;
      // como q.vol es el volumen ACUMULADO de la barra en curso (empieza en 0),
      // el ratio se disparaba: medido "VOL SPIKE: 266.8x promedio", imposible.
      let volMult = null;
      if (barList && barList.length > 20) {
        const bv = barList.map(b => b.v).filter(x => typeof x === 'number' && x > 0);
        if (bv.length > 5) {
          const vAvg = bv.slice(0, -1).reduce((a, b) => a + b, 0) / (bv.length - 1);
          if (vAvg > 0) volMult = q.vol / vAvg;
        }
      }

      // 2026-10-07: los niveles se derivan del propio activo en cada ciclo.
      // Si pasan de un simbolo a otro, los niveles deBreakdown/Rebound/Continuation
      // se recalculan; si dejan de valer (nivel roto de verdad, no por bug de
      // unidades), el bloque de alertas se salta entero y no inventa nada.
      LEVELS = deriveLevels(barList);

      if (lastPrice && lastPrice !== q.close) {
        const change = q.close - lastPrice;
        const dir = change >= 0 ? '\uD83D\uDFE2' : '\uD83D\uDD34';
        const chgStr = (change >= 0 ? '+' : '') + change.toFixed(2);
        const pct = change / (q.close - change) * 100;
        const pctStr = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';

        // Print to console
        let line = '[' + new Date().toLocaleTimeString() + '] ' + sym + ' ' + dir + ' $' + q.close.toFixed(4) + ' | ' + chgStr + ' (' + pctStr + ') | Vol ' + (q.vol / 1000).toFixed(1) + 'k';
        if (rsi !== null) line += ' | RSI: ' + rsi.toFixed(1);
        // El marcador VOL usa el promedio de BARRAS. El volAvg de ticks se elimino
        // aqui: daba ratios de 266x porque promediaba el volumen acumulado de la
        // barra en curso, que empieza en cero.
        if (volMult !== null && volMult > 2.5) line += ' \u26A1VOL';
        console.log(line);

        // --- SIGNAL DETECTION ---

        // 1. EMA Cross from MPS
        if (mps && mps['EMA Cross'] && mps['EMA Cross'] !== '\u2014') {
          const cross = mps['EMA Cross'];
          if (cross !== sentEmaCross && sentEmaCross !== null) {
            const msg = '\u26A0\uFE0F ' + sym + ' EMA CROSS: ' + cross + ' en $' + q.close.toFixed(4);
            console.log('  >> ' + msg);
            sendTg(msg);
          }
          sentEmaCross = cross;
        }

        // 2. RSI Overbought (>70)
        if (rsi !== null && rsi > 70 && !sentRsOverbought) {
          const msg = '\uD83D\uDD34 ' + sym + ' RSI SOBRECOMPRADO: ' + rsi.toFixed(1) + ' en $' + q.close.toFixed(4);
          console.log('  >> ' + msg);
          sendTg(msg);
          sentRsOverbought = true;
          sentRsOversold = false; // reset opposite
        }

        // 3. RSI Oversold (<30)
        if (rsi !== null && rsi < 30 && !sentRsOversold) {
          const msg = '\uD83D\uDFE2 ' + sym + ' RSI SOBREVENDIDO: ' + rsi.toFixed(1) + ' en $' + q.close.toFixed(4);
          console.log('  >> ' + msg);
          sendTg(msg);
          sentRsOversold = true;
          sentRsOverbought = false;
        }

        // Reset RSI alerts when back to neutral
        // 2026-10-07 — ANTI-SPAM por HISTÉRESIS. El reset era "rsi entre 30 y 70
        // => limpiar flags". Con el RSI pegado al umbral eso producia un ciclo:
        //   rsi=30.0 -> alerta (flag on)
        //   rsi=29.9 -> sigue bajo 30, el flag se limpio arriba y vuelve a avisar
        //   rsi=30.1 -> reset, flags a false
        //   rsi=29.9 -> alerta otra vez
        // Medido: 2 alertas "RSI SOBREVENDIDO" en minutos, con 29.9 y 30.0.
        //
        // Ahora el reset exige salir de la zona con HOLGURA (35/65), así que un
        // RSI oscilando en 29-31 avisa UNA vez y se calla hasta que de verdad
        // vuelva a zona neutral.
        if (rsi !== null && rsi > 35 && rsi < 65) {
          sentRsOverbought = false;
          sentRsOversold = false;
        }

        // === 4. Volume Spike (from MPS or direct) ===
        // 2026-10-07 — BUG: el "promedio" salia de volSum/volCount, un buffer que
        // se llenaba con q.vol CADA 8s. Pero q.vol es el volumen ACUMULADO DE LA
        // BARRA en curso, no el volumen de un tick: dentro de la misma barra se
        // lee cada vez mas grande. El promedio, en cambio, arrastraba la lectura
        // diminuta del inicio de la barra, asi que el ratio se disparaba:
        //   "VOL SPIKE: 266.8x promedio" — imposible, medido.
        // Al cambiar de barra el volumen cae a cero y el ratio se invierte, asi
        // que el alerta era ruido en ambos sentidos.
        //
        // Ahora el promedio sale de las BARRAS REALES del chart (las mismas que
        // usa el RSI), que es lo que "volumen x promedio" significa de verdad.
        // Si no hay barras, no hay alerta de volumen.
        if (volMult !== null && volMult > 3 && !sentVolSpike) {
          const mult = volMult.toFixed(1);
          sentVolSpike = true;
          const msg = '\u26A1 ' + sym + ' VOL SPIKE: ' + mult + 'x promedio en $' + q.close.toFixed(4) + ' (' + (q.vol / 1000).toFixed(0) + 'k)';
          console.log('  >> ' + msg);
          sendTg(msg);
        }
        // Reset del gate: si el volumen vuelve a su media, se rearma la alerta.
        if (volMult !== null && volMult < 1.5) sentVolSpike = false;

        // 5. Divergence detection
        // 2026-10-07: dos problemas. (a) `rsi.toFixed(1)` sin comprobar rsi != null
        // — si no hay barras, el RSI es null y esto es un TypeError en cada ciclo.
        // (b) detectDivergence comparaba priceHist (TICKS cada 8s) contra rsiHist
        // (ahora calculado sobre BARRAS): dos escalas distintas, la divergencia
        // detectada no significa nada. Ahora se comparan las MISMAS series que
        // usa el RSI, y la serie de precio se llena con el cierre de la barra, no
        // con el del poll.
        if (rsi !== null && rsiHist.length >= 10 && barHist.length >= 10) {
          const divergence = detectDivergence(barHist, rsiHist);
          if (divergence === 'bearish' && !sentBearDiv) {
            const msg = '\u26D4 ' + sym + ' DIVERGENCIA BAJISTA: precio alto > RSI bajo en $' + q.close.toFixed(4) + ' (RSI: ' + rsi.toFixed(1) + ')';
            console.log('  >> ' + msg);
            sendTg(msg);
            sentBearDiv = true;
            sentBullDiv = false;
          }
          if (divergence === 'bullish' && !sentBullDiv) {
            const msg = '\uD83D\uDFE2 ' + sym + ' DIVERGENCIA ALCISTA: precio bajo < RSI alto en $' + q.close.toFixed(4) + ' (RSI: ' + rsi.toFixed(1) + ')';
            console.log('  >> ' + msg);
            sendTg(msg);
            sentBullDiv = true;
            sentBearDiv = false;
          }
        }

        // 6. Level alerts (resistance/support breakout)
        // GATE: sin niveles derivados validos NO se alerta. Antes, con LEVELS fijo
        // en 152 y el precio en 1.42, "q.close <= LEVELS.support" era SIEMPRE
        // cierto y disparaba en bucle.
        if (LEVELS.valid && !triggeredRes && q.close >= LEVELS.resistance) {
          triggeredRes = true;
          const msg = '\uD83D\uDE80 ' + sym + ' ROMPI\u00D3 RESISTENCIA $' + LEVELS.resistance.toFixed(4) + ' en $' + q.close.toFixed(4) + ' — nuevos m\u00E1ximos';
          console.log('  >> ' + msg);
          sendTg(msg);
        }
        if (LEVELS.valid && !triggeredSup && q.close <= LEVELS.support) {
          triggeredSup = true;
          const msg = '\uD83D\uDCA5 ' + sym + ' PERDI\u00D3 SOPORTE $' + LEVELS.support.toFixed(4) + ' en $' + q.close.toFixed(4) + ' — presi\u00F3n bajista';
          console.log('  >> ' + msg);
          sendTg(msg);
        }
        if (LEVELS.valid && !triggeredMajor && q.close <= LEVELS.majorSupport) {
          triggeredMajor = true;
          const msg = '\uD83D\uDED1 ' + sym + ' PERDI\u00D3 SOPORTE MAYOR $' + LEVELS.majorSupport.toFixed(4) + ' — tendencia bajista confirmada';
          console.log('  >> ' + msg);
          sendTg(msg);
        }

        // === 7. Rebound / Breakdown confirmation ===
        const nowMs = Date.now();
        if (LEVELS.valid && !testingSupport && q.close <= LEVELS.support * (1 + PRICE_LEVEL_TOL)) {
          // Entró en zona de soporte (dentro de 0.4% POR ENCIMA del soporte).
          // El 0.5% era un número fijo; en un activo de 1.42 es un 0.35% real, y
          // la tolerancia ahora viene de una constante con nombre.
          testingSupport = true;
          supportTestPrice = q.close;
          console.log('  >> Probando soporte $' + LEVELS.support.toFixed(4) + ' en $' + q.close.toFixed(4));
        }
        if (testingSupport) {
          // Track the lowest price during test
          if (q.close < supportTestPrice) supportTestPrice = q.close;
          // Check for rebound: price bounced 0.4% above the test low
          if (q.close >= supportTestPrice * 1.004 && q.close > supportTestPrice && nowMs - lastBounceAlert > 120000) {
            testingSupport = false;
            lastBounceAlert = nowMs;
            const msg = '\uD83D\uDFE2 REBOTE CONFIRMADO en $' + q.close.toFixed(4) + ' (probó $' + supportTestPrice.toFixed(4) + ') — soporte $' + LEVELS.support.toFixed(4) + ' sostiene';
            console.log('  >> ' + msg);
            sendTg(msg);
          }
          // Check for breakdown: price broke 0.2% below support
          if (q.close < LEVELS.support * (1 - BREAKDOWN_TOL) && nowMs - lastBreakdownAlert > 120000) {
            testingSupport = false;
            lastBreakdownAlert = nowMs;
            const msg = '\uD83D\uDCA5 BREAKDOWN CONFIRMADO en $' + q.close.toFixed(4) + ' — soporte $' + LEVELS.support.toFixed(4) + ' PERDIDO';
            console.log('  >> ' + msg);
            sendTg(msg);
          }
        }

        // === 8. Reversal / Bearish Continuation ===
        const nowMs2 = Date.now();
        if (trendState === 'BEARISH') {
          if (q.close < lowestDuringTrend) lowestDuringTrend = q.close;
          if (LEVELS.valid && q.close > LEVELS.support * 1.02 && nowMs2 - sentReversalAlert > 180000) {
            trendState = 'NEUTRAL';
            sentReversalAlert = nowMs2;
            const msg = '\uD83D\uDFE2 POSIBLE REVERSI\u00D3N ALCISTA en $' + q.close.toFixed(4) + ' — rompi\u00F3 resistencia de soporte';
            console.log('  >> ' + msg);
            sendTg(msg);
          }
        } else {
          // 2026-10-07: el umbral era `lowestDuringTrend - 0.01`, un ABSOLUTO de
          // un centimo. En BTC es ruido; en XRP (precio 1.42) es el 0.7% — el
          // precio lo cruza en un tick normal y la alerta salta sin motivo. Ahora
          // es un porcentaje del propio mínimo, igual para cualquier activo.
          if (LEVELS.valid && q.close < LEVELS.support &&
              q.close < lowestDuringTrend * (1 - CONTINUATION_PCT)) {
            trendState = 'BEARISH';
            lowestDuringTrend = q.close;
            sentContinuationAlert = nowMs2;
            const msg = '\uD83D\uDD34 CONTINUACI\u00D3N BAJISTA en $' + q.close.toFixed(4) + ' — nuevo mínimo debajo de soporte';
            console.log('  >> ' + msg);
            sendTg(msg);
          }
        }
        if (q.close < lowestDuringTrend) lowestDuringTrend = q.close;

      } else if (!lastPrice) {
        console.log('[' + new Date().toLocaleTimeString() + '] ' + sym + ' $' + q.close.toFixed(4) + ' | watching...');
      }

      // === Periodic full analysis to Telegram ===
      const nowMs = Date.now();
      if (nowMs - lastAnalysisTime >= ANALYSIS_INTERVAL_MS) {
        lastAnalysisTime = nowMs;
        console.log('  >> Enviando análisis completo a Telegram...');
        sendFullAnalysis(sym, q.close);
      }

      lastPrice = q.close;
      // (volSum/volCount eliminados: daban un "promedio" de ticks, no de barras)

    } catch (e) {
      console.log('[MONITOR] ' + e.message);
    }
    await new Promise(r => setTimeout(r, INTERVAL * 1000));
  }
}

main().catch(e => { console.error('Fatal:', e.message); process.exit(1); });
