# migrations/ — Migrations manuais idempotentes (DATA-01)

Enquanto o projeto **não adota Alembic** (ver WP-DATA-02 / RM-12), as reconciliações
de schema para bancos **já existentes** ficam aqui, como scripts **idempotentes**,
**fora do startup** do app.

> **Por que não no `app/main.py`?** Rodar `ALTER TABLE` no `lifespan` aplica schema
> em produção de forma automática e não-controlada (sem backup, sem aprovação,
> sem rollback). Movido para cá no DATA-01.

## Princípios
- **Bancos novos**: criados completos por `Base.metadata.create_all()` a partir dos models. Não precisam destes scripts.
- **Bancos existentes**: aplicar o script correspondente, **manualmente e com controle**.
- Cada script é **idempotente** (pode rodar várias vezes sem efeito colateral) e compatível com SQLite (dev) e PostgreSQL (prod).

## Scripts
| # | Script | O que faz |
|---|---|---|
| 001 | `m001_schema_drift_leads_tasks.py` | `leads`: +6 colunas de viagem (`dias_por_destino`, `total_dias`, `datas_destinos`, `num_viajantes`, `num_criancas`, `idades_criancas`); `tasks`: `user_id` nullable + `resultado_ia`; índices de performance |
| 011 | `m011_audit_unique_constraints.py` | AUDIT-2026-08-W2E — 4 índices UNIQUE que faltavam: `conversations(whatsapp)`, `funnel_entries(lead_id,funnel_id)`, `operational_card_assignees(card_id,user_id)`, `operational_card_field_values(card_id,definition_id)`; + `SET DEFAULT` (só PostgreSQL) em `leads.campos_personalizados/status_venda/is_active` e `messages.send_attempts` (F5), que roda **primeiro** por ser puro DDL. **Recusa** rodar sem `DATABASE_URL`, contra SQLite (salvo `--allow-sqlite`) e — AUDIT-2026-08-WF2 — contra alvo que não tenha **nenhuma** das tabelas acima (exit 1: banco errado, não “NO-OP”). Duplicata **bloqueia só o próprio índice**: os demais objetos são aplicados, o relatório sai completo numa rodada e o exit é 2 — não deduplica, não apaga. Exit != 0 em qualquer falha. |
| 012 | `m012_conversas_primeira_resposta_humana.py` | AUDIT-2026-08-WA — `conversations.primeira_resposta_humana_at` + índice + backfill conservador (só conversas abertas, sem bot, com dono e com outbound). AUDIT-2026-08-WF2: também zera `queued_at` onde `primeira_resposta_humana_at` está preenchida — o invariante que `conversas/app/services/atendimento.py:aplicar_estado_humano` declara — e **recusa** alvo sem a tabela `conversations` (exit 1). Rodar depois da m008. |
| 013 | `m013_conversation_events.py` | BIA-V2 Fase 0 / Task 0.2 (CONVERSAS) — cria a tabela `conversation_events`: trilha append-only para reconstruir "o que aconteceu com esta conversa?" sem abrir workflow nenhum. `event_id` com `uq_conversation_events_event_id` (UNIQUE de verdade, não só checagem de aplicação) + `ix_conversation_events_conversation_id` e `ix_conversation_events_created_at`. Sem FK em `conversation_id` de propósito — o evento é registro de auditoria e precisa sobreviver à deleção da conversa que descreve. Roda em processo **próprio** do Conversas (insere `conversas/` no `sys.path`), nunca junto das migrations do CRM. **Recusa** rodar sem `DATABASE_URL`, contra SQLite (salvo `--allow-sqlite`) e contra alvo sem a tabela `conversations` (exit 1: banco errado, não "NO-OP"). Rollback: `DROP TABLE` — a V1 nunca referencia esta tabela. |
| 014 | `m014_conversas_state_columns.py` | BIA-V2 Fase 4 — acrescenta `conversations.state` (VARCHAR, `DEFAULT 'LEGACY'`), `state_version` (INTEGER, `DEFAULT 0` já embutido no próprio `ADD COLUMN` — não o padrão `SET DEFAULT` só-PostgreSQL da m011, que deixaria o SQLite sem default no catálogo) e `closed_at` (TIMESTAMP): as 3 colunas da máquina de estados da V2. Aditivas e nullable — a V1 continua lendo a linha sem notar a mudança. Roda em processo próprio do Conversas, como a m013. |
| 015 | `m015_conversas_estado_humano_check.py` | BIA-V2 Fase 4 — `CheckConstraint` em `conversations` garantindo que toda linha em `WAITING_HUMAN`/`HUMAN_ACTIVE` tem dono humano coerente (bot desligado, atendente e responsável preenchidos e iguais, fila registrada). No PostgreSQL usa `NOT VALID` + `VALIDATE CONSTRAINT` em duas transações separadas, para não manter a tabela inteira sob lock `ACCESS EXCLUSIVE` durante o scan de validação; no SQLite a validação é pulada (sem cobertura empírica lá). Reexecução confere `pg_constraint.convalidated` antes de reportar sucesso, para nunca confirmar uma validação que uma interrupção anterior deixou incompleta. |
| 016 | `m016_handoff_operations.py` | BIA-V2 Fase 6 (CONVERSAS) — cria a tabela `handoff_operations`: uma linha por **operação** de handoff BIA → humano, o log de intenção que torna o handoff reconciliável (uma linha parada em `REQUESTED` significa "o processo morreu entre a chamada ao CRM e o commit local"). `event_id` com `uq_handoff_operations_event_id` (UNIQUE de verdade — é a chave de idempotência compartilhada com o CRM) + `ck_handoff_operations_status` (CHECK do vocabulário fechado `REQUESTED`/`COMPLETED`/`FAILED`, declarado **dentro** do `CREATE TABLE` para não precisar do `ALTER TABLE ... ADD CONSTRAINT` que difere por dialeto). Sem FK em `conversation_id` (auditoria sobrevive à deleção da conversa) nem em `target_user_id` (`users` vive no schema do CRM). Sem índice secundário: `app/v2/handoff.py` consulta só por `event_id`. Roda em processo **próprio** do Conversas, como a m013. **Recusa** rodar sem `DATABASE_URL`, contra SQLite (salvo `--allow-sqlite`) e contra alvo sem a tabela `conversations` (exit 1). Reversão: remover a tabela — a V1 nunca referencia esta tabela. **Precisa ser aplicada junto com a m017** (mesma Fase 6, irmãs — ver linha abaixo). |
| 017 | `m017_lead_handoff_operations.py` | BIA-V2 Fase 6 (CRM) — cria a tabela `lead_handoff_operations`: o livro-razão de idempotência do lado CRM para `POST /api/v2/lead-domain/handoff`, irmã de `handoff_operations` (m016) do lado Conversas — nomes distintos de propósito, já que os dois apps compartilham o mesmo PostgreSQL em produção. **Não reusa `lead_history`** (decisão registrada no módulo: `target_user_id` não tem coluna própria lá, a escrita só acontece quando o valor muda — o ledger precisa gravar sempre — e uma coluna nova teria que ser nullable, o que enfraqueceria a própria garantia de idempotência). `event_id NOT NULL UNIQUE`, sem FK em `lead_id`/`target_user_id` (mesmo precedente append-only de m013). Roda no processo do CRM (não insere `conversas/` no `sys.path`). **Recusa** rodar sem `DATABASE_URL` e contra SQLite (salvo `--allow-sqlite`). **Aplicar a m016 sem esta migration deixa `POST /api/v2/lead-domain/handoff` retornando 500 em todo handoff** — as duas nascem juntas na Fase 6 e precisam ser aplicadas juntas. Reversão: remover a tabela — nenhum fluxo V1 a referencia. |
| 018 | `m018_lead_handoff_previous_responsavel.py` | Fechamento de R-B (CRM) — `lead_handoff_operations.previous_responsavel_id INTEGER NULL`: snapshot do dono anterior do lead no momento em que a operação de handoff aplicou, usado pela reclassificação de erro/reconciliação de R15. Aditiva, sem backfill (linhas pré-existentes ficam `NULL`, estado correto — não havia esse dado para reconstruir). Roda no processo do CRM, mesmo padrão de recusa/idempotência da m017. Reversão: coluna sem uso, nunca `DROP COLUMN`. |
| 019 | `m019_handoff_operations_reconciliacao.py` | Fechamento de R-B (CONVERSAS) — `handoff_operations` ganha `expected_responsavel_id INTEGER NULL` (baseline do CAS enviado ao CRM), `attempt_count INTEGER NOT NULL DEFAULT 0` (só conta tentativas que de fato chamaram a rede — nunca incrementada na transação de reclamação do worker), `next_attempt_at TIMESTAMPTZ NULL`, `reconciled_at TIMESTAMPTZ NULL`, `error_class VARCHAR(32) NULL` (classe de erro persistida — necessária porque o mesmo `error_code` pode ser retriável ou ambíguo dependendo do status HTTP que o originou, então a partição de fila entre o worker de retomada e o de reconciliação não é expressável só por `error_code`), mais índice parcial `ix_handoff_operations_pendentes (next_attempt_at) WHERE status IN ('REQUESTED','FAILED')` — **superado pela m020** (ver linha abaixo), que corrige a ordem desse índice; o model já reflete só a forma final. Sem backfill (mesma lógica de `closed_at` da m014: nenhuma linha pré-existente tem esse dado para reconstruir; `attempt_count` recebe seu `DEFAULT 0` diretamente do `ADD COLUMN`, sem UPDATE). Roda em processo próprio do Conversas, como a m013/m014/m016. Reversão: colunas sem uso, nunca `DROP COLUMN`. **Sequenciamento obrigatório**: como QUALQUER migration deste projeto, aplicar uma única vez, em ordem — não é uma limitação nova desta migration. Ressalva específica: depois que a m020 já tiver rodado num banco, NÃO re-execute a m019 nele — a verificação pós-DDL da m019 espera a forma ORIGINAL do índice (só `next_attempt_at`) e falha contra a forma corrigida que a m020 deixa (`next_attempt_at, id`); isso é o comportamento correto de uma migration cujo DDL foi propositalmente substituído por outra mais nova, não um defeito a corrigir aqui (a m019, uma vez escrita, nunca é editada). |
| 020 | `m020_handoff_operations_indice_pendentes_nulls_first.py` | Fechamento de achado de concorrência pós-R-B (CONVERSAS) — corrige a **ordem** do índice parcial `ix_handoff_operations_pendentes` (m019), provada por revisão empírica (EXPLAIN ANALYZE, PostgreSQL 16 real, 30k linhas) como nunca usada pela consulta real de reclamação dos workers: a consulta ordena `next_attempt_at ASC NULLS FIRST, id ASC`, mas o índice da m019 era um btree comum — `ASC NULLS LAST` por padrão no PostgreSQL — então o planner descartava o índice e fazia Seq Scan + Sort completo a cada ciclo (30s) de cada worker (14.796ms/587 buffers sem índice efetivo contra 0.098ms/24 buffers com a ordem corrigida). Substitui (`DROP INDEX IF EXISTS` + `CREATE INDEX`, na mesma transação) pelo índice logicamente equivalente `(next_attempt_at ASC NULLS FIRST, id ASC) WHERE status IN ('REQUESTED','FAILED')` no PostgreSQL; no SQLite (que não aceita `NULLS FIRST` dentro de `CREATE INDEX` — só em `ORDER BY`) o índice fica `(next_attempt_at, id)` sem anotação, suficiente porque o ASC padrão do SQLite já ordena NULL primeiro. `id` como segunda coluna nos dois dialetos elimina também um "Incremental Sort" residual no desempate. Exceção **literal**, não reinterpretação, da política de nunca `DROP INDEX` desta página: o índice é substituído por outro logicamente equivalente dentro da mesma migration que o corrige, nunca removido sem substituto. Sem `CONCURRENTLY` de propósito (índice parcial, custo proporcional só às operações pendentes — ver docstring do módulo para o trade-off). **Depende da m019** (exige a coluna `next_attempt_at`, não só a tabela). Roda em processo próprio do Conversas, como a m013/m014/m016/m019. Reversão: recriar o índice no formato da m019 — nunca há perda de dado, só a regressão de performance já conhecida. |

