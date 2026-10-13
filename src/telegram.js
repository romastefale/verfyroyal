export class TelegramError extends Error {
  /**
   * @param {string} method
   * @param {string} description Telegram's description; never contains the token.
   * @param {number|null} errorCode null when no answer was received, so the
   *   outcome of the call is unknown.
   * @param {number|null} retryAfter seconds, from a 429 answer.
   */
  constructor(method, description, errorCode, retryAfter = null) {
    super(`${method}: ${description}`);
    this.name = 'TelegramError';
    this.method = method;
    this.description = description;
    this.errorCode = errorCode;
    this.retryAfter = retryAfter;
  }
}

export function createTelegramClient(token, { fetch = globalThis.fetch, timeoutMs = 10_000 } = {}) {
  const baseUrl = `https://api.telegram.org/bot${token}/`;

  async function call(method, params = {}) {
    let response;
    try {
      response = await fetch(baseUrl + method, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(params),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (err) {
      // Only the error name: messages from some runtimes include the URL,
      // which contains the token.
      throw new TelegramError(method, `no response (${err?.name ?? 'Error'})`, null);
    }

    let data = null;
    try {
      data = await response.json();
    } catch {
      // handled below
    }
    if (!data || typeof data !== 'object') {
      throw new TelegramError(method, `invalid response (HTTP ${response.status})`, response.status);
    }
    if (data.ok !== true) {
      const retryAfter = Number.isFinite(data.parameters?.retry_after) ? data.parameters.retry_after : null;
      throw new TelegramError(
        method,
        String(data.description ?? 'request failed'),
        Number.isInteger(data.error_code) ? data.error_code : response.status,
        retryAfter,
      );
    }
    return data.result;
  }

  return { call };
}
