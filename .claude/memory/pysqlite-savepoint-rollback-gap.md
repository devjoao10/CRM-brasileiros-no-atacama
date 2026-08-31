---
name: pysqlite-savepoint-rollback-gap
description: db.begin_nested() (SAVEPOINT) on SQLite in this repo does not actually roll back on a later db.rollback() — pysqlite driver gap, not an app bug
metadata:
  type: architecture
---

Neither `app/database.py` (CRM) nor `conversas/app/database.py` (Conversas) configures
the standard SQLAlchemy pysqlite fix (`isolation_level=None` on `"connect"` +
manual `conn.exec_driver_sql("BEGIN")` on `"begin"` — see SQLAlchemy docs,
"Serializable isolation / Savepoints / Transactional DDL" for pysqlite).

**Effect:** on SQLite (dev/tests only — this does not happen on PostgreSQL),
`with db.begin_nested(): db.add(x); db.flush()` followed by a **later, separate**
`db.rollback()` (after the nested block already exited successfully) does **not**
undo the flushed row. Verified empirically: `RELEASE SAVEPOINT` + a subsequent
`ROLLBACK` are both sent to pysqlite, but the row survives anyway. Applying the
standard fix (as a throwaway repro engine) makes the exact same sequence roll
back correctly — confirming this is the well-known pysqlite legacy-transaction
bug, not an ORM misuse.

**What still works fine on the current (unfixed) setup:** `begin_nested()` used
purely to catch `IntegrityError` on a colliding INSERT and keep the *outer*
transaction alive for a subsequent `commit()` — this is the existing pattern in
`app/services/lead_creation.py` (`garantir_entrada_no_funil`, `_obter_ou_criar_tag`).
That pattern never calls `db.rollback()` afterward; it only relies on the
exception propagating cleanly and the session staying usable for more inserts +
a final `commit()`. That part is unaffected by this gap.

**What breaks:** any new code that flushes inside `begin_nested()` with
`commit=False` and expects a caller's later plain `db.rollback()` to undo it —
e.g. `conversas/app/v2/eventos.py:registrar_evento(..., commit=False)`. This
is correct and works as intended on PostgreSQL (production); it only fails
under this repo's current SQLite test/dev engine config.

**Why:** the fix requires an `event.listens_for(engine, "connect"/"begin")`
pair on the shared engine in `database.py`. Neither engine currently has it.

**Fix (not yet applied — needs explicit sign-off; `database.py` is shared
infra used by both V1 and V2, not something to patch as a side effect of an
unrelated feature)**, in `database.py`:
```python
if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_savepoint_fix_connect(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _sqlite_savepoint_fix_begin(conn):
        conn.exec_driver_sql("BEGIN")
```
Gate on `IS_SQLITE` — PostgreSQL doesn't need or want this. No existing V1 code
in either package uses `begin_nested()` today, so applying this fix has zero
observable effect on current behavior; it only matters for code (like
`registrar_evento`) that flushes inside a savepoint and later expects a plain
rollback to undo it.

This is a real fix, not a workaround — do not route around it from inside a
leaf module (e.g. registering the same event listeners on the shared `engine`
from within `eventos.py` to avoid editing the "off-limits" `database.py`);
that has the identical global blast radius as editing `database.py` directly,
just harder to find later.

**Second finding (Task 0.2, second follow-up): the SQLite symptom is itself
order-dependent — a passing test here proves nothing about production.**
Empirically isolated while splitting `test_v2_eventos.py`: whether the bug
manifests for a given `begin_nested()` call depends on whether
`db.in_transaction()` was already `True` going into it. A **prior**
`begin_nested()` that raises (e.g. `IntegrityError` from a colliding
`event_id`, caught as `EventoDuplicado`) leaves the session's own
`in_transaction()` at `True` even after the exception is handled — SQLAlchemy's
autobegin marks the session "in transaction" the moment `begin_nested()` runs,
and catching the exception only rolls back to the savepoint, not the outer
(autobegun) transaction. If nothing calls `db.commit()`/`db.rollback()`
afterward, the **next** `begin_nested()` in the same session nests inside that
still-open transaction instead of starting bare — and the bug's usual symptom
(row visible to another connection before the outer commit) silently stops
reproducing. Not fixed — masked by accidental leftover session state from an
unrelated prior check.

Concretely: `db.begin_nested()` → `IntegrityError` caught → **no** explicit
`db.rollback()` → next `db.begin_nested()` in the same session → `RELEASE`
does NOT commit early (looks correct). Insert one `db.rollback()` right after
catching the duplicate, and the very same next `begin_nested()` goes back to
committing early (the real, unfixed behavior). Verified by direct A/B repro
against `conversas/app/database.py`'s actual `SessionLocal`/`engine`, not a
throwaway engine.

**Consequence for any test (or future Phase 6 code) that calls
`registrar_evento`/`begin_nested()` more than once in the same session:**
after catching `EventoDuplicado` (or any exception from a nested block) with
no further action, call `db.rollback()` before relying on the session's
transaction state again — otherwise a later `commit=False` check (or worse,
real Phase 6 handoff logic composing multiple events in one transaction) is
silently testing a different code path than the one that will run in
isolation. `tests/test_v2_eventos.py` documents this inline where it matters
(the `db.rollback()` between the UUID-canonicalization-collision check and
the `commit=False` check exists *because of* this, not just for hygiene).

This is also why R12's plan mitigation was rewritten to require proof against
**real PostgreSQL** before Phase 6, rather than trusting any specific SQLite
behavior (locked-in or not) to predict production — SQLite's behavior here
isn't even stable against itself.
