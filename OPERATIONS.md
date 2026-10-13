# Operational behaviour

The bot never reports a verification it cannot prove. A user counts as verified only when `verifyUser` answers with the literal value `true`.

## Who can trigger a run

A run starts only for a message that meets all of these conditions:

- it arrives on `/telegram/webhook` with the correct `X-Telegram-Bot-Api-Secret-Token` (checked in constant time; otherwise `401`);
- it is a `message` (edits and other update types are ignored);
- it is a private chat with the owner, sent by the owner, who is not a bot;
- its first word is `/verify` (an `@BotName` suffix is allowed);
- it is at most 5 minutes old, so a late delivery cannot start an unexpected run.

Anything else gets no reply and causes no Telegram call.

## A run

Targets are the owner, then `VERIFICATION_TARGET_IDS`, deduplicated, verified one at a time. Each result is one of:

| Result | Cause |
| --- | --- |
| verified | `verifyUser` returned `true`. |
| failed | Telegram answered with an error (e.g. 400), a non-`true` result, or a 429 that could not be retried. |
| no verifier rights | Telegram answered 403. The run stops: every other call would fail the same way. |
| unknown | No answer (network error or 10 s timeout). Verification may or may not have been applied. |
| not attempted | Skipped after a 403, or because the run's time budget ran out. |

The owner receives one message with the count and a line per target, including Telegram's error description.

## Limits

- **429:** waits `retry_after` (1 s if absent) and retries, at most twice per target, only when the wait is 10 s or less and fits in the time budget. Otherwise the target is reported as failed.
- **Time budget:** 25 s per run, so the webhook answers before Telegram gives up on it. Targets not started in time are reported as not attempted.
- **Request body:** 1 MB maximum (`413` above that).

## Delivery and duplicates

After the secret check, the webhook always answers `200`, even when a run fails. A non-2xx answer would make Telegram redeliver the update and run the verification again. `set-webhook` sets `max_connections: 1`, so updates are delivered one at a time and two runs never overlap. Re-running `/verify` simply re-applies the same verification.

## Configuration errors

Configuration is validated at start-up. If it is invalid, the error is logged (without secrets), `/health` answers `503` and the webhook answers `503` without doing anything. A deploy with bad variables is therefore not shown as healthy.

## Logs

Each run logs one line with the update ID and the result per target. The token is never logged: errors without an answer from Telegram keep only the error name, because some runtimes include the request URL, and with it the token, in error messages.
