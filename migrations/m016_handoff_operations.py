"""
m016 — BIA-V2 Fase 6: tabela `handoff_operations` (CONVERSAS).

Cria (aditivo, sem destruir nada):
  - tabela `handoff_operations` — uma linha por OPERACAO de handoff BIA ->
    humano; e o log de INTENCAO que torna o handoff reconciliavel (ver o
    docstring de conversas/app/models/handoff_op.py).
      id                INTEGER/SERIAL PK
      event_id          VARCHAR(36)  NOT NULL, UNIQUE (indice de verdade)
      conversation_id   INTEGER      NOT NULL (SEM ForeignKey — ver abaixo)
      target_user_id    INTEGER      NOT NULL (users vive no schema do CRM)
      status            VARCHAR(16)  NOT NULL, CHECK do vocabulario fechado
      error_code        VARCHAR(64)
      created_at        TIMESTAMP(TZ) DEFAULT CURRENT_TIMESTAMP
      updated_at        TIMESTAMP(TZ) DEFAULT CURRENT_TIMESTAMP
  - uq_handoff_operations_event_id   UNIQUE (event_id)
  - ck_handoff_operations_status     CHECK (status IN ('REQUESTED','COMPLETED','FAILED'))

Os tres objetos usam os MESMOS nomes que `conversas/app/models/handoff_op.py`
declara em `__table_args__`, para que `create_all()` (bancos novos) e esta
migration (bancos existentes) emitam exatamente o mesmo schema — mesmo motivo
documentado na m013 e em `Conversation.whatsapp` (AUDIT-2026-08-W2E).

A expressao do CHECK e uma COPIA DELIBERADA da que o model monta a partir do
enum `StatusHandoff` — este script NAO importa o model para montar DDL (mesma
convencao da m013, que repete a lista de colunas como literal). A divergencia
entre as duas copias e travada por gate executavel em
`tests/test_v2_m016_gate.py`, no mesmo espirito do par m015/`conversation.py`
(F4 da revisao da Fase 6 corrigiu esta referencia — apontava para
tests/test_v2_handoff.py, que nao faz essa checagem).

SEM ForeignKey de proposito, nas duas colunas: `conversation_id` porque este
registro e auditoria/reconciliacao e precisa sobreviver a delecao da conversa
que descreve (mesma decisao de `conversation_events`); `target_user_id` porque
`users` e tabela do CRM, tratada aqui como inteiro sem FK — mesmo padrao ja
adotado por `Conversation.lead_id`.

NENHUM INDICE SECUNDARIO: `app/v2/handoff.py` consulta esta tabela SO por
`event_id`, ja coberto pelo UNIQUE.

DDL PORTAVEL, NAO POR DIALETO
------------------------------
`CREATE TABLE IF NOT EXISTS` e `CREATE UNIQUE INDEX IF NOT EXISTS` tem sintaxe
IDENTICA em SQLite e PostgreSQL (confirmado nesta base pela m013). O CHECK
NOMEADO vai DENTRO do `CREATE TABLE` — nao ha `ALTER TABLE ... ADD CONSTRAINT`
aqui, justamente o comando que a m009 precisou ramificar por dialeto. So o TIPO
de `id` e de `created_at`/`updated_at` muda por dialeto (padrao da m012/m013).

REVERSAO: remover a tabela `handoff_operations` — nenhum fluxo da V1 a
referencia (Familia A de tests/test_v2_v1_intacta.py continua intacta), entao
a reversao e total e sem efeito colateral.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja
nascem completos via `Base.metadata.create_all()` no lifespan do Conversas.

ATENCAO — este e o app CONVERSAS (nao o CRM): o script insere `conversas/` no
inicio do sys.path para que `app.*` resolva para conversas/app. Deve rodar em
PROCESSO PROPRIO, nunca importado junto das migrations do CRM.

Ordem em bancos antigos: m003 -> ... -> m013 -> m014 -> m015 -> m016 (nenhuma
dependencia real entre elas; a ordem so acompanha a numeracao).

Uso (LOCAL / STAGING):
    DATABASE_URL=postgresql://... python migrations/m016_handoff_operations.py
    python migrations/m016_handoff_operations.py --allow-sqlite

PRODUCAO: somente apos backup verificado + aprovacao humana (migrations/README.md).
"""
import logging
import os
import pathlib
import sys

# `app.*` deve resolver para conversas/app — ver docstring.
_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

logger = logging.getLogger("migrations.m016")


