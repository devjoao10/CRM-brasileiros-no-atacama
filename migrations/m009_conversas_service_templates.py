"""
m009 — CONV-CURATION-01: curadoria de templates do atendimento (CONVERSAS).

Cria (aditivo, sem destruir nada):
  - tabela `service_templates` — quais templates APROVADOS PELA META o CRM
    autoriza a aparecer no seletor do atendimento.
      id             INTEGER PK
      name           VARCHAR(512) NOT NULL
      language       VARCHAR(10)  NOT NULL
      created_at     TIMESTAMP(TZ)
  - UNIQUE (name, language) — a identidade de um template na Meta e o PAR,
    nunca o nome sozinho (a mesma conta pode ter o mesmo nome em varios idiomas).

BOOTSTRAP DELIBERADAMENTE VAZIO. A migration NAO insere linha nenhuma.

Isso e a decisao central deste pacote, nao um esquecimento: a conta tem 34
templates APPROVED, entre eles `alerta_novo_lead`, `alerta_crm`,
`notificacao_crm`, `hello_world` e `teste`. Autorizar todos no bootstrap
preservaria exatamente o problema que este pacote existe para resolver —
templates internos oferecidos a clientes. Autorizar por heuristica de nome
(prefixo `alerta_`, etc.) seria adivinhar finalidade a partir de string.

Consequencia operacional, ACEITA E EXPLICITA: apos o deploy, o seletor do
atendimento comeca VAZIO e um administrador precisa liberar em Templates >
"Disponiveis no atendimento" antes do primeiro uso. A UI diz isso ao operador
com todas as letras, e o composer permanece BLOQUEADO (fail closed) — nunca
liberando texto livre como consolo.

Nao toca `message_templates`, nao altera status vindo da Meta, nao apaga dados.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja
nascem completos via `Base.metadata.create_all()` no lifespan do Conversas.
Este script reconcilia bancos EXISTENTES (dev/staging/prod).

ATENCAO — este e o app CONVERSAS (nao o CRM): o script insere `conversas/` no
inicio do sys.path para que `app.*` resolva para conversas/app (mesma tecnica
dos tests/test_conversas_*). Rodar em PROCESSO PROPRIO.

Ordem em bancos antigos: m003 -> m004 -> m005 -> m006 -> m007 -> m008 -> m009.

Uso (LOCAL / STAGING):
    python migrations/m009_conversas_service_templates.py

PRODUCAO: somente apos backup verificado + aprovacao humana (migrations/README.md).
"""
import logging
import pathlib
import sys

# `app.*` deve resolver para conversas/app — ver docstring.
_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.config import DATABASE_URL  # noqa: E402 — config do CONVERSAS
from app.database import Base  # noqa: E402
from app.models.template import ServiceTemplate  # noqa: E402,F401 — registra a tabela

logger = logging.getLogger("migrations.m009")

TABELA = "service_templates"
CHAVE = ("name", "language")
NOME_UNIQUE = "uq_service_templates_name_language"


class SchemaQuebrado(RuntimeError):
    """O invariante da tabela nao vale e a migration NAO pode dizer que esta OK."""


class DuplicatasEncontradas(SchemaQuebrado):
    """Existe dado que impede criar a UNIQUE. Reconciliacao e decisao humana."""


def _unique_vigente(engine):
    """
    Nome do objeto que garante UNIQUE sobre (name, language), ou None.

    AUDIT-2026-08-F2 — casa por CONJUNTO DE COLUNAS, nao por nome, e olha
    constraints E indices unicos. Os tres motivos:

      * o nome nao sobrevive ao dialeto. Uma UNIQUE declarada no CREATE TABLE
        vira, no SQLite, o auto-indice `sqlite_autoindex_service_templates_1`;
        procurar `uq_service_templates_name_language` diria AUSENTE numa tabela
        recem-criada e perfeitamente correta;
      * no PostgreSQL, `ALTER TABLE ... ADD CONSTRAINT` aparece em
        `get_unique_constraints` e `CREATE UNIQUE INDEX` aparece em
        `get_indexes` — sao lugares diferentes para a mesma garantia;
      * se a unicidade ja existir sob OUTRO nome (um `ADD CONSTRAINT` antigo,
        por exemplo), o invariante esta valendo e criar um segundo objeto so
        gastaria espaco. O que importa e a garantia, nao a nomenclatura.
    """
    insp = inspect(engine)
    alvo = set(CHAVE)
    for u in insp.get_unique_constraints(TABELA):
        if set(u.get("column_names") or []) == alvo:
            return u.get("name") or "<sem nome>"
    for i in insp.get_indexes(TABELA):
        if i.get("unique") and set(i.get("column_names") or []) == alvo:
            return i.get("name") or "<sem nome>"
    return None


def _duplicatas(conn):
    """
    Linhas cuja chave (name, language) se repete.

    EXISTS correlacionado em vez de GROUP BY com agregacao de ids: `string_agg`
    e `group_concat` tem nomes diferentes por dialeto, e esta consulta precisa
    ser identica no PostgreSQL de producao e no SQLite de desenvolvimento.
    Coluna NULL nao colide em UNIQUE, e `d.c = t.c` tambem e falso para NULL —
    os dois criterios coincidem, entao nao ha falso positivo.
    """
    onde = " AND ".join(f"d.{c} = t.{c}" for c in CHAVE)
    cols = ", ".join(f"t.{c}" for c in CHAVE)
    sql = (
        f"SELECT t.id, {cols} FROM {TABELA} t "
        f"WHERE EXISTS (SELECT 1 FROM {TABELA} d WHERE {onde} AND d.id <> t.id) "
        f"ORDER BY {cols}, t.id"
    )
    return conn.execute(text(sql)).fetchall()


