# Memory index

- [pysqlite SAVEPOINT rollback gap](pysqlite-savepoint-rollback-gap.md) — `db.begin_nested()` on SQLite in this repo (both `database.py`) doesn't actually undo on a later `db.rollback()`; works fine on PostgreSQL. Blocks `test_v2_eventos.py` test 14b until `database.py` gets the standard pysqlite fix.

