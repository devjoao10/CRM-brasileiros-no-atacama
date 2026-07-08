# Live export — workflows n8n da BIA (N8N-BIA-GUARDRAILS-02)

```text
Package ID:   N8N-BIA-GUARDRAILS-02
Source:       n8n live (https://n8n.crmbrasileirosnoatacama.cloud/) via n8n MCP oficial, read-only
Timestamp:    2026-07-08 14:43 (local)
Purpose:      snapshot de rollback ANTES de qualquer edição de guardrails
```

## Workflows exportados (6/6)

| Arquivo | Workflow | ID | Ativo | Nós | published == draft |
|---|---|---|---|---|---|
| `WF-01_Agente_Bia.json` | WF-01 Agente Bia | `sd9gjIKZpGi75qmq` | sim | 7 | sim |
| `Agente_Gerenciador_de_Leads_BnA.json` | Agente Gerenciador de Leads — BnA | `6o8aUBnewvDU7eTT` | sim | 18 | sim |
| `Notificacao_WhatsApp.json` | Notificação WhatsApp | `prM7IEhhDyg5mwlM` | sim | 4 | sim |
| `Envio_de_Tarefas_por_Responsavel.json` | Envio de Tarefas por Responsável | `vtj8ZfQLBn1Ai38R` | sim | 5 | sim |
| `Analista_de_Metricas.json` | Analista de Métricas | `V8miBkgbY4bUWOns` | sim | 10 | sim |
| `Gerente_Autonomo_de_Tarefas_IA.json` | Gerente Autônomo de Tarefas IA | `rYkKEJ81LghRil42` | **NÃO** (`activeVersionId: null` — nunca publicado) | 8 | n/a |

## Garantias deste export

- **Nenhum workflow foi executado.**
- **Nenhum workflow foi editado.** Leitura pura via `get_workflow_details`.
- Nenhum valor de credencial, token ou segredo está nestes arquivos — o MCP do
  n8n **não inclui os bindings de credencial** nos nós (apenas
  `authentication`/`genericAuthType`). Consequência para rollback: ao restaurar,
  **re-selecionar a credencial pelo NOME em cada nó** no n8n UI
  (CRM API key / Gemini / Gmail OAuth2 / header auth).
- Nenhum dado de cliente. Telefone de notificação, e-mails da equipe e
  `phone_number_id` do Graph que aparecem em parâmetros de nós **já estavam
  versionados neste repositório** (exports antigos, `.env.example`,
  `docker-compose.yml`, `bna_agent_context/`) — mantidos para fidelidade de
  rollback. Qualquer telefone/e-mail novo seria substituído por
  `__REDACTED_*__` (nenhum caso ocorreu; ver `_export.sanitization` em cada
  arquivo).

## Estrutura de cada arquivo

JSON com bloco `_export` (metadados desta exportação: fonte, timestamp,
trigger, notas de sanitização, `published_version_matches_draft`) seguido da
definição do workflow: `id`, `name`, `active`, `isArchived`, `versionId`,
`activeVersionId`, `settings`, `nodes` (com parâmetros completos),
`connections`, `tags`, `meta`. O bloco duplicado `activeVersion` do MCP foi
removido após verificação de que `published == draft` (coluna acima).

## Uso pretendido (rollback)

1. Identificar o workflow pelo `id`/`name`.
2. No n8n UI, restaurar os nós/conexões/parâmetros conforme este JSON
   (ou reimportar o JSON e re-linkar credenciais por nome).
3. Conferir `active` conforme a coluna acima — **Gerente Autônomo de Tarefas IA
   deve permanecer INATIVO** (ver avisos em `n8n/workflows/README.md` e
   `docs/n8n_guardrails_plan.md`, subpacote D).
4. Publicar. Estes snapshots são o estado de referência pré-guardrails.

Estes exports substituem, como referência de rollback, os arquivos antigos de
2026-05-21 em `n8n/workflows/` (mantidos por valor histórico — o WF-01 antigo
é a única fonte do prompt pré-rewrite usado pelo `bna_agent_context/`).
