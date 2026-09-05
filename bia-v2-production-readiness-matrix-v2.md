# Matriz de prontidão para produção — R1 a R17 (v2)

**HEAD de referência:** `54f160f`. Fonte normativa: `docs/superpowers/plans/2026-08-29-bia-v2.md`,
seção "Registro de riscos" (linhas 82-100), citada literalmente onde indicado, mais
`bia-v2-r15-r17-design-v4.md` e `bia-v2-rb-proof.md` para R15/R16/R17.
Nenhuma solução nova proposta aqui além do que já está registrado nesses documentos.

**Mudanças desta v2 em relação à v1**: R15, R16 e R17 atualizados para
refletir a arquitetura de fechamento de R-B (CAS no CRM sob lock de linha),
provada empiricamente nesta rodada. R1-R14 inalterados desde a v1 — sem
mudança de bytes nessas linhas.

| # | Risco (resumo) | Bloqueia dry-run? | Bloqueia shadow? | Bloqueia canary? | Bloqueia produção? | Solução existente? | Trabalho ainda necessário | Teste que fecha o risco |
|---|---|---|---|---|---|---|---|---|
| **R1** | `HUMAN_ACTIVE` inalcançável sem alterar V1 (`queued_at=None` na 1ª resposta humana) | Não | Não | **Sim** (via P1/HOTFIX-01) | Sim | Não — HOTFIX-01 registrado, não implementado | Implementar HOTFIX-01 (PR próprio contra V1, aprovação própria) | Nenhum ainda — depende do HOTFIX existir primeiro |
| **R2** | V2 rotula conversa real + humano responde pela UI V1 → viola CHECK → 500 | Não | Não | **Sim** (consequência de R1) | Sim | Não — mesma dependência de HOTFIX-01 | Mesmo de R1 | Nenhum ainda |
| **R3** | Migration de coluna não aplicada antes do deploy V2 → `ProgrammingError` | Não | Não | Sim (gate de deploy) | Sim | Sim — `create_all()` nunca faz `ALTER`; gate de deploy futuro verifica a coluna | Definir o gate de deploy formal (fase futura, não implementada) | Verificação manual/gate de deploy, a definir |
| **R4** | Testes só em SQLite; `FOR UPDATE`/CHECK por ALTER não exercitados lá | Não | Não | Não (mitigado) | Não (mitigado) | Sim — concorrência testada via lógica de `state_version` direto, não via `FOR UPDATE`; CHECK exercitado via `create_all` em SQLite novo | Nenhum | `tests/test_v2_m015_gate.py`, testes de concorrência de `state_version` |
| **R5** | `resolver_atendente_elegivel` devolve `None` → sem atendente | Não | Não | Não (comportamento desejado) | Não | Sim — vira `HANDOFF_FAILED`/`SEM_ATENDENTE_ELEGIVEL`, testado | Nenhum | `test_v2_pipeline.py` cenário 1, `test_v2_e2e_bia.py` cenário C |
| **R6** | `conversation_events` cresce sem limite | Não | Não | Não | Baixa (≤50 conversas/dia hoje) | Parcial — medir antes de otimizar, decisão adiada deliberadamente | Definir política quando o volume justificar | N/A — decisão de negócio, não técnica |
| **R7** | Logging (Task 0.1) muda comportamento da V1 | Não | Não | Não | Não | Sim — só acrescenta handler/formatter ao Conversas, comportamento antes/depois comparado por teste | Nenhum | Teste de comparação da Task 0.1 |
| **R8** | `state_version` não é autoritativo enquanto V1 não o incrementa | Não | Não | **Sim** | Sim | Não — "exige desenho e aprovação próprios" (trigger de banco, incremento nos writers V1, ou hash derivado) | Desenho + aprovação + implementação, fora deste plano | Teste B.4 da Task 0.3 já documenta o risco de forma executável (não fecha, só prova) |
| **R9** | Conversa real promovida a V2 acidentalmente/em massa | Não | Não | **Sim** | Sim | Sim, estruturalmente — `LEGACY→AI_TRIAGE` nunca acontece automaticamente hoje | Implementar a operação explícita "Promoção LEGACY → V2" (P3, fora deste plano) | Teste da operação de promoção, a implementar junto dela |
| **R10** | `state` não autoritativo enquanto writers V1 não passam pela máquina V2 | Não | **Sim, condicionado** (shadow só libera se V2 não produz side effect) | **Sim** | Sim | Não — "exige desenho e aprovação próprios" (funil único de escrita, ou coerência atômica state+state_version+campos operacionais) | Desenho + aprovação + implementação (P6) | Nenhum ainda — é pré-requisito de desenho. **Confirmado nesta rodada**: o CAS de R-B protege V2×V2, não protege contra um writer V1 sobrescrever uma decisão V2 já commitada (provado em teste de deadlock dedicado) — R10 continua sendo pré-requisito independente, não subsumido pela solução de R-B |
| **R11** | INFO expõe PII do cliente em log | Não | Não | — (não é sobre canary) | **Sim** | Parcial — injeção de linha já resolvida (`FormatadorSeguro`); PII não | DEPLOY-GATE-01: revisão call-site por call-site, política escrita, testes por call site | Testes de call-site do DEPLOY-GATE-01, a implementar |
| **R12** | `commit=False` não honrado em SQLite (SAVEPOINT sem BEGIN) | Não (dev/CI apenas) | Não | Não (não afeta PostgreSQL) | Não | **Sim, PROVADO contra PostgreSQL real** — Opção A escolhida e executada nesta sessão (container efêmero, 5 cenários A-E) | Nenhum — gate da Fase 6 já satisfeito | `tests/test_v2_r12_postgres_proof.py` (diagnóstico, untracked, skip sem `DATABASE_URL_R12_PROOF`); `tests/test_v2_eventos.py` |
| **R13** | `conversation_events` sem política de retenção/expurgo de PII | Não | Não | Não | **Sim** (antes de eventos reais) | Não — decisão de negócio/compliance explicitamente adiada | Definir política de retenção, acesso, expurgo, revisão LGPD | N/A — decisão de negócio |
| **R14** | `TriageData.destinos` sem teto de cardinalidade | Não | Não | Não | Baixa hoje | Parcial — sem crash hoje (função pura); dois riscos prospectivos identificados | Definir teto quando a fase que reabrir a allowlist decidir | Nenhum ainda — não é bloqueante hoje |
| **R15** | Confirmação do CRM não compensada se transição local falhar permanentemente | Não | Não | **Sim** | Sim | **Desenho COMPLETO, revisado e corrigido nesta rodada** (`bia-v2-r15-r17-design-v4.md` seção 4): política híbrida determinística (direção A ou B conforme o tipo de falha), todos os campos definidos (`previous_responsavel_id`, `target_user_id`, `operation_id` derivado deterministicamente, transação que grava, idempotência, crash-durante-correção, replay), **provado empiricamente** (P5 + prova extra de crash durante correção + prova extra de CAS recusando correção obsoleta). Achado adicional fechado no processo: replay de operação já superada (token novo `CRM_OPERACAO_SUPERADA`). **Revisão de segurança (achado MEDIUM) e de concorrência (achado MEDIUM) corrigidos**: `operation_id` da direção B agora deriva de `event_id`+`target_user_id` (não só `event_id`, que colidia em conflito terminal permanente sob mudança de alvo); proibição explícita de usar o atalho pré-existente `sync_responsavel_to_crm` (contornaria o CAS por completo) | Implementar (migration CRM: `previous_responsavel_id`; código do worker de reconciliação; os 2 novos error tokens) | `tests/test_v2_e2e_distribuido.py` cenários C, H, K (definidos, não implementados) |
| **R16** | `event_id` do handoff reusado do `MESSAGE_RECEIVED` inviabiliza retry | Não | Não | **Sim** | Sim | **Desenho COMPLETO, revisado e corrigido nesta rodada**: worker de retomada totalmente especificado (execução, frequência 30s, exclusão entre instâncias via `FOR UPDATE SKIP LOCKED` — provado com 2 instâncias concorrentes, backoff via `clock_timestamp()` do banco — não do processo, teto de 5 tentativas, dead-letter, alerta por classe). Chama `handoff_to_human` diretamente com a mesma identidade — nunca reenvia `MESSAGE_RECEIVED`. **Revisão de concorrência (achado HIGH) corrigido**: `attempt_count` deixou de ser incrementado na transação de reclamação (antes da chamada de rede) e passou para a transação de desfecho (depois dela) — a especificação original deixava um crash-loop do worker esgotar o teto de 5 tentativas sem nunca ter tentado uma chamada real | Implementar (migration Conversas: `expected_responsavel_id`, `attempt_count`, `next_attempt_at`, `reconciled_at` + índice; o processo worker em si) | `tests/test_v2_e2e_distribuido.py` cenários E, F (definidos, não implementados) |
| **R17** | Duas operações de handoff (`event_id` diferentes) para a mesma conversa não são mutuamente exclusivas — split-brain CRM×Conversas | Não | Não | **Sim** (era a mais severa das três — agora a única cuja arquitetura de fechamento foi refutada e corrigida DUAS VEZES antes de convergir, e cuja especificação final ainda assim foi BLOQUEADA numa primeira revisão antes de fechar de verdade) | Sim | **Desenho COMPLETO, revisado e corrigido nesta rodada, incluindo R-B** (`bia-v2-r15-r17-design-v4.md`): caso intercalado e caso sequencial (fechados na v3) + **R-B fechado nesta rodada** via compare-and-swap sob `SELECT...FOR UPDATE` em `leads.responsavel_id`, dentro do CRM, sem lifecycle nem claim table. Hipótese alternativa (índice único parcial por `lead_id`) foi **refutada por um dilema de dois chifres, provado empiricamente** antes de convergir no CAS. **Revisão obrigatória de 4 tipos (concorrência/database-architect, arquitetura/code-reviewer, segurança, Python/contratos) BLOQUEOU a primeira especificação**: achado CRITICAL — a leitura sob lock, descrita apenas como "SELECT...FOR UPDATE" sem detalhar a chamada, é um no-op silencioso quando o objeto já está no identity map do SQLAlchemy (o caso real do handler, que pré-carrega o lead para o 404) — CAS passa contra um valor obsoleto. **Corrigido** (mandato explícito de `.populate_existing()` ou `db.refresh(with_for_update=True)`) **e re-verificado empiricamente, isolado do resto**: 0 falhas em 100 rodadas com a correção (as duas formas equivalentes, 50 rodadas cada), contra 50 falhas em 50 rodadas (100%) sem ela, no mesmo container, mesmo schema, mesma barreira, mais um controle determinístico que isola a causa exata — ver `bia-v2-rb-proof.md`, seção "Re-verificação dedicada". Mais 1 HIGH de arquitetura (dict de payload e dict de eco de `_chamar_crm` precisam ser separados) e demais MEDIUM/LOW, todos corrigidos no texto — ver seção 11 do desenho | Implementar (2 migrations — 1 coluna no CRM, 4 colunas+1 índice no Conversas; a lógica de CAS no endpoint, seguindo a forma exata verificada; a reclassificação de erro em 4 classes; o worker); corrigir o texto do plano sobre `selecionar_atendente` (problema irmão, não fechado pela mesma solução, item próprio de severidade baixa) | `tests/test_v2_e2e_distribuido.py` cenários B1+B2+G+I+J+K+L (definidos, não implementados) — cenário G em particular muda de "esperado vermelho" (v3) para "esperado verde" (v4, pós-R-B); mais um teste de regressão dedicado provando que a forma SEM `populate_existing()`/`refresh()` falha (não só que a forma correta passa) |

