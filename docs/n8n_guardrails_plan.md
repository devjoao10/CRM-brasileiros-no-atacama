# Plano de Guardrails — workflows n8n da BIA (N8N-BIA-GUARDRAILS-01)

```text
Package ID:               N8N-BIA-GUARDRAILS-01
Package name:             Guardrails and safety hardening for BIA n8n workflows
Package type:             SECURITY (workflow hardening / controlled change)
Target module:            n8n/Automations
Secondary modules:        CRM, Conversas (design-impact only nesta fase)
Status:                   DESIGN ONLY — nenhum workflow editado/executado/ativado
Data:                     2026-07-08
Fonte:                    definições LIVE dos 6 workflows lidas via n8n MCP (read-only)
```

> **Regra deste documento:** nenhum segredo, valor de credencial, telefone da
> equipe ou e-mail privado é impresso aqui. Credenciais são referidas por NOME/
> TIPO; números e endereços hardcoded são referidos por descrição.

## 0. Confirmação live (2026-07-08)

| # | Achado | Confirmado live |
|---|--------|-----------------|
| 1 | `POST /webhook/agent-bia` (WF-01 Bia) ativo e **sem autenticação** | ✅ "No credentials required" |
| 2 | `POST /webhook/gerenciador-leads` ativo e **sem autenticação** | ✅ idem |
| 3 | `POST /webhook/notificacao` ativo e **sem autenticação** | ✅ idem |
| 4 | Notificação retorna sucesso mesmo com falha do Graph API | ✅ `onError: continueRegularOutput` no nó "Enviar WhatsApp" + respondToWebhook fixo `sucesso: true` |
| 5 | Envio de Tarefas: e-mail da Júlia é placeholder | ✅ `user_id 5 → EMAIL_REAL_DA_JULIA@gmail.com` no code node |
| 5b | **NOVO:** Gmail `sendTo`/`subject` sem prefixo `=` | ✅ provável envio literal `{{ ... }}` — o digest pode estar quebrado para TODOS, não só Júlia (verificar histórico de execuções) |
| 5c | **NOVO:** e-mail do user_id 1 no mapa hardcoded diverge por 1 letra do endereço usado no Analista de Métricas | ✅ provável typo — digest do João possivelmente indo para endereço inexistente |
| 6 | Gerente Autônomo inativo e perigoso se ativado | ✅ `active: false`, `activeVersionId: null`; tool "CRM API Tool" com `$fromAI('method')` + `$fromAI('url')` livres, autenticada com X-API-Key |
| 7 | Gerenciador: 9 writes CRM decididos por LLM; "Definir Tags" SUBSTITUI | ✅ PUT `/api/tags/lead/{id}` substitui; handoff `responsavel_id=5` hardcoded; nota via query string; `retryOnFail` no agente |

**Manter (funciona e é invariante):** Bia devolve texto ao Conversas (Conversas
envia via `record_outbound_message` — integridade CONV-08b); BIA delega writes
ao Gerenciador; `is_bot_active` + debounce; X-API-Key nas tools CRM.

---

## A. WEBHOOK-AUTH — autenticação nos 3 webhooks

### Problema
Os 3 webhooks são públicos (Traefik) e os paths estão versionados no repo
(compose/docs) — qualquer um pode: injetar conversas falsas na Bia (custo Gemini
+ poluição de memória), disparar mutações no CRM via Gerenciador (criação/
alteração de leads, tags, funil, handoff) e spammar WhatsApp da equipe via
Notificação.

### Desenho
Header-based auth usando o suporte nativo do nó Webhook do n8n
(`authentication: headerAuth` + credencial Header Auth):

| Webhook | Credencial n8n (NOME, valor só no n8n) | Caller a atualizar |
|---|---|---|
| `agent-bia` | `Webhook Auth — Bia` (header `X-Webhook-Token`) | Conversas `_forward_to_agent` |
| `gerenciador-leads` | `Webhook Auth — Gerenciador` | WF-01 Bia → "Tool Enviar ao Gerenciador de Leads" |
| `notificacao` | `Webhook Auth — Notificação` | Gerenciador → "Tool Acionar Notificador" |

- **3 segredos distintos** (um por webhook) para rotação independente e menor
  raio de dano. Gerar com `openssl rand -base64 32` (João, fora deste pacote).
