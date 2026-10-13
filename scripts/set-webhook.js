// Registers the webhook with Telegram. Run once per deploy URL or secret change:
//   WEBHOOK_BASE_URL=https://your-app.example npm run set-webhook
import { loadConfig } from '../src/config.js';
import { createTelegramClient } from '../src/telegram.js';
import { WEBHOOK_PATH } from '../src/handler.js';

const config = loadConfig(process.env);
const base = (process.env.WEBHOOK_BASE_URL ?? '').trim();
if (!base.startsWith('https://')) {
  console.error('WEBHOOK_BASE_URL must be an https:// URL');
  process.exit(1);
}
const url = new URL(WEBHOOK_PATH, base).href;
const telegram = createTelegramClient(config.token);

const me = await telegram.call('getMe');
await telegram.call('setWebhook', {
  url,
  secret_token: config.webhookSecret,
  allowed_updates: ['message'],
  // Commands sent while no webhook was set are discarded, not replayed.
  drop_pending_updates: true,
  // One update at a time, so two /verify runs never overlap.
  max_connections: 1,
});
const info = await telegram.call('getWebhookInfo');

console.log(`Bot: @${me.username} (${me.id})`);
console.log(`Webhook: ${info.url}`);
console.log(`Pending updates: ${info.pending_update_count}`);
if (info.last_error_message) console.log(`Last delivery error: ${info.last_error_message}`);
