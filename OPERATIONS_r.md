# Operational invariants

This service intentionally fails closed. A green process is not treated as proof that verification happened.

## What counts as success

A verification is accepted as success only when the Bot API `verifyUser` call returns the literal boolean `true`. Any other value or shape is treated as failure.

On the `main` branch, a `/verify` execution is reported as total success only when every configured unique target returns `true`. The success message is emitted exclusively when the number of successes equals the total number of targets and the number of failures is zero. Any `false`, protocol error, network error, malformed response or failed retry produces an incomplete result with explicit success and failure counts.

On the `product/` branches, verification is unit-based (one target at a time, with confirmation). A `"verified"` record is written to the persistent store only after `verifyUser` returns `true`. Failed attempts, except for capability-missing, leave no durable record. The inventory shown to the owner is built solely from successful records.

## Failures that are expected and verifiable

- `BOT_VERIFIER_FORBIDDEN` (403): the bot does not currently have Telegram’s verifier capability. This is never converted into a product success. On `main` it stops the current batch; on `product/` it is recorded as `capability_missing`. Remedy: confirm the verifier authorization on the Telegram side, then retry the same deployed code.
- `429 Too Many Requests`: the service uses Telegram’s `retry_after` once for the affected target. A second failure remains a failure and is reported as such.
- Invalid token / invalid bot identity: startup stops during `getMe`. The identity must include `is_bot: true` and a numeric id. Remedy: correct `TELEGRAM_BOT_TOKEN` in Railway.
- Existing webhook: startup calls `deleteWebhook` with `drop_pending_updates=false` because this product deliberately uses `getUpdates`. The call must return `true`. Telegram documents the two update modes as mutually exclusive.
- Invalid or missing target configuration: startup fails before contacting users. Exactly two distinct owner IDs are required. Remedy: correct `VERIFICATION_OWNER_IDS` / `VERIFICATION_EXECUTIVE_IDS`.
- Partial target failure (`main` only): the operator receives `Verificação incompleta` together with success/failure counts. Remedy: inspect Telegram/runtime logs and retry only after the underlying cause is resolved.
- Transient or rejection failures (`product/` only): the operator receives a classified error message. These events are not written to the persistent store. Only successful verifications and capability-missing events are retained.

## Why these checks exist

The dangerous failure mode for this product is not a visible exception; it is a false success. For that reason, `verifyUser` is accepted only on literal `true`, response shapes are validated (more strictly on `main`), and success is never inferred from the mere absence of an error.

The implementation prioritises preventing false success over complete durable evidence of every failed attempt. On the delivered `product/` line the operator sees mainly successes and selected error messages; the full history of failed tries is not retained. The process itself continues running after most protocol errors, so a green process remains insufficient proof that verification occurred.
