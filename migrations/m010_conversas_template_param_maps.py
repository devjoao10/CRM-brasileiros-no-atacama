"""
m010 — CONV-TPLMAP-01: mapeamento {{n}} -> @VARIAVEL de templates (CONVERSAS).

Cria (aditivo, sem destruir nada):
  - tabela `template_param_maps` — de que variavel interna sai o valor de cada
    parametro posicional do BODY de um template da Meta.
      id          INTEGER PK
      name        VARCHAR(512) NOT NULL
      language    VARCHAR(10)  NOT NULL
      position    INTEGER      NOT NULL   -- o n de {{n}}, base 1
      token       VARCHAR(61)  NOT NULL   -- "@PRIMEIRONOMECLIENTE"
      created_at  TIMESTAMP(TZ)
  - UNIQUE (name, language, position) — a identidade de um template e o PAR
    (name, language), e cada posicao admite NO MAXIMO um mapeamento. A regra
    fica no BANCO, nao numa verificacao em Python que o proximo endpoint
    esquece de chamar.

BOOTSTRAP DELIBERADAMENTE VAZIO. A migration NAO insere linha nenhuma.

Sem mapeamento, `_build_template_send` exige exatamente os mesmos parametros
que exigia antes deste pacote — o comportamento manual atual e preservado byte
a byte. Popular por heuristica (ex.: "todo {{1}} deve ser o primeiro nome")
transformaria templates antigos em automaticos por adivinhacao, exatamente o
que o pacote proibe. Mapear e ato explicito do administrador em
Templates > Variaveis do template.

Nao toca `message_templates`, nao toca `service_templates`, nao fala com a Meta,
nao altera BODY aprovado nenhum, nao apaga dados.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja
nascem completos via `Base.metadata.create_all()` no lifespan do Conversas.
Este script reconcilia bancos EXISTENTES (dev/staging/prod).

ATENCAO — este e o app CONVERSAS (nao o CRM): o script insere `conversas/` no
inicio do sys.path para que `app.*` resolva para conversas/app (mesma tecnica
dos tests/test_conversas_*). Rodar em PROCESSO PROPRIO.

Ordem em bancos antigos: m003 -> m004 -> m005 -> m006 -> m007 -> m008 -> m009 -> m010.

Uso (LOCAL / STAGING):
    python migrations/m010_conversas_template_param_maps.py

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
from app.models.template import TemplateParamMap  # noqa: E402,F401 — registra a tabela

logger = logging.getLogger("migrations.m010")

TABELA = "template_param_maps"
# TRES colunas. A m009 resolve o mesmo defeito com uma chave de DUAS, e a
# diferenca nao e cosmetica: chavear por (name, language) aqui acusaria como
# conflito o par legitimo {{1}} e {{2}} do mesmo template.
CHAVE = ("name", "language", "position")
NOME_UNIQUE = "uq_template_param_maps_key"


class SchemaQuebrado(RuntimeError):
    """O invariante da tabela nao vale e a migration NAO pode dizer que esta OK."""


class DuplicatasEncontradas(SchemaQuebrado):
    """Existe dado que impede criar a garantia. Reconciliar e decisao humana."""


def _unique_vigente(engine):
    """
    Nome do objeto que garante UNIQUE sobre (name, language, position), ou None.

    AUDIT-2026-08-F2 — casa por CONJUNTO DE COLUNAS, nao por nome, e olha
    constraints E indices unicos:

      * o nome nao sobrevive ao dialeto — uma UNIQUE declarada no CREATE TABLE
        vira, no SQLite, um auto-indice sem o nome da constraint, e procurar
        `uq_template_param_maps_key` diria AUSENTE numa tabela recem-criada e
        perfeitamente correta;
      * no PostgreSQL, `ALTER TABLE ... ADD CONSTRAINT` aparece em
        `get_unique_constraints` e `CREATE UNIQUE INDEX` aparece em
        `get_indexes` — dois lugares para a mesma garantia;
      * se a unicidade ja existir sob outro nome, o invariante esta valendo e
        criar um segundo objeto so gastaria espaco.
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
    Linhas cujo trio (name, language, position) se repete, com o `token` de cada
    uma — e o token que faz o conflito ser decidivel: duas linhas com o mesmo
    trio e tokens diferentes sao exatamente a ambiguidade que a UNIQUE impede.

    EXISTS correlacionado em vez de GROUP BY com agregacao de ids: `string_agg`
    e `group_concat` tem nomes diferentes por dialeto, e esta consulta precisa
    ser identica no PostgreSQL de producao e no SQLite de desenvolvimento.
    Coluna NULL nao colide em UNIQUE, e `d.c = t.c` tambem e falso para NULL —
    os dois criterios coincidem, entao nao ha falso positivo.
    """
    onde = " AND ".join(f"d.{c} = t.{c}" for c in CHAVE)
    cols = ", ".join(f"t.{c}" for c in CHAVE)
    sql = (
        f"SELECT t.id, {cols}, t.token FROM {TABELA} t "
        f"WHERE EXISTS (SELECT 1 FROM {TABELA} d WHERE {onde} AND d.id <> t.id) "
        f"ORDER BY {cols}, t.id"
    )
    return conn.execute(text(sql)).fetchall()


def _relatorio_duplicatas(linhas):
    """
    Um bloco por grupo em conflito, com os ids e o token de cada linha.

    A ordenacao usa a POSICAO COMO NUMERO. Ordenar a chave inteira como texto
    poria `position` 10 antes de 2 num template com dez parametros, e um
    relatorio que lista fora de ordem e mais dificil de conferir a mao —
    justamente quando alguem esta decidindo qual linha apagar em producao.
    """
    grupos = {}
    for id_, nome, idioma, posicao, token in linhas:
        grupos.setdefault((nome, idioma, posicao), []).append((id_, token))

    partes = [
        f"{TABELA}: {len(grupos)} grupo(s) (name, language, position) repetido(s) — "
        f"{len(linhas)} linha(s) envolvida(s). NADA foi alterado.",
    ]
    for (nome, idioma, posicao), itens in sorted(
        grupos.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), int(kv[0][2]))
    ):
        ids = [i for i, _ in itens]
        partes.append(
            f"  name={nome!r} language={idioma!r} position={posicao}  ->  ids {ids}"
        )
        for id_, token in itens:
            partes.append(f"      id={id_}  token={token!r}")
    partes.append(
        "Cada posicao admite NO MAXIMO um mapeamento. Decida qual linha fica e "
        "remova as demais A MAO, com backup verificado. Esta migration nao "
        "escolhe por voce, e nao mexe em token nenhum: os tokens acima estao "
        "impressos justamente porque e a diferenca entre eles que decide."
    )
    return "\n".join(partes)


def _criar_unique(engine):
    """
    Emite o DDL que passa a garantir UNIQUE (name, language, position).

    PostgreSQL e producao, entao ele ganha uma CONSTRAINT de verdade, com o nome
    `uq_template_param_maps_key` — que e onde `get_unique_constraints` procura e
    o que o modelo declara. O SQLite nao tem `ALTER TABLE ... ADD CONSTRAINT`;
    la a unica forma de acrescentar a garantia a uma tabela existente e um
    indice unico, que impoe a mesma regra. A diferenca e de sintaxe, nao de
    rigor, e a verificacao depois do DDL e a mesma nos dois.
    """
    colunas = ", ".join(CHAVE)
    if engine.dialect.name == "sqlite":
        ddl = f'CREATE UNIQUE INDEX "{NOME_UNIQUE}" ON {TABELA} ({colunas})'
    else:
        ddl = f'ALTER TABLE {TABELA} ADD CONSTRAINT "{NOME_UNIQUE}" UNIQUE ({colunas})'
    with engine.begin() as conn:
        conn.execute(text(ddl))
    return ddl


def run(engine=None):
    """
    Reconcilia o schema de `template_param_maps`. Devolve a lista de acoes.

    LEVANTA em vez de devolver quando o invariante nao pode ser garantido — e
    essa e a correcao de AUDIT-2026-08-F2. Antes, a ausencia da UNIQUE virava
    uma string no log e a migration terminava com exit 0, sobre um schema em que
    duas linhas podiam mapear a MESMA posicao para tokens diferentes.
    """
    engine = engine or create_engine(DATABASE_URL)
    actions = []

    if TABELA in inspect(engine).get_table_names():
        actions.append(f"{TABELA}:already-present")
    else:
        # create_all com tables=[...] cria SO esta tabela: nenhuma outra tabela
        # do metadata e tocada, mesmo que exista drift em alguma delas.
        Base.metadata.create_all(bind=engine, tables=[TemplateParamMap.__table__])
        actions.append(f"{TABELA}:created")

    vigente = _unique_vigente(engine)
    if vigente:
        actions.append(f"{NOME_UNIQUE}:present (via {vigente})")
    else:
        # Ausente numa tabela que ja existia. Antes de qualquer DDL, olhar o
        # DADO: criar a garantia sobre linhas repetidas falharia no meio, e
        # "resolver" removendo linha seria a migration decidindo sozinha qual
        # variavel alimenta um parametro de template.
        with engine.connect() as conn:
            dupes = _duplicatas(conn)
        if dupes:
            raise DuplicatasEncontradas(_relatorio_duplicatas(dupes))

        ddl = _criar_unique(engine)
        actions.append(f"{NOME_UNIQUE}:criada ({ddl})")

        # Confirmar que o DDL de fato produziu a garantia. Sem isto voltariamos
        # ao mesmo fail-open por outro caminho: DDL aceito, invariante ausente.
        if not _unique_vigente(engine):
            raise SchemaQuebrado(
                f"{NOME_UNIQUE}: o DDL foi aceito mas a unicidade sobre "
                f"{CHAVE} continua ausente. Verifique o schema a mao antes de "
                f"seguir — nao ha migration a repetir aqui."
            )

    # Bootstrap vazio e INTENCIONAL — ver docstring.
    with engine.connect() as conn:
        total = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {TABELA}").scalar()
    actions.append(f"mapeamentos-existentes:{total} (bootstrap vazio e proposital)")

    return actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m010] alvo (conversas): {safe}")
    try:
        print("[m010] acoes:", run())
    except DuplicatasEncontradas as e:
        print("[m010] ABORTADA — dado impede criar a garantia de unicidade:")
        print(str(e))
        print("[m010] nenhuma linha foi removida, alterada ou deduplicada.")
        sys.exit(2)
    except SchemaQuebrado as e:
        print(f"[m010] ABORTADA — schema nao integro: {e}")
        sys.exit(1)
    # So chega aqui com a tabela presente, a unicidade vigente e o dado intacto.
    print("[m010] OK (idempotente)")
