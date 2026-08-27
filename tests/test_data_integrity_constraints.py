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
  8. AUDIT-2026-08-WF2 — m011 RECUSA alvo que nao tem NENHUMA das tabelas
     (base recem-provisionada / nome errado / replica vazia), em vez de dizer
     "NO-OP, ja estava tudo aplicado" sobre um estado que nunca verificou; e o
     abort por duplicata bloqueia SO o indice daquela tabela — o F5 (puro DDL)
     e os indices das tabelas limpas continuam sendo aplicados.
  9. AUDIT-2026-08-WF2 — m012 deixa o banco no estado que
     `aplicar_estado_humano` declara UNICO possivel:
     primeira_resposta_humana_at NOT NULL => queued_at NULL.
 10. F7 — trava a forma PERSISTIDA de users.role ("ADMIN", o NOME do membro).
     Nao foi corrigida de proposito (exigiria reescrever dados + ALTER TYPE no
     enum nativo do PostgreSQL); ver comentario em app/models/user.py.

O Conversas roda em SUBPROCESSO: `app.*` do CRM e `app.*` do Conversas sao dois
pacotes com o mesmo nome — nao cabem no mesmo interpretador. Mesma tecnica que
as migrations do conversas usam ("processo proprio"). A m012 e a excecao: ela
nao importa `app.*` (so sqlalchemy), entao roda em processo, e a insercao que
ela faz de `conversas/` no sys.path e desfeita logo apos o import.

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


# 7f - AUDIT-2026-08-WF2: alvo com tabelas, mas NENHUMA das quatro.
# `DATABASE_URL` para outra base (nome errado, replica vazia, banco recem
# provisionado) fazia a m011 imprimir "OK - NO-OP (ja estava tudo aplicado)" e
# sair 0. O operador marcava o runbook como feito e producao seguia sem os
# quatro indices unicos. "table-absent" so e verdade quando a base E a nossa.
_, outro_url = fresh("data_integrity_m011_outro_banco.db")
outro_eng = create_engine(outro_url)
with outro_eng.begin() as conn:
    conn.execute(text("CREATE TABLE alguma_outra_coisa (id INTEGER PRIMARY KEY)"))
outro_eng.dispose()
res = run_m011(outro_url, "--allow-sqlite")
check(res.returncode != 0,
      f"m011 RECUSA alvo sem nenhuma das 4 tabelas (exit={res.returncode})")
check("RECUSADO" in res.stdout, "m011 diz RECUSADO no alvo errado, nao 'NO-OP'")
check("OK" not in res.stdout, "m011 nao imprime OK sobre um estado que nao verificou")

# 7g - banco COMPLETAMENTE vazio: mesmo veredito.
_, vazio_url = fresh("data_integrity_m011_vazio.db")
create_engine(vazio_url).connect().close()
res = run_m011(vazio_url, "--allow-sqlite")
check(res.returncode != 0 and "RECUSADO" in res.stdout,
      f"m011 RECUSA banco vazio (exit={res.returncode})")

# 7h - nao-regressao: um alvo com ALGUMA das quatro continua rodando. Em dev o
# CRM e o Conversas sao dois arquivos SQLite distintos e cada um tem so uma
# parte das tabelas; a recusa nao pode pegar esse caso legitimo.
_, parcial_url = fresh("data_integrity_m011_parcial.db")
parcial_eng = create_engine(parcial_url)
with parcial_eng.begin() as conn:
    conn.execute(text(_LEGACY_DDL[0]))  # so `conversations`, como o arquivo do Conversas
parcial_eng.dispose()
res = run_m011(parcial_url, "--allow-sqlite")
check(res.returncode == 0, f"m011 AINDA roda com so uma das tabelas presente (exit={res.returncode})")
check(unique_index_present(create_engine(parcial_url), "conversations", "uq_conversations_whatsapp"),
      "m011: alvo parcial (so conversations) aplica o indice que da para aplicar")

