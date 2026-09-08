#!/usr/bin/env node
/**
 * Unified Telegram Bot — TWO-WAY + PERIODIC ANALYSIS + SELF-HEALING
 *
 * Fixes (vs old telegram_bot.mjs / monitor.mjs / telegram-bot.js):
 * 1. LiveExporter (HTTP) is the PRIMARY data source — never dies.
 * 2. TradingView CDP reads are OPTIONAL enrichment wrapped in try/catch —
 *    a closed WebSocket can no longer crash the bot ("WebSocket is not open").
 * 3. Periodic full analysis every 15 min + heartbeat so the bot is verifiable alive.
 * 4. Telegraf polling with 409-conflict retry (only ONE poller for the token).
 * 5. Structured timestamped logging.
 *
 * Commands:
 *   /start, /help       — help
 *   /a  /analisis       — full quant analysis (LiveExporter order flow + quant engine)
 *   /long               — long trade plan
 *   /short              — short trade plan
 *   /p  /price          — price snapshot
 *   /status             — bot health (data sources up/down)
 *
 * Usage:
 *   node telegram-bot.mjs              (foreground)
 *   node telegram-bot.mjs --once       (send one analysis, then exit — for testing)
 */

import 'dotenv/config';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
process.env.TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN || (await readEnv()).token;
process.env.TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID || (await readEnv()).chatId;

const TG_TOKEN = process.env.TELEGRAM_TOKEN;
const TG_CHAT = process.env.TELEGRAM_CHAT_ID;
const LX_API = 'http://127.0.0.1:8000';
const ANALYSIS_INTERVAL_MS = 15 * 60 * 1000; // 15 min periodic (anti-spam)
const HEARTBEAT_INTERVAL_MS = 15 * 60 * 1000; // 15 min heartbeat

if (!TG_TOKEN) {
  console.error('[BOT] TELEGRAM_TOKEN missing (set .env)');
  process.exit(1);
}

async function readEnv() {
  try {
    const dotenv = await import('dotenv');
    const fs = await import('fs');
    const envPath = resolve(__dirname, '.env');
    if (fs.existsSync(envPath)) {
      const parsed = dotenv.config({ path: envPath }).parsed || {};
      return { token: parsed.TELEGRAM_TOKEN, chatId: parsed.TELEGRAM_CHAT_ID };
    }
  } catch (_) {}
  return { token: null, chatId: null };
}

// ─── Logging ────────────────────────────────────────────────────
function log(...args) {
  const ts = new Date().toISOString();
  console.log(`[${ts}]`, ...args);
}

