# verfyroyal

Bot institucional do Telegram para o fluxo oficial de verificação de terceiros.

Nenhum token ou ID real é armazenado no repositório.

## Uso

1. Os alvos configurados devem iniciar conversa privada com o bot e enviar `/start`.
2. Um dos dois owners envia `/verify` na conversa privada com o bot.
3. Se um owner usar `/verify` em grupo ou supergrupo, o bot não executa a verificação ali: responde com um botão para abrir a conversa privada.
4. O bot processa todos os owners e executivos configurados e só declara sucesso total quando todos forem concluídos.
5. Se algum alvo ainda não estiver acessível ao bot (`PEER_ID_INVALID`), o resultado orienta que ele envie `/start` e oferece ao owner um botão para compartilhar uma mensagem preparada com o alvo.
6. Se a permissão de verificador falhar durante o lote, o bot preserva no reporte quantos alvos já tinham sido verificados, quantos falharam e quantos ficaram sem tentativa.

## Variáveis do Railway

- `TELEGRAM_BOT_TOKEN`
- `VERIFICATION_OWNER_IDS`
- `VERIFICATION_EXECUTIVE_IDS`
- `LOG_LEVEL` (opcional; padrão `INFO`)

A branch de entrega é `product/final-verifier`.
