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

## Futuro (WP-DATA-02)
Substituir estes scripts por **Alembic** com `upgrade`/`downgrade` versionados e
zerar qualquer migration inline remanescente.
