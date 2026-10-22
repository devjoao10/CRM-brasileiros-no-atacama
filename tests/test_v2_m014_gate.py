"""
BIA-V2 Fase 4 - gate pos-DDL da migration m014 (colunas de estado).

Cobre SO `_verificar_pos_ddl(engine, actions)`, extraida de
`migrations/m014_conversas_state_columns.py` para ser exercivel sem rodar a
migration inteira. `run()`/`main()` da m014 NUNCA sao chamados neste arquivo -
nenhum dos dois e sequer importado. Os cenarios abaixo constroem engines
SQLite DESCARTAVEIS em scratch/ com DDL cru e chamam so a funcao de
verificacao contra cada um.

O que os cenarios provam, alem do caminho feliz:

- Banco PARCIAL (uma ou duas das tres colunas) tem de ser REJEITADO. E o caso
  real: `ALTER TABLE ADD COLUMN` que ja rodou para uma coluna e morreu antes
  das outras deixa a proxima execucao pulando por "already-present" contra um
  schema incompleto.
- DEFAULT AUSENTE tem de ser rejeitado. Sem ele, um INSERT vindo de fora da
  ORM grava NULL e a garantia de que toda linha da V1 nasce 'LEGACY' cai.
- DEFAULT de `state_version` como STRING (`'0'`) tem de ser rejeitado, mesmo
  "existindo". A distincao entre `DEFAULT 0` e `DEFAULT '0'` e o que
  `server_default=text("0")` protege no lado do ORM; aqui e reforco. Um gate
  que so pergunta "existe default?" passaria com o bug.
- LINHA COM NULL depois do backfill tem de ser rejeitada.

Um gate que so aceita o caminho feliz nao e gate: cada cenario negativo abaixo
prova que a funcao DISCRIMINA, nao que ela e complacente.

Roda standalone:  python tests/test_v2_m014_gate.py
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
os.environ["DATABASE_URL"] = f"sqlite:///{(SCRATCH / 'v2_m014_gate_boot.db').as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""

from sqlalchemy import create_engine, text  # noqa: E402

from migrations.m014_conversas_state_columns import (  # noqa: E402
    _normalizar_default,
    _verificar_pos_ddl,
)

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


def _engine(nome):
    """Engine SQLite descartavel, sempre do zero."""
    caminho = SCRATCH / f"v2_m014_gate_{nome}.db"
    if caminho.exists():
        caminho.unlink()
    return create_engine(f"sqlite:///{caminho.as_posix()}")


def _criar_conversations(engine, colunas_extra, linhas=()):
    """
    Monta uma `conversations` minima por DDL CRU - nunca pelo model, para que o
    cenario controle exatamente o que existe e o que falta.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE conversations ("
                "  id INTEGER PRIMARY KEY,"
                "  whatsapp VARCHAR(30) NOT NULL,"
                "  is_bot_active BOOLEAN NOT NULL DEFAULT 1,"
                "  atendente_id INTEGER,"
                "  responsavel_id INTEGER,"
                "  queued_at TIMESTAMP"
                + (", " + ", ".join(colunas_extra) if colunas_extra else "")
                + ")"
            )
        )
        for linha in linhas:
            conn.execute(text(linha))
    return engine


_COMPLETO = (
    "state VARCHAR(32) DEFAULT 'LEGACY'",
    "state_version INTEGER DEFAULT 0",
    "closed_at TIMESTAMP",
)


def _gate(engine):
    """Roda o gate e devolve (ok, mensagem_de_erro_ou_None, actions)."""
    actions = []
    try:
        _verificar_pos_ddl(engine, actions)
        return True, None, actions
    except RuntimeError as exc:
        return False, f"{type(exc).__name__}: {exc}", actions


print("=== m014 - gate pos-DDL ===\n")

# --- 1. Caminho feliz --------------------------------------------------------
eng = _criar_conversations(_engine("completo"), _COMPLETO)
ok, erro, actions = _gate(eng)
check(ok, f"1. banco completo e ACEITO (erro={erro})")
check("colunas:verificadas" in actions, "1b. actions registra colunas:verificadas")
check("default-state:verificado" in actions, "1c. actions registra default-state")
check("default-state_version:numerico" in actions, "1d. actions registra default numerico")
check(
    "sem-nulos-state:verificado" in actions and "sem-nulos-state_version:verificado" in actions,
    "1e. actions registra ausencia de nulos nas duas colunas",
)

# --- 2. Banco PARCIAL -------------------------------------------------------
for nome, extras, faltando in (
    ("so_state", ("state VARCHAR(32) DEFAULT 'LEGACY'",), "state_version e closed_at"),
    (
        "sem_closed_at",
        ("state VARCHAR(32) DEFAULT 'LEGACY'", "state_version INTEGER DEFAULT 0"),
        "closed_at",
    ),
    ("nenhuma", (), "as tres"),
):
    eng = _criar_conversations(_engine(nome), extras)
    ok, erro, _ = _gate(eng)
    check(not ok, f"2. banco parcial ({faltando} faltando) e REJEITADO")
    check(
        (not ok) and "colunas ausentes" in (erro or ""),
        f"2b. mensagem nomeia as colunas ausentes ({faltando}) - erro={erro}",
    )

