"""
BIA-V2 Fase 6 - gate da migration m016 (handoff_operations).

Mesmo formato dos gates das m013/m014/m015. Cobre:

  A-F  `_verificar_pos_ddl(engine, actions)` contra engines SQLite
       DESCARTAVEIS em scratch/, construidos com DDL cru: tabela ausente,
       tabela PARCIAL pre-existente (o caso que faz `CREATE TABLE IF NOT
       EXISTS` virar NO-OP silencioso), UNIQUE ausente e CHECK ausente.
  G-H  EQUIVALENCIA model x migration - a expressao do CHECK e a lista de
       colunas sao COPIAS deliberadas (a migration nao importa o model, por
       convencao de migrations/CLAUDE.md), entao a divergencia entre as duas
       precisa de gate executavel, como no par m015/`conversation.py`.
  I    `run()` de verdade contra um SQLite descartavel: cria, e idempotente na
       segunda rodada, e RECUSA alvo sem a tabela `conversations`. Chamar
       `run()` num alvo descartavel segue o precedente do grupo F de
       tests/test_v2_m015_gate.py.
  J    `_resolve_target`: sem `DATABASE_URL`, contra SQLite sem
       `--allow-sqlite`, e com a flag.
  K    prova COMPORTAMENTAL do CHECK: a tabela criada pela PROPRIA migration
       recusa um `status` fora do vocabulario fechado. Sem isto, o gate de I
       provaria so que existe um objeto com aquele nome.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_v2_m016_gate.py
"""
import os
import pathlib
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT))           # para `import migrations.*` como pacote
sys.path.insert(0, str(CONVERSAS_DIR))  # para `import app.*` resolver ao Conversas

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{(SCRATCH / 'v2_m016_gate_boot.db').as_posix()}"
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
from sqlalchemy.exc import IntegrityError  # noqa: E402

from migrations.m016_handoff_operations import (  # noqa: E402
    _CHECK_STATUS,
    _CHECK_STATUS_SQL,
    _COLUNAS_OBRIGATORIAS,
    _UNIQUE_EVENT_ID,
    WrongTargetError,
    _resolve_target,
    _verificar_pos_ddl,
    run,
)
from app.database import Base  # noqa: E402
from app.models.handoff_op import (  # noqa: E402
    HandoffOperation,
    StatusHandoff,
    _CHECK_STATUS_SQL as _CHECK_DO_MODEL,
)


def _engine_descartavel(nome: str):
    """Engine SQLite proprio deste teste, em arquivo NOVO dentro de scratch/."""
    caminho = SCRATCH / f"v2_m016_gate_{nome}.db"
    if caminho.exists():
        caminho.unlink()
    return create_engine(f"sqlite:///{caminho.as_posix()}")


def _falha_do_gate(engine):
    """Roda o gate e devolve a mensagem de erro, ou None se passou."""
    try:
        _verificar_pos_ddl(engine, [])
        return None
    except RuntimeError as exc:
        return str(exc)


