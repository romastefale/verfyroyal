# Operational invariants

This service fails closed. A running process is not proof that verification happened.

## What counts as success

Success exists only when `verifyUser` returns the literal boolean `true`.

On `main`, total success is reported only when every configured target returns `true` and failures equal zero. Any other outcome produces “Verificação incompleta” with counts.

On `product/`, verification is unit-based. A `"verified"` record is written only after `true`. Failed attempts (except capability-missing) leave no durable record. The owner inventory shows only successes.

## Failures that are expected and verifiable

- `BOT_VERIFIER_FORBIDDEN` (403): never treated as success. On `main` stops the batch; on `product/` recorded as `capability_missing`. Remedy: enable verifier capability on Telegram, then retry.
- `429 Too Many Requests`: one retry using `retry_after`. Second failure remains failure.
- Invalid token or bot identity: startup aborts on `getMe`. Remedy: fix `TELEGRAM_BOT_TOKEN`.
- Existing webhook: startup calls `deleteWebhook` (`drop_pending_updates=false`) and requires `true`.
- Invalid target configuration: startup aborts. Exactly two distinct owner IDs required.
- Partial failure (`main`): operator receives incomplete result with counts.
- Transient/rejection failures (`product/`): operator receives classified message; nothing is persisted except successes and capability-missing.

## Why these checks exist

The dangerous failure mode is false success. Therefore `verifyUser` is accepted only on literal `true` and success is never inferred from the absence of an error.

The code prioritises preventing false success over keeping a full durable history of failures. On the delivered `product/` line the operator mostly sees successes. A green process remains insufficient proof.
