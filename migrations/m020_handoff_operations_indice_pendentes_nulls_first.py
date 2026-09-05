"""
BIA-V2 R15/R16 (fechamento de achado de concorrencia pos-R-B) — corrige a
ORDEM do indice parcial `ix_handoff_operations_pendentes` (criado pela m019),
que uma revisao de concorrencia PROVOU nunca ser usado pela consulta real de
reclamacao dos workers (EXPLAIN ANALYZE, PostgreSQL 16 real, 30k linhas).

O DEFEITO, PRECISAMENTE
-----------------------
`conversas/app/v2/worker_fila.py:consulta_de_reclamacao` ordena por:

    ORDER BY next_attempt_at ASC NULLS FIRST, id ASC

A m019 criou o indice como:

    CREATE INDEX ix_handoff_operations_pendentes
    ON handoff_operations (next_attempt_at)
    WHERE status IN ('REQUESTED','FAILED')

— um btree comum, que no PostgreSQL e `ASC NULLS LAST` por padrao (o SQLite
de dev/CI e o OPOSTO: ASC ja poe NULL primeiro por padrao ali, entao o
mesmo DDL "funciona por acaso" no dialeto de dev/CI e so falha, em silencio,
no dialeto de producao). `NULLS FIRST` pedido pela consulta x `NULLS LAST`
do indice sao direcoes de varredura INCOMPATIVEIS: o planner do PostgreSQL
descarta o indice inteiro e faz Seq Scan + Sort completo da tabela a CADA
CICLO de CADA um dos dois workers (30s, secao 5 do desenho). Prova empirica
que motivou esta migration: 14.796ms/587 buffers (sem indice efetivo) contra
0.098ms/24 buffers (com a ordem corrigida) — 150x mais lento, e o custo
cresce linearmente com o tamanho da tabela, que por desenho NUNCA e podada
(ver "CRESCIMENTO ILIMITADO POR DESENHO" no docstring de
`conversas/app/models/handoff_op.py`).

A CORRECAO
----------
Substitui o indice por um logicamente equivalente, na ordem certa:

    DROP INDEX IF EXISTS ix_handoff_operations_pendentes;
    -- PostgreSQL:
    CREATE INDEX IF NOT EXISTS ix_handoff_operations_pendentes
        ON handoff_operations (next_attempt_at ASC NULLS FIRST, id ASC)
        WHERE status IN ('REQUESTED','FAILED');
    -- SQLite:
    CREATE INDEX IF NOT EXISTS ix_handoff_operations_pendentes
        ON handoff_operations (next_attempt_at, id)
        WHERE status IN ('REQUESTED','FAILED');

MESMO NOME, MESMA TABELA, MESMO PREDICADO — so a ordem/anotacao das colunas
muda, e so entre dialetos (nunca entre model e migration: os dois emitem o
mesmo DDL efetivo, replicado em `HandoffOperation.__table_args__` como
sempre). `id` entra como SEGUNDA coluna nos dois dialetos (novo em relacao a
m019): a consulta desempata por `id.asc()` depois de `next_attempt_at`, e
sem a segunda coluna no indice o PostgreSQL ainda precisaria de um
"Incremental Sort" residual para as linhas empatadas (o caso mais comum e
justamente `NULL` — a linha nunca agendada, a mais urgente segundo a
"Legenda A" da secao 6.1 do desenho).

`NULLS FIRST` SO NO RAMO POSTGRESQL — NAO E OMISSAO
----------------------------------------------------
O SQLite deste projeto (>= 3.30) aceita `NULLS FIRST`/`NULLS LAST` dentro de
`ORDER BY`, mas NAO dentro de `CREATE INDEX` — a tentativa levanta
`OperationalError: unsupported use of NULLS FIRST` (provado contra o driver
`sqlite3` real desta maquina antes de escrever este script, e de novo contra
o SQLAlchemy: o compilador ate GERA o DDL com `NULLS FIRST` para o dialeto
sqlite sem reclamar, e so o driver real recusa a instrucao na hora de
executar — por isso a prova tem de ser EXECUTANDO contra SQLite de verdade,
nao so compilando a string). E, ao contrario do que se poderia temer, isso
NAO deixa o SQLite pior: o ASC padrao do SQLite ja ordena NULL PRIMEIRO (o
inverso do padrao do PostgreSQL — mesmo fato que
`conversas/app/v2/worker_fila.py` ja documenta na funcao `_janela_chegou`),
entao um indice ascendente comum `(next_attempt_at, id)` ja bate EXATAMENTE
com `next_attempt_at.asc().nullsfirst(), id.asc()` da consulta, sem anotacao
nenhuma — confirmado com `EXPLAIN QUERY PLAN` (`SCAN ... USING INDEX`, sem
passo de SORT separado). Por isso esta migration ramifica por
`engine.dialect.name`, no MESMO padrao ja usado por `_TIMESTAMP_TYPE_BY_
DIALECT` nas m003/m012/m014/m019 — so que aqui a variacao e na SINTAXE do
indice (a ordem/anotacao das colunas), nao no TIPO de uma coluna.

POR QUE DROP + CREATE, E NAO SO "ADD" — excecao LITERAL, nao reinterpretacao,
da regra de `migrations/README.md` de nunca fazer `DROP INDEX`
------------------------------------------------------------------------------
`migrations/README.md` documenta "nunca DROP INDEX" para indices de
PRODUCAO ja em uso por dados reais, e esse principio continua valendo aqui:
a excecao e que o indice `ix_handoff_operations_pendentes` esta sendo
SUBSTITUIDO por outro logicamente equivalente e estritamente mais correto
(mesmo nome, mesma tabela, mesmo predicado, mesmo CONJUNTO de colunas
cobertas — so ordem/anotacao muda), DENTRO DA MESMA migration que o corrige
— nunca removido sem substituto, e nunca deixando a tabela sem indice em
nenhum instante observavel por outra sessao: o `DROP INDEX` e o `CREATE
INDEX` seguinte rodam na MESMA transacao (`with engine.begin() as conn:`),
entao outra sessao ve o estado ANTES (indice antigo, indices por nome unico
nao permitem dois com o mesmo nome simultaneamente de qualquer forma) ou
DEPOIS (indice novo) do commit — nunca um meio-termo sem indice nenhum.

CUSTO EM PRODUCAO — SEM `CONCURRENTLY`, DE PROPOSITO
-------------------------------------------------------
`CREATE INDEX` (sem `CONCURRENTLY`) bloqueia ESCRITAS na tabela pela duracao
da construcao do indice. Para `handoff_operations` — tabela append-only, de
crescimento ilimitado por desenho — isso seria preocupante se o indice
cobrisse a tabela INTEIRA; mas e um indice PARCIAL (`WHERE status IN
('REQUESTED','FAILED')`), entao o custo e proporcional as operacoes ainda
PENDENTES, nao ao historico completo — tipicamente pequeno, ja que uma
operacao sai dessa fatia assim que completa ou atinge o teto de tentativas
(dead-letter, `TETO_TENTATIVAS` em `worker_fila.py`). `CREATE INDEX
CONCURRENTLY` (a alternativa sem lock de escrita) NAO pode rodar dentro de
uma transacao — exigiria abandonar o padrao `with engine.begin() as conn:`
que toda esta familia de scripts compartilha, e carrega o proprio risco
(indice INVALIDO se a sessao morrer no meio, exigindo limpeza manual
`DROP INDEX`/recriar). Dado o tamanho esperado, o trade-off nao se paga
aqui. Quem for aplicar em producao deve confirmar o volume de linhas
PENDENTES antes (`SELECT count(*) FROM handoff_operations WHERE status IN
('REQUESTED','FAILED')`) como parte do gate humano de `migrations/
README.md`; se esse numero for inesperadamente grande, reconsiderar
`CONCURRENTLY` como script separado, fora do escopo desta correcao.

DEPENDE DA m019 (nao so da m016)
---------------------------------
Ao contrario da m019 (que so exige a TABELA `handoff_operations`, criada
pela m016), esta migration exige que a COLUNA `next_attempt_at` ja exista —
ou seja, que a m019 ja tenha rodado. Alvo com `handoff_operations` mas sem
`next_attempt_at`: RECUSA (`WrongTargetError`), nao "NO-OP" — mesmo
principio que a m019 ja aplica em relacao a m016.

COMPATIBILIDADE E REVERSAO
----------------------------
Total, para todo leitor/escritor: nenhuma linha muda de valor, nenhuma
coluna nova, nenhuma mudanca de tipo — SO a definicao do indice muda.
Nenhum resultado de `tests/test_v2_workers.py` deveria mudar (eles testam o
COMPORTAMENTO da reclamacao — particao, janela, teto, exclusao entre
instancias — nunca a FORMA do indice); so o PLANO de execucao no PostgreSQL
muda. Reversao: recriar o indice no formato da m019 (`DROP INDEX IF EXISTS
...; CREATE INDEX ... (next_attempt_at) WHERE ...`) — nunca ha perda de
dado, so uma regressao de performance ja conhecida e documentada.

IDEMPOTENCIA DESTE SCRIPT, EM PALAVRAS EXATAS
------------------------------------------------
Ao contrario da maioria dos scripts desta familia (que so fazem `ADD COLUMN`
uma vez e reconhecem "already-present" depois), este SEMPRE substitui o
indice (`DROP IF EXISTS` + `CREATE`) a cada execucao — nao ha estado
intermediario "correto pela metade" para detectar, e o `DROP IF EXISTS` e
inofensivo tanto contra o indice ANTIGO (m019) quanto contra o indice NOVO
(ja corrigido por uma rodada anterior deste script). "Idempotente" aqui
significa o que `migrations/CLAUDE.md` pede — "pode rodar varias vezes sem
efeito colateral" —, nao "a segunda rodada nao faz nada": toda rodada
recria o MESMO indice, verificado pela MESMA `_verificar_pos_ddl`, e o
resultado observavel de rodar uma vez ou dez e identico.

Roda em processo proprio do Conversas:

    DATABASE_URL=postgresql://... python migrations/m020_handoff_operations_indice_pendentes_nulls_first.py
    python migrations/m020_handoff_operations_indice_pendentes_nulls_first.py --allow-sqlite

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
    """Alvo sem `handoff_operations`/`next_attempt_at`: banco errado ou fora de ordem, nao 'NO-OP'."""


_TABELA = "handoff_operations"
_INDICE_PENDENTES = "ix_handoff_operations_pendentes"
_INDICE_PENDENTES_WHERE = "status IN ('REQUESTED','FAILED')"
# Prova de que a m019 ja rodou (ela e quem cria esta coluna) — ver "DEPENDE
# DA m019" no docstring do modulo.
_COLUNA_GATE = "next_attempt_at"

# So a SINTAXE do indice muda por dialeto aqui (nao um TIPO de coluna, como
# nas outras migrations desta familia) — ver "NULLS FIRST SO NO RAMO
# POSTGRESQL" no docstring do modulo.
_INDICE_PENDENTES_COLS_BY_DIALECT = {
    "postgresql": "next_attempt_at ASC NULLS FIRST, id ASC",
    "default": "next_attempt_at, id",
}


def _ddl_indice(dialect):
    """Texto DDL do `CREATE INDEX`, na sintaxe do dialeto atual."""
    colunas = _INDICE_PENDENTES_COLS_BY_DIALECT.get(
        dialect, _INDICE_PENDENTES_COLS_BY_DIALECT["default"]
    )
    return (
        f"CREATE INDEX IF NOT EXISTS {_INDICE_PENDENTES} "
        f"ON {_TABELA} ({colunas}) WHERE {_INDICE_PENDENTES_WHERE}"
    )


def run(engine=None, actions=None):
    """
    Devolve a lista de acoes aplicadas. Levanta `WrongTargetError` (alvo
    errado ou fora de ordem) ou `RuntimeError` (o mundo nao ficou como
    deveria depois do DDL) — nunca engole nada, nunca imprime OK sobre um
    estado que nao verificou.

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
        # Mesmo gate da m019: banco NOVO nasce completo pelo create_all() e
        # nao precisa deste script; banco EXISTENTE sem a tabela quer dizer
        # ALVO ERRADO ou fora de ordem (falta a m016).
        raise WrongTargetError(
            "[m020] RECUSADO — o alvo nao tem a tabela `handoff_operations`.\n"
            "[m020] Banco NOVO nasce completo pelo create_all() do Conversas e nao\n"
            "[m020] precisa deste script. Banco EXISTENTE sem essa tabela: confira se\n"
            "[m020] falta rodar a m016 antes (ela cria `handoff_operations`), ou se\n"
            "[m020] DATABASE_URL aponta para o banco errado. Aqui nao existe 'NO-OP'\n"
            "[m020] honesto."
        )

    colunas_existentes = {c["name"] for c in insp.get_columns(_TABELA)}
    if _COLUNA_GATE not in colunas_existentes:
        # Gate ESPECIFICO desta migration (m019 nao precisava dele: ela e a
        # que CRIA a coluna). Ver "DEPENDE DA m019" no docstring do modulo.
        raise WrongTargetError(
            f"[m020] RECUSADO — `{_TABELA}` existe mas nao tem a coluna "
            f"`{_COLUNA_GATE}`.\n"
            "[m020] Falta rodar a m019 antes (ela acrescenta as 5 colunas de\n"
            "[m020] reconciliacao/retomada, incluindo esta). Aqui nao existe\n"
            "[m020] 'NO-OP' honesto."
        )

    dialect = engine.dialect.name

    with engine.begin() as conn:
        # DROP + CREATE, nunca so ADD — ver "POR QUE DROP + CREATE" no
        # docstring do modulo. As duas instrucoes na MESMA transacao: outra
        # sessao ve o indice ANTES ou DEPOIS do commit, nunca um meio-termo
        # sem indice nenhum.
        conn.execute(text(f"DROP INDEX IF EXISTS {_INDICE_PENDENTES}"))
        conn.execute(text(_ddl_indice(dialect)))
        actions.append(f"{_INDICE_PENDENTES}:recriado(dialect={dialect})")

    return _verificar_pos_ddl(engine, actions)


