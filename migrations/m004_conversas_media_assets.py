"""
m004 — CONV-01: cria a tabela `media_assets` (fundacao de midia do CONVERSAS)
em bancos JA existentes.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos nascem
completos via `Base.metadata.create_all()` no lifespan do Conversas (o model e
importado pelo webhook router). Este script reconcilia dev/staging/prod
existentes, usando o proprio metadata do model (checkfirst) → DDL correto por
dialeto (SQLite dev / PostgreSQL prod) e idempotencia garantida (padrao m002).

ATENCAO — este e o app CONVERSAS (nao o CRM):
  - Insere `conversas/` no inicio do sys.path para que `app.*` resolva para
    conversas/app (padrao m003). Rodar em PROCESSO PROPRIO, nunca importado
    junto das migrations do CRM.
  - Usa o DATABASE_URL do Conversas (em prod, o mesmo PostgreSQL compartilhado).
  - A tabela referencia `messages` (FK) — m003 deve ter rodado antes em bancos
    antigos (ordem: m003 -> m004).

Uso (LOCAL / STAGING):
    python migrations/m004_conversas_media_assets.py

PRODUCAO: somente apos backup verificado + aprovacao humana, ANTES de subir o
codigo do CONV-01 (migrations/README.md).
"""
import logging
import pathlib
import sys

# `app.*` deve resolver para conversas/app — ver docstring.
_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))

from sqlalchemy import create_engine, inspect  # noqa: E402

from app.config import DATABASE_URL  # noqa: E402 — config do CONVERSAS
import app.models.conversation  # noqa: F401, E402 — registra `messages` no metadata (FK)
from app.models.media_asset import MediaAsset  # noqa: E402

logger = logging.getLogger("migrations.m004")


def run(engine=None):
    engine = engine or create_engine(DATABASE_URL)
    existed = "media_assets" in inspect(engine).get_table_names()
    # checkfirst=True: cria so se nao existir (idempotente)
    MediaAsset.__table__.create(bind=engine, checkfirst=True)
    return ["media_assets:already-present"] if existed else ["media_assets:created"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m004] alvo (conversas): {safe}")
    print("[m004] acoes:", run())
    print("[m004] OK (idempotente)")