# DDL com todas as colunas obrigatorias, SEM UNIQUE e SEM CHECK - base dos
# cenarios D e E, que testam cada objeto ausente isoladamente.
_DDL_SO_COLUNAS = """
    CREATE TABLE handoff_operations (
        id INTEGER PRIMARY KEY,
        event_id VARCHAR(36) NOT NULL,
        conversation_id INTEGER NOT NULL,
        target_user_id INTEGER NOT NULL,
        status VARCHAR(16) NOT NULL,
        error_code VARCHAR(64),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""

print("=== Fase 6 - gate da m016 (handoff_operations) ===\n")

# --- A. Tabela criada por Base.metadata.create_all() passa limpa ------------
# E a prova mais forte de equivalencia model x migration: o gate da migration
# aceita, sem ajuste nenhum, a tabela que o model emite.
engine_a = _engine_descartavel("create_all")
HandoffOperation.__table__.create(bind=engine_a)
acoes_a = []
try:
    _verificar_pos_ddl(engine_a, acoes_a)
    _erro_a = None
except RuntimeError as exc:
    _erro_a = exc
check(_erro_a is None, f"A. tabela emitida pelo model passa no gate da migration (erro={_erro_a!r})")
check(
    "colunas:verificadas" in acoes_a
    and f"{_UNIQUE_EVENT_ID}:verificado" in acoes_a
    and f"{_CHECK_STATUS}:verificado" in acoes_a,
    f"A2. actions registra cada garantia verificada (acoes={acoes_a!r})",
)

# --- B. Tabela ausente ------------------------------------------------------
engine_b = _engine_descartavel("ausente")
with engine_b.begin() as conn:
    conn.execute(text("CREATE TABLE outra_coisa (id INTEGER PRIMARY KEY)"))
_erro_b = _falha_do_gate(engine_b)
check(_erro_b is not None and "AUSENTE depois do DDL" in _erro_b,
      f"B. tabela ausente e recusada (erro={_erro_b!r})")

# --- C. Tabela PARCIAL pre-existente (o NO-OP silencioso) -------------------
engine_c = _engine_descartavel("parcial")
with engine_c.begin() as conn:
    conn.execute(text(
        "CREATE TABLE handoff_operations ("
        " id INTEGER PRIMARY KEY, event_id VARCHAR(36) NOT NULL, status VARCHAR(16) NOT NULL)"
    ))
    conn.execute(text(f"CREATE UNIQUE INDEX {_UNIQUE_EVENT_ID} ON handoff_operations (event_id)"))
_erro_c = _falha_do_gate(engine_c)
check(
    _erro_c is not None and "colunas ausentes" in _erro_c and "target_user_id" in _erro_c,
    f"C. tabela PARCIAL pre-existente e recusada, nomeando o que falta (erro={_erro_c!r})",
)

# --- D. Colunas completas, UNIQUE ausente -----------------------------------
engine_d = _engine_descartavel("sem_unique")
with engine_d.begin() as conn:
    conn.execute(text(_DDL_SO_COLUNAS))
_erro_d = _falha_do_gate(engine_d)
check(
    _erro_d is not None and "UNIQUE em event_id AUSENTE" in _erro_d,
    f"D. UNIQUE ausente e recusado - sem ele duas operacoes dividiriam a chave "
    f"de idempotencia (erro={_erro_d!r})",
)

# --- E. Colunas + UNIQUE, CHECK ausente -------------------------------------
engine_e = _engine_descartavel("sem_check")
with engine_e.begin() as conn:
    conn.execute(text(_DDL_SO_COLUNAS))
    conn.execute(text(f"CREATE UNIQUE INDEX {_UNIQUE_EVENT_ID} ON handoff_operations (event_id)"))
_erro_e = _falha_do_gate(engine_e)
check(
    _erro_e is not None and _CHECK_STATUS in _erro_e,
    f"E. CHECK ausente e recusado - `status` aceitaria valor fora do vocabulario "
    f"(erro={_erro_e!r})",
)

# --- F. CHECK com nome DIFERENTE nao vale pelo nome esperado ----------------
engine_f = _engine_descartavel("check_outro_nome")
with engine_f.begin() as conn:
    conn.execute(text(
        "CREATE TABLE handoff_operations ("
        " id INTEGER PRIMARY KEY, event_id VARCHAR(36) NOT NULL,"
        " conversation_id INTEGER NOT NULL, target_user_id INTEGER NOT NULL,"
        " status VARCHAR(16) NOT NULL, error_code VARCHAR(64),"
        " created_at TIMESTAMP, updated_at TIMESTAMP,"
        " CONSTRAINT ck_nome_errado CHECK (status IN ('REQUESTED','COMPLETED','FAILED')))"
    ))
    conn.execute(text(f"CREATE UNIQUE INDEX {_UNIQUE_EVENT_ID} ON handoff_operations (event_id)"))
_erro_f = _falha_do_gate(engine_f)
check(
    _erro_f is not None and "ck_nome_errado" in _erro_f,
    f"F. CHECK com outro nome e recusado, e o erro mostra o que foi encontrado (erro={_erro_f!r})",
)

# --- G. Expressao do CHECK: copia deliberada, provada equivalente ----------
check(
    _CHECK_STATUS_SQL == _CHECK_DO_MODEL,
    f"G1. expressao do CHECK identica em model e migration "
    f"(model={_CHECK_DO_MODEL!r}, migration={_CHECK_STATUS_SQL!r})",
)
for _membro in StatusHandoff:
    check(
        f"'{_membro.value}'" in _CHECK_STATUS_SQL,
        f"G2. o token {_membro.value} do enum StatusHandoff aparece no CHECK da migration",
    )
check(
    _CHECK_STATUS_SQL.count("'") == 2 * len(list(StatusHandoff)),
    f"G3. o CHECK nao lista NENHUM token alem dos {len(list(StatusHandoff))} do enum "
    f"({_CHECK_STATUS_SQL!r})",
)

# --- H. Lista de colunas: copia deliberada, provada equivalente -------------
_COLUNAS_DO_MODEL = [c.name for c in HandoffOperation.__table__.columns]
check(
    sorted(_COLUNAS_OBRIGATORIAS) == sorted(_COLUNAS_DO_MODEL),
    f"H. _COLUNAS_OBRIGATORIAS da migration == colunas do model "
    f"(migration={sorted(_COLUNAS_OBRIGATORIAS)}, model={sorted(_COLUNAS_DO_MODEL)})",
)

# --- I. run() contra SQLite DESCARTAVEL: cria, e idempotente, e recusa alvo -
engine_i = _engine_descartavel("run")
with engine_i.begin() as conn:
    # A m016 exige `conversations` no alvo: sem ela, e banco errado, nao NO-OP.
    conn.execute(text("CREATE TABLE conversations (id INTEGER PRIMARY KEY)"))
acoes_1 = run(engine_i, [])
check(
    "handoff_operations:created" in acoes_1,
    f"I1. run() cria a tabela na primeira rodada (acoes={acoes_1!r})",
)
acoes_2 = run(engine_i, [])
check(
    "handoff_operations:already-present" in acoes_2,
    f"I2. run() e IDEMPOTENTE - segunda rodada reconhece o que ja existia (acoes={acoes_2!r})",
)
check(
    f"{_CHECK_STATUS}:verificado" in acoes_2 and f"{_UNIQUE_EVENT_ID}:verificado" in acoes_2,
    "I3. a reexecucao reverifica UNIQUE e CHECK, nunca confia no DDL que acabou de emitir",
)

engine_i_vazio = _engine_descartavel("run_alvo_errado")
try:
    run(engine_i_vazio, [])
    _erro_alvo = None
except WrongTargetError as exc:
    _erro_alvo = str(exc)
except Exception as exc:  # noqa: BLE001
    _erro_alvo = f"tipo errado: {type(exc).__name__}"
check(
    _erro_alvo is not None and "RECUSADO" in _erro_alvo,
    f"I4. alvo sem a tabela `conversations` levanta WrongTargetError, nao 'NO-OP' (={_erro_alvo!r})",
)

# --- J. _resolve_target: sem URL, SQLite sem flag, SQLite com flag ---------
with mock.patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
    _url, _flag, _recusa = _resolve_target([])
check(
    _url is None and _recusa is not None and "DATABASE_URL nao esta definida" in _recusa,
    f"J1. sem DATABASE_URL o script RECUSA - nao adivinha alvo (recusa={_recusa!r})",
)

with mock.patch.dict(os.environ, {"DATABASE_URL": "sqlite:///./x.db"}, clear=False):
    _url, _flag, _recusa = _resolve_target([])
check(
    _url is None and _recusa is not None and "aponta para SQLite" in _recusa,
    f"J2. SQLite sem --allow-sqlite e RECUSADO (recusa={_recusa!r})",
)

with mock.patch.dict(os.environ, {"DATABASE_URL": "sqlite:///./x.db"}, clear=False):
    _url, _flag, _recusa = _resolve_target(["--allow-sqlite"])
check(
    _recusa is None and _url == "sqlite:///./x.db" and _flag is True,
    f"J3. SQLite COM --allow-sqlite e aceito (url={_url!r}, recusa={_recusa!r})",
)

# --- K. Prova COMPORTAMENTAL: o CHECK da migration realmente bloqueia -------
# `engine_i` foi criado pela PROPRIA m016 (DDL cru), nao pelo model - e a
# tabela que vai para producao.
def _inserir(engine, status):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO handoff_operations (event_id, conversation_id, target_user_id, status) "
                "VALUES (:e, 1, 2, :s)"
            ),
            {"e": f"evento-{status}", "s": status},
        )


for _membro in StatusHandoff:
    try:
        _inserir(engine_i, _membro.value)
        _erro_k = None
    except IntegrityError as exc:
        _erro_k = str(exc)
    check(_erro_k is None, f"K1. status '{_membro.value}' e ACEITO pela tabela da migration ({_erro_k})")

try:
    _inserir(engine_i, "PENDING")
    _erro_fora = None
except IntegrityError:
    _erro_fora = "bloqueado"
check(
    _erro_fora == "bloqueado",
    "K2. status fora do vocabulario ('PENDING') e BLOQUEADO pelo banco, nao so pelo Python",
)

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
