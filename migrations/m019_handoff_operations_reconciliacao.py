"""
BIA-V2 R15/R17 (fechamento de R-B, CONVERSAS) - acrescenta a `handoff_operations`
cinco colunas que sustentam a reconciliacao (R15) e a retomada (R16) de operacoes
de handoff BIA -> humano, mais o indice parcial que as duas usam para reclamar
trabalho pendente:

    handoff_operations.expected_responsavel_id INTEGER        NULL
    handoff_operations.attempt_count           INTEGER        NOT NULL DEFAULT 0
    handoff_operations.next_attempt_at         TIMESTAMP(TZ)  NULL
    handoff_operations.reconciled_at           TIMESTAMP(TZ)  NULL
    handoff_operations.error_class             VARCHAR(32)    NULL

    + indice parcial:
      CREATE INDEX ix_handoff_operations_pendentes
      ON handoff_operations (next_attempt_at)
      WHERE status IN ('REQUESTED','FAILED')

Especificacao normativa: secao 8 ("Conversas (m019)") de
`bia-v2-r15-r17-design-v4.md`, com a regra de particao de fila entre R15/R16 na
secao 4.2 do mesmo documento. Nada aqui e escolha deste arquivo; tudo vem de la.
A migration irma do lado CRM (m018, `lead_handoff_operations.
previous_responsavel_id`) e INDEPENDENTE desta: tabelas diferentes, nenhuma
ordem de aplicacao e exigida entre as duas.

PARA QUE CADA COLUNA SERVE (schema apenas — a LOGICA de R15/R16 vive em
`app/v2/handoff.py` e no worker, fora do escopo desta migration)
--------------------------------------------------------------------
`expected_responsavel_id` — o `responsavel_id` que a operacao esperava
encontrar no CRM no momento em que foi criada; usado pelo CAS da reconciliacao
(R15), nao o valor atual. `attempt_count` — quantas vezes R16 ja tentou
retomar esta operacao. `next_attempt_at` — proximo instante elegivel para
retomada; e a coluna que o indice parcial abaixo cobre. `reconciled_at` —
instante em que R15 resolveu a linha; NULL enquanto pendente. `error_class` —
NAO_APLICADO_RETRIAVEL/AMBIGUO_RETRIAVEL/etc., a CLASSE do erro que a regra de
particao de fila do §4.2 usa para decidir se R16 e elegivel: `error_code`
sozinho NAO determina a classe (o mesmo token pode ser retriavel OU ambiguo
dependendo do status HTTP que o originou — ver §8 do desenho), por isso a
classe precisa de coluna propria, gravada por `_registrar_falha` junto do
`error_code` na MESMA transacao.

POR QUE NENHUMA DAS 5 COLUNAS RECEBE BACKFILL
----------------------------------------------
As 4 nullable sem DEFAULT (`expected_responsavel_id`, `next_attempt_at`,
`reconciled_at`, `error_class`) - NULL ja e o valor correto para toda linha
existente (mesma logica de `closed_at` na m014): nao ha "vazio" para preencher,
so ausencia legitima de reconciliacao/retomada ainda nao iniciada.

`attempt_count` TEM DEFAULT (0), mas embutido no proprio `ADD COLUMN ... NOT
NULL DEFAULT 0`: SQLite e PostgreSQL preenchem TODAS as linhas existentes com
0 como parte do MESMO comando DDL, atomicamente. Ao contrario de
`state_version` (m014, `conversations`), que e NULLABLE e por isso pode, em
tese, acabar com NULL vindo de um INSERT externo a ORM, aqui isso e
ESTRUTURALMENTE IMPOSSIVEL: a coluna nasce NOT NULL, entao nenhuma linha
jamais tera NULL em `attempt_count` depois que ela existir. Um backfill
`WHERE attempt_count IS NULL` seria codigo morto, sempre 0 linhas -
propositalmente OMITIDO em vez de mantido "por precaucao". A verificacao
pos-DDL prova a garantia certa para uma coluna NOT NULL: reinspeciona
`nullable=False` no catalogo, nao conta linhas (essa segunda forma so faz
sentido para colunas nullable-com-default, como state/state_version).

INDICE PARCIAL — SINTAXE IDENTICA NOS DOIS DIALETOS, DECLARADO NOS DOIS DONOS
------------------------------------------------------------------------------
`CREATE INDEX IF NOT EXISTS ... WHERE <predicado>` e sintaxe portavel: SQLite
suporta indice parcial desde a 3.8.0, PostgreSQL desde sempre - mesmo
principio ja provado nesta base pela m013 (DDL identico, so o TIPO de outras
colunas varia por dialeto), agora estendido a um indice inteiro. Nenhum ramo
por dialeto e necessario aqui.

O indice tambem e declarado no MODEL
(`conversas/app/models/handoff_op.py:HandoffOperation.__table_args__`), com
`sqlite_where=`/`postgresql_where=` (os dois, nao so o de producao - os testes
locais e o CI rodam em SQLite), pelo MESMO motivo ja documentado em
`Conversation.whatsapp`, `ConversationEvent` e no proprio
`uq_handoff_operations_event_id` desta tabela: sem isso, `create_all()`
(bancos novos) e esta migration (bancos existentes) emitiriam esquemas
DIFERENTES - um banco novo nasceria SEM o indice que um banco migrado tem.
Mesmo nome, mesma coluna, mesmo predicado nos dois lugares; nao editar um sem
o outro.

Este indice e COMPARTILHADO por R15 e R16 (secao 4.2 do desenho): os dois
workers fazem `FOR UPDATE SKIP LOCKED` sobre a MESMA condicao
`(next_attempt_at) WHERE status IN ('REQUESTED','FAILED')`. A regra que
decide qual das duas linhas cada worker pode reclamar (R16 elegivel so para
`REQUESTED` ou `FAILED`+`error_class` retriavel/ambiguo; R15 dono exclusivo de
`FAILED` com `error_code` de colisao) vive na LOGICA de `handoff.py`/worker,
nao no indice - esta migration so cria a estrutura, nunca escreve nela.

COMPATIBILIDADE COM A V1 E COM A M016
---------------------------------------
Total. As 5 colunas sao aditivas e nullable/com DEFAULT; nenhum fluxo V1 ou
V2 anterior a este WP as le ou escreve - `app/v2/handoff.py` hoje constroi
`HandoffOperation` so com `event_id`/`conversation_id`/`target_user_id`/
`status`, que continuam funcionando sem alteracao (os 5 campos novos ficam
omitidos do INSERT e recebem o DEFAULT do banco, mesmo mecanismo de
`send_attempts`/`state_version`). Rollback: deixar sem uso. NUNCA `DROP
COLUMN`/`DROP INDEX` (padrao do projeto, ver `migrations/README.md`).

Roda em processo proprio do Conversas:

    DATABASE_URL=postgresql://... python migrations/m019_handoff_operations_reconciliacao.py
    python migrations/m019_handoff_operations_reconciliacao.py --allow-sqlite

Aplicacao em PRODUCAO e acao humana com backup + aprovacao (ver
`migrations/README.md`). Agentes de IA NAO executam isto.
"""
import logging
import os
import pathlib
import sys

