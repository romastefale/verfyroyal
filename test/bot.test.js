import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config.js';
import { createHandler, WEBHOOK_PATH, HEALTH_PATH } from '../src/handler.js';

const OWNER = 1001;
const SECRET = 'a'.repeat(32);
const NOW_MS = 1_700_000_000_000;
const ENV = {
  TELEGRAM_BOT_TOKEN: '123:abc-DEF_9',
  TELEGRAM_WEBHOOK_SECRET: SECRET,
  VERIFICATION_OWNER_ID: String(OWNER),
  VERIFICATION_TARGET_IDS: '2002, 3003',
};

function fakeTelegram(behaviour = {}) {
  const calls = [];
  const fetch = async (url, init) => {
    const method = url.split('/').pop();
    const params = JSON.parse(init.body);
    calls.push({ method, params });
    const answer = behaviour[method]?.(params, calls) ?? { ok: true, result: method === 'sendMessage' ? { message_id: 1 } : true };
    if (answer instanceof Error) throw answer;
    return new Response(JSON.stringify(answer), { status: answer.ok ? 200 : answer.error_code });
  };
  return { fetch, calls };
}

function setup(behaviour, env = ENV) {
  const telegram = fakeTelegram(behaviour);
  const sleeps = [];
  const handle = createHandler({
    env,
    fetch: telegram.fetch,
    now: () => NOW_MS,
    sleep: async (ms) => { sleeps.push(ms); },
    logger: { log() {}, error() {} },
  });
  return { handle, calls: telegram.calls, sleeps };
}

function message(overrides = {}) {
  return {
    message_id: 1,
    date: NOW_MS / 1000,
    text: '/verify',
    from: { id: OWNER, is_bot: false },
    chat: { id: OWNER, type: 'private' },
    ...overrides,
  };
}

function webhook(body, secret = SECRET) {
  return new Request(`https://bot.example${WEBHOOK_PATH}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-telegram-bot-api-secret-token': secret },
    body: JSON.stringify(body),
  });
}

const verifyCalls = (calls) => calls.filter((c) => c.method === 'verifyUser').map((c) => c.params.user_id);
const sentText = (calls) => calls.find((c) => c.method === 'sendMessage')?.params.text;

test('config: owner first, targets deduplicated', () => {
  const config = loadConfig({ ...ENV, VERIFICATION_TARGET_IDS: '2002,1001,2002' });
  assert.deepEqual(config.targetIds, [OWNER, 2002]);
  assert.equal(config.customDescription, null);
});

test('config: rejects bad values', () => {
  const bad = [
    { TELEGRAM_BOT_TOKEN: '' },
    { TELEGRAM_WEBHOOK_SECRET: 'short' },
    { TELEGRAM_WEBHOOK_SECRET: `${'a'.repeat(32)}!` },
    { VERIFICATION_OWNER_ID: '' },
    { VERIFICATION_OWNER_ID: '1,2' },
    { VERIFICATION_OWNER_ID: '12abc' },
    { VERIFICATION_TARGET_IDS: '-5' },
    { VERIFICATION_TARGET_IDS: '99999999999999999' },
    { VERIFICATION_CUSTOM_DESCRIPTION: 'x'.repeat(71) },
  ];
  for (const override of bad) {
    assert.throws(() => loadConfig({ ...ENV, ...override }), undefined, JSON.stringify(override));
  }
});

test('health reflects configuration', async () => {
  const ok = setup().handle(new Request(`https://bot.example${HEALTH_PATH}`));
  assert.equal((await ok).status, 200);
  const broken = setup({}, { ...ENV, VERIFICATION_OWNER_ID: '' }).handle(new Request(`https://bot.example${HEALTH_PATH}`));
  assert.equal((await broken).status, 503);
});

test('wrong or missing secret is rejected before any Telegram call', async () => {
  const { handle, calls } = setup();
  assert.equal((await handle(webhook({ update_id: 1, message: message() }, 'b'.repeat(32)))).status, 401);
  assert.equal((await handle(webhook({ update_id: 1, message: message() }, ''))).status, 401);
  assert.equal(calls.length, 0);
});

test('other paths and methods', async () => {
  const { handle } = setup();
  assert.equal((await handle(new Request('https://bot.example/'))).status, 404);
  assert.equal((await handle(new Request(`https://bot.example${WEBHOOK_PATH}`))).status, 405);
});

test('owner /verify verifies every target and reports success', async () => {
  const { handle, calls } = setup();
  const res = await handle(webhook({ update_id: 1, message: message({ text: '/verify@SomeBot extra' }) }));
  assert.equal(res.status, 200);
  assert.deepEqual(verifyCalls(calls), [OWNER, 2002, 3003]);
  assert.match(sentText(calls), /^Verificação concluída: 3 de 3/);
  assert.equal(calls.find((c) => c.method === 'sendMessage').params.chat_id, OWNER);
});