class WrongTargetError(RuntimeError):
    """O alvo nao tem a tabela `conversations`: banco errado, nao 'NO-OP'."""


_TABELA = "handoff_operations"
_UNIQUE_EVENT_ID = "uq_handoff_operations_event_id"
_CHECK_STATUS = "ck_handoff_operations_status"

# COPIA DELIBERADA da expressao que o model monta a partir de `StatusHandoff`
# (conversas/app/models/handoff_op.py) — ver docstring do modulo.
_CHECK_STATUS_SQL = "status IN ('REQUESTED','COMPLETED','FAILED')"

# Espelha as colunas de conversas/app/models/handoff_op.py:HandoffOperation —
# usado SO pelo gate pos-DDL para provar presenca (nao tipo: ver
# _verificar_pos_ddl).
_COLUNAS_OBRIGATORIAS = [
    "id", "event_id", "conversation_id", "target_user_id", "status",
    "error_code", "created_at", "updated_at",
]

# Mesmo padrao da m012/m013: so o TIPO muda por dialeto, nunca a FORMA do DDL.
_ID_TYPE_BY_DIALECT = {"postgresql": "SERIAL", "default": "INTEGER"}
_TIMESTAMP_TYPE_BY_DIALECT = {"postgresql": "TIMESTAMP WITH TIME ZONE", "default": "TIMESTAMP"}


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

    if "conversations" not in insp.get_table_names():
        # Mesmo gate da m012/m013: banco NOVO nasce completo pelo create_all()
        # e nao precisa deste script; banco EXISTENTE sem `conversations` quer
        # dizer ALVO ERRADO, nao "nada a fazer".
        raise WrongTargetError(
            "[m016] RECUSADO — o alvo nao tem a tabela `conversations`.\n"
            "[m016] Banco NOVO nasce completo pelo create_all() do Conversas e nao\n"
            "[m016] precisa deste script. Banco EXISTENTE sem essa tabela quer dizer\n"
            "[m016] ALVO ERRADO (DATABASE_URL para outra base, nome trocado, replica\n"
            "[m016] vazia). Confira DATABASE_URL: aqui nao existe 'NO-OP' honesto."
        )

    dialect = engine.dialect.name
    ja_existia = _TABELA in insp.get_table_names()
    id_type = _ID_TYPE_BY_DIALECT.get(dialect, _ID_TYPE_BY_DIALECT["default"])
    timestamp_type = _TIMESTAMP_TYPE_BY_DIALECT.get(dialect, _TIMESTAMP_TYPE_BY_DIALECT["default"])

    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {_TABELA} ("
            f"    id {id_type} PRIMARY KEY,"
            "    event_id VARCHAR(36) NOT NULL,"
            "    conversation_id INTEGER NOT NULL,"
            "    target_user_id INTEGER NOT NULL,"
            "    status VARCHAR(16) NOT NULL,"
            "    error_code VARCHAR(64),"
            f"    created_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP,"
            f"    updated_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP,"
            f"    CONSTRAINT {_CHECK_STATUS} CHECK ({_CHECK_STATUS_SQL})"
            ")"
        ))
        actions.append(f"{_TABELA}:{'already-present' if ja_existia else 'created'}")

        conn.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_UNIQUE_EVENT_ID} "
            f"ON {_TABELA} (event_id)"
        ))
        actions.append(f"{_UNIQUE_EVENT_ID}:ensured")

    return _verificar_pos_ddl(engine, actions)


