# FULL_SYSTEM_STABILIZATION_PLAN.md

Plano derivado **exclusivamente** dos 588 findings reais em `FINDINGS.csv` e das
14 causas em `ROOT_CAUSES.md`. Nenhuma tarefa genérica.

## Como as waves foram montadas

Prioridade por **risco combinado**, não por domínio: severidade × confiança ×
risco de dado/segurança × blast radius × reprodutibilidade × risco da própria
correção. Dentro de cada wave, os agentes têm **propriedade exclusiva de
arquivo** — nenhum arquivo aparece em duas tarefas simultâneas. Onde há
sobreposição, a tarefa é serializada para a wave seguinte.

## Restrição de escopo que molda todo o plano

A missão proíbe deploy, alteração de dados de produção, execução de migrations em
produção e mudança de infraestrutura externa. Isso divide os findings em dois
conjuntos que este plano trata de forma diferente:

- **Corrigível no repositório** → implementado, com regression test.
- **Exige ação do operador ou decisão de negócio** → NÃO tocado, documentado com
  instrução precisa em `RELEASE_READINESS.md`. Inclui: rotação da chave vazada,
  purga do histórico git, autenticação dos webhooks n8n, arquivamento do
  `Gerente_Autonomo_de_Tarefas_IA`, `NOSUPERUSER` no `crm_user`, re-execução do
  hardening do Postgres, configuração do Traefik, chave SSH do deploy, e as
  decisões de preço/altitude da base de conhecimento.

---

## WAVE 1 — segurança e perda de dados (código, alta confiança)

| Tarefa | Arquivos (propriedade exclusiva) | Findings | Regression test |
|---|---|---|---|
| **W1-A** sessão do CRM | `app/auth.py`, `app/routers/auth.py`, `app/main.py`, `static/js/auth.js`, `static/js/login.js`, `templates/login.html` | Bearer sombreia cookie; JWT sem `typ`; flags de cookie lendo env cru; logout sem auth e sem checar resposta; **senha em texto claro na URL**; sem CSP; page shells cacheáveis | `tests/test_auth_hardening.py` |
| **W1-B** sessão/config do Conversas | `conversas/app/{config,main,auth}.py`, `conversas/app/routers/{pages,auth,api_config}.py`, `conversas/app/schemas/api_config.py`, `conversas/static/js/auth.js`, `conversas/templates/login.html` | **`SECRET_KEY` hardcoded**; portão de página só por presença de cookie; cookie escrito por JS; logout no-op; `/me/validate` sempre `true`; `/me` sempre 500; verify token em texto claro; sem security headers nem rate limit; CORS `*`+credentials | `tests/test_conversas_auth_hardening.py` |
| **W1-C** superfície de ferramentas do LLM | `app/services/ai_tools.py`, `app/routers/ai.py` | **SSRF via `@host`**; path traversal na escrita; sem allowlist de tabela; `/api/auth/token` alcançável; fallback para a conexão owner; 4 tools escrevendo direto; `campos_personalizados` descartado; limiter duplicado; `except:` nus | `tests/test_ai_tool_hardening.py` |
| **W1-D** perda de dados no webhook | `conversas/app/routers/webhook.py`, `conversas/app/services/{whatsapp,outbound}.py` | **200 incondicional descartando o lote**; **auto-reply impede a Bia de responder**; `simulated` → `sent`; status regride; timestamp da Meta ignorado; reaction dispara envio fadado a falhar; histórico ilimitado | `tests/test_conversas_webhook_hardening.py` |
| **W1-E** segredo vazado e runtime | `docs/n8n-toolHttpRequest-guia.md`, `docker-compose.yml`, `Dockerfile`, `conversas/Dockerfile`, `.dockerignore` ×2, `.gitignore`, `.env.example`, `requirements.txt` | **API key commitada**; `SECRET_KEY` sem `:?`; drift de `N8N_AGENT_ENABLED`; 12 variáveis nunca entregues; `META_API_VERSION` inalcançável; banco de dev indo para a imagem; `.gitignore` sem `.env.*`/dumps; 3 dependências abaixo da correção; containers como root; sem `--proxy-headers` | `tests/test_secret_hygiene.py` |

**Critério de conclusão da wave:** os 5 testes novos passam, a suíte existente não
regride além dos conflitos previstos (ver abaixo), e cada agente reporta o que
deixou de fora e por quê.

### Conflitos previstos (testes que travam correções)

Três testes existentes **afirmam o comportamento defeituoso** e vão ficar
vermelhos. Isso é esperado e correto; a wave 2 os corrige:

