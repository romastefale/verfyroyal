# verfyroyal

Bot institucional do Telegram para o fluxo oficial de verificação de terceiros.

## Variáveis de ambiente

- `TELEGRAM_BOT_TOKEN` — token do bot (apenas no ambiente seguro)
- `VERIFICATION_OWNER_IDS` — exatamente dois IDs dos co-owners, separados por vírgula
- `VERIFICATION_EXECUTIVE_IDS` — IDs dos executivos (opcional), separados por vírgula
- `LOG_LEVEL` — opcional (padrão: `INFO`)

Nenhum token ou ID real é armazenado no repositório.

## Uso

1. Os alvos configurados devem iniciar conversa com o bot (`/start`).
2. Um dos owners envia `/verify` em chat privado com o bot.
3. O bot processa a verificação dos alvos e responde com o resultado.

## Execução

```bash
python main.py