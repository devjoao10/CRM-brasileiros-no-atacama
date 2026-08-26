# MASTER STABILIZATION PLAN — CRM BnA + Papos/Conversas + Bia

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** eliminar as causas raiz funcionais que fazem atendentes perderem
clientes: o handoff que mente, a fila que não recebe ninguém, o lead que nasce
fora do funil, o dado que some, e a mensagem que parece enviada e não foi.

**Architecture:** dois apps FastAPI sobre o mesmo PostgreSQL. O CRM (`app/`) é
dono do schema de negócio; o Conversas (`conversas/`) é dono do estado
operacional da conversa. Este plano **respeita essa fronteira**: nenhuma nova
escrita cruzada em SQL cru; onde o CRM precisa de um efeito no Conversas, ele
chama o endpoint HTTP que o Conversas já expõe (`CONVERSAS_BASE_URL`, hoje
configurado e morto).

**Tech Stack:** Python 3.11/3.12, FastAPI, SQLAlchemy 2.0, PostgreSQL 16,
Jinja2 + JS vanilla. Testes: `python tests/test_<nome>.py` (um processo por
arquivo). **Não existe pytest/ruff/mypy neste projeto.**

**Spec / inventário:** `docs/audit/MASTER_FUNCTIONAL_BUG_MATRIX.md`

## Global Constraints

- Sem deploy, push, merge, PR, alteração de produção ou de n8n.
- Migrations: `migrations/mNNN_*.py`, idempotentes, `run(engine=None)`,
  verificação pós-DDL, gate — ver `migrations/CLAUDE.md`. **Não executar em
  produção.**
- Banco de validação: `postgresql+psycopg2://bna_test:bna_test_2026@127.0.0.1:55432/bna_app_audit`
  (container `bna-postgres-audit`, descartável). Nenhum outro container é tocado.
- Toda query com função específica de banco precisa do ramo `IS_SQLITE`.
- Comentários, docstrings e mensagens de erro em PT-BR. Rastreio `AUDIT-2026-08-W<N>`.
- Fronteira de commit: no módulo `operational_*` só o service commita; nos
  módulos legados o commit vive no router. Seguir o arquivo que se edita.
- Nenhum `ALTER TABLE` no startup.

---

## Descobertas que moldam o plano (evidência, não suposição)

### D1 — o handoff não tem quem o chame

`POST /api/conversations/{id}/handoff` existe, está correto e **não tem
chamador**. Os 18 nós do workflow *Agente Gerenciador de Leads* apontam para
`http://crm:8000/...` ou `http://n8n:5678/webhook/notificacao`; **nenhum**
alcança a porta 8001. Verificado por dump de todos os `url`/`method` dos
exports e por grep de `8001` e `/api/conversations/` em `n8n/`.

O único sinal determinístico que o repositório recebe no momento do handoff é
`PUT /api/leads/{lead_id}/responsavel?responsavel_id=5`
(`Tool Alterar Responsavel`, id **hardcoded no n8n**).

Consequência: `is_bot_active` nunca vira `False` e `queued_at` nunca é
preenchido. A conversa fica em ATENDIMENTOS BIA para sempre enquanto a Bia diz
ao cliente que ele está na fila. **Isto explica W1-01, W1-02, W1-03, W1-04 e
W1-12 com uma causa só.**

### D2 — o handoff, quando chamado, não atribui ninguém

`handoff_conversation` chama
`_apply_human_state(conversation, conversation.atendente_id, keep_queue_position=True)`
— reaplica o atendente **atual** (`NULL`). Nunca resolve um humano.
Não existe pool de atendentes elegíveis em lugar nenhum do repositório.

### D3 — "atribuído" e "atendido" são a mesma coluna

`_inbox_predicates` classifica por `atendente_id IS NULL`:
`fila` = sem atendente, `meus` = atendente sou eu. E `_apply_human_state`
**apaga `queued_at` assim que um atendente é definido**. Logo, atribuir tira da
fila — exatamente o oposto do requisito operacional.

