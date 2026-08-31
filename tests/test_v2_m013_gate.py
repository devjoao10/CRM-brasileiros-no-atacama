"""
BIA-V2 Fase 0 / Task 0.2 - gate pos-DDL da migration m013 (conversation_events).

Cobre SO `_verificar_pos_ddl(engine, actions)`, extraida de
`migrations/m013_conversation_events.py` especificamente para ser testavel
sem rodar a migration inteira (ver docstring da funcao). `run()`/`main()` da
m013 NUNCA sao chamados neste arquivo - nenhum dos dois e sequer importado.
Os 4 cenarios abaixo constroem engines SQLite DESCARTAVEIS em scratch/ com
DDL cru e chamam so a funcao de verificacao contra cada um.

O cenario que motivou a extracao (tabela PARCIAL pre-existente faz
`CREATE TABLE IF NOT EXISTS` virar NO-OP silencioso) nao tinha nenhuma
cobertura antes deste arquivo.

Roda standalone:  python tests/test_v2_m013_gate.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Migration do CONVERSAS - o literal abaixo tambem e o discriminador de job do
# CI (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT))           # para `import migrations.*` como pacote
sys.path.insert(0, str(CONVERSAS_DIR))  # para `import app.*` resolver ao Conversas

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{(SCRATCH / 'v2_m013_gate_boot.db').as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


from sqlalchemy import create_engine, text  # noqa: E402

# So a funcao de verificacao e os nomes dos objetos que ela checa - `run` e
# `main` nao aparecem nem no import, de proposito (ver docstring do modulo).
from migrations.m013_conversation_events import (  # noqa: E402
    _UNIQUE_EVENT_ID,
    _INDEX_CONVERSATION_ID,
    _verificar_pos_ddl,
)
# Import por ultimo de proposito, mesma logica do check 18 de
# test_v2_eventos_validacao.py: so precisamos do model de eventos, nao do
# resto do Conversas.
from app.database import Base  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402,F401 - registra a tabela em Base.metadata


def _engine_descartavel(nome: str):
    """Engine SQLite proprio deste teste, em arquivo NOVO dentro de scratch/."""
    caminho = SCRATCH / f"v2_m013_gate_{nome}.db"
    if caminho.exists():
        caminho.unlink()
    return create_engine(f"sqlite:///{caminho.as_posix()}")


# DDL com as 18 colunas obrigatorias, SEM nenhum indice - usado pelos
# cenarios 3 e 4, que testam a falta de indice/UNIQUE isoladamente da falta
# de coluna.
_DDL_COLUNAS_COMPLETAS = """
    CREATE TABLE conversation_events (
        id INTEGER PRIMARY KEY,
        event_id VARCHAR(36) NOT NULL,
        event_type VARCHAR(48) NOT NULL,
        conversation_id INTEGER,
        lead_id INTEGER,
        message_id INTEGER,
        whatsapp_msg_id VARCHAR(100),
        state_before VARCHAR(32),
        state_after VARCHAR(32),
        action VARCHAR(64),
        target_user_id INTEGER,
        model VARCHAR(64),
        model_attempt INTEGER,
        duration_ms INTEGER,
        result VARCHAR(32),
        error_code VARCHAR(64),
        payload JSON,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""


# --- 1. Tabela criada por Base.metadata.create_all() passa limpa ----------
engine_limpo = _engine_descartavel("limpo")
Base.metadata.create_all(bind=engine_limpo)
acoes_limpo = []
try:
    _verificar_pos_ddl(engine_limpo, acoes_limpo)
    ok, erro = True, None
except RuntimeError as exc:
    ok, erro = False, exc
check(ok, f"1. tabela criada por Base.metadata.create_all() passa limpa no gate (erro={erro!r})")
check(
    ok and "colunas:verificadas" in acoes_limpo and f"{_UNIQUE_EVENT_ID}:verificado" in acoes_limpo,
    f"1b. actions registra cada garantia verificada (acoes={acoes_limpo!r})",
)


# --- 2. Tabela PARCIAL (so id/event_id) - o cenario que motivou a extracao -
# CREATE TABLE IF NOT EXISTS contra isto seria NO-OP silencioso; o gate
# pos-DDL e a UNICA coisa que pega a lacuna.
engine_parcial = _engine_descartavel("parcial")
with engine_parcial.begin() as conn:
    conn.execute(text(
        "CREATE TABLE conversation_events (id INTEGER PRIMARY KEY, event_id VARCHAR(36))"
    ))
levantou, mensagem = False, ""
try:
    _verificar_pos_ddl(engine_parcial, [])
except RuntimeError as exc:
    levantou, mensagem = True, str(exc)
check(levantou, "2. tabela PARCIAL (so id/event_id) e rejeitada pelo gate")
check(
    "event_type" in mensagem and "created_at" in mensagem,
    f"2b. RuntimeError nomeia as colunas ausentes (mensagem={mensagem!r})",
)


# --- 3. Todas as 18 colunas, mas SEM a UNIQUE - RuntimeError sobre a UNIQUE
engine_sem_unique = _engine_descartavel("sem_unique")
with engine_sem_unique.begin() as conn:
    conn.execute(text(_DDL_COLUNAS_COMPLETAS))
levantou, mensagem = False, ""
try:
    _verificar_pos_ddl(engine_sem_unique, [])
except RuntimeError as exc:
    levantou, mensagem = True, str(exc)
check(levantou, "3. tabela com as 18 colunas mas SEM a UNIQUE de event_id e rejeitada")
check("UNIQUE" in mensagem, f"3b. RuntimeError menciona a UNIQUE ausente (mensagem={mensagem!r})")


# --- 4. UNIQUE presente, mas falta ix_conversation_events_conversation_id -
engine_sem_indice = _engine_descartavel("sem_indice")
with engine_sem_indice.begin() as conn:
    conn.execute(text(_DDL_COLUNAS_COMPLETAS))
    conn.execute(text(
        f"CREATE UNIQUE INDEX {_UNIQUE_EVENT_ID} ON conversation_events (event_id)"
    ))
levantou, mensagem = False, ""
try:
    _verificar_pos_ddl(engine_sem_indice, [])
except RuntimeError as exc:
    levantou, mensagem = True, str(exc)
check(levantou, "4. UNIQUE presente mas ix_conversation_events_conversation_id ausente e rejeitado")
check(
    _INDEX_CONVERSATION_ID in mensagem,
    f"4b. RuntimeError nomeia {_INDEX_CONVERSATION_ID} (mensagem={mensagem!r})",
)


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
