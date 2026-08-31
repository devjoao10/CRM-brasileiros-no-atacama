"""
m013 — BIA-V2 Fase 0 / Task 0.2: tabela `conversation_events` (CONVERSAS).

Cria (aditivo, sem destruir nada):
  - tabela `conversation_events` — trilha de eventos para reconstruir "o que
    aconteceu com esta conversa?" sem abrir workflow nenhum.
      id                INTEGER/SERIAL PK
      event_id          VARCHAR(36)  NOT NULL, UNIQUE (indice de verdade)
      event_type        VARCHAR(48)  NOT NULL
      conversation_id   INTEGER      (SEM ForeignKey — ver docstring do model)
      lead_id           INTEGER
      message_id        INTEGER
      whatsapp_msg_id   VARCHAR(100)
      state_before      VARCHAR(32)
      state_after       VARCHAR(32)
      action            VARCHAR(64)
      target_user_id    INTEGER
      model             VARCHAR(64)
      model_attempt     INTEGER
      duration_ms       INTEGER
      result            VARCHAR(32)
      error_code        VARCHAR(64)
      payload           JSON
      created_at        TIMESTAMP(TZ) DEFAULT CURRENT_TIMESTAMP
  - uq_conversation_events_event_id            UNIQUE (event_id)
  - ix_conversation_events_conversation_id     INDEX  (conversation_id)
  - ix_conversation_events_created_at          INDEX  (created_at)

Os tres objetos usam os MESMOS nomes que `conversas/app/models/evento.py`
declara em `__table_args__`, para que `create_all()` (bancos novos) e esta
migration (bancos existentes) emitam exatamente o mesmo schema — mesmo
motivo documentado em `Conversation.whatsapp` (AUDIT-2026-08-W2E) e na m011.

SEM ForeignKey em `conversation_id` DE PROPOSITO: `conversation_events` e
append-only e precisa sobreviver a delecao da conversa que descreve.

DDL PORTAVEL, NAO POR DIALETO
------------------------------
`CREATE TABLE IF NOT EXISTS` e `CREATE UNIQUE/NORMAL INDEX IF NOT EXISTS` tem
sintaxe IDENTICA em SQLite e PostgreSQL — confirmado nesta base pela m009
(que precisou ramificar `ALTER TABLE ... ADD CONSTRAINT` vs
`CREATE UNIQUE INDEX` porque o primeiro nao existe identico nos dois
dialetos). Aqui so o TIPO de `id` e de `created_at` muda por dialeto (mesmo
padrao de `_TYPE_BY_DIALECT` da m012) — o restante do DDL e uma unica string.

ROLLBACK: `DROP TABLE conversation_events` — a V1 nunca referencia esta
tabela (ver tests/test_v2_v1_intacta.py, Task 0.3), entao a reversao e total
e sem efeito colateral em nenhum fluxo existente.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja
nascem completos via `Base.metadata.create_all()` no lifespan do Conversas.

ATENCAO — este e o app CONVERSAS (nao o CRM): o script insere `conversas/` no
inicio do sys.path para que `app.*` resolva para conversas/app. Deve rodar em
PROCESSO PROPRIO, nunca importado junto das migrations do CRM.

Ordem em bancos antigos: m003 -> ... -> m011 -> m012 -> m013 (nenhuma
dependencia real entre elas; a ordem so acompanha a numeracao).

Uso (LOCAL / STAGING):
    DATABASE_URL=postgresql://... python migrations/m013_conversation_events.py
    python migrations/m013_conversation_events.py --allow-sqlite

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

logger = logging.getLogger("migrations.m013")


class WrongTargetError(RuntimeError):
    """O alvo nao tem a tabela `conversations`: banco errado, nao 'NO-OP'."""


_TABELA = "conversation_events"
_UNIQUE_EVENT_ID = "uq_conversation_events_event_id"
_INDEX_CONVERSATION_ID = "ix_conversation_events_conversation_id"
_INDEX_CREATED_AT = "ix_conversation_events_created_at"

# Contrato NOME+COLUNA dos indices secundarios - usado SO pelo gate pos-DDL.
# Checar NOME sozinho aceitaria um indice com o nome certo sobre a coluna
# ERRADA (ex.: alguem cria "ix_conversation_events_conversation_id" sobre
# lead_id por engano) - o gate precisa provar "indice DE conversation_id",
# nao so "objeto com esse nome existe". Sem validacao de tipo/ordem/opclass
# de proposito: fora do escopo desta funcao (ver _verificar_pos_ddl).
_INDICES_ESPERADOS = {
    _INDEX_CONVERSATION_ID: ["conversation_id"],
    _INDEX_CREATED_AT: ["created_at"],
}

# Espelha as colunas de conversas/app/models/evento.py:ConversationEvent —
# usado SO pelo gate pos-DDL para provar presenca (nao tipo: ver
# _verificar_pos_ddl).
_COLUNAS_OBRIGATORIAS = [
    "id", "event_id", "event_type", "conversation_id", "lead_id", "message_id",
    "whatsapp_msg_id", "state_before", "state_after", "action", "target_user_id",
    "model", "model_attempt", "duration_ms", "result", "error_code", "payload",
    "created_at",
]

# Mesmo padrao da m012: so o TIPO muda por dialeto, nunca a FORMA do DDL.
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
        # Mesmo gate da m012: banco NOVO nasce completo pelo create_all() e
        # nao precisa deste script; banco EXISTENTE sem `conversations` quer
        # dizer ALVO ERRADO, nao "nada a fazer".
        raise WrongTargetError(
            "[m013] RECUSADO — o alvo nao tem a tabela `conversations`.\n"
            "[m013] Banco NOVO nasce completo pelo create_all() do Conversas e nao\n"
            "[m013] precisa deste script. Banco EXISTENTE sem essa tabela quer dizer\n"
            "[m013] ALVO ERRADO (DATABASE_URL para outra base, nome trocado, replica\n"
            "[m013] vazia). Confira DATABASE_URL: aqui nao existe 'NO-OP' honesto."
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
            "    event_type VARCHAR(48) NOT NULL,"
            "    conversation_id INTEGER,"
            "    lead_id INTEGER,"
            "    message_id INTEGER,"
            "    whatsapp_msg_id VARCHAR(100),"
            "    state_before VARCHAR(32),"
            "    state_after VARCHAR(32),"
            "    action VARCHAR(64),"
            "    target_user_id INTEGER,"
            "    model VARCHAR(64),"
            "    model_attempt INTEGER,"
            "    duration_ms INTEGER,"
            "    result VARCHAR(32),"
            "    error_code VARCHAR(64),"
            "    payload JSON,"
            f"    created_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP"
            ")"
        ))
        actions.append(f"{_TABELA}:{'already-present' if ja_existia else 'created'}")

        conn.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_UNIQUE_EVENT_ID} "
            f"ON {_TABELA} (event_id)"
        ))
        actions.append(f"{_UNIQUE_EVENT_ID}:ensured")

        conn.execute(text(
            f"CREATE INDEX IF NOT EXISTS {_INDEX_CONVERSATION_ID} "
            f"ON {_TABELA} (conversation_id)"
        ))
        actions.append(f"{_INDEX_CONVERSATION_ID}:ensured")

        conn.execute(text(
            f"CREATE INDEX IF NOT EXISTS {_INDEX_CREATED_AT} "
            f"ON {_TABELA} (created_at)"
        ))
        actions.append(f"{_INDEX_CREATED_AT}:ensured")

    return _verificar_pos_ddl(engine, actions)


def _verificar_pos_ddl(engine, actions):
    """
    Prova cada garantia que o docstring de `run()` promete — nunca engole
    nada, nunca deixa `run()` imprimir OK sobre estado que nao verificou.

    `CREATE TABLE IF NOT EXISTS` e `CREATE INDEX IF NOT EXISTS` sao NO-OP
    silencioso contra uma tabela/indice PRE-EXISTENTE ou PARCIAL — so a
    reinspecao pos-DDL (via `inspect()`, nunca lendo de volta o que o proprio
    DDL declarou) pega essa lacuna. Checa PRESENCA de colunas e da UNIQUE, e
    para os indices secundarios checa NOME+COLUNA (`_INDICES_ESPERADOS`) —
    um objeto com o nome certo sobre a coluna errada tambem e rejeitado.
    Nunca tenta provar equivalencia de TIPO/ordem/opclass entre SQLite e
    PostgreSQL, o que exigiria logica fragil por dialeto e esta fora do
    escopo desta funcao.

    Extraida de `run()` para poder ser exercida sozinha (ex.: contra um banco
    SQLite descartavel criado via `Base.metadata.create_all()`) sem nunca
    chamar `run()`/`main()` — os dois continuam proibidos fora de uso humano
    aprovado (ver docstring do modulo).
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
            "UNIQUE em event_id AUSENTE depois do DDL — duplicata de evento nao "
            "seria bloqueada pelo banco. Migration abortada."
        )
    actions.append(f"{_UNIQUE_EVENT_ID}:verificado")

    indices_pos_por_nome = {ix.get("name"): ix for ix in indices_pos}
    for nome_indice, colunas_esperadas in _INDICES_ESPERADOS.items():
        indice = indices_pos_por_nome.get(nome_indice)
        if indice is None:
            raise RuntimeError(f"{nome_indice} AUSENTE depois do DDL — migration abortada.")
        colunas_reais = indice.get("column_names")
        if colunas_reais != colunas_esperadas:
            raise RuntimeError(
                f"{nome_indice} existe mas indexa {colunas_reais} em vez de "
                f"{colunas_esperadas} — nome do indice bate, coluna indexada nao bate. "
                "Migration abortada."
            )
        actions.append(f"{nome_indice}:verificado")

    return actions


