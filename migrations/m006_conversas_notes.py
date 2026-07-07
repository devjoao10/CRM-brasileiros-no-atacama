"""
m006 — CONV-07: cria `conversation_notes` (notas internas do CONVERSAS)
em bancos JA existentes.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Padrao m004/m005:
sys.path do conversas, processo proprio, checkfirst.

Ordem em bancos antigos: m003 -> m004 -> m005 -> m006.

Uso (LOCAL / STAGING):
    python migrations/m006_conversas_notes.py

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
from app.models.note import ConversationNote  # noqa: E402

logger = logging.getLogger("migrations.m006")


def run(engine=None):
    engine = engine or create_engine(DATABASE_URL)
    existed = "conversation_notes" in inspect(engine).get_table_names()
    ConversationNote.__table__.create(bind=engine, checkfirst=True)
    return ["conversation_notes:already-present"] if existed else ["conversation_notes:created"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m006] alvo (conversas): {safe}")
    print("[m006] acoes:", run())
    print("[m006] OK (idempotente)")
