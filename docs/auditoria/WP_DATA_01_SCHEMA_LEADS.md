# WP-DATA-01 — Schema drift de `leads` (colunas de viagem)

| Campo | Valor |
|---|---|
| Severidade | ALTO (processo/banco) — **sem risco local**, risco em deploy sem backup |
| Arquivo | `app/main.py` (lifespan, +15 linhas) — **não commitado** |
| Status | Segregado do WP-UX. Aguarda decisão/execução com backup |

## 1. O que é
`app/main.py` adiciona, no `lifespan` (startup), 5 migrations inline idempotentes:
```sql
ALTER TABLE leads ADD COLUMN total_dias      INTEGER       DEFAULT NULL;
ALTER TABLE leads ADD COLUMN datas_destinos  JSON          DEFAULT NULL;
ALTER TABLE leads ADD COLUMN num_viajantes   INTEGER       DEFAULT NULL;
ALTER TABLE leads ADD COLUMN num_criancas    INTEGER       DEFAULT 0;
ALTER TABLE leads ADD COLUMN idades_criancas VARCHAR(200)  DEFAULT NULL;
```
Cada uma é guardada por `if '<col>' not in existing_cols:` → **idempotente e aditiva**.

## 2. Por que existe (diagnóstico)
As 5 colunas **já estão definidas e em uso** no código já commitado:
- `app/models/lead.py` (linhas 22–27) define os campos;
- `app/schemas/lead.py` e `app/routers/leads.py` (e `pipeline.py`) os utilizam.

Como o app usa `Base.metadata.create_all()` (não cria colunas novas em tabelas existentes), um **banco já existente** (dev/prod) ficaria **sem** essas colunas → queries referenciando-as quebram (`column does not exist`). A migration inline é a **remediação de schema-drift** para destravar a feature de campos de viagem do lead.

**Conclusão:** não é mudança rogue — é a peça que falta para uma feature já mergeada. Mas é **mudança de banco** e não pertence ao escopo de UX.

## 3. Riscos
- **Local:** nenhum (SQLite dev recria; sem deploy).
- **Deploy:** `ALTER TABLE` roda contra o Postgres de produção no próximo startup. Aditivo e nullable/default → baixo risco de dados, mas:
  - viola o protocolo (docs 25/37/110) se feito sem **backup fresco verificado**;
  - sem Alembic, não há downgrade versionado (rollback = `DROP COLUMN`, destrutivo).

## 4. Correção recomendada
1. **Commit isolado** desta mudança nesta branch (ou branch `wp-data-01`), **separado do UX**, com mensagem deixando claro: *"requer backup antes de deploy"*.
2. **Antes de qualquer deploy:** backup `pg_dump` fresco + verificação de integridade (doc 37), caminho registrado, plano de rollback.
3. **Validação pós-deploy:** `\d leads` confirma as 5 colunas; smoke de criar/editar lead com campos de viagem.
4. **Médio prazo:** migrar migrations inline → **Alembic** (DATA-02 / RM-12), com `upgrade`/`downgrade` versionados. As migrations inline do `lifespan` viram dívida a zerar.

## 5. Plano de rollback (deploy)
- Código: `git revert` do commit DATA-01.
- Banco: as colunas são aditivas e nullable → podem **permanecer** sem efeito (preferível) ou `ALTER TABLE leads DROP COLUMN <col>` (destrutivo, só com backup).

## 6. Critérios de aceite
- [ ] Commit DATA-01 isolado do UX, documentado.
- [ ] Backup verificado registrado antes de deploy.
- [ ] `\d leads` mostra as 5 colunas em staging/prod.
- [ ] Smoke de lead com campos de viagem OK.
- [ ] Plano de migração para Alembic registrado (DATA-02).