- `tests/test_conversas_outbound_integrity.py:378-387` exige `status == "sent"`
  para um envio simulado — exatamente o bug que apaga a falha de credencial.
- `tests/test_conversas_service_window.py:418-445` ancora a janela de 24h num
  timestamp `"1"` (epoch) — trava o bug de âncora.
- `tests/test_conversas_service_window.py:588` exige
  `count("_require_open_window(conversation)") == 3` — adicionar um guard
  *correto* deixa o teste vermelho.

Regra dada aos agentes: **nunca enfraquecer a correção para satisfazer o teste**;
reportar arquivo:linha para adjudicação.

---

## WAVE 2 — XSS, integridade de dados e concorrência

| Tarefa | Arquivos | Findings |
|---|---|---|
| **W2-A** XSS nos templates CRUD | `templates/{tarefas,tags,equipes,relatorios}.html` | `JSON.stringify` dentro de atributo (CRITICAL); `esc()` inútil dentro de `onclick`; `relatorios.html` sem helper nenhum |
| **W2-B** XSS nos templates de lead | `templates/{segmentacao,leads,pipeline}.html`, `templates/partials/_lead_edit_modal.html` | destinos e e-mail/whatsapp sem escape; `datas_destinos` em atributo; **perda silenciosa de tags ao editar pelo Pipeline**; `formatWhatsappInput` inexistente no Pipeline |
| **W2-C** kanban operacional | `templates/operational/kanban.html` | 3 sinks de XSS; sem `Auth.requireAuth`; token lido uma vez; contrato errado com `/api/users`; sem tratamento de erro em 6 fetches |
| **W2-D** inbox do Conversas | `conversas/static/js/{conversas,settings,templates}.js`, `conversas/app/routers/media.py` | **blob com mime declarado pelo cliente** (2 CRITICAL); retry sem guard; lista reconstruída a cada 5s destruindo o scroll; verify token num input |
| **W2-E** constraints + migration | `app/models/pipeline.py`, `app/models/operational/card.py`, `conversas/app/models/conversation.py`, NOVO `migrations/m011_*.py` | UNIQUE ausentes em 4 pares que o código trata como chave; `conversations.whatsapp` não-unique |
| **W2-F** concorrência e contrato CRM↔Conversas | `conversas/app/routers/conversations.py`, `conversas/app/services/crm.py` | claim/release/retry check-then-act; `lead_history.dados` NULL quebrando o histórico; `unread_count` com dois significados |
| **W2-G** autorização e mass assignment | `app/routers/{analytics,tasks}.py`, `app/schemas/task.py` | `/api/analytics/reports` sem `require_admin`; `user_id` client-settable em tarefa |
| **W2-H** destravar os testes | `tests/test_conversas_service_window.py`, `tests/test_conversas_outbound_integrity.py`, `tests/test_pipeline_inline_lead_edit.py` | os 3 conflitos acima + o `or True` que mata a asserção de XSS |

---

## WAVE 3 — verificação

1. Suíte completa (51 + os novos arquivos), um processo por arquivo, como o CI faz.
2. Comparação contra o BASELINE: toda regressão investigada, nenhuma justificada
   com "já estava quebrado" sem prova do baseline.
3. Reauditoria: agentes independentes releem os arquivos alterados **e** os
   vizinhos, procurando efeito colateral, contrato alterado, race introduzida e
   dead code criado.
4. `RELEASE_READINESS.md` com números reais.

---

## O que este plano deliberadamente NÃO faz

- **Não** toca produção, nem roda migration, nem faz deploy.
- **Não** reescreve a arquitetura de duas-aplicações-um-banco (ROOT-001). A
  correção estrutural completa — o CRM virar dono exclusivo do schema e expor um
  caminho de escrita compartilhado — é um projeto próprio, não uma wave de
  estabilização. O que esta missão faz é fechar os buracos que essa arquitetura
  abriu.
- **Não** remove `'unsafe-inline'` do CSP. Os templates dependem de script e
  style inline em dezenas de lugares; de-inlinar é uma mudança de front-end
  inteira. O CSP entra com `'unsafe-inline'` e isso fica registrado como dívida
  explícita — ainda assim ele fecha `frame-ancestors`, `object-src` e `base-uri`.
- **Não** decide preço, prazo de reembolso ou regra de altitude para menores de
  7 anos. São decisões de negócio.


---

# FASE 2 — plano reconciliado (2026-08-25)

