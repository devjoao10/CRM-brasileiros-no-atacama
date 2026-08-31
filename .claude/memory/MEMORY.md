# Memory index

- [pysqlite SAVEPOINT rollback gap](pysqlite-savepoint-rollback-gap.md) — `db.begin_nested()` no SQLite deste repo nao desfaz num `db.rollback()` posterior; no PostgreSQL funciona. R12 e travado POR DESIGN pelo check 9b de `test_v2_eventos.py` (passa enquanto a divergencia existir). Bloqueado mesmo: a composicao transacional da Fase 6. Aplicar a receita NAO e no-op — ver o arquivo antes de mexer em `database.py`.

