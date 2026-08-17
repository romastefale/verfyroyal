# verfyroyal

Bot institucional do Telegram preparado para o fluxo oficial de verificação de terceiros.

## Variáveis do Railway

- `TELEGRAM_BOT_TOKEN` — token do bot. Deve permanecer apenas no ambiente seguro do Railway.
- `VERIFICATION_OWNER_IDS` — exatamente os dois IDs dos co-owners, separados por vírgula. Os dois são alvos da verificação e os únicos autorizados a executar `/verify`.
- `VERIFICATION_EXECUTIVE_IDS` — IDs dos executivos que também receberão a verificação, separados por vírgula.
- `LOG_LEVEL` — opcional; padrão `INFO`.

Nenhum token ou ID real é armazenado no repositório.

## Funcionamento

O serviço usa long polling da Bot API oficial. Ao iniciar, confirma o token com `getMe` e remove eventual webhook anterior para deixar `getUpdates` como único modo de recebimento de updates.

Antes de verificar qualquer pessoa, o bot confirma com `getChat` que todos os owners e executivos configurados estão acessíveis como usuários privados. Para garantir isso, cada alvo deve ter aberto uma conversa com o bot (por exemplo, enviando `/start`) antes da primeira execução de `/verify`.

Quando um dos dois owners envia `/verify`, o bot:

1. valida todos os alvos antes de modificar qualquer verificação;
2. chama o método oficial `verifyUser` para cada owner e executivo único;
3. trata apenas o retorno literal `True` da Bot API como sucesso;
4. só declara sucesso quando todos os alvos foram processados com sucesso.

O bot não envia `custom_description`. Assim, utiliza a descrição padrão configurada pelo Telegram para a organização verificadora e não depende da permissão opcional de descrição individual.

## Execução local

```bash
python main.py
```

## Testes

```bash
python -m unittest discover -s tests -v
```

## Railway

O repositório inclui `Dockerfile` e `railway.toml`. A branch de entrega atual é `product/final-verifier`. Configure as três variáveis reais no serviço Railway antes do primeiro deploy dessa branch.
