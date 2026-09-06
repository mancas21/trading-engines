import { z } from 'zod';
import { jsonResult } from './_format.js';

// Lazy-loaded core references — populated on first command use
let _dataCore = null;
let _chartCore = null;

async function getDataCore() {
  if (!_dataCore) _dataCore = await import('../core/data.js');
  return _dataCore;
}

async function getChartCore() {
  if (!_chartCore) _chartCore = await import('../core/chart.js');
  return _chartCore;
}

let botPromise = null;
let chatId = null;

// NOTE: This module is SEND-ONLY. It deliberately does NOT launch a Telegraf
// poller (no b.launch()). The standalone `telegram-bot.mjs` (run under
// supervisor.ps1) is the single long-polling consumer of this token. Running
// multiple pollers on the same token causes Telegram 409 conflicts, which is
// the historical cause of the "two-way bot broke" failure. Send-only Telegraf
// instances (b.telegram.sendMessage) work fine without launch().

function formatPrice(num) {
  return num < 1 ? num.toFixed(6) : num < 100 ? num.toFixed(2) : num.toFixed(2);
}

async function getBot() {
  if (botPromise) return botPromise;

  botPromise = (async () => {
    const { Telegraf } = await import('telegraf');
    const token = process.env.TELEGRAM_TOKEN;
    if (!token) {
      throw new Error('TELEGRAM_TOKEN environment variable not set');
    }
    const b = new Telegraf(token);
    chatId = process.env.TELEGRAM_CHAT_ID || null;

    b.on('text', async (ctx) => {
      const msg = ctx.message.text;
      const senderId = ctx.chat.id;
      process.stderr.write(`[telegram] "${msg}" from ${senderId}\n`);

      if (!msg.startsWith('/')) {
        await ctx.reply('Usa /p <symbol>, /a <symbol>, /help');
        return;
      }

      const cmd = msg.split(' ')[0].toLowerCase();
      const args = msg.slice(cmd.length).trim();

      try {
        switch (cmd) {
          case '/start':
            await ctx.reply('🤖 Bot TV activo.\n/p <symbol> — precio\n/a <symbol> — analisis');
            break;

          case '/price':
          case '/p': {
            const sym = args || 'SPCXUSDT.P';
            await ctx.reply(`Consultando ${sym}...`);
            const d = await getDataCore();
            const quote = await d.getQuote({ symbol: sym });
            if (quote.success) {
              await ctx.reply(
                '📊 *' + sym + '*\n' +
                'Price: `$' + formatPrice(quote.last) + '`\n' +
                'O: $' + formatPrice(quote.open) + ' H: $' + formatPrice(quote.high) + ' L: $' + formatPrice(quote.low) + '\n' +
                'Vol: ' + (quote.volume / 1000).toFixed(1) + 'k',
                { parse_mode: 'Markdown' }
              );
            } else {
              await ctx.reply('Error: ' + (quote.error || 'sin datos'));
            }
            break;
          }

          case '/analyze':
          case '/a': {
            const sym = args || 'SPCXUSDT.P';
            await ctx.reply(`Analizando ${sym}...`);
            const d = await getDataCore();
            const [quote, tables] = await Promise.all([
              d.getQuote({ symbol: sym }),
              d.getPineTables({ study_filter: 'Master Pattern Scanner' }).catch(() => ({ studies: [] })),
            ]);

            const lines = [];
            if (quote.success) {
              lines.push('📊 *' + sym + '* | $' + formatPrice(quote.last) + ' | Vol: ' + (quote.volume / 1000).toFixed(1) + 'k');
            }

            const patterns = tables.studies?.[0]?.tables?.[0]?.rows || [];
            const active = patterns.filter(r =>
              !r.includes('Pattern |') && !r.includes('Pivot') && !r.includes('ATR') && !r.includes('| —') && r.includes('|')
            );
            if (active.length) {
              lines.push('');
              lines.push('*Patrones:*');
              active.forEach(r => {
                const [pat, status] = r.split('|').map(s => s.trim());
                const emoji = status.includes('Bull') || status.includes('Active') || status.includes('✓') || status.includes('Up') ? '🟢' : status.includes('Bear') || status.includes('Down') || status.includes('Top') || status.includes('⚠') ? '🔴' : '⚪';
                lines.push(emoji + ' ' + pat + ': ' + status);
              });
            } else {
              lines.push('\nSin patrones activos.');
            }

            await ctx.reply(lines.join('\n'), { parse_mode: 'Markdown' });
            break;
          }

          case '/help':
          case '/h':
            await ctx.reply('/p <sym> - Precio\n/a <sym> - Analisis\n/setchatid - Fijar chat');
            break;

          case '/setchatid':
            chatId = String(senderId);
            await ctx.reply('Chat ID fijado: ' + chatId);
            break;

          default:
            await ctx.reply('Comando no reconocido: ' + cmd);
        }
      } catch (e) {
        process.stderr.write('[telegram] Error: ' + e.message + '\n');
        try { await ctx.reply('Error: ' + e.message); } catch (_) {}
      }
    });

    b.launch = async () => {}; // SEND-ONLY: never start polling (see module note)

    return b;
  })();

  return botPromise;
}

