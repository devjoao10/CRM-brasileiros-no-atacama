# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-W3 — onde o codigo se comporta DIFERENTE em PostgreSQL e SQLite.

Producao e UM PostgreSQL compartilhado pelos dois servicos. A suite inteira
roda em SQLite. Toda divergencia de dialeto e, por construcao, um ponto cego:
o CI diz verde sobre um SQL que o banco de producao nunca executou.

Este arquivo fecha o buraco pelo unico caminho que existe sem servidor: o
SQLAlchemy COMPILA contra o dialeto `postgresql` sem conexao nenhuma. Cada
divergencia aqui vem com os DOIS SQLs lado a lado, nao com uma opiniao.

O QUE ESTE ARQUIVO E
--------------------
Uma TRAVA, nao um detector de bug novo. Cada check fixa o comportamento ATUAL
de um ponto divergente e diz, na mensagem, o que a divergencia significa em
producao. Se alguem mudar o predicado, a ordenacao ou o DDL, o check quebra e
a pessoa e obrigada a decidir de proposito em vez de por acidente.

O QUE ELE NAO FAZ
-----------------
Nao sobe PostgreSQL, nao se conecta a lugar nenhum e NAO PULA NADA. Se um
pre-requisito faltar (psycopg2, dialeto, versao do SQLite), ele REPROVA com a
mensagem dizendo o que falta — um SKIP aqui seria exatamente o falso verde que
este arquivo existe para eliminar.

AUDIT-2026-08-WF2 — unica excecao, e ela nao afrouxa nada: a secao 10 EXECUTA
um corpus hostil contra um PostgreSQL de verdade quando (e so quando) o
operador apontar DATABASE_URL para um. Nao e um SKIP invertido — os checks de
compilacao daquela secao rodam SEMPRE e ja reprovam sozinhos; o servidor so
acrescenta a prova de comportamento, que por definicao nao existe sem servidor.

Nao importa o pacote `app` do Conversas: `app` existe nos DOIS servicos e
colide no mesmo processo. Onde o alvo e do Conversas, a evidencia e (a) o
texto-fonte do arquivo e (b) a compilacao de uma expressao equivalente sobre
uma tabela declarada aqui com os MESMOS tipos. As duas coisas juntas detectam
mudanca; nenhuma sozinha detectaria.

