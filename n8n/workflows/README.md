# n8n/workflows — exports versionados

> ✅ **EXPORT LIVE ATUAL: `live_exports/20260708_1443/`** (N8N-BIA-GUARDRAILS-02,
> 6/6 workflows, sanitizado, com README de rollback). Esta é a baseline de
> rollback para qualquer edição de guardrails.
>
> ⚠️ Os arquivos soltos NESTA pasta (2026-05-21) permanecem DESATUALIZADOS e
> são mantidos por valor histórico — em especial o export antigo do WF-01
> Agente Bia, única fonte do conteúdo removido no rewrite live do prompt
> (LGPD, pagamento, saúde/altitude), usado pelo `bna_agent_context/`.
> Antes de editar qualquer workflow live: re-exportar os 6 para
> `live_exports/<timestamp>/` (rollback versionado).

## Inventário live (2026-07-08)

| Workflow | Ativo | Trigger | Export atual |
|---|---|---|---|
| WF-01 Agente Bia | sim | webhook `agent-bia` POST — **sem auth** | `live_exports/20260708_1443/WF-01_Agente_Bia.json` |
| Agente Gerenciador de Leads — BnA | sim | webhook `gerenciador-leads` POST — **sem auth** | `live_exports/20260708_1443/Agente_Gerenciador_de_Leads_BnA.json` |
| Notificação WhatsApp | sim | webhook `notificacao` POST — **sem auth** | `live_exports/20260708_1443/Notificacao_WhatsApp.json` |
| Envio de Tarefas por Responsável | sim | cron `0 8 * * *` | `live_exports/20260708_1443/Envio_de_Tarefas_por_Responsavel.json` |
| Analista de Métricas | sim | cron `0 7 * * *` | `live_exports/20260708_1443/Analista_de_Metricas.json` |
| Gerente Autônomo de Tarefas IA | **NÃO** (nunca publicado) | schedule (minutos) | `live_exports/20260708_1443/Gerente_Autonomo_de_Tarefas_IA.json` |

## Avisos de segurança

- 🔴 **3 webhooks públicos sem autenticação** (paths versionados no repo — o
  path NÃO é segredo; a proteção precisa ser auth). Plano de correção:
  [`docs/n8n_guardrails_plan.md`](../../docs/n8n_guardrails_plan.md).
- 🔴 **Gerente Autônomo de Tarefas IA: NUNCA ATIVAR como está.** Contém tool
  onde o LLM monta método+URL livres contra a API autenticada do CRM
  (inclusive DELETE). Ver subpacote D do plano de guardrails.
- 🟠 **Notificação WhatsApp responde sucesso mesmo quando o envio falha**
  (`onError: continueRegularOutput` + resposta fixa). Ver subpacote B.
- 🟠 **Envio de Tarefas**: e-mail da Júlia é placeholder e os campos
  `sendTo`/`subject` do Gmail estão sem o prefixo `=` de expressão. Ver
  subpacote C.
- Credenciais são referidas por NOME dentro dos exports (googlePalmApi,
  httpHeaderAuth, gmailOAuth2) — **valores existem apenas no n8n**. Nunca
  commitar valores de credencial, `.env` ou dados de clientes nesta pasta.

## Documentação relacionada

- Plano de guardrails: `docs/n8n_guardrails_plan.md`
- Preparação de implementação (diffs por nó + safelock): `docs/n8n_guardrails_02_implementation_prep.md`
- Arquitetura dos workflows: `docs/arquitetura_workflows_n8n.md`
- Contexto versionado da BIA: `bna_agent_context/`
- Vault (untracked): `TECNOLOGIA_E_SISTEMAS/04_MODULOS_DO_SISTEMA/N8N/`