### D4 — nenhuma mensagem sabe quem a enviou

`Message` não tem `sender_user_id`/`is_bot`/`author`. `record_outbound_message`
é o mesmo caminho para a Bia, para as auto-respostas e para o humano. Sem um
discriminador, "primeira resposta humana" é indecidível.

### D5 — `POST /api/leads` cria só a linha `leads`

Sem `FunnelEntry`, sem `LeadHistory`, sem tag. O caminho equivalente do
Conversas (`auto_create_lead_in_crm`) faz as três coisas. É o mesmo defeito por
trás de W2-13 (F-341), W5-02 (formulário sem tag) e parte de W2-10.

### D6 — a Wave 4 é menor do que o relato sugere

A auditoria anterior já corrigiu, com teste que exercita comportamento:
`simulated` deixou de virar `sent`; a âncora da janela passou a ser o relógio da
Meta e é monotônica; o lookup de template é `(name, language)`; a aridade vem do
Meta; existe preview de template. O que **sobra** de real: o atendente não vê
**quando** a janela fecha (só um cadeado binário), não há retry/backoff, o
callback de status é descartado quando a linha ainda não commitou, e
`/initiate` reporta `message_sent: false` para envio `simulated`.

---

## Ordem das waves

Cada wave é um commit. A ordem é por dependência, não por número do relato.

| Wave | Entrega | Depende de |
|---|---|---|
| A | Estado operacional: fila real, handoff real, atendente elegível | — |
| B | Criação de lead unificada e idempotente (funil + histórico + tag) | — |
| C | Integridade CRM↔Conversas: tags, merge de campos, funil, UI do pipeline | B |
| D | Meta/janela/resiliência: visibilidade da janela, retry, callback órfão | A |
| E | Formulário + follow-up (repo-side) e instruções MANUAL_N8N | B |
| F | Mensagens rápidas / formatação WhatsApp | — |
| G | Segmentação / login / cache de asset | — |
| H | Base de conhecimento da Bia (contradições e regras operacionais) | — |

---

## WAVE A — estado operacional da conversa

**Invariantes que passam a valer (e viram teste):**

1. `bia` ⇔ conversa aberta **e** `is_bot_active = True`.
2. `fila` ⇔ conversa aberta, `is_bot_active = False` e
   `primeira_resposta_humana_at IS NULL`. **Ter atendente atribuído não tira da
   fila.**
3. `meus` ⇔ conversa aberta, `is_bot_active = False`,
   `primeira_resposta_humana_at IS NOT NULL` e `atendente_id = eu`.
4. `todos` ⇔ conversa aberta e `is_bot_active = False` (fila ∪ em atendimento).
5. `encerradas` ⇔ `status = 'encerrada'`.
6. Abrir, visualizar ou reabrir uma conversa **não** altera
   `is_bot_active`, `atendente_id`, `queued_at` nem
   `primeira_resposta_humana_at`.
7. A primeira mensagem outbound **humana** (rota autenticada por um usuário)
   grava `primeira_resposta_humana_at` e, se ainda não houver atendente,
   atribui quem enviou. Bia e auto-resposta **nunca** gravam esse campo.
8. Mensagem nova do cliente numa conversa em fila **não** move `queued_at`
   (FIFO preservado) e **não** apaga a pendência.
9. `release` devolve à fila: `atendente_id = NULL`,
   `primeira_resposta_humana_at = NULL`, `queued_at` novo.
10. Falha definitiva da Bia move a conversa para a fila humana em vez de
    deixá-la invisível.

### Task A1 — colunas de estado (migration m012)

**Files:**
- Create: `migrations/m012_conversas_primeira_resposta_humana.py`
- Modify: `conversas/app/models/conversation.py`
- Test: `tests/test_conversas_handoff_fila.py` (criado na Task A4)

- [ ] Adicionar `primeira_resposta_humana_at DateTime(timezone=True) NULL`,
      indexado, a `conversations`.
