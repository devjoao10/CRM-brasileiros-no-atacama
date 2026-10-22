# Bia ativa na fila + frases de fora do expediente — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development, com ondas
> paralelas (`.claude/rules/08-parallel-subagent-driven-development.md`). Steps usam `- [ ]`.

**Goal:** (1) a Bia continua ATIVA quando a conversa sai da triagem e entra na fila de espera — só a
vendedora a desliga; (2) as frases automáticas de fora do expediente passam a funcionar para quem
escreve de madrugada depois que a vendedora desligou a Bia.

**Architecture:** mudança na V1 do Conversas (único caminho em produção): ponto único de escrita do
estado operacional (`conversas/app/services/atendimento.py`), predicados das abas do inbox
(`conversas/app/routers/conversations.py`), webhook da Meta (`conversas/app/routers/webhook.py`) e a
API/tela de Configurações. Nada de V2, n8n, migration ou deploy.

**Tech Stack:** Python 3.12 (Conversas) / 3.11 (CRM) · FastAPI · SQLAlchemy síncrono · Pydantic v2 ·
SQLite (dev/CI) / PostgreSQL (prod) · JS vanilla.

**Spec:** pedido do dono (2026-10-06): "Atualmente o sistema desativa o atendimento da BIA logo depois
que ela sai do atendimento da bia e vai para a fila de espera. Preciso que ele permaneça ativado e a
vendedora desativa manualmente. Também preciso que as frases automáticas de fora de expediente
funcionem, depois que a Julia desativa a Bia, às vezes as pessoas cobram atendimento na madrugada, e
isso deve ser tratado com mensagens automáticas." A seção "Decisões" abaixo é a especificação
derivada desse pedido + da auditoria desta rodada; ela é a autoridade quando o texto de uma task for
ambíguo.

WP codes: **CONV-BIA-FILA-01** (Bia ativa na fila) e **CONV-EXPEDIENTE-01** (fora do expediente).

---

## Diagnóstico (auditoria desta rodada — fatos verificados no código)

- Cadeia real do handoff em produção: Bia (n8n) → Gerenciador → workflow n8n "Handoff Humano → Julia":
  (1) `PUT /api/leads/{id}/responsavel?responsavel_id=5` no CRM, que pela ponte
  `app/services/conversas_bridge.py` chama `POST /api/conversations/by-lead/{id}/handoff`;
  (2) `POST /api/conversations/by-lead/{id}/handoff` de novo; (3) `POST /api/conversations/{id}/assign`
  `{"user_id": 5}`. Handoff E assign passam por `aplicar_estado_humano`, que grava
  `is_bot_active = False` (`conversas/app/services/atendimento.py:186`). Mudar só o handoff não
  resolveria: o passo (3) desligaria a Bia de novo.
- As abas do inbox dependem do flag: `bia` = bot ligado; `fila`/`todos` = bot desligado
  (`conversations.py:261-276`). Bia ligada + na fila sumiria da FILA DE ESPERA.