// ─── LiveExporter (primary, HTTP, robust) ───────────────────────
async function lxRequest(path, timeoutMs = 8000) {
  const http = (await import('http')).default;
  return new Promise((resolvePromise, reject) => {
    const req = http.get(LX_API + path, { timeout: timeoutMs }, (res) => {
      let data = '';
      res.on('data', (d) => (data += d));
      res.on('end', () => {
        try {
          resolvePromise(JSON.parse(data));
        } catch (e) {
          reject(e);
        }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => {
      req.destroy(new Error('LiveExporter timeout'));
    });
  });
}

// ─── TradingView CDP (optional enrichment, never fatal) ─────────
let client = null;
let cdpOk = false;

async function cdpConnect() {
  try {
    const CDP = (await import('chrome-remote-interface')).default;
    const targets = await CDP.List({ host: '127.0.0.1', port: 9222 });
    const target = targets.find((t) => t.url && (t.url.includes('tradingview.com/chart') || (t.title || '').includes('TradingView'))) || targets[0];
    if (!target) return false;
    if (client) {
      try { client.close(); } catch (_) {}
    }
    client = await CDP({ host: '127.0.0.1', port: 9222, target: target.id });
    await client.Runtime.enable();
    cdpOk = true;
    return true;
  } catch (e) {
    cdpOk = false;
    return false;
  }
}

async function cdpEval(expr) {
  // Always ensure a live socket before evaluating — this is the crash fix.
  if (!client || !client._ws || client._ws.readyState !== 1) {
    const ok = await cdpConnect();
    if (!ok) throw new Error('CDP unavailable');
  }
  const { result } = await client.Runtime.evaluate({
    expression: expr,
    returnByValue: true,
    awaitPromise: false,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'Eval error');
  return result.value;
}

async function cdpSafe(fn, fallback = null) {
  try {
    return await fn();
  } catch (e) {
    return fallback;
  }
}

async function getChartQuote() {
  return cdpSafe(() =>
    cdpEval(
      '(function(){var wv=window.TradingViewApi._activeChartWidgetWV;var ms=wv.value()._chartWidget.model().mainSeries();var last=ms.bars().last();return{sym:ms.symbol(),close:last.value[4],open:last.value[1],high:last.value[2],low:last.value[3],vol:last.value[5]};})()'
    )
  );
}

// ─── Analysis Engine (LiveExporter-first) ───────────────────────
async function runAnalysis() {
  log('[BOT] Running analysis...');
  const ind = await lxRequest('/indicators');
  if (!indicatorsFresh(ind)) {
    log('[BOT] Analysis skipped — /indicators stale or no_data (ATAS/LiveExporter closed)');
    return null;
  }

  const ticker = (ind.ticker || '').toUpperCase() || 'SUIUSDT';
  const shortSym = ticker.endsWith('USDT') ? ticker.slice(0, -4) : ticker;

  const [cvd, atas, quant] = await Promise.all([
    lxRequest('/cvd').catch(() => ({})),
    lxRequest(`/state/${ticker}`).catch(() => ({ alerts: [] })),
    lxRequest(`/quant/${shortSym}`).catch(() => ({ alerts: [] })),
  ]);

  const price = ind.price?.last ?? 'N/A';
  const open = ind.price?.open ?? 'N/A';
  const high = ind.price?.high ?? 'N/A';
  const low = ind.price?.low ?? 'N/A';
  const vol = ind.volume?.total ?? 'N/A';
  const delta = ind.delta ?? 'N/A';
  const domImb = ind.dom?.imbalance_pct ?? 'N/A';
  const bidAsk = ind.dom?.bid_ask_ratio ?? 'N/A';
  const poc = ind.footprint?.poc ?? 'N/A';
  const vah = ind.footprint?.value_area_high ?? 'N/A';
  const val = ind.footprint?.value_area_low ?? 'N/A';

  const cvdVal = cvd?.[ticker]?.cvd ?? 'N/A';
  const prevCvd = cvd?.[ticker]?.prev_cvd ?? 'N/A';
  const lastDelta = cvd?.[ticker]?.last_delta ?? 'N/A';

  const buyVol = ind.tape?.buy_vol ?? 'N/A';
  const sellVol = ind.tape?.sell_vol ?? 'N/A';
  const netVol = ind.tape?.net_vol ?? 'N/A';
  const buyPct = ind.tape?.buy_count_pct ?? 'N/A';
  const sellPct = ind.tape?.sell_count_pct ?? 'N/A';
  const largestTrade = ind.tape?.largest_trade ?? 'N/A';
  const largestSide = ind.tape?.largest_trade_side ?? 'N/A';
  const avgTrade = ind.tape?.avg_trade_size ?? 'N/A';

  const atasAlerts = (atas?.alerts || []).slice(-5)
    .map((a) => `- ${a.raw}`).join('\n') || 'Sin alertas recientes';

  const quantSignals = (quant?.alerts || []).slice(-5)
    .map((a) => `- ${a.signal_name} @ ${a.precio} [${a.timeframe}] ${a.color === 'green' ? '🟢' : a.color === 'red' ? '🔴' : '🟡'}`)
    .join('\n') || 'Sin señales del Quant Engine';

  // Bias scorecard — mirrors signal_engine.py (Rule 4): centered DOM, volume-weighted tape, delta/volume absorption
  let biasScore = 50;
  const biasReasons = [];
  // Use live context for expected move
  let ctx = null;
  try { ctx = await lxRequest(`/context/${ticker}`).catch(()=>null); } catch(_){ ctx=null; }
  let stats = null;
  try { stats = await lxRequest(`/calls/stats`).catch(()=>null); } catch(_){ stats=null; }
  let expMed = ctx?.expected_move?.value ?? 0.44; // percent points (0.44 = 0.44%)
  let expP90 = ctx?.expected_move?.confidence_interval?.[1] ?? expMed * 4.6;
  // Normalize if API returns fraction (<0.05) vs percent points
  if (expMed < 0.05) expMed *= 100;
  if (expP90 < 0.05) expP90 *= 100;
  const volTotal = Number(vol) || 0;
  const deltaNum = Number(delta) || 0;
  const deltaRatio = volTotal ? Math.abs(deltaNum)/volTotal : 0;
  // Footprint depth
  const lvls = ind.footprint?.top_levels || [];
  const buyLvls = lvls.filter(l=> (l.delta||0) > 0).length;
  const sellLvls = lvls.filter(l=> (l.delta||0) < 0).length;
  const fpDepth = lvls.length ? buyLvls/lvls.length : 0.5;
  // Scores 0-10
  const sDom = Math.max(0, Math.min(10, 5 + Number(domImb||0)*0.125)); // 40% -> 0/10, fixes 20% cap
  const totalVol = (Number(buyVol)||0) + (Number(sellVol)||0);
  const volRatio = totalVol ? (Number(buyVol)||0)/totalVol : 0.5;
  const cntRatio = (Number(buyPct)||50)/100;
  const sTape = Math.round(((0.70*volRatio + 0.30*cntRatio)*10)*100)/100; // volume-weighted 70/30
  const sClimax = deltaRatio < 0.05 ? 3.0 : deltaRatio > 0.40 ? 8.5 : 6.0; // absorption
  const sFpDepth = fpDepth*10;
  const sFpConf = lvls.length >= 2 ? 10 : 5;
  // Weighted bias (rules.json weights 25/25/20/15/15) — same as signal_engine.py
  biasScore = Math.round(((sFpDepth*25 + sDom*25 + sTape*20 + sClimax*15 + sFpConf*15)/100)*10);
  biasScore = Math.max(0, Math.min(100, biasScore));
  if (sDom >= 6.5) biasReasons.push(`DOM ${(Number(domImb)).toFixed(1)}% bid (${sDom.toFixed(1)}/10)`);
  else if (sDom <= 3.5) biasReasons.push(`DOM ${(Number(domImb)).toFixed(1)}% ask (${sDom.toFixed(1)}/10)`);
  else biasReasons.push(`DOM neutral ${(Number(domImb)).toFixed(1)}%`);
  biasReasons.push(`Tape ${sTape.toFixed(1)}/10 vol-wtd` + (Number(sellPct)>60?` Sell ${sellPct}%`:` Buy ${buyPct}%`));
  biasReasons.push(`Climax ${sClimax}/10 (Δ/vol ${(deltaRatio*100).toFixed(1)}%)`);
  biasReasons.push(`Footprint ${buyLvls}B/${sellLvls}S ${sFpDepth.toFixed(0)}/10`);
  biasReasons.push(Number(cvdVal) > Number(prevCvd) ? 'CVD subiendo' : 'CVD bajando');
  if (deltaNum > 0) biasReasons.push(`Delta +${deltaNum.toFixed(1)}`); else biasReasons.push(`Delta ${deltaNum.toFixed(1)}`);

  let bias = 'NEUTRO';
  if (biasScore >= 60) bias = 'BULL';
  else if (biasScore <= 40) bias = 'BEAR';

  let plan = '';
  const p = Number(price) || 0;
  if (biasScore >= 60) {
    const halfEntry = p * 0.0015; // 0.15% zone around live
    const slDist = p * (expMed/100) * 0.85;
    const t1Dist = p * (expMed/100);
    const t2Dist = p * (expMed/100) * 1.8;
    const entryLow = p - halfEntry, entryHigh = p + halfEntry;
    plan = `🟢 PLAN LONG\nEntry: ${entryLow.toFixed(2)} – ${entryHigh.toFixed(2)} (live $${p.toFixed(2)} ±0.15%)\nStop: ${(entryLow - slDist).toFixed(2)} (invalida cierre < SL)\nT1: ${(entryHigh + t1Dist).toFixed(2)} | T2: ${(entryHigh + t2Dist).toFixed(2)} (med ${expMed.toFixed(2)}% / p90 ${expP90.toFixed(2)}%)`;
    // log call for stats
    try { await lxRequest(`/calls`); } catch(_){}
    import('http').then(h=>{
      const body=JSON.stringify({symbol:ticker,direction:'long',entry_low:Number(entryLow.toFixed(2)),entry_high:Number(entryHigh.toFixed(2)),sl:Number((entryLow-slDist).toFixed(2)),t1:Number((entryHigh+t1Dist).toFixed(2)),t2:Number((entryHigh+t2Dist).toFixed(2)),bias_score:biasScore,note:`tg-bot live-anchored ${p.toFixed(2)}`});
      const req=h.request({hostname:'127.0.0.1',port:8000,path:'/calls',method:'POST',headers:{'Content-Type':'application/json'}},()=>{});
      req.on('error',()=>{}); req.write(body); req.end();
    }).catch(()=>{});
  } else if (biasScore <= 40) {
    const halfEntry = p * 0.0015;
    const slDist = p * (expMed/100) * 0.85;
    const t1Dist = p * (expMed/100);
    const t2Dist = p * (expMed/100) * 1.8;
    const entryLow = p - halfEntry, entryHigh = p + halfEntry;
    plan = `🔴 PLAN SHORT\nEntry: ${entryLow.toFixed(2)} – ${entryHigh.toFixed(2)} (live $${p.toFixed(2)} ±0.15%)\nStop: ${(entryHigh + slDist).toFixed(2)} (invalida cierre > SL)\nT1: ${(entryLow - t1Dist).toFixed(2)} | T2: ${(entryLow - t2Dist).toFixed(2)} (med ${expMed.toFixed(2)}% / p90 ${expP90.toFixed(2)}%)`;
    import('http').then(h=>{
      const body=JSON.stringify({symbol:ticker,direction:'short',entry_low:Number(entryLow.toFixed(2)),entry_high:Number(entryHigh.toFixed(2)),sl:Number((entryHigh+slDist).toFixed(2)),t1:Number((entryLow-t1Dist).toFixed(2)),t2:Number((entryLow-t2Dist).toFixed(2)),bias_score:biasScore,note:`tg-bot live-anchored ${p.toFixed(2)}`});
      const req=h.request({hostname:'127.0.0.1',port:8000,path:'/calls',method:'POST',headers:{'Content-Type':'application/json'}},()=>{});
      req.on('error',()=>{}); req.write(body); req.end();
    }).catch(()=>{});
  } else {
    plan = `🟡 NEUTRO — Esperar confirmación (bias ${biasScore} en 40-59, Rule 10: sin plan)`;
  }

  const timestamp = new Date().toLocaleString('es-ES', { timeZone: 'America/Mexico_City' });

  const report = `
╗══ ${ticker} — ANALISIS EN VIVO ══╗

PRECIO: $${price}
O/H/L: ${open} / ${high} / ${low}
Volumen: ${vol}

━━━ ORDER FLOW ━━━
Delta: ${delta}
DOM Imbalance: ${domImb}%
Bid/Ask Ratio: ${bidAsk}
POC: ${poc} | VAH: ${vah} | VAL: ${val}

━━━ CVD ━━━
CVD: ${cvdVal} (prev: ${prevCvd})
Last Delta: ${lastDelta}

━━━ SMART TAPE ━━━
Buy: ${buyVol} (${buyPct}%)
Sell: ${sellVol} (${sellPct}%)
Net: ${netVol}
Largest: ${largestTrade} ${String(largestSide).toUpperCase()}
Avg Trade: ${avgTrade}

━━━ SEÑALES QUANT ENGINE ━━━
${quantSignals}

━━━ ALERTAS ATAS ━━━
${atasAlerts}

━━━ BIAS SCORECARD ━━━
Score: ${biasScore}/100
Bias: ${bias}
Razones:
${biasReasons.map((r) => `• ${r}`).join('\n')}

━━━ PLAN ━━━
${plan}

  ━━━━━━━━━━━━━━━━━━━
${timestamp} | Bar ${ind.bar_index ?? '?'} | CVD ${cvd?.[ticker]?.bars ?? '?'} bars | Live age ${Math.round(Date.now()/1000 - (ind.timestamp||Date.now())/1000)}s
${(() => {
  const n = stats?.n_traded ?? 0;
  const conf = stats?.confidence ?? 'insufficient';
  if (n >= 30 && (conf==='medium'||conf==='high')) {
    return `Probability: validated — n=${n} winrate ${(stats.winrate*100).toFixed(1)}% PF ${stats.profit_factor} exp ${stats.expectancy_r>0?'+':''}${stats.expectancy_r}R (Rule 3)`;
  }
  return `Probability: unvalidated — not backtested (Rule 3). n=${n} ${conf}, need N≥30 (audit N≥185 for 60% @p<0.05).`;
})()}
`;
  return report.trim();
}

async function runStatus() {
  const lxOk = await lxRequest('/indicators').then(() => true).catch(() => false);
  const quantOk = await lxRequest('/quant/SUI').then(() => true).catch(() => false);
  if (!client || !client._ws || client._ws.readyState !== 1) await cdpConnect();
  return [
    '🔧 *STATUS DEL BOT*',
    '',
    `LiveExporter (8000): ${lxOk ? '✅ UP' : '❌ DOWN'}`,
    `Quant Engine: ${quantOk ? '✅ UP' : '❌ DOWN'}`,
    `TradingView CDP (9222): ${cdpOk ? '✅ UP' : '⚠️ NO (analysis still works via LiveExporter)'}`,
    '',
    `Token: ${TG_TOKEN.slice(0, 10)}...`,
    `Chat ID: ${TG_CHAT}`,
    'Comandos: /a /long /short /p /status /help',
  ].join('\n');
}

// ─── Telegram send (direct HTTPS — always works, no poller needed) ──
async function tgSend(text) {
  const https = await import('https');
  const body = JSON.stringify({ chat_id: TG_CHAT, text, parse_mode: 'Markdown' });
  return new Promise((resolvePromise, reject) => {
    const req = https.request({
      hostname: 'api.telegram.org',
      path: `/bot${TG_TOKEN}/sendMessage`,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      timeout: 25000,
    }, (res) => {
      let data = '';
      res.on('data', (d) => (data += d));
      res.on('end', () => {
        try {
          const j = JSON.parse(data);
          if (!j.ok) reject(new Error(j.description || 'tg error'));
          else resolvePromise(j);
        } catch (e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('tg timeout')));
    req.write(body);
    req.end();
  });
}

async function tgSendLong(text) {
  const MAX = 4000;
  const chunks = [];
  let remaining = text;
  while (remaining.length > MAX) {
    let cut = remaining.lastIndexOf('\n', MAX);
    if (cut < 0) cut = MAX;
    chunks.push(remaining.slice(0, cut));
    remaining = remaining.slice(cut + 1);
  }
  chunks.push(remaining);
  for (const c of chunks) {
    await tgSend(c);
    await new Promise((r) => setTimeout(r, 400));
  }
}

// ─── Two-way commands (manual getUpdates polling) ───────────────
// NOTE: Uses manual HTTPS getUpdates long-polling — NOT Telegraf.
// Telegraf's bot.launch() hangs on this machine (verified: 35s+ no resolve),
// which is the historical root cause of "two-way bot broke". This manual
// pattern is proven working (original telegram-bot.js logs show "Report sent").

let lastUpdateId = 0;

async function tgRequest(method, params = {}) {
  const https = (await import('https')).default;
  return new Promise((resolvePromise, reject) => {
    const url = new URL(`https://api.telegram.org/bot${TG_TOKEN}/${method}`);
    Object.keys(params).forEach((k) => {
      if (params[k] !== undefined && params[k] !== null) {
        url.searchParams.set(k, typeof params[k] === 'object' ? JSON.stringify(params[k]) : String(params[k]));
      }
    });
    https.get(url, { timeout: 30000 }, (res) => {
      let data = '';
      res.on('data', (d) => (data += d));
      res.on('end', () => {
        try { resolvePromise(JSON.parse(data)); } catch (e) { reject(e); }
      });
    }).on('error', reject);
  });
}

function isAnalysisCmd(text) {
  const lower = text.toLowerCase();
  return lower === '/a' || lower === '/analisis' || lower === '/analysis' || lower === 'analisis' || lower === 'analysis' || lower.includes('anal');
}
function isLongCmd(text) {
  const lower = text.toLowerCase();
  return lower === '/long' || lower === 'long';
}
function isShortCmd(text) {
  const lower = text.toLowerCase();
  return lower === '/short' || lower === 'short';
}
function isPriceCmd(text) {
  const lower = text.toLowerCase();
  return lower === '/p' || lower === '/price';
}
function isStatusCmd(text) {
  const lower = text.toLowerCase();
  return lower === '/status';
}

async function handleCommand(text, chatId, reply) {
  const fromUser = 'telegram';
  const ageSeconds = 0;
  log(`[BOT] Message from @${fromUser} (${chatId}): "${text}"`);

  if (text.trim().toLowerCase() === '/start' || text.trim().toLowerCase() === '/help') {
    await reply('🤖 *Bot OPCode activo*\n\n/a — análisis completo\n/long — plan long\n/short — plan short\n/p — precio\n/status — estado del bot');
    return;
  }
  if (isLongCmd(text) || isShortCmd(text)) {
    const dir = isLongCmd(text) ? 'LONG' : 'SHORT';
    await reply(`⏳ Calculando plan ${dir}...`);
    const report = await runAnalysis();
    if (report == null) {
      await reply('⚠️ No puedo generar el plan: LiveExporter/ATAS sin datos frescos. Abre ATAS con el indicador LiveExporter.');
      return;
    }
    await tgSendLong(`🎯 *SEÑAL SOLICITADA: ${dir}*\n\n${report}`);
    log(`[BOT] ${dir} report sent.`);
    return;
  }
  if (isAnalysisCmd(text)) {
    await reply('⏳ Ejecutando análisis...');
    const report = await runAnalysis();
    if (report == null) {
      await reply('⚠️ No puedo generar el análisis: LiveExporter/ATAS sin datos frescos. Abre ATAS con el indicador LiveExporter.');
      return;
    }
    await tgSendLong(report);
    log('[BOT] Report sent.');
    return;
  }
  if (isPriceCmd(text)) {
    const ind = await lxRequest('/indicators');
    const q = ind.price || {};
    await reply(`📊 *${ind.ticker}* — $${q.last}\nO:${q.open} H:${q.high} L:${q.low} | Vol ${ind.volume?.total}`);
    return;
  }
  if (isStatusCmd(text)) {
    await reply(await runStatus());
    return;
  }
  await reply('Usa /a, /long, /short, /p, /status');
}

async function pollOnce() {
  const resp = await tgRequest('getUpdates', {
    offset: lastUpdateId + 1,
    timeout: 30,
    limit: 10,
  });
  if (!resp.ok || !resp.result) return;
  for (const update of resp.result) {
    lastUpdateId = update.update_id;
    const msg = update.message;
    if (!msg || !msg.text) continue;
    if (String(msg.chat.id) !== TG_CHAT) {
      log(`[BOT] Ignoring chat ${msg.chat.id} (configured: ${TG_CHAT})`);
      continue;
    }
    const ageSeconds = Math.floor(Date.now() / 1000) - msg.date;
    if (ageSeconds > 1800) {
      log(`[BOT] Skipping old message (${ageSeconds}s): "${msg.text}"`);
      continue;
    }
    try {
      await handleCommand(msg.text, msg.chat.id, (txt) => tgSend(txt));
    } catch (e) {
      log('[BOT] Command error:', e.message);
      try { await tgSend(`❌ Error: ${e.message}`); } catch (_) {}
    }
  }
}

async function startTwoWay() {
  log('[BOT] Manual getUpdates polling started (two-way LIVE)');
  while (true) {
    try {
      await pollOnce();
    } catch (e) {
      log('[BOT] Polling error:', e.message);
      await new Promise((r) => setTimeout(r, 2000));
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

// ─── Periodic analysis + heartbeat ──────────────────────────────
// Freshness guard: /indicators carries a `timestamp` (ms). If it is older
// than STALE_MS (ATAS/LiveExporter closed or frozen), treat the data as
// stale and DO NOT emit a report — otherwise the bot re-sends the same
// frozen analysis every 15 min.
const STALE_MS = 5 * 60 * 1000; // 5 min max age for a live snapshot
const INDICATOR_OK_KEYS = ['price', 'volume', 'delta', 'dom', 'tape', 'footprint'];
let lastStaleWarned = 0;

function indicatorsFresh(ind) {
  if (!ind || typeof ind !== 'object') return false;
  if (ind.status === 'no_data' || ind.status === 'error') return false;
  const ts = Number(ind.timestamp);
  if (!Number.isFinite(ts) || ts <= 0) return false;
  // Guard against a frozen LiveExporter file re-read with an old timestamp.
  return (Date.now() - ts) < STALE_MS;
}

// Dedup: the 15-min tick only sends a new report when the underlying
// data actually changed (bar/price/delta/CVD/DOM/footprint/alerts).
// While LiveExporter is frozen (ATAS closed) or nothing moves, the
// same report is NOT re-sent and no duplicate /calls entry is logged.
const _roundNum = (v, d = 2) => {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(d) : '∅';
};

async function periodicStateDigest() {
  // Side-effect-free snapshot of everything that changes the periodic report.
  const ind = await lxRequest('/indicators');
  const ticker = (ind.ticker || '').toUpperCase() || 'SUIUSDT';
  const shortSym = ticker.endsWith('USDT') ? ticker.slice(0, -4) : ticker;
  const [cvd, atas, quant, ctx] = await Promise.all([
    lxRequest('/cvd').catch(() => ({})),
    lxRequest(`/state/${ticker}`).catch(() => ({ alerts: [] })),
    lxRequest(`/quant/${shortSym}`).catch(() => ({ alerts: [] })),
    lxRequest(`/context/${ticker}`).catch(() => ({})),
  ]);
  const p = ind.price || {};
  const d = ind.dom || {};
  const v = ind.volume || {};
  const t = ind.tape || {};
  const f = ind.footprint || {};
  const cv = cvd[ticker] || {};
  const em = (ctx.expected_move) || {};
  return [
    ticker,
    String(ind.bar_index ?? '?'),
    _roundNum(p.last),
    [_roundNum(p.open), _roundNum(p.high), _roundNum(p.low)].join('/'),
    _roundNum(ind.delta, 1),
    _roundNum(v.total, 0),
    _roundNum(d.imbalance_pct, 1),
    _roundNum(f.poc),
    _roundNum(f.value_area_high),
    _roundNum(f.value_area_low),
    [_roundNum(t.buy_vol, 1), _roundNum(t.sell_vol, 1), _roundNum(t.net_vol, 1)].join('/'),
    `${_roundNum(cv.cvd, 0)}/${_roundNum(cv.prev_cvd, 0)}/${cv.bars ?? '?'}`,
    _roundNum(em.value, 4),
    'A:' + (atas.alerts || []).slice(-3).map((a) => a.raw || a.text || a.signal_name || '').join('|'),
    'Q:' + (quant.alerts || []).slice(-3).map((a) => `${a.signal_name || ''}@${a.precio}${a.timeframe || ''}${a.color || ''}`).join('|'),
  ].join('§');
}

function startScheduler() {
  let lastDigest = null;
  const tick = async () => {
    try {
      const ind = await lxRequest('/indicators').catch(() => null);
      if (!indicatorsFresh(ind)) {
        log('[BOT] Periodic analysis skipped (stale/no_data indicators)');
        // Warn at most once every 30 min instead of spamming every 15.
        const now = Date.now();
        if (now - lastStaleWarned > 30 * 60 * 1000) {
          lastStaleWarned = now;
          try { await tgSend('⚠️ Bot: LiveExporter/ATAS sin datos frescos — análisis periódico en pausa hasta que ATAS esté abierto.'); } catch (_) {}
        }
        return;
      }
      const digest = await periodicStateDigest();
      if (digest === lastDigest) {
        log('[BOT] Periodic analysis skipped (state unchanged)');
        return;
      }
      lastDigest = digest;
      const report = await runAnalysis();
      if (report == null) {
        log('[BOT] Periodic analysis skipped (runAnalysis stale)');
        return;
      }
      await tgSendLong(report);
      log('[BOT] Periodic analysis sent');
    } catch (e) {
      log('[BOT] Periodic analysis FAILED:', e.message);
      // Fail loud — tell the user the bot is degraded (separate concern from signals)
      try { await tgSend(`⚠️ Bot: análisis periódico falló (${e.message})`); } catch (_) {}
    }
  };
  setInterval(tick, ANALYSIS_INTERVAL_MS);
  setInterval(async () => {
    try { await tgSend('❤️ Heartbeat OK'); } catch (e) { log('[BOT] Heartbeat fail:', e.message); }
  }, HEARTBEAT_INTERVAL_MS);
}

// ─── Main ───────────────────────────────────────────────────────
async function main() {
  log('[BOT] Unified Telegram Bot starting...');
  log('[BOT] LiveExporter:', LX_API);
  log('[BOT] Chat ID:', TG_CHAT);

  if (!process.argv.includes('--once')) {
    await cdpConnect(); // best-effort warmup (optional)
  }

  // Startup notification
  try {
    await tgSend('🤖 Bot OPCode unificado ONLINE — dos vías activas. Envía /a, /long, /short.');
    log('[BOT] Startup notification sent');
  } catch (e) {
    log('[BOT] Startup msg failed (may be duplicate/conflict):', e.message);
  }

  if (process.argv.includes('--once')) {
    log('[BOT] --once mode: sending one analysis then exiting');
    const report = await runAnalysis();
    await tgSendLong(report);
    log('[BOT] Done');
    process.exit(0);
  }

  // Start periodic scheduler (background) BEFORE the infinite polling loop
  startScheduler();
  log('[BOT] Scheduler armed (periodic analysis every 15min, heartbeat 15min)');

  await startTwoWay(); // infinite loop — polls getUpdates
  log('[BOT] All systems LIVE. Ctrl+C to stop.');
}

main().catch((e) => {
  log('[BOT] Fatal:', e.message);
  process.exit(1);
});
