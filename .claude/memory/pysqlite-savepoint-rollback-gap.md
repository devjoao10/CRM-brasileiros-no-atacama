---
name: pysqlite-savepoint-rollback-gap
description: db.begin_nested() (SAVEPOINT) on SQLite in this repo does not actually roll back on a later db.rollback() — pysqlite driver gap, not an app bug. CRM already uses begin_nested() elsewhere (a different, unaffected path) — applying the fix is NOT a no-op and needs its own review (HOTFIX-09).
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

**CRM V1 already uses `begin_nested()` today — this is NOT inert/hypothetical
code.** Verified by `grep -rn "begin_nested" app/ conversas/`:
`app/services/lead_creation.py` calls `db.begin_nested()` at **line 326**
(`garantir_entrada_no_funil`) and **line 359** (`_obter_ou_criar_tag`). Both
follow the same shape: SAVEPOINT -> `IntegrityError` caught -> re-SELECT to
decide whether the row that already exists is the one this call would have
inserted -> re-raise the ORIGINAL exception when it is not (comment: "nao
era essa a violacao... levanta o original"). **Neither call site calls
`db.rollback()` afterward** — the handled exception just lets the function
return normally, and the CALLER's own later `db.commit()` (in `criar_lead`)
closes out the transaction. This is a DIFFERENT path from the one that
exposed the gap in `registrar_evento`: the gap only bites `commit=False`
followed by a SEPARATE, later, plain `db.rollback()` that expects to undo an
already-flushed-and-released SAVEPOINT. `lead_creation.py`'s pattern never
does that, so it is unaffected by the gap AS WRITTEN TODAY — but that means
the fix below would change the transactional timing of an already-shipped,
already-used CRM code path, not just of a not-yet-called Fase 0 function.

**Conversas V1 not calling `begin_nested()` today does NOT mean "applying
the fix has no impact on V1" — that inference is invalid and must not be
repeated.** The fix is not scoped to `begin_nested()` call sites: it is a
`connect`/`begin` listener pair on the SHARED SQLite `engine`, so it changes
WHEN every transaction on that engine starts — including plain reads that
never touch a SAVEPOINT. Today, a session on the default (unpatched) pysqlite
driver begins its underlying transaction LAZILY. With the fix
(`isolation_level=None` + manual `BEGIN` on `"begin"`), every session on that
engine starts a real transaction EAGERLY, the moment SQLAlchemy's autobegin
fires — a plain `SELECT` now holds a real SQLite transaction (with SQLite's
file-level locking) for as long as the session stays open. Any V1 code that
opens a session and keeps it around across time — multiple queries in one
request, a background task, anything spanning an `await` — is affected by
this change in when a transaction starts and how long it is held, regardless
of whether it ever calls `begin_nested()`.

**Blast radius on long-running work — checked, NOT confirmed; do not cite the
obvious candidate as proven.** The obvious suspect is
`conversas/app/routers/webhook.py`'s `_forward_to_agent`/
`_debounce_then_forward` (the n8n call carries a 240s read timeout,
`AGENT_TIMEOUT = httpx.Timeout(240.0, connect=10.0)`). Read directly: it does
**NOT** currently hold a transaction open across that wait.
`_forward_to_agent` calls `db.close()` at line 1131 immediately BEFORE
`await _fetch_agent_parts(...)` at line 1133 — an explicit, deliberate fix
(`AUDIT-2026-08-WF2 (D1)`, documented inline right above it) written
specifically because holding the pool connection for up to 240s previously
exhausted the connection pool under bursty inbound traffic. So this function
is a REFUTED candidate, not a confirmed instance of the risk above — a
future session must not repeat "webhook.py holds a transaction open for
240s" as fact. No other call site was confirmed in this pass to hold a
session open across a slow `await` on the shared engine. Treat the eager-BEGIN
risk itself as real (the mechanism above is provable independent of any
single call site), but NOT yet pinned to a specific confirmed long-running
instance — a fuller audit of Conversas' async call sites (whatsapp.py,
meta_templates.py, crm.py all hold `db: Session` params alongside `httpx`
calls and were not individually audited here) is still needed before
Phase 6, not just of `begin_nested()` usages.

**Consequence:** installing the fix requires its own review (HOTFIX-09), not
a drive-by patch inside an unrelated feature, and it cannot be justified as
"zero observable effect" — it measurably changes when `lead_creation.py`'s
existing SAVEPOINTs start relative to the enclosing transaction, and it
changes locking semantics for every Conversas session on the shared engine
in ways this file has not fully audited. The preferred option before Phase 6
remains proving the semantics against real/ephemeral PostgreSQL rather than
patching the SQLite engine at all (R12 in the plan, Option A/B — PostgreSQL
real PREFERRED).

**Why the fix isn't just "flip it on":** it requires an
`event.listens_for(engine, "connect"/"begin")` pair on the shared engine in
`database.py`. Neither engine currently has it. `database.py` is shared infra
used by both V1 and V2 in both packages — not something to patch as a side
effect of an unrelated feature, and per the above, not provably a no-op for
existing V1 code either.

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
