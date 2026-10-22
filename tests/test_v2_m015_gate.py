"""
BIA-V2 Fase 4 - gate da migration m015 (CHECK ck_conversations_estado_humano)
e GATE ANTI-DRIFT entre as duas copias da expressao canonica.

A decisao 5 do subgate da Fase 4 troca "single source of truth" por duas
copias deliberadas mais um gate executavel:

    conversas/app/models/conversation.py  ->  _CHECK_ESTADO_HUMANO_SQL
    migrations/m015_...py                 ->  _CHECK_ESTADO_HUMANO_SQL

Os dois arquivos de PRODUCAO nao se importam (migration nao importa model neste
repo). Este TESTE importa os dois - a proibicao de import e entre eles, nao
para quem os verifica. E ele que falha se alguem mexer num lado so.

`main()` da m015 NUNCA e chamado aqui. A maioria dos cenarios exercita
`_verificar_pos_ddl` e `normalizar_expressao` isoladamente, no molde de
`tests/test_v2_m013_gate.py`; o grupo F chama `run()` de verdade contra um
engine e um inspector simulados, para provar o fix da reexecucao com
`pg_constraint.convalidated=False` (ver `_constraint_validada` em
`migrations/m015_conversas_estado_humano_check.py`).

Roda standalone:  python tests/test_v2_m015_gate.py
"""
import os
import pathlib
import re
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Migration do CONVERSAS - o literal abaixo tambem e o discriminador de job do
# CI (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT))           # para `import migrations.*` como pacote
sys.path.insert(0, str(CONVERSAS_DIR))  # para `import app.*` resolver ao Conversas

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{(SCRATCH / 'v2_m015_gate_boot.db').as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""

from sqlalchemy import CheckConstraint  # noqa: E402

from app.models.conversation import (  # noqa: E402
    _CHECK_ESTADO_HUMANO_SQL as SQL_MODEL,
    Conversation,
)
from migrations.m015_conversas_estado_humano_check import (  # noqa: E402
    _CHECK_ESTADO_HUMANO_SQL as SQL_MIGRATION,
    _CONSTRAINT,
    _TABELA,
    normalizar_expressao,
)

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


print("=== m015 - gate anti-drift e pos-DDL ===\n")

# ---------------------------------------------------------------------------
# GRUPO A - as duas copias concordam
# ---------------------------------------------------------------------------
check(
    normalizar_expressao(SQL_MODEL) == normalizar_expressao(SQL_MIGRATION),
    "A1. constante do model e da m015 sao identicas apos normalizacao minima",
)
check(
    SQL_MODEL.strip() != "" and SQL_MIGRATION.strip() != "",
    "A2. nenhuma das duas constantes esta vazia (guarda contra falso positivo)",
)
# As cinco condicoes da decisao 5 tem de estar nas duas - se as duas
# estivessem igualmente ERRADAS, A1 passaria e nada teria sido provado.
for token in (
    "WAITING_HUMAN",
    "HUMAN_ACTIVE",
    "NOT is_bot_active",
    "atendente_id IS NOT NULL",
    "responsavel_id IS NOT NULL",
    "atendente_id = responsavel_id",
    "queued_at IS NOT NULL",
):
    check(
        token in normalizar_expressao(SQL_MODEL),
        f"A3. a expressao contem a condicao da decisao 5: {token!r}",
    )
check(
    "primeira_resposta_humana_at" not in SQL_MODEL and "status" not in SQL_MODEL,
    "A4. a expressao NAO cita primeira_resposta_humana_at nem status (decisao 5: cinco condicoes)",
)

# ---------------------------------------------------------------------------
# GRUPO B - o CheckConstraint do model usa a CONSTANTE, nao uma string inline
# ---------------------------------------------------------------------------
_constraints = [c for c in Conversation.__table_args__ if isinstance(c, CheckConstraint)]
check(len(_constraints) == 1, f"B1. o model tem exatamente 1 CheckConstraint (achou {len(_constraints)})")
if _constraints:
    _cc = _constraints[0]
    check(_cc.name == _CONSTRAINT, f"B2. o CheckConstraint chama-se {_CONSTRAINT}")
    check(
        normalizar_expressao(str(_cc.sqltext)) == normalizar_expressao(SQL_MODEL),
        "B3. o CheckConstraint usa a constante do model, nao uma string inline divergente",
    )

# O codigo-fonte do model referencia a constante pelo NOME dentro do
# CheckConstraint - se alguem inlinear a string, isto pega.
_fonte_model = (CONVERSAS_DIR / "app" / "models" / "conversation.py").read_text(encoding="utf-8")
check(
    re.search(r"CheckConstraint\(\s*_CHECK_ESTADO_HUMANO_SQL", _fonte_model) is not None,
    "B4. o fonte do model passa a CONSTANTE ao CheckConstraint (nao um literal)",
)

# ---------------------------------------------------------------------------
# GRUPO C - o gate DISCRIMINA: alterar um lado so tem de reprovar
# ---------------------------------------------------------------------------
# Este e o item (4) da decisao 5. Sem ele as duas copias poderiam divergir e o
# teste seguiria verde. Alimenta-se o comparador com pares deliberadamente
# divergentes e prova-se que ele os separa.
_divergentes = (
    ("condicao removida", SQL_MODEL.replace("    AND queued_at IS NOT NULL\n", "")),
    ("estado trocado", SQL_MODEL.replace("HUMAN_ACTIVE", "CLOSED")),
    ("igualdade invertida", SQL_MODEL.replace("atendente_id = responsavel_id", "atendente_id != responsavel_id")),
    ("NOT removido", SQL_MODEL.replace("NOT is_bot_active", "is_bot_active")),
    ("parenteses removidos", SQL_MODEL.replace("OR (", "OR ").replace("\n)\n", "\n")),
    ("caixa trocada", SQL_MODEL.replace(" AND ", " and ")),
)
for rotulo, mutante in _divergentes:
    check(
        normalizar_expressao(mutante) != normalizar_expressao(SQL_MODEL),
        f"C1. comparador REJEITA divergencia: {rotulo}",
    )

# ---------------------------------------------------------------------------
# GRUPO D - a normalizacao e minima, nao semanticamente frouxa
# ---------------------------------------------------------------------------
check(
    normalizar_expressao("a   AND\n  b") == "a AND b",
    "D1. colapsa espacos e quebras em um espaco",
)
check(normalizar_expressao("  a AND b  ") == "a AND b", "D2. apara as bordas")
check(
    normalizar_expressao("(a AND b)") != normalizar_expressao("a AND b"),
    "D3. NAO remove parenteses",
)
check(
    normalizar_expressao("a AND b") != normalizar_expressao("a and b"),
    "D4. NAO normaliza caixa de operador",
)
check(
    normalizar_expressao("a AND b") != normalizar_expressao("b AND a"),
    "D5. NAO trata AND como comutativo",
)
check(
    normalizar_expressao("x = 'LEGACY'") != normalizar_expressao("x = 'legacy'"),
    "D6. NAO normaliza caixa de literal",
)
check(normalizar_expressao(None) is None, "D7. ausencia continua ausencia")

# ---------------------------------------------------------------------------
# GRUPO E - pos-DDL: nome certo com expressao errada tem de FALHAR
# ---------------------------------------------------------------------------
from migrations.m015_conversas_estado_humano_check import _verificar_pos_ddl  # noqa: E402


class _InspFake:
    """Dubla `get_check_constraints` e `get_table_names`, que e tudo que o
    pos-gate e o `run()` consultam via `inspect()`."""

    def __init__(self, constraints, tabelas=(_TABELA,)):
        self._constraints = constraints
        self._tabelas = tabelas

    def get_check_constraints(self, tabela):
        return self._constraints

    def get_table_names(self):
        return self._tabelas


class _ConnFake:
    """
    Dubla de Connection usada tanto por `.connect()` (leitura de
    `convalidated`) quanto por `.begin()` (DDL do VALIDATE CONSTRAINT). Cada
    `.execute()` e registrado em `log`, para o teste provar SE e QUANDO o
    VALIDATE CONSTRAINT da reexecucao rodou.
    """

    class _ResultFake:
        def __init__(self, valor):
            self._valor = valor

        def scalar(self):
            return self._valor

    def __init__(self, log, scalar_result=None):
        self._log = log
        self._scalar_result = scalar_result

    def execute(self, stmt, params=None):
        self._log.append(str(stmt))
        return _ConnFake._ResultFake(self._scalar_result)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _EngineFake:
    """
    Dubla de Engine. `convalidated` simula `pg_constraint.convalidated` para o
    `_constraint_validada` REAL rodar contra ela (nao mockado); `ddl_log`
    acumula todo `.execute()` feito via `.connect()`/`.begin()`.
    """

    class _DialectFake:
        def __init__(self, name):
            self.name = name

    def __init__(self, constraints, convalidated=True, dialect="postgresql"):
        self._insp = _InspFake(constraints)
        self.dialect = _EngineFake._DialectFake(dialect)
        self._convalidated = convalidated
        self.ddl_log = []

    def connect(self):
        return _ConnFake(self.ddl_log, scalar_result=self._convalidated)

    def begin(self):
        return _ConnFake(self.ddl_log)


def _pos_gate(constraints):
    """Roda o pos-gate contra um catalogo simulado. Devolve (ok, erro)."""
    with mock.patch(
        "migrations.m015_conversas_estado_humano_check.inspect",
        lambda _engine: _InspFake(constraints),
    ):
        try:
            _verificar_pos_ddl(_EngineFake(constraints), [])
            return True, None
        except RuntimeError as exc:
            return False, f"{type(exc).__name__}: {exc}"


_expressao_boa = normalizar_expressao(SQL_MIGRATION)

ok, erro = _pos_gate([{"name": _CONSTRAINT, "sqltext": _expressao_boa}])
check(ok, f"E1. constraint com nome e expressao certos e ACEITA (erro={erro})")

ok, erro = _pos_gate([])
check(not ok, "E2. constraint AUSENTE e rejeitada")
check((not ok) and "AUSENTE" in (erro or ""), f"E2b. mensagem certa - erro={erro}")

ok, erro = _pos_gate([{"name": _CONSTRAINT, "sqltext": "state IS NOT NULL"}])
check(not ok, "E3. nome CERTO com expressao ERRADA e rejeitada")
check(
    (not ok) and "expressao nao bate" in (erro or ""),
    f"E3b. mensagem separa nome de expressao - erro={erro}",
)

ok, erro = _pos_gate([{"name": _CONSTRAINT, "sqltext": None}])
check(not ok, "E4. catalogo sem a expressao e rejeitado (nao da para provar nada)")

ok, erro = _pos_gate([{"name": "outra_constraint_qualquer", "sqltext": _expressao_boa}])
check(not ok, "E5. constraint com OUTRO nome nao conta como presente")

# Expressao a que falta UMA das colunas obrigatorias - o caso sutil.
_faltando_queued = _expressao_boa.replace("AND queued_at IS NOT NULL", "")
ok, erro = _pos_gate([{"name": _CONSTRAINT, "sqltext": _faltando_queued}])
check(not ok, "E6. expressao sem `queued_at` e rejeitada, apesar de nome certo")

# ---------------------------------------------------------------------------
# GRUPO F - reexecucao com VALIDATE CONSTRAINT incompleto (achado dos
# revisores security-reviewer/python-reviewer): run() tem que completar o
# VALIDATE antes de reportar sucesso, e nao repetir o DDL quando desnecessario.
# ---------------------------------------------------------------------------
from migrations.m015_conversas_estado_humano_check import run  # noqa: E402


def _run_com_duplas(constraints, convalidated):
    """Roda run() de verdade contra inspect()/engine simulados. Devolve (actions, engine, erro)."""
    engine = _EngineFake(constraints, convalidated=convalidated)
    with mock.patch(
        "migrations.m015_conversas_estado_humano_check.inspect",
        lambda _engine: _InspFake(constraints),
    ):
        try:
            return run(engine, []), engine, None
        except RuntimeError as exc:
            return None, engine, f"{type(exc).__name__}: {exc}"


actions, engine, erro = _run_com_duplas(
    [{"name": _CONSTRAINT, "sqltext": _expressao_boa}], convalidated=False
)
check(erro is None, f"F1. run() nao levanta na reexecucao com convalidated=False (erro={erro})")
check(
    any("VALIDATE CONSTRAINT" in s for s in engine.ddl_log),
    "F2. run() executa VALIDATE CONSTRAINT quando a reexecucao acha convalidated=False",
)
check(
    actions is not None and f"{_CONSTRAINT}:validated" in actions,
    "F3. a acao 'validated' so aparece depois do VALIDATE CONSTRAINT ter rodado",
)

actions2, engine2, erro2 = _run_com_duplas(
    [{"name": _CONSTRAINT, "sqltext": _expressao_boa}], convalidated=True
)
check(erro2 is None, f"F4. run() nao levanta na reexecucao com convalidated=True (erro={erro2})")
check(
    not any("VALIDATE CONSTRAINT" in s for s in engine2.ddl_log),
    "F5. run() NAO repete o VALIDATE CONSTRAINT quando a constraint ja esta convalidated",
)

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