# 7i - AUDIT-2026-08-WF2: duplicata numa tabela nao pode bloquear o resto.
# O F5 e puro DDL (ALTER COLUMN SET DEFAULT: nao le nem escreve uma linha) e
# rodava DEPOIS de quatro verificacoes dependentes de dado. Uma duplicata em
# funnel_entries abortava a run inteira e producao ficava sem os DEFAULT - com
# todo INSERT vindo de psql/n8n/COPY ainda sendo rejeitado.
_, blk_url, _ = legacy_db(
    "data_integrity_m011_bloqueio.db",
    extra_rows=[
        "INSERT INTO funnel_entries (id, lead_id, funnel_id, etapa_id) VALUES (41, 7, 3, 'a')",
        "INSERT INTO funnel_entries (id, lead_id, funnel_id, etapa_id) VALUES (42, 7, 3, 'b')",
        "INSERT INTO conversations (id, lead_id, whatsapp) VALUES (61, 1, '5511777770000')",
        "INSERT INTO conversations (id, lead_id, whatsapp) VALUES (62, 2, '5511777770000')",
    ],
)
res = run_m011(blk_url, "--allow-sqlite")
check(res.returncode == 2, f"m011 continua saindo 2 com duplicata (exit={res.returncode})")
check("F5 server-defaults" in res.stdout,
      "m011: o passo F5 (puro DDL) e alcancado mesmo com duplicata em outra tabela")
blk_eng = create_engine(blk_url)
for _table, _name in (("operational_card_assignees", "uq_operational_card_assignees_card_user"),
                      ("operational_card_field_values", "uq_operational_card_field_values_card_definition")):
    check(unique_index_present(blk_eng, _table, _name),
          f"m011: {_name} (tabela LIMPA) e criado apesar do abort em outra tabela")
check("funnel_entries" in res.stdout and "conversations" in res.stdout,
      "m011: uma rodada so reporta AS DUAS tabelas sujas (nao uma por vez)")
check(not unique_index_present(blk_eng, "funnel_entries", "uq_funnel_entries_lead_funnel"),
      "m011: nenhum indice criado sobre tabela suja")


# ---------------------------------------------------------------------
# Parte 4 - m012: o invariante de fila que `aplicar_estado_humano` declara
# ---------------------------------------------------------------------
print("\n[8] AUDIT-2026-08-WF2 - m012 respeita 'prh NOT NULL => queued_at NULL'")

# `conversas/app/services/atendimento.py:aplicar_estado_humano` declara este
# invariante como o UNICO estado possivel. O backfill da m012 gravava
# primeira_resposta_humana_at e deixava queued_at intacto, produzindo em massa
# exatamente o estado que o codigo trata como impossivel: conversa "ja atendida"
# que continua ocupando lugar na fila de espera.
_m012_syspath = list(sys.path)
_spec012 = importlib.util.spec_from_file_location(
    "m012_wf2", ROOT / "migrations" / "m012_conversas_primeira_resposta_humana.py")
m012 = importlib.util.module_from_spec(_spec012)
_spec012.loader.exec_module(m012)
# O modulo insere `conversas/` no sys.path (ele espera rodar em processo
# proprio). Aqui o processo e o do CRM e `app.*` ja esta resolvido para o CRM -
# desfazer a insercao impede que um import posterior caia no pacote errado.
sys.path[:] = _m012_syspath

_CONV_LEGACY_DDL = (
    "CREATE TABLE conversations ("
    "id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL, whatsapp VARCHAR(30) NOT NULL, "
    "nome VARCHAR(200), status VARCHAR(20) NOT NULL DEFAULT 'aberta', "
    "atendente_id INTEGER, is_bot_active BOOLEAN NOT NULL DEFAULT 1, "
    "queued_at TIMESTAMP, created_at TIMESTAMP{extra})",
    "CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, "
    "direction VARCHAR(10) NOT NULL, content TEXT, created_at TIMESTAMP)",
)


def conv_db(name, extra_col="", rows=()):
    """Banco do Conversas no estado PRE-m012 (ja pos-m008: tem queued_at)."""
    _, url = fresh(name)
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text(_CONV_LEGACY_DDL[0].format(extra=extra_col)))
        conn.execute(text(_CONV_LEGACY_DDL[1]))
        for row in rows:
            conn.execute(text(row))
    return eng