Escrito **depois** de ter os três workflows de produção na mão. O plano das waves
1–3 acima continua válido como registro; o que muda é a priorização, porque a
evidência nova invalidou parte dela e revelou o que realmente quebra.

## O que a evidência nova fez com o plano anterior

- **Saiu do plano:** tudo que dependia de `Gerente Autônomo de Tarefas IA`,
  `Notificador`, `Analista de Métricas`, `Envio de Tarefas por Responsável` e
  `Notificação WhatsApp` — nenhum está em produção. Isso inclui o CRITICAL "o LLM
  escolhe método e URL".
- **Entrou no plano:** o workflow *Formulário do Site → CRM BnA*, que nunca havia
  sido auditado e escreve no CRM a partir de um webhook público.
- **Mudou de dono:** três correções que pareciam de código são de **operador**
  (M1, M2, M4 em `N8N_MANUAL_CHANGES.md`), e uma que parecia de operador era de
  código (o 422 do `PUT /api/leads`).

## WAVE 4 — executada nesta fase

| Tarefa | Arquivos | Estado |
|---|---|---|
| Contrato `PUT /api/leads` com o n8n | `app/schemas/lead.py`, `app/routers/leads.py` | feito, `tests/test_n8n_contract_lead_update.py` |
| Silêncio ≠ falha na ponte Conversas↔Bia | `conversas/app/routers/webhook.py` | **metade** — a outra é M3 |
| Reverter o risco de disponibilidade do `StageSchema.id` | `app/schemas/pipeline.py` | feito |
| Classificação de erro dialeto-dependente | `conversas/app/routers/webhook.py` | feito, `tests/test_postgres_dialect_divergence.py` |
| `SELECT ... INTO` na denylist da IA | `app/services/ai_tools.py` | feito |
| Ordenação de NULL na fila legada | `conversas/app/routers/conversations.py` | feito |
| Corrida de primeiro contato, antes da m011 | `conversas/app/routers/webhook.py` | feito |
| Backup executado de verdade | `scripts/backup_postgres.sh` | feito, 2 defeitos graves corrigidos |

## WAVE 5 — o operador, e só ele

Não há tarefa de código aqui. `docs/audit/N8N_MANUAL_CHANGES.md` tem o passo a
passo. Ordem que eu seguiria:

1. **M1** (um caractere) e **M2** (remover nó morto) — são os dois que estão no
   caminho da entrada na fila humana. Aplique e observe um dia.
2. **D4** — verificar o nome do modelo. Se estiver errado, tudo o mais é
   irrelevante porque os agentes não rodam.
3. **M3** — fecha a metade que falta do pedido de desculpas por emoji.
4. **D5** — rotacionar a chave, na ordem descrita (n8n primeiro, revogação por
   último), senão os três workflows param juntos.
5. **D1/D2** — autenticar os dois webhooks service-to-service.
6. **D3** — decidir o que o formulário público pode sobrescrever.
7. **M4**, **M5**, **D6**, **D7**.

## WAVE 6 — o que fecha o veredito, e ainda não foi feito

| Tarefa | Por que ainda não | O que destrava |
|---|---|---|
| PostgreSQL no CI | sem Docker nesta máquina | rodar a suíte no dialeto real, fechando ROOT-016 |
| `m011` contra clone do dado real | precisa do dump de produção | saber se há duplicatas a reconciliar |
| Restore real do backup | precisa de PostgreSQL | fechar o último blocker de dado |
| Cobertura do boundary CRM↔Conversas (F-106) | `auto_link_conversation` está mockado em 10 testes | dar teste às correções de `crm.py`, que hoje não têm nenhum |
| `FunnelEntry` para lead criado pelo n8n (F-341) | é decisão de produto: o endpoint do CRM cria só o Lead | leads da Bia deixam de nascer fora do Kanban |

## O que este plano deliberadamente NÃO faz

- **Não recria o Notificador**, nem propõe substituto. A fila humana já é o
  mecanismo, e o prompt da Bia já explica isso ao cliente.
- **Não edita `n8n/workflows/live_exports/`** — snapshots são registro histórico.
- **Não refatora a hierarquia de `LeadBase`/`LeadCreate`/`LeadUpdate`**, embora a
  duplicação de validadores seja real (apontada pelo Caveman). É redesenho do
  schema mais consumido do sistema, e o teste que a Fase 1 reancorou trava o
  conjunto de campos — o risco não paga o ganho numa fase de estabilização.
- **Não decide preço, política de reembolso nem regra de altitude.**
