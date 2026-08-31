# Memory index

- [pysqlite SAVEPOINT rollback gap](pysqlite-savepoint-rollback-gap.md) — `db.begin_nested()` on SQLite in this repo (both `database.py`) doesn't actually undo on a later `db.rollback()`; works fine on PostgreSQL. Also: the SQLite symptom is order-dependent within a session — an uncaught-but-handled prior `begin_nested()` failure masks it for the next one unless you `db.rollback()` first. Blocks `test_v2_eventos.py` check 9b — but the fix is NOT a no-op: CRM's `app/services/lead_creation.py` already uses `begin_nested()` (different, unaffected path today), and the fix changes shared engine-wide transaction semantics (eager BEGIN) for every session, needing its own review (HOTFIX-09) before applying.

