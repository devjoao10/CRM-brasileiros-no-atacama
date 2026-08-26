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
print("10) campo personalizado: o cast json->jsonb precisa ficar DENTRO do guard")
# AUDIT-2026-08-WG (F-043) — travamento da FORMA. A prova de COMPORTAMENTO exige
# um PostgreSQL de verdade (o SQLite nao tem `jsonb` e nunca reproduz), e esta em
# `docs/audit/POSTGRES_VALIDATION.md`: `'{"origem":"\u0000x"}'::json` e aceito,
# `::json::jsonb` levanta `UntranslatableCharacter`, e UMA linha assim derrubava a
# consulta inteira — o filtro de campo personalizado e todo segmento que o usasse
# viravam 500 permanente para TODOS os leads.
#
# O defeito era a ORDEM: `cast(coluna, JSONB)` estava FORA do CASE, entao era
# avaliado por linha antes de qualquer protecao. O que este check trava e
# exatamente isso — o guard de TEXTO tem de aparecer ANTES do primeiro cast para
# jsonb no SQL emitido. Um `check` de presenca do NOT LIKE em qualquer lugar
# passaria com o cast de volta para fora, que e a regressao real.
from sqlalchemy import Column as _Col, Integer as _Int, JSON as _JSON, MetaData as _MD, Table as _Tbl

import app.query_filters as _qf  # noqa: E402

_md_cp = _MD()
_t_cp = _Tbl("leads_cp", _md_cp, _Col("id", _Int), _Col("campos_personalizados", _JSON))

_qf_sqlite_original = _qf.IS_SQLITE
try:
    _qf.IS_SQLITE = False
    _sql_pg = sql(
        select(_t_cp.c.id).where(
            _qf.campo_personalizado_match(_t_cp.c.campos_personalizados, "origem", "x")),
        PG,
    )
finally:
    _qf.IS_SQLITE = _qf_sqlite_original

_up = _sql_pg.upper()
# AUDIT-2026-08-WG (revisao 2) — o guard e `strpos`, nao `LIKE`. No PostgreSQL a
# BARRA e o caractere de escape default do LIKE, entao `LIKE '%BARRAu0000%'`
# significava `LIKE '%u0000%'` e barrava valor legitimo com essa substring sem
# barra. `strpos` procura a substring literal, sem semantica de escape.
_pos_guard = _up.find("STRPOS")
_pos_cast = _up.find("AS JSONB")
check("NOT LIKE" not in _up,
      "o guard NAO usa LIKE — a barra seria consumida como escape e o padrao "
      "casaria texto sem barra nenhuma")
check(_pos_guard != -1,
      "ramo PostgreSQL tem o guard de texto contra o escape de NUL (via strpos)")
check(_pos_cast != -1, "ramo PostgreSQL ainda faz o cast para jsonb")
check(-1 < _pos_guard < _pos_cast,
      f"o guard vem ANTES do primeiro cast para jsonb "
      f"(guard@{_pos_guard}, cast@{_pos_cast}) — se o cast voltar para fora do "
      f"CASE, uma unica linha legada derruba o filtro para todos")

# AUDIT-2026-08-WG (revisao) — o guard precisa estar num CASE PROPRIO, nao
# num `AND` dentro do mesmo WHEN. O PostgreSQL nao garante curto-circuito de
# `AND` (a ordem de avaliacao e livre para o planner), so de `CASE`. Com o
# `AND`, o cast para jsonb PODE ser avaliado antes do guard e reproduzir a
# queda — e isso depende de estatistica e volume, entao passa em teste pequeno
# e falha em producao. Este check mata o retorno para a forma `AND`.
_prefixo_ate_cast = _up[:_pos_cast]
check(_prefixo_ate_cast.count("CASE WHEN") >= 2,
      "o guard esta num CASE proprio (aninhado), nao num AND dentro do mesmo WHEN — "
      "so CASE tem curto-circuito garantido no PostgreSQL")
check(" AND " not in _up[_pos_guard:_pos_cast],
      "nao ha `AND` entre o guard e o cast — seria exatamente a forma sem garantia")
check(_qf._ESCAPE_NUL == chr(92) + "u0000",
      "o padrao procurado sao os SEIS caracteres do escape, nao um byte NUL")

# O ramo SQLite NAO muda: `json` do SQLite nao tem a restricao do `jsonb`.
_qf_sqlite_original = _qf.IS_SQLITE
try:
    _qf.IS_SQLITE = True
    _sql_lite = sql(
        select(_t_cp.c.id).where(
            _qf.campo_personalizado_match(_t_cp.c.campos_personalizados, "origem", "x")),
        LITE,
    )
finally:
    _qf.IS_SQLITE = _qf_sqlite_original
check("NOT LIKE" not in _sql_lite.upper(),
      "ramo SQLite segue sem o guard — la o defeito nao existe e o custo seria gratuito")
check("json_each" in _sql_lite,
      "ramo SQLite continua usando json_each")

_con.close()
eng.dispose()

# ══════════════════════════════════════════════════════════════════════════
print()
if falhas:
    print(f"{len(falhas)} FALHA(S) — alguma divergencia de dialeto mudou de forma:")
    for f in falhas:
        print(f"  - {f}")
    sys.exit(1)
print("OK: todas as divergencias PostgreSQL/SQLite conhecidas seguem na forma travada")