- Callers internos n8n→n8n ("Tool Enviar ao Gerenciador", "Tool Acionar
  Notificador"): trocar de `authentication: none` para
  `genericCredentialType/httpHeaderAuth` apontando para a credencial certa —
  mesmo padrão já usado nas tools CRM.
- **Conversas (mudança de backend/env necessária — fora deste pacote):**
  - `conversas/app/config.py`: novo `N8N_WEBHOOK_TOKEN = os.getenv("N8N_WEBHOOK_TOKEN", "")`.
  - `conversas/app/routers/webhook.py::_forward_to_agent` (linha ~512): enviar
    header `X-Webhook-Token` quando configurado.
  - `.env` de produção: nova variável (valor NUNCA no repo).
  - Sugerido como pacote próprio: **CONV-N8N-AUTH-01** (1 arquivo de config +
    1 header + teste com sentinel).

### Ordem de rollout SEM downtime
1. Conversas passa a ENVIAR o header (webhook ainda sem auth — header extra é
   ignorado; zero impacto).
2. Criar as 3 credenciais no n8n (humano, no UI — valores nunca via MCP/repo).
3. Ativar Header Auth webhook a webhook, começando por `notificacao` (menor
   tráfego), validando entre cada um.
4. Só então ativar no `agent-bia` (depende do deploy do CONV-N8N-AUTH-01).

### Rollback
Reverter o nó Webhook para `authentication: none` (1 campo, publish) — ou
restaurar o export pré-mudança versionado em `n8n/workflows/`. Re-exportar os
6 workflows ANTES de qualquer edição é pré-requisito deste subpacote.

### Teste sem enviar WhatsApp
- **Negativo (seguro, sem execução):** `POST /webhook/<path>` sem header →
  esperar 401/403; a execução nem inicia.
- **Positivo `agent-bia`:** POST com header + payload sintético de conversa de
  teste com mensagem não-qualificante ("oi, teste interno") → resposta volta ao
  caller; nenhum WhatsApp é enviado (quem envia é o Conversas, que não está no
  loop desse teste). Custo: 1 chamada Gemini.
- **Positivo `gerenciador-leads`/`notificacao`:** NÃO testar com payload real
  (mutaria CRM / enviaria WhatsApp). Validar apenas o negativo + observar o
  próximo lead qualificado real com monitoramento das execuções.

---

## B. NOTIFICATION-HONESTY — falha honesta na Notificação WhatsApp

### Problema
`onError: continueRegularOutput` no nó "Enviar WhatsApp" + resposta fixa
`sucesso: true` ⇒ falha do Graph API é invisível: o Gerenciador (e o histórico)
registram sucesso e a equipe simplesmente não fica sabendo do lead qualificado.

### Desenho (preserva o caminho feliz)
1. Nó "Enviar WhatsApp": trocar `onError` para **`continueErrorOutput`** (saída
   de erro separada).
2. Novo nó Code **"Validar Resposta Graph"** na saída principal: sucesso real =
   corpo contém `messages[0].id`; caso contrário tratar como falha.
3. Dois respondToWebhook (ou um só alimentado por merge), com **schema de
   resposta explícito**:

```json
{
  "success": true|false,
  "provider_status": "sent|http_<code>|error",
  "error_message": "resumo seguro (HTTP status + message pública da Meta; NUNCA token, headers, phone_number_id ou payload bruto)",
  "timestamp": "{{ $now.toISO() }}"
}
```

4. Compatibilidade: manter também o campo legado `sucesso` espelhando `success`
   até o Gerenciador ser atualizado (o consumidor é a tool LLM, que só lê texto
   — baixo risco).
5. Atualizar a descrição da "Tool Acionar Notificador" no Gerenciador para
   informar que a resposta pode indicar falha.

Fora de escopo aqui (registrar como follow-up): destinatário hardcoded no code
node (mover para variável de ambiente do n8n exige mudança de env no VPS);
`v19.0` do Graph API desatualizada vs `v21.0` usada pelo Conversas.

### Rollback
Restaurar export pré-mudança (1 workflow, 4 nós).

### Teste sem enviar WhatsApp
Editar em duplicata INATIVA (ver §Modo de implementação): apontar temporariamente
a URL do Graph para um endpoint inexistente OU usar pin data com resposta de erro
simulada → verificar `success:false`; pin data com corpo `messages[0].id` →
`success:true`. Nenhuma chamada real à Meta é necessária para validar os ramos.

---

## C. JULIA-DIGEST-FIX — Envio de Tarefas por Responsável

### Problema (3 defeitos no mesmo workflow)
1. `user_id 5 (Júlia)` → e-mail placeholder → digest dela nunca chega.
2. `user_id 1` → endereço com typo aparente (1 letra a menos que o usado no
   Analista de Métricas) → digest do João provavelmente indo a endereço errado.
3. Nó Gmail: `sendTo` e `subject` SEM prefixo `=` → o n8n envia o texto literal
   `{{ $json.email_destino }}` em vez de avaliar a expressão ⇒ o envio
   provavelmente FALHA para todos, todos os dias, silenciosamente (cron sem
   error workflow). **Verificar histórico de execuções antes de concluir.**

### Desenho — estratégia recomendada (Opção 1 + fallback)
- **Opção 1 (recomendada): buscar e-mails dinamicamente do CRM.** Novo nó HTTP
  `GET http://crm:8000/api/users?is_active=true` (X-API-Key já existente).
  Pré-requisitos a validar: (a) o usuário da API key do n8n tem `role=admin`
  (o endpoint exige `require_admin`); (b) os e-mails na tabela `users` do CRM
  estão corretos — o comentário no próprio code node sugere que talvez não
  estejam ("quando os emails do CRM forem corrigidos"). Se (b) falhar → João
  corrige os e-mails no CRM UI primeiro (dado, não código).
  O code node passa a agrupar por `user_id` usando o mapa vindo do CRM; se um
  responsável não tiver e-mail válido, pular + logar aviso no output (nunca
  inventar destino).
- **Opção 2 (fallback):** variável de ambiente do n8n (exige env no VPS) — só
  se a Opção 1 for bloqueada por (a) ou (b).
- **Opção 3:** se nem CRM nem env tiverem o e-mail real da Júlia → PARAR e
  perguntar ao João o destino correto (nunca chutar).
- Em qualquer opção: corrigir `sendTo` → `={{ $json.email_destino }}` e
  `subject` → `=Pauta do Dia: ... {{ $json.nome_responsavel }}` (prefixo `=`).

### Rollback
Restaurar export pré-mudança.

### Teste sem enviar e-mail
Duplicata inativa + pin data com tarefas sintéticas; executar até o nó "Tem
tarefas?" e INSPECIONAR o input do nó Gmail (destinatário/assunto resolvidos)
sem executar o nó Gmail (desabilitá-lo na duplicata). Nenhum e-mail sai.

---

## D. AUTONOMOUS-MANAGER-SAFELOCK — Gerente Autônomo de Tarefas IA

### Problema
Inativo, mas **uma ativação acidental** liga um loop por minuto onde um LLM
monta MÉTODO + URL livres (`$fromAI`) autenticados com a X-API-Key do CRM —
inclusive DELETE/PUT em qualquer endpoint. Nunca foi publicado
(`activeVersionId: null`).

### Desenho (defesa em profundidade, mantendo INATIVO)
1. **Agora (aprovação simples, sem risco de produção):**
   - Renomear para `[NÃO ATIVAR — INSEGURO] Gerente Autônomo de Tarefas IA`.
   - Adicionar sticky note de aviso explicando o risco e apontando este plano.
   - Exportar o JSON atual para `n8n/workflows/` (hoje não há export dele).
2. **Recomendado: ARQUIVAR** o workflow no n8n (reversível; sai da lista e não
   pode ser ativado por engano). Decisão do João: arquivar vs reescrever.
3. **Se reescrever (pacote futuro, ex. N8N-WORKFLOW-CLEANUP-01):**
   - Remover a tool genérica `$fromAI(method)+$fromAI(url)`.
   - Substituir por tools ALLOWLIST, GET-only, URLs fixas:
     `GET /api/leads?limit=500`, `GET /api/leads/{id}`, `GET /api/tasks`,
     `GET /api/analytics/dashboard` (os 4 já citados no system prompt).
   - Zero DELETE/POST/PUT a partir de URL gerada por IA. Os dois PUTs de status
     de tarefa (nós fixos, URL determinística) podem permanecer.
4. **Não ativar em nenhuma hipótese neste pacote.**

---

## E. GERENCIADOR-MUTATION-GUARDRAILS — design (implementação parcial futura)

| Guardrail | Problema hoje | Desenho | Classificação |
|---|---|---|---|
| Tags merge-safe | `PUT /api/tags/lead/{id}` SUBSTITUI; o LLM precisa ler-e-reenviar (racy, e se esquecer apaga tags) | Novo endpoint CRM **aditivo** `POST /api/tags/lead/{id}/add {tag_ids}` (merge server-side, idempotente); tool do n8n passa a usá-lo; prompt simplifica (remove passos 2/4) | **Requer backend** (pacote CRM, ex. CRM-TAGS-MERGE-01) + edição n8n depois |
| Handoff atômico | 3 passos LLM (responsável + tags + notificação) — pode parar no meio | Novo endpoint CRM `POST /api/leads/{id}/handoff` que num commit: seta responsável (configurável), aplica tags de handoff e dispara a notificação (server-side) | **Requer backend** + edição n8n depois |
| `responsavel_id` não hardcoded | `?responsavel_id=5` fixo na URL da tool | Curto prazo: variável de ambiente do n8n (exige env no VPS); definitivo: config no CRM (ex. setting `default_handoff_user`) consumida pelo endpoint de handoff | Curto prazo: **edição n8n + env VPS**; definitivo: **backend** |
| Notas fora de query string | `PUT /api/leads/{id}/anotacoes?texto={texto}` — texto longo na URL (limite/encoding/log) | CRM aceitar body JSON `{texto}` (aditivo, manter query por compat); tool n8n muda para `sendBody` | **Backend pequeno** + edição n8n |
| Payload schema-validado antes do write | Writes dependem do que o LLM montar | (1) CRM/Pydantic já valida tipos — documentar como garantia real; (2) endurecer: rejeitar campos vazios que sobrescrevem valor existente (`""` apagando dado real) no `PUT /api/leads/{id}`; (3) n8n: manter placeholders descritivos | (1) docs; (2) **backend**; (3) já ok |

**Seguro AGORA (só n8n, com aprovação):** nada de E — os itens de E dependem de
backend ou env de VPS; implementar A–D primeiro.

---

## Modo de implementação proposto (Fase 3 do pacote)

| Subpacote | Workflows tocados | Nós tocados | Risco | Downtime esperado | Modo |
|---|---|---|---|---|---|
| A auth `notificacao` | Notificação WhatsApp | Webhook Notificação + credencial | baixo (caller é interno) | ~0 (publish atômico) | editar ativo APÓS export + aprovação |
| A auth `gerenciador-leads` | Gerenciador + WF-01 (tool) | Webhook Gerenciador; Tool Enviar ao Gerenciador | baixo | ~0 | idem |
| A auth `agent-bia` | WF-01 Bia | Webhook Mensagem | médio (depende de deploy do Conversas ANTES) | ~0 se ordem respeitada | idem, por último |
| B honestidade | Notificação WhatsApp | Enviar WhatsApp + Validar Resposta + respond | baixo | 0 | desenvolver em duplicata INATIVA, validar com pin data, depois aplicar no ativo |
| C digest | Envio de Tarefas | code node + nó Gmail (+ novo GET users) | baixo (cron 08:00; editar fora do horário) | 0 | duplicata inativa + pin data, Gmail desabilitado no teste |
| D safelock | Gerente Autônomo (inativo) | rename + sticky (+ arquivar) | ~zero | 0 | edição direta (workflow inativo) |

Pré-requisito universal: **re-exportar os 6 workflows live para
`n8n/workflows/`** (rollback versionado) antes de qualquer edição.

## O que exige aprovação do João
1. Autorizar edições live do subpacote A (e a ordem de rollout).
2. Criar as 3 credenciais Header Auth no n8n UI (valores fora do repo/MCP).
3. Deploy do CONV-N8N-AUTH-01 (Conversas envia header) + env `N8N_WEBHOOK_TOKEN` no VPS.
4. Aprovar B e C (edições em workflow ativo após validação em duplicata).
5. Decidir D: arquivar vs manter renomeado (recomendação: arquivar).
6. Confirmar/corrigir e-mails reais (Júlia + typo do user_id 1) no CRM.
7. Priorizar os pacotes backend de E (tags merge, handoff atômico, anotações via body).

## O que exige mudança em Conversas/backend/env
- Conversas: `config.py` + `_forward_to_agent` (header) — CONV-N8N-AUTH-01.
- CRM backend: endpoints de E (tags add, handoff, anotações body) — pacotes CRM futuros.
- Env VPS: `N8N_WEBHOOK_TOKEN` (Conversas) e, se adotado, envs do n8n (destinatário da notificação, responsavel_id).

## O que exige mudança em workflows n8n
- A: 3 nós Webhook + 2 tools caller + 3 credenciais novas.
- B: Notificação (3-4 nós).
- C: Envio de Tarefas (code node, nó Gmail, novo GET users).
- D: Gerente Autônomo (rename/sticky/arquivar).
- E (fase 2, depois do backend): tools do Gerenciador.

## Próxima ação imediata recomendada
1. João aprova o subpacote D (zero risco) + re-export dos 6 workflows.
2. João cria as credenciais e aprova A na ordem `notificacao` →
   `gerenciador-leads` → (após CONV-N8N-AUTH-01 deployado) `agent-bia`.
3. Em paralelo: CONV-N8N-AUTH-01 no repo (pequeno, testável com sentinel).

---

Status: **design only** · fixed locally (docs) / tested locally (n/a — sem
código de runtime) / **not deployed** · migration: **nenhuma** · nenhum
workflow editado/executado/ativado · nenhum WhatsApp/e-mail enviado · nenhuma
mutação CRM/Conversas.
