# VerfyRoyal

Minimal Telegram institutional verifier worker built for the official third-party verification flow. This project has been migrated to a native Node.js architecture to run cleanly as a Web Service on platforms like Railway.

## Runtime variables

- `TELEGRAM_BOT_TOKEN` — Telegram bot token. Keep it secret in Railway.
- `VERIFICATION_OWNER_IDS` — exactly two co-owner Telegram user IDs, comma-separated. Both owners are verification targets and the only accounts authorized to trigger `/verify`.
- `VERIFICATION_EXECUTIVE_IDS` — optional comma-separated Telegram user IDs for the authorized executive targets.

No token or user ID is stored in the repository.

## Operation

The worker uses an Express web server for cloud health checks, while long-polling Telegram in the background. It ignores every command except `/verify`.

When either configured owner sends `/verify`, the worker calls the official Bot API `verifyUser` method once for every unique configured owner and executive target. Rate limits (HTTP 429) are handled securely.

Before Telegram enables the bot's verifier capability, Telegram can reject the operation with a 403 Forbidden error. The same deployment is ready to work after that capability is enabled; no code change is required.

## Run Locally

```bash
npm install
npm run dev
```

## Railway Deployment

The repository is built natively for Railway. 

1. Deploy the project from your GitHub repo.
2. In your Railway service settings, add the required runtime variables in the **Variables** tab.
3. The platform will automatically run `npm run start` and map the internal port.
