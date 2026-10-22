# R8 / R10 — inventário de writers, escopo do write-barrier e desenho do helper

> **Investigação e recomendação apenas.** Nenhum arquivo-fonte foi alterado, nenhum commit foi
> feito, Docker não foi usado. Este documento é o único arquivo criado.
>
> Evidência coletada em `feat/bia-v2-2026-08-29`, HEAD `fb64bd7`, em 2026-09-05.

---

## ⛔ AVISO DE ESTADO DO WORKTREE — ler antes de tudo

**No momento em que este documento foi finalizado, a Família A está VERMELHA — e a mudança
aconteceu durante a investigação, sem ser minha.**

No início desta sessão `git status` não mostrava **nenhum** arquivo rastreado modificado. Ao final,
mostra dois:

```
 M conversas/app/config.py
 M conversas/app/services/atendimento.py      <-- ARQUIVO PROTEGIDO (Família A)
```

`conversas/app/services/atendimento.py` é um dos 8 arquivos com hash fixado. O conteúdo mudou
(implementação do **HOTFIX-01** — "preservar `queued_at` na primeira resposta humana", que é
literalmente o item **P1** dos pré-requisitos da Fase 11+ do plano; o próprio diff diz
"pre-autorizada pelo dono do projeto"). `conversas/app/config.py` ganhou
`CONVERSATION_EVENTS_RETENTION_DAYS` (R13).

**O baseline não foi atualizado junto:** `tests/test_v2_v1_intacta.py` está **inalterado**
(`git status` limpo para ele) e continua esperando `e7031041…`, enquanto o arquivo em disco agora
é `1c4b4746…`. Consequência medida, não inferida:

```
FAIL  conversas/app/services/atendimento.py:
      esperado=e7031041d8d302184e1a161b68bef861e34f3e28f1ddd85e37802929aaf95360
      atual   =1c4b4746dfcc73856f23063c6e5298436fc258367a5e567c254850b258cfad9b
```

**Eu não fiz essas alterações.** Nesta sessão só li arquivos, executei
`python tests/test_v2_v1_intacta.py` e escrevi este único `.md`. A explicação provável é uma
**sessão concorrente trabalhando no mesmo worktree**. Também **não as desfiz nem "consertei" o
hash**: atualizar o baseline da Família A é ato humano deliberado, com procedência registrada — é
exatamente o que este documento argumenta em §2.4 que ninguém deve fazer de passagem.

**O que isso muda neste documento — e o que não muda:**

