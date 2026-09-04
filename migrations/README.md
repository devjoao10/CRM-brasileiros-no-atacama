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
