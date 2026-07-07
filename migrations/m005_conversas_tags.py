"""
m005 — CONV-05: cria `conversation_tags` + `conversation_tag_links` (CONVERSAS)
em bancos JA existentes.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos nascem
completos via create_all (models importados pelos routers). Padrao m004:
sys.path do conversas, processo proprio, checkfirst por tabela.

Ordem em bancos antigos: m003 -> m004 -> m005 (links referenciam conversations).

Uso (LOCAL / STAGING):
    python migrations/m005_conversas_tags.py

PRODUCAO: somente apos backup verificado + aprovacao humana, ANTES do codigo.
"""
import logging
import pathlib
import sys

_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))

from sqlalchemy import create_engine, inspect  # noqa: E402

from app.config import DATABASE_URL  # noqa: E402 — config do CONVERSAS
import app.models.conversation  # noqa: F401, E402 — registra `conversations` (FK)
from app.models.tag import ConversationTag, conversation_tag_links  # noqa: E402

logger = logging.getLogger("migrations.m005")


def run(engine=None):
    engine = engine or create_engine(DATABASE_URL)
    existing = set(inspect(engine).get_table_names())
    actions = []
    for name, table in [
        ("conversation_tags", ConversationTag.__table__),
        ("conversation_tag_links", conversation_tag_links),
    ]:
        if name in existing:
            actions.append(f"{name}:already-present")
        else:
            table.create(bind=engine, checkfirst=True)
            actions.append(f"{name}:created")
    return actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m005] alvo (conversas): {safe}")
    print("[m005] acoes:", run())
    print("[m005] OK (idempotente)")
