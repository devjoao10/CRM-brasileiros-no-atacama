# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-W2E — as constraints que faltavam atras dos "PKs de mentira".

Cinco lugares tratavam um par de colunas como chave primaria em Python
(SELECT-entao-INSERT) sem constraint nenhuma no banco. Este arquivo prova que
o banco agora recusa a duplicata — nao que o Python lembra de checar.

Cobre:
  1. Os 4 UNIQUE existem no schema criado pelos MODELS (Inspector).
  2. COMPORTAMENTO: inserir o par duplicado levanta IntegrityError. Este e o
     teste que teria pego as races; o item 1 sozinho so olha metadados.
  3. F5 — `server_default` no DDL COMPILADO (leads.campos_personalizados,
     leads.status_venda, leads.is_active, messages.send_attempts).
  4. F6 — Task.user_id/lead_id declaram `ondelete`.
  5. m011 roda DUAS vezes contra SQLite novo com --allow-sqlite; a 2a e no-op.
  6. m011 RECUSA SQLite sem a flag, e recusa DATABASE_URL ausente.
  7. m011 detecta duplicata, NAO cria o indice, NAO apaga nada, sai com 2.
  8. F7 — trava a forma PERSISTIDA de users.role ("ADMIN", o NOME do membro).
     Nao foi corrigida de proposito (exigiria reescrever dados + ALTER TYPE no
     enum nativo do PostgreSQL); ver comentario em app/models/user.py.

O Conversas roda em SUBPROCESSO: `app.*` do CRM e `app.*` do Conversas sao dois
pacotes com o mesmo nome — nao cabem no mesmo interpretador. Mesma tecnica que
as migrations do conversas usam ("processo proprio").

