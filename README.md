# verfyroyal

Bot institucional do Telegram preparado para o fluxo oficial de verificação de terceiros.

## Variáveis do Railway

- `TELEGRAM_BOT_TOKEN` — token do bot. Deve permanecer apenas no ambiente seguro do Railway.
- `VERIFICATION_OWNER_IDS` — exatamente os dois IDs dos co-owners, separados por vírgula. Os dois são alvos da verificação e os únicos autorizados a executar `/verify`.
- `VERIFICATION_EXECUTIVE_IDS` — IDs dos executivos que também receberão a verificação, separados por vírgula.
- `LOG_LEVEL` — opcional; padrão `INFO`.

Nenhum token ou ID real é armazenado no repositório.

## Funcionamento

O serviço usa a Bot API oficial do Telegram por long polling. Ao iniciar, valida o token com `getMe` e remove eventual webhook anterior para usar `getUpdates` como único modo de recebimento de updates.

Os owners e executivos configurados podem enviar `/start` ao bot para estabelecer o contato direto necessário quando o Telegram ainda não reconhecer o usuário como um peer acessível ao bot.

Quando um dos dois owners envia `/verify`, o serviço chama o método oficial `verifyUser` para cada owner e executivo único. Cada alvo é tratado individualmente, erros transitórios recebem novas tentativas limitadas e somente o retorno literal `True` é considerado sucesso. O bot só declara sucesso total quando todos os alvos tiverem sido verificados com sucesso.

Se o Telegram responder `PEER_ID_INVALID` para algum alvo, esse alvo permanece explicitamente como falha e pode enviar `/start` ao bot antes de uma nova tentativa. Se a capacidade de verificador ainda não estiver habilitada, `BOT_VERIFIER_FORBIDDEN` interrompe a execução sem ser tratado como sucesso.

O bot não envia `custom_description`; portanto, usa a descrição padrão definida para a organização verificadora e não depende da permissão opcional de descrição individual.

## Execução local

```bash
python main.py
```

## Testes

```bash
python -m unittest discover -s tests -v
```

## Railway

O repositório inclui `Dockerfile` e `railway.toml`. A branch de entrega é `product/final-verifier`. Configure as variáveis reais no serviço Railway antes do primeiro deploy dessa branch.
