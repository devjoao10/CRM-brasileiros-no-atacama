"""
m008 — PACOTE-A: estado operacional confiavel do CONVERSAS.

Adiciona (aditivo, sem destruir nada):
  - conversations.queued_at  TIMESTAMP(TZ) NULL — momento em que a conversa
    ENTROU na fila de atendimento humano. NAO e last_customer_msg_at,
    updated_at nem created_at.

Indices (a tabela tem ~81 linhas em producao; custo desprezivel):
  - ix_conversations_queued_at     — ordenacao FIFO da fila
  - ix_conversations_atendente_id  — filtro "Meus atendimentos"/"fila"
    (responsavel_id ja tem indice; atendente_id nunca teve)

Correcao de estado LEGADO (idempotente):
  - conversations com atendente_id NOT NULL e is_bot_active = TRUE passam a
    is_bot_active = FALSE. Auditoria em producao encontrou 5 linhas nesse
    estado, contraditorio com a nova regra "humano assumiu -> BIA desligada".
    Seguro porque `atendente_id` so e escrito por acao HUMANA (claim/assign/
    PUT); nenhum fluxo automatizado o usa — ver tests/test_conversas_operational_state.py.

BACKFILL DE queued_at: NAO E FEITO AQUI, de proposito. Nenhuma coluna atual
representa o instante do handoff; usar last_customer_msg_at/updated_at/
created_at inventaria uma posicao de fila falsa. Dados legados ficam com
queued_at = NULL e serao tratados numa auditoria propria antes do PR da UI.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja
nascem completos via `Base.metadata.create_all()` no lifespan do Conversas.
Este script reconcilia bancos EXISTENTES (dev/staging/prod).

ATENCAO — este e o app CONVERSAS (nao o CRM):
  - O script insere `conversas/` no inicio do sys.path para que `app.*` resolva
    para conversas/app (mesma tecnica dos tests/test_conversas_*). Por isso
    deve rodar em PROCESSO PROPRIO, nunca importado junto das migrations do CRM.
  - Usa o DATABASE_URL do Conversas (em prod, o mesmo PostgreSQL compartilhado).

Ordem em bancos antigos: m003 -> m004 -> m005 -> m006 -> m007 -> m008.

Uso (LOCAL / STAGING):
    python migrations/m008_conversas_queued_at.py

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

logger = logging.getLogger("migrations.m008")

# (coluna, DDL por dialeto). ADD COLUMN e suportado por SQLite e PostgreSQL.
_COLUMNS = [
    ("queued_at", {"postgresql": "TIMESTAMP WITH TIME ZONE", "default": "TIMESTAMP"}),
]

# (nome do indice, coluna). CREATE INDEX IF NOT EXISTS: SQLite e PostgreSQL.
_INDEXES = [
    ("ix_conversations_queued_at", "queued_at"),
    ("ix_conversations_atendente_id", "atendente_id"),
]


def run(engine=None):
    engine = engine or create_engine(DATABASE_URL)
    insp = inspect(engine)

    if "conversations" not in insp.get_table_names():
        # Banco novo: create_all() do app cria a tabela ja completa.
        return ["conversations:table-absent (sera criada completa pelo create_all)"]

    existing = {c["name"] for c in insp.get_columns("conversations")}
    dialect = engine.dialect.name
    actions = []

    with engine.begin() as conn:
        for name, ddl_by_dialect in _COLUMNS:
            if name in existing:
                actions.append(f"{name}:already-present")
                continue
            ddl_type = ddl_by_dialect.get(dialect, ddl_by_dialect["default"])
            conn.execute(text(f"ALTER TABLE conversations ADD COLUMN {name} {ddl_type}"))
            actions.append(f"{name}:added ({ddl_type})")

        for idx_name, column in _INDEXES:
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {idx_name} "
                    f"ON conversations ({column})"
                )
            )
            actions.append(f"{idx_name}:ensured")

        # Estado legado contraditorio: humano atendendo com a BIA ligada.
        result = conn.execute(
            text(
                "UPDATE conversations SET is_bot_active = :off "
                "WHERE atendente_id IS NOT NULL AND is_bot_active = :on"
            ),
            {"off": False, "on": True},
        )
        actions.append(f"bot-legado-desligado:{result.rowcount}")

    return actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m008] alvo (conversas): {safe}")
    print("[m008] acoes:", run())
    print("[m008] OK (idempotente)")
