# Operational invariants

This service is designed to avoid reporting success when verification did not fully occur. A running process by itself is not considered sufficient evidence that verification succeeded. The service is built in native Node.js and relies on robust asynchronous checks.

## What counts as success

A verification is treated as successful only when the Bot API `verifyUser` method returns the literal value `true`.

A `/verify` command is reported as fully successful only when every configured target returns `true`. If any target fails, the operator receives an incomplete result together with the counts of successes and failures. The underlying implementation never coerces or fakes a success payload.

## Failures that are expected and verifiable

- `BOT_VERIFIER_FORBIDDEN` (403): the bot does not yet have verifier capability. This is reported clearly to the operator and is not treated as success. Remedy: obtain the capability on the Telegram side and retry.
- `429 Too Many Requests`: the service handles the rate limit seamlessly. It reads the `retry_after` parameter, pauses the exact asynchronous thread without blocking the web health-check server, and retries the target to guarantee completion.
- Invalid token or bot identity: startup stops at `getMe`. The error is logged and the worker halts. Remedy: correct `TELEGRAM_BOT_TOKEN`.
- Existing webhook: startup removes it with `deleteWebhook` so that `getUpdates` (long-polling) can be used cleanly.
- Invalid or incomplete target configuration: startup stops before any user is contacted. Exactly two distinct owner IDs are required to operate securely.
- Partial failure: the operator is informed with exact success and failure counts. No partial result is masked as a complete success.
- Network instability: The bot's long-polling loop automatically recovers and reconnects after 2 seconds upon receiving an unexpected network or API error, preventing crashes.

## Why these checks exist

The main risk is reporting success when verification did not actually complete. The checks above exist to keep that distinction clear and ensure data integrity.

The service focuses on not claiming success incorrectly. The Node.js worker isolates the Telegram polling layer from the Express web server, guaranteeing that health checks pass (e.g. on Railway) while the background worker safely handles network or API failure modes independently.
