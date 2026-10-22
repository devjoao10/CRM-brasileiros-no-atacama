"""
BIA-V2 Fase 4 - maquina de estados (`conversas/app/v2/estados.py`) e o CHECK
`ck_conversations_estado_humano` no model.

Cobre a "Matriz de testes da Fase 4" do subgate (decisoes 1 a 15, em
`docs/superpowers/plans/2026-08-29-bia-v2.md`), grupo por grupo:

  A. Enum `Estado` - ordem e valores literais sao contrato
  B. Grafo - as 15 arestas, e todo par ausente recusado
  C. Os seis tokens de recusa, um a um
  D. Mensagens adversariais - o valor cru NUNCA e alcancavel, nem pela
     cadeia de excecao (`__cause__`/`__context__`)
  E. Atomicidade - recusa nao muta nenhum dos tres campos de estado
  F. Nao-escrita - os seis campos operacionais nunca sao tocados
  G. `closed_at` - relogio fixado por substituicao, nunca por tolerancia
  H. Pureza - prova AST, com a allowlist propria da Fase 4
  I. Reabertura - `decidir_reabertura` e `dentro_janela_reabertura`
  J. Model e CHECK - contra SQLite descartavel criado por `create_all`

Roda standalone:  python tests/test_v2_estados.py
"""
import ast
import itertools
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Modulo do CONVERSAS - o literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)

