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
    # Invariante de isolamento do handoff V2 (BIA-V2 R-B, desenho secao 2.1.1),
    # espelhando `app/database.py` do CRM — engine SEPARADA (processo proprio),
    # entao o parametro NAO se herda entre os dois lados e precisa ser
    # declarado aqui tambem. READ COMMITTED ja e o default do PostgreSQL, mas
    # ELE PRECISA SER EXPLICITO: `handoff._ler_baseline_cas` le
    # `leads.responsavel_id` SEM lock para montar o baseline do
    # compare-and-swap que este lado ENVIA ao CRM, e
    # `worker_reconciliacao._dono_no_crm` le a MESMA coluna para decidir entre
    # "convergiu" e "divergencia nova" — as duas leituras exigem ver o ultimo
    # valor COMMITADO no banco compartilhado, exatamente o que READ COMMITTED
    # garante. Sob REPEATABLE READ/SERIALIZABLE o snapshot congelaria no
    # primeiro SELECT da sessao e as duas leriam um valor obsoleto EM
    # SILENCIO, sem erro nenhum. Nao ha como o CI pegar uma mudanca acidental
    # aqui: SQLite nao tem o mesmo conceito de nivel de isolamento, e por isso
    # este parametro so existe no ramo PostgreSQL.
    _engine_kwargs["isolation_level"] = "READ COMMITTED"

engine = create_engine(DATABASE_URL, **_engine_kwargs)

# AUDIT-2026-08-WA — exportado para que as rotas possam ramificar o dialeto.
# O `SELECT ... FOR UPDATE` que serializa claim/handoff e no-op no SQLite (o
# banco inteiro ja e serializado por lock de arquivo) e obrigatorio no
# PostgreSQL de producao. O ramo NAO existe porque `with_for_update()` levante
# excecao no SQLite — o SQLAlchemy DESCARTA a clausula EM SILENCIO nesse
# dialeto (nenhuma excecao, com ou sem transacao aberta; ver tambem o
# comentario de `_aplicar_estado_local` em `conversas/app/v2/handoff.py`, que
# confirma isso por probe direto contra a versao instalada do SQLAlchemy),
# nunca levanta. O ramo existe porque, sem o lock real, duas escritas
# concorrentes no PostgreSQL de producao aplicariam as duas sem serializacao
# nenhuma.
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