def _verificar_pos_ddl(engine, actions):
    """
    Prova cada garantia que o docstring do modulo promete, por REINSPECAO —
    nunca lendo de volta o que o proprio DDL declarou.

    Checa: o indice existe, cobre EXATAMENTE `[next_attempt_at, id]` (nesta
    ordem), e preserva a clausula WHERE do dialeto atual (dialect_options —
    prova que continua PARCIAL). No PostgreSQL, checa ADICIONALMENTE — via
    `pg_indexes`, reinspecionando o catalogo, nunca confiando no DDL que
    acabou de emitir — que a definicao reflete `NULLS FIRST` de verdade:
    refletir so `column_names` prova PRESENCA das colunas, nao a ORDEM de
    varredura, que e o ponto inteiro desta migration.

    Extraida de `run()` para poder ser exercida sozinha contra um SQLite
    descartavel, sem nunca chamar `run()`/`main()`.
    """
    insp_pos = inspect(engine)
    if _TABELA not in insp_pos.get_table_names():
        raise RuntimeError(f"{_TABELA} AUSENTE depois do DDL — migration abortada.")

    indices_pos = insp_pos.get_indexes(_TABELA)
    indice = next((ix for ix in indices_pos if ix.get("name") == _INDICE_PENDENTES), None)
    if indice is None:
        raise RuntimeError(
            f"{_INDICE_PENDENTES} AUSENTE depois do DDL — R15/R16 perderiam o "
            "indice compartilhado de retomada. Migration abortada."
        )

    colunas_indice = indice.get("column_names")
    if colunas_indice != ["next_attempt_at", "id"]:
        raise RuntimeError(
            f"{_INDICE_PENDENTES} existe mas cobre {colunas_indice!r}, esperado "
            "['next_attempt_at', 'id']. Migration abortada."
        )
    actions.append(f"{_INDICE_PENDENTES}:colunas-verificadas")

    dialect = engine.dialect.name
    chave_where = f"{dialect}_where"
    opts = indice.get("dialect_options") or {}
    if chave_where not in opts:
        raise RuntimeError(
            f"{_INDICE_PENDENTES} existe mas nao tem clausula WHERE "
            f"({chave_where!r} ausente de dialect_options={opts!r}) — deixaria de "
            "ser parcial, cobrindo TODAS as linhas em vez de so REQUESTED/FAILED. "
            "Migration abortada."
        )
    actions.append(f"{_INDICE_PENDENTES}:where-verificado")

    if dialect == "postgresql":
        with engine.connect() as conn:
            indexdef = conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :nome"),
                {"nome": _INDICE_PENDENTES},
            ).scalar_one_or_none()
        if indexdef is None or "NULLS FIRST" not in indexdef:
            raise RuntimeError(
                f"{_INDICE_PENDENTES} existe mas o catalogo do PostgreSQL nao mostra "
                f"NULLS FIRST na definicao (indexdef={indexdef!r}) — a correcao de "
                "ordem nao pegou de verdade. Migration abortada."
            )
        actions.append(f"{_INDICE_PENDENTES}:nulls-first-verificado(postgresql)")

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
            "[m020] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[m020] Exporte-a explicitamente; este script nao adivinha alvo.\n"
            "[m020]   DATABASE_URL=postgresql://... python migrations/m020_handoff_operations_indice_pendentes_nulls_first.py"
        )
    if url.startswith("sqlite") and not allow_sqlite:
        return None, allow_sqlite, (
            f"[m020] RECUSADO — DATABASE_URL aponta para SQLite ({url}).\n"
            "[m020] Producao e PostgreSQL. Para rodar em dev/teste de proposito,\n"
            "[m020] passe --allow-sqlite."
        )
    return url, allow_sqlite, None


def main(argv):
    logging.basicConfig(level=logging.INFO)
    url, _allow_sqlite, refusal = _resolve_target(argv)
    if refusal:
        print(refusal)
        return 1

    safe = url.split("@")[-1] if "@" in url else url
    print(f"[m020] alvo (conversas): {safe}")

    actions = []

    def report():
        for action in actions:
            print(f"[m020]   {action}")

    try:
        run(create_engine(url), actions)
    except WrongTargetError as exc:
        report()
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada e falha RUIDOSA
        report()
        logger.exception("[m020] falha inesperada")
        print(f"[m020] FALHOU: {type(exc).__name__}: {exc}")
        return 1

    report()
    # Sem branch "NO-OP" aqui de proposito — ver "IDEMPOTENCIA DESTE SCRIPT,
    # EM PALAVRAS EXATAS" no docstring: toda rodada recria o indice.
    print("[m020] OK — indice ix_handoff_operations_pendentes recriado e verificado (idempotente)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