# `app.*` deve resolver para conversas/app — ver docstring.
_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

logger = logging.getLogger(__name__)


class WrongTargetError(RuntimeError):
    """O alvo nao tem a tabela `handoff_operations`: banco errado, nao 'NO-OP'."""


_TABELA = "handoff_operations"

# So o TIPO muda por dialeto, nunca a FORMA do DDL — mesmo padrao de m003/m012/m014.
_TIMESTAMP_TYPE_BY_DIALECT = {"postgresql": "TIMESTAMP WITH TIME ZONE", "default": "TIMESTAMP"}

_COLUNA_EXPECTED_RESPONSAVEL_ID = "expected_responsavel_id"
_COLUNA_ATTEMPT_COUNT = "attempt_count"
_COLUNA_NEXT_ATTEMPT_AT = "next_attempt_at"
_COLUNA_RECONCILED_AT = "reconciled_at"
_COLUNA_ERROR_CLASS = "error_class"

_COLUNAS_OBRIGATORIAS = (
    _COLUNA_EXPECTED_RESPONSAVEL_ID,
    _COLUNA_ATTEMPT_COUNT,
    _COLUNA_NEXT_ATTEMPT_AT,
    _COLUNA_RECONCILED_AT,
    _COLUNA_ERROR_CLASS,
)

