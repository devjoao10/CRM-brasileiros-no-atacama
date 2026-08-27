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
