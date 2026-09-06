import 'dotenv/config';
import CDP from 'chrome-remote-interface';

const TG_TOKEN = process.env.TELEGRAM_TOKEN;
const CHAT_ID = process.env.TELEGRAM_CHAT_ID;
const POLL_TIMEOUT = 30; // seconds for long polling

let client = null;

async function findChartTarget() {
  const targets = await CDP.List({ host: 'localhost', port: 9222 });
  return targets.find(t =>
    t.url && (t.url.includes('tradingview.com/chart') || t.title.includes('TradingView'))
  ) || targets[0];
}

async function connectCDP() {
  const target = await findChartTarget();
  if (!target) throw new Error('No TradingView target found');
  client = await CDP({ host: 'localhost', port: 9222, target: target.id });
  await client.Runtime.enable();
  console.log('[BOT] CDP Runtime enabled');
}

async function cdpEval(expr) {
  if (!client) throw new Error('CDP not connected');
  const { result } = await client.Runtime.evaluate({
    expression: expr,
    returnByValue: true,
    awaitPromise: false,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'Eval error');
  return result.value;
}

async function getQuote() {
  return cdpEval(
    '(function(){' +
    'var wv=window.TradingViewApi._activeChartWidgetWV;' +
    'var ms=wv.value()._chartWidget.model().mainSeries();' +
    'var last=ms.bars().last();' +
    'return{sym:ms.symbol(),close:last.value[4],open:last.value[1],high:last.value[2],low:last.value[3],vol:last.value[5],time:last.value[0]};' +
    '})()'
  );
}

async function getMpsTable() {
  return cdpEval(
    '(function(){' +
    'var sources=window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();' +
    'for(var i=0;i<sources.length;i++){' +
    'var s=sources[i];var meta=s.metaInfo();' +
    'if(meta&&meta.description&&meta.description.indexOf("Master Pattern Scanner")>=0){' +
    'var pc=s._graphics&&s._graphics._primitivesCollection;' +
    'if(!pc||!pc._primitives)return null;' +
    'for(var j=0;j<pc._primitives.length;j++){' +
    'var prim=pc._primitives[j];' +
    'if(prim._type==="table"){' +
    'var out={};' +
    'for(var r=0;r<prim._rows;r++){' +
    'for(var c=0;c<prim._columns;c++){' +
    'var cell=prim._cells&&prim._cells[r]&&prim._cells[r][c];' +
    'if(cell&&cell._text!==undefined){' +
    'if(c===0)out[cell._text]=null;' +
    'else if(out[prim._cells[r][0]._text]!==undefined)out[prim._cells[r][0]._text]=cell._text;' +
    '}' +
    '}' +
    '}' +
    'return out;' +
    '}' +
    '}' +
    'return null;' +
    '}' +
    '}' +
    'return null;' +
    '})()'
  );
}

async function getBookFlowData() {
  return cdpEval(
    '(function(){' +
    'var sources=window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().model().dataSources();' +
    'for(var i=0;i<sources.length;i++){' +
    'var s=sources[i];var meta=s.metaInfo();' +
    'if(meta&&meta.description&&meta.description.indexOf("BookFlow Pro")>=0){' +
    'var pc=s._graphics&&s._graphics._primitivesCollection;' +
    'if(!pc||!pc._primitives)return null;' +
    'var result={tables:[],labels:[],lines:[]};' +
    'for(var j=0;j<pc._primitives.length;j++){' +
    'var prim=pc._primitives[j];' +
    'if(prim._type==="table"){' +
    'var rows=[];' +
    'for(var r=0;r<prim._rows;r++){' +
    'var row=[];' +
    'for(var c=0;c<prim._columns;c++){' +
    'var cell=prim._cells&&prim._cells[r]&&prim._cells[r][c];' +
    'if(cell&&cell._text!==undefined)row.push(cell._text);' +
    '}' +
    'if(row.length)rows.push(row.join(" | "));' +
    '}' +
    'result.tables=result.tables.concat(rows);' +
    '}' +
    'if(prim._type==="label"&&prim._text!==undefined){' +
    'result.labels.push(prim._text+(prim._price!==undefined?" @ "+prim._price:""));' +
    '}' +
    'if(prim._type==="line"&&prim._price!==undefined){' +
    'result.lines.push(prim._price);' +
    '}' +
    '}' +
    'return result;' +
    '}' +
    '}' +
    'return null;' +
    '})()'
  );
}

function formatMps(mps) {
  if (!mps) return null;
  const lines = [];
  const keyRows = ['Trend', 'Marubozu', 'Engulfing', 'FVG', 'Liq Sweep', 'BOS', 'CHoCH', 'ATR(14)', 'Pivot Len', 'Doji', 'Hammer Family', 'Star', 'Spinning Top'];
  for (const k of keyRows) {
    if (mps[k] !== undefined) lines.push(k + ': ' + mps[k]);
  }
  return lines.join('\n');
}

function formatBookFlow(bf) {
  if (!bf) return null;
  const parts = [];
  if (bf.tables && bf.tables.length) parts.push('📋 Tablas:\n' + bf.tables.join('\n'));
  if (bf.labels && bf.labels.length) parts.push('🏷 Labels:\n' + bf.labels.join('\n'));
  if (bf.lines && bf.lines.length) {
    const levels = [...new Set(bf.lines.map(v => '$' + Number(v).toFixed(2)))];
    parts.push('📊 Niveles:\n' + levels.join(' '));
  }
  return parts.length ? parts.join('\n\n') : null;
}

async function main() {
  console.log('[BOT] Connecting to CDP...');
  try {
    await connectCDP();
  } catch (e) {
    console.log('[BOT] ERROR: Cannot connect to TradingView CDP on port 9222 —', e.message);
    process.exit(1);
  }
  console.log('[BOT] CDP connected');

  await new Promise(r => setTimeout(r, 1000));
  try {
    const q = await getQuote();
    if (q && q.sym) console.log('[BOT] Current:', q.sym, '$' + q.close);
  } catch (e) {
    console.log('[BOT] Warmup error:', e.message);
  }

  const { Telegraf } = await import('telegraf');
  const bot = new Telegraf(TG_TOKEN);

  let knownChatId = CHAT_ID;

  bot.on('text', async (ctx) => {
    const txt = ctx.message.text;
    const chatId = String(ctx.chat.id);
    if (chatId !== knownChatId) {
      console.log('[BOT] Chat ID changed:', knownChatId, '->', chatId);
      knownChatId = chatId;
    }
    console.log('[BOT] <<<', txt);

    try {
      const parts = txt.split(' ');
      const cmd = parts[0].toLowerCase();

      if (cmd === '/p' || cmd === '/price') {
        const q = await getQuote();
        if (q) {
          const change = (q.close - q.open);
          const changePct = (change / q.open * 100);
          const dir = change >= 0 ? '🟢' : '🔴';
          await ctx.replyWithMarkdown(
            '📊 *' + q.sym + '* ' + dir + '\n' +
            'Precio: `$' + q.close.toFixed(2) + '`\n' +
            'O: $' + q.open.toFixed(2) + ' | H: $' + q.high.toFixed(2) + ' | L: $' + q.low.toFixed(2) + '\n' +
            'Cambio: ' + (change >= 0 ? '+' : '') + change.toFixed(2) + ' (' + (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%)\n' +
            'Vol: ' + (q.vol / 1000).toFixed(1) + 'k'
          );
        } else {
          await ctx.reply('❌ No se pudo obtener precio');
        }

      } else if (cmd === '/a' || cmd === '/analyze') {
        const [q, mps, bf] = await Promise.all([getQuote(), getMpsTable(), getBookFlowData()]);

        const msg = [];
        if (q) {
          const change = (q.close - q.open);
          const changePct = (change / q.open * 100);
          const dir = change >= 0 ? '🟢' : '🔴';
          msg.push('📊 *' + q.sym + '* ' + dir);
          msg.push('$' + q.close.toFixed(2) + ' | ' + (change >= 0 ? '+' : '') + change.toFixed(2) + ' (' + (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%) | Vol ' + (q.vol / 1000).toFixed(1) + 'k');
        }

        if (mps) {
          const trend = mps['Trend'] || '—';
          const trendIcon = trend === 'Up' ? '🟢' : trend === 'Down' ? '🔴' : '⚪';
          msg.push('');
          msg.push('*MPS v6.2*');
          msg.push('Trend: ' + trend + ' ' + trendIcon);
          const active = Object.entries(mps).filter(([k, v]) => v && v !== '—' && k !== 'Trend' && k !== 'ATR(14)' && k !== 'Pivot Len');
          for (const [k, v] of active) {
            msg.push(k + ': ' + v);
          }
          if (mps['ATR(14)']) msg.push('ATR(14): ' + mps['ATR(14)']);
          if (mps['Pivot Len']) msg.push('Pivot Len: ' + mps['Pivot Len']);
        }

        if (bf) {
          msg.push('');
          msg.push('*BookFlow Pro*');
          if (bf.tables && bf.tables.length) msg.push(bf.tables.join('\n'));
          if (bf.lines && bf.lines.length) {
            const levels = [...new Set(bf.lines.map(v => '$' + Number(v).toFixed(2)))];
            msg.push('Niveles: ' + levels.join(' | '));
          }
        }

        await ctx.replyWithMarkdown(msg.join('\n') || '❌ Sin datos');

      } else if (cmd === '/start') {
        await ctx.replyWithMarkdown(
          '🤖 *Bot TV activo*\n\n' +
          '*/p* — precio y cambio\n' +
          '*/a* — análisis completo (MPS + BookFlow)\n' +
          '*/setchatid* — ver Chat ID'
        );

      } else if (cmd === '/setchatid') {
        await ctx.reply('Chat ID: `' + ctx.chat.id + '`');

      } else {
        await ctx.reply('Usa /p, /a, /setchatid');
      }
    } catch (e) {
      console.log('[BOT] Error:', e.message);
      try { await ctx.reply('❌ Error: ' + e.message); } catch (_) {}
    }
    console.log('[BOT] Handler done');
  });

  console.log('[BOT] Telegram bot is LIVE');
  try {
    const me = await bot.telegram.getMe();
    console.log('[BOT] Bot username: @' + me.username);
  } catch (_) {}
  // Start polling in background with auto-retry on 409 conflicts
  async function startPolling() {
    try {
      await bot.launch();
    } catch (e) {
      console.log('[BOT] Polling error:', e.message);
      if (e.message.includes('409')) {
        console.log('[BOT] Conflict detected, retrying in 5s...');
        await new Promise(r => setTimeout(r, 5000));
        startPolling();
      }
    }
  }
  startPolling();
  console.log('[BOT] Polling started');
}

main().catch(e => {
  console.log('[BOT] Fatal:', e.message);
  process.exit(1);
});