## Como rodar (LOCAL / STAGING)
```bash
# usa DATABASE_URL de app.config (dev = SQLite)
python -m migrations.m001_schema_drift_leads_tasks
```

## Aplicação em PRODUÇÃO — **gate obrigatório**
> ⛔ Agentes de IA NÃO executam isto. É ação humana controlada.

1. **Backup fresco verificado** do PostgreSQL (`pg_dump` + checksum + caminho registrado) — doc 37.
2. **Validação de integridade** do backup (tamanho > 0, header do dump).
3. **Aprovação humana** explícita (João Pedro) — doc 26.
4. Rodar o script apontando para o banco de produção (via container, fora do startup).
5. **Validação pós-deploy**: `\d leads` mostra as 6 colunas; `\d tasks` mostra `resultado_ia`; smoke de criar/editar lead com campos de viagem.
6. **Plano de rollback**: colunas são aditivas/nullable → podem permanecer sem efeito; reversão destrutiva (`DROP COLUMN`) só com backup.

## Atenção — m008 depois de CONV-BIA-FILA-01

**Não re-execute a `m008_conversas_queued_at.py` em banco com conversas na fila.** Além de criar
`queued_at` e os índices, a m008 normaliza estado legado:

```sql
UPDATE conversations SET is_bot_active = FALSE
WHERE atendente_id IS NOT NULL AND is_bot_active = TRUE;
```

