const TOKEN_RE = /^\d+:[A-Za-z0-9_-]+$/;
const USER_ID_RE = /^[1-9]\d{0,15}$/;
// Telegram accepts 1-256 characters from this set; the lower bound is ours.
const WEBHOOK_SECRET_RE = /^[A-Za-z0-9_-]{32,256}$/;
export const MAX_CUSTOM_DESCRIPTION_LENGTH = 70;

function parseUserId(raw, name) {
  const value = raw.trim();
  const id = Number(value);
  if (!USER_ID_RE.test(value) || !Number.isSafeInteger(id)) {
    throw new Error(`${name} must contain positive integer Telegram user IDs`);
  }
  return id;
}

export function loadConfig(env) {
  const token = (env.TELEGRAM_BOT_TOKEN ?? '').trim();
  if (!TOKEN_RE.test(token)) {
    throw new Error('TELEGRAM_BOT_TOKEN is missing or malformed');
  }

  const webhookSecret = (env.TELEGRAM_WEBHOOK_SECRET ?? '').trim();
  if (!WEBHOOK_SECRET_RE.test(webhookSecret)) {
    throw new Error('TELEGRAM_WEBHOOK_SECRET must be 32-256 characters of A-Z, a-z, 0-9, _ or -');
  }

  const ownerRaw = (env.VERIFICATION_OWNER_ID ?? '').trim();
  if (!ownerRaw) throw new Error('VERIFICATION_OWNER_ID is required');
  const ownerId = parseUserId(ownerRaw, 'VERIFICATION_OWNER_ID');

  const extraIds = (env.VERIFICATION_TARGET_IDS ?? '')
    .split(',')
    .filter((part) => part.trim() !== '')
    .map((part) => parseUserId(part, 'VERIFICATION_TARGET_IDS'));

  const customDescription = (env.VERIFICATION_CUSTOM_DESCRIPTION ?? '').trim();
  if (Array.from(customDescription).length > MAX_CUSTOM_DESCRIPTION_LENGTH) {
    throw new Error(`VERIFICATION_CUSTOM_DESCRIPTION must be at most ${MAX_CUSTOM_DESCRIPTION_LENGTH} characters`);
  }

  return Object.freeze({
    token,
    webhookSecret,
    ownerId,
    // The owner is always verified first; duplicates are dropped.
    targetIds: Object.freeze([...new Set([ownerId, ...extraIds])]),
    customDescription: customDescription || null,
  });
}
