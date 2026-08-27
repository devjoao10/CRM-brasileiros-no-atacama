from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DATABASE_URL

# Adapt engine kwargs to the backend (SQLite vs PostgreSQL)
_is_sqlite = DATABASE_URL.startswith("sqlite")

_engine_kwargs: dict = {"pool_pre_ping": True}
if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10

engine = create_engine(DATABASE_URL, **_engine_kwargs)

# AUDIT-2026-08-WA — exportado para que as rotas possam ramificar o dialeto.
# O `SELECT ... FOR UPDATE` que serializa claim/handoff e no-op no SQLite (o
# banco inteiro ja e serializado por lock de arquivo) e obrigatorio no
# PostgreSQL de producao. Sem o ramo, `with_for_update()` levanta no SQLite.
IS_SQLITE = _is_sqlite

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependency that provides a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
