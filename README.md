# verfyroyal

Minimal Telegram institutional verifier worker built for the official third-party verification flow.

## Runtime variables

- `TELEGRAM_BOT_TOKEN` — Telegram bot token. Keep it secret in Railway.
- `VERIFICATION_OWNER_IDS` — exactly two co-owner Telegram user IDs, comma-separated. Both owners are verification targets and the only accounts authorized to trigger `/verify`.
- `VERIFICATION_EXECUTIVE_IDS` — optional comma-separated Telegram user IDs for the authorized executive targets.
- `LOG_LEVEL` — optional, defaults to `INFO`.

No token or user ID is stored in the repository.

## Operation

The worker long-polls Telegram and ignores every command except `/verify`.

When either configured owner sends `/verify`, the worker calls the official Bot API `verifyUser` method once for every unique configured owner and executive target.

No custom verification description is sent. The verifier organization's Telegram-configured/default description is therefore used, avoiding reliance on optional per-account description customization.

Before Telegram enables the bot's verifier capability, Telegram can reject the operation with the verifier permission error. The same deployment is ready to work after that capability is enabled; no code change is required.

## Run

```bash
python main.py
```

## Test

```bash
python -m unittest discover -s tests -v
```

## Railway

The repository includes a `Dockerfile` and `railway.toml`. Configure the runtime variables in the Railway service and deploy from `main`.