test('ignored without any Telegram call', async () => {
  const cases = [
    message({ from: { id: 9, is_bot: false }, chat: { id: 9, type: 'private' } }),
    message({ chat: { id: -100, type: 'group' } }),
    message({ text: '/start' }),
    message({ text: 'verify' }),
    message({ date: NOW_MS / 1000 - 301 }),
    message({ from: { id: OWNER, is_bot: true } }),
  ];
  for (const m of cases) {
    const { handle, calls } = setup();
    assert.equal((await handle(webhook({ update_id: 1, message: m }))).status, 200);
    assert.equal(calls.length, 0, JSON.stringify(m));
  }
  const { handle, calls } = setup();
  await handle(webhook({ update_id: 1, edited_message: message() }));
  assert.equal(calls.length, 0);
});

test('403 stops the run and is reported as missing verifier rights', async () => {
  const { handle, calls } = setup({
    verifyUser: () => ({ ok: false, error_code: 403, description: 'Forbidden: not enough rights' }),
  });
  await handle(webhook({ update_id: 1, message: message() }));
  assert.deepEqual(verifyCalls(calls), [OWNER]);
  const text = sentText(calls);
  assert.match(text, /^Verificação incompleta: 0 de 3/);
  assert.match(text, /403/);
  assert.match(text, /2002 — ⏭️ não tentado/);
});

test('429 is retried after retry_after, within limits', async () => {
  let attempts = 0;
  const { handle, calls, sleeps } = setup({
    verifyUser: (params) => (params.user_id === 2002 && attempts++ < 2
      ? { ok: false, error_code: 429, description: 'Too Many Requests', parameters: { retry_after: 3 } }
      : { ok: true, result: true }),
  });
  await handle(webhook({ update_id: 1, message: message() }));
  assert.deepEqual(sleeps, [3000, 3000]);
  assert.deepEqual(verifyCalls(calls), [OWNER, 2002, 2002, 2002, 3003]);
  assert.match(sentText(calls), /^Verificação concluída: 3 de 3/);
});

test('429 with a long retry_after fails without waiting', async () => {
  const { handle, sleeps, calls } = setup({
    verifyUser: () => ({ ok: false, error_code: 429, description: 'Too Many Requests', parameters: { retry_after: 60 } }),
  });
  await handle(webhook({ update_id: 1, message: message() }));
  assert.deepEqual(sleeps, []);
  assert.match(sentText(calls), /0 de 3/);
});

test('no answer from Telegram is reported as unknown, other errors as failed', async () => {
  const { handle, calls } = setup({
    verifyUser: (params) => {
      if (params.user_id === OWNER) return new TypeError('fetch failed https://api.telegram.org/bot123:abc-DEF_9/verifyUser');
      if (params.user_id === 2002) return { ok: false, error_code: 400, description: 'Bad Request: user not found' };
      return { ok: true, result: false };
    },
  });
  await handle(webhook({ update_id: 1, message: message() }));
  const text = sentText(calls);
  assert.match(text, /1001 — ❔ resultado desconhecido/);
  assert.match(text, /2002 — ❌ falhou: Bad Request: user not found/);
  assert.match(text, /3003 — ❌ falhou: Telegram did not return true/);
  assert.doesNotMatch(text, /abc-DEF_9/);
});

test('custom description is sent when configured', async () => {
  const { handle, calls } = setup({}, { ...ENV, VERIFICATION_CUSTOM_DESCRIPTION: 'Equipe oficial' });
  await handle(webhook({ update_id: 1, message: message() }));
  assert.ok(calls.filter((c) => c.method === 'verifyUser').every((c) => c.params.custom_description === 'Equipe oficial'));
});

test('a failing sendMessage still answers 200 so Telegram does not redeliver', async () => {
  const { handle, calls } = setup({
    sendMessage: () => ({ ok: false, error_code: 400, description: 'Bad Request: chat not found' }),
  });
  const res = await handle(webhook({ update_id: 1, message: message() }));
  assert.equal(res.status, 200);
  assert.equal(verifyCalls(calls).length, 3);
});

test('invalid JSON body is rejected', async () => {
  const { handle } = setup();
  const req = new Request(`https://bot.example${WEBHOOK_PATH}`, {
    method: 'POST',
    headers: { 'x-telegram-bot-api-secret-token': SECRET },
    body: '{not json',
  });
  assert.equal((await handle(req)).status, 400);
});