# Indice parcial compartilhado por R15/R16 — ver docstring do modulo.
_INDICE_PENDENTES = "ix_handoff_operations_pendentes"
_INDICE_PENDENTES_COLUNA = _COLUNA_NEXT_ATTEMPT_AT
_INDICE_PENDENTES_WHERE = "status IN ('REQUESTED','FAILED')"


def _ddl_colunas(dialect):
    """
    Texto DDL de cada coluna. So `attempt_count` leva DEFAULT — as outras 4 sao
    nullable puras, sem default nenhum (NULL ja e o valor correto — ver
    docstring do modulo).
    """
    timestamp_type = _TIMESTAMP_TYPE_BY_DIALECT.get(
        dialect, _TIMESTAMP_TYPE_BY_DIALECT["default"]
    )
    return (
        (_COLUNA_EXPECTED_RESPONSAVEL_ID, "INTEGER"),
        (_COLUNA_ATTEMPT_COUNT, "INTEGER NOT NULL DEFAULT 0"),
        (_COLUNA_NEXT_ATTEMPT_AT, timestamp_type),
        (_COLUNA_RECONCILED_AT, timestamp_type),
        (_COLUNA_ERROR_CLASS, "VARCHAR(32)"),
    )


def _normalizar_default(bruto):
    """
    Reduz o texto de default do catalogo a um token comparavel entre dialetos.

    SQLite devolve o texto do DDL verbatim (`0`); PostgreSQL devolve a
    expressao ja resolvida, que pode vir com cast (`0::integer`). Cortar cast,
    aspas e caixa deixa os dois comparaveis SEM afirmar equivalencia de TIPO —
    mesma funcao de m014, copiada aqui porque cada migration e independente
    por convencao (migrations/CLAUDE.md).
    """
    if bruto is None:
        return None
    texto = str(bruto).strip()
    if "::" in texto:
        texto = texto.split("::", 1)[0].strip()
    texto = texto.strip("'\"").strip()
    return texto.lower()


def run(engine=None, actions=None):
    """
    Devolve a lista de acoes aplicadas. Levanta RuntimeError se o mundo nao
    ficou como deveria depois do DDL — nunca engole nada, nunca imprime OK
    sobre um estado que nao verificou.

    `actions` e recebida de fora de proposito: se isto levantar no meio, o
    chamador ainda precisa saber QUAIS objetos ja foram aplicados.
    """
    if engine is None:
        url = os.getenv("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError(
                "DATABASE_URL nao definida — este script nao adivinha alvo. "
                "Passe um engine ou exporte a variavel."
            )
        engine = create_engine(url)

    actions = [] if actions is None else actions
    insp = inspect(engine)

    if _TABELA not in insp.get_table_names():
        # Banco NOVO nasce completo pelo create_all() e nao precisa deste
        # script; banco EXISTENTE sem `handoff_operations` quer dizer ALVO
        # ERRADO ou fora de ordem (falta rodar a m016 primeiro).
        raise WrongTargetError(
            "[m019] RECUSADO — o alvo nao tem a tabela `handoff_operations`.\n"
            "[m019] Banco NOVO nasce completo pelo create_all() do Conversas e nao\n"
            "[m019] precisa deste script. Banco EXISTENTE sem essa tabela: confira se\n"
            "[m019] falta rodar a m016 antes (ela cria `handoff_operations`), ou se\n"
            "[m019] DATABASE_URL aponta para o banco errado. Aqui nao existe 'NO-OP'\n"
            "[m019] honesto."
        )

    dialect = engine.dialect.name
    existentes = {c["name"] for c in insp.get_columns(_TABELA)}

    with engine.begin() as conn:
        for nome, ddl_tipo in _ddl_colunas(dialect):
            if nome in existentes:
                actions.append(f"{nome}:already-present")
                continue
            conn.execute(text(f"ALTER TABLE {_TABELA} ADD COLUMN {nome} {ddl_tipo}"))
            actions.append(f"{nome}:added ({ddl_tipo})")

        # Sintaxe identica em SQLite e PostgreSQL — ver docstring do modulo.
        conn.execute(text(
            f"CREATE INDEX IF NOT EXISTS {_INDICE_PENDENTES} "
            f"ON {_TABELA} ({_INDICE_PENDENTES_COLUNA}) WHERE {_INDICE_PENDENTES_WHERE}"
        ))
        actions.append(f"{_INDICE_PENDENTES}:ensured")

    return _verificar_pos_ddl(engine, actions)


