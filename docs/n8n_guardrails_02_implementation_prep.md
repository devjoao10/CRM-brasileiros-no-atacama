# Preparação segura de implementação — guardrails n8n da BIA (N8N-BIA-GUARDRAILS-02)

```text
Package ID:               N8N-BIA-GUARDRAILS-02
Package name:             Safe implementation prep for n8n BIA guardrails
Package type:             OPS (workflow hardening / backup / controlled change)
Target module:            n8n/Automations
Secondary modules:        none
Status:                   backup/export COMMITADO · safelock AGUARDANDO APROVAÇÃO ·
                          mudanças em workflows ativos NÃO APLICADAS (design only)
Data:                     2026-07-08
Baseline de rollback:     n8n/workflows/live_exports/20260708_1443/ (6/6 workflows)
```

> Regra deste documento: nenhum segredo, valor de credencial ou dado de cliente.
> Credenciais são referidas por NOME/TIPO. Diffs referem-se aos JSONs do export
> `20260708_1443`, que é a baseline de rollback de TODAS as mudanças abaixo.

## 0. Delta de confirmação live (export 20260708_1443 vs plano -01)

| # | Achado do plano -01 | Estado no export de hoje |
|---|---|---|
| 1 | Typo no e-mail do user_id 1 | **CONFIRMADO caractere a caractere**: mapa do Envio de Tarefas usa `joaopedro.fg.baldo@…`; o Analista de Métricas (funcionando) envia para `joaopedro.fig.baldo@…`. Falta o “i”. |
| 2 | Gmail sem prefixo `=` | **CONFIRMADO**: no nó "Disparar Gmail", `sendTo` e `subject` NÃO têm `=`; `message` tem. O destinatário vai como texto literal `{{ $json.email_destino }}` → envio provavelmente falha para TODOS diariamente. |
| 3 | Notificação mente sucesso | **CONFIRMADO**: "Enviar WhatsApp" com `onError: continueRegularOutput`; "Responder ao Gerenciador" responde `sucesso: true` fixo. Graph `v19.0`, timeout 15 s. Nota interna do nó está desatualizada (diz "API do Conversas", mas chama `graph.facebook.com` direto). |
| 4 | Destinatário hardcoded | **CONFIRMADO**: `destinatario = '<telefone da equipe>'` fixo no code node "Formatar Mensagem" (valor já versionado em `.env.example`/compose). |
| 5 | Gerente Autônomo perigoso | **CONFIRMADO**: `active: false`, `activeVersionId: null` (nunca publicado); tool "CRM API Tool" com `$fromAI('method')` + `$fromAI('url')` livres autenticados por header; trigger por minutos. |
| 6 | Gerenciador | **CONFIRMADO**: `responsavel_id=5` hardcoded; nota via query string; `retryOnFail: true` no nó do agente; "Tool Acionar Notificador" chama `http://n8n:5678/webhook/notificacao` SEM auth. |
| 7 | Bindings de credencial | O MCP não exporta bindings de credencial → em qualquer rollback/import, re-selecionar credenciais por NOME no n8n UI. |

---

## Fase 3 — SAFELOCK do Gerente Autônomo (proposta exata; NÃO aplicada)

**Alvo:** `Gerente Autônomo de Tarefas IA` (`rYkKEJ81LghRil42`), hoje inativo e
nunca publicado. Objetivo: impedir ativação acidental. Nada é ativado,
executado ou tem credencial alterada.

### Opção 1 — ARQUIVAR via MCP (recomendada)

- **Mudança exata:** uma única chamada `archive_workflow(rYkKEJ81LghRil42)`.
  O workflow sai da lista principal e não pode ser ativado por engano.
  Nenhum nó, nome ou credencial é tocado.
- **Rollback:** desarquivar no n8n UI (ação reversível nativa); estado de
  referência em `live_exports/20260708_1443/Gerente_Autonomo_de_Tarefas_IA.json`.
- **Verificação pós-mudança:** re-ler via MCP e confirmar `isArchived: true` e
  `active: false`.
- Por que preferir: o MCP atual só edita workflows por SUBSTITUIÇÃO completa de
  código (`update_workflow`); um rename “simples” exigiria reconstruir os 8 nós
  (agente IA + tools) com risco de drift estrutural. Arquivar é 1 operação
  atômica, reversível e sem reconstrução.