Isso era correto quando atribuir um dono desligava a Bia. Desde o CONV-BIA-FILA-01 a Bia **continua
ligada** na fila (handoff, assign e release preservam `is_bot_active`), então "atendente preenchido +
Bia ligada" é um estado válido — e re-executar a m008 desligaria a Bia de **todas** as conversas na fila
que já têm dono. O script não é editado (migration escrita não se altera): a cautela é de quem opera.
Banco que ainda não rodou a m008: confira antes se esse `WHERE` pega alguma linha.

## Drift conhecido e ACEITO — `ix_conversations_whatsapp` (AUDIT-2026-08-WF2)

Produção tem **dois** índices sobre `conversations.whatsapp`:

```sql
CREATE INDEX        ix_conversations_whatsapp ON public.conversations USING btree (whatsapp);
CREATE UNIQUE INDEX uq_conversations_whatsapp ON public.conversations USING btree (whatsapp);
```

O `ix_` é legado: nasceu do `index=True` que a m011/W2E removeu do model quando
declarou o `Index("uq_conversations_whatsapp", unique=True)`. Ele é **redundante**
— btree na mesma coluna única, e o planner nunca prefere o não-único para uma
igualdade — mas a m011 **não o dropa**, de propósito:

- A m011 é auditável exatamente por ser **puramente aditiva** (“não deduplica,
  não apaga”, e o próprio docstring proíbe `DROP INDEX`). Embutir um DROP
  entregaria uma remoção de surpresa a quem roda o script pelos UNIQUE.
- O custo de deixar é desprezível: `conversations` tem ~81 linhas em produção.
- Remover é destrutivo e irreversível sem recriar — mesma classe de decisão
  para a qual a seção “Aplicação em PRODUÇÃO” acima exige backup + aprovação.

Quem quiser eliminar o drift roda isto **como passo próprio**, sob o mesmo gate
de produção (é instantâneo neste tamanho de tabela, e a volta é um `CREATE
INDEX` com a DDL acima):

```sql
DROP INDEX IF EXISTS ix_conversations_whatsapp;
```

**Caso irmão, ainda em aberto:** `funnel_entries.lead_id` continua com
`index=True` em `app/models/pipeline.py`, redundante com
`uq_funnel_entries_lead_funnel(lead_id, funnel_id)` — `lead_id` é a coluna
líder do índice composto. Mesma decisão se aplica; a mudança fica no model.

## Futuro (WP-DATA-02)
Substituir estes scripts por **Alembic** com `upgrade`/`downgrade` versionados e
zerar qualquer migration inline remanescente.