- [ ] Backfill conservador: conversas abertas, `is_bot_active = False`,
      `atendente_id IS NOT NULL` **e** com pelo menos uma mensagem outbound →
      `primeira_resposta_humana_at = created_at` da conversa (não temos
      autoria histórica; o critério é "já tinha atendente e já falou").
      Registrar o critério no docstring. Idempotente por
      `WHERE primeira_resposta_humana_at IS NULL`.
- [ ] Gate: abortar com exit≠0 se a coluna não existir após o DDL.

### Task A2 — resolução de atendente elegível

**Files:**
- Create: `conversas/app/services/atendimento.py`
- Modify: `conversas/app/config.py`

**Interfaces:**
- Produz: `resolver_atendente_elegivel(db) -> int | None`
- Produz: `atendentes_elegiveis(db) -> list[int]`

- [ ] Config `ATENDENTES_ELEGIVEIS` (CSV de ids ou vazio). Vazio ⇒ todos os
      usuários `is_active` que **não** sejam o sentinela Agente IA.
- [ ] Estratégia: menor carga (`COUNT` de conversas abertas atribuídas),
      desempate por menor id. Com um único elegível devolve sempre ele — sem
      hardcode de nome nem de id. Com dois, distribui.
- [ ] `None` quando não há elegível: o handoff então enfileira **sem** atendente
      (a fila continua correta; ninguém fica com dono falso).

### Task A3 — handoff que atribui de verdade

**Files:**
- Modify: `conversas/app/routers/conversations.py`

- [ ] `_apply_human_state` ganha o parâmetro
      `marcar_primeira_resposta: bool = False` e **para de apagar `queued_at`
      quando um atendente é definido** — `queued_at` só é limpo quando
      `primeira_resposta_humana_at` é gravado.
- [ ] `handoff_conversation` passa a resolver o atendente elegível quando
      `atendente_id IS NULL`, com `SELECT ... FOR UPDATE` na linha da conversa
      (ramo `IS_SQLITE` para o dialeto sem lock).
- [ ] `claim`/`assign` atribuem sem tirar da fila.
- [ ] `release` limpa `primeira_resposta_humana_at`.
- [ ] `update_conversation` (PUT) deixa de escrever `atendente_id`/
      `is_bot_active` direto e passa por `_apply_human_state` (F-085/W1-19).
- [ ] O `PUT .../responsavel` passa a usar `_apply_responsavel` (hoje código
      morto) — valida usuário ativo e mantém o sync CRM na mesma transação
      (F-086, F-304, F-316 / W1-20, W1-23).

### Task A4 — primeira resposta humana

**Files:**
- Modify: `conversas/app/services/outbound.py`, `conversas/app/routers/conversations.py`
- Test: `tests/test_conversas_handoff_fila.py`

- [ ] `record_outbound_message` ganha `autor_user_id: int | None = None`.
      Rotas humanas passam `current_user.id`; Bia e auto-resposta passam `None`.
- [ ] Quando `autor_user_id` não é `None` e
      `conversation.primeira_resposta_humana_at IS NULL`: grava o timestamp,
      limpa `queued_at`, e atribui `atendente_id = autor_user_id` se estiver
      vazio.
- [ ] Teste de regressão cobrindo os 10 invariantes acima, incluindo:
      abrir não move; outro usuário abrir não move; cliente cutucar não move
      `queued_at`; primeira mensagem humana move; a segunda não re-grava.

### Task A5 — predicados, ordenação e contadores

**Files:**
- Modify: `conversas/app/routers/conversations.py`

- [ ] `_inbox_predicates` reescrito conforme os invariantes 1-5.
- [ ] `_inbox_order`: `fila` por `queued_at ASC NULLS LAST, id ASC`; `meus`/
      `todos` por `updated_at DESC, id DESC`; **todo ramo com desempate por
      `id`** (F-523/W1-24).
- [ ] O ramo legado `?queue=` passa a usar os mesmos predicados (hoje ordena por
      `last_customer_msg_at`, divergindo da fila real).
- [ ] `/counts` ganha `aguardando_humano` = |fila|, separado de `unread`
      (W1-18/W1-11).

