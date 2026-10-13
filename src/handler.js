import { loadConfig } from './config.js';
import { createTelegramClient } from './telegram.js';
import { handleUpdate } from './bot.js';

export const WEBHOOK_PATH = '/telegram/webhook';
export const HEALTH_PATH = '/health';

function reply(status, body, headers = {}) {
  return new Response(body, { status, headers: { 'content-type': 'text/plain; charset=utf-8', ...headers } });
}

// Constant-time comparison so the secret cannot be guessed byte by byte.
function safeEqual(received, expected) {
  const encoder = new TextEncoder();
  const a = encoder.encode(received);
  const b = encoder.encode(expected);
  let diff = a.length ^ b.length;
  for (let i = 0; i < b.length; i++) diff |= (a[i] ?? 0) ^ b[i];
  return diff === 0;
}

/**
 * Builds a Web-standard (Request -> Response) handler, usable from any
 * serverless runtime. The configuration is validated once, here.
 */
export function createHandler({ env, fetch = globalThis.fetch, now, sleep, logger = console }) {
  let config = null;
  try {
    config = loadConfig(env);
  } catch (err) {
    logger.error(`[config] ${err.message}`);
  }
  const telegram = config && createTelegramClient(config.token, { fetch });

  return async function handleRequest(request) {
    const { pathname } = new URL(request.url);

    if (pathname === HEALTH_PATH) {
      // Fails when misconfigured, so a deploy with bad variables is not shown as healthy.
      return config ? reply(200, 'ok') : reply(503, 'misconfigured');
    }
    if (pathname !== WEBHOOK_PATH) return reply(404, 'not found');
    if (request.method !== 'POST') return reply(405, 'method not allowed', { allow: 'POST' });
    if (!config) return reply(503, 'misconfigured');

    const secret = request.headers.get('x-telegram-bot-api-secret-token') ?? '';
    if (!safeEqual(secret, config.webhookSecret)) return reply(401, 'unauthorized');

    let update;
    try {
      update = await request.json();
    } catch {
      return reply(400, 'bad request');
    }

    try {
      const results = await handleUpdate(update, { config, telegram, now, sleep });
      if (results) {
        const summary = results.map((r) => `${r.userId}=${r.status}`).join(' ');
        logger.log(`[verify] update ${update.update_id}: ${summary}`);
      }
    } catch (err) {
      logger.error(`[verify] update ${update?.update_id}: ${err.message}`);
    }
    // Always 200 once authenticated: a non-2xx answer makes Telegram redeliver
    // the update, which would run the verification again.
    return reply(200, 'ok');
  };
}
