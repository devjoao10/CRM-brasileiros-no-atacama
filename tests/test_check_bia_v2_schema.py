"""
BIA-V2 R3 - gate de leitura de `scripts/check_bia_v2_schema.py` (schema-readiness
antes de `V2_MODE=live`). Cobre:

  A     Schema COMPLETO, construido a partir dos MODELS REAIS dos dois apps
        (CRM + Conversas) via `Base.metadata.create_all()`, cada um no seu
        PROPRIO subprocesso (o pacote `app` do CRM e do Conversas colidem no
        mesmo processo - mesma razao de tests/CLAUDE.md; nenhum dos dois lados
        roda no processo deste teste, que so importa o script sob teste e
        `sqlalchemy`) -> `main()` reporta READY, exit 0.
  B     Schema combinado (DDL cru, um unico SQLite - modela a topologia real
        de producao, onde CRM e Conversas compartilham o mesmo banco) faltando
        UMA coluna obrigatoria (`handoff_operations.error_class`) -> BLOCKED,
        exit != 0, nomeando EXATAMENTE a coluna que falta.
  C     Mesmo schema combinado, mas com `ix_handoff_operations_pendentes` na
        forma SUPERADA da m019 (uma coluna so, sem `id`) -> BLOCKED, exit != 0,
        nomeando o indice com as colunas erradas.
  D     (opcional, gated por env var) prova ESPECIFICA de `NULLS FIRST` contra
        um PostgreSQL efemero real - SQLite nao representa a diferenca
        NULLS FIRST/NULLS LAST em `CREATE INDEX` (ver o docstring da
        migrations/m020_handoff_operations_indice_pendentes_nulls_first.py),
        entao esta e a UNICA forma de provar a checagem de catalogo do
        PostgreSQL. Diferente de tests/test_v2_rb_cas_postgres.py (que FALHA
        sem Postgres, porque SEM ele a propria logica sob prova nao reproduz
        em SQLite), aqui A/B/C ja provam o grosso da logica do gate em SQLite;
        so a leitura literal de `pg_indexes` fica de fora sem a env var, por
        isso este bloco faz SKIP gracioso (nao falha) quando ela esta ausente.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por `grep -l CONVERSAS_DIR`).
Roda com Python 3.12 + conversas/requirements.txt (tem psycopg2-binary, usado
so no bloco D opcional) - o subprocesso do lado CRM so precisa de `sqlalchemy`
+ stdlib (nunca bcrypt/passlib/jose/slowapi), entao roda sob qualquer um dos
dois ambientes deste repo sem atrito.

Roda standalone:  python tests/test_check_bia_v2_schema.py
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
SCRIPT_PATH = ROOT / "scripts" / "check_bia_v2_schema.py"

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


# Carrega o script sob teste por CAMINHO DE ARQUIVO. `scripts/` nao e pacote
# Python (sem __init__.py) e o modulo sob teste nao importa nenhum `app.*`
# (checagem generica via `inspect()`, contra listas hardcoded) - carrega-lo
# aqui nao colide com os workers em subprocesso abaixo, que importam `app.*`
# do CRM e do Conversas cada um no seu PROPRIO processo.
_spec = importlib.util.spec_from_file_location("check_bia_v2_schema", str(SCRIPT_PATH))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

from sqlalchemy import create_engine, inspect, text  # noqa: E402


def _engine_descartavel(nome: str):
    """Engine SQLite proprio deste teste, em arquivo NOVO dentro de scratch/."""
    caminho = SCRATCH / f"check_bia_v2_schema_{nome}.db"
    if caminho.exists():
        caminho.unlink()
    return create_engine(f"sqlite:///{caminho.as_posix()}"), caminho


def _rodar_main_capturando(argv):
    """Roda gate.main(argv) capturando stdout. Devolve (codigo_saida, texto)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gate.main(argv)
    return rc, buf.getvalue()


print("=== BIA-V2 R3 - gate de schema (scripts/check_bia_v2_schema.py) ===\n")


# ═══════════════════════════════════════════════════════════════════════════
# A. Schema COMPLETO - models REAIS dos dois apps, um subprocesso por app.
# ═══════════════════════════════════════════════════════════════════════════