// Eager init — start bot when module loads
getBot().catch(err => {
  process.stderr.write('[telegram] Eager init error: ' + err.message + '\n');
});

export function registerTelegramTools(server) {
  server.tool('telegram_send_message', 'Send a message/alert to your Telegram', {
    message: z.string().describe('Message text to send'),
  }, async ({ message }) => {
    try {
      const b = await getBot();
      const target = chatId;
      if (!target) {
        return jsonResult({ success: false, error: 'TELEGRAM_CHAT_ID not configured. Use /setchatid in Telegram or set env var.' }, true);
      }
      await b.telegram.sendMessage(target, message);
      return jsonResult({ success: true, sent: true });
    } catch (err) {
      return jsonResult({ success: false, error: err.message }, true);
    }
  });

  server.tool('telegram_send_alert', 'Send a formatted trading alert to Telegram', {
    symbol: z.string().describe('Trading symbol'),
    signal: z.string().describe('Signal type (LONG/SHORT/NEUTRAL)'),
    price: z.number().describe('Current price'),
    reason: z.string().describe('Brief reason for the signal'),
    levels: z.string().optional().describe('Support/resistance levels'),
  }, async ({ symbol, signal, price, reason, levels }) => {
    try {
      const emoji = signal === 'LONG' ? '🟢' : signal === 'SHORT' ? '🔴' : '⚪';
      const message = [
        emoji + ' *' + symbol + ' \u2014 ' + signal + '*',
        'Price: `$' + price + '`',
        'Reason: ' + reason,
        levels ? 'Levels: ' + levels : '',
      ].filter(Boolean).join('\n');

      const b = await getBot();
      const target = chatId;
      if (!target) {
        return jsonResult({ success: false, error: 'TELEGRAM_CHAT_ID not configured' }, true);
      }
      await b.telegram.sendMessage(target, message, { parse_mode: 'Markdown' });
      return jsonResult({ success: true, sent: true });
    } catch (err) {
      return jsonResult({ success: false, error: err.message }, true);
    }
  });

  server.tool('telegram_set_chat_id', 'Set the Telegram chat ID for outgoing messages', {
    chat_id: z.string().describe('Telegram chat ID'),
  }, async ({ chat_id }) => {
    chatId = chat_id;
    return jsonResult({ success: true, chat_id });
  });

  server.tool('telegram_bot_status', 'Check if Telegram bot is initialized and running', {}, async () => {
    try {
      const b = await getBot();
      return jsonResult({ success: true, status: 'running', chat_id: chatId || null });
    } catch (err) {
      return jsonResult({ success: false, status: 'error', error: err.message }, true);
    }
  });
}
