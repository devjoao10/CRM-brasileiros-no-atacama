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
    # Invariante de isolamento do handoff V2 (BIA-V2 R-B, desenho secao 2.1.1).
    # READ COMMITTED ja e o default do PostgreSQL, mas ELE PRECISA SER
    # EXPLICITO: o ramo de replay de `POST /api/v2/lead-domain/handoff` le o
    # lead SEM `FOR UPDATE` e compara o dono atual contra o livro-razao — sob
    # READ COMMITTED cada SELECT enxerga o ultimo valor COMMITADO, que e
    # exatamente o "fresco o bastante" que a comparacao exige. Sob REPEATABLE
    # READ/SERIALIZABLE o snapshot congelaria no PRIMEIRO SELECT da requisicao
    # (o do `get_current_user`, antes de a rota rodar) e essa leitura voltaria
    # obsoleta EM SILENCIO — sem erro nenhum, ao contrario do caminho com
    # `FOR UPDATE`, que falharia ruidosamente. Nao ha como o CI pegar uma
    # mudanca acidental aqui: SQLite nao tem o mesmo conceito de nivel de
    # isolamento, e por isso este parametro so existe no ramo PostgreSQL.
    _engine_kwargs["isolation_level"] = "READ COMMITTED"

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
