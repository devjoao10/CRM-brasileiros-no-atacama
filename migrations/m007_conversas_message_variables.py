"""
m007 — CONV-VAR-01: cria `message_variables` (variaveis dinamicas @TOKEN do
CONVERSAS) em bancos JA existentes.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Padrao m004/m005/m006:
sys.path do conversas, processo proprio, checkfirst.

ADITIVA: cria UMA tabela nova. Nao altera, nao migra e nao reescreve nenhuma
tabela existente (templates, mensagens rapidas e historico ficam intactos).

Ordem em bancos antigos: m003 -> m004 -> m005 -> m006 -> m007.

Uso (LOCAL / STAGING):
    python migrations/m007_conversas_message_variables.py

PRODUCAO: somente apos backup verificado + aprovacao humana, ANTES do codigo.
"""
import logging
import pathlib
import sys

_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))

from sqlalchemy import create_engine, inspect  # noqa: E402

from app.config import DATABASE_URL  # noqa: E402 — config do CONVERSAS
from app.models.message_variable import MessageVariable  # noqa: E402

logger = logging.getLogger("migrations.m007")


def run(engine=None):
    engine = engine or create_engine(DATABASE_URL)
    existed = "message_variables" in inspect(engine).get_table_names()
    MessageVariable.__table__.create(bind=engine, checkfirst=True)
    return ["message_variables:already-present"] if existed else ["message_variables:created"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m007] alvo (conversas): {safe}")
    print("[m007] acoes:", run())
    print("[m007] OK (idempotente)")
