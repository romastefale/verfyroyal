# verfyroyal

Bot Telegram para verificação oficial de contas.

## Comandos

- `/start` — inicia a conversa e mostra o estado atual ao owner
- `/verifyme` — owner verifica a própria conta
- `/verify <user_id>` — prepara a verificação de uma conta e pede confirmação

A pessoa alvo precisa ter enviado `/start` neste bot antes da verificação.

## Variáveis

- `TELEGRAM_BOT_TOKEN`
- `VERIFICATION_OWNER_IDS` — exatamente dois IDs de owners, separados por vírgula
- `VERIFICATION_EXECUTIVE_IDS` — opcional; IDs acompanhados como pendentes
- `VERIFIER_STATE_PATH` — opcional; padrão `/data/verfyroyal-events.jsonl`
- `LOG_LEVEL` — opcional; padrão `INFO`

## Execução

```bash
python main.py
```