## Notas de leitura da matriz

- **"Bloqueia X?" = Sim** significa: o próprio plano (para R1-R14) ou a
  análise das rodadas de desenho (para R15-R17) já classifica o risco como
  impeditivo para aquele estágio, não uma opinião nova.
- **Nenhum risco bloqueia dry-run em produção/canary real** — nenhuma das
  17 exposições é alcançável contra tráfego real, e nenhum tráfego real
  ocorreu em nenhum momento desta sessão nem das rodadas de desenho
  subsequentes. **A ressalva factual já registrada permanece válida**:
  `V2_MODE` não foi `dry_run` em 100% dos testes locais —
  `tests/test_v2_e2e_bia.py` cenário B roda com `V2_MODE="live"` contra um
  CRM inteiramente fake e um banco Conversas descartável (nunca produção,
  nunca canary real) — ver `bia-v2-e2e-boundaries.md`.
- **R10 continua sendo o risco mais independente e mais central** —
  nenhuma das rodadas de desenho de R15/R16/R17/R-B o resolve nem o
  subsume; confirmado explicitamente nesta rodada que o mecanismo de R-B
  (CAS) protege V2×V2, não protege contra um writer V1.
- **R15, R16 e R17 (incluindo R-B) têm agora desenho COMPLETO, revisado
  por 4 tipos de revisor obrigatórios, e zero implementação.** Nenhum
  código de produção, migration real, ou edição do plano principal foi
  criado em nenhuma das rodadas de desenho. A rodada de revisão desta
  versão **BLOQUEOU** a primeira especificação de R-B com 1 achado
  CRITICAL (ver nota da linha R17) — corrigido no texto e re-verificado
  empiricamente antes desta matriz ser escrita, não apenas corrigido em
  prosa. Nenhum item técnico permanece aberto dentro do escopo de
  R15/R16/R17/R-B — ver `bia-v2-r15-r17-design-v4.md` seção 11 para a
  trilha completa das 4 revisões e da re-verificação. Os três ainda
  precisam de: implementação, e os testes E2E distribuídos (A-L) antes de
  qualquer um poder ser marcado como efetivamente fechado nesta tabela em
  sentido operacional (não só de desenho).
- **Nenhum risco desta tabela foi corrigido, implementado ou testado em
  produção nesta rodada nem nas anteriores** — esta matriz é um mapa de
  estado de DESENHO, não um relatório de trabalho de implementação
  concluído.