def _verificar_pos_ddl(engine, actions):
    """
    Prova cada garantia que o docstring do modulo promete, por REINSPECAO —
    nunca lendo de volta o que o proprio DDL declarou.

    Checa: as 5 colunas existem pelo NOME; `attempt_count` e NOT NULL de
    verdade no catalogo E tem DEFAULT numerico (nao string); o indice parcial
    existe, cobre a coluna certa e preserva a clausula WHERE do dialeto atual
    (dialect_options — prova que continua PARCIAL, nao virou indice cheio).

    NUNCA tenta provar equivalencia de TIPO entre SQLite e PostgreSQL, nem
    equivalencia textual da EXPRESSAO do WHERE entre dialetos — exigiria
    logica fragil por dialeto e esta fora do escopo desta funcao, mesma
    fronteira que m014/m016 ja declaram.

    Extraida de `run()` para poder ser exercida sozinha contra um SQLite
    descartavel, sem nunca chamar `run()`/`main()`.
    """
    insp_pos = inspect(engine)
    if _TABELA not in insp_pos.get_table_names():
        raise RuntimeError(f"{_TABELA} AUSENTE depois do DDL — migration abortada.")

    colunas_pos = {c["name"]: c for c in insp_pos.get_columns(_TABELA)}
    faltando = [c for c in _COLUNAS_OBRIGATORIAS if c not in colunas_pos]
    if faltando:
        raise RuntimeError(
            f"colunas ausentes em {_TABELA} depois do DDL: {faltando} — tabela "
            "pre-existente/parcial (ALTER foi pulado por 'already-present' contra "
            "um schema incompleto). Migration abortada."
        )
    actions.append("colunas:verificadas")

    # attempt_count — unica coluna com NOT NULL + DEFAULT de verdade.
    attempt_count_info = colunas_pos[_COLUNA_ATTEMPT_COUNT]
    if attempt_count_info.get("nullable", True):
        raise RuntimeError(
            f"{_COLUNA_ATTEMPT_COUNT} esta NULLABLE no catalogo depois do DDL — "
            "esperado NOT NULL. Um INSERT sem o campo passaria a inserir NULL "
            "numa coluna que R16 trata como contador nunca-ausente. Migration "
            "abortada."
        )
    actions.append(f"{_COLUNA_ATTEMPT_COUNT}:not-null-verificado")

    bruto_default = attempt_count_info.get("default")
    if bruto_default is None:
        raise RuntimeError(
            f"{_COLUNA_ATTEMPT_COUNT} sem DEFAULT no catalogo depois do DDL — um "
            "INSERT vindo de fora da ORM (psql, COPY, restore) que omitisse o "
            "campo violaria o NOT NULL. Migration abortada."
        )
    if "'" in str(bruto_default) or '"' in str(bruto_default):
        raise RuntimeError(
            f"{_COLUNA_ATTEMPT_COUNT} tem DEFAULT {bruto_default!r} — literal "
            "string onde a coluna e INTEGER. O DDL precisa emitir `DEFAULT 0`, "
            "sem aspas (no model, `server_default=text(\"0\")`, nunca a string "
            "\"0\"). Migration abortada."
        )
    if _normalizar_default(bruto_default) != "0":
        raise RuntimeError(
            f"{_COLUNA_ATTEMPT_COUNT} tem DEFAULT {bruto_default!r} no catalogo, "
            "esperado equivalente a 0. Migration abortada."
        )
    actions.append(f"default-{_COLUNA_ATTEMPT_COUNT}:numerico")

    # Indice parcial — existencia, coluna certa, e a clausula WHERE do
    # dialeto atual presente em dialect_options (prova que segue PARCIAL).
    indices_pos = insp_pos.get_indexes(_TABELA)
    indice = next((ix for ix in indices_pos if ix.get("name") == _INDICE_PENDENTES), None)
    if indice is None:
        raise RuntimeError(
            f"{_INDICE_PENDENTES} AUSENTE depois do DDL — R15/R16 perderiam o "
            "indice compartilhado de retomada (secao 4.2 do desenho). Migration "
            "abortada."
        )
    if indice.get("column_names") != [_INDICE_PENDENTES_COLUNA]:
        raise RuntimeError(
            f"{_INDICE_PENDENTES} existe mas cobre {indice.get('column_names')!r}, "
            f"esperado [{_INDICE_PENDENTES_COLUNA!r}]. Migration abortada."
        )
    chave_where = f"{engine.dialect.name}_where"
    opts = indice.get("dialect_options") or {}
    if chave_where not in opts:
        raise RuntimeError(
            f"{_INDICE_PENDENTES} existe mas nao tem clausula WHERE "
            f"({chave_where!r} ausente de dialect_options={opts!r}) — deixaria de "
            "ser parcial, cobrindo TODAS as linhas em vez de so REQUESTED/FAILED. "
            "Migration abortada."
        )
    actions.append(f"{_INDICE_PENDENTES}:verificado")

    return actions


