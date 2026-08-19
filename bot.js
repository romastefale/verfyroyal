class TelegramAPIError extends Error {
  constructor(description, errorCode = null, parameters = {}) {
    super(description);
    this.name = 'TelegramAPIError';
    this.description = description;
    this.errorCode = errorCode;
    this.parameters = parameters;
  }
}

export function loadSettings(env = process.env) {
  const token = (env.TELEGRAM_BOT_TOKEN || '').trim();
  if (!token) throw new Error('TELEGRAM_BOT_TOKEN is required');

  const parseIds = (raw, name, required) => {
    raw = (raw || '').trim();
    if (!raw) {
      if (required) throw new Error(`${name} is required`);
      return [];
    }
    const values = [];
    for (let part of raw.split(',')) {
      part = part.trim();
      if (!part) continue;
      const val = parseInt(part, 10);
      if (isNaN(val) || val <= 0) {
        throw new Error(`${name} must contain positive integer Telegram user IDs`);
      }
      values.push(val);
    }
    if (required && values.length === 0) {
      throw new Error(`${name} is required`);
    }
    return [...new Set(values)];
  };

  const ownerIds = parseIds(env.VERIFICATION_OWNER_IDS, 'VERIFICATION_OWNER_IDS', true);
  if (ownerIds.length !== 2) {
    throw new Error('VERIFICATION_OWNER_IDS must contain exactly two distinct owner IDs');
  }

  const executiveIds = parseIds(env.VERIFICATION_EXECUTIVE_IDS, 'VERIFICATION_EXECUTIVE_IDS', false);

  const targets = [...new Set([...ownerIds, ...executiveIds])];

  return {
    token,
    ownerIds,
    executiveIds,
    targets
  };
}

export class TelegramBotAPI {
  constructor(token) {
    this.baseUrl = `https://api.telegram.org/bot${token}`;
  }

  async call(method, payload = {}) {
    try {
      const res = await fetch(`${this.baseUrl}/${method}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json().catch(() => null);
      
      if (!data || typeof data !== 'object') {
        throw new TelegramAPIError('Telegram returned invalid response shape', res.status);
      }
      
      if (data.ok !== true) {
        throw new TelegramAPIError(
          data.description || 'Telegram API request failed',
          data.error_code || res.status,
          data.parameters || {}
        );
      }
      return data.result;
    } catch (err) {
      if (err instanceof TelegramAPIError) throw err;
      throw new TelegramAPIError(`Network error: ${err.message}`);
    }
  }

  async getMe() {
    const result = await this.call('getMe');
    if (!result?.is_bot || typeof result?.id !== 'number') {
      throw new TelegramAPIError('getMe did not return a valid bot identity');
    }
    return result;
  }

  async deleteWebhook() {
    const result = await this.call('deleteWebhook', { drop_pending_updates: false });
    if (result !== true) {
      throw new TelegramAPIError('deleteWebhook did not return True');
    }
  }

  async getUpdates(offset) {
    const payload = { timeout: 30, allowed_updates: ['message'] };
    if (offset !== null && offset !== undefined) payload.offset = offset;
    
    const result = await this.call('getUpdates', payload);
    if (!Array.isArray(result)) {
      throw new TelegramAPIError('getUpdates returned an invalid result');
    }
    return result;
  }

  async sendMessage(chatId, text) {
    const result = await this.call('sendMessage', { chat_id: chatId, text });
    if (!result || typeof result.message_id !== 'number') {
      throw new TelegramAPIError('sendMessage did not return a valid Message');
    }
  }

  async verifyUser(userId) {
    const result = await this.call('verifyUser', { user_id: userId });
    return result === true;
  }
}

async function verifyTargets(api, targets) {
  let succeeded = 0;
  let failed = 0;
  let permissionMissing = false;

  for (const userId of targets) {
    try {
      if (await api.verifyUser(userId)) {
        succeeded++;
      } else {
        failed++;
      }
    } catch (exc) {
      if (exc.errorCode === 403 && (exc.description || '').toUpperCase().includes('BOT_VERIFIER_FORBIDDEN')) {
        permissionMissing = true;
        failed++;
        break;
      }
      if (exc.errorCode === 429) {
        const retryAfter = exc.parameters?.retry_after || 1;
        await new Promise(r => setTimeout(r, retryAfter * 1000));
        try {
          if (await api.verifyUser(userId)) succeeded++;
          else failed++;
        } catch (e) {
          failed++;
        }
      } else {
        failed++;
      }
    }
  }
  return { succeeded, failed, permissionMissing };
}

async function handleMessage(api, settings, message) {
  const text = message?.text;
  const senderId = message?.from?.id;
  const chatId = message?.chat?.id;

  if (typeof text !== 'string' || typeof senderId !== 'number' || typeof chatId !== 'number') return;
  
  const cmd = text.trim().split(/\s+/)[0].split('@')[0].toLowerCase();
  if (cmd !== '/verify') return;

  if (!settings.ownerIds.includes(senderId)) {
    await api.sendMessage(chatId, 'Ação não autorizada.');
    return;
  }

  const { succeeded, failed, permissionMissing } = await verifyTargets(api, settings.targets);

  if (permissionMissing) {
    await api.sendMessage(chatId, 'A capacidade oficial de verificador ainda não está ativa para este bot.');
    return;
  }

  const total = settings.targets.length;
  if (succeeded === total && failed === 0) {
    await api.sendMessage(chatId, `Verificação concluída com sucesso para todos os ${total} alvos.`);
  } else {
    await api.sendMessage(chatId, `Verificação incompleta. Sucesso: ${succeeded}. Falhas: ${failed}. Total: ${total}.`);
  }
}

export async function startBotWorker(settings) {
  const api = new TelegramBotAPI(settings.token);
  
  try {
    const identity = await api.getMe();
    console.log(`[Bot] Identity confirmed. Bot ID: ${identity.id}`);
    await api.deleteWebhook();
  } catch (err) {
    console.error('[Bot] Failed to prepare:', err.message);
    return;
  }

  console.log(`[Bot] Verifier worker started. Targets: ${settings.targets.length}`);
  
  let offset = null;
  while (true) {
    try {
      const updates = await api.getUpdates(offset);
      for (const update of updates) {
        if (typeof update.update_id !== 'number') {
          throw new TelegramAPIError('Update without valid update_id');
        }
        offset = update.update_id + 1;
        
        if (update.message) {
          await handleMessage(api, settings, update.message).catch(err => {
            console.error('[Bot] Error handling message:', err.message);
          });
        }
      }
    } catch (err) {
      console.warn('[Bot] Telegram API error:', err.message);
      await new Promise(r => setTimeout(r, 2000));
    }
  }
}
