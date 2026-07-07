"""
m002 — Cria a tabela `internal_tasks` (Gestão Interna, WP-GI) em bancos JÁ existentes.

Migration MANUAL e IDEMPOTENTE — **não roda no startup**. Bancos novos são
criados completos por `Base.metadata.create_all()` (model importado em
app/main.py). Este script existe para reconciliar dev/staging/prod existentes.

Usa o próprio metadata do model (checkfirst) → DDL correto por dialeto
(SQLite dev / PostgreSQL prod) e idempotência garantida.

Uso (LOCAL / STAGING):
    python -m migrations.m002_internal_tasks

PRODUÇÃO: somente após backup verificado + aprovação humana (migrations/README.md).
"""
import logging

from sqlalchemy import create_engine, inspect

from app.config import DATABASE_URL
import app.models.user  # noqa: F401 — registra a tabela `users` no metadata (FK de assignee/creator)
from app.models.internal_task import InternalTask

logger = logging.getLogger("migrations.m002")


def run(engine=None):
    engine = engine or create_engine(DATABASE_URL)
    existed = "internal_tasks" in inspect(engine).get_table_names()
    # checkfirst=True: cria só se não existir (idempotente)
    InternalTask.__table__.create(bind=engine, checkfirst=True)
    return ["internal_tasks:already-present"] if existed else ["internal_tasks:created"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m002] alvo: {safe}")
    print("[m002] ações:", run())
    print("[m002] OK (idempotente)")