# --- 3. DEFAULT ausente -----------------------------------------------------
eng = _criar_conversations(
    _engine("sem_default_state"),
    ("state VARCHAR(32)", "state_version INTEGER DEFAULT 0", "closed_at TIMESTAMP"),
)
ok, erro, _ = _gate(eng)
check(not ok, "3. state SEM DEFAULT e REJEITADO")
check((not ok) and "sem DEFAULT" in (erro or ""), f"3b. mensagem explica o risco - erro={erro}")

eng = _criar_conversations(
    _engine("sem_default_version"),
    ("state VARCHAR(32) DEFAULT 'LEGACY'", "state_version INTEGER", "closed_at TIMESTAMP"),
)
ok, erro, _ = _gate(eng)
check(not ok, "3c. state_version SEM DEFAULT e REJEITADO")

# --- 4. DEFAULT existe mas com a FORMA errada -------------------------------
# Este e o cenario que um gate ingenuo ("existe default?") deixaria passar.
eng = _criar_conversations(
    _engine("default_string"),
    ("state VARCHAR(32) DEFAULT 'LEGACY'", "state_version INTEGER DEFAULT '0'", "closed_at TIMESTAMP"),
)
ok, erro, _ = _gate(eng)
check(not ok, "4. state_version com DEFAULT '0' (string) e REJEITADO, apesar de EXISTIR")
check(
    (not ok) and "literal string" in (erro or ""),
    f"4b. mensagem aponta o literal string e o text(\"0\") - erro={erro}",
)

eng = _criar_conversations(
    _engine("default_errado"),
    ("state VARCHAR(32) DEFAULT 'NEW'", "state_version INTEGER DEFAULT 0", "closed_at TIMESTAMP"),
)
ok, erro, _ = _gate(eng)
check(not ok, "4c. state com DEFAULT 'NEW' (valor errado) e REJEITADO")

# --- 5. Linha com NULL depois do backfill -----------------------------------
eng = _criar_conversations(
    _engine("com_nulo"),
    _COMPLETO,
    linhas=(
        "INSERT INTO conversations (id, whatsapp, state, state_version) "
        "VALUES (1, '5599', NULL, 0)",
    ),
)
ok, erro, _ = _gate(eng)
check(not ok, "5. linha com state NULL e REJEITADA")
check((not ok) and "NULL depois do backfill" in (erro or ""), f"5b. mensagem nomeia o backfill - erro={erro}")

eng = _criar_conversations(
    _engine("com_nulo_version"),
    _COMPLETO,
    linhas=(
        "INSERT INTO conversations (id, whatsapp, state, state_version) "
        "VALUES (1, '5599', 'LEGACY', NULL)",
    ),
)
ok, erro, _ = _gate(eng)
check(not ok, "5c. linha com state_version NULL e REJEITADA")

# --- 6. Tabela ausente ------------------------------------------------------
eng = _engine("sem_tabela")
ok, erro, _ = _gate(eng)
check(not ok, "6. banco sem `conversations` e REJEITADO")
check((not ok) and "AUSENTE depois do DDL" in (erro or ""), f"6b. mensagem certa - erro={erro}")

# --- 7. Linha valida nao atrapalha ------------------------------------------
eng = _criar_conversations(
    _engine("com_linha_valida"),
    _COMPLETO,
    linhas=(
        "INSERT INTO conversations (id, whatsapp, state, state_version) "
        "VALUES (1, '5599', 'LEGACY', 0)",
    ),
)
ok, erro, _ = _gate(eng)
check(ok, f"7. banco completo COM linha valida e ACEITO (erro={erro})")

# --- 8. Normalizador de default ---------------------------------------------
# Cortar cast e aspas deixa PostgreSQL e SQLite comparaveis sem afirmar
# equivalencia de TIPO - que o gate declara fora de escopo.
check(_normalizar_default("'LEGACY'") == "legacy", "8. normaliza aspas do SQLite")
check(
    _normalizar_default("'LEGACY'::character varying") == "legacy",
    "8b. normaliza cast do PostgreSQL",
)
check(_normalizar_default("0") == "0", "8c. numerico passa intacto")
check(_normalizar_default(None) is None, "8d. ausencia continua ausencia")
# O normalizador COME as aspas - e por isso a checagem de forma numerica nao
# pode depender dele. Se dependesse, `DEFAULT '0'` viraria "0" e o cenario 4
# passaria. A prova de que ela olha o texto BRUTO e o proprio cenario 4, que
# rejeita; aqui so se registra que os dois lados da distincao normalizam igual.
check(
    _normalizar_default("'0'") == _normalizar_default("0") == "0",
    "8e. normalizador iguala '0' e 0 - por isso a forma numerica olha o BRUTO (cenario 4)",
)

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
