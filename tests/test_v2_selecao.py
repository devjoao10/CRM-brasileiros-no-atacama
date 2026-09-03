"""
BIA-V2 Fase 5 - selecao de humano.

`conversas/app/v2/selecao.py::selecionar_atendente(db)` e um envelope fino
sobre `resolver_atendente_elegivel(db)` (V1, conversas/app/services/atendimento.py)
- existe so para que o codigo V2 chame modulos `v2/`, nunca a V1 diretamente.
Fonte normativa: docs/superpowers/plans/2026-08-29-bia-v2.md, secao "Fase 5 -
Selecao de humano".

Este arquivo prova:

  1. Nenhum atendente elegivel (banco vazio ou todos inativos) -> None, sem
     fallback e sem excecao.
  2. Exatamente um atendente elegivel -> devolve o id dele.
  3. Multiplos elegiveis com cargas diferentes -> o MESMO id que
     `resolver_atendente_elegivel(db)` devolveria para a MESMA configuracao
     de banco - prova de delegacao real, nao de uma reimplementacao paralela
     que por acaso concorda.
  4. Prova estatica (AST) de que nenhum literal inteiro aparece como valor de
     retorno hardcoded em `selecao.py`.

A Fase 6 (handoff, WAITING_HUMAN, HANDOFF_FAILED) NAO e testada aqui - esta
fase prova so a funcao de selecao em si.

Roda standalone: python tests/test_v2_selecao.py
"""
import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Conversas - o literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)

sys.path.insert(0, str(CONVERSAS_DIR))

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{(SCRATCH / 'v2_selecao_boot.db').as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
# Deterministico: nao herdar uma configuracao real de ATENDENTES_ELEGIVEIS do
# ambiente onde o teste roda - os cenarios abaixo querem exercitar "todos os
# usuarios ativos" (comportamento default de atendentes_elegiveis).
os.environ["ATENDENTES_ELEGIVEIS"] = ""

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.auth import User  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
# `Conversation`/`Message` tem relationships por STRING ("ConversationTag",
# "ConversationNote", "MediaAsset"...) que so resolvem se TODAS as classes
# mapeadas do Conversas ja estiverem registradas no mesmo registro
# declarativo no momento em que o primeiro query configura os mappers.
# Importar `app.main` e o jeito estabelecido nesta suite de garantir isso
# (mesmo problema resolvido do mesmo jeito em test_v2_v1_intacta.py, Familia
# B) - o import so registra classes/rotas em memoria, nao inicia o servidor
# nem abre conexao nenhuma (isso so aconteceria no lifespan do ASGI).
import app.main  # noqa: E402,F401
from app.services.atendimento import resolver_atendente_elegivel  # noqa: E402
from app.v2.selecao import selecionar_atendente  # noqa: E402


def _sessao_nova(nome):
    """Session sobre uma engine SQLite descartavel, isolada por cenario."""
    caminho = SCRATCH / f"v2_selecao_{nome}.db"
    if caminho.exists():
        caminho.unlink()
    engine = create_engine(f"sqlite:///{caminho.as_posix()}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


print("=== Fase 5 - selecionar_atendente ===\n")

# --- 1. Banco vazio -----------------------------------------------------
db = _sessao_nova("vazio")
check(selecionar_atendente(db) is None, "1. banco sem nenhum usuario -> None")
db.close()

# --- 2. Todos os atendentes inativos -------------------------------------
db = _sessao_nova("todos_inativos")
db.add_all([
    User(nome="Inativa Um", email="inativa1@x.com", hashed_password="x", is_active=False),
    User(nome="Inativa Dois", email="inativa2@x.com", hashed_password="x", is_active=False),
])
db.commit()
check(selecionar_atendente(db) is None, "2. todos os usuarios inativos -> None")
db.close()

# --- 3. Exatamente um atendente elegivel ---------------------------------
db = _sessao_nova("um_elegivel")
unica = User(nome="Unica", email="unica@x.com", hashed_password="x", is_active=True)
db.add(unica)
db.commit()
check(selecionar_atendente(db) == unica.id, "3. um unico elegivel -> devolve o id dele")
db.close()

# --- 4. Multiplos elegiveis, cargas diferentes - prova de DELEGACAO REAL -
# u1 ja tem duas conversas ABERTAS atribuidas, u2 nenhuma: resolver_atendente_
# elegivel deve preferir u2 (menor carga). O teste chama as duas funcoes na
# MESMA sessao/fixture e compara - isso e o que distingue delegacao real de
# uma reimplementacao paralela que por acaso acerta o caso trivial de 1
# elegivel (caso 3) mas nao precisa acertar o desempate por carga.
db = _sessao_nova("multiplos")
u1 = User(nome="Carregada", email="carregada@x.com", hashed_password="x", is_active=True)
u2 = User(nome="Livre", email="livre@x.com", hashed_password="x", is_active=True)
db.add_all([u1, u2])
db.commit()
db.add_all([
    Conversation(lead_id=1, whatsapp="5511900000001", status="aberta",
                 unread_count=0, is_bot_active=False, atendente_id=u1.id),
    Conversation(lead_id=1, whatsapp="5511900000002", status="aberta",
                 unread_count=0, is_bot_active=False, atendente_id=u1.id),
])
db.commit()

_esperado = resolver_atendente_elegivel(db)
_obtido = selecionar_atendente(db)
check(_esperado is not None, "4a. pre-condicao: resolver_atendente_elegivel encontrou alguem")
check(
    _obtido == _esperado,
    f"4b. selecionar_atendente delega para o MESMO id que a V1 devolveria (esperado={_esperado} obtido={_obtido})",
)
check(_obtido == u2.id, f"4c. quem tem MENOR carga (Livre={u2.id}) e quem recebe (obtido={_obtido})")
db.close()

# --- 5. Prova estatica: nenhum literal inteiro no modulo inteiro ---------
# Varre o MODULO INTEIRO, nao so o valor do `return` — um literal escondido
# atras de nome/ramo (`_ID = 1; return x or _ID`) escaparia de uma checagem
# escopada só à subárvore do `return`, e essa mesma checagem escopada
# também confundiria um argumento de chamada com o valor retornado. Este
# módulo existe para ser um envelope sem decisão própria: a invariante real
# é "nenhum literal inteiro em lugar nenhum do arquivo", não só "no return".
_arquivo_selecao = CONVERSAS_DIR / "app" / "v2" / "selecao.py"
_codigo = _arquivo_selecao.read_text(encoding="utf-8")
_arvore = ast.parse(_codigo, filename=str(_arquivo_selecao))

_linhas_com_int_hardcoded = sorted({
    _no.lineno
    for _no in ast.walk(_arvore)
    if isinstance(_no, ast.Constant) and isinstance(_no.value, int) and not isinstance(_no.value, bool)
})

check(
    _linhas_com_int_hardcoded == [],
    f"5. nenhum literal inteiro hardcoded em lugar nenhum de selecao.py (linhas={_linhas_com_int_hardcoded})",
)

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