### Opção 2 — Renomear + sticky note (conforme texto do pacote)

- **Mudança exata:** nome → `DO_NOT_ACTIVATE__Gerente Autônomo de Tarefas IA__UNSAFE`
  + sticky note descrevendo o risco (`$fromAI` método+URL livres com X-API-Key).
- **Custo/risco:** via MCP exige `update_workflow` com o código completo do
  workflow reconstruído em SDK → risco de drift no draft; alternativa segura é
  João renomear manualmente no UI (2 cliques, zero risco).
- **Rollback:** restaurar nome original a partir do export 20260708_1443.
- **Verificação pós-mudança:** re-ler via MCP e confirmar `active: false` +
  nome novo.

**Decisão pedida ao João:** Opção 1 (arquivar via MCP), Opção 2 (rename manual
no UI ou via MCP com reconstrução), ou manter como está. **Sem aprovação
explícita, nada será aplicado** — o pacote para aqui nesta frente.

---

## Fase 4 — mudanças em workflows ATIVOS (preparadas, NÃO aplicadas)

### A. Notificação WhatsApp — honestidade de erro (`prM7IEhhDyg5mwlM`)

Diff em nível de nó (aplicar futuramente em duplicata INATIVA, validar com pin
data, depois portar ao ativo com aprovação):

1. Nó `Enviar WhatsApp` (`04cb6c97…`): `onError: continueRegularOutput` →
   **`continueErrorOutput`** (separa o ramo de falha).
2. Novo nó Code `Validar Resposta Graph` na saída principal: sucesso real ⇔
   corpo contém `messages[0].id`; senão tratar como falha.
3. `Responder ao Gerenciador` (`203c2e3f…`): substituir o corpo fixo por schema
   explícito alimentado pelos dois ramos:

```json
{
  "success": true,
  "provider_status": "sent | http_<code> | error",
  "error_message": "resumo seguro: HTTP status + mensagem pública da Meta; NUNCA token, headers, phone_number_id ou payload bruto",
  "timestamp": "{{ $now.toISO() }}",
  "sucesso": true
}
```

   (`sucesso` legado espelhando `success` até o Gerenciador ser atualizado.)
4. Atualizar a descrição da "Tool Acionar Notificador" no Gerenciador para
   declarar que a resposta pode indicar falha.
5. Teste sem WhatsApp: pin data com corpo de erro simulado → `success:false`;
   pin data com `messages[0].id` → `success:true`. Nenhuma chamada real à Meta.

Follow-ups registrados (fora deste pacote): destinatário hardcoded → variável
de ambiente do n8n (exige env no VPS); Graph `v19.0` → alinhar com `v21.0`
usada pelo Conversas; nota interna do nó desatualizada.

### B. Envio de Tarefas por Responsável (`vtj8ZfQLBn1Ai38R`)

Correções preparadas (aplicar em duplicata inativa com Gmail desabilitado):

1. Nó `Disparar Gmail` (`ae1df2a7…`):
   - `sendTo`: `{{ $json.email_destino }}` → `={{ $json.email_destino }}`
   - `subject`: `Pauta do Dia: … {{ $json.nome_responsavel }}` →
     `=Pauta do Dia: Suas Tarefas no CRM - {{ $json.nome_responsavel }}`
2. Nó `Formatar E-mail (HTML)` (`2002576d…`), mapa `responsaveis`:
   - `user_id 1`: corrigir typo (`fg` → `fig`), alinhando com o endereço que o
     Analista de Métricas já usa com sucesso.
   - `user_id 5` (Júlia): **e-mail real NÃO existe em nenhuma fonte segura
     disponível (placeholder no live). PARADO — perguntar ao João o destino
     correto; nunca chutar.**
3. Estratégia definitiva (Opção 1 do plano -01): buscar e-mails do CRM via
   `GET http://crm:8000/api/users?is_active=true`. Pré-requisitos a validar
   antes: (a) usuário da API key do n8n tem `role=admin` (endpoint é
   `require_admin`); (b) e-mails na tabela `users` estão corretos. Enquanto
   (a)/(b) não confirmados, o mapa hardcoded corrigido é o intermediário.
4. Se um responsável não tiver e-mail válido: pular + logar aviso no output do
   code node (nunca inventar destino).

