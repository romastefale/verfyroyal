# VerfyRoyal

Single-owner Telegram bot that applies Telegram's official third-party verification (Bot API `verifyUser`, added in Bot API 8.2) to a fixed list of users when its owner sends `/verify`.

It is a webhook bot with no background process and no dependencies. Requests are handled by a Web-standard `Request -> Response` handler (`src/handler.js`). `server.js` adapts that handler to Node for Railway, where the service can run in **Serverless** mode and sleep while idle.

## Prerequisite on Telegram's side

`verifyUser` only works for a bot that Telegram has approved as a third-party verifier. Per [telegram.org/verify](https://telegram.org/verify), the organization needs an official bot verified by Telegram and then applies through @VerifyBot. Until then, every call answers 403 and the bot reports that verifier rights are missing. No code change is needed once rights are granted.

## Variables

| Variable | Required | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | Bot token. |
| `TELEGRAM_WEBHOOK_SECRET` | yes | 32-256 characters of `A-Z a-z 0-9 _ -`, e.g. `openssl rand -hex 32`. Telegram sends it on every delivery; other requests are rejected. |
| `VERIFICATION_OWNER_ID` | yes | The single Telegram user ID allowed to run `/verify`. The owner is always the first target. |
| `VERIFICATION_TARGET_IDS` | no | Extra user IDs to verify, comma-separated. |
| `VERIFICATION_CUSTOM_DESCRIPTION` | no | Up to 70 characters, sent as `custom_description`. Leave empty unless Telegram allows custom descriptions for your organization. |
| `WEBHOOK_BASE_URL` | for `set-webhook` only | Public `https://` URL of the deployment. |

No token or ID is stored in the repository.

## Endpoints

- `POST /telegram/webhook`: Telegram updates. Needs the secret header.
- `GET /health`: `200` when configuration is valid, `503` otherwise. Use it as the Railway health check path.

## Deploy on Railway

1. Deploy the repository; Railway runs `npm start`.
2. Set the variables above in the service's **Variables** tab.
3. Generate a public domain, set the health check path to `/health`, and optionally enable **Serverless** in the service settings.
4. Register the webhook once (again whenever the domain or secret changes):

   ```bash
   WEBHOOK_BASE_URL=https://<your-domain> npm run set-webhook
   ```

   With the same variables loaded, this calls `setWebhook` with the secret, `allowed_updates: ["message"]`, `max_connections: 1` and `drop_pending_updates: true`, then prints `getWebhookInfo`.

## Local development

```bash
cp .env.example .env   # fill it in
npm run dev
npm test
```

Telegram can only deliver to a public HTTPS URL, so local runs need a tunnel to receive real updates. The tests use a fake Telegram and need no network.

See [OPERATIONS.md](OPERATIONS.md) for exact behaviour and failure handling.