Rodar:  python tests/test_data_integrity_constraints.py
"""
import importlib
import os
import pathlib
import pkgutil
import subprocess
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT))

os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": f"sqlite:///{(SCRATCH / 'data_integrity_crm.db').as_posix()}",
    "SECRET_KEY": "test-secret-key",
    "GEMINI_API_KEY": "",
    "SEED_INITIAL_ADMIN": "false",
})

failures = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        failures.append(msg)


def fresh(name):
    """Arquivo SQLite descartavel. Devolve (Path, url)."""
    path = SCRATCH / name
    if path.exists():
        path.unlink()
    return path, f"sqlite:///{path.as_posix()}"


# ─────────────────────────────────────────────────────────────────────
# Parte 1 — CRM: schema vindo dos models
# ─────────────────────────────────────────────────────────────────────
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.dialects import postgresql, sqlite as sqlite_dialect  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402

from app.database import Base  # noqa: E402
import app.models  # noqa: E402

# `app/models/__init__.py` e vazio: sem importar CADA modulo, o metadata sai
# incompleto e o create_all nao cria funnel_entries/operational_*.
for _mod in pkgutil.walk_packages(app.models.__path__, "app.models."):
    importlib.import_module(_mod.name)

from app.models.lead import Lead  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402

CRM_DB, CRM_URL = fresh("data_integrity_crm.db")
# Engine PROPRIA, sem o PRAGMA foreign_keys do app: aqui o alvo e o indice
# unico, nao a integridade referencial — inserir linhas orfas mantem o teste
# focado numa coisa so.
crm_engine = create_engine(CRM_URL)
Base.metadata.create_all(bind=crm_engine)


def unique_index_present(engine, table, name):
    """
    Aceita indice unico OU constraint UNIQUE homonima: os dois garantem a
    unicidade, e `create_all` e a m011 podem materializar de formas diferentes
    em dialetos diferentes.
    """
    insp = inspect(engine)
    if any(ix["name"] == name and ix.get("unique") for ix in insp.get_indexes(table)):
        return True
    return any(uc["name"] == name for uc in insp.get_unique_constraints(table))


print("\n[1] UNIQUE no schema criado pelos models (CRM)")
CRM_UNIQUES = [
    ("F2", "funnel_entries", "uq_funnel_entries_lead_funnel"),
    ("F3", "operational_card_assignees", "uq_operational_card_assignees_card_user"),
    ("F4", "operational_card_field_values", "uq_operational_card_field_values_card_definition"),
]
for finding, table, name in CRM_UNIQUES:
    check(unique_index_present(crm_engine, table, name), f"{finding}: {name} existe em {table}")


print("\n[2] COMPORTAMENTO: o banco recusa o par duplicado (CRM)")


def insert(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def duplicate_rejected(engine, sql, first, second):
    """Insere `first` (deve passar) e `second` (deve levantar IntegrityError)."""
    insert(engine, sql, **first)
    try:
        insert(engine, sql, **second)
    except IntegrityError:
        return True
    return False


check(
    duplicate_rejected(
        crm_engine,
        "INSERT INTO funnel_entries (id, lead_id, funnel_id, etapa_id, posicao) "
        "VALUES (:id, 10, 20, :etapa, 0)",
        {"id": 1, "etapa": "novo"},
        {"id": 2, "etapa": "outra-etapa"},  # etapa diferente: a chave e (lead, funil)
    ),
    "F2: 2o FunnelEntry com (lead_id=10, funnel_id=20) levanta IntegrityError",
)
check(
    duplicate_rejected(
        crm_engine,
        "INSERT INTO operational_card_assignees (id, card_id, user_id) VALUES (:id, 5, 7)",
        {"id": 1}, {"id": 2},
    ),
    "F3: 2o assignee com (card_id=5, user_id=7) levanta IntegrityError",
)
check(
    duplicate_rejected(
        crm_engine,
        "INSERT INTO operational_card_field_values (id, card_id, definition_id, value_text) "
        "VALUES (:id, 9, 3, :v)",
        {"id": 1, "v": "primeiro"},
        {"id": 2, "v": "segundo"},  # valor diferente: a chave e (card, definition)
    ),
    "F4: 2o field_value com (card_id=9, definition_id=3) levanta IntegrityError",
)


print("\n[3] F5: server_default no DDL compilado")


def column_ddl(table, column, dialect):
    """A linha do CREATE TABLE correspondente a essa coluna."""
    ddl = str(CreateTable(table).compile(dialect=dialect))
    for line in ddl.splitlines():
        if line.strip().startswith(column + " "):
            return line.strip().rstrip(",")
    return ""


for dname, dialect in (("sqlite", sqlite_dialect.dialect()), ("postgresql", postgresql.dialect())):
    for column, needle in (
        ("campos_personalizados", "DEFAULT"),
        ("status_venda", "DEFAULT 'em_negociacao'"),
        ("is_active", "DEFAULT"),
    ):
        line = column_ddl(Lead.__table__, column, dialect)
        check(needle in line, f"F5 [{dname}] leads.{column}: DDL tem DEFAULT -> {line!r}")


print("\n[4] F6: Task declara ondelete nas duas FKs")
for column, expected in (("user_id", "SET NULL"), ("lead_id", "CASCADE")):
    fks = list(Task.__table__.c[column].foreign_keys)
    got = fks[0].ondelete if fks else None
    check(got == expected, f"F6: Task.{column} ondelete={got!r} (esperado {expected!r})")


print("\n[5] F7: forma PERSISTIDA de users.role continua sendo o NOME do membro")
# NAO foi corrigido de proposito — ver app/models/user.py. Este teste existe
# para que a mudanca, se acontecer, seja DELIBERADA e nao um efeito colateral.
insert(
    crm_engine,
    "INSERT INTO users (id, nome, email, hashed_password, role, is_active, email_verified) "
    "VALUES (1, 'Adm', 'f7@local.test', 'x', :role, 1, 0)",
    role=UserRole.ADMIN.name,
)
with crm_engine.connect() as conn:
    persisted = conn.execute(text("SELECT role FROM users WHERE id = 1")).scalar()
check(persisted == "ADMIN",
      f"F7: SAEnum persiste o NOME ('ADMIN'), nao o valor ('admin') — lido {persisted!r}")
check(UserRole.ADMIN.value == "admin",
      "F7: o Python continua enxergando 'admin' — e ESSA a divergencia documentada")
# A CONSEQUENCIA real da divergencia, reproduzida: uma linha gravada em
# minusculo (e o que conversas/app/seed.py:54 faz na tabela COMPARTILHADA)
# faz a ORM do CRM explodir com LookupError em toda query que a retorne.
insert(
    crm_engine,
    "INSERT INTO users (id, nome, email, hashed_password, role, is_active, email_verified) "
    "VALUES (2, 'Seed Conversas', 'f7lower@local.test', 'x', 'admin', 1, 0)",
)
with Session(crm_engine) as session:
    try:
        session.query(User).filter(User.id == 2).all()
        exploded = False
    except LookupError:
        exploded = True
check(exploded,
      "F7: linha com role='admin' (minusculo) faz a ORM do CRM levantar LookupError")


# ─────────────────────────────────────────────────────────────────────
# Parte 2 — Conversas em subprocesso (`app.*` colide com o do CRM)
# ─────────────────────────────────────────────────────────────────────
print("\n[6] Conversas (subprocesso): F1 unique em conversations.whatsapp + F5 send_attempts")

CONV_DB, CONV_URL = fresh("data_integrity_conversas.db")
_CONV_SCRIPT = textwrap.dedent(
    """
    import os, pathlib, sys
    root = pathlib.Path(sys.argv[1])
    os.environ.update({
        "ENVIRONMENT": "development",
        "DATABASE_URL": sys.argv[2],
        "SECRET_KEY": "test-secret-key",
        "CONVERSAS_SEED_DEV_DATA": "false",
    })
    sys.path.insert(0, str(root / "conversas"))

    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import postgresql, sqlite as sqlite_dialect

    from app.database import Base
    from app.models.conversation import Conversation, Message

    engine = create_engine(sys.argv[2])
    Base.metadata.create_all(bind=engine)

    insp = inspect(engine)
    has_uq = any(ix["name"] == "uq_conversations_whatsapp" and ix.get("unique")
                 for ix in insp.get_indexes("conversations")) or any(
                 uc["name"] == "uq_conversations_whatsapp"
                 for uc in insp.get_unique_constraints("conversations"))
    print("RESULT uq_conversations_whatsapp", has_uq)

    sql = ("INSERT INTO conversations (id, lead_id, whatsapp, status, unread_count, is_bot_active) "
           "VALUES (:id, :lead, '5511999990000', 'aberta', 0, 1)")
    with engine.begin() as conn:
        conn.execute(text(sql), {"id": 1, "lead": 1})
    rejected = False
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), {"id": 2, "lead": 2})
    except IntegrityError:
        rejected = True
    print("RESULT duplicate_rejected", rejected)

    for name, dialect in (("sqlite", sqlite_dialect.dialect()),
                          ("postgresql", postgresql.dialect())):
        ddl = str(CreateTable(Message.__table__).compile(dialect=dialect))
        line = [l.strip() for l in ddl.splitlines() if l.strip().startswith("send_attempts ")]
        print("RESULT send_attempts_default_" + name,
              bool(line) and "DEFAULT 0" in line[0])
    """
)


def run_child(script, *args):
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


conv = run_child(_CONV_SCRIPT, str(ROOT), CONV_URL)
if conv.returncode != 0:
    check(False, f"subprocesso do Conversas falhou:\n{conv.stdout}\n{conv.stderr}")
else:
    results = dict(
        line.split()[1:3] for line in conv.stdout.splitlines() if line.startswith("RESULT ")
    )
    check(results.get("uq_conversations_whatsapp") == "True",
          "F1: uq_conversations_whatsapp existe em conversations")
    check(results.get("duplicate_rejected") == "True",
          "F1: 2a conversa com o MESMO whatsapp levanta IntegrityError")
    for dname in ("sqlite", "postgresql"):
        check(results.get(f"send_attempts_default_{dname}") == "True",
              f"F5 [{dname}] messages.send_attempts: DDL tem DEFAULT 0")


# ─────────────────────────────────────────────────────────────────────
# Parte 3 — a migration m011
# ─────────────────────────────────────────────────────────────────────
print("\n[7] m011: recusa, aplicacao, idempotencia e deteccao de duplicata")

M011 = ROOT / "migrations" / "m011_audit_unique_constraints.py"

# Schema LEGADO: as tabelas existem SEM os indices unicos — exatamente o estado
# de um banco que nasceu antes desta auditoria. Se usassemos create_all a
# migration ja acharia tudo pronto e a 1a rodada nao provaria nada.
_LEGACY_DDL = [
    "CREATE TABLE conversations (id INTEGER PRIMARY KEY, lead_id INTEGER, whatsapp VARCHAR(30))",
    "CREATE TABLE funnel_entries (id INTEGER PRIMARY KEY, lead_id INTEGER, funnel_id INTEGER, "
    "etapa_id VARCHAR(100))",
    "CREATE TABLE operational_card_assignees (id INTEGER PRIMARY KEY, card_id INTEGER, user_id INTEGER)",
    "CREATE TABLE operational_card_field_values (id INTEGER PRIMARY KEY, card_id INTEGER, "
    "definition_id INTEGER, value_text TEXT)",
]


def legacy_db(name, extra_rows=()):
    path, url = fresh(name)
    eng = create_engine(url)
    with eng.begin() as conn:
        for ddl in _LEGACY_DDL:
            conn.execute(text(ddl))
        for row in extra_rows:
            conn.execute(text(row))
    eng.dispose()
    return path, url, eng


def run_m011(url, *args):
    env = dict(os.environ)
    if url is None:
        env.pop("DATABASE_URL", None)
    else:
        env["DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, str(M011), *args],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


# 7a — recusa SQLite sem a flag
_, refuse_url, _ = legacy_db("data_integrity_m011_refuse.db")
res = run_m011(refuse_url)
check(res.returncode != 0 and "RECUSADO" in res.stdout,
      f"m011 RECUSA SQLite sem --allow-sqlite (exit={res.returncode})")
check("OK" not in res.stdout, "m011 nao imprime OK quando recusa")

# 7b — recusa DATABASE_URL ausente
res = run_m011(None, "--allow-sqlite")
check(res.returncode != 0 and "DATABASE_URL" in res.stdout,
      f"m011 RECUSA DATABASE_URL ausente mesmo com --allow-sqlite (exit={res.returncode})")

# 7c — 1a rodada aplica os 4 indices
_, apply_url, apply_engine = legacy_db("data_integrity_m011_apply.db")
first = run_m011(apply_url, "--allow-sqlite")
check(first.returncode == 0, f"m011 1a rodada sai 0 (exit={first.returncode})\n{first.stdout}")
check(first.stdout.count(":created") == 4,
      f"m011 1a rodada cria os 4 indices (contou {first.stdout.count(':created')})")

verify_engine = create_engine(apply_url)
for finding, table, name in CRM_UNIQUES + [("F1", "conversations", "uq_conversations_whatsapp")]:
    check(unique_index_present(verify_engine, table, name),
          f"m011: {name} existe em {table} depois da 1a rodada")

# 7d — 2a rodada e no-op limpa
second = run_m011(apply_url, "--allow-sqlite")
check(second.returncode == 0, f"m011 2a rodada sai 0 (exit={second.returncode})")
check("NO-OP" in second.stdout, "m011 2a rodada declara NO-OP")
check(":created" not in second.stdout, "m011 2a rodada nao cria nada")
check(second.stdout.count(":already-present") == 4,
      f"m011 2a rodada reporta 4 already-present (contou {second.stdout.count(':already-present')})")

# 7e — duplicata: aborta, nao cria o indice, NAO apaga nada
_, dup_url, _ = legacy_db(
    "data_integrity_m011_dupes.db",
    extra_rows=[
        "INSERT INTO funnel_entries (id, lead_id, funnel_id, etapa_id) VALUES (41, 7, 3, 'a')",
        "INSERT INTO funnel_entries (id, lead_id, funnel_id, etapa_id) VALUES (42, 7, 3, 'b')",
    ],
)
res = run_m011(dup_url, "--allow-sqlite")
check(res.returncode == 2, f"m011 sai 2 quando ha duplicata (exit={res.returncode})")
check("id=41" in res.stdout and "id=42" in res.stdout,
      "m011 imprime os ids exatos que colidem")
check("lead_id=7" in res.stdout and "funnel_id=3" in res.stdout,
      "m011 imprime a chave que colide")
check("OK" not in res.stdout, "m011 nao imprime OK depois de abortar")

dup_engine = create_engine(dup_url)
with dup_engine.connect() as conn:
    remaining = conn.execute(text("SELECT COUNT(*) FROM funnel_entries")).scalar()
check(remaining == 2, f"m011 NAO apagou nada ao abortar (linhas={remaining})")
check(not unique_index_present(dup_engine, "funnel_entries", "uq_funnel_entries_lead_funnel"),
      "m011 nao criou o indice enquanto ha duplicata")

# O indice de conversations (tabela SEM duplicata) foi criado ANTES do abort:
# prova da transacao-por-objeto — o objeto que passou fica aplicado.
check(unique_index_present(dup_engine, "conversations", "uq_conversations_whatsapp"),
      "m011: objeto aplicado antes do abort permanece (transacao por objeto)")


print("\n" + "=" * 60)
if failures:
    print(f"FALHAS: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
sys.exit(0)
