"""
m003 — CONV-08b: colunas de integridade de outbound na tabela `messages` (CONVERSAS).

Adiciona (aditivo, sem destruir nada):
  - last_error       TEXT NULL      — resumo SEGURO da ultima falha de envio
  - send_attempts    INTEGER NOT NULL DEFAULT 0 — contador de tentativas
  - last_attempt_at  TIMESTAMP(TZ) NULL — ultima tentativa de envio

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja nascem
completos via `Base.metadata.create_all()` no lifespan do Conversas. Este script
reconcilia bancos EXISTENTES (dev/staging/prod).

ATENCAO — este e o app CONVERSAS (nao o CRM):
  - O script insere `conversas/` no inicio do sys.path para que `app.*` resolva
    para conversas/app (mesma tecnica dos tests/test_conversas_*). Por isso
    deve rodar em PROCESSO PROPRIO, nunca importado junto das migrations do CRM.
  - Usa o DATABASE_URL do Conversas (em prod, o mesmo PostgreSQL compartilhado).

Uso (LOCAL / STAGING):
    python migrations/m003_conversas_message_error_fields.py

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

logger = logging.getLogger("migrations.m003")

# (coluna, DDL por dialeto). ADD COLUMN e suportado por SQLite e PostgreSQL.
_COLUMNS = [
    ("last_error", {"default": "TEXT"}),
    ("send_attempts", {"default": "INTEGER NOT NULL DEFAULT 0"}),
    ("last_attempt_at", {"postgresql": "TIMESTAMP WITH TIME ZONE", "default": "TIMESTAMP"}),
]


def run(engine=None):
    engine = engine or create_engine(DATABASE_URL)
    insp = inspect(engine)

    if "messages" not in insp.get_table_names():
        # Banco novo: create_all() do app cria a tabela ja completa.
        return ["messages:table-absent (sera criada completa pelo create_all)"]

    existing = {c["name"] for c in insp.get_columns("messages")}
    dialect = engine.dialect.name
    actions = []

    with engine.begin() as conn:
        for name, ddl_by_dialect in _COLUMNS:
            if name in existing:
                actions.append(f"{name}:already-present")
                continue
            ddl_type = ddl_by_dialect.get(dialect, ddl_by_dialect["default"])
            conn.execute(text(f"ALTER TABLE messages ADD COLUMN {name} {ddl_type}"))
            actions.append(f"{name}:added ({ddl_type})")

    return actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m003] alvo (conversas): {safe}")
    print("[m003] acoes:", run())
    print("[m003] OK (idempotente)")