Rodar:  python tests/test_postgres_dialect_divergence.py
Saida:  exit 0 = nenhuma divergencia mudou de forma | exit 1 = alguma mudou
"""
import os
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pathlib.Path(ROOT / "scratch").mkdir(exist_ok=True)

# AUDIT-2026-08-WF2 — o `app` deste arquivo continua em SQLite: as provas de
# COMPILACAO nao precisam de servidor e o CI nao tem um. Mas se o operador
# apontar DATABASE_URL para um PostgreSQL descartavel, a secao 10 ganha por
# cima dele a prova COMPORTAMENTAL do F-043. Precisa ser lido ANTES do update
# abaixo, que sobrescreve a variavel. Mesmo desvio de
# tests/test_pipeline_funnel_race.py e tests/test_leads_segment_drift.py.
_PG_URL = os.environ.get("DATABASE_URL", "")
if not _PG_URL.startswith("postgres"):
    _PG_URL = ""

os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/pg_dialect_divergence.db",
    "SEED_INITIAL_ADMIN": "false",
    "SECRET_KEY": "dialect-probe",
    "GEMINI_API_KEY": "",
})

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


def fonte(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# 0. Pre-requisitos — faltar qualquer um REPROVA (nunca vira SKIP)
# ══════════════════════════════════════════════════════════════════════════
print("0) pre-requisitos da prova")

try:
    import psycopg2  # noqa: F401
    import psycopg2.errors as PGERR
    _psycopg2_ok = True
except Exception as exc:  # noqa: BLE001
    PGERR = None
    _psycopg2_ok = False
    print(f"     (import falhou: {type(exc).__name__}: {exc})")
check(_psycopg2_ok,
      "psycopg2 importavel — sem ele nao da para provar como o PostgreSQL "
      "classifica os erros (requirements.txt:psycopg2-binary)")

from sqlalchemy import (  # noqa: E402
    Column, DateTime, Integer, MetaData, String, Table, create_engine, event,
    func, select, text,
)
from sqlalchemy.dialects import postgresql, sqlite as sqlite_dialect  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402

PG = postgresql.dialect()
LITE = sqlite_dialect.dialect()
check(PG.name == "postgresql" and LITE.name == "sqlite",
      "dialetos postgresql e sqlite instanciaveis sem conexao")

# `.nullslast()` compila para `NULLS LAST`, que so existe no SQLite 3.30+.
_sv = tuple(int(p) for p in sqlite3.sqlite_version.split("."))
check(_sv >= (3, 30, 0),
      f"SQLite {sqlite3.sqlite_version} >= 3.30 (antes disso `NULLS LAST`, que "
      f"o _inbox_order emite, e erro de sintaxe)")


def sql(expr, dialect):
    """SQL literal de uma expressao/statement num dialeto. Uma linha."""
    compiled = expr.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    return " ".join(str(compiled).split())


# ══════════════════════════════════════════════════════════════════════════
# 1. ILIKE / lower() — ASCII-only no SQLite, locale-aware no PostgreSQL
# ══════════════════════════════════════════════════════════════════════════
print("\n1) busca case-insensitive sobre dado com acento")

import app.main  # noqa: E402,F401  (registra todos os mappers do CRM)
from app.models.lead import Lead  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.models.pipeline import FunnelEntry  # noqa: E402
from app.models.user import User  # noqa: E402

busca = select(Lead.id).where(Lead.nome.ilike("%JOÃO%"))
pg_ilike = sql(busca, PG)
lite_ilike = sql(busca, LITE)
print(f"     [PG]   {pg_ilike}")
print(f"     [LITE] {lite_ilike}")

check("ILIKE" in pg_ilike and "lower(" not in pg_ilike,
      "PostgreSQL usa ILIKE NATIVO (dobra a caixa com as regras de locale, "
      "entao 'JOÃO' casa 'João')")
check("lower(" in lite_ilike and "ILIKE" not in lite_ilike,
      "SQLite nao tem ILIKE: o SQLAlchemy emite lower() LIKE lower(), e o "
      "lower() do SQLite e ASCII-only")

_con = sqlite3.connect(":memory:")


def _lite(q):
    return _con.execute(q).fetchone()[0]


check(_lite("SELECT lower('JOÃO')") == "joÃo",
      "PROVA: lower('JOÃO') no SQLite devolve 'joÃo' — o Ã nao e dobrado")
check(_lite("SELECT lower('João') LIKE lower('%JOÃO%')") == 0,
      "CONSEQUENCIA: no SQLite, buscar 'JOÃO' NAO acha o lead 'João'. No "
      "PostgreSQL acha. A suite nunca ve o comportamento de producao — e um "
      "teste local que 'confirme' que a busca nao acha estaria fixando o ERRADO")

# O mesmo lower() decide o filtro de campo personalizado, e la ele e comparado
# contra um valor que o Python ja normalizou com str.lower() (Unicode completo).
qf = fonte("app/query_filters.py")
check("func.lower(valor_col).contains(valor_norm" in qf,
      "app/query_filters.py compara lower() do SQL com um valor que o Python "
      "baixou com str.lower(); str.lower() e Unicode e o lower() do SQLite nao "
      "— a assimetria so existe no SQLite")

# ══════════════════════════════════════════════════════════════════════════
# 2. _json_list_contains — TRES copias, e a do pipeline.py estava sem teste
# ══════════════════════════════════════════════════════════════════════════
print("\n2) filtro de destino (JSON) — leads.py, segments.py e pipeline.py")

import app.routers.leads as R_LEADS  # noqa: E402
import app.routers.segments as R_SEG  # noqa: E402
import app.routers.pipeline as R_PIPE  # noqa: E402

COPIAS = (("leads.py", R_LEADS), ("segments.py", R_SEG), ("pipeline.py", R_PIPE))


def json_contains_sql(modulo, is_sqlite):
    original = modulo.IS_SQLITE
    modulo.IS_SQLITE = is_sqlite
    try:
        expr = modulo._json_list_contains(Lead.destinos, "Atacama")
        return sql(expr, LITE if is_sqlite else PG)
    finally:
        modulo.IS_SQLITE = original


pg_json = {nome: json_contains_sql(m, False) for nome, m in COPIAS}
lite_json = {nome: json_contains_sql(m, True) for nome, m in COPIAS}
print(f"     [PG]   {pg_json['pipeline.py']}")
print(f"     [LITE] {lite_json['pipeline.py']}")

check(len(set(pg_json.values())) == 1,
      f"as TRES copias de _json_list_contains geram o MESMO SQL no PostgreSQL "
      f"(divergiram: {pg_json})")
check(len(set(lite_json.values())) == 1,
      f"as TRES copias geram o MESMO SQL no SQLite (divergiram: {lite_json})")

for nome in pg_json:
    s = pg_json[nome]
    check("AS JSONB" in s.upper() and "@>" in s,
          f"{nome}: ramo PostgreSQL faz CAST(... AS JSONB) antes do @> "
          f"(a coluna e `json` e `json @> unknown` nao existe no PostgreSQL)")
    check(not re.search(r"leads\.destinos\s*@>", s),
          f"{nome}: a coluna crua nunca cola no @> — isso seria 500 em producao")

check("json_type" not in pg_json["pipeline.py"] and "@>" not in lite_json["pipeline.py"],
      "pipeline.py: os ramos nao vazam um para o outro "
      "(tests/test_leads_destino_filter_dialect.py cobre so leads.py e "
      "segments.py; pipeline.py entrou aqui)")

# Divergencia SEMANTICA, nao de sintaxe: o ramo SQLite dobra a caixa dos dois
# lados e o `@>` do PostgreSQL compara o elemento JSON literalmente.
check("lower(" in lite_json["leads.py"],
      "SQLite: `lower(CAST(destinos AS VARCHAR)) LIKE lower('%\"Atacama\"%')` "
      "— o filtro e CASE-INSENSITIVE")
check("lower(" not in pg_json["leads.py"],
      "PostgreSQL: `@> '[\"Atacama\"]'` e CASE-SENSITIVE e exato. "
      "DIVERGENCIA REAL: destino='atacama' devolve leads no SQLite e devolve "
      "ZERO em producao — sem erro, so uma lista vazia")

# ══════════════════════════════════════════════════════════════════════════
# 3. Ordenacao de NULL — SQLite poe primeiro, PostgreSQL poe por ultimo
# ══════════════════════════════════════════════════════════════════════════
print("\n3) ordenacao de coluna nullable sem NULLS FIRST/LAST")

_con.execute("CREATE TABLE ord (a TEXT)")
_con.executemany("INSERT INTO ord VALUES (?)", [(None,), ("b",), ("a",)])
asc_lite = [r[0] for r in _con.execute("SELECT a FROM ord ORDER BY a ASC")]
desc_lite = [r[0] for r in _con.execute("SELECT a FROM ord ORDER BY a DESC")]
print(f"     SQLite ASC : {asc_lite}")
print(f"     SQLite DESC: {desc_lite}")
check(asc_lite[0] is None,
      "PROVA: SQLite ASC poe NULL PRIMEIRO. O PostgreSQL documenta o oposto "
      "('null values sort as if larger than any non-null value' => ASC NULLS LAST)")
check(desc_lite[-1] is None,
      "PROVA: SQLite DESC poe NULL POR ULTIMO; no PostgreSQL DESC e NULLS FIRST")

# Conversas: as duas filas do inbox ordenam por coluna nullable, e SO UMA
# declara o tratamento de NULL.
conv_src = fonte("conversas/app/routers/conversations.py")
check("Conversation.queued_at.asc().nullslast()" in conv_src,
      "conversations.py/_inbox_order: `inbox=fila` declara nullslast() — "
      "ordem IDENTICA nos dois bancos")
check("Conversation.last_customer_msg_at.asc(), Conversation.id.asc()" in conv_src,
      "conversations.py: `queue=fila` (legado) ordena por "
      "last_customer_msg_at.asc() SEM nullsfirst/nullslast. DIVERGENCIA: a "
      "conversa sem inbound do cliente (last_customer_msg_at NULL) abre a fila "
      "no SQLite e fecha a fila em producao")

# Compilacao da MESMA ordenacao sobre os MESMOS tipos (sem importar o Conversas).
_md = MetaData()
espelho = Table(
    "conversations", _md,
    Column("id", Integer, primary_key=True),
    Column("last_customer_msg_at", DateTime(timezone=True), nullable=True),
    Column("queued_at", DateTime(timezone=True), nullable=True),
)
sem_nulls = select(espelho.c.id).order_by(espelho.c.last_customer_msg_at.asc(),
                                          espelho.c.id.asc())
com_nulls = select(espelho.c.id).order_by(espelho.c.queued_at.asc().nullslast(),
                                          espelho.c.id.asc())
print(f"     [fila legado PG]   {sql(sem_nulls, PG)}")
print(f"     [fila legado LITE] {sql(sem_nulls, LITE)}")
check("NULLS" not in sql(sem_nulls, PG).upper()
      and "NULLS" not in sql(sem_nulls, LITE).upper(),
      "o SQL da fila legada nao carrega clausula de NULL em dialeto nenhum — "
      "cada banco aplica o SEU default, e os defaults sao opostos")
check("NULLS LAST" in sql(com_nulls, PG).upper()
      and "NULLS LAST" in sql(com_nulls, LITE).upper(),
      "com nullslast() o SQL fica IDENTICO nos dois dialetos — e a forma "
      "correta, ja usada em _inbox_order")

# CRM: created_at/updated_at sao nullable no DDL e sao a chave de ordenacao.
check(Lead.__table__.c.created_at.nullable,
      "leads.created_at e NULLABLE no DDL (server_default nao implica NOT NULL)")
check(FunnelEntry.__table__.c.updated_at.nullable,
      "funnel_entries.updated_at e NULLABLE no DDL")
ord_leads = sql(select(Lead.id).order_by(Lead.created_at.desc(), Lead.id.desc()), PG)
check("NULLS" not in ord_leads.upper(),
      "leads.py:236/407 ordena created_at DESC sem NULLS: uma linha com "
      "created_at NULL (INSERT direto por psql/n8n) fica FIXA no topo da lista "
      "em producao e no fim da lista no SQLite — e o keyset "
      "`(created_at, id) < (...)` e NULL para ela, entao ela some da pagina 2")

# ══════════════════════════════════════════════════════════════════════════
# 4. Enum — tipo NATIVO no PostgreSQL, VARCHAR SEM CHECK no SQLite
# ══════════════════════════════════════════════════════════════════════════
print("\n4) users.role: SAEnum(UserRole)")

ddl_pg = " ".join(str(CreateTable(User.__table__).compile(dialect=PG)).split())
ddl_lite = " ".join(str(CreateTable(User.__table__).compile(dialect=LITE)).split())
check("role userrole" in ddl_pg,
      "PostgreSQL: `role userrole` — TIPO ENUM NATIVO, validado pelo banco")
check("role VARCHAR(5)" in ddl_lite,
      "SQLite: `role VARCHAR(5)` — texto puro")
check("CHECK" not in ddl_lite.upper(),
      "SQLite NAO ganha CHECK constraint (create_constraint=False e o default "
      "do SQLAlchemy 2.x): NADA valida o valor")

_con.execute("CREATE TABLE users_probe (role VARCHAR(5) NOT NULL)")
_con.execute("INSERT INTO users_probe VALUES ('admin')")
_con.execute("INSERT INTO users_probe VALUES ('valor totalmente fora do enum')")
check(_con.execute("SELECT COUNT(*) FROM users_probe").fetchone()[0] == 2,
      "PROVA: o SQLite aceita 'admin' minusculo E texto arbitrario na coluna "
      "role, inclusive maior que VARCHAR(5) (o SQLite ignora o tamanho)")
if PGERR is not None:
    check(issubclass(PGERR.lookup("22P02"), psycopg2.DataError),
          "PostgreSQL: o MESMO INSERT levanta 22P02 invalid_text_representation "
          "-> DataError, e ABORTA A TRANSACAO. Um valor de role fora do enum e "
          "gravacao silenciosa no SQLite e erro duro em producao")

# Filtro por role: o SQLAlchemy resolve o valor para o NOME do membro.
role_sql = sql(select(User.id).where(User.role == "admin"), PG)
check("'ADMIN'" in role_sql,
      "o SQLAlchemy grava/compara o NOME do membro ('ADMIN'), nao o valor "
      "('admin') — quem escreve fora da ORM precisa saber disso")

conv_auth = fonte("conversas/app/auth.py")
check('role = Column(String(20), default="USER")' in conv_auth,
      "o espelho do Conversas declara role como String(20), nao como o enum. "
      "Em PostgreSQL as duas declaracoes produzem DDL DIFERENTE para a MESMA "
      "coluna; em dev sao dois arquivos SQLite separados e isso nunca aparece")
check('t.name != "users"' in fonte("conversas/app/main.py"),
      "mitigacao existente: fora de development o Conversas NAO cria `users`, "
      "entao o dono do DDL do enum e sempre o CRM")

# ══════════════════════════════════════════════════════════════════════════
# 5. Classe de excecao — a MESMA falha vira classes diferentes
# ══════════════════════════════════════════════════════════════════════════
print("\n5) classificacao de erro (webhook decide 503-retry vs 200-descarta)")

wh = fonte("conversas/app/routers/webhook.py")
# fecha no `)` que esta sozinho no comeco da linha — os comentarios das
# entradas tem parenteses e truncavam a extracao no meio da tupla
infra = wh.split("_INFRA_ERRORS = (", 1)[1].split(chr(10) + ")", 1)[0]
check("sa_exc.OperationalError" in infra,
      "webhook.py/_INFRA_ERRORS inclui OperationalError -> 503 (Meta reentrega)")
# AUDIT-2026-08-F2: este check foi INVERTIDO junto com a correcao. Ele travava o
# defeito ("a lista NAO inclui"); agora trava a correcao. O diagnostico original,
# que continua valido e e o motivo de o check existir: a MESMA falha de schema
# vira OperationalError no SQLite (na lista -> 503 -> a Meta reentrega) e
# ProgrammingError/DataError no PostgreSQL (fora da lista -> 200 -> a Meta NUNCA
# reentrega). A suite demonstrava o comportamento oposto ao de producao.
check("ProgrammingError" in infra and "DataError" in infra,
      "webhook.py/_INFRA_ERRORS inclui ProgrammingError e DataError -> drift de "
      "schema no PostgreSQL agora devolve 503 e a Meta REENTREGA, em vez de 200 "
      "e mensagem perdida")
check("IntegrityError" not in infra,
      "IntegrityError segue FORA de proposito: dado invalido nao se resolve com "
      "reentrega (a corrida de primeiro contato e tratada na criacao da conversa)")

if PGERR is not None:
    for code, nome in (("42703", "coluna inexistente"),
                       ("42P01", "tabela inexistente"),
                       ("42883", "funcao inexistente")):
        check(issubclass(PGERR.lookup(code), psycopg2.ProgrammingError),
              f"PostgreSQL: {nome} ({code}) -> ProgrammingError, que agora "
              f"ESTA em _INFRA_ERRORS -> 503 e reentrega, como no SQLite")
    check(issubclass(PGERR.lookup("23505"), psycopg2.IntegrityError),
          "PostgreSQL: unique_violation (23505) -> IntegrityError, a MESMA "
          "classe do SQLite — os except IntegrityError do CRM valem nos dois")
    check(issubclass(PGERR.lookup("25P02"), psycopg2.InternalError),
          "PostgreSQL: in_failed_sql_transaction (25P02) -> InternalError, que "
          "ESTA em _INFRA_ERRORS. Depois do primeiro erro numa transacao, TODO "
          "statement seguinte vira 503-retry — estado que o SQLite nao tem")

from sqlalchemy import exc as sa_exc  # noqa: E402

_probe_engine = create_engine("sqlite://")
_classe_lite = None
try:
    with _probe_engine.connect() as c:
        c.execute(text("SELECT coluna_que_nao_existe FROM sqlite_master"))
except Exception as e:  # noqa: BLE001
    _classe_lite = type(e)
check(_classe_lite is not None and issubclass(_classe_lite, sa_exc.OperationalError),
      f"PROVA: no SQLite 'coluna inexistente' vira sqlalchemy."
      f"{_classe_lite.__name__ if _classe_lite else '???'}, que ESTA em "
      f"_INFRA_ERRORS -> 503 e a Meta reentrega. O MESMO defeito em producao "
      f"vira ProgrammingError -> 200 e a mensagem some. A suite ve o oposto "
      f"exato do que producao faz")

# ══════════════════════════════════════════════════════════════════════════
# 6. SQL cru exclusivo do PostgreSQL no servico compartilhado
# ══════════════════════════════════════════════════════════════════════════
print("\n6) conversas/app/services/crm.py — SQL que so roda em PostgreSQL")

crm = fonte("conversas/app/services/crm.py")
check("NOW()" in crm, "crm.py usa NOW()")
check("::jsonb" in crm, "crm.py usa o cast `::jsonb`")
check("RETURNING id" in crm, "crm.py usa RETURNING id")

for expr, rotulo in (("SELECT NOW()", "NOW()"), ("SELECT '{}'::jsonb", "::jsonb")):
    erro = None
    try:
        _con.execute(expr)
    except Exception as e:  # noqa: BLE001
        erro = e
    check(erro is not None,
          f"PROVA: o SQLite REJEITA {rotulo} ({type(erro).__name__ if erro else 'nao rejeitou'}) "
          f"— auto_create_lead_in_crm/sync_responsavel_to_crm sao codigo "
          f"SO-PostgreSQL e a suite em SQLite nao pode executa-los nunca")

check(crm.count("except Exception as e:") >= 5,
      "e cada um desses caminhos esta dentro de `except Exception` que devolve "
      "None/False: em SQLite o erro de dialeto e indistinguivel de 'CRM "
      "inacessivel (dev isolado)', que e o cenario esperado. Um bug real no "
      "ramo PostgreSQL tem exatamente a mesma assinatura")

# ══════════════════════════════════════════════════════════════════════════
# 7. datetime naive vs aware contra TIMESTAMPTZ
# ══════════════════════════════════════════════════════════════════════════
print("\n7) literal de data/hora contra coluna TIMESTAMP WITH TIME ZONE")

from datetime import date, datetime, time, timezone  # noqa: E402

check("TIMESTAMP WITH TIME ZONE" in ddl_pg and "DATETIME" in ddl_lite,
      "PostgreSQL declara TIMESTAMPTZ; SQLite declara DATETIME (sempre naive)")

naive = datetime.combine(date(2026, 7, 27), time.min)   # analytics.py:42-43
aware = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)  # tasks.py:85 / pipeline.py:355
s_naive_pg = sql(select(func.count(Lead.id)).where(Lead.created_at >= naive), PG)
s_aware_pg = sql(select(func.count(Task.id)).where(Task.data_vencimento < aware), PG)
s_naive_lite = sql(select(func.count(Lead.id)).where(Lead.created_at >= naive), LITE)
s_aware_lite = sql(select(func.count(Task.id)).where(Task.data_vencimento < aware), LITE)
print(f"     [PG naive] {s_naive_pg}")
print(f"     [PG aware] {s_aware_pg}")

check("'2026-07-27 00:00:00'" in s_naive_pg and "+00:00" not in s_naive_pg,
      "analytics.py manda literal SEM offset contra TIMESTAMPTZ: o PostgreSQL "
      "o interpreta no fuso da SESSAO (parametro TimeZone). Se o servidor nao "
      "estiver em UTC, as bordas do dashboard deslocam horas")
check("+00:00" in s_aware_pg,
      "tasks.py/pipeline.py mandam literal COM offset (+00:00) — inequivoco. "
      "Duas convencoes diferentes contra o mesmo tipo de coluna")
check("+00:00" not in s_aware_lite and s_naive_lite == s_naive_lite,
      "no SQLite o SQLAlchemy DESCARTA o tzinfo na serializacao: as duas "
      "convencoes colapsam na mesma string e a diferenca fica invisivel na suite")
check("00:00:00.000000" in s_naive_lite,
      "SQLite serializa com microssegundos e sem fuso — comparacao de TEXTO, "
      "nao de instante")

# ══════════════════════════════════════════════════════════════════════════
# 8. run_select_query: guard escrito para SQLite, executado em PostgreSQL
# ══════════════════════════════════════════════════════════════════════════
print("\n8) app/services/ai_tools.py — denylist da query de leitura da IA")

import app.services.ai_tools as AIT  # noqa: E402

# `_dangerous` e local de run_select_query — extraido do fonte para que o check
# quebre se o padrao mudar, em vez de testar uma copia envelhecida.
ait_src = fonte("app/services/ai_tools.py")
# AUDIT-2026-08-F2: a denylist virou multilinha ao ganhar into/copy/grant/revoke,
# entao a extracao passou a juntar os pedacos do literal.
_m = re.findall(r"r'([^']+)'", ait_src.split("_dangerous = re.compile(", 1)[1].split("re.IGNORECASE", 1)[0])
check(bool(_m),
      "a denylist `_dangerous` continua em app/services/ai_tools.py na forma "
      "esperada (se mudou, este bloco precisa ser relido)")
DANGEROUS = re.compile("".join(_m), re.IGNORECASE) if _m else re.compile(r"(?!)")

check(DANGEROUS.search("PRAGMA table_info(leads)") is not None
      and DANGEROUS.search("ATTACH DATABASE 'x'") is not None,
      "a denylist segue bloqueando `pragma` e `attach` (formas do SQLite)")
check(DANGEROUS.search("COPY leads TO stdout") is not None
      and DANGEROUS.search("GRANT ALL ON leads TO x") is not None,
      "e agora tambem as formas de escrita EXCLUSIVAS do PostgreSQL "
      "(copy/grant/revoke), que a lista antiga nao cobria")
INTO = "SELECT * INTO copia_de_leads FROM leads"
# AUDIT-2026-08-F2: era aqui que o buraco estava. `SELECT ... INTO` passava por
# TODOS os guards — comeca com select, sem `;`, fora da denylist antiga, nao cita
# users/chat_messages — e no PostgreSQL isso e DDL que CRIA TABELA.
check(DANGEROUS.search(INTO) is not None,
      "`SELECT ... INTO` agora e BLOQUEADO pela denylist")
check(DANGEROUS.search("SELECT nome, email FROM leads LIMIT 5") is None
      and DANGEROUS.search("SELECT count(*) FROM leads") is None,
      "e a consulta de leitura legitima continua passando (a lista nao virou "
      "um bloqueio geral)")
erro_into = None
try:
    _con.execute(INTO)
except Exception as e:  # noqa: BLE001
    erro_into = e
check(erro_into is not None,
      f"PROVA: no SQLite `SELECT ... INTO` e erro de sintaxe "
      f"({type(erro_into).__name__}) — inofensivo. No PostgreSQL e DDL: "
      f"`SELECT INTO` CRIA TABELA. A ferramenta 'somente leitura' da IA (que le "
      f"texto de cliente vindo do WhatsApp) so e somente-leitura porque o GRANT "
      f"do usuario crm_readonly a segurava — e agora tambem porque o guard a bloqueia. O GRANT continua sendo a defesa que importa: revogue CREATE.")

# ══════════════════════════════════════════════════════════════════════════
# 9. migration m011 — o ramo PostgreSQL, inspecionado statement a statement
# ══════════════════════════════════════════════════════════════════════════
print("\n9) migrations/m011_audit_unique_constraints.py — ramo PostgreSQL")

from migrations import m011_audit_unique_constraints as M011  # noqa: E402

banco = ROOT / "scratch" / "pg_dialect_m011.db"
if banco.exists():
    banco.unlink()
eng = create_engine(f"sqlite:///{banco.as_posix()}")
with eng.begin() as c:
    # Tabelas MINIMAS e sem DEFAULT: e o estado de producao (schema anterior a
    # esta auditoria), o unico em que os dois ramos da m011 tem o que fazer.
    c.execute(text("CREATE TABLE conversations (id INTEGER PRIMARY KEY, whatsapp TEXT)"))
    c.execute(text("CREATE TABLE leads (id INTEGER PRIMARY KEY, "
                   "campos_personalizados TEXT, status_venda TEXT, is_active BOOLEAN)"))
    c.execute(text("CREATE TABLE messages (id INTEGER PRIMARY KEY, send_attempts INTEGER)"))

capturado = []
event.listen(eng, "before_cursor_execute",
             lambda conn, cur, st, par, ctx, many: capturado.append(st))

acoes = []
M011._apply_unique_indexes(eng, acoes)
ddl_index = [s for s in capturado if s.upper().startswith("CREATE UNIQUE INDEX")]
print(f"     {ddl_index[0] if ddl_index else '(nenhum)'}")
check(len(ddl_index) == 1,
      f"a m011 emitiu exatamente 1 CREATE UNIQUE INDEX para a unica tabela "
      f"presente (veio {len(ddl_index)})")
check(bool(ddl_index) and re.fullmatch(
    r"CREATE UNIQUE INDEX IF NOT EXISTS [a-z0-9_]+ ON [a-z0-9_]+ \([a-z0-9_, ]+\)",
    ddl_index[0]),
    "o DDL bate a forma que e valida e IDENTICA nos dois dialetos: "
    "`CREATE UNIQUE INDEX IF NOT EXISTS <nome> ON <tabela> (<colunas>)` "
    "(o `IF NOT EXISTS` de indice existe no PostgreSQL desde o 9.5)")
check("CONCURRENTLY" not in ddl_index[0].upper(),
      "sem CONCURRENTLY — ele nao roda dentro de bloco transacional e a m011 "
      "usa engine.begin() por objeto")

# Ramo F5: so PostgreSQL. Forcamos o nome do dialeto para capturar o statement
# REAL que producao receberia; o SQLite recusa a sintaxe, o que ja e a prova de
# que este ramo nunca foi exercitado pela suite.
acoes_skip = []
M011._apply_server_defaults(eng, acoes_skip)
check(any("skipped" in a for a in acoes_skip),
      "com dialeto sqlite a m011 PULA os SET DEFAULT (o SQLite nao tem ALTER "
      "COLUMN) — logo a suite nunca viu o SQL que producao vai receber")

eng.dialect.name = "postgresql"
alters = []
_alvos = list(M011._DEFAULT_TARGETS)
for alvo in _alvos:
    capturado.clear()
    M011._DEFAULT_TARGETS = [alvo]
    try:
        M011._apply_server_defaults(eng, [])
    except Exception:  # noqa: BLE001  o SQLite rejeita a sintaxe DEPOIS de emiti-la
        pass
    alters += [s for s in capturado if s.upper().startswith("ALTER TABLE")]
M011._DEFAULT_TARGETS = _alvos
eng.dialect.name = "sqlite"
for a in alters:
    print(f"     {a}")

check(len(alters) == 4,
      f"os quatro SET DEFAULT do F5 sao emitidos no ramo PostgreSQL "
      f"(vieram {len(alters)})")
_forma = re.compile(r"ALTER TABLE [a-z_]+ ALTER COLUMN [a-z_]+ SET DEFAULT .+")
check(all(_forma.fullmatch(a) for a in alters),
      "todos batem `ALTER TABLE <t> ALTER COLUMN <c> SET DEFAULT <expr>` — "
      "sintaxe valida no PostgreSQL e inexistente no SQLite")
check(all("USING" not in a and "TYPE" not in a for a in alters),
      "nenhum ALTER muda TIPO nem reescreve linha: SET DEFAULT so afeta INSERT "
      "futuro, entao a migration nao toca dado existente (como a docstring diz)")
check("ALTER TABLE leads ALTER COLUMN is_active SET DEFAULT true" in alters,
      "`true` (nao `1`) — literal booleano do PostgreSQL")
check("ALTER TABLE leads ALTER COLUMN campos_personalizados SET DEFAULT '{}'" in alters,
      "`'{}'` sem cast explicito: o PostgreSQL coage o literal desconhecido "
      "para o tipo da coluna (json)")

# Deteccao de duplicata: precisa RODAR nos dois dialetos, entao nao pode usar
# nada exclusivo de um deles.
dup_sql = None
_orig_find = M011._find_duplicates


def _espia(conn, table, cols):
    global dup_sql
    where = " AND ".join(f"d.{c} = t.{c}" for c in cols)
    sel = ", ".join(f"t.{c}" for c in cols)
    dup_sql = (f"SELECT t.id, {sel} FROM {table} t WHERE EXISTS "
               f"(SELECT 1 FROM {table} d WHERE {where} AND d.id <> t.id) "
               f"ORDER BY {sel}, t.id")
    return _orig_find(conn, table, cols)


with eng.begin() as c:
    c.execute(text("DROP INDEX uq_conversations_whatsapp"))
    c.execute(text("INSERT INTO conversations (whatsapp) VALUES ('5511900000001')"))
    c.execute(text("INSERT INTO conversations (whatsapp) VALUES ('5511900000001')"))
M011._find_duplicates = _espia
try:
    duplicadas = M011._find_duplicates(eng.connect(), "conversations", ("whatsapp",))
finally:
    M011._find_duplicates = _orig_find
print(f"     {dup_sql}")
check(len(duplicadas) == 2,
      "o SQL de deteccao de duplicata EXECUTA em SQLite e acha as duas linhas")
for proibido in ("string_agg", "group_concat", "::", "NOW()", "GROUP BY"):
    check(proibido not in dup_sql,
          f"o SQL de deteccao nao usa `{proibido}` — o EXISTS correlacionado e "
          f"a unica forma identica nos dois dialetos")

# O SQL assume `t.id`: sem essa coluna ele estoura no PostgreSQL, no meio de uma
# migration de producao, com erro do driver em vez de relatorio.
import app.models.operational.card  # noqa: E402,F401
from app.database import Base as CRM_BASE  # noqa: E402

for _nome, _tabela, _cols, _finding in M011._UNIQUE_TARGETS:
    t = CRM_BASE.metadata.tables.get(_tabela)
    if t is None:
        check(_tabela == "conversations",
              f"{_tabela} nao esta no metadata do CRM — so `conversations` "
              f"(dona do Conversas) pode faltar aqui")
        continue
    check("id" in t.c,
          f"{_tabela} tem coluna `id` — _find_duplicates faz SELECT t.id e "
          f"d.id <> t.id, e sem ela a m011 quebra em producao")


# ══════════════════════════════════════════════════════════════════════════
print()
print("10) campo personalizado: UMA linha impossivel nao pode derrubar a consulta de TODOS")
# AUDIT-2026-08-WF2 — SECAO REESCRITA. A versao anterior travava o MECANISMO de
# UMA implementacao: "o cast para jsonb tem de ficar DENTRO do guard", "o guard
# e strpos", "o guard esta num CASE aninhado". Duas consequencias, as duas
# ruins: ela quebrou assim que o mecanismo mudou, e — pior — ela dava PASS na
# implementacao que MORRIA em producao. `{"orcamento": 1e1000000}` e `json`
# valido, entra na coluna sem reclamar, passa inteiro por um guard de NUL e
# derruba o cast para jsonb com NumericValueOutOfRange. O F-043 nunca foi sobre
# `jsonb`: e sobre UMA linha derrubar a consulta de TODOS.
#
# A PROPRIEDADE travada aqui, que sobrevive a qualquer reimplementacao:
#
#   (P1) uma unica linha cujo conteudo o PostgreSQL nao consegue processar nao
#        pode derrubar o filtro de campo personalizado para TODOS os leads;
#   (P2) o guard tambem nao pode sumir com linha LEGITIMA em silencio — a ORM
#        grava com json.dumps(ensure_ascii=True), entao TODO acento do banco ja
#        e um escape de codepoint e um guard grosseiro apagaria meio CRM.
#
# O CORPUS abaixo E a especificacao dessas duas frases: cada entrada e um texto
# que o PostgreSQL ACEITA guardar numa coluna `json`, com o veredito de se a
# linha e legitima (tem de continuar encontravel) ou impossivel de processar
# (pode sumir do filtro, jamais derrubar a consulta). Ele alimenta as duas
# provas abaixo.
_B = chr(92)   # a barra literal, montada como em app/query_filters.py

# (rotulo, texto JSON exatamente como fica na coluna, e uma linha LEGITIMA?)
CORPUS_F043 = [
    ("ascii",               '{"origem": "Instagram"}',                             True),
    ("acento-ensure-ascii", '{"origem": "indica' + _B + 'u00e7' + _B + 'u00e3o"}', True),
    ("emoji-par-valido",    '{"origem": "festa ' + _B + 'ud83c' + _B + 'udf89"}',  True),
    ("u0000-sem-barra",     '{"origem": "ref-u0000-alpha"}',                       True),
    ("barra-escapada",      '{"origem": "C:' + _B * 2 + 'users"}',                 True),
    ("barra-mais-u0000",    '{"origem": "C:' + _B * 2 + 'u0000dir"}',              True),
    ("overflow-numerico",   '{"orcamento": 1e1000000, "origem": "x"}',             True),
    ("escape-de-nul",       '{"origem": "' + _B + 'u0000x"}',                      False),
    ("substituto-solto",    '{"origem": "' + _B + 'ud800 solto"}',                 False),
    ("legado-lista",        '["a", "b"]',                                          False),
    ("legado-escalar",      '"sou string"',                                        False),
]

from sqlalchemy import JSON as _JSON  # noqa: E402
from sqlalchemy.sql.elements import Case as _Case, Cast as _Cast  # noqa: E402
from sqlalchemy.sql.functions import Function as _Function  # noqa: E402

import app.query_filters as _qf  # noqa: E402

_md_cp = MetaData()
_t_cp = Table("leads_cp_prova", _md_cp,
              Column("id", Integer), Column("campos_personalizados", _JSON))
_col_cp = _t_cp.c.campos_personalizados


def _predicado_cp(is_sqlite):
    """O predicado REAL de app/query_filters.py, no dialeto pedido."""
    _orig = _qf.IS_SQLITE
    try:
        _qf.IS_SQLITE = is_sqlite
        # valor vazio = casa pela presenca da chave, para o corpus inteiro ser
        # julgado pelo guard e nao pelo conteudo do valor
        return _qf.campo_personalizado_match(_col_cp, "origem", "")
    finally:
        _qf.IS_SQLITE = _orig


# ─── PROVA A: comportamental, contra PostgreSQL de verdade ────────────────
# E a unica prova possivel de (P1): "o PostgreSQL nao estoura" nao se demonstra
# sem um PostgreSQL — o SQLite nao tem `jsonb`, aceita tudo e nunca reproduz.
# Roda quando DATABASE_URL apontar para um PostgreSQL descartavel. NAO e um
# skip disfarcado: a prova B abaixo roda SEMPRE e sozinha ja reprova a
# implementacao anterior. Sem servidor, o que se perde e forca, nao veredito.
if _PG_URL:
    print(f"     [A] corpus contra PostgreSQL real ({_PG_URL.split('@')[-1]})")
    _eng_cp = create_engine(_PG_URL)
    try:
        with _eng_cp.connect() as _con_cp:
            # TEMP TABLE: morre com a sessao, nao toca em dado de ninguem.
            _con_cp.execute(text(
                "CREATE TEMP TABLE leads_cp_prova (id int, campos_personalizados json)"))
            _guardadas = []
            for _i, (_rot, _txt, _leg) in enumerate(CORPUS_F043, 1):
                _sp = _con_cp.begin_nested()
                try:
                    _con_cp.execute(
                        text("INSERT INTO leads_cp_prova VALUES (:i, CAST(:j AS json))"),
                        {"i": _i, "j": _txt})
                    _sp.commit()
                    _guardadas.append(_rot)
                except Exception:  # noqa: BLE001
                    _sp.rollback()
            _recusadas = [r for r, _, _ in CORPUS_F043 if r not in _guardadas]
            check(not _recusadas,
                  f"o PostgreSQL ACEITA todo o corpus numa coluna `json` — se ele "
                  f"recusasse alguma entrada ja no INSERT, essa linha nunca chegaria "
                  f"ao filtro e o corpus estaria mentindo. Recusadas: {_recusadas}")

            try:
                _ids = {r[0] for r in _con_cp.execute(
                    select(_t_cp.c.id).where(_predicado_cp(is_sqlite=False)))}
                _erro_cp = None
            except Exception as exc:  # noqa: BLE001
                _ids = set()
                _erro_cp = f"{type(exc).__name__}: {str(exc).strip().splitlines()[0]}"
            check(_erro_cp is None,
                  f"(P1) a consulta RODA com o corpus inteiro na tabela — a linha "
                  f"que o PostgreSQL nao processa sai do RESULTADO, nunca derruba a "
                  f"consulta para todos os leads (F-043). Estourou com: {_erro_cp}")

            # P2 so tem resposta se houve resultado: com a consulta estourada
            # nao existe "sumiu em silencio" para medir, e P1 acima ja reprovou.
            if _erro_cp is None:
                _sumiram = [r for _i, (r, _t, _leg) in enumerate(CORPUS_F043, 1)
                            if _leg and _i not in _ids]
                check(not _sumiram,
                      f"(P2) nenhuma linha LEGITIMA sumiu em silencio: {_sumiram}. "
                      f"Acento, emoji e barra do Windows chegam ao banco como escape "
                      f"de codepoint — um guard que barre escape em bloco, ou que "
                      f"case a substring errada, apaga lead sem erro nenhum")
    finally:
        _eng_cp.dispose()
else:
    print("     [A] corpus contra PostgreSQL real: nao rodou (DATABASE_URL nao "
          "aponta para PostgreSQL). Rode com "
          "DATABASE_URL=postgresql+psycopg2://... para a prova completa")

# ─── PROVA B: estatica, sempre ────────────────────────────────────────────
# Estatica porque este arquivo, por contrato (ver o cabecalho), nao conecta em
# lugar nenhum e nao pula nada — e o CI nao tem PostgreSQL. Ela nao afirma "nao
# estoura"; afirma o unico invariante do qual isso decorre:
#
#   toda operacao que o SQL aplica a coluna CRUA tem de ser uma que o
#   PostgreSQL avalia para QUALQUER valor armazenavel em `json`.
#
# Isso nao e o mecanismo: nao diz qual guard usar, nem em que ordem, nem com
# que nome. Diz so que a coluna crua nao pode ser entregue a uma operacao
# parcial, porque operacao parcial roda LINHA A LINHA, antes de qualquer
# protecao — que e literalmente o F-043. Lida da ARVORE DE EXPRESSAO do
# SQLAlchemy, nao do texto do SQL: renomear identificador Python, alias ou
# parametro nao mexe neste check.
def _nome_da_op(no):
    """Nome SQL do no; None em no transparente (ClauseList, Grouping, `==`)."""
    if isinstance(no, _Function):
        return no.name.lower()
    if isinstance(no, _Cast):
        return f"cast:{no.type}".lower()
    if isinstance(no, _Case):
        return "case"
    return None


def _ops_sobre_a_coluna(expr, coluna=None):
    """Operacoes aplicadas DIRETAMENTE a coluna crua, em qualquer profundidade."""
    coluna = _col_cp if coluna is None else coluna
    achados, vistos = set(), set()

    def anda(no, dono):
        if id(no) in vistos:
            return
        vistos.add(id(no))
        nome = _nome_da_op(no) or dono
        for filho in no.get_children():
            if filho is coluna:
                achados.add(nome)
            else:
                anda(filho, nome)

    anda(expr, None)
    return achados


def _gates_da_coluna(expr):
    """CASEs que DEVOLVEM a coluna crua — a unica porta por onde ela pode
    chegar a uma operacao parcial sem que a linha ruim mate a consulta."""
    out, vistos = [], set()

    def anda(no):
        if id(no) in vistos:
            return
        vistos.add(id(no))
        if isinstance(no, _Case) and any(r is _col_cp for _, r in no.whens):
            out.append(no)
        for filho in no.get_children():
            anda(filho)

    anda(expr)
    return out


# Medido em PostgreSQL 16.14 contra o corpus acima: destas operacoes nenhuma
# levanta erro para qualquer valor que caiba numa coluna `json`. TODA operacao
# que le DENTRO do JSON (`->`, `->>`, `json_each*`, `json_object_keys`, e o
# cast para `jsonb`) des-escapa avidamente e estoura — essas so podem receber
# um valor que ja passou pelo gate.
_TOTAIS_PG = {
    "cast:varchar": "`::text` devolve o texto cru, sem des-escapar nada",
    "json_typeof": "le so o tipo do topo, nunca o conteudo",
    "case": "o GATE — o unico no que pode entregar a coluna crua adiante",
}
_TOTAIS_LITE = {
    "json_type": "gate de tipo: campos_personalizados legado nao-objeto nao vira par",
    "case": "o GATE",
}

_expr_pg = _predicado_cp(is_sqlite=False)
_parciais = _ops_sobre_a_coluna(_expr_pg) - set(_TOTAIS_PG)
check(not _parciais,
      f"(P1) no ramo PostgreSQL a coluna CRUA so chega a operacao TOTAL "
      f"{sorted(_TOTAIS_PG)}; encontradas alem dessas: {sorted(_parciais)}. "
      f"Operacao parcial sobre a coluna crua e avaliada linha a linha antes de "
      f"qualquer protecao — foi assim que `cast(coluna, JSONB)` transformou uma "
      f"linha legada em 500 permanente para TODOS os leads")

_gates = _gates_da_coluna(_expr_pg)
check(len(_gates) == 1,
      f"(P1) existe UM gate no ramo PostgreSQL — o CASE que decide se a linha "
      f"segue para a expansao dos pares. Encontrados: {len(_gates)}")
check(all(_ops_sobre_a_coluna(_cond) for _g in _gates for _cond, _ in _g.whens),
      "(P1) o gate INSPECIONA o valor que libera: um CASE cuja condicao nao "
      "olha a coluna nao e gate nenhum, e so um desvio que entrega a linha ruim "
      "adiante — passaria neste arquivo e cairia em producao")

_ops_lite = _ops_sobre_a_coluna(_predicado_cp(is_sqlite=True))
check(_ops_lite == set(_TOTAIS_LITE),
      f"ramo SQLite intocado: esperado {sorted(_TOTAIS_LITE)}, encontrado "
      f"{sorted(_ops_lite)}. Se aparecer aqui a maquinaria do ramo PostgreSQL, "
      f"alguem esta pagando custo por linha por um defeito que este dialeto nao "
      f"tem (`json` do SQLite nao tem a restricao do `jsonb`); se o gate de tipo "
      f"sumir, some tambem a paridade entre os dois ramos")

_con.close()
eng.dispose()

# ══════════════════════════════════════════════════════════════════════════
print()
print("11) created_at de mensagem: `now()` e o inicio da TRANSACAO no PostgreSQL")
# AUDIT-2026-08-WF2 — a divergencia mais silenciosa deste arquivo.
#
#   postgres: now()  ==  transaction_timestamp()  -> UM valor por TRANSACAO
#   sqlite  : CURRENT_TIMESTAMP                   -> avaliado por STATEMENT
#
# `_debounce_then_forward` (conversas/app/routers/webhook.py) abre a transacao
# numa leitura, chama a Bia no n8n (AGENT_TIMEOUT=240s; 1m30-2m40 reais),
# persiste cada parte da resposta com `commit=False` e so commita no fim. Com
# `server_default=func.now()` sozinho, TODAS as partes ficavam com o timestamp
# em que o debounce ACORDOU — anterior ao das mensagens que o cliente mandou
# durante a espera, cada uma commitada na transacao curta dela.
#
# PROVA em PostgreSQL 16.14 real, mesmo cenario, so a coluna mudando:
#
#   ANTES   22:01:00.316  inbound   1) cliente: quanto custa o tour?
#           22:01:00.350  outbound  3) Bia: R$ 250          <- now() da txn
#           22:01:00.350  outbound  4) Bia: almoco incluso  <- now() da txn
#           22:01:00.350  outbound  5) Bia: quer reservar?  <- now() da txn
#           22:01:02.350  inbound   2) cliente: e com almoco?
#           => a RESPOSTA ordena ANTES da PERGUNTA, no inbox e no `historico`
#
#   DEPOIS  22:01:15.174 / 16.761 / 18.286 x3  => ordem cronologica
#
# No SQLite as duas formas parecem iguais — e por isso a suite dizia verde.
# O que este bloco trava e a FORMA da coluna, nao o comportamento (que exige
# servidor): se alguem devolver `created_at` para o server_default sozinho, cai.
from datetime import datetime as _dt, timezone as _tz  # noqa: E402

_md_ts = MetaData()
_t_nu = Table(  # forma ANTIGA: quem decide e o servidor, uma vez por transacao
    "msgs_nu", _md_ts,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)
_t_ok = Table(  # forma CORRIGIDA: a ORM decide, a cada INSERT
    "msgs_ok", _md_ts,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True),
           default=lambda: _dt.now(_tz.utc), server_default=func.now()),
)

_ddl_nu_pg = " ".join(str(CreateTable(_t_nu).compile(dialect=PG)).split())
_ddl_nu_lite = " ".join(str(CreateTable(_t_nu).compile(dialect=LITE)).split())
_ddl_ok_pg = " ".join(str(CreateTable(_t_ok).compile(dialect=PG)).split())
print(f"     [PG]   {_ddl_nu_pg}")
print(f"     [LITE] {_ddl_nu_lite}")

check("DEFAULT now()" in _ddl_nu_pg,
      "PostgreSQL grava `DEFAULT now()` no DDL — e `now()` e o timestamp da "
      "TRANSACAO, nao do INSERT")
check("DEFAULT (CURRENT_TIMESTAMP)" in _ddl_nu_lite,
      "SQLite grava `DEFAULT CURRENT_TIMESTAMP`, avaliado por STATEMENT: o "
      "MESMO modelo se comporta diferente nos dois bancos")
check(_ddl_ok_pg.replace("msgs_ok", "msgs_nu") == _ddl_nu_pg,
      "o DDL das duas formas e IDENTICO — o default do lado do Python nao "
      "aparece no CREATE TABLE. Consequencia pratica: a correcao NAO exige "
      "migration e NAO cria drift com a tabela que ja existe em producao")

# Onde o valor e decidido: sem parametro no INSERT = o servidor decide (uma vez
# por transacao); com parametro = a ORM decide, ao emitir cada INSERT.
_eng_ts = create_engine("sqlite://")
_inserts = []
event.listen(
    _eng_ts, "before_cursor_execute",
    lambda conn, cur, stmt, params, ctx, many: _inserts.append(" ".join(stmt.split())),
)
_md_ts.create_all(_eng_ts)
with _eng_ts.begin() as _c_ts:
    _c_ts.execute(_t_nu.insert())
    _c_ts.execute(_t_ok.insert())
_ins_nu = next(i for i in _inserts if i.upper().startswith("INSERT INTO MSGS_NU"))
_ins_ok = next(i for i in _inserts if i.upper().startswith("INSERT INTO MSGS_OK"))
print(f"     [antiga]    {_ins_nu}")
print(f"     [corrigida] {_ins_ok}")
check("created_at" not in _ins_nu,
      "forma antiga: `created_at` nem aparece no INSERT — o valor vem do "
      "DEFAULT do servidor, que no PostgreSQL e o mesmo para a transacao toda")
check("created_at" in _ins_ok,
      "forma corrigida: `created_at` viaja como PARAMETRO do INSERT, avaliado "
      "no momento em que a ORM emite a linha — igual nos dois dialetos")
_eng_ts.dispose()

# ─── A TRAVA ───────────────────────────────────────────────────────────────
_conv_src = fonte("conversas/app/models/conversation.py")
_msg_src = _conv_src[_conv_src.index("class Message(Base):"):]

check(not re.search(
          r"created_at\s*=\s*Column\(\s*DateTime\(timezone=True\)\s*,\s*"
          r"server_default=func\.now\(\)\s*\)", _msg_src),
      "`Message.created_at` NAO e so `server_default=func.now()` — nessa "
      "forma, em PostgreSQL, TODA a resposta da Bia recebe o timestamp em que "
      "o debounce acordou e o historico ordena a resposta ANTES da pergunta")
check("default=lambda: datetime.now(timezone.utc)" in _msg_src,
      "`Message.created_at` tem default do lado do PYTHON — avaliado no "
      "INSERT, sem depender de dialeto nem de quando a transacao comecou")
check("server_default=func.now()" in _msg_src,
      "`server_default` FICA: e o DEFAULT do DDL para INSERT fora da ORM "
      "(psql, COPY, restore), onde a transacao e curta e `now()` basta. "
      "Remove-lo criaria drift com a tabela de producao")
check('order_by="Message.created_at, Message.id"' in _conv_src,
      "`Conversation.messages` desempata por `id`: com autoflush=False as "
      "partes da mesma resposta da Bia sao flushadas juntas e chegam a ficar a "
      "microssegundos (medido: 3us em PostgreSQL 16.14). Sem desempate a ordem "
      "entre elas e arbitraria — os outros dois leitores ja usam Message.id")

# Por que a trava existe: se estas duas coisas sairem do webhook, a transacao
# deixa de ficar aberta durante a chamada da Bia e a decisao pode ser revista.
_wh_src = fonte("conversas/app/routers/webhook.py")
check("AGENT_TIMEOUT = httpx.Timeout(240.0" in _wh_src,
      "webhook.py ainda permite 240s de chamada ao n8n DENTRO da transacao do "
      "debounce — e essa janela que transforma `now()` em timestamp errado")
check("commit=False," in _wh_src,
      "webhook.py ainda persiste as partes da resposta com `commit=False` "
      "(uma unica transacao para o lote inteiro)")
check("sessionmaker(autocommit=False, autoflush=False" in fonte("conversas/app/database.py"),
      "conversas/app/database.py mantem `autoflush=False` — nada flusha as "
      "partes antes do commit final, entao os timestamps delas sao vizinhos e "
      "o desempate por `id` e o que garante ordem TOTAL")

# ══════════════════════════════════════════════════════════════════════════
print()
if falhas:
    print(f"{len(falhas)} FALHA(S) — alguma divergencia de dialeto mudou de forma:")
    for f in falhas:
        print(f"  - {f}")
    sys.exit(1)
print("OK: todas as divergencias PostgreSQL/SQLite conhecidas seguem na forma travada")