### Task A6 — UI da fila

**Files:**
- Modify: `conversas/static/js/conversas.js`, `conversas/templates/conversas.html`

- [ ] Badge da FILA DE ESPERA pintado a partir de `aguardando_humano`, com
      destaque visual próprio (não some ao abrir a conversa).
- [ ] A linha da conversa passa a mostrar **o atendente** (`atendente_nome`)
      quando existe, com o responsável comercial como informação secundária —
      hoje mostra só `responsavel_nome || 'Agente IA'` (W1-02/W1-09).
- [ ] Poll da conversa aberta passa a comparar também `atendente_id`,
      `responsavel_id`, `status` e `queued_at`, não só a contagem de mensagens
      (F-115/W1-22).

### Task A7 — ponte CRM → Conversas no handoff

**Files:**
- Modify: `app/routers/leads.py`, `app/config.py`
- Create: `app/services/conversas_bridge.py`

- [ ] `PUT /api/leads/{id}/responsavel` — o único sinal determinístico que o
      n8n entrega no handoff — passa a notificar o Conversas via
      `POST {CONVERSAS_BASE_URL}/api/conversations/by-lead/{lead_id}/handoff`,
      autenticado pela API key interna.
- [ ] A ponte é **best-effort e não pode derrubar o request**: falha vira log
      de warning e um campo `conversa_notificada: false` na resposta. Nunca
      levanta.
- [ ] Novo endpoint no Conversas: handoff por `lead_id` (o n8n não conhece
      `conversation_id`).
- [ ] Se a variável não estiver configurada, a ponte é no-op silencioso — o
      comportamento atual, sem regressão em dev.

### Task A8 — falha da Bia não some com o cliente

**Files:**
- Modify: `conversas/app/routers/webhook.py`

- [ ] Quando `_forward_to_agent` falha definitivamente (após o fallback), a
      conversa vai para a fila humana (`is_bot_active=False`, `queued_at=now`)
      em vez de continuar em ATENDIMENTOS BIA (W1-13, W3-15).
- [ ] O fallback deixa de pedir "repita sua mensagem": informa que um humano vai
      assumir. O contexto não é descartado.

---

## WAVE B — criação de lead unificada (F-341)

### Task B1 — serviço único de criação

**Files:**
- Create: `app/services/lead_creation.py`
- Modify: `app/routers/leads.py`, `app/services/ai_tools.py`
- Test: `tests/test_lead_funnel_entry.py`

- [ ] `criar_lead(db, dados, *, funnel_id=None, tag_nome=None, origem)` cria o
      `Lead`, garante **um** `FunnelEntry` na primeira etapa do funil alvo,
      escreve `LeadHistory` de criação e aplica a tag de origem.
- [ ] Idempotência do `FunnelEntry`: `INSERT` protegido por
      `UNIQUE(lead_id, funnel_id)` + `IntegrityError` tratado como caminho
      normal — não `SELECT` antes do `INSERT` (ROOT-007).
- [ ] Funil alvo: parâmetro explícito quando dado; senão o funil default do
      CRM. **Nenhum nome de funil hardcoded.**
- [ ] `POST /api/leads` passa a usar o serviço.
- [ ] `POST /api/pipeline/funnels/{id}/leads` (usado pelo n8n do formulário)
      torna-se idempotente: lead já no funil ⇒ 200 com a entry existente, não
      duplica nem erra.

### Task B2 — migration da UNIQUE

**Files:**
- Create: `migrations/m013_funnel_entry_unique.py`

- [ ] `UNIQUE(lead_id, funnel_id)` em `funnel_entries`. Abortar com exit 2 e
      listar os ids se houver duplicata pré-existente — **nunca deduplicar
      automaticamente**.

---

## WAVE C — integridade CRM ↔ Conversas

### Task C1 — tags sem perda concorrente

- [ ] `PUT /api/tags/lead/{id}` ganha semântica explícita: além de `tag_ids`
      (substituição), aceita `adicionar`/`remover`. O editor de lead do CRM
      passa a mandar o delta que o usuário fez, não o snapshot (W2-01/W2-02/W2-03).