def _relatorio_duplicatas(linhas):
    por_chave = {}
    for linha in linhas:
        por_chave.setdefault(tuple(linha[1:]), []).append(linha[0])
    partes = [
        f"{TABELA}: {len(por_chave)} par(es) (name, language) repetido(s) — "
        f"{len(linhas)} linha(s) envolvida(s). NADA foi alterado.",
    ]
    for chave, ids in sorted(por_chave.items(), key=lambda kv: [str(x) for x in kv[0]]):
        legivel = ", ".join(f"{c}={v!r}" for c, v in zip(CHAVE, chave))
        partes.append(f"  {legivel}  ->  ids {ids}")
    partes.append(
        "Decida qual linha fica e remova as demais A MAO, com backup verificado. "
        "Esta migration nao escolhe por voce: as duas linhas parecem iguais aqui, "
        "mas so quem cuida da curadoria sabe qual foi a autorizada de fato."
    )
    return "\n".join(partes)


def _criar_unique(engine):
    """
    Emite o DDL que passa a garantir UNIQUE (name, language).

    PostgreSQL e producao, entao ele ganha uma CONSTRAINT de verdade — que e
    onde `get_unique_constraints` procura e o que o modelo declara. O SQLite nao
    tem `ALTER TABLE ... ADD CONSTRAINT`; la a unica forma de acrescentar a
    garantia a uma tabela existente e um indice unico, que impoe exatamente a
    mesma regra. A diferenca e de sintaxe, nao de rigor: nenhum dos dois ramos
    e mais permissivo que o outro, e a verificacao depois do DDL e a mesma.
    """
    dialeto = engine.dialect.name
    if dialeto == "sqlite":
        ddl = f'CREATE UNIQUE INDEX "{NOME_UNIQUE}" ON {TABELA} (name, language)'
    else:
        ddl = (
            f'ALTER TABLE {TABELA} '
            f'ADD CONSTRAINT "{NOME_UNIQUE}" UNIQUE (name, language)'
        )
    with engine.begin() as conn:
        conn.execute(text(ddl))
    return ddl


def run(engine=None):
    """
    Reconcilia o schema de `service_templates`. Devolve a lista de acoes.

    LEVANTA em vez de devolver quando o invariante nao pode ser garantido — e
    essa e a correcao de AUDIT-2026-08-F2. Antes, a ausencia da UNIQUE virava
    uma string no log e a migration terminava com exit 0: um pipeline via
    sucesso sobre um schema quebrado.
    """
    engine = engine or create_engine(DATABASE_URL)
    actions = []

    if TABELA in inspect(engine).get_table_names():
        actions.append(f"{TABELA}:already-present")
    else:
        # create_all com tables=[...] cria SO esta tabela: nenhuma outra tabela
        # do metadata e tocada, mesmo que exista drift em alguma delas.
        Base.metadata.create_all(bind=engine, tables=[ServiceTemplate.__table__])
        actions.append(f"{TABELA}:created")

    vigente = _unique_vigente(engine)
    if vigente:
        actions.append(f"{NOME_UNIQUE}:present (via {vigente})")
    else:
        # Ausente numa tabela que ja existia. Antes de qualquer DDL, olhar o
        # DADO: criar a UNIQUE sobre linhas duplicadas falharia no meio, e
        # "resolver" apagando linha seria a migration decidindo sozinha qual
        # autorizacao de template vale.
        with engine.connect() as conn:
            dupes = _duplicatas(conn)
        if dupes:
            raise DuplicatasEncontradas(_relatorio_duplicatas(dupes))

        ddl = _criar_unique(engine)
        actions.append(f"{NOME_UNIQUE}:criada ({ddl})")

        # Confirmar que o DDL de fato produziu a garantia. Sem isto voltariamos
        # ao mesmo fail-open por outro caminho: DDL aceito, invariante ausente.
        confirmado = _unique_vigente(engine)
        if not confirmado:
            raise SchemaQuebrado(
                f"{NOME_UNIQUE}: o DDL foi aceito mas a unicidade sobre "
                f"{CHAVE} continua ausente. Verifique o schema a mao antes de "
                f"seguir — nao ha migration a repetir aqui."
            )

    # Bootstrap vazio e INTENCIONAL — ver docstring. Reportado para nao parecer
    # que a migration falhou em popular alguma coisa.
    with engine.connect() as conn:
        total = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {TABELA}").scalar()
    actions.append(f"autorizacoes-existentes:{total} (bootstrap vazio e proposital)")

    return actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m009] alvo (conversas): {safe}")
    try:
        print("[m009] acoes:", run())
    except DuplicatasEncontradas as e:
        print("[m009] ABORTADA — dado impede criar a UNIQUE (name, language):")
        print(str(e))
        print("[m009] nenhuma linha foi apagada, alterada ou deduplicada.")
        sys.exit(2)
    except SchemaQuebrado as e:
        print(f"[m009] ABORTADA — schema nao integro: {e}")
        sys.exit(1)
    # So chega aqui com a tabela presente, a unicidade vigente e o dado intacto.
    print("[m009] OK (idempotente)")