sys.path.insert(0, str(CONVERSAS_DIR))

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{(SCRATCH / 'v2_estados_boot.db').as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""

from app.v2.estados import (  # noqa: E402
    JANELA_REABERTURA_PADRAO,
    TRANSICOES_PERMITIDAS,
    Estado,
    TransicaoInvalida,
    aplicar_transicao,
    decidir_reabertura,
    dentro_janela_reabertura,
)

failures = []
total = 0


def check(cond, msg):
    global total
    total += 1
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


# ---------------------------------------------------------------------------
# Dublê de conversa. Campos operacionais preparados pelo CALLER, como a
# decisao 14 manda - `aplicar_transicao` so os LE.
# ---------------------------------------------------------------------------
_SEIS_OPERACIONAIS = (
    "is_bot_active",
    "atendente_id",
    "responsavel_id",
    "queued_at",
    "primeira_resposta_humana_at",
    "status",
)

_AGORA = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


class Conversa:
    def __init__(self, **kw):
        self.state = None
        self.state_version = 0
        self.closed_at = None
        self.is_bot_active = True
        self.atendente_id = None
        self.responsavel_id = None
        self.queued_at = None
        self.primeira_resposta_humana_at = None
        self.status = "aberta"
        for k, v in kw.items():
            setattr(self, k, v)

    def snapshot_operacional(self):
        return {c: getattr(self, c) for c in _SEIS_OPERACIONAIS}

    def snapshot_estado(self):
        return (self.state, self.state_version, self.closed_at)


def conversa_em(estado, **kw):
    """Conversa com os campos operacionais coerentes com `estado` (tabela da decisao 5)."""
    base = dict(state=estado.value, state_version=1)
    if estado in (Estado.NEW, Estado.AI_TRIAGE):
        base.update(is_bot_active=True, status="aberta")
    elif estado is Estado.WAITING_HUMAN:
        base.update(
            is_bot_active=False, atendente_id=7, responsavel_id=7,
            queued_at=_AGORA, primeira_resposta_humana_at=None, status="aberta",
        )
    elif estado is Estado.HUMAN_ACTIVE:
        base.update(
            is_bot_active=False, atendente_id=7, responsavel_id=7,
            queued_at=_AGORA, primeira_resposta_humana_at=_AGORA, status="aberta",
        )
    elif estado is Estado.CLOSED:
        base.update(is_bot_active=False, status="encerrada", closed_at=_AGORA)
    base.update(kw)
    return Conversa(**base)


def preparar_para(destino, conv):
    """Ajusta os campos operacionais que o CALLER escreveria antes da transicao."""
    if destino in (Estado.NEW, Estado.AI_TRIAGE):
        conv.is_bot_active = True
        conv.atendente_id = None
        conv.queued_at = None
        conv.primeira_resposta_humana_at = None
        conv.status = "aberta"
    elif destino is Estado.WAITING_HUMAN:
        conv.is_bot_active = False
        conv.atendente_id = 7
        conv.responsavel_id = 7
        conv.queued_at = _AGORA
        conv.primeira_resposta_humana_at = None
        conv.status = "aberta"
    elif destino is Estado.HUMAN_ACTIVE:
        conv.is_bot_active = False
        conv.atendente_id = 7
        conv.responsavel_id = 7
        conv.queued_at = _AGORA
        conv.primeira_resposta_humana_at = _AGORA
        conv.status = "aberta"
    elif destino is Estado.CLOSED:
        conv.is_bot_active = False
        conv.status = "encerrada"
    return conv


def erro_de(fn, *a, **kw):
    """Roda e devolve a TransicaoInvalida, ou None se nao levantou."""
    try:
        fn(*a, **kw)
        return None
    except TransicaoInvalida as exc:
        return exc


def cadeia_de(exc):
    """Toda mensagem alcancavel pela cadeia de excecao, incluindo __context__."""
    partes = [str(exc)]
    cur = exc
    while (cur.__cause__ or cur.__context__) and len(partes) < 6:
        cur = cur.__cause__ or cur.__context__
        partes.append(str(cur))
    return partes


print("=== Fase 4 - maquina de estados ===\n")

# ---------------------------------------------------------------------------
# A. Enum
# ---------------------------------------------------------------------------
check(
    list(Estado) == [Estado.NEW, Estado.AI_TRIAGE, Estado.WAITING_HUMAN,
                     Estado.HUMAN_ACTIVE, Estado.CLOSED, Estado.LEGACY],
    "A1. list(Estado) na ordem de declaracao do contrato",
)
check(
    [e.value for e in Estado] == ["NEW", "AI_TRIAGE", "WAITING_HUMAN",
                                  "HUMAN_ACTIVE", "CLOSED", "LEGACY"],
    "A2. valores literais uppercase, iguais aos nomes (sem auto())",
)
check(Estado("WAITING_HUMAN") is Estado.WAITING_HUMAN, "A3. coercao devolve o MEMBRO (is)")
check(Estado("LEGACY") is Estado.LEGACY, "A4. idem para LEGACY")
for ruim in ("waiting_human", "qualquer_coisa", ""):
    try:
        Estado(ruim)
        check(False, f"A5. Estado({ruim!r}) devia levantar ValueError")
    except ValueError:
        check(True, f"A5. Estado({ruim!r}) levanta ValueError na coercao direta")

# ---------------------------------------------------------------------------
# B. Grafo
# ---------------------------------------------------------------------------
_arestas = {(o, d) for o, ds in TRANSICOES_PERMITIDAS.items() for d in ds}
check(len(_arestas) == 15, f"B1. exatamente 15 arestas (achou {len(_arestas)})")
check(TRANSICOES_PERMITIDAS[Estado.LEGACY] == set(), "B2. LEGACY sem aresta de SAIDA")
check(
    not any(Estado.LEGACY in ds for ds in TRANSICOES_PERMITIDAS.values()),
    "B3. LEGACY sem aresta de ENTRADA",
)
check(set(TRANSICOES_PERMITIDAS) == set(Estado), "B4. as 6 origens estao no dicionario")

for origem, destino in sorted(_arestas, key=lambda p: (p[0].value, p[1].value)):
    conv = preparar_para(destino, conversa_em(origem))
    if destino is Estado.CLOSED:
        conv.closed_at = None
    versao_antes = conv.state_version
    exc = erro_de(aplicar_transicao, conv, destino, motivo="teste_aresta")
    check(exc is None, f"B5. aresta permitida {origem.value} -> {destino.value} e ACEITA (erro={exc})")
    check(
        conv.state == destino.value and conv.state_version == versao_antes + 1,
        f"B6. {origem.value} -> {destino.value} grava o destino e incrementa 1",
    )

_proibidos = [
    (Estado.NEW, Estado.NEW), (Estado.NEW, Estado.WAITING_HUMAN),
    (Estado.NEW, Estado.HUMAN_ACTIVE), (Estado.NEW, Estado.LEGACY),
    (Estado.AI_TRIAGE, Estado.HUMAN_ACTIVE), (Estado.AI_TRIAGE, Estado.LEGACY),
    (Estado.WAITING_HUMAN, Estado.NEW), (Estado.WAITING_HUMAN, Estado.LEGACY),
    (Estado.HUMAN_ACTIVE, Estado.WAITING_HUMAN), (Estado.HUMAN_ACTIVE, Estado.LEGACY),
    (Estado.CLOSED, Estado.CLOSED), (Estado.CLOSED, Estado.NEW),
    (Estado.CLOSED, Estado.LEGACY), (Estado.LEGACY, Estado.LEGACY),
    (Estado.LEGACY, Estado.AI_TRIAGE),
]
for origem, destino in _proibidos:
    conv = preparar_para(destino, conversa_em(origem))
    exc = erro_de(aplicar_transicao, conv, destino, motivo="teste_proibida")
    check(exc is not None, f"B7. aresta PROIBIDA {origem.value} -> {destino.value} levanta")
    check(
        exc is not None and "aresta_nao_permitida" in str(exc),
        f"B8. {origem.value} -> {destino.value} usa o token aresta_nao_permitida",
    )

for estado in (Estado.AI_TRIAGE, Estado.WAITING_HUMAN, Estado.HUMAN_ACTIVE):
    conv = conversa_em(estado)
    antes = conv.state_version
    exc = erro_de(aplicar_transicao, conv, estado, motivo="reassign")
    check(exc is None, f"B9. self-transition {estado.value} e permitida (erro={exc})")
    check(conv.state_version == antes + 1, f"B10. self-transition {estado.value} incrementa exatamente 1")

# ---------------------------------------------------------------------------
# C. Os seis tokens
# ---------------------------------------------------------------------------
_casos_token = [
    ("motivo_invalido", conversa_em(Estado.NEW), Estado.AI_TRIAGE, "Motivo Com Espaco"),
    ("estado_atual_invalido", Conversa(state="INEXISTENTE"), Estado.AI_TRIAGE, "ok"),
    ("destino_invalido", conversa_em(Estado.NEW), "AI_TRIAGE", "ok"),
    ("aresta_nao_permitida", conversa_em(Estado.NEW), Estado.HUMAN_ACTIVE, "ok"),
    ("state_version_invalido", conversa_em(Estado.NEW, state_version=None), Estado.AI_TRIAGE, "ok"),
    ("invariante_destino", conversa_em(Estado.AI_TRIAGE, is_bot_active=False), Estado.AI_TRIAGE, "ok"),
]
for token, conv, destino, motivo in _casos_token:
    exc = erro_de(aplicar_transicao, conv, destino, motivo=motivo)
    check(exc is not None, f"C1. {token}: levanta TransicaoInvalida")
    check(exc is not None and token in str(exc), f"C2. {token}: a mensagem carrega o token")

for ruim, rot in ((None, "None"), ("", "vazio"), ("MAIUSCULA", "maiuscula"),
                  ("com espaco", "espaco"), ("ok\n", "newline final"), (7, "nao-str")):
    conv = conversa_em(Estado.NEW)
    exc = erro_de(aplicar_transicao, conv, Estado.AI_TRIAGE, motivo=ruim)
    check(exc is not None and "motivo_invalido" in str(exc), f"C3. motivo {rot} e recusado")

for ruim, rot in ((None, "None"), (True, "bool True"), (False, "bool False"),
                  (-1, "negativo"), ("7", "string"), (7.0, "float"), (object(), "objeto")):
    conv = conversa_em(Estado.NEW, state_version=ruim)
    exc = erro_de(aplicar_transicao, conv, Estado.AI_TRIAGE, motivo="ok")
    check(
        exc is not None and "state_version_invalido" in str(exc),
        f"C4. state_version {rot} e recusado",
    )

conv = conversa_em(Estado.NEW)
exc = erro_de(aplicar_transicao, conv, "WAITING_HUMAN", motivo="ok")
check(
    exc is not None and "destino_invalido" in str(exc),
    "C5. string crua 'WAITING_HUMAN' como destino e recusada (isinstance explicito)",
)

# ---------------------------------------------------------------------------
# D. Mensagens adversariais - PII nunca alcancavel
# ---------------------------------------------------------------------------
_VENENO = "11987654321 joao@cliente.com"
for destino_ruim, rot in ((_VENENO, "texto com telefone e email"), (object(), "objeto"),
                          ("11999999999", "telefone"), ("nome do cliente", "nome")):
    conv = conversa_em(Estado.NEW)
    exc = erro_de(aplicar_transicao, conv, destino_ruim, motivo="ok")
    partes = cadeia_de(exc) if exc else []
    check(exc is not None, f"D1. destino {rot} levanta")
    check(
        exc is not None and "destino_invalido" in str(exc),
        f"D2. destino {rot} usa o token destino_invalido",
    )
    check(
        exc is not None and not any(str(destino_ruim)[:12] in p for p in partes),
        f"D3. destino {rot}: valor NAO alcancavel nem pela cadeia de excecao",
    )

conv = Conversa(state=_VENENO)
exc = erro_de(aplicar_transicao, conv, Estado.AI_TRIAGE, motivo="ok")
partes = cadeia_de(exc) if exc else []
check(exc is not None and "estado_atual_invalido" in str(exc), "D4. state corrompido usa o token certo")
check(
    exc is not None and not any(_VENENO[:12] in p for p in partes),
    "D5. state corrompido: valor NAO alcancavel nem pela cadeia (nao ha __context__ a vazar)",
)
check(exc is not None and len(partes) == 1, "D6. a excecao nao encadeia nada (cadeia de 1)")

# `motivo` de FORMA valida, numa transicao que RECUSA por outro motivo: o
# valor nao pode aparecer na mensagem nem na cadeia. NEW -> AI_TRIAGE seria
# aceita, entao nao serve - precisa de um caminho que levante.
for conv, destino, rot in (
    (conversa_em(Estado.NEW), Estado.HUMAN_ACTIVE, "aresta_nao_permitida"),
    (conversa_em(Estado.AI_TRIAGE, is_bot_active=False), Estado.AI_TRIAGE, "invariante_destino"),
    (conversa_em(Estado.NEW, state_version=None), Estado.AI_TRIAGE, "state_version_invalido"),
    (Conversa(state="INEXISTENTE"), Estado.AI_TRIAGE, "estado_atual_invalido"),
):
    exc = erro_de(aplicar_transicao, conv, destino, motivo="segredo_do_cliente_x")
    check(exc is not None, f"D7. {rot}: levanta (pre-condicao do check seguinte)")
    check(
        exc is not None and not any("segredo_do_cliente" in p for p in cadeia_de(exc)),
        f"D7b. {rot}: motivo NAO aparece na mensagem nem na cadeia",
    )

# E quando o proprio `motivo` e o invalido, o valor recusado tampouco vaza.
conv = conversa_em(Estado.NEW)
exc = erro_de(aplicar_transicao, conv, Estado.AI_TRIAGE, motivo="SEGREDO Do Cliente 11987654321")
check(
    exc is not None and not any("11987654321" in p for p in cadeia_de(exc)),
    "D8. motivo_invalido nao ecoa o valor recusado",
)

# ---------------------------------------------------------------------------
# E. Atomicidade
# ---------------------------------------------------------------------------
for rot, conv, destino, motivo in (
    ("aresta invalida", conversa_em(Estado.NEW), Estado.HUMAN_ACTIVE, "ok"),
    ("motivo invalido", conversa_em(Estado.NEW), Estado.AI_TRIAGE, "COM MAIUSCULA"),
    ("invariante de destino", conversa_em(Estado.AI_TRIAGE, is_bot_active=False), Estado.AI_TRIAGE, "ok"),
    ("state_version invalido", conversa_em(Estado.NEW, state_version="x"), Estado.AI_TRIAGE, "ok"),
):
    antes = conv.snapshot_estado()
    erro_de(aplicar_transicao, conv, destino, motivo=motivo)
    check(conv.snapshot_estado() == antes, f"E1. {rot}: state/state_version/closed_at intactos")

# ---------------------------------------------------------------------------
# F. Nao-escrita dos seis campos operacionais
# ---------------------------------------------------------------------------
for origem, destino in sorted(_arestas, key=lambda p: (p[0].value, p[1].value)):
    conv = preparar_para(destino, conversa_em(origem))
    if destino is Estado.CLOSED:
        conv.closed_at = None
    antes = conv.snapshot_operacional()
    erro_de(aplicar_transicao, conv, destino, motivo="teste_naoescrita")
    check(
        conv.snapshot_operacional() == antes,
        f"F1. {origem.value} -> {destino.value} nao toca nenhum dos seis campos operacionais",
    )

# ---------------------------------------------------------------------------
# G. closed_at - relogio por substituicao
# ---------------------------------------------------------------------------
_FIXO = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
with mock.patch("app.v2.estados._agora_utc", return_value=_FIXO):
    conv = preparar_para(Estado.CLOSED, conversa_em(Estado.AI_TRIAGE))
    conv.closed_at = None
    erro_de(aplicar_transicao, conv, Estado.CLOSED, motivo="fechamento_humano")
    check(conv.closed_at == _FIXO, "G1. entrar em CLOSED grava o relogio SUBSTITUIDO, sem tolerancia")

    conv2 = preparar_para(Estado.AI_TRIAGE, conversa_em(Estado.CLOSED))
    erro_de(aplicar_transicao, conv2, Estado.AI_TRIAGE, motivo="reabertura")
    check(conv2.closed_at is None, "G2. sair de CLOSED limpa closed_at")

conv = conversa_em(Estado.NEW, closed_at=_AGORA)
exc = erro_de(aplicar_transicao, conv, Estado.AI_TRIAGE, motivo="ok")
check(exc is not None, "G3. aberto -> aberto com closed_at nao-nulo levanta")

conv = conversa_em(Estado.CLOSED, closed_at=None)
exc = erro_de(aplicar_transicao, conv, Estado.AI_TRIAGE, motivo="ok")
check(exc is not None, "G4. CLOSED com closed_at=None nao e estado valido para transicionar")

with mock.patch("app.v2.estados._agora_utc", return_value=_FIXO + timedelta(days=1)):
    conv = preparar_para(Estado.CLOSED, conversa_em(Estado.AI_TRIAGE))
    conv.closed_at = None
    erro_de(aplicar_transicao, conv, Estado.CLOSED, motivo="refechamento")
    check(conv.closed_at == _FIXO + timedelta(days=1), "G5. refechar grava timestamp NOVO")

# ---------------------------------------------------------------------------
# H. Pureza (AST) - allowlist propria da Fase 4
# ---------------------------------------------------------------------------
_FONTE = (CONVERSAS_DIR / "app" / "v2" / "estados.py").read_text(encoding="utf-8")
_ARVORE = ast.parse(_FONTE)
_RAIZES_PERMITIDAS = {"datetime", "enum", "re", "typing", "types", "__future__"}
_PROIBIDOS = (
    "sqlalchemy", "fastapi", "starlette", "httpx", "requests", "os", "pathlib",
    "subprocess", "sqlite3", "psycopg2", "app.database", "app.models",
    "app.services", "app.config", "app.routers", "app.schemas", "app.main",
    "app.v2.eventos",
)
_importados = set()
for no in ast.walk(_ARVORE):  # walk pega import dentro de funcao, if ou try
    if isinstance(no, ast.Import):
        for alias in no.names:
            _importados.add(alias.name)
    elif isinstance(no, ast.ImportFrom):
        _importados.add("." * no.level + (no.module or ""))
check(
    all(n.split(".")[0] in _RAIZES_PERMITIDAS for n in _importados),
    f"H1. so importa da allowlist da decisao 14 (achou {sorted(_importados)})",
)
check(
    not any(n == p or n.startswith(p + ".") for n in _importados for p in _PROIBIDOS),
    "H2. nenhum modulo proibido, inclusive sob if TYPE_CHECKING",
)
check("datetime" in _importados, "H3. datetime e PERMITIDO aqui (a Fase 3 o proibia)")

# ---------------------------------------------------------------------------
# I. Reabertura
# ---------------------------------------------------------------------------
_regras = [
    (False, True, True, Estado.AI_TRIAGE, "1. fora da janela"),
    (False, None, False, Estado.AI_TRIAGE, "1b. fora da janela, sem dono"),
    (True, None, True, Estado.AI_TRIAGE, "2. dentro, sem dono anterior"),
    (True, True, False, Estado.HUMAN_ACTIVE, "3. dentro, dono elegivel"),
    (True, True, True, Estado.HUMAN_ACTIVE, "3b. dentro, dono elegivel (novo dono irrelevante)"),
    (True, False, True, Estado.WAITING_HUMAN, "4. dentro, dono inelegivel, novo dono disponivel"),
    (True, False, False, Estado.AI_TRIAGE, "5. dentro, dono inelegivel, SEM novo dono (reconcilia R5)"),
]
for janela, dono, novo, esperado, rot in _regras:
    obtido = decidir_reabertura(
        dentro_janela=janela, dono_anterior_elegivel=dono, novo_dono_disponivel=novo
    )
    check(obtido is esperado, f"I1. decidir_reabertura {rot} -> {esperado.value} (obteve {obtido})")

_base = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
_casos_janela = [
    (_base - timedelta(hours=1), _base, True, "1/7. fechada ha 1h -> dentro"),
    (_base - timedelta(hours=23, minutes=59), _base, True, "2. ha 23h59 -> dentro"),
    (_base - timedelta(hours=24), _base, False, "5. exatamente 24h -> FORA (< estrito)"),
    (_base - timedelta(hours=25), _base, False, "6. mais de 24h -> fora"),
    (_base + timedelta(hours=1), _base, False, "7. closed_at FUTURO -> fora (limite esquerdo)"),
    (None, _base, False, "8. closed_at None -> fora"),
]
for closed, agora, esperado, rot in _casos_janela:
    check(
        dentro_janela_reabertura(closed, agora=agora) is esperado,
        f"I2. janela aware {rot}",
    )
    naive = closed.replace(tzinfo=None) if closed else None
    check(
        dentro_janela_reabertura(naive, agora=agora.replace(tzinfo=None)) is esperado,
        f"I3. janela naive (interpretado como UTC) {rot}",
    )

for janela, rot in ((timedelta(0), "zero"), (timedelta(hours=-1), "negativa")):
    check(
        dentro_janela_reabertura(_base - timedelta(minutes=1), agora=_base, janela=janela) is False,
        f"I4. janela {rot} devolve False, sem levantar",
    )
check(JANELA_REABERTURA_PADRAO == timedelta(hours=24), "I5. JANELA_REABERTURA_PADRAO e 24h")

# ---------------------------------------------------------------------------
# J. Model e CHECK, contra SQLite descartavel
# ---------------------------------------------------------------------------
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.sql.elements import TextClause  # noqa: E402
import sqlalchemy.exc  # noqa: E402

import app.models.media_asset  # noqa: E402,F401 - registra mappers
import app.models.note  # noqa: E402,F401
import app.models.tag  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402

_col = Conversation.__table__.c
check(str(_col["state"].type) == "VARCHAR(32)", "J1. state e VARCHAR(32)")
check(str(_col["state_version"].type) == "INTEGER", "J2. state_version e INTEGER")
check(_col["closed_at"].type.timezone is True, "J3. closed_at e DateTime(timezone=True)")
check(all(_col[c].nullable for c in ("state", "state_version", "closed_at")), "J4. as tres sao nullable")
check(
    isinstance(_col["state_version"].server_default.arg, TextClause),
    "J5. server_default de state_version e TextClause, NAO str (protege DEFAULT 0 vs '0')",
)
check(
    isinstance(_col["state"].server_default.arg, str),
    "J6. server_default de state e str ('LEGACY') - correto para coluna String",
)
check(
    any(getattr(x, "name", "") == "uq_conversations_whatsapp" for x in Conversation.__table_args__),
    "J7. __table_args__ preserva o Index unico de whatsapp",
)

_db = SCRATCH / "v2_estados_check.db"
if _db.exists():
    _db.unlink()
_eng = create_engine(f"sqlite:///{_db.as_posix()}")
Base.metadata.create_all(_eng, tables=[Conversation.__table__])

_INSERT = (
    "INSERT INTO conversations (id, lead_id, whatsapp, status, unread_count, "
    "is_bot_active, atendente_id, responsavel_id, queued_at, state) VALUES "
    "(:id, 1, :wa, 'aberta', 0, :bot, :at, :resp, :q, :st)"
)


def _insere(**kw):
    with _eng.begin() as conn:
        conn.execute(text(_INSERT), kw)


def _aceita(rot, **kw):
    try:
        _insere(**kw)
        return True, None
    except sqlalchemy.exc.IntegrityError as exc:
        return False, str(exc)


ok, _ = _aceita("legacy", id=1, wa="1", bot=1, at=1, resp=2, q=None, st="LEGACY")
check(ok, "J8. CHECK INERTE para LEGACY (at != resp, bot ON, queued_at NULL)")
ok, _ = _aceita("null", id=2, wa="2", bot=1, at=1, resp=2, q=None, st=None)
check(ok, "J9. CHECK INERTE para state NULL - storage tolera")
_ids_unicos = itertools.count(1000)  # contador deterministico - sem colisao de PK entre execucoes

for st in ("NEW", "AI_TRIAGE", "CLOSED"):
    ok, _ = _aceita(st, id=next(_ids_unicos), wa=st, bot=1, at=1, resp=2, q=None, st=st)
    check(ok, f"J10. CHECK inerte para {st}")

for st in ("WAITING_HUMAN", "HUMAN_ACTIVE"):
    ok, _ = _aceita(st, id=next(_ids_unicos), wa=st + "a", bot=1, at=7, resp=7, q=_AGORA, st=st)
    check(not ok, f"J11. {st} com bot LIGADO e REJEITADO")
    ok, _ = _aceita(st, id=next(_ids_unicos), wa=st + "b", bot=0, at=None, resp=7, q=_AGORA, st=st)
    check(not ok, f"J12. {st} com atendente NULL e REJEITADO")
    ok, _ = _aceita(st, id=next(_ids_unicos), wa=st + "c", bot=0, at=7, resp=None, q=_AGORA, st=st)
    check(not ok, f"J13. {st} com responsavel NULL e REJEITADO")
    ok, _ = _aceita(st, id=next(_ids_unicos), wa=st + "d", bot=0, at=7, resp=9, q=_AGORA, st=st)
    check(not ok, f"J14. {st} com atendente != responsavel e REJEITADO")
    ok, _ = _aceita(st, id=next(_ids_unicos), wa=st + "e", bot=0, at=7, resp=7, q=None, st=st)
    check(not ok, f"J15. {st} com queued_at NULL e REJEITADO")
    ok, _ = _aceita(st, id=next(_ids_unicos), wa=st + "f", bot=0, at=7, resp=7, q=_AGORA, st=st)
    check(ok, f"J16. {st} com as cinco condicoes satisfeitas e ACEITO")

# A maquina recusa a linha que o storage aceitou em J9 - as duas propriedades
# sao diferentes, e a decisao 10 exige que nao sejam confundidas.
exc = erro_de(aplicar_transicao, Conversa(state=None), Estado.AI_TRIAGE, motivo="ok")
check(
    exc is not None and "estado_atual_invalido" in str(exc),
    "J17. banco ACEITA state NULL, mas a MAQUINA recusa (decisao 10, fail closed)",
)

print()
print(f"{total} verificacoes executadas.")
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
