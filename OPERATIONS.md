# Operational invariants

This service intentionally fails closed. A green process is not treated as proof that verification happened.

## What counts as success

A `/verify` execution is reported as total success only when every configured unique target returns the Bot API `verifyUser` result `true`. Any `false`, protocol error, network error, malformed response or failed retry produces an incomplete result instead.

## Failures that are expected and verifiable

- `BOT_VERIFIER_FORBIDDEN` (403): the bot does not currently have Telegram's verifier capability. This is not converted into a product success. Remedy: confirm the verifier authorization on the Telegram side, then retry the same deployed code.
- `429 Too Many Requests`: the service uses Telegram's `retry_after` once for the affected target. A second failure remains a failure and is reported as such.
- Invalid token / invalid bot identity: startup stops during `getMe`. Remedy: correct `TELEGRAM_BOT_TOKEN` in Railway.
- Existing webhook: startup calls `deleteWebhook` with `drop_pending_updates=false` because this product deliberately uses `getUpdates`; Telegram documents these update modes as mutually exclusive.
- Invalid or missing target configuration: startup fails before contacting users. Remedy: correct `VERIFICATION_OWNER_IDS` / `VERIFICATION_EXECUTIVE_IDS`.
- Partial target failure: the operator receives `Verificação incompleta` with success/failure counts. Remedy: inspect Telegram/runtime logs and retry only after the underlying cause is resolved.

## Why these checks exist

The dangerous failure mode for this product is not a visible exception; it is a false success. For that reason, response shapes are validated strictly, `verifyUser` succeeds only on literal `true`, and malformed `getUpdates` results are errors rather than silently becoming an empty update list.
