from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DATABASE_URL

# Detecta qual banco está sendo usado
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# Configurações do engine conforme o banco
_engine_kwargs = {}
if IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL: pool de conexões para concorrência
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 20
    _engine_kwargs["pool_pre_ping"] = True  # Detecta conexões mortas

engine = create_engine(DATABASE_URL, **_engine_kwargs)

# SQLite não ativa foreign keys por padrão — sem isso, CASCADE não funciona!
# PostgreSQL já ativa por padrão, então só aplicamos para SQLite.
if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency injection for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
