"""
BIA-V2 Fase 4 (CONVERSAS) - acrescenta a `conversations` as tres colunas da
maquina de estados V2: `state`, `state_version` e `closed_at`.

    conversations.state          VARCHAR(32)   NULL  DEFAULT 'LEGACY'
    conversations.state_version  INTEGER       NULL  DEFAULT 0
    conversations.closed_at      TIMESTAMP(TZ) NULL  (sem default)

Especificacao normativa: decisao 11 do subgate da Fase 4, em
`docs/superpowers/plans/2026-08-29-bia-v2.md`. Nada aqui e escolha deste
arquivo; tudo vem de la.

POR QUE O `DEFAULT` VAI NO PROPRIO `ADD COLUMN`
A m011 resolve o mesmo problema com `ALTER COLUMN ... SET DEFAULT` aplicado
SO no PostgreSQL, porque SQLite nao tem `ALTER COLUMN`. Copiar aquele desenho
aqui deixaria o SQLite migrado SEM default no catalogo — e a garantia de que
toda linha da V1 nasce `state='LEGACY'` vale nos dois bancos, nao so em
producao. `'LEGACY'` e `0` sao constantes literais, aceitas pelos dois
dialetos no `ADD COLUMN`. Ressalva de versao: em PostgreSQL anterior a 11 o
`ADD COLUMN ... DEFAULT <constante>` reescreve a tabela sob `ACCESS
EXCLUSIVE`; da 11 em diante o fast default (`pg_attribute.attmissingval`)
evita a reescrita. A correcao nao muda, so o tempo de lock — e producao roda
PostgreSQL 16 com ~81 linhas nesta tabela.

POR QUE O BACKFILL EXISTE MESMO ASSIM
`ADD COLUMN ... DEFAULT <constante>` ja preenche as linhas existentes nos dois
bancos, entao no caminho feliz o `UPDATE` e NO-OP. Ele fica porque e defensivo
e idempotente: repara execucao PARCIAL anterior (coluna criada, processo
morto antes do resto) e da um `rowcount` auditavel em `actions`. Nunca
presumir que o caminho feliz foi o unico que aconteceu.

`closed_at` NAO recebe backfill: `NULL` ja e o valor correto de "nunca fechada
pela V2". O tipo varia por dialeto pelo mesmo mapa que a m012 aplica ao irmao
`primeira_resposta_humana_at` na mesma tabela — sem isso, `TIMESTAMP` no ramo
PostgreSQL criaria `TIMESTAMP WITHOUT TIME ZONE`, diferente do `TIMESTAMPTZ`
que o `create_all()` emite a partir do model, e o drift entre os dois donos de
schema e problema conhecido deste repo.

COMPATIBILIDADE COM A V1
Total. As colunas sao nullable/com default, e a V1 nunca as le nem escreve —
a garantia e independencia semantica, provada por `tests/test_v2_v1_intacta.py`
(Familia B), nao invisibilidade. Rollback: deixar sem uso. NUNCA `DROP COLUMN`
(padrao do projeto, ver `migrations/README.md`).

Roda em processo proprio do Conversas:

    DATABASE_URL=postgresql://... python migrations/m014_conversas_state_columns.py
    python migrations/m014_conversas_state_columns.py --allow-sqlite

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
    """O alvo nao tem a tabela `conversations`: banco errado, nao 'NO-OP'."""


_TABELA = "conversations"

# So o TIPO muda por dialeto, nunca a FORMA do DDL — mesmo padrao de m003/m012.
_TIMESTAMP_TYPE_BY_DIALECT = {"postgresql": "TIMESTAMP WITH TIME ZONE", "default": "TIMESTAMP"}

# (nome, tipo_fixo_ou_None, default_literal_ou_None). `None` em tipo significa
# "resolve por dialeto"; `None` em default significa "sem DEFAULT no DDL".
_COLUNA_STATE = "state"
_COLUNA_STATE_VERSION = "state_version"
_COLUNA_CLOSED_AT = "closed_at"

_COLUNAS_OBRIGATORIAS = (_COLUNA_STATE, _COLUNA_STATE_VERSION, _COLUNA_CLOSED_AT)

# Colunas que precisam de DEFAULT no catalogo depois do DDL, com o literal
# esperado ja normalizado (minusculas, sem aspas simples, sem cast do PG).
_DEFAULTS_ESPERADOS = {
    _COLUNA_STATE: "legacy",
    _COLUNA_STATE_VERSION: "0",
}


def _ddl_colunas(dialect):
    """Texto DDL de cada coluna, com o `DEFAULT` embutido onde a decisao 11 pede."""
    timestamp_type = _TIMESTAMP_TYPE_BY_DIALECT.get(
        dialect, _TIMESTAMP_TYPE_BY_DIALECT["default"]
    )
    return (
        (_COLUNA_STATE, "VARCHAR(32) DEFAULT 'LEGACY'"),
        (_COLUNA_STATE_VERSION, "INTEGER DEFAULT 0"),
        (_COLUNA_CLOSED_AT, timestamp_type),
    )


def _normalizar_default(bruto):
    """
    Reduz o texto de default do catalogo a um token comparavel entre dialetos.

    SQLite devolve o texto do DDL verbatim (`'LEGACY'`, `0`); PostgreSQL
    devolve a expressao ja resolvida, que pode vir com cast
    (`'LEGACY'::character varying`). Cortar cast, aspas e caixa deixa os dois
    comparaveis SEM afirmar equivalencia de TIPO — que a decisao 11 declara
    fora do escopo deste gate.
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
        # script; banco EXISTENTE sem `conversations` quer dizer ALVO ERRADO.
        raise WrongTargetError(
            "[m014] RECUSADO — o alvo nao tem a tabela `conversations`.\n"
            "[m014] Banco NOVO nasce completo pelo create_all() do Conversas e nao\n"
            "[m014] precisa deste script. Banco EXISTENTE sem essa tabela quer dizer\n"
            "[m014] ALVO ERRADO (DATABASE_URL para outra base, nome trocado, replica\n"
            "[m014] vazia). Confira DATABASE_URL: aqui nao existe 'NO-OP' honesto."
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

        # Backfill defensivo: NO-OP no caminho feliz, reparo em execucao parcial.
        for nome, valor_sql in ((_COLUNA_STATE, "'LEGACY'"), (_COLUNA_STATE_VERSION, "0")):
            resultado = conn.execute(
                text(f"UPDATE {_TABELA} SET {nome} = {valor_sql} WHERE {nome} IS NULL")
            )
            actions.append(f"backfill-{nome}:{resultado.rowcount}")

    return _verificar_pos_ddl(engine, actions)


def _verificar_pos_ddl(engine, actions):
    """
    Prova cada garantia que o docstring de `run()` promete, por REINSPECAO —
    nunca lendo de volta o que o proprio DDL declarou.

    Checa: as tres colunas existem pelo NOME; `state` e `state_version` tem
    DEFAULT no catalogo, e o de `state_version` e `0` NUMERICO e nao `'0'`
    string (a distincao que `server_default=text("0")` protege no lado do ORM
    — aqui e reforco, nao o mecanismo principal, porque o PostgreSQL coage o
    literal no parse e pode normalizar os dois para o mesmo valor); e nenhuma
    linha ficou com `state` ou `state_version` NULL.

    NUNCA tenta provar equivalencia de TIPO entre SQLite e PostgreSQL — exige
    logica fragil por dialeto e esta fora do escopo desta funcao, mesma
    fronteira que o gate da m013 ja declara. O que protege o tipo de
    `closed_at` e o `_TIMESTAMP_TYPE_BY_DIALECT` na EMISSAO, nao uma
    comparacao de strings aqui.

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

    for nome, esperado in _DEFAULTS_ESPERADOS.items():
        bruto = colunas_pos[nome].get("default")
        if bruto is None:
            raise RuntimeError(
                f"{nome} sem DEFAULT no catalogo depois do DDL — um INSERT vindo de "
                "fora da ORM (psql, COPY, restore) gravaria NULL, e a garantia de que "
                "toda linha da V1 nasce 'LEGACY' cai. Migration abortada."
            )
        obtido = _normalizar_default(bruto)
        if obtido != esperado:
            raise RuntimeError(
                f"{nome} tem DEFAULT {bruto!r} no catalogo, esperado equivalente a "
                f"{esperado!r}. Migration abortada."
            )
        actions.append(f"default-{nome}:verificado")

    # `state_version` e coluna numerica: o default nao pode estar entre aspas.
    bruto_version = str(colunas_pos[_COLUNA_STATE_VERSION].get("default"))
    if "'" in bruto_version or '"' in bruto_version:
        raise RuntimeError(
            f"{_COLUNA_STATE_VERSION} tem DEFAULT {bruto_version!r} — literal string "
            "onde a coluna e INTEGER. O DDL precisa emitir `DEFAULT 0`, sem aspas "
            "(no model, `server_default=text(\"0\")`, nunca a string \"0\"). "
            "Migration abortada."
        )
    actions.append(f"default-{_COLUNA_STATE_VERSION}:numerico")

    with engine.connect() as conn:
        for nome in (_COLUNA_STATE, _COLUNA_STATE_VERSION):
            restantes = conn.execute(
                text(f"SELECT COUNT(*) FROM {_TABELA} WHERE {nome} IS NULL")
            ).scalar()
            if restantes:
                raise RuntimeError(
                    f"{restantes} linha(s) com {nome} NULL depois do backfill — "
                    "migration abortada."
                )
            actions.append(f"sem-nulos-{nome}:verificado")

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
            "[m014] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[m014] Exporte-a explicitamente; este script nao adivinha alvo.\n"
            "[m014]   DATABASE_URL=postgresql://... python migrations/m014_conversas_state_columns.py"
        )
    if url.startswith("sqlite") and not allow_sqlite:
        return None, allow_sqlite, (
            f"[m014] RECUSADO — DATABASE_URL aponta para SQLite ({url}).\n"
            "[m014] Producao e PostgreSQL. Para rodar em dev/teste de proposito,\n"
            "[m014] passe --allow-sqlite."
        )
    return url, allow_sqlite, None


def main(argv):
    logging.basicConfig(level=logging.INFO)
    url, _allow_sqlite, refusal = _resolve_target(argv)
    if refusal:
        print(refusal)
        return 1

    safe = url.split("@")[-1] if "@" in url else url
    print(f"[m014] alvo (conversas): {safe}")

    actions = []

    def report():
        for action in actions:
            print(f"[m014]   {action}")

    try:
        run(create_engine(url), actions)
    except WrongTargetError as exc:
        report()
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada e falha RUIDOSA
        report()
        logger.exception("[m014] falha inesperada")
        print(f"[m014] FALHOU: {type(exc).__name__}: {exc}")
        return 1

    report()
    if any(":added" in a for a in actions):
        print("[m014] OK — colunas aplicadas (idempotente)")
    else:
        print("[m014] OK — NO-OP (ja estavam aplicadas)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