def _resolve_target(argv):
    """
    DATABASE_URL vem do AMBIENTE, nunca de app.config: os defaults de config
    apontam para arquivos SQLite locais e transformariam um deploy esquecido em
    "sucesso" contra um banco descartavel (mesmo motivo da m012).
    """
    allow_sqlite = "--allow-sqlite" in argv
    url = os.getenv("DATABASE_URL", "").strip()

    if not url and allow_sqlite:
        # Modo dev explicito: cai no DATABASE_URL do proprio Conversas.
        from app.config import DATABASE_URL as CONVERSAS_URL  # noqa: E402

        return CONVERSAS_URL, allow_sqlite, None
    if not url:
        return None, allow_sqlite, (
            "[m013] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[m013] Exporte-a explicitamente; este script nao adivinha alvo.\n"
            "[m013]   DATABASE_URL=postgresql://... python migrations/m013_conversation_events.py"
        )
    if url.startswith("sqlite") and not allow_sqlite:
        return None, allow_sqlite, (
            f"[m013] RECUSADO — DATABASE_URL aponta para SQLite ({url}).\n"
            "[m013] Producao e PostgreSQL. Para rodar em dev/teste de proposito,\n"
            "[m013] passe --allow-sqlite."
        )
    return url, allow_sqlite, None


def main(argv):
    logging.basicConfig(level=logging.INFO)
    url, _allow_sqlite, refusal = _resolve_target(argv)
    if refusal:
        print(refusal)
        return 1

    safe = url.split("@")[-1] if "@" in url else url
    print(f"[m013] alvo (conversas): {safe}")

    actions = []

    def report():
        for action in actions:
            print(f"[m013]   {action}")

    try:
        run(create_engine(url), actions)
    except WrongTargetError as exc:
        report()
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada e falha RUIDOSA
        report()
        logger.exception("[m013] falha inesperada")
        print(f"[m013] FALHOU: {type(exc).__name__}: {exc}")
        return 1

    report()
    if any(":created" in a for a in actions):
        print("[m013] OK — tabela e indices aplicados (idempotente)")
    else:
        print("[m013] OK — NO-OP (ja estava aplicada)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
