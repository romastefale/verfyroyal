# Operational invariants

This service is designed to avoid reporting success when verification did not fully occur. A running process by itself is not considered sufficient evidence that verification succeeded.

## What counts as success

A verification is treated as successful only when the Bot API `verifyUser` method returns the literal value `true`.

On the `main` branch, a `/verify` command is reported as fully successful only when every configured target returns `true`. If any target fails, the operator receives an incomplete result together with the counts of successes and failures.

On the `product/` branches, verification is performed one target at a time. A successful result is recorded only after `verifyUser` returns `true`. Other outcomes are reported to the operator but are not stored as verified records.

## Failures that are expected and verifiable

- `BOT_VERIFIER_FORBIDDEN` (403): the bot does not yet have verifier capability. This is reported clearly and is not treated as success. Remedy: obtain the capability on the Telegram side and retry.
- `429 Too Many Requests`: the service respects `retry_after` once. A further failure is reported as failure.
- Invalid token or bot identity: startup stops at `getMe`. Remedy: correct `TELEGRAM_BOT_TOKEN`.
- Existing webhook: startup removes it with `deleteWebhook` so that `getUpdates` can be used cleanly.
- Invalid or incomplete target configuration: startup stops before any user is contacted. Exactly two distinct owner IDs are required.
- Partial failure on `main`: the operator is informed with success and failure counts.
- Other failures on `product/`: the operator receives a clear message; only confirmed successes are kept in the store.

## Why these checks exist

The main risk is reporting success when verification did not actually complete. The checks above exist to keep that distinction clear.

The service focuses on not claiming success incorrectly. On the `product/` line, the record kept for the operator emphasises confirmed successes.