### C. Auth interna dos webhooks `notificacao` e `gerenciador-leads`

(`agent-bia` fica FORA — depende de CONV-N8N-AUTH-01 deployado antes; hard rule
deste pacote.)

O que o João precisa criar no n8n UI (valores nunca via MCP/repo/chat):

| Credencial (tipo Header Auth) | Header | Usada por |
|---|---|---|
| `Webhook Auth — Notificação` | `X-Webhook-Token` | Webhook `notificacao` + "Tool Acionar Notificador" (Gerenciador) |
| `Webhook Auth — Gerenciador` | `X-Webhook-Token` | Webhook `gerenciador-leads` + "Tool Enviar ao Gerenciador de Leads" (WF-01) |

Gerar 2 segredos distintos (ex.: `openssl rand -base64 32`), um por webhook,
para rotação independente. Edições n8n correspondentes (após aprovação):

1. Webhook `notificacao` (`a2b844b7…`): `authentication: none` → `headerAuth`
   com `Webhook Auth — Notificação`.
2. "Tool Acionar Notificador" no Gerenciador (`ba043d13…`): hoje SEM auth →
   `genericCredentialType/httpHeaderAuth` com a mesma credencial (mesmo padrão
   já usado nas tools CRM).
3. Webhook `gerenciador-leads` (`2eabee3d…`) + "Tool Enviar ao Gerenciador" no
   WF-01: idem com `Webhook Auth — Gerenciador`.

Ordem sem downtime: atualizar o CALLER primeiro (header extra é ignorado
enquanto o webhook não exige), depois ativar a auth no webhook; começar por
`notificacao` (menor tráfego) e validar entre cada passo. Teste negativo
seguro: `POST` sem header → 401/403 (execução nem inicia). Rollback: reverter
`authentication` para `none` (1 campo) ou restaurar export 20260708_1443.

Caller futuro do `agent-bia`: Conversas `_forward_to_agent` enviando
`X-Webhook-Token` — pacote **CONV-N8N-AUTH-01** (config + header + teste com
sentinel), a deployar ANTES de qualquer auth no webhook da Bia.

### D. Guardrails de mutação do Gerenciador (plano; maioria exige backend CRM)

| Guardrail | Curto prazo (n8n) | Definitivo | Exige backend? |
|---|---|---|---|
| Tags merge-safe | — (o fluxo ler-e-reenviar continua racy) | `POST /api/tags/lead/{id}/add {tag_ids}` aditivo/idempotente; tool passa a usá-lo; prompt simplifica | **SIM** (ex. CRM-TAGS-MERGE-01) |
| Handoff atômico | — | `POST /api/leads/{id}/handoff` (responsável + tags + notificação em 1 commit) | **SIM** |
| `responsavel_id` não hardcoded | variável de ambiente n8n (exige env no VPS) | setting `default_handoff_user` no CRM consumido pelo endpoint de handoff | curto: não; definitivo: **SIM** |
| Nota fora de query string | tool muda para `sendBody` | CRM aceitar body JSON `{texto}` (aditivo, manter query por compat) | **SIM** (pequeno) |
| Payload schema-validado | manter placeholders descritivos (já ok) | endurecer `PUT /api/leads/{id}` contra `""` sobrescrevendo valor real; documentar validação Pydantic existente | **SIM** (endurecimento) |

Nada de D é aplicável só via n8n com segurança → implementar após os pacotes
backend correspondentes.

---

## Ordem recomendada de aplicação (pacote N8N-BIA-GUARDRAILS-03)

1. Safelock D (aprovado → arquivar/renomear; zero risco).
2. C intermediário (typo + prefixos `=` em duplicata inativa; e-mail da Júlia
   pendente de João).
3. A em `notificacao` → `gerenciador-leads` (credenciais criadas por João).
4. B (honestidade da Notificação) em duplicata → portar ao ativo.
5. `agent-bia` auth SOMENTE após CONV-N8N-AUTH-01 deployado.

Status: **backup/export commitado · nenhum workflow editado/executado/ativado ·
nenhum WhatsApp/e-mail enviado · nenhuma mutação CRM/Conversas · fixed locally
(docs) / tested locally (validação de estrutura dos exports) / not deployed ·
migration: nenhuma.**