def fila(engine):
    """{id: (tem_queued_at, tem_prh)} - a forma do invariante, sem os valores."""
    with engine.connect() as conn:
        return {
            r[0]: (bool(r[1]), bool(r[2]))
            for r in conn.execute(text(
                "SELECT id, queued_at IS NOT NULL, primeira_resposta_humana_at IS NOT NULL "
                "FROM conversations ORDER BY id"))
        }


# 8a - banco legado (pre-m012): quem o backfill marca sai da fila DE VERDADE.
eng_8a = conv_db(
    "data_integrity_m012_backfill.db",
    rows=[
        "INSERT INTO conversations (id, lead_id, whatsapp, status, atendente_id, is_bot_active, "
        "queued_at, created_at) VALUES "
        "(1, 30, '551190020', 'aberta', 5, 0, '2026-01-01 09:00:00', '2026-01-01 10:00:00'),"
        "(2, 31, '551190021', 'aberta', NULL, 0, '2026-01-01 09:00:00', '2026-01-01 10:00:00')",
        "INSERT INTO messages (id, conversation_id, direction, content, created_at) "
        "VALUES (1, 1, 'outbound', 'oi', '2026-01-01 10:05:00')",
    ],
)
m012.run(engine=eng_8a)
estado_8a = fila(eng_8a)
check(estado_8a[1] == (False, True),
      f"m012: conversa backfillada sai da fila (queued_at NULL) - veio {estado_8a[1]}")
check(estado_8a[2] == (True, False),
      f"m012: conversa AINDA na fila mantem queued_at e continua sem prh - veio {estado_8a[2]}")

# 8b - banco onde a m012 ANTIGA ja rodou e deixou o estado impossivel.
# `WHERE primeira_resposta_humana_at IS NULL` (a clausula que torna o backfill
# idempotente) exclui essas linhas: sem um passo que enderece o invariante
# diretamente, rodar de novo nao conserta nada.
eng_8b = conv_db(
    "data_integrity_m012_reparo.db",
    extra_col=", primeira_resposta_humana_at TIMESTAMP",
    rows=[
        "INSERT INTO conversations (id, lead_id, whatsapp, status, atendente_id, is_bot_active, "
        "queued_at, created_at, primeira_resposta_humana_at) VALUES "
        "(1, 30, '551190020', 'aberta', 5, 0, '2026-01-01 09:00:00', '2026-01-01 10:00:00', "
        " '2026-01-01 10:00:00')",
    ],
)
m012.run(engine=eng_8b)
estado_8b = fila(eng_8b)
check(estado_8b[1] == (False, True),
      f"m012: REPARA linha ja marcada que ficou com queued_at (estado impossivel) - veio {estado_8b[1]}")
with eng_8b.connect() as conn:
    prh_8b = conn.execute(text(
        "SELECT primeira_resposta_humana_at FROM conversations WHERE id = 1")).scalar()
check(str(prh_8b) == "2026-01-01 10:00:00",
      f"m012: o reparo NAO reescreve primeira_resposta_humana_at (veio {prh_8b!r})")

# 8c - idempotencia: a 2a rodada nao tem mais nada a fazer.
acoes_8a2 = m012.run(engine=eng_8a)
check("backfill-em-atendimento:0" in acoes_8a2, f"m012: 2a rodada nao backfilla nada ({acoes_8a2})")
check(fila(eng_8a) == estado_8a, "m012: 2a rodada nao muda o estado da fila (idempotente)")

# 8d - alvo errado: sem `conversations` nao ha o que verificar, e a m012
# imprimia "OK - NO-OP (ja estava aplicada)" mesmo assim.
eng_8d = create_engine(fresh("data_integrity_m012_alvo_errado.db")[1])
with eng_8d.begin() as conn:
    conn.execute(text("CREATE TABLE alguma_outra_coisa (id INTEGER PRIMARY KEY)"))
try:
    m012.run(engine=eng_8d)
    recusou = False
except RuntimeError:
    recusou = True
check(recusou, "m012 RECUSA alvo sem a tabela conversations em vez de dizer NO-OP")


print("\n" + "=" * 60)
if failures:
    print(f"FALHAS: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
sys.exit(0)