_WORKER_CRM = r'''
import os, sys
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = sys.argv[1]
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_INITIAL_ADMIN"] = "false"
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, sys.argv[2])
from app.database import Base, engine
from app.models.lead_handoff_operation import LeadHandoffOperation  # noqa: F401
Base.metadata.create_all(bind=engine)
print("OK")
'''

_WORKER_CONVERSAS = r'''
import os, sys
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = sys.argv[1]
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
sys.path.insert(0, sys.argv[2])
from app.database import Base, engine
from app.models.evento import ConversationEvent  # noqa: F401
from app.models.conversation import Conversation, Message  # noqa: F401
from app.models.handoff_op import HandoffOperation  # noqa: F401
Base.metadata.create_all(bind=engine)
print("OK")
'''


def _construir_schema_real(worker_src, nome, extra_sys_path):
    """Roda `worker_src` em subprocesso proprio para montar, via models REAIS
    e `Base.metadata.create_all()`, o schema daquele app num SQLite descartavel
    dedicado. Devolve a URL do SQLite resultante."""
    caminho = SCRATCH / f"check_bia_v2_schema_{nome}.db"
    if caminho.exists():
        caminho.unlink()
    worker_path = SCRATCH / f"_worker_check_bia_v2_schema_{nome}.py"
    worker_path.write_text(worker_src, encoding="utf-8")
    url = f"sqlite:///{caminho.as_posix()}"
    resultado = subprocess.run(
        [sys.executable, str(worker_path), url, str(extra_sys_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if resultado.returncode != 0:
        raise RuntimeError(
            f"worker {nome} falhou (rc={resultado.returncode}):\n"
            f"STDOUT:\n{resultado.stdout}\nSTDERR:\n{resultado.stderr}"
        )
    return url


_crm_url_real = _construir_schema_real(_WORKER_CRM, "crm_real", ROOT)
_conversas_url_real = _construir_schema_real(_WORKER_CONVERSAS, "conversas_real", CONVERSAS_DIR)

_rc_a, _saida_a = _rodar_main_capturando([
    "--database-url", _crm_url_real,
    "--conversas-database-url", _conversas_url_real,
    "--allow-sqlite",
])
check(_rc_a == 0, f"A1. schema COMPLETO (models reais dos dois apps) -> exit 0 (rc={_rc_a})")
check("READY" in _saida_a, f"A2. relatorio imprime READY (saida={_saida_a!r})")
check("BLOCKED" not in _saida_a, "A3. relatorio nao menciona BLOCKED quando tudo esta presente")


# ═══════════════════════════════════════════════════════════════════════════
# B/C. Schema combinado via DDL cru, num UNICO SQLite (modela a topologia real
#      de producao - CRM e Conversas no MESMO banco). Formato COMPLETO e
#      CORRETO, exceto pelo desvio deliberado de cada cenario.
# ═══════════════════════════════════════════════════════════════════════════

def _construir_schema_combinado(nome, *, colunas_handoff_omitir=(), indice_pendentes_cols="next_attempt_at, id"):
    engine, caminho = _engine_descartavel(nome)

    colunas_handoff = {
        "id": "id INTEGER PRIMARY KEY",
        "event_id": "event_id VARCHAR(36) NOT NULL",
        "conversation_id": "conversation_id INTEGER NOT NULL",
        "target_user_id": "target_user_id INTEGER NOT NULL",
        "status": "status VARCHAR(16) NOT NULL",
        "error_code": "error_code VARCHAR(64)",
        "created_at": "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "expected_responsavel_id": "expected_responsavel_id INTEGER",
        "attempt_count": "attempt_count INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "next_attempt_at TIMESTAMP",
        "reconciled_at": "reconciled_at TIMESTAMP",
        "error_class": "error_class VARCHAR(32)",
    }
    for col in colunas_handoff_omitir:
        colunas_handoff.pop(col, None)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE lead_handoff_operations ("
            "id INTEGER PRIMARY KEY, event_id VARCHAR(36) NOT NULL, lead_id INTEGER NOT NULL,"
            "target_user_id INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "previous_responsavel_id INTEGER"
            ")"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_lead_handoff_operations_event_id ON lead_handoff_operations (event_id)"
        ))

        conn.execute(text(
            "CREATE TABLE conversation_events ("
            "id INTEGER PRIMARY KEY, event_id VARCHAR(36) NOT NULL, event_type VARCHAR(48) NOT NULL,"
            "conversation_id INTEGER, lead_id INTEGER, message_id INTEGER,"
            "whatsapp_msg_id VARCHAR(100), state_before VARCHAR(32), state_after VARCHAR(32),"
            "action VARCHAR(64), target_user_id INTEGER, model VARCHAR(64), model_attempt INTEGER,"
            "duration_ms INTEGER, result VARCHAR(32), error_code VARCHAR(64), payload JSON,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_conversation_events_event_id ON conversation_events (event_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_conversation_events_conversation_id ON conversation_events (conversation_id)"
        ))
        conn.execute(text(
            "CREATE INDEX ix_conversation_events_created_at ON conversation_events (created_at)"
        ))

        conn.execute(text(
            "CREATE TABLE conversations ("
            "id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL, whatsapp VARCHAR(30) NOT NULL,"
            "is_bot_active BOOLEAN NOT NULL DEFAULT 1, atendente_id INTEGER, responsavel_id INTEGER,"
            "queued_at TIMESTAMP, state VARCHAR(32) DEFAULT 'LEGACY', state_version INTEGER DEFAULT 0,"
            "closed_at TIMESTAMP,"
            "CONSTRAINT ck_conversations_estado_humano CHECK ("
            "state NOT IN ('WAITING_HUMAN','HUMAN_ACTIVE') OR ("
            "NOT is_bot_active AND atendente_id IS NOT NULL AND responsavel_id IS NOT NULL "
            "AND atendente_id = responsavel_id AND queued_at IS NOT NULL))"
            ")"
        ))

        colunas_handoff_ddl = ", ".join(colunas_handoff.values())
        conn.execute(text(
            f"CREATE TABLE handoff_operations ({colunas_handoff_ddl}, "
            f"CONSTRAINT ck_handoff_operations_status CHECK (status IN ('REQUESTED','COMPLETED','FAILED')))"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_handoff_operations_event_id ON handoff_operations (event_id)"
        ))
        conn.execute(text(
            f"CREATE INDEX ix_handoff_operations_pendentes ON handoff_operations ({indice_pendentes_cols}) "
            f"WHERE status IN ('REQUESTED','FAILED')"
        ))
    engine.dispose()
    return f"sqlite:///{caminho.as_posix()}"


# --- B. Falta UMA coluna obrigatoria (handoff_operations.error_class) -------
_url_b = _construir_schema_combinado("sem_error_class", colunas_handoff_omitir=("error_class",))
_rc_b, _saida_b = _rodar_main_capturando(["--database-url", _url_b, "--allow-sqlite"])
check(_rc_b != 0, f"B1. schema sem handoff_operations.error_class -> exit != 0 (rc={_rc_b})")
check("BLOCKED" in _saida_b, f"B2. relatorio imprime BLOCKED (saida={_saida_b!r})")
check(
    "missing column: handoff_operations.error_class" in _saida_b,
    f"B3. relatorio nomeia EXATAMENTE a coluna que falta (saida={_saida_b!r})",
)

# --- C. Indice ix_handoff_operations_pendentes na forma SUPERADA da m019 ----
_url_c = _construir_schema_combinado("indice_m019", indice_pendentes_cols="next_attempt_at")
_rc_c, _saida_c = _rodar_main_capturando(["--database-url", _url_c, "--allow-sqlite"])
check(_rc_c != 0, f"C1. indice na forma m019 (sem `id`) -> exit != 0 (rc={_rc_c})")
check("BLOCKED" in _saida_c, f"C2. relatorio imprime BLOCKED (saida={_saida_c!r})")
check(
    "wrong index columns: ix_handoff_operations_pendentes" in _saida_c,
    f"C3. relatorio nomeia o indice com as colunas erradas (saida={_saida_c!r})",
)

# --- Controle: schema combinado COMPLETO (sem nenhum desvio) -> READY ------
# Sem isto, B/C provariam so que o gate as vezes falha - nao que ele falha
# PELO MOTIVO CERTO. Este controle prova que a MESMA construcao, sem o desvio,
# passa - isolando o desvio como a UNICA variavel.
_url_completo = _construir_schema_combinado("completo")
_rc_completo, _saida_completo = _rodar_main_capturando(["--database-url", _url_completo, "--allow-sqlite"])
check(
    _rc_completo == 0 and "READY" in _saida_completo,
    f"Controle. schema combinado sem nenhum desvio -> READY, exit 0 "
    f"(rc={_rc_completo}, saida={_saida_completo!r})",
)


# ═══════════════════════════════════════════════════════════════════════════
# D. (opcional) prova especifica de NULLS FIRST contra PostgreSQL real.
# ═══════════════════════════════════════════════════════════════════════════
_PROOF_URL = os.getenv("DATABASE_URL_CHECK_BIA_V2_SCHEMA_PROOF", "").strip()

if not _PROOF_URL:
    print(
        "SKIP D. prova especifica de NULLS FIRST contra PostgreSQL real - "
        "DATABASE_URL_CHECK_BIA_V2_SCHEMA_PROOF ausente (opcional; suba um "
        "Postgres efemero e exporte a variavel para exercitar este bloco)."
    )
else:
    _engine_pg = create_engine(_PROOF_URL)
    with _engine_pg.begin() as _conn:
        _conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        _conn.execute(text("CREATE SCHEMA public"))
        _conn.execute(text(
            "CREATE TABLE handoff_operations ("
            "id SERIAL PRIMARY KEY, event_id VARCHAR(36) NOT NULL, conversation_id INTEGER NOT NULL,"
            "target_user_id INTEGER NOT NULL, status VARCHAR(16) NOT NULL, error_code VARCHAR(64),"
            "created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,"
            "updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,"
            "expected_responsavel_id INTEGER, attempt_count INTEGER NOT NULL DEFAULT 0,"
            "next_attempt_at TIMESTAMP WITH TIME ZONE, reconciled_at TIMESTAMP WITH TIME ZONE,"
            "error_class VARCHAR(32),"
            "CONSTRAINT ck_handoff_operations_status CHECK (status IN ('REQUESTED','COMPLETED','FAILED'))"
            ")"
        ))
        _conn.execute(text(
            "CREATE UNIQUE INDEX uq_handoff_operations_event_id ON handoff_operations (event_id)"
        ))
        # forma SUPERADA da m019 - sem NULLS FIRST, uma coluna so (sem `id`).
        _conn.execute(text(
            "CREATE INDEX ix_handoff_operations_pendentes ON handoff_operations (next_attempt_at) "
            "WHERE status IN ('REQUESTED','FAILED')"
        ))

    _insp_m019 = inspect(_engine_pg)
    _faltando_m019 = gate.check_handoff_operations(_insp_m019, _engine_pg)
    check(
        len(_faltando_m019) > 0,
        f"D1. forma SUPERADA da m019 (sem NULLS FIRST, sem `id`) e RECUSADA "
        f"(faltando={_faltando_m019!r})",
    )

    with _engine_pg.begin() as _conn:
        _conn.execute(text("DROP INDEX IF EXISTS ix_handoff_operations_pendentes"))
        _conn.execute(text(
            "CREATE INDEX ix_handoff_operations_pendentes ON handoff_operations "
            "(next_attempt_at ASC NULLS FIRST, id ASC) WHERE status IN ('REQUESTED','FAILED')"
        ))

    _insp_m020 = inspect(_engine_pg)
    _faltando_m020 = gate.check_handoff_operations(_insp_m020, _engine_pg)
    check(
        _faltando_m020 == [],
        f"D2. forma CORRIGIDA da m020 (NULLS FIRST + id) e ACEITA (faltando={_faltando_m020!r})",
    )
    _engine_pg.dispose()


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