def _resolve_target(argv):
    """
    DATABASE_URL vem do AMBIENTE, nunca de app.config: os defaults de config
    apontam para arquivos SQLite locais e transformariam um deploy esquecido em
    "sucesso" contra um banco descartavel.
    """
    allow_sqlite = "--allow-sqlite" in argv
    url = os.getenv("DATABASE_URL", "").strip()

    if not url and allow_sqlite:
        # Modo dev explicito: cai no DATABASE_URL do proprio Conversas.
        from app.config import DATABASE_URL as CONVERSAS_URL

        return CONVERSAS_URL, allow_sqlite, None
    if not url:
        return None, allow_sqlite, (
            "[m019] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[m019] Exporte-a explicitamente; este script nao adivinha alvo.\n"
            "[m019]   DATABASE_URL=postgresql://... python migrations/m019_handoff_operations_reconciliacao.py"
        )
    if url.startswith("sqlite") and not allow_sqlite:
        return None, allow_sqlite, (
            f"[m019] RECUSADO — DATABASE_URL aponta para SQLite ({url}).\n"
            "[m019] Producao e PostgreSQL. Para rodar em dev/teste de proposito,\n"
            "[m019] passe --allow-sqlite."
        )
    return url, allow_sqlite, None


def main(argv):
    logging.basicConfig(level=logging.INFO)
    url, _allow_sqlite, refusal = _resolve_target(argv)
    if refusal:
        print(refusal)
        return 1

    safe = url.split("@")[-1] if "@" in url else url
    print(f"[m019] alvo (conversas): {safe}")

    actions = []

    def report():
        for action in actions:
            print(f"[m019]   {action}")

    try:
        run(create_engine(url), actions)
    except WrongTargetError as exc:
        report()
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada e falha RUIDOSA
        report()
        logger.exception("[m019] falha inesperada")
        print(f"[m019] FALHOU: {type(exc).__name__}: {exc}")
        return 1

    report()
    if any(":added" in a for a in actions):
        print("[m019] OK — colunas e indice aplicados (idempotente)")
    else:
        print("[m019] OK — NO-OP (ja estavam aplicados)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
