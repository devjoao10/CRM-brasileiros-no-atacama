# n8n/workflows — exports versionados

> ⚠️ **EXPORTS DESATUALIZADOS (2026-05-21).** A auditoria live de 2026-07-08
> (N8N-LIVE-WORKFLOWS-DEEP-AUDIT-02) confirmou que os workflows em produção
> divergem destes arquivos — em especial o prompt do WF-01 Agente Bia, que foi
> REESCRITO live (20.470 chars vs ~36k do export). O export antigo do WF-01 é
> mantido de propósito: é a única fonte do conteúdo removido no rewrite
> (LGPD, pagamento, saúde/altitude), usado pelo `bna_agent_context/`.
> Antes de editar qualquer workflow live: **re-exportar os 6** para esta pasta
> (rollback versionado).

## Inventário live (2026-07-08)

| Workflow | Ativo | Trigger | Export nesta pasta |
|---|---|---|---|
| WF-01 Agente Bia | sim | webhook `agent-bia` POST — **sem auth** | `WF-01 Agente Bia (1).json` (stale) |
| Agente Gerenciador de Leads — BnA | sim | webhook `gerenciador-leads` POST — **sem auth** | `Agente Gerenciador de Leads — BnA (1).json` (stale) |
| Notificação WhatsApp | sim | webhook `notificacao` POST — **sem auth** | `Notificação WhatsApp.json` (stale) |
| Envio de Tarefas por Responsável | sim | cron `0 8 * * *` | `Envio de Tarefas por Responsável (1).json` (stale) |
| Analista de Métricas | sim | cron `0 7 * * *` | `Analista de Métricas.json` (stale) |
| Gerente Autônomo de Tarefas IA | **NÃO** | schedule (minutos) | **SEM export** |

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
- Arquitetura dos workflows: `docs/arquitetura_workflows_n8n.md`
- Contexto versionado da BIA: `bna_agent_context/`
- Vault (untracked): `TECNOLOGIA_E_SISTEMAS/04_MODULOS_DO_SISTEMA/N8N/`