def _verificar_pos_ddl(engine, actions):
    """
    Prova cada garantia que o docstring de `run()` promete — nunca engole nada,
    nunca deixa `run()` imprimir OK sobre estado que nao verificou.

    `CREATE TABLE IF NOT EXISTS` e `CREATE UNIQUE INDEX IF NOT EXISTS` sao
    NO-OP silencioso contra uma tabela/indice PRE-EXISTENTE ou PARCIAL — so a
    reinspecao pos-DDL (via `inspect()`, nunca lendo de volta o que o proprio
    DDL declarou) pega essa lacuna. E o unico caso que importa aqui: uma tabela
    antiga com esse nome, sem o CHECK, aceitaria `status` fora do vocabulario e
    a reconciliacao passaria a decidir sobre um valor que nao existe.

    Checa PRESENCA de colunas, da UNIQUE e do CHECK NOMEADO. Nunca tenta provar
    equivalencia de TIPO entre SQLite e PostgreSQL, nem equivalencia textual da
    EXPRESSAO do CHECK (cada dialeto normaliza a sua) — as duas exigiriam
    logica fragil por dialeto e estao fora do escopo desta funcao; a
    equivalencia model x migration e provada em tests/test_v2_m016_gate.py
    (F4 da revisao da Fase 6 corrigiu esta referencia — apontava para
    tests/test_v2_handoff.py, que nao faz essa checagem).

    Extraida de `run()` para poder ser exercida sozinha (ex.: contra um banco
    SQLite descartavel criado via `Base.metadata.create_all()`) sem nunca
    chamar `run()`/`main()`.
    """
    insp_pos = inspect(engine)
    if _TABELA not in insp_pos.get_table_names():
        raise RuntimeError(f"{_TABELA} AUSENTE depois do DDL — migration abortada.")

    colunas_pos = {c["name"] for c in insp_pos.get_columns(_TABELA)}
    faltando = [c for c in _COLUNAS_OBRIGATORIAS if c not in colunas_pos]
    if faltando:
        raise RuntimeError(
            f"colunas ausentes em {_TABELA} depois do DDL: {faltando} — tabela "
            "pre-existente/parcial (CREATE TABLE IF NOT EXISTS foi NO-OP contra "
            "ela). Migration abortada."
        )
    actions.append("colunas:verificadas")

    indices_pos = insp_pos.get_indexes(_TABELA)
    uniques_pos = insp_pos.get_unique_constraints(_TABELA)
    tem_unique = any(
        ix.get("unique") and ix.get("column_names") == ["event_id"] for ix in indices_pos
    ) or any(uc.get("column_names") == ["event_id"] for uc in uniques_pos)
    if not tem_unique:
        raise RuntimeError(
            "UNIQUE em event_id AUSENTE depois do DDL — duas operacoes de handoff "
            "poderiam compartilhar a mesma chave de idempotencia. Migration abortada."
        )
    actions.append(f"{_UNIQUE_EVENT_ID}:verificado")

    nomes_check = {c.get("name") for c in insp_pos.get_check_constraints(_TABELA)}
    if _CHECK_STATUS not in nomes_check:
        raise RuntimeError(
            f"{_CHECK_STATUS} AUSENTE depois do DDL (encontrados: {sorted(n for n in nomes_check if n)}) "
            "— `status` aceitaria valor fora do vocabulario fechado. Migration abortada."
        )
    actions.append(f"{_CHECK_STATUS}:verificado")

    return actions


def _resolve_target(argv):
    """
    DATABASE_URL vem do AMBIENTE, nunca de app.config: os defaults de config
    apontam para arquivos SQLite locais e transformariam um deploy esquecido em
    "sucesso" contra um banco descartavel (mesmo motivo da m012/m013).
    """
    allow_sqlite = "--allow-sqlite" in argv
    url = os.getenv("DATABASE_URL", "").strip()

    if not url and allow_sqlite:
        # Modo dev explicito: cai no DATABASE_URL do proprio Conversas.
        from app.config import DATABASE_URL as CONVERSAS_URL  # noqa: E402

        return CONVERSAS_URL, allow_sqlite, None
    if not url:
        return None, allow_sqlite, (
            "[m016] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[m016] Exporte-a explicitamente; este script nao adivinha alvo.\n"
            "[m016]   DATABASE_URL=postgresql://... python migrations/m016_handoff_operations.py"
        )
    if url.startswith("sqlite") and not allow_sqlite:
        return None, allow_sqlite, (
            f"[m016] RECUSADO — DATABASE_URL aponta para SQLite ({url}).\n"
            "[m016] Producao e PostgreSQL. Para rodar em dev/teste de proposito,\n"
            "[m016] passe --allow-sqlite."
        )
    return url, allow_sqlite, None


def main(argv):
    logging.basicConfig(level=logging.INFO)
    url, _allow_sqlite, refusal = _resolve_target(argv)
    if refusal:
        print(refusal)
        return 1

    safe = url.split("@")[-1] if "@" in url else url
    print(f"[m016] alvo (conversas): {safe}")

    actions = []

    def report():
        for action in actions:
            print(f"[m016]   {action}")

    try:
        run(create_engine(url), actions)
    except WrongTargetError as exc:
        report()
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada e falha RUIDOSA
        report()
        logger.exception("[m016] falha inesperada")
        print(f"[m016] FALHOU: {type(exc).__name__}: {exc}")
        return 1

    report()
    if any(":created" in a for a in actions):
        print("[m016] OK — tabela, UNIQUE e CHECK aplicados (idempotente)")
    else:
        print("[m016] OK — NO-OP (ja estava aplicada)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
