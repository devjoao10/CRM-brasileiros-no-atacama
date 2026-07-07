"""
m001 — Schema drift: colunas de viagem em `leads`, ajustes em `tasks` e índices.

Migration MANUAL e IDEMPOTENTE. **NÃO roda no startup do app** (foi removida de
`app/main.py` no DATA-01). Bancos NOVOS são criados completos por
`Base.metadata.create_all()` a partir dos models; esta migration existe apenas
para reconciliar bancos JÁ EXISTENTES (dev/staging/prod) que não têm as colunas.

Colunas exigidas (definidas em app/models/lead.py e usadas em
app/schemas/lead.py, app/routers/leads.py e app/routers/pipeline.py):
  leads: dias_por_destino, total_dias, datas_destinos, num_viajantes,
         num_criancas, idades_criancas
  tasks: user_id NULLABLE (tarefas da IA), resultado_ia

Uso (LOCAL / STAGING):
    python -m migrations.m001_schema_drift_leads_tasks
    (usa DATABASE_URL de app.config; dev = SQLite)

PRODUÇÃO: somente após backup verificado + aprovação humana (ver README.md).
Idempotente: cada ALTER é guardado por verificação de coluna existente.
Compatível com SQLite (dev) e PostgreSQL (prod).
"""
import logging

from sqlalchemy import create_engine, inspect, text

from app.config import DATABASE_URL

logger = logging.getLogger("migrations.m001")

LEAD_COLUMNS = [
    ("dias_por_destino", "JSON DEFAULT NULL"),
    ("total_dias", "INTEGER DEFAULT NULL"),
    ("datas_destinos", "JSON DEFAULT NULL"),
    ("num_viajantes", "INTEGER DEFAULT NULL"),
    ("num_criancas", "INTEGER DEFAULT 0"),
    ("idades_criancas", "VARCHAR(200) DEFAULT NULL"),
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_leads_created_at ON leads (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks (status)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_user_id ON tasks (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_lead_id ON tasks (lead_id)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_data_vencimento ON tasks (data_vencimento)",
]


def run(engine=None):
    """Aplica a migration idempotente. Retorna a lista de ações executadas."""
    engine = engine or create_engine(DATABASE_URL)
    actions = []
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    with engine.begin() as conn:
        if "leads" in tables:
            existing = [c["name"] for c in inspector.get_columns("leads")]
            for col, ddl in LEAD_COLUMNS:
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE leads ADD COLUMN {col} {ddl}"))
                    actions.append(f"leads.+{col}")

        if "tasks" in tables:
            # user_id deve permitir NULL (tarefas da IA). SQLite não suporta DROP NOT NULL.
            try:
                conn.execute(text("ALTER TABLE tasks ALTER COLUMN user_id DROP NOT NULL"))
                actions.append("tasks.user_id->nullable")
            except Exception as e:  # noqa: BLE001 — esperado em SQLite
                logger.info("skip tasks.user_id DROP NOT NULL (%s)", e.__class__.__name__)
            tcols = [c["name"] for c in inspector.get_columns("tasks")]
            if "resultado_ia" not in tcols:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN resultado_ia TEXT DEFAULT NULL"))
                actions.append("tasks.+resultado_ia")

        for ddl in INDEXES:
            try:
                conn.execute(text(ddl))
            except Exception as e:  # noqa: BLE001
                logger.warning("index skip (%s)", e.__class__.__name__)
        actions.append("indexes:ensured")

    return actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # NUNCA imprime credenciais — só o host/arquivo do banco
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m001] alvo: {safe}")
    done = run()
    print("[m001] ações:", done or "(nenhuma — schema já atualizado)")
    print("[m001] OK (idempotente)")
