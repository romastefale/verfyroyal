import { TelegramError } from './telegram.js';

export const COMMAND = '/verify';
// Commands older than this are ignored, so a delayed delivery cannot trigger
// a verification run the owner no longer expects.
export const MAX_MESSAGE_AGE_SECONDS = 300;
export const MAX_RATE_LIMIT_RETRIES = 2;
export const MAX_RETRY_AFTER_SECONDS = 10;
// Whole run budget, kept below the time Telegram waits for a webhook answer.
export const RUN_BUDGET_MS = 25_000;
const MAX_TEXT_LENGTH = 4096;
const MAX_DETAIL_LENGTH = 120;

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function isOwnerCommand(message, config, nowMs) {
  if (!message || typeof message.text !== 'string') return false;
  if (message.chat?.type !== 'private') return false;
  if (message.from?.id !== config.ownerId || message.from.is_bot) return false;
  if (message.chat.id !== config.ownerId) return false;
  if (!Number.isInteger(message.date) || nowMs / 1000 - message.date > MAX_MESSAGE_AGE_SECONDS) return false;
  const first = message.text.trim().split(/\s+/, 1)[0];
  return first.split('@', 1)[0].toLowerCase() === COMMAND;
}

async function verifyOne(userId, { config, telegram, now, sleep, deadline }) {
  const params = { user_id: userId };
  if (config.customDescription) params.custom_description = config.customDescription;

  for (let attempt = 0; ; attempt++) {
    try {
      const result = await telegram.call('verifyUser', params);
      return result === true
        ? { userId, status: 'verified' }
        : { userId, status: 'failed', detail: 'Telegram did not return true' };
    } catch (err) {
      if (!(err instanceof TelegramError)) throw err;
      if (err.errorCode === 429) {
        const waitMs = (err.retryAfter ?? 1) * 1000;
        const canRetry = attempt < MAX_RATE_LIMIT_RETRIES
          && waitMs <= MAX_RETRY_AFTER_SECONDS * 1000
          && now() + waitMs < deadline;
        if (canRetry) {
          await sleep(waitMs);
          continue;
        }
        return { userId, status: 'failed', detail: err.description };
      }
      if (err.errorCode === 403) return { userId, status: 'forbidden', detail: err.description };
      if (err.errorCode === null) return { userId, status: 'unknown', detail: err.description };
      return { userId, status: 'failed', detail: err.description };
    }
  }
}

export async function verifyTargets(ctx) {
  const deadline = ctx.now() + RUN_BUDGET_MS;
  const results = [];
  let stopReason = null;

  for (const userId of ctx.config.targetIds) {
    if (!stopReason && ctx.now() >= deadline) stopReason = 'time budget exhausted';
    if (stopReason) {
      results.push({ userId, status: 'skipped', detail: stopReason });
      continue;
    }
    const outcome = await verifyOne(userId, { ...ctx, deadline });
    results.push(outcome);
    // A 403 means the bot lacks verifier rights; every other call would fail too.
    if (outcome.status === 'forbidden') stopReason = 'not attempted after 403';
  }
  return results;
}

const LABELS = {
  verified: '✅ verificado',
  failed: '❌ falhou',
  forbidden: '⛔ sem permissão de verificador',
  unknown: '❔ resultado desconhecido (sem resposta da Telegram)',
  skipped: '⏭️ não tentado',
};

export function formatReport(results) {
  const total = results.length;
  const verified = results.filter((r) => r.status === 'verified').length;
  const lines = [
    verified === total
      ? `Verificação concluída: ${total} de ${total} alvos verificados.`
      : `Verificação incompleta: ${verified} de ${total} alvos verificados.`,
  ];
  if (results.some((r) => r.status === 'forbidden')) {
    lines.push('A Telegram recusou com 403: a capacidade de verificador não está ativa para este bot.');
  }
  if (results.some((r) => r.status === 'unknown')) {
    lines.push('Para alvos com resultado desconhecido, a verificação pode ou não ter sido aplicada.');
  }
  lines.push('');
  for (const r of results) {
    const detail = r.detail ? `: ${r.detail.slice(0, MAX_DETAIL_LENGTH)}` : '';
    lines.push(`${r.userId} — ${LABELS[r.status]}${detail}`);
  }
  const text = lines.join('\n');
  return text.length <= MAX_TEXT_LENGTH ? text : `${text.slice(0, MAX_TEXT_LENGTH - 1)}…`;
}

/**
 * Handles one Telegram update. Returns the per-target results when the update
 * was an owner /verify command, otherwise null. Everything else is ignored
 * without an answer, so strangers cannot make the bot call Telegram.
 */
export async function handleUpdate(update, { config, telegram, now = Date.now, sleep = defaultSleep }) {
  const message = update?.message;
  if (!isOwnerCommand(message, config, now())) return null;

  const results = await verifyTargets({ config, telegram, now, sleep });
  await telegram.call('sendMessage', { chat_id: message.chat.id, text: formatReport(results) });
  return results;
}
