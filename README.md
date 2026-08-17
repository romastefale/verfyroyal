# verfyroyal

Bot institucional do Telegram para o fluxo oficial de verificação de terceiros, com operação owner-centric.

## Fluxo

- `/start` apenas diagnostica e roteia o estado do owner; nunca verifica terceiros.
- `/verifyme` executa somente a auto-verificação do owner autenticado pela própria mensagem.
- Um terceiro precisa primeiro abrir a conversa privada com o bot e enviar `/start`; apenas essa interação o torna elegível para seguir ao fluxo de confirmação.
- `/verify <user_id>` prepara uma única verificação de terceiro e exige confirmação explícita antes de chamar `verifyUser`.
- Sucesso existe somente quando `verifyUser` retorna literalmente `True`.
- A lista `verified` é derivada exclusivamente de sucessos persistidos; pendentes permanecem separados.

## Estados de `/start`

- **Estado A** — capacidade do bot observada como `missing`.
- **Estado B** — owner ainda não registrado como verificado; nenhuma verificação de terceiro é executada.
- **Estado C** — owner verificado; mostra o inventário de sucessos reais e a quantidade de pendentes configurados.

## Variáveis

- `TELEGRAM_BOT_TOKEN`
- `VERIFICATION_OWNER_IDS` — exatamente dois IDs de owners.
- `VERIFICATION_EXECUTIVE_IDS` — IDs configurados como inventário/pending; não são verificados em lote.
- `VERIFIER_STATE_PATH` — opcional; padrão `/data/verfyroyal-events.jsonl`.
- `LOG_LEVEL` — opcional; padrão `INFO`.

O arquivo de estado é append-only e registra evidências produzidas em runtime. Status `verified` nasce somente de sucesso real de `verifyUser`; interações e pendências nunca são promovidas a sucesso. Nenhum token ou ID real é armazenado no repositório.

## Persistência

Em Railway, monte armazenamento persistente em `/data`. O estado dos owners e o inventário de verificados dependem desse registro sobreviver a restart/redeploy.

## Testes

```bash
python -m unittest discover -s tests -v
```

A suíte é baseada em contratos de comportamento: `/start` não verifica terceiros, owner não verificado não autoriza `/verify`, um comando afeta no máximo um alvo, alvo sem `/start` continua inacessível, `PEER_ID_INVALID` vira `target_inaccessible`, pendentes não entram em `verified` e nenhum sucesso é persistido sem `True` literal.