- O prompt da Bia no n8n já prevê continuar respondendo depois da fila ("PÓS-ENTRADA NA FILA", "ENTRADA
  ÚNICA NA FILA"); o backend era o ponto fora da curva.
- Fora do expediente: `_send_auto_reply_if_needed` (`webhook.py:397-453`) não tem trava de Bia/atendente;
  falhas reais: (a) produção não roda seed (`ENVIRONMENT=production`) e a tela de Configurações só
  EDITA linhas existentes — com as tabelas vazias não há como cadastrar a frase nem o horário;
  (b) `@NOMEATENDENTE`/`@EMAILATENDENTE` nunca resolvem no webhook (`user=None`) e a frase é pulada em
  silêncio; (c) sem anti-spam: cada mensagem de madrugada recebe um aviso; (d) o toggle da tela mostra
  "Frase ativada" mesmo quando o PUT falha.
- Exposições NOVAS criadas por manter a Bia ligada na fila (tratadas aqui): a resposta da Bia pode chegar
  depois que a vendedora assumiu (a espera do n8n dura até 240s e o envio não re-checa o flag); a falha
  degradada da Bia (`webhook.py:1211-1212`) zeraria o `atendente_id` de uma conversa já atribuída.

## Decisões (spec derivada — vinculante)

- **D1** Entrar ou continuar na fila NÃO desliga a Bia: handoff (`/{id}/handoff` e `/by-lead/{id}/handoff`),
  `/assign`, `/release` e `PUT /{id}` que só troca `atendente_id` preservam `is_bot_active` como está
  (nunca religam uma Bia desligada).
- **D2** Continuam desligando a Bia (é a vendedora assumindo): botão "Desativar Bot"
  (`PUT {"is_bot_active": false}`), `/claim` ("Assumir"), a primeira resposta humana
  (`marcar_atendimento_humano`) e `/initiate`. A falha degradada da Bia também continua desligando
  (evita o loop do "repita sua mensagem", AUDIT-2026-08-WA), mas agora PRESERVA o atendente atual.
- **D3** Religar a Bia (`PUT {"is_bot_active": true}`) mantém o reset existente (sem dono, sem fila, sem
  1ª resposta).
- **D4** Abas: `bia` = aberta ∧ bot ligado ∧ `queued_at IS NULL`; `fila` = aberta ∧ (bot desligado ∨
  `queued_at IS NOT NULL`) ∧ `primeira_resposta_humana_at IS NULL`; `todos` = aberta ∧ (bot desligado ∨
  `queued_at IS NOT NULL`); `meus` e `encerradas` inalteradas. `bia` e `todos` continuam disjuntas e
  juntas cobrem todas as abertas. `/counts` e `aguardando_humano` seguem os mesmos predicados.
- **D5** A Bia não fala por cima da vendedora: depois da espera do agente, `_forward_to_agent` relê a
  conversa e, se `is_bot_active` estiver False, descarta a resposta (inclusive o fallback) e não altera
  nenhum campo operacional.
- **D6** O aviso `out_of_hours` só é enviado quando a mensagem NÃO vai para a Bia
  (`forward_to_agent` falso: Bia desligada na conversa ou agente global desligado). Quando a Bia vai
  responder, o aviso não sai, mas a prioridade atual é mantida: fora do horário com `out_of_hours`
  configurado, `greeting`/`waiting` continuam suprimidos.
- **D7** Anti-spam: no máximo 1 aviso de fora do expediente por conversa a cada 12h —
  `OUT_OF_HOURS_COOLDOWN = timedelta(hours=12)`; conta só envios com o MESMO texto e status diferente
  de `"failed"` (um envio que falhou é tentado de novo na próxima mensagem).
- **D8** Nas frases automáticas, `@NOMEATENDENTE`/`@EMAILATENDENTE` resolvem para o atendente da
  conversa (`atendente_id`) quando houver um; sem atendente continuam insolúveis e a frase é pulada
  como hoje (CONV-VAR-01-HARD-01).
- **D9** Configurações utilizáveis em produção: `GET /api/settings/auto-replies` lista todos os gatilhos
  do catálogo (os que não existem no banco aparecem INATIVOS, com o texto padrão e `id` nulo);
  `PUT /api/settings/auto-replies/{trigger}` cria a linha se faltar (só gatilho do catálogo; outro →
  404); ativar uma frase com mensagem vazia → 422. `GET /api/settings/business-hours` devolve sempre os
  7 dias (dia sem linha = aberto sem horário, que é o que o webhook já considera); o `PUT` valida
  `HH:MM` e, em dia aberto com os dois horários, fechamento > abertura.
- **D10** Fuso horário mantido: UTC-3 fixo (horário de Brasília), como hoje.
- **D11** Sinal para o n8n: o payload enviado à Bia ganha `"na_fila_humana": <bool>`
  (`queued_at IS NOT NULL`). Inerte até o WF-01 lê-lo; habilita o n8n a bloquear uma 2ª entrada na fila.

---

## Global Constraints

Valem para TODA task.

- Workspace: `C:\Users\João\Projects\CRM-brasileiros-no-atacama\.claude\worktrees\bia-atendimento-config-16d588`.
  Todo arquivo lido/escrito fica DENTRO dele. Nada de deploy, banco de produção, escrita no n8n,
  migration nova ou execução de migration.
- **Não faça commit, stash, reset ou checkout.** Deixe as mudanças no working tree e liste os arquivos
  alterados no relatório. Outras tasks editam OUTROS arquivos no MESMO working tree ao mesmo tempo.
- Altere SOMENTE os arquivos listados em `Files:` da sua task.
- **Guarda da V1** (`tests/test_v2_v1_intacta.py`): oito arquivos têm hash fixado. Neste plano só podem
  mudar `conversas/app/services/atendimento.py`, `conversas/app/routers/conversations.py` e
  `conversas/app/routers/webhook.py` (o baseline é atualizado SÓ na Task 4). NÃO modifique
  `conversas/app/services/outbound.py`, `conversas/app/services/crm.py`, `app/routers/leads.py`,
  `app/services/lead_creation.py`, `app/services/conversas_bridge.py`.
- Nesses oito arquivos as palavras inteiras `state`, `state_version` e `closed_at` NÃO podem aparecer fora
  de comentário `#` — **docstring e string literal contam** (check B.1). Escreva todo texto novo em
  português. Não rode `tests/test_v2_v1_intacta.py` (a Task 4 roda); confira com
  `Select-String -Path <arquivo> -Pattern '\bstate\b|\bstate_version\b|\bclosed_at\b'` e garanta que
  qualquer ocorrência esteja só em comentário `#`.
- Não toque V2 (`conversas/app/v2/**`, `conversas/app/routers/v2_*`, `app/routers/v2_*`).
- Sem dependência nova. Comentários, docstrings e mensagens de erro em PT-BR, citando o WP
  (`CONV-BIA-FILA-01` / `CONV-EXPEDIENTE-01`). Mudanças cirúrgicas, no estilo do arquivo.
- Preserve a codificação UTF-8 dos arquivos (acentos/emoji existentes) — use a ferramenta Edit, nunca
  reescreva arquivo por shell.
- **Testes** — um processo por arquivo, sem `pytest`:
  - Conversas (Python 3.12 com os pins de `conversas/requirements.txt`), a partir da raiz do worktree:
    PowerShell: `$env:ENVIRONMENT='development'; $env:PYTHONIOENCODING='utf-8'; & .\.venv\Scripts\python.exe tests\test_<nome>.py`
  - CRM (pins de `requirements.txt`, Python global):
    PowerShell: `$env:ENVIRONMENT='development'; $env:PYTHONIOENCODING='utf-8'; python tests\test_<nome>.py`
  - Todo arquivo de teste NOVO do Conversas contém a string literal `CONVERSAS_DIR` (discriminador do CI)
    e segue o padrão da suíte: helper `check(cond, msg)` acumulando `failures`, banco SQLite em
    `scratch/<nome-do-arquivo>.db`, `app.dependency_overrides` limpo no fim, `sys.exit(1)` se falhar,
    sem rede (Meta e n8n neutralizados).
  - Rode SÓ os arquivos de teste da sua task (lista "Verificação"). Os mesmos arquivos de teste não
    podem rodar em duas tasks ao mesmo tempo (o banco em `scratch/` colide). A suíte completa é rodada
    pelo controlador depois da onda.

---

## File map

| Task | Files | Depends-on |
|---|---|---|
| 1 | `conversas/app/services/atendimento.py`, `conversas/app/routers/conversations.py`, `conversas/static/js/conversas.js`, `conversas/app/config.py`, `templates/pipeline.html`, `tests/test_conversas_operational_state.py`, `tests/test_conversas_hotfix01_queued_at.py`, `tests/test_conversas_inbox_filters.py`, `tests/test_conversas_queue.py`, `tests/test_conversas_assignment_notes.py`, `tests/test_conversas_bia_ativa_fila.py` (novo) | none |
| 2 | `conversas/app/routers/webhook.py`, `tests/test_conversas_variables.py`, `tests/test_conversas_webhook_hardening.py`, `tests/test_conversas_agent_timeout.py`, `tests/test_conversas_fora_expediente.py` (novo), `tests/test_conversas_bia_agente_fila.py` (novo) | none |
| 3 | `conversas/app/models/auto_reply.py`, `conversas/app/seed.py`, `conversas/app/routers/settings.py`, `conversas/app/schemas/settings.py`, `conversas/static/js/settings.js`, `conversas/templates/settings.html`, `tests/test_conversas_settings_expediente.py` (novo) | none |
| 4 | `tests/test_v2_v1_intacta.py` | 1, 2 |

Interface compartilhada: `aplicar_estado_humano(...)` ganha o keyword-only `desligar_bia: bool = True`
(Task 1). A Task 2 só a chama no ramo degradado, sem passar `desligar_bia` (desliga, D2).

---

### Task 1: Bia permanece ativa na fila (CONV-BIA-FILA-01)

**Files:** os da linha 1 do File map. Testes existentes só podem ser alterados onde afirmam o
comportamento antigo que D1/D4 mudam (handoff/assign/release desligando a Bia; abas).
**Depends-on:** none

- [ ] **1.1 `conversas/app/services/atendimento.py` — `aplicar_estado_humano`**
  - Novo parâmetro keyword-only `desligar_bia: bool = True` (depois de `resetar_atendimento`).
  - Corpo: `conversation.atendente_id = atendente_id` como hoje; `conversation.is_bot_active = False`
    SÓ quando `desligar_bia` for True. Nada mais muda (queued_at / primeira_resposta_humana_at idênticos).
  - Docstring: substituir o parágrafo "`is_bot_active` vira False em TODOS os casos..." por um que diga:
    CONV-BIA-FILA-01 — entrar ou continuar na fila não desliga mais a Bia; `desligar_bia=True` (padrão) é
    a vendedora/um humano assumindo (claim, botão "Desativar Bot" no PUT, initiate) e a falha degradada
    da Bia no webhook; `desligar_bia=False` preserva o flag como está — handoff, assign (inclusive o do
    workflow n8n "Handoff Humano -> Julia"), release e PUT que só troca o atendente; nunca religa uma
    Bia desligada; quem desliga nesses casos é a vendedora, manualmente.
  - `marcar_atendimento_humano`: código inalterado. Só atualize o item da docstring
    "is_bot_active = False (defensivo...)" para: a Bia segue ligada na fila (CONV-BIA-FILA-01); a
    primeira resposta humana é a vendedora assumindo, e a Bia para de responder.

- [ ] **1.2 `conversas/app/routers/conversations.py`**
  - `_inbox_predicates` (D4): acrescente `na_fila = Conversation.queued_at.isnot(None)` e
    `lado_humano = or_(humana, na_fila)` (`or_` já é importado; confirme). `bia` →
    `[aberta, Conversation.is_bot_active.is_(True), Conversation.queued_at.is_(None)]`; `fila` →
    `[aberta, lado_humano, aguardando]`; `todos` → `[aberta, lado_humano]`; `meus`/`encerradas`
    inalterados. Atualize a docstring com um parágrafo CONV-BIA-FILA-01 explicando por que `queued_at`
    também classifica o lado humano.
  - `handoff_conversation`: passe `desligar_bia=False` ao `_apply_human_state` e corrija o item
    "is_bot_active = False (a BIA para de ser acionada...)" da docstring: o flag é preservado — a Bia
    continua respondendo na fila até a vendedora desligá-la; um handoff nunca religa a Bia.
  - `assign_conversation`: `desligar_bia=False` + comentário curto (o n8n "Handoff Humano -> Julia"
    chama esta rota no fim do handoff; atribuir não é assumir).
  - `release_conversation`: `desligar_bia=False` + comentário (devolver à fila não desliga a Bia).
    O comentário "PACOTE-A: assumir desliga a BIA" do claim continua verdadeiro: `/claim` NÃO muda.
  - `update_conversation`, ramo `else` (bot desligado ou só troca de atendente): passe
    `desligar_bia=data.is_bot_active is False`. Assim `{"is_bot_active": false}` desliga (botão) e
    `{"atendente_id": N}` sozinho preserva. O ramo `is True` (reset) não muda. Atualize a docstring da
    rota: desde CONV-BIA-FILA-01 "dono definido + Bia ligada" é estado legítimo quando a conversa está
    na fila (aparece em `fila`/`todos` via `queued_at`); o problema do F-085 era o PUT gravar fora do
    ponto único.
  - Revise o comentário "DECISAO — conversa ainda com a Bia (`is_bot_active=True`) ESTA incluida" de
    `list_inactive_conversations` (`/inativas`): acrescente que, desde CONV-BIA-FILA-01, Bia ligada
    também pode significar "na fila com a Bia ainda respondendo". Sem mudar a query.
  - Não altere nenhuma outra rota nem o parâmetro legado `?queue=`.

- [ ] **1.3 `conversas/static/js/conversas.js`** — o destaque "aguardando humano" (≈L2049-2056):
  `aguardandoHumano = isOpenStatus(conv.status) && (conv.is_bot_active === false || !!conv.queued_at)
  && !conv.primeira_resposta_humana_at;` e ajuste o comentário logo acima (a Bia pode seguir ligada na
  fila; quem marca a espera é `queued_at`). `queued_at` já vem em `ConversationResponse`.

- [ ] **1.4 `conversas/app/config.py`** — `VERSION = "1.0.0"` → `VERSION = "1.0.1"` (cache-busting dos
  assets estáticos; cobre também a mudança de `settings.js` da Task 3).

- [ ] **1.5 `templates/pipeline.html`** (`updateLeadResponsavel`, ≈L1061-1080) — o comentário
  AUDIT-2026-08-WF2 diz que o Conversas "DESLIGA a Bia": corrija para "põe a conversa na fila humana;
  a Bia continua ligada até um atendente desativá-la no inbox (CONV-BIA-FILA-01)". Alertas:
  - `movida`: `'✅ Responsável alterado.\n\nA conversa deste lead no WhatsApp entrou na FILA DE ESPERA do Conversas, aguardando atendimento humano. A Bia continua respondendo o cliente até um atendente desativá-la no inbox.'`
  - `pendente`: `'⚠️ Responsável alterado no CRM, mas o Conversas não respondeu.\n\nA conversa no WhatsApp pode não ter entrado na fila de espera. Confira no inbox.'`
  Mantenha a quebra das strings em linhas concatenadas como no arquivo.

- [ ] **1.6 Testes existentes** — rode os da lista "Verificação"; onde falharem por afirmar o
  comportamento antigo (handoff/assign/release desligando a Bia, debounce não agendado após handoff,
  abas), atualize a asserção e o comentário para D1/D4, citando CONV-BIA-FILA-01. Nunca enfraqueça
  uma asserção que não trate desses comportamentos.

- [ ] **1.7 Novo `tests/test_conversas_bia_ativa_fila.py`** (TDD: escreva primeiro, veja falhar, depois
  implemente). Cenários mínimos, todos via HTTP real (`TestClient`) sobre SQLite:
  1. `POST /api/conversations/by-lead/{lead}/handoff` numa conversa com a Bia ligada →
     `is_bot_active` continua True, `queued_at` preenchido, atendente resolvido; aparece em
     `?inbox=fila` e `?inbox=todos`, NÃO em `?inbox=bia`; `/counts` coerente (`fila`,
     `aguardando_humano`, `bia`).
  2. Handoff repetido preserva `queued_at`, dono e `is_bot_active`.
  3. `POST /{id}/assign` numa conversa na fila com a Bia ligada → Bia continua ligada, dono trocado,
     `queued_at` preservado.
  4. `POST /{id}/release` com a Bia ligada → continua ligada; com a Bia desligada (após `/claim`) →
     continua desligada.
  5. `PUT /{id}` com só `atendente_id` → flag preservado; `PUT {"is_bot_active": false}` → desliga,
     preserva `queued_at` e dono, e a conversa continua em `fila`.
  6. `POST /{id}/claim` → desliga a Bia (inalterado).
  7. Primeira mensagem humana (`POST /{id}/messages`, janela de 24h aberta) numa conversa na fila com a
     Bia ligada → Bia desligada, `primeira_resposta_humana_at` preenchida, sai de `fila`, entra em
     `meus` do atendente.
  8. Handoff numa conversa cuja Bia já foi desligada manualmente → continua desligada.
  9. Uma conversa ainda em triagem (Bia ligada, sem `queued_at`) aparece só em `bia`.

**Verificação (rode exatamente estes):** Conversas: `test_conversas_bia_ativa_fila.py`,
`test_conversas_operational_state.py`, `test_conversas_hotfix01_queued_at.py`,
`test_conversas_inbox_filters.py`, `test_conversas_queue.py`, `test_conversas_assignment_notes.py`,
`test_conversas_hotfix_filters_resp.py`, `test_conversas_followup_inatividade.py`,
`test_conversas_notifications.py`, `test_conversas_security.py`, `test_conversas_admin_role.py`.
CRM (Python global): `test_xss_crud_templates.py`, `test_pipeline_ui_fixes.py`,
`test_pipeline_review_final.py`, `test_pipeline_inline_lead_edit.py`, `test_leads_handoff_bridge.py`.

---

### Task 2: Webhook — aviso fora do expediente e Bia que não atropela a vendedora (CONV-EXPEDIENTE-01 + CONV-BIA-FILA-01)

**Files:** os da linha 2 do File map. `tests/test_conversas_variables.py`,
`tests/test_conversas_webhook_hardening.py` e `tests/test_conversas_agent_timeout.py` só mudam se uma
asserção existente contrariar D5–D8/D11.
**Depends-on:** none

Todas as mudanças de código são em `conversas/app/routers/webhook.py`.

- [ ] **2.1 Constante e import** — `OUT_OF_HOURS_COOLDOWN = timedelta(hours=12)` junto das constantes
  do módulo (perto de `AGENT_DEBOUNCE_SECONDS`), com comentário CONV-EXPEDIENTE-01; importe
  `timedelta` no topo se ainda não estiver (não mexa nos `from datetime import timedelta` locais
  existentes de outras funções).

- [ ] **2.2 `_resolve_auto_reply` (D8)** — monte o `VariableContext` com `user=` o `User` do
  `conversation.atendente_id` quando houver (consulta por id; `User` vem de `app.auth`); senão `None`.
  Docstring/comentário: CONV-EXPEDIENTE-01; sem atendente continua insolúvel e pulado.

- [ ] **2.3 `_send_auto_reply_if_needed` (D6, D7)**
  - Novo parâmetro obrigatório keyword `bia_vai_responder: bool`; a chamada em
    `_process_incoming_message` passa `bia_vai_responder=forward_to_agent`.
  - No bloco fora do horário, com `configured` verdadeiro: envia SÓ se `message` não vazio E
    `not bia_vai_responder` E o mesmo aviso não foi enviado nas últimas `OUT_OF_HOURS_COOLDOWN`
    (helper do 2.4). Em qualquer caso, `return` (prioridade atual preservada: nada de
    greeting/waiting fora do horário quando `out_of_hours` está configurado). Log INFO quando pular
    por "Bia vai responder" e quando pular por "aviso já enviado nas últimas 12h", identificando a
    conversa pelo id (não pelo telefone).
  - Mantenha `if configured:` e exatamente 3 chamadas a `_resolve_auto_reply` dentro desta função
    (`tests/test_conversas_variables.py` inspeciona o fonte). Ramos `greeting` e `waiting` inalterados.

- [ ] **2.4 Helper anti-spam** — função de módulo (nome em PT, ex.
  `_aviso_fora_do_expediente_recente(db, conversation_id, texto) -> bool`): último `Message` outbound
  da conversa com `content == texto` e `status != "failed"`, por `created_at` desc; True se existe e
  `agora - created_at < OUT_OF_HOURS_COOLDOWN`. `created_at` sem tzinfo é UTC (mesma normalização do
  ramo `waiting`). Não use `NOT_FAILED_STATUSES` (não inclui `delivered`/`read`).

- [ ] **2.5 `_forward_to_agent` (D5, D2, D11)**
  - Payload: `"na_fila_humana": conversation.queued_at is not None` com comentário CONV-BIA-FILA-01
    (sinal determinístico para o WF-01 não abrir uma 2ª entrada na fila; inerte até o n8n lê-lo).
  - Logo depois de recarregar a conversa após o `await` (e do tratamento de `None`): se
    `not conversation.is_bot_active` → log INFO ("Bia desligada durante a chamada ao agente na conversa
    {id}: resposta descartada") e `return` sem enviar nada (nem fallback), sem tocar campos e sem
    commit. Comentário CONV-BIA-FILA-01 explicando a corrida (até 240s).
  - Ramo degradado: `aplicar_estado_humano(conversation, conversation.atendente_id,
    keep_queue_position=True)` — preserva o dono atual (com a Bia ligada na fila a conversa pode já ter
    atendente). Atualize o comentário "Nao atribuimos atendente aqui de proposito... entra na fila sem
    dono": não atribui atendente NOVO; quem já era dono continua.

- [ ] **2.6 Novo `tests/test_conversas_fora_expediente.py`** (TDD). Use o harness de
  `tests/test_conversas_webhook_hardening.py` (webhook real, envio Meta neutralizado/capturado,
  `N8N_AGENT_ENABLED` controlado, `_schedule_agent_debounce` capturado). Controle "aberto/fechado agora"
  pelas linhas de `BusinessHours` relativas ao relógio real (dia atual fechado = fora do horário;
  `00:00`–`23:59` = dentro), sem patch de relógio; para o cooldown, retroceda o `created_at` do aviso
  anterior por SQL. Cenários:
  1. Bia desligada + atendente + fora do horário + `out_of_hours` ativo → exatamente 1 aviso enviado e
     persistido.
  2. 2ª mensagem do cliente dentro de 12h → nenhum aviso novo.
  3. Aviso anterior com `created_at` 13h atrás → novo aviso.
  4. Aviso anterior `failed` → nova mensagem envia de novo.
  5. Bia ligada e agente global ligado, fora do horário → nenhum aviso, nenhum greeting/waiting, e o
     debounce da Bia é agendado.
  6. `N8N_AGENT_ENABLED` desligado com a conversa `is_bot_active=True` → aviso enviado.
  7. Dentro do horário, Bia desligada e com atendente → nenhum aviso.
  8. Texto com `@NOMEATENDENTE` + conversa com atendente → aviso com o nome resolvido; sem atendente →
     nada enviado e nada promovido no lugar.

- [ ] **2.7 Novo `tests/test_conversas_bia_agente_fila.py`** (TDD). Chame `_forward_to_agent` (ou
  `_debounce_then_forward`) com `_fetch_agent_parts` substituído por um fake assíncrono:
  1. Durante a chamada o fake desliga a Bia por OUTRA sessão (simula o botão da vendedora) → nenhum
     envio à Meta, nenhuma outbound persistida, campos operacionais intactos.
  2. Mesmo cenário com resposta degradada (`([], False)`) → também nada enviado, nada alterado.
  3. Controle: Bia continua ligada → partes enviadas e persistidas.
  4. Degradado com a Bia ligada, conversa na fila COM atendente → atendente preservado, Bia desligada,
     `queued_at` preservado.
  5. Payload enviado ao agente contém `na_fila_humana` True para conversa com `queued_at` e False sem.

**Verificação (rode exatamente estes, Conversas):** `test_conversas_fora_expediente.py`,
`test_conversas_bia_agente_fila.py`, `test_conversas_variables.py`, `test_conversas_webhook.py`,
`test_conversas_webhook_hardening.py`, `test_conversas_webhook_signature.py`,
`test_conversas_agent_timeout.py`, `test_conversas_agent_silence.py`, `test_conversas_service_window.py`,
`test_conversas_outbound_integrity.py`, `test_conversas_outbound.py`, `test_conversas_meta_resiliencia.py`,
`test_conversas_formatacao_mensagem.py`, `test_conversas_lead_link.py`.

---

### Task 3: Configurações — catálogo de frases, criação pela tela e validação do horário (CONV-EXPEDIENTE-01)

**Files:** os da linha 3 do File map.
**Depends-on:** none

- [ ] **3.1 `conversas/app/models/auto_reply.py`** — constante de módulo `AUTO_REPLY_PADROES`
  (tupla de dicts `trigger`, `title`, `message`, `is_active`) com EXATAMENTE os 7 gatilhos, títulos,
  textos e flags que `seed_auto_replies` usa hoje (onde o seed omite `is_active`, o valor é True).
  Comentário CONV-EXPEDIENTE-01: fonte única do seed de desenvolvimento e da tela de Configurações;
  `is_active` aqui é o padrão do SEED — na tela, gatilho sem linha no banco aparece INATIVO.
- [ ] **3.2 `conversas/app/seed.py`** — `seed_auto_replies` cria as linhas a partir de
  `AUTO_REPLY_PADROES` (cópia de cada dict), com resultado idêntico ao atual. Nada mais muda.
- [ ] **3.3 `conversas/app/schemas/settings.py`**
  - `AutoReplyResponse`: `id: Optional[int] = None`, `created_at: Optional[datetime] = None`,
    `updated_at: Optional[datetime] = None`.
  - `BusinessHoursResponse`: `id: Optional[int] = None`.
  - `BusinessHoursDay`: `open_time`/`close_time` com `Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")`;
    `model_validator(mode="after")`: se `is_open` e os dois horários presentes e `close_time <= open_time`
    → `ValueError("Horario de fechamento deve ser depois da abertura (expediente que atravessa a
    meia-noite nao e suportado)")`.
- [ ] **3.4 `conversas/app/routers/settings.py`**
  - `GET /auto-replies`: linhas do banco (ordem por id, como hoje) + um item por gatilho do catálogo
    ausente no banco, na ordem do catálogo, com `id=None`, `is_active=False`, `title`/`message` do
    catálogo; `total` = tamanho da lista.
  - `PUT /auto-replies/{trigger}`: se a linha não existe e o gatilho está no catálogo, cria com
    `title` do catálogo, `message` = `data.message` se veio, senão o texto do catálogo, `is_active` =
    `data.is_active` se veio, senão False; gatilho fora do catálogo e sem linha → 404 (mensagem atual).
    Depois de aplicar os campos: se a frase ficaria ativa com mensagem vazia/só espaços →
    `db.rollback()` e 422 `"Escreva a mensagem antes de ativar a frase automatica."`.
  - `GET /business-hours` e a resposta do `PUT /business-hours`: sempre 7 itens (weekday 0..6); dia sem
    linha → `id=None, is_open=True, open_time=None, close_time=None`. Extraia um helper para não
    duplicar a montagem.
  - Auth inalterada (`get_current_user`). Comentários CONV-EXPEDIENTE-01.
- [ ] **3.5 `conversas/static/js/settings.js`**
  - `_toggleAutoReply`: só mostra "Frase ativada/desativada" se `resp.ok`; senão mostra a mensagem de
    erro do backend (`detail` string, ou `detail[0].msg` quando for lista do Pydantic, ou um texto
    padrão) e recarrega a lista (`loadAutoReplies()`) para o toggle voltar ao valor real.
  - `_saveAutoReply` e `saveBusinessHours`: no erro, mostram a mesma mensagem do backend (helper único).
- [ ] **3.6 `conversas/templates/settings.html`** — texto do card "Horario Comercial" (sem acentos, como
  o arquivo): `Defina os dias e horarios de atendimento (horario de Brasilia). Fora do horario, a
  mensagem automatica "Fora do expediente" e enviada pelo WhatsApp nas conversas em que a Bia esta
  desativada, no maximo uma vez a cada 12 horas por conversa.`
- [ ] **3.7 Novo `tests/test_conversas_settings_expediente.py`** (TDD), via HTTP real sobre SQLite com
  as tabelas `auto_replies`/`business_hours` VAZIAS no início:
  1. GET auto-replies lista os 7 gatilhos do catálogo, todos inativos e `id` nulo.
  2. PUT `out_of_hours` `{"is_active": true}` → 200, linha criada com o texto padrão e ativa; GET passa a
     devolvê-la com `id`.
  3. PUT gatilho desconhecido → 404.
  4. PUT `greeting` `{"is_active": true}` (texto padrão vazio) → 422, nenhuma linha criada.
  5. PUT `{"message": "   "}` numa frase ativa → 422 e o texto anterior permanece.
  6. GET business-hours com tabela vazia → 7 dias abertos sem horário.
  7. PUT business-hours: `"9:00"` → 422; dia aberto com fechamento <= abertura → 422; dia fechado com
     horários nulos → 200; payload válido → 200 e GET devolve os 7 dias com os valores salvos.
  8. `seed_auto_replies` num banco vazio continua criando as mesmas 7 linhas (gatilhos, títulos,
     textos e flags) — compare com `AUTO_REPLY_PADROES`.

**Verificação (rode exatamente estes):** Conversas: `test_conversas_settings_expediente.py`,
`test_conversas_smoke_seed.py`, `test_conversas_mobile_pwa.py`,
`test_conversas_mobile_responsive_refinement.py`, `test_conversas_tags_sync.py`,
`test_conversas_quick_replies.py`. CRM (Python global): `test_frontend_injection_contract.py`,
`test_render_templates.py`.

---

### Task 4: Baseline da guarda da V1 + regressão completa

**Files:** `tests/test_v2_v1_intacta.py`
**Depends-on:** 1, 2 (rodar só depois que TODAS as alterações em `atendimento.py`, `conversations.py` e
`webhook.py`, inclusive rodadas de correção, estiverem finais).

- [ ] **4.1** Recalcule à mão, com a fórmula de `_sha256_normalizado` (SHA-256 dos bytes com CRLF→LF),
  os digests de `conversas/app/services/atendimento.py`, `conversas/app/routers/conversations.py` e
  `conversas/app/routers/webhook.py`, e substitua os três valores em `_PROTEGIDOS`.
- [ ] **4.2** Acima de cada um, comentário no estilo dos HOTFIX existentes: "CONV-BIA-FILA-01 +
  CONV-EXPEDIENTE-01 (pedido explicito do dono em 2026-10-06; hash recalculado A MAO): <o que mudou
  no arquivo em uma ou duas linhas>. Nenhuma referencia as colunas da V2 — a Familia B.1 continua
  verde." Não altere mais nada no arquivo; nunca automatize a regeneração.
- [ ] **4.3** Rode `tests/test_v2_v1_intacta.py` (Conversas) — todas as verificações OK.
