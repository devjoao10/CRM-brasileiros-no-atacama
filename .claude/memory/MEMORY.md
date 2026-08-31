# Memory index

- [pysqlite SAVEPOINT rollback gap](pysqlite-savepoint-rollback-gap.md) — `db.begin_nested()` on SQLite in this repo (both `database.py`) doesn't actually undo on a later `db.rollback()`; works fine on PostgreSQL. Also: the SQLite symptom is order-dependent within a session — an uncaught-but-handled prior `begin_nested()` failure masks it for the next one unless you `db.rollback()` first. Blocks `test_v2_eventos.py` check 9b until `database.py` gets the standard pysqlite fix.