- [ ] `sync_lead_tags_to_conversation` deixa de sobrescrever remoções feitas no
      Conversas que ainda não subiram ao CRM (F-529/W2-04).

### Task C2 — merge de campos do lead

- [ ] O contrato `""` vs `null` já está correto e testado
      (`descartar_strings_vazias` + guard NOT NULL). Verificar e **preservar**.
- [ ] `append_anotacao`: read-modify-write em JSON sem lock → `FOR UPDATE`
      (ramo `IS_SQLITE`) (F-239/W2-08).
- [ ] `LeadUpdate`: `null` explícito em coluna nullable continua limpando; sem
      mudança de contrato, com teste que fixa isso (W2-05/W2-07).

### Task C3 — funil e pipeline na UI

- [ ] "Ver no Funil" resolve pela `FunnelEntry` persistida (W2-11).
- [ ] Mover card atualiza o estado local a partir do retorno persistido; o card
      some da etapa anterior sem refresh (W2-14).
- [ ] `formatWhatsappInput` movido para o partial compartilhado (F-427/W2-23).
- [ ] Filtro "Chegada em X dias" passa a ir na request (F-430/W2-24).
- [ ] `loadAllTags()` aguardado antes de abrir o editor por deep link
      (F-419/W2-22).
- [ ] `loadStage` sequencia requisições em vez de descartar; busca com debounce
      (F-165/W2-25).
- [ ] Filtro de viajantes: quantidade **exata** quando a regra pede exata
      (W2-18).

---

## WAVE D — Meta / janela / resiliência

- [ ] **W4-05/W4-02:** a UI passa a mostrar quando a janela fecha
      (`last_customer_msg_at + 24h`), não só o cadeado.
- [ ] **W4-11:** callback de status para `wamid` desconhecido deixa de ser
      descartado — fica pendente e é reconciliado quando a linha aparecer.
- [ ] **W4-09/W4-10:** retry com backoff para 429/5xx, com chave de
      idempotência para não reenviar o que a Meta já aceitou.
- [ ] **Bug novo:** `conversations.py:488` usa `msg.status == 'sent'` em vez de
      `NOT_FAILED_STATUSES`; `/initiate` reporta falha para envio `simulated`.
- [ ] Itens já corrigidos pela auditoria anterior (W4-01, W4-03, W4-06, W4-07)
      viram `NOT_REPRODUCED_WITH_EVIDENCE` na matriz, com o teste que prova.

---

## WAVE E — formulário e follow-up

- [ ] Lead de formulário nasce com tag e `FunnelEntry` — herdado da Wave B.
- [ ] Follow-up por inatividade: o repo expõe o que for necessário; o disparo
      depende do n8n ⇒ `MANUAL_N8N` com instrução campo a campo.
- [ ] Segundo formulário do rodapé: `MANUAL_N8N`.

---

## WAVE F — mensagens rápidas / formatação

- [ ] Preservar `\n`, `*negrito*`, `_itálico_`, `~riscado~` e ```` ``` ```` na
      renderização e na reutilização; copiar mensagem devolve o corpo
      armazenado, não o `innerText`.

---

## WAVE G — segmentação / login / cache

- [ ] Só bugs reproduzíveis. O que exigir infraestrutura vira
      `FIXED_PENDING_PRODUCTION_VALIDATION`.
- [ ] Cache-buster de asset se — e só se — for simples e seguro.

---

## WAVE H — base de conhecimento da Bia

- [ ] Resolver as contradições que **não** exigem decisão de negócio (primeiro
      nome, e-mail, cotação antiga, promessa de ação inexistente, redundância de
      roteiro, B2B).
- [ ] Preço `[PENDENTE_VALIDACAO]` e regra de altitude **exigem decisão de
      negócio** ⇒ `BLOCKED_OPERATOR`, sem inventar número.
- [ ] Ampliar `scripts/validate_bna_agent_context.py` para falhar em
      contradição conhecida, e cobrir com teste.