| | Situação |
|---|---|
| **§2.1 (o revert de `_repair_responsavel_cache` está intacto)** | **INALTERADO e ainda válido.** O arquivo relevante é `conversas/app/routers/conversations.py`, cujo hash **continua batendo** (`0d7463e2…`, verificado depois da mudança). |
| **Números de linha de `atendimento.py`** (writers #2 e #3: L170-179 e L228-237) | **DESLOCADOS** — o diff acrescenta ~15 linhas de docstring acima. A *substância* do inventário não muda (as mesmas funções escrevem os mesmos campos); **as linhas precisam ser reconferidas** antes de virar tarefa. |
| **Semântica de `queued_at`** | **MUDOU.** O HOTFIX-01 faz `aplicar_estado_humano` parar de zerar `queued_at` depois da primeira resposta humana, alinhando-o à invariante `queued_at IS NOT NULL` que `estados.py` e o CHECK m015 já exigiam para `WAITING_HUMAN`/`HUMAN_ACTIVE`. **Isso ajuda a coerência de R10** — mas `queued_at` é um dos 7 campos que a B.3 compara (`_OPERACIONAIS`, L265-266), então o efeito na B.3 precisa ser verificado rodando o teste no ambiente do Conversas. |
| **Toda a análise de §0** (escopo do barrier, B.3/B.4) | **INALTERADA.** O argumento é sobre qual predicado o barrier avalia e sobre a coluna `state`, que nenhum dos dois diffs toca. |

**Ação recomendada antes de qualquer coisa: confirmar com o dono de quem é esse HOTFIX-01, se ele
está completo, e atualizar (ou não) o hash da Família A deliberadamente. Enquanto isso não for
resolvido, o CI está vermelho e nenhuma conclusão sobre "V1 intacta" pode ser tirada da suíte.**

---

## 0. A PERGUNTA DIFÍCIL — escopo do barrier vs. Família B.3/B.4

### 0.1 Resposta curta

| # | Pergunta | Resposta |
|---|---|---|
| **1** | A máquina distingue `AI_TRIAGE` de `WAITING_HUMAN`/`HUMAN_ACTIVE`? | **SIM, e a distinção é exatamente "pré-handoff" vs. "pós-handoff".** Não é convenção: é estrutural, em dois lugares independentes. |
| **2** | É são escopar o write-barrier só para `WAITING_HUMAN`/`HUMAN_ACTIVE`, nunca `AI_TRIAGE`, mantendo B.3/B.4 intactos? | **SIM para o barrier, e B.3/B.4 continuam verdes por construção — é demonstrável, não é torcida.** O repo **já tomou essa mesma decisão de escopo** uma vez, no CHECK `ck_conversations_estado_humano`. |
| **3** | Algo faz esse escopo falhar? | **SIM — e não é sobre B.3/B.4.** O escopo é seguro para os testes, mas **não fecha o P6 do plano**. O próprio plano normativo cita, nominalmente, dois casos de R10 que ficam de fora — e **um deles está em contradição direta e insolúvel com B.3/B.4**. Ver §0.4. **Isso é decisão do dono, não de engenharia.** |

### 0.2 Prova da pergunta 1 — a distinção é estrutural, em dois lugares

**(a) `conversas/app/v2/estados.py`, `_validar_campos_operacionais` (L297-345).** A tabela de
invariantes por estado-destino define os dois grupos em termos de propriedade humana:

| Destino | `is_bot_active` | `atendente_id` | `responsavel_id` | `queued_at` | `primeira_resposta_humana_at` | `status` |
|---|---|---|---|---|---|---|
| `NEW` / `AI_TRIAGE` | `True` | `None` | *(não restringido)* | `None` | `None` | `aberta` |
| `WAITING_HUMAN` | `False` | `not None` | `not None` **e `== atendente_id`** | `not None` | `None` | `aberta` |
| `HUMAN_ACTIVE` | `False` | `not None` | `not None` **e `== atendente_id`** | `not None` | `not None` | `aberta` |
| `CLOSED` | `False` | *(preservado)* | *(preservado)* | *(preservado)* | *(preservado)* | `encerrada` |

`AI_TRIAGE` é *definido* como "bot ligado, ninguém assumiu, ninguém na fila, ninguém respondeu".
`WAITING_HUMAN`/`HUMAN_ACTIVE` são *definidos* como "bot desligado, dono decidido, e o dono
operacional é o mesmo que o dono comercial". A pergunta 1 é literalmente a linha divisória da tabela.

Note também: **`responsavel_id` não é restringido em `AI_TRIAGE`.** Isso importa muito para o §2.

**(b) `conversas/app/models/conversation.py`, `_CHECK_ESTADO_HUMANO_SQL` (L53-62), materializado
como `CheckConstraint(..., name="ck_conversations_estado_humano")` (L137) e migration
`migrations/m015_conversas_estado_humano_check.py`:**

```sql
state NOT IN ('WAITING_HUMAN','HUMAN_ACTIVE')
OR ( NOT is_bot_active AND atendente_id IS NOT NULL AND responsavel_id IS NOT NULL
     AND atendente_id = responsavel_id AND queued_at IS NOT NULL )
```

**Este é o achado mais importante deste documento para a decisão de escopo.** O banco **já contém
um write-barrier parcial, escopado exatamente aos dois estados pós-handoff, e deliberadamente
inerte em todos os outros** (`LEGACY`, `NEW`, `AI_TRIAGE`, `CLOSED`, `NULL`).

Ou seja: **o escopo proposto para o R10 não é uma invenção nova — é o mesmo recorte que o projeto
já escolheu, aprovou, migrou e testou.** A Família B.5 do próprio `test_v2_v1_intacta.py` existe
justamente para provar que o CHECK é inerte fora desses dois estados ("B.5 CHECK INERTE para
LEGACY"). Escopar o barrier de R10 da mesma forma é *consistência com o desenho existente*, não um
atalho para não quebrar teste.

Consequência prática que muda a leitura do R10: em `WAITING_HUMAN`/`HUMAN_ACTIVE` o dano de R10
hoje **não é corrupção silenciosa — é `IntegrityError`** (o CHECK barra). Corrupção silenciosa é o
que acontece nos estados onde o CHECK é inerte. São dois problemas diferentes e o desenho tem que
tratá-los diferente.

### 0.3 Prova da pergunta 2 — por que B.3 e B.4 continuam verdes

Não é "os testes não exercitam esse caminho". É mais forte que isso — o barrier **provadamente
nunca dispara** durante B.3/B.4, por três fatos encadeados:

1. **O barrier só pode ser chaveado pela coluna `state`.** É a única coluna que diz se a conversa
   está sob V2.
2. **A V1 nunca escreve `state`.** Verificado por varredura própria: as únicas três atribuições a
   `state` / `state_version` / `closed_at` em todo o repositório fora de `tests/` estão em
   `conversas/app/v2/estados.py:406-408`, dentro de `aplicar_transicao`. Nenhum outro arquivo, em
   nenhum dos dois apps, escreve essas colunas. (É também o que a Família B.1 prova por AST + grep
   sobre os 8 arquivos protegidos, e o que confirma o R8 — ver §1.3.)
3. **Logo, `state` é imutável durante todo o ciclo V1.** As linhas de B.3/B.4 são criadas com
   `state='LEGACY'` e `state='AI_TRIAGE'` (`_cria`, L243-259) e **terminam o ciclo com o mesmo
   valor** — o próprio B.3 afirma isso na última checagem (L360-361).

Um barrier com predicado `state IN ('WAITING_HUMAN','HUMAN_ACTIVE')` avalia **falso em todas as
sete chamadas do `_ciclo_v1`, para as duas linhas, do início ao fim**. Portanto:

- **B.3 (a):** mesmos status HTTP — preservado, o barrier não intercepta nada.
- **B.3 (b):** mesmos campos operacionais — preservado, nenhuma escrita é recusada nem alterada.
- **B.4:** `state_version` continua 7 — preservado, o helper não é chamado, e mesmo que fosse, o
  ramo `AI_TRIAGE` é passthrough puro (§3).

**Verificação empírica adicional (executada):** `python tests/test_v2_v1_intacta.py` rodou **26 OK,
0 FAIL** — os 8 hashes da Família A e todas as checagens B.1 passavam **no início desta sessão**
(ver o AVISO DE ESTADO DO WORKTREE acima: `atendimento.py` foi alterado por outra mão durante a
investigação e hoje esse resultado é **1 FAIL / 25 OK**; `conversations.py`, o arquivo que importa
para o §2, continua verde). A execução
para em `import app.main` com `ImportError: cannot import name 'genai' from 'google'`, porque este
ambiente é o do CRM (Python 3.11) e não tem `google-genai`, que é dependência do Conversas. **Ou
seja: B.2–B.5 não foram executados por mim e eu não afirmo que passam** — afirmo que A e B.1
passam, e que o argumento acima para B.3/B.4 é estático (baseado em qual predicado o barrier avalia),
não empírico. **Quem implementar deve rodar o arquivo no ambiente do Conversas (Python 3.12) antes
e depois da mudança.**

**Varredura de outros testes que o barrier poderia quebrar (feita):** nenhum outro teste da suíte
exercita uma rota HTTP V1 contra uma conversa em `WAITING_HUMAN`/`HUMAN_ACTIVE`. Os arquivos que
mencionam esses dois estados (`test_v2_estados.py`, `test_v2_handoff.py`, `test_v2_gate.py`,
`test_v2_workers.py`, `test_v2_m015_gate.py`, `test_v2_selecao.py`, `test_v2_e2e_bia.py`,
`test_v2_e2e_distribuido.py`) não chamam `/api/conversations/...`; e
`test_conversas_operational_state.py`, que chama essas rotas, opera sobre conversas em `LEGACY`
(server_default). **O barrier escopado não quebra nenhum teste existente.**

### 0.4 Pergunta 3 — ONDE ISSO FALHA (leia antes de aprovar implementação)

O escopo é seguro. **Mas ele não equivale a "R10 resolvido", e é errado registrá-lo como tal.**

`docs/superpowers/plans/2026-08-29-bia-v2.md` é a fonte normativa e é explícito. Linha **2049**,
tradução literal do que está lá:

> "…`state='WAITING_HUMAN'` após um `release` que zerou `atendente_id` violaria o CHECK e daria
> erro, mas **`state='AI_TRIAGE'` após um `claim` que setou `atendente_id` e desligou o bot passa
> no CHECK e fica silenciosamente errado**."

E a linha **2051**:

> "Um fechamento executado por writer V1 grava `status='encerrada'` e **não** grava `closed_at`,
> nem move `state` para `CLOSED`. […] a conversa **reabre sempre como se estivesse fora da
> janela** — perdendo o dono humano exatamente no caso que a regra existe para proteger."

Isto é, **o plano nomeia dois subconjuntos de R10 que um barrier escopado a
`WAITING_HUMAN`/`HUMAN_ACTIVE` deixa abertos:**

| Subconjunto de R10 | Estado da conversa | CHECK pega? | Barrier escopado pega? | Situação |
|---|---|---|---|---|
| `release` zera `atendente_id` em conversa humana | `WAITING_HUMAN`/`HUMAN_ACTIVE` | **Sim** (`IntegrityError`) | **Sim** | Fechado pelo escopo proposto |
| `_repair_responsavel_cache` quebra `atendente_id = responsavel_id` | `WAITING_HUMAN`/`HUMAN_ACTIVE` | **Sim** (`IntegrityError`) | **Sim** | Fechado — mas ver §2, o call site é intocável |
| **`claim`/`assign` da V1 em conversa `AI_TRIAGE`** | `AI_TRIAGE` | **Não** (inerte) | **Não** (fora do escopo) | **ABERTO — e citado nominalmente no plano** |
| **Reabertura pelo webhook em conversa `CLOSED`** | `CLOSED` | **Não** (inerte) | **Não** (fora do escopo) | **ABERTO — e citado nominalmente no plano** |
| Fechamento V1 (`status='encerrada'`) sem `closed_at`/`state` | qualquer | **Não** | **Não** | **ABERTO** |

**O ponto que precisa ir para o dono do projeto, e que eu não vou dissolver em texto conciliador:**

> O caso `AI_TRIAGE + claim` está em **contradição direta e insolúvel** com a Família B.3/B.4.
> O plano (L2049) diz que ele é um bug de R10 que precisa ser corrigido para o canary (P6).
> B.3 exige que a V1 possa fazer `claim`/`assign`/`release`/`mensagem`/`encerrar` numa conversa
> `AI_TRIAGE` com **resultado idêntico** ao de uma `LEGACY`; B.4 exige que isso **não** incremente
> `state_version`. **Não existe implementação que satisfaça os dois.** Ou o barrier cobre
> `AI_TRIAGE` (e B.3/B.4 quebram, exigindo HOTFIX formal no teste), ou não cobre (e P6 fica
> parcialmente aberto, exigindo que o registro de risco diga isso com todas as letras).

**Recomendação:** implementar o barrier escopado a `WAITING_HUMAN`/`HUMAN_ACTIVE` **agora**
(fecha o subconjunto que hoje derruba o inbox com `IntegrityError`, sem tocar em nenhum teste
protegido), e **registrar explicitamente que P6 permanece parcialmente aberto** para
`AI_TRIAGE` e `CLOSED`. Não marcar R10 como resolvido.

### 0.5 Atenuante real para o subconjunto `AI_TRIAGE` (por que ele não é urgente)

Isto não anula §0.4, mas dimensiona o risco — e é a razão pela qual B.4 pode continuar de pé como
tripwire sem ser um bug de produção ativo.

`conversas/app/v2/gate.py`, `autorizar_envio` (L131-171), é a última barreira antes de a IA falar.
Ele recusa por **cinco** condições, e duas delas são exatamente a pegada que a V1 deixa:

3. `is_bot_active is False` → `BOT_INATIVO`
4. `atendente_id is not None` → `HUMANO_ASSUMIU`

E `conversas/app/services/atendimento.py`, `aplicar_estado_humano` (L170-179), escreve
**`conversation.is_bot_active = False` incondicionalmente**, em *todos* os caminhos — claim,
assign, release, handoff, initiate (o docstring L157-158 declara isso: "a única volta para a BIA é
a REABERTURA"). `marcar_atendimento_humano` (L235) faz o mesmo.

Consequência: **para o R8 stricto sensu — invalidar resposta da IA em voo — `state_version` é
defesa redundante no caso `AI_TRIAGE`.** Qualquer operação V1 de propriedade humana já liga o
latch `is_bot_active=False`, que é monotônico durante todo o ciclo V1 (nem o `release` religa o
bot), e o gate o observa diretamente na releitura fresca. Há ainda uma segunda checagem em
`conversas/app/v2/pipeline.py:344` (`if not conversation.is_bot_active: return BOT_INATIVO`).

**Furo residual identificado no próprio gate (achado novo, independente do escopo do barrier):**
`autorizar_envio` **não checa `status`**. Um `PUT /api/conversations/{id}` com
`{"status":"encerrada"}` isolado (sem claim antes) fecha a conversa **sem** desligar o bot —
`update_conversation` (L1108-1116) escreve só `conversation.status` nesse ramo. Numa conversa
`AI_TRIAGE` com bot ligado, as cinco condições passam e o gate **autoriza a IA a responder numa
conversa que um humano acabou de encerrar**. Este é um caso em que `state_version` seria a única
defesa — e é exatamente o que B.4 proíbe. Vale registrar como item próprio; a correção mais barata
e menos invasiva é acrescentar uma sexta condição ao gate (`status != 'aberta'`), que vive em
`v2/gate.py` (**não protegido**) e não toca nenhum dos 8 arquivos nem nenhum teste da Família B.

---

## 1. Inventário de writers

Metodologia: `graphify explain`/`query` para orientação, depois varredura própria por atribuição
(`\.campo *= `), `update(Conversation)` / `sa_update`, e `UPDATE conversations` em SQL cru, sobre
`conversas/`, `app/`, `scripts/` e `migrations/`. `tests/` fora do escopo detalhado.

**Legenda de classe:** **A** = necessário para LEGACY, não deve escrever em conversa sob V2 ·
**B** = necessário para LEGACY **e** V2, precisa de helper compatível · **C** = pode ser
desativado/bypassado quando sob V2 · **D** = morto/inalcançável.

**Legenda `Prot.`:** ✋ = arquivo entre os 8 protegidos por hash (Família A) + AST/grep (Família B.1).
**Um call site novo dentro de um arquivo ✋ exige HOTFIX formal (hash recalculado à mão + procedência
registrada). Não existe atalho.**

### 1.1 Conversas — `Conversation`

| # | Arquivo:linha | Função | Prot. | Campos escritos | Quem aciona | V1/V2 | Necessário no rollout? | Pode ser só LEGACY? | Pode virar no-op sob V2? | Precisa helper? | Risco de concorrência | Classe |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `conversas/app/v2/estados.py:406-408` | `aplicar_transicao` | | `state`, `state_version`, `closed_at` | Todo caminho V2 (`handoff`, `pipeline`, workers) | **V2** | Sim | Não | Não | **É o motor** | Puro, sem sessão; caller commita | — |
| 2 | `conversas/app/services/atendimento.py:170-179` | `aplicar_estado_humano` | ✋ | `atendente_id`, `is_bot_active`, `queued_at`, `primeira_resposta_humana_at` | claim, assign, release, handoff, PUT `/{id}`, **e `v2/handoff.py:758`** | **Ambos** | **Sim** | **Não** — a V2 já o reusa | Não | **Sim (indireto)** | Callers usam `_lock_conversation`; **`update_conversation` NÃO** | **B** |
| 3 | `conversas/app/services/atendimento.py:228-237` | `marcar_atendimento_humano` | ✋ | `status`, `primeira_resposta_humana_at`, `queued_at`, `is_bot_active`, `atendente_id` | `outbound.record_outbound_message:255`; `conversations.py:1734` (retry) | V1 | Sim | Não | Não | **Sim** | Sem lock no caminho de outbound | **B** |
| 4 | `conversas/app/routers/conversations.py:1470` | `claim_conversation` | ✋ | via #2 | `POST /{id}/claim` | V1 | Sim | Não | Parcial | Sim | `_lock_conversation` (`FOR UPDATE` fora do SQLite) | **A** |
| 5 | `conversas/app/routers/conversations.py:1504` | `assign_conversation` | ✋ | via #2 | `POST /{id}/assign` | V1 | Sim | Não | Parcial | Sim | `_lock_conversation` | **A** |
| 6 | `conversas/app/routers/conversations.py:1533` | `release_conversation` | ✋ | via #2 (`resetar_atendimento=True`) | `POST /{id}/release` | V1 | Sim | Não | Parcial | Sim | `_lock_conversation` | **A** |
| 7 | `conversas/app/routers/conversations.py:1395` | `handoff_conversation` | ✋ | via #2 | `POST /{id}/handoff`, `POST /by-lead/{id}/handoff` (n8n) | V1 | Sim | Não | **Sim** — a V2 tem `handoff_to_human` | Sim | `_lock_conversation` | **C** |
| 8 | `conversas/app/routers/conversations.py:1116` | `update_conversation` | ✋ | `status` | `PUT /{id}` (encerrar/reabrir) | V1 | Sim | Não | Não | **Sim** | **SEM LOCK** (L1099 é `.first()` puro) | **B** |
| 9 | `conversas/app/routers/conversations.py:1127-1130` | `update_conversation` — ramo "religar a BIA" | ✋ | `is_bot_active=True`, `atendente_id=None`, `queued_at=None`, `primeira_resposta_humana_at=None` | `PUT /{id}` com `is_bot_active=true` (toggle da UI, `static/js/conversas.js:648`) | V1 | Sim | **Deveria** | **Sim** | **Sim** | **SEM LOCK** | **A** (alto risco) |
| 10 | `conversas/app/routers/conversations.py:1135` | `update_conversation` | ✋ | via #2 | `PUT /{id}` com `atendente_id`/`is_bot_active=false` | V1 | Sim | Não | Parcial | Sim | **SEM LOCK** | **B** |
| 11 | `conversas/app/routers/conversations.py:412-417` | `_apply_responsavel` | ✋ | `responsavel_id` (+`responsavel_nome`) | `PUT /{id}`, `PUT /{id}/responsavel` | V1 | Sim | Não | Não | **Sim** | Sem lock; sincroniza CRM na mesma transação | **B** |
| 12 | `conversas/app/routers/conversations.py:444-451` | **`_repair_responsavel_cache`** | ✋ | `responsavel_id` (via `sa_update` Core) | **`GET /api/conversations` (L729) e `GET /{id}?opening=true` (L1038)** | V1 | Sim | **Sim** | **Sim** | **Sim** | **Sem lock, em rota de LEITURA, em polling** | **A** — ver §2 |
| 13 | `conversas/app/routers/conversations.py:545-555` | `initiate_conversation` | ✋ | `status='aberta'`, `is_bot_active=False`, `atendente_id=current_user.id`, `primeira_resposta_humana_at=now()` (kwargs do construtor) | `POST /initiate` | V1 | Sim | n/a | n/a | Não | Cria linha nova (`state` = default `LEGACY`) | **A** |
| 14 | `conversas/app/routers/webhook.py:644-651` | `_process_incoming_message` — **reabertura** | ✋ | `atendente_id=None`, `is_bot_active=True`, `queued_at=None`, `primeira_resposta_humana_at=None` (L644-650, **só se reabrindo**) + **`status='aberta'` (L651, INCONDICIONAL** para toda conversa existente) | Webhook Meta (mensagem do cliente) | V1 | Sim | **Deveria** | **Sim** | **Sim** | Alto — tráfego externo, concorrente | **B** (alto risco) |
| 14b | `conversas/app/routers/webhook.py:586-594` | `_process_incoming_message` — criação | ✋ | `status='aberta'` (kwarg do construtor) | Webhook Meta (número novo) | V1 | Sim | n/a | n/a | Não | Linha nova (`state` = default `LEGACY`); `UNIQUE` em `whatsapp` é a trava | **A** |
| 14c | `conversas/app/routers/webhook.py:1212` | `_forward_to_agent` — **modo degradado** | ✋ | via #2, com `atendente_id=None` → enfileira e desliga o bot | `POST /webhook` → `_schedule_agent_debounce` → `_debounce_then_forward`; dispara quando o agente n8n está degradado **e** `primeira_resposta_humana_at is None` | V1 | Sim | Não | **Sim** | **Sim** | Alto — assíncrono, fora do request, sem lock | **B** (alto risco) |
| 15 | `conversas/app/services/crm.py:583-585` | `auto_link_conversation` | ✋ | `responsavel_id` (+`lead_id`, `responsavel_nome`); **commita em L591** | `POST /{id}/auto-link` (L1760); vínculo automático | V1 | Sim | Sim | Sim | Sim | Commit próprio, sem lock | **A** |
| 16 | `conversas/app/v2/handoff.py:758-764` | `_aplicar_estado_local` | | via #2 + `responsavel_id` + #1 | `handoff_to_human` (Fase 6) | **V2** | Sim | Não | Não | **É o precedente do helper** | `db.refresh(..., with_for_update=True)` fora do SQLite | — |
| 17 | `migrations/m008_conversas_queued_at.py:102` | migration | | `is_bot_active` (UPDATE em massa) | Execução humana, fora do runtime | — | n/a | n/a | n/a | Não | Manual, com backup | **D** (runtime) |
| 18 | `migrations/m012_conversas_primeira_resposta_humana.py:157,176` | migration | | `primeira_resposta_humana_at`, `queued_at` | Execução humana | — | n/a | n/a | n/a | Não | Manual | **D** (runtime) |

### 1.2 CRM — `Lead.responsavel_id`

| # | Arquivo:linha | Função | Prot. | Quem aciona | V1/V2 | Concorrência | Classe |
|---|---|---|---|---|---|---|---|
| 19 | `app/routers/leads.py:1116` | `update_lead_responsavel` | ✋ | `PUT /api/leads/{lead_id}/responsavel` — **é o endpoint que o n8n chama** (`n8n/workflows/Agente Gerenciador de Leads — BnA (1).json:582`, nó "Tool Alterar Responsavel", com `responsavel_id=5` fixo — o defeito citado em `atendimento.py:8-9`) e o seletor da UI do CRM | V1 | **Nenhuma** — `.first()` puro, sem `with_for_update`, sem CAS. Só atomicidade transacional com o `LeadHistory` (L1133) | **A** — é a **fonte** da divergência do §2 |
| 19b | `app/services/lead_creation.py:409` | `criar_lead` | ✋ | `POST /api/leads` (único caminho que passa valor real); `POST /api/leads/import` e a AI-tool `ai_tools.py:635` sempre passam `None` | V1 | INSERT de linha nova — sem alvo de corrida | **A** |
| 19c | `conversas/app/services/crm.py:224` | `sync_responsavel_to_crm` | ✋ | `conversations.py:420` (`_apply_responsavel`) ← `PUT /api/conversations/{id}` e `PUT /{id}/responsavel`. **Escreve o `leads` do CRM por SQL cru no banco compartilhado**: `UPDATE leads SET responsavel_id = :resp_id WHERE id = :lid` | V1 (**proibido para a V2** — `worker_reconciliacao.py:26-36`, com teste AST em `test_v2_workers.py`) | **Nenhuma** — SELECT sem lock + UPDATE sem CAS. Não commita: partilha a transação com a escrita local (#11) | **A** |
| 20 | `app/routers/v2_lead_domain.py:732` | `aplicar_handoff` | | `POST /api/v2/lead-domain/handoff`, chamado por `conversas/app/v2/handoff.py:609` | **V2** | `with_for_update().populate_existing()` + `SET LOCAL lock_timeout='3s'` + CAS + idempotência por `event_id` | — |
| 21 | `conversas/app/v2/worker_reconciliacao.py` (`_corrigir_crm_para_conversas`, L388; `_convergir_conversas_para_crm`, L327) | reconciliação | | Worker V2 | **V2** | Não escreve direto — delega a #20 pelo HTTP guardado | — |

> **Nota de escopo.** Os itens 20 e 21 pertencem ao mecanismo CAS/idempotência R15/R16/R17, que me
> foi instruído a não tocar. Estão listados apenas porque são writers do campo e omiti-los tornaria
> o inventário falso. **Nenhuma recomendação deste documento os altera.**

**Guardas existentes que NÃO são lacunas (registradas para não serem "corrigidas" por engano):**

- `app/routers/leads.py:642-650` — o `PUT /api/leads/{lead_id}` genérico **recusa** `responsavel_id`
  no corpo com 422, justamente para forçar o caminho que grava `LeadHistory` e notifica o Conversas.
  O `setattr` genérico da L671-672 nunca alcança o campo.
- **Não existe endpoint de atribuição em massa** de `responsavel_id` em `leads.py`, `pipeline.py` nem
  `segments.py` (verificado). `move_lead_stage`/`transfer_lead` mexem só em `FunnelEntry`.
- `app/services/conversas_bridge.py` **não escreve `responsavel_id`** — `notificar_handoff` (L52-100)
  é só um notificador CRM→Conversas, best-effort, chamado **depois** do commit de #19 (L1170).
- `conversas/app/services/crm.py:268-277` — `auto_create_lead_in_crm` omite a coluna no INSERT.
- `conversas/app/services/atendimento.py:167` — comentário explícito: não toca `responsavel_id`
  ("responsabilidade COMERCIAL e do CRM").
- `scripts/` e `migrations/` — **nenhuma escrita** em `leads.responsavel_id` (m018/m019 só adicionam
  colunas de ledger).

### 1.3 Confirmação do R8 — resposta direta

**Confirmado, com varredura exaustiva: nenhum código de aplicação legado escreve `state_version`.
Nem `state`. Nem `closed_at`.**

As **únicas** atribuições a essas três colunas em código de aplicação, em todo o repositório e nos
dois apps, são `conversas/app/v2/estados.py:406`, `:407` e `:408`, dentro de `aplicar_transicao`.
Não há `update(Conversation).values(...)` sobre elas, não há `onupdate=` no model, e **não existe
trigger nenhum** — `grep` por `CREATE TRIGGER`/`CREATE FUNCTION` em `migrations/` dá **zero
ocorrências**. `state` é `String(32)` livre e `state_version` é `Integer` com
`server_default=text("0")`; `conversation.py:116-124` documenta a escolha deliberada de não pôr
CHECK de vocabulário no schema (a recusa é da máquina, decisão 10).

**Uma ressalva de precisão, para o inventário não ser falso:** existe SQL cru tocando `state` e
`state_version` **fora** de `app/v2/` — `migrations/m014_conversas_state_columns.py:167-171`
(`UPDATE conversations SET state = 'LEGACY' WHERE state IS NULL` e o equivalente para
`state_version = 0`). É a **própria migration que cria as colunas**, é idempotente, é **no-op no
caminho feliz** (o `ADD COLUMN ... DEFAULT` já preenche as linhas existentes; o `UPDATE` só existe
para reparar execução parcial anterior), roda **por mão humana fora do startup**, e nunca é
invocada em runtime. Não é um writer legado no sentido do R8 — mas dizer "não há SQL cru" sem
qualificar seria impreciso. `closed_at` **não tem nem essa exceção**: nenhum backfill o toca (o
docstring de m014 diz que `NULL` já é o valor correto), e seu único writer em todo o repositório é
`estados.py:408`.

Portanto o enunciado de R8 está correto e a Família B.4 é uma trava honesta: hoje o incremento de
`state_version` é **exclusivo** do motor V2, e nenhuma escrita operacional da V1 o acompanha.

### 1.4 Resumo por classe

- **Classe A** (LEGACY apenas, não deve tocar conversa sob V2) — #4, #5, #6, #9, #12, #13, #14b,
  #15, #19, #19b, #19c.
- **Classe B** (precisa de helper compatível) — #2, #3, #8, #10, #11, #14, #14c.
- **Classe C** (desativável sob V2) — #7 (`handoff_conversation`, e por tabela também
  `handoff_by_lead`, que delega a ele em `conversations.py:1444`): a V2 tem `handoff_to_human`, que
  é o caminho correto; a rota V1 vira redundante quando a conversa está sob V2.
- **Classe D** (morto/inalcançável em runtime) — #17, #18 (migrations, execução humana pontual) e
  `migrations/m014:167-171` (§1.3). **Nenhum writer de aplicação foi classificado como D.**
  Verifiquei cada candidato por `graphify explain` + grep de call site antes de descartar; todos os
  writers de aplicação listados têm caminho de chamada real. Não há "código morto" a eliminar aqui.

**Confirmados negativos (varridos, sem writer):** `conversas/app/routers/notes.py`, `tags.py`,
`variables.py`, `conversas/app/main.py`, `conversas/app/v2/worker_fila.py` (só mexe em
`HandoffOperation`), `conversas/app/v2/gate.py` (**só lê** — recusa via evento, nunca muta a
conversa), `conversas/app/v2/pipeline.py` (só lê; delega toda escrita a `handoff_to_human`).
Nenhum arquivo sob `app/` (CRM) escreve `Conversation.*` — o CRM fala com o Conversas por HTTP
(`conversas_bridge.py`) e só escreve `Lead.responsavel_id`. Nenhum workflow n8n contém SQL cru.
`scripts/` só tem `smoke_seed_conversas.py` (seed de dev).

**Observação estrutural que decide o §3:** de 19 writers de aplicação, **17 estão dentro dos 8
arquivos protegidos**. Só #16 (V2) e #20/#21 (V2) estão fora. Isso é o fato dominante do desenho.

---

## 2. `_repair_responsavel_cache`

### 2.1 (a) O revert está intacto — confirmado

**Sim, confirmado por três evidências independentes:**

1. **Leitura direta.** `conversas/app/routers/conversations.py:432-451`: a função lê
   `lead_resp["responsavel_id"]`/`["responsavel_nome"]`, compara com o cache, e se divergir emite
   `sa_update(Conversation)...values(responsavel_id=..., responsavel_nome=..., updated_at=...)` +
   dois `set_committed_value`. **Nenhuma referência a `state`, `state_version` ou `closed_at`;
   nenhuma ramificação sobre estado V2.**
2. **Execução do próprio guarda.** `python tests/test_v2_v1_intacta.py` → o hash da Família A para
   `conversas/app/routers/conversations.py` bate exatamente
   (`0d7463e2128862cc5260481b9826e29c56b8e8294895cca5ef5b8555636d8e6f`), e as duas checagens B.1
   (AST e grep) passam para esse arquivo. **O arquivo está byte-a-byte na baseline registrada.**
   Reconferido **depois** da alteração de `atendimento.py` descrita no aviso do topo: este arquivo
   específico continua verde, então a conclusão do §2 não é afetada por ela.
3. **Procedência documentada.** `bia-v2-r15-r17-design-v4.md` §4.1 registra que a correção (pular
   conversas com `state IN ('WAITING_HUMAN','HUMAN_ACTIVE')` dentro da função) **chegou a ser
   implementada e foi revertida por decisão explícita do usuário** — não por estar tecnicamente
   errada, mas porque fazia "exatamente o que a Família B.1 existe para impedir: um writer V1
   passando a ramificar sobre estado V2". O item permanece registrado como residual sob R10.

### 2.2 (b) Call sites reais — todos, e por quê

Apenas **dois** call sites em código de produção, ambos em **rotas de LEITURA**:

| Call site | Rota | Guarda | Frequência |
|---|---|---|---|
| `conversations.py:729` (dentro de `list_conversations`) | **`GET /api/conversations`** | Roda depois de filtro/count/paginação; só se `get_leads_responsaveis` devolver dict não-vazio. `db.commit()` em L740 **só se houve reparo** | **Polling do inbox** — a cada refresh da lista |
| `conversations.py:1038` (dentro de `get_conversation`) | **`GET /api/conversations/{id}`**, apenas com `opening=true` (que é o **default**, L991) | Só se `conversation.lead_id > 0` e o lookup no CRM retornar | Abertura de conversa; o poll de 5s manda `opening=false` para pular (L1010-1013) |

Fonte do dado: `conversas/app/services/crm.py:599-633`, `get_leads_responsaveis` — `SELECT l.id,
l.responsavel_id, u.nome FROM leads l LEFT JOIN users u ...`, ou seja **lê a tabela `leads` do CRM
no banco compartilhado**. O CRM é a fonte de verdade do responsável comercial.

### 2.3 A cadeia de dano completa (não é hipótese — é mecânica)

1. `PUT /api/leads/{id}/responsavel` (writer #19 — n8n ou UI do CRM) grava `lead.responsavel_id = X`.
   Sem lock, sem CAS, sem qualquer noção de que existe uma conversa em estado humano.
2. A conversa correspondente está em `WAITING_HUMAN`/`HUMAN_ACTIVE`, onde a invariante 4 exige
   `atendente_id = responsavel_id = Y`. Agora `X ≠ Y`.
3. **A divergência não se autocura pelo bridge — e isso é contraintuitivo.** Depois do commit, #19
   chama `conversas_bridge.notificar_handoff` (`leads.py:1170`), que faz `POST
   /api/conversations/by-lead/{lead_id}/handoff` → `handoff_conversation` (#7). Mas esse handoff,
   em `conversations.py:1389-1391`, **preserva o dono atual se já houver um**
   (`if conversation.atendente_id is None: ... else: atendente_id = conversation.atendente_id`) e o
   docstring L1385 declara que ele "NAO escreve responsavel_id/responsavel_nome nem toca a tabela
   `leads`". Resultado: `atendente_id` continua `Y`, `lead.responsavel_id` é `X`. **A notificação
   confirma a divergência em vez de resolvê-la.**
4. Próximo `GET /api/conversations` → `get_leads_responsaveis` devolve `X` → `_repair_responsavel_cache`
   detecta divergência e escreve `conversation.responsavel_id = X` (**sem tocar `atendente_id`**).
5. `db.commit()` em L740 → **`ck_conversations_estado_humano` violado → `IntegrityError`**.
6. O erro derruba **a listagem inteira do inbox**, não só aquela conversa — e **repete a cada
   ciclo de polling** enquanto a divergência existir.

> Note o contraste com o caminho simétrico: quando a troca de responsável entra **pelo Conversas**
> (`PUT /api/conversations/{id}/responsavel` → `_apply_responsavel` #11 + `sync_responsavel_to_crm`
> #19c), as duas escritas ficam na **mesma transação** (`crm.py:191-204` documenta que a função não
> commita justamente para isso), e os dois lados ficam coerentes. **É só a entrada pelo CRM (#19)
> que produz a divergência.** Isso é o que torna R2.2 (barrar na origem) a mitigação certa.

**Verificação de que não é dano no caminho feliz:** `conversas/app/v2/handoff.py:9-10` e `:759-762`
declaram que "um único `target_user_id` alimenta `lead.responsavel_id` (no CRM),
`conversation.atendente_id` e `conversation.responsavel_id`". No handoff V2 normal os três são
iguais, `_repair_responsavel_cache` cai no early-return da L442-443 e é no-op. **O dano exige que
alguém reatribua o lead no CRM enquanto a conversa está em estado humano** — que é justamente o que
o endpoint do n8n faz.

**Resposta à hipótese "talvez só dispare para conversas LEGACY":** *investigado, e é falso.* A
função não filtra por `state` nem por nada correlacionado a ele; o único filtro é
`lead_id > 0` + divergência de valor. Ela roda sobre **todas** as conversas da página, em qualquer
estado. Em `AI_TRIAGE` e `LEGACY` ela é inofensiva (o CHECK é inerte e `AI_TRIAGE` não restringe
`responsavel_id` — §0.2); em `WAITING_HUMAN`/`HUMAN_ACTIVE` ela é o writer mais perigoso do
inventário, porque roda **sem lock, em rota GET, em polling**.

### 2.4 (c) Recomendação — limitar o raio de explosão sem tocar nos 8 arquivos

Ordenadas por custo. **Nenhuma delas esconde a referência de coisa nenhuma** — é exatamente o que a
tentativa revertida fazia de errado.

**R2.1 — Encurtar a janela de divergência (mais barato, zero risco, recomendado já).**
A divergência é transitória por natureza; o dano é proporcional ao tempo que ela dura. O worker
`conversas/app/v2/worker_reconciliacao.py` (**não protegido**) já existe e já reconcilia
CRM↔Conversas. Reduzir seu intervalo e garantir que ele cubra o caso `lead.responsavel_id ≠
conversation.atendente_id` encolhe a janela sem alterar uma linha de V1. **Não muda comportamento
síncrono nenhum → B.3/B.4 inalterados.**

**R2.2 — Barrar na origem, não no destino (recomendado).**
O writer que *cria* a divergência é #19 (`PUT /api/leads/{id}/responsavel`). Ele está em
`app/routers/leads.py`, que também é ✋ — mas a diferença crucial é que **do lado do CRM já existe
um caminho V2 não-protegido**: `app/routers/v2_lead_domain.py`. Encaminhar a reatribuição de lead de
conversa-sob-V2 por lá (ou recusá-la enquanto houver `handoff_operation` ativa) resolve o problema
onde ele nasce, e `_repair_responsavel_cache` volta a ser o no-op que é no caminho feliz.
**Custo:** exige decidir o comportamento de produto quando um humano tenta reatribuir no CRM um
lead cuja conversa está em atendimento — isso é decisão do dono, não técnica.

**R2.3 — Guard de sessão fora do arquivo protegido (cobertura ampla, com uma ressalva honesta).**
Um listener SQLAlchemy `before_flush` registrado fora dos 8 (em `conversas/app/main.py` ou em
`app/models/conversation.py`, nenhum dos dois protegido) pode recusar/corrigir qualquer flush que
deixe uma linha `WAITING_HUMAN`/`HUMAN_ACTIVE` incoerente. **Isto não é trigger de banco** — é
código Python, na aplicação, respeitando a preferência do dono.
**Ressalva que não deve ser omitida:** `before_flush` observa mutações **do ORM**. O
`_repair_responsavel_cache` usa `sa_update(Conversation)` — statement **Core** — e por isso
**não seria visto por `before_flush`**. Cobri-lo exigiria um listener de `do_orm_execute`/
`before_execute` no engine, que é mais frágil e tem raio global. **Portanto R2.3 sozinho NÃO
resolve o `_repair_responsavel_cache`** — resolve os writers #2, #3, #8, #10, #11, #14. Dizer o
contrário seria vender cobertura que não existe.

**R2.4 — HOTFIX formal (a única correção completa e direta).**
Reintroduzir o skip dentro da função, com o processo completo: diff provado, revisão, hash da
Família A recalculado à mão, procedência registrada. **É a única opção que fecha o item de vez.**
Foi explicitamente rejeitada uma vez pelo dono. **Não a re-proponho como decisão técnica — a
registro como a opção que existe, cuja escolha é do dono do projeto.**

**Recomendação combinada:** R2.1 + R2.2 agora (nenhuma toca arquivo protegido, nenhuma altera
comportamento síncrono da V1, nenhuma mexe em teste); R2.3 como parte do §3; R2.4 só por decisão
explícita do dono.

---

## 3. Desenho do helper de compatibilidade (recomendação, não implementação)

### 3.1 Nome

**`aplicar_escrita_operacional`**, em um módulo novo `conversas/app/v2/compat.py`.

Justificativa do nome: o repo já usa o verbo `aplicar_` para exatamente esta categoria de operação
(`aplicar_estado_humano`, `aplicar_transicao`, `_aplicar_estado_local`), e "escrita operacional" é o
termo que o próprio plano usa (L2061: *"toda escrita operacional sobre uma conversa controlada pela
V2"*). Domínio em PT-BR, consistente com `nome`/`destinos`/`responsavel_id`.

### 3.2 O precedente já existe — o helper é a generalização dele

`conversas/app/v2/handoff.py`, `_aplicar_estado_local` (L746-766), já faz **exatamente** o que o
enunciado pede, e é o molde a seguir:

```
db.refresh(conversation, with_for_update=True)   # lock de linha (ramo IS_SQLITE)
aplicar_estado_humano(conversation, target_user_id, keep_queue_position=True)
conversation.responsavel_id = target_user_id
aplicar_transicao(conversation, Estado.WAITING_HUMAN, motivo=reason)
db.flush()   # o CHECK falha AQUI, no lugar certo
```

Três propriedades desse molde que o helper deve preservar, e que resolvem os requisitos do enunciado:

- **Incremento de `state_version` na MESMA transação da mutação.** `aplicar_transicao` só muta o
  objeto em memória; **nunca commita**. Quem commita é o caller, **uma vez**. Não há segunda
  transação, logo não há janela de corrida. Isto atende literalmente o requisito.
- **Atomicidade sem `SAVEPOINT`.** As duas sessões são `autoflush=False`
  (`conversas/app/database.py:47` e `app/database.py:43`), então nada é flushado até o `db.flush()`
  final: se `aplicar_transicao` levantar `TransicaoInvalida`, **nada foi ao banco**. Isto é
  **obrigatório**: `.claude/memory/pysqlite-savepoint-rollback-gap.md` documenta que
  `db.begin_nested()` **não desfaz** num `db.rollback()` posterior no SQLite deste repo. **O helper
  não pode usar `begin_nested()`.**
- **Não duplica o motor de regras.** Toda a validação de aresta, invariante e `closed_at` continua
  em `estados.py`. O helper só decide *qual destino* a operação implica.

### 3.3 Assinatura e semântica propostas

```python
# conversas/app/v2/compat.py   (NOVO — dentro de v2/, logo fora do escopo da Família A e B.1)

class OperacaoOperacional(StrEnum):
    """Vocabulário FECHADO das escritas operacionais da V1, uma por rota/efeito."""
    ASSUMIR       = "ASSUMIR"        # claim
    ATRIBUIR      = "ATRIBUIR"       # assign / handoff dirigido
    DEVOLVER      = "DEVOLVER"       # release
    RESPONDER     = "RESPONDER"      # primeira resposta humana
    ENCERRAR      = "ENCERRAR"       # PUT status='encerrada'
    REABRIR       = "REABRIR"        # webhook / PUT status='aberta'
    DEVOLVER_A_IA = "DEVOLVER_A_IA"  # toggle "religar a BIA"
    TROCAR_DONO_COMERCIAL = "TROCAR_DONO_COMERCIAL"  # responsavel_id

@dataclass(frozen=True)
class ResultadoEscrita:
    aplicada: bool
    sob_v2: bool
    destino: Estado | None
    divergencia: str | None     # token fechado, p/ diagnóstico; nunca dado de cliente

def aplicar_escrita_operacional(
    db: Session,
    conversation: Conversation,
    *,
    operacao: OperacaoOperacional,
    atendente_id: int | None = None,
    responsavel_id: int | None = None,
    motivo: str,
) -> ResultadoEscrita:
    ...
```

**Semântica, por faixa de `state` — este é o coração do desenho:**

| `state` lido | Comportamento | Classe atendida |
|---|---|---|
| `LEGACY` | **Passthrough puro.** Aplica a mutação V1 exatamente como hoje, via `aplicar_estado_humano`/atribuição direta. **Não toca `state`, `state_version`, `closed_at`.** Retorna `sob_v2=False`. | **A** |
| `AI_TRIAGE` | **Passthrough puro, idêntico ao ramo `LEGACY`.** Opcionalmente registra um evento de divergência para o reconciliador. **Este ramo é o que preserva B.3 e B.4 literalmente.** | **A/C** |
| `WAITING_HUMAN`, `HUMAN_ACTIVE` | **Ramo V2.** (1) `db.refresh(..., with_for_update=True)` fora do SQLite; (2) snapshot dos 9 campos; (3) aplica as mutações operacionais; (4) mapeia `operacao → destino` e chama `aplicar_transicao(conversation, destino, motivo=motivo)`; (5) `db.flush()`. Em `TransicaoInvalida`, **restaura o snapshot** e propaga — nada foi flushado (`autoflush=False`). **Nunca commita.** | **B** |
| `CLOSED` | Só `REABRIR` é significativa; delega o destino a `decidir_reabertura` (que já existe em `estados.py`) e segue o ramo V2. Demais operações: recusa explícita. | **B** |
| `None` ou fora do vocabulário | **Ponto de decisão do dono.** A decisão 10 da máquina é *fail closed*; mas levantar aqui transformaria uma linha corrompida num 500 numa rota V1 que hoje funciona. **Recomendo passthrough + evento de diagnóstico**, e marco isto como decisão pendente, não como escolha minha. | — |

**O que o helper NÃO faz, deliberadamente:** não commita; não abre sessão; não usa `begin_nested()`;
não cria trigger de banco; não reimplementa nenhuma regra de `estados.py`; não espalha
`if state == ...` por dezenas de arquivos — a ramificação existe **em um único lugar**, este.

### 3.4 Quem chamaria — e quem não chamaria, com o motivo

**Chamariam (classe B):** #2 `aplicar_estado_humano`, #3 `marcar_atendimento_humano`, #8 e #10
`update_conversation`, #11 `_apply_responsavel`, #14 reabertura do webhook, #14c `_forward_to_agent`
em modo degradado.

**NÃO chamariam:**
- #1 `aplicar_transicao` — **é** o motor; seria recursão.
- #16 `_aplicar_estado_local` — já faz o certo; é o molde.
- #12 `_repair_responsavel_cache` — ver §2; é `sa_update` Core e o call site é intocável.
- #13 `initiate_conversation`, #14b criação pelo webhook, #15 `auto_link_conversation` —
  criam/vinculam; a linha nasce `LEGACY`, não há estado V2 a preservar.
- #17, #18, m014 — migrations, execução humana, fora do runtime.
- #19, #19b, #19c — lado CRM/`leads`; o helper é do domínio `Conversation`. A coerência do
  `leads.responsavel_id` é problema de #20 (que já tem CAS) e do §2.
- #20/#21 — mecanismo R15/R17, fora deste escopo por instrução.

### 3.5 ⚠️ O obstáculo que decide a viabilidade — leia isto

**17 dos 19 writers de aplicação vivem dentro dos 8 arquivos protegidos.** Adicionar uma chamada a
`aplicar_escrita_operacional` dentro de qualquer um deles **é** uma mudança nesses arquivos e
**exige HOTFIX formal** (hash recalculado, procedência registrada). Não há truque que contorne isso:
um import indireto, um wrapper, um `getattr` dinâmico — qualquer coisa que faça um arquivo ✋ passar
a ramificar sobre estado V2 é exatamente o que a Família B.1 existe para impedir, e foi exatamente o
que já foi revertido uma vez (§2.1).

Portanto, **honestamente**: o helper, sozinho, **não pode ser adotado pelos writers que mais
precisam dele** sem HOTFIX. O que é implementável hoje sem tocar em arquivo protegido é:

1. **O helper em si** (`v2/compat.py`), disponível para todo código novo e para o lado V2.
2. **O guard `before_flush`** de R2.3 — que cobre #2, #3, #8, #10, #11, #14 e #14c **sem editar
   nenhum dos 8**, porque intercepta no flush, não no call site. É o caminho de maior cobertura por
   menor custo político. (Ressalva do §R2.3: não cobre o `sa_update` Core do #12.)
3. **A sexta condição do gate** (`status != 'aberta'`), em `v2/gate.py`, não protegido (§0.5).
4. **R2.1 + R2.2** para o `_repair_responsavel_cache`.

A adoção in-place do helper pelos 14 writers protegidos é um **HOTFIX planejado**, e deve ser
proposta como tal — não embutida numa tarefa de R8/R10.

---

## 4. O que deve fazer você parar e perguntar ao dono antes de implementar

1. **`AI_TRIAGE` é contradição, não lacuna.** O plano (L2049) manda corrigir `claim` sobre
   `AI_TRIAGE`; B.3/B.4 mandam preservá-lo. **Não dá para os dois.** Escolher "preservar B.3/B.4"
   significa **registrar P6 como parcialmente aberto** — não como resolvido. Precisa de decisão.
2. **A reabertura pelo webhook (writer #14) e o fechamento V1 (#8) deixam `closed_at`/`state`
   divergentes**, e o plano (L2051) diz que isso faz a conversa "reabrir sempre como se estivesse
   fora da janela". Também fora do escopo `WAITING_HUMAN`/`HUMAN_ACTIVE`. Segundo item de P6 aberto.
3. **`_repair_responsavel_cache` só fecha de verdade com HOTFIX (R2.4)**, que já foi rejeitado uma
   vez. R2.1/R2.2/R2.3 **mitigam**, não eliminam. Não deixe registrar como fechado.
4. **Três lacunas de lock pré-existentes, independentes de R8/R10** — não corrigi nem toquei;
   valem WP próprio, e a #4c é a que mais me incomoda:
   - **4a.** `update_conversation` (`PUT /{id}`) não pega lock de linha (L1099 é `.first()` puro),
     enquanto claim/assign/release/handoff pegam via `_lock_conversation`. O ramo "religar a BIA"
     (#9) roda **sem lock** e zera quatro campos operacionais de uma vez.
   - **4b.** `update_lead_responsavel` (#19) não pega lock nem CAS — e **`app/routers/leads.py:727`
     já tem um helper `_lock_lead()` com `with_for_update()`, no mesmo arquivo, usado só por
     `append_anotacao`.** O lock está a uma função de distância e não é usado justamente na escrita
     mais disputada do CRM. (`tests/test_leads_responsavel_atomic.py` prova atomicidade
     *transacional* entre o lead e o `LeadHistory` — **não** prova nada sobre corrida.)
   - **4c.** `sync_responsavel_to_crm` (#19c) escreve `UPDATE leads SET responsavel_id` **direto no
     Postgres compartilhado, sem `FOR UPDATE` e sem CAS**, a partir do app Conversas. O
     `worker_reconciliacao.py:26-36` já a declara **proibida para a V2** ("ACHADO DE SEGURANCA DA
     REVISAO"), com teste AST em `test_v2_workers.py` — mas ela continua **viva e acessível pelo
     caminho V1** (`PUT /api/conversations/{id}` e `PUT /{id}/responsavel`). Ou seja: existe uma
     escrita cross-app sem controle de concorrência que a própria V2 já classificou como insegura,
     e que nenhum guard impede a V1 de usar.
5. **`webhook.py:651` escreve `status='aberta'` INCONDICIONALMENTE** em toda conversa existente que
   recebe mensagem — não só na reabertura (o bloco condicional é L644-650). Numa conversa V2 em
   `CLOSED`, isso reabre operacionalmente sem mover `state` nem limpar `closed_at`. É o caso do
   plano L2051, e chega pelo tráfego externo da Meta.
6. **Furo do gate: `autorizar_envio` não checa `status`** (§0.5). Correção barata e fora dos 8
   arquivos, mas é mudança de comportamento da V2 e merece aprovação explícita.
7. **Nada aqui foi executado contra PostgreSQL.** Todo o raciocínio sobre `FOR UPDATE`,
   `IntegrityError` do CHECK e ordem de flush vale para o dialeto de produção; a suíte local roda
   SQLite, onde `with_for_update()` é no-op por desenho (`_lock_conversation` L328-331). O R12 do
   plano já exige prova em PostgreSQL real — **este desenho herda essa exigência**.
8. **Eu não executei B.2–B.5.** Este ambiente é o do CRM (Python 3.11) e falta `google-genai`, que é
   do Conversas. Família A e B.1 rodaram e passaram (26 OK / 0 FAIL). Quem implementar precisa rodar
   `python tests/test_v2_v1_intacta.py` no ambiente do Conversas (3.12), antes e depois.
