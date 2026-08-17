# verfyroyal

Bot institucional do Telegram para verificação oficial de contas.

## Comandos

- `/start`
- `/verifyme`
- `/verify <user_id>`

## Variáveis

- `TELEGRAM_BOT_TOKEN`
- `VERIFICATION_OWNER_IDS`
- `VERIFICATION_EXECUTIVE_IDS`
- `VERIFIER_STATE_PATH` — opcional; padrão `/data/verfyroyal-events.jsonl`
- `LOG_LEVEL` — opcional; padrão `INFO`

## Execução

```bash
python main.py
```

No Railway, o serviço usa a branch `product/final-verifier` e armazenamento persistente montado em `/data`.
