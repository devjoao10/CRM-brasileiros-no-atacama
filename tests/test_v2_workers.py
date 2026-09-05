"""
BIA-V2 R15/R16 - os dois workers da fila de `handoff_operations`.

Cobre `conversas/app/v2/worker_fila.py` (particao + reclamacao),
`conversas/app/v2/worker_retomada.py` (R16) e
`conversas/app/v2/worker_reconciliacao.py` (R15).

Fonte normativa: `bia-v2-r15-r17-design-v4.md`, secoes 4, 4.2, 5 e 6.2.

NENHUMA CHAMADA DE REDE REAL. O CRM e substituido por `_CRMFalso` (mesmo
padrao de `tests/test_v2_handoff.py`: troca so o ATRIBUTO `httpx` do modulo sob
teste). `crm.get_leads_responsaveis` tambem e substituido - o SQLite de teste
nao tem as tabelas `leads`/`users` do CRM, e o que importa nao e a query e sim
o VALOR que o CRM reporta como dono do lead.

GRUPOS COBERTOS (um teste real por item, nunca prosa):
  1  PARTICAO (4.2): as fatias de R16 e R15 sao disjuntas contra um universo
     construido de proposito - inclusive as combinacoes HOSTIS, em que uma
     linha exclusiva de R15 carrega uma `error_class` que estaria na lista de
     R16 (prova que a exclusao por CODIGO e estrutural, nao acidental), e a
     linha de `error_code` NULL, que a logica de tres valores do SQL deixaria
     ORFA se a fatia de R16 nao tratasse o NULL explicitamente
  2  janela (`next_attempt_at`) e teto (`attempt_count`) na ELEGIBILIDADE; a
     reclamacao adia mas NAO incrementa; e o lote inteiro cabe na janela de
     exclusao (`LOTE * pior caso por chamada <= BACKOFF`), sem estourar o teto
     compartilhado de 200/min mesmo com varias instancias
  3  exclusao entre instancias: (a) prova ESTRUTURAL, sempre roda, de que o
     ramo PostgreSQL emite `FOR UPDATE SKIP LOCKED`; (b) duas threads reais
     nao processam a mesma linha; (c) prova REAL de `SKIP LOCKED` contra
     PostgreSQL, opt-in
  4  `attempt_count` so anda no DESFECHO, e NUNCA para 429; dead-letter depois
     de 5 tentativas REAIS, com um 429 no meio provando que ele nao conta
  5  R16 retoma uma linha presa em `REQUESTED` com a MESMA identidade
  6  R15 Direcao A converge o Conversas para o dono que o CRM ja tem
  7  R15 Direcao B corrige o CRM para o atendente local - inclusive o caso em
     que `handoff_to_human` reporta FALHA (estado local recusado) e a
     divergencia esta, ainda assim, RESOLVIDA
  8  a comparacao `current_responsavel_id` x `atendente_id` decide certo nos
     DOIS sentidos (achado HIGH: 409 nao significa "resolvido" sozinho)
  9  nenhum dos tres modulos importa ou menciona `sync_responsavel_to_crm`
 10  `_registrar_falha` persiste `error_class` junto de `error_code`
 11  `attempt_count` conta ENTREGA JULGADA, nem mais nem menos: um veredito de
     `handoff_to_human` (ValueError e subclasses) consome tentativa e leva a
     linha ruim ao dead-letter; um crash generico e um early-return que nunca
     chamou o CRM nao consomem nenhuma

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_v2_workers.py
"""
import ast
import logging
import os
import pathlib
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / f"v2_workers_test_{uuid.uuid4().hex}.db"

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
os.environ["CRM_BASE_URL"] = "http://crm-de-teste.invalido"
os.environ["BIA_V2_CRM_API_KEY"] = "chave-de-teste"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


import httpx  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

import app.models.media_asset  # noqa: E402,F401
import app.models.note  # noqa: E402,F401
import app.models.tag  # noqa: E402,F401
import app.v2.handoff as handoff_mod  # noqa: E402
import app.v2.worker_fila as fila_mod  # noqa: E402
import app.v2.worker_reconciliacao as r15_mod  # noqa: E402
import app.v2.worker_retomada as r16_mod  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402
from app.models.handoff_op import HandoffOperation, StatusHandoff  # noqa: E402
from app.v2.handoff import ClasseErroCrm, TokenErroCrm, handoff_to_human  # noqa: E402

Base.metadata.create_all(
    bind=engine,
    tables=[
        Conversation.__table__, ConversationEvent.__table__, HandoffOperation.__table__,
        Message.__table__,
    ],
)


# ---------------------------------------------------------------------------
# CRM falso - idempotente por event_id, igual ao contrato HTTP.
# ---------------------------------------------------------------------------
class _RespostaFalsa:
    def __init__(self, status_code, corpo=None):
        self.status_code = status_code
        self._corpo = corpo

    def json(self):
        return self._corpo


class _CRMFalso:
    def __init__(self):
        self.chamadas = []
        self.vistos = set()
        self.roteiro = []
        # `donos[lead_id]` e o que este CRM falso reporta como
        # `leads.responsavel_id` - lido tanto pelo baseline do CAS quanto pelo
        # worker de reconciliacao.
        self.donos = {}

    def programar(self, *itens):
        self.roteiro.extend(itens)

    def limpar(self):
        self.chamadas = []
        self.roteiro = []

    def post(self, url, json=None, headers=None):
        self.chamadas.append({"url": url, "corpo": json})
        if self.roteiro:
            item = self.roteiro.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        replayed = json["event_id"] in self.vistos
        self.vistos.add(json["event_id"])
        if json.get("lead_id"):
            self.donos[json["lead_id"]] = json["target_user_id"]
        return _RespostaFalsa(200, {**json, "replayed": replayed})


CRM = _CRMFalso()


class _ClientFalso:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        return CRM.post(*args, **kwargs)


handoff_mod.httpx = SimpleNamespace(
    Client=_ClientFalso,
    RequestError=httpx.RequestError,
    ConnectError=httpx.ConnectError,
    ConnectTimeout=httpx.ConnectTimeout,
    PoolTimeout=httpx.PoolTimeout,
    Timeout=httpx.Timeout,
)


def _leads_responsaveis_falso(lead_ids, db):
    """Substitui a query em `leads`/`users`, que nao existem no SQLite de teste."""
    return {
        lead_id: {"responsavel_id": CRM.donos.get(lead_id), "responsavel_nome": None}
        for lead_id in lead_ids
        if lead_id in CRM.donos
    }


# Os DOIS pontos que leem o dono do lead: o baseline do CAS (`handoff.py`) e a
# confirmacao de convergencia (`worker_reconciliacao.py`).
handoff_mod.get_leads_responsaveis = _leads_responsaveis_falso
r15_mod.get_leads_responsaveis = _leads_responsaveis_falso


# ---------------------------------------------------------------------------
# Captura de log - os "alertas" dos dois workers sao linhas de log por nivel.
# ---------------------------------------------------------------------------
class _Coletor(logging.Handler):
    def __init__(self):
        super().__init__()
        self.registros = []

    def emit(self, record):
        self.registros.append(record)

    def limpar(self):
        self.registros = []

    def textos(self, nivel=None):
        return [
            r.getMessage() for r in self.registros if nivel is None or r.levelno == nivel
        ]


COLETOR = _Coletor()
_logger_v2 = logging.getLogger("app.v2")
_logger_v2.setLevel(logging.DEBUG)
_logger_v2.addHandler(COLETOR)


db = SessionLocal()
_contador = iter(range(1, 100000))
MOTIVO = "handoff_bia"


def criar_conversa(*, state="AI_TRIAGE", is_bot_active=True, atendente_id=None,
                   responsavel_id=None, queued_at=None,
                   primeira_resposta_humana_at=None):
    n = next(_contador)
    conv = Conversation(
        lead_id=9000 + n,
        whatsapp=f"5511900{n:06d}",
        status="aberta",
        unread_count=0,
        is_bot_active=is_bot_active,
        atendente_id=atendente_id,
        responsavel_id=responsavel_id,
        queued_at=queued_at,
        primeira_resposta_humana_at=primeira_resposta_humana_at,
        last_customer_msg_at=datetime.now(timezone.utc),
        state=state,
        state_version=0,
    )
    db.add(conv)
    db.commit()
    return conv.id, conv.lead_id


def criar_operacao(*, status, error_code=None, error_class=None, attempt_count=0,
                   next_attempt_at=None, reconciled_at=None, conversation_id=1,
                   target_user_id=7, event_id=None):
    op = HandoffOperation(
        event_id=event_id or str(uuid.uuid4()),
        conversation_id=conversation_id,
        target_user_id=target_user_id,
        status=status,
        error_code=error_code,
        error_class=error_class,
        attempt_count=attempt_count,
        next_attempt_at=next_attempt_at,
        reconciled_at=reconciled_at,
    )
    db.add(op)
    db.commit()
    return op.id


def ler_operacao(operacao_id):
    """Le por uma SESSAO NOVA: so enxerga o que foi COMITADO."""
    sessao = SessionLocal()
    try:
        op = sessao.get(HandoffOperation, operacao_id)
        if op is None:
            return None
        return SimpleNamespace(
            id=op.id, event_id=op.event_id, status=op.status, error_code=op.error_code,
            error_class=op.error_class, attempt_count=op.attempt_count,
            next_attempt_at=op.next_attempt_at, reconciled_at=op.reconciled_at,
            target_user_id=op.target_user_id, conversation_id=op.conversation_id,
        )
    finally:
        sessao.close()


def ler_conversa(cid):
    sessao = SessionLocal()
    try:
        c = sessao.get(Conversation, cid)
        return SimpleNamespace(
            state=c.state, atendente_id=c.atendente_id, responsavel_id=c.responsavel_id,
            is_bot_active=c.is_bot_active,
        )
    finally:
        sessao.close()


def ler_operacao_por_event_id(event_id):
    sessao = SessionLocal()
    try:
        op = sessao.query(HandoffOperation).filter_by(event_id=event_id).one_or_none()
        if op is None:
            return None
        return SimpleNamespace(
            id=op.id, status=op.status, target_user_id=op.target_user_id,
            error_code=op.error_code,
        )
    finally:
        sessao.close()


def liberar_janela(operacao_id):
    """Simula a passagem do tempo: devolve a linha a fila imediatamente."""
    sessao = SessionLocal()
    try:
        sessao.query(HandoffOperation).filter_by(id=operacao_id).update(
            {"next_attempt_at": None}, synchronize_session=False
        )
        sessao.commit()
    finally:
        sessao.close()


def limpar_fila():
    """Zera a fila entre grupos - cada grupo monta o universo que precisa."""
    sessao = SessionLocal()
    try:
        sessao.query(HandoffOperation).delete()
        sessao.commit()
    finally:
        sessao.close()


PASSADO = datetime.now(timezone.utc) - timedelta(hours=1)
FUTURO = datetime.now(timezone.utc) + timedelta(hours=1)
_TC = TokenErroCrm
_CE = ClasseErroCrm


print("\n--- GRUPO 1: particao entre R15 e R16 (secao 4.2) ---")
limpar_fila()

id_requested = criar_operacao(status=StatusHandoff.REQUESTED.value)
id_retriavel = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code=_TC.INDISPONIVEL.value,
    error_class=_CE.NAO_APLICADO_RETRIAVEL.value,
)
id_ambiguo = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code=_TC.HTTP_ERRO.value,
    error_class=_CE.AMBIGUO_RETRIAVEL.value,
)
id_terminal = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code=_TC.NAO_AUTORIZADO.value,
    error_class=_CE.NAO_APLICADO_TERMINAL.value,
)
id_completed = criar_operacao(status=StatusHandoff.COMPLETED.value)
id_ja_atribuido = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code=_TC.LEAD_JA_ATRIBUIDO.value,
    error_class=_CE.NAO_APLICADO_TERMINAL.value,
)
id_estado_local = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    error_class=None,
)
# COMBINACOES HOSTIS: linha exclusiva de R15 carregando uma classe que ESTA na
# lista de R16. Se a exclusao por codigo nao fosse estrutural, R16 as pegaria.
id_hostil_a = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code=_TC.LEAD_JA_ATRIBUIDO.value,
    error_class=_CE.AMBIGUO_RETRIAVEL.value,
)
id_hostil_b = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    error_class=_CE.NAO_APLICADO_RETRIAVEL.value,
)
# TERCEIRA combinacao hostil, e a mais silenciosa: `error_code IS NULL` com uma
# classe retriavel. Em SQL de tres valores, `NULL NOT IN (...)` NAO e TRUE - e
# NULL, que o WHERE descarta. Sem o `IS NULL` explicito em `criterios_r16()`,
# esta linha nao seria de NINGUEM: R16 a rejeitaria pelo NOT IN e R15 pelo IN.
# Nao e alcancavel pelo codigo de hoje (`_registrar_falha` sempre grava codigo
# junto de classe), mas a particao da secao 4.2 e uma afirmacao sobre toda
# linha possivel da tabela, e uma fatia com buraco nao e uma particao.
id_sem_codigo = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code=None,
    error_class=_CE.NAO_APLICADO_RETRIAVEL.value,
)

sessao_r16 = SessionLocal()
fatia_r16 = set(fila_mod.reclamar(
    sessao_r16, criterios=fila_mod.criterios_r16(), limite=100
))
sessao_r16.close()

sessao_r15 = SessionLocal()
fatia_r15 = set(fila_mod.reclamar(
    sessao_r15, criterios=fila_mod.criterios_r15(), limite=100
))
sessao_r15.close()

_ESPERADO_R16 = {id_requested, id_retriavel, id_ambiguo, id_sem_codigo}
check(
    fatia_r16 == _ESPERADO_R16,
    f"1a. R16 pega REQUESTED + as duas classes retriaveis, e SO elas "
    f"(esperado {sorted(_ESPERADO_R16)}, obteve {sorted(fatia_r16)})",
)
check(
    fatia_r15 == {id_ja_atribuido, id_estado_local, id_hostil_a, id_hostil_b},
    f"1b. R15 pega exatamente as linhas dos dois codigos exclusivos "
    f"(esperado {sorted({id_ja_atribuido, id_estado_local, id_hostil_a, id_hostil_b})}, "
    f"obteve {sorted(fatia_r15)})",
)
check(not (fatia_r16 & fatia_r15), f"1c. as duas fatias sao DISJUNTAS (interseccao {fatia_r16 & fatia_r15})")
check(
    id_hostil_a not in fatia_r16 and id_hostil_b not in fatia_r16,
    "1d. linha exclusiva de R15 com error_class da lista de R16 continua fora de R16 "
    "- a exclusao por error_code e estrutural, nao acidental",
)
check(
    id_terminal not in fatia_r16 and id_terminal not in fatia_r15,
    "1e. FAILED de classe terminal nao e de ninguem: nenhum retry mecanico o corrige",
)
check(
    id_completed not in fatia_r16 and id_completed not in fatia_r15,
    "1f. COMPLETED nunca e reclamado por nenhum dos dois",
)
check(
    id_sem_codigo in fatia_r16,
    f"1h. linha com error_code NULL e error_class RETRIAVEL e de R16 - a fatia trata "
    f"NULL explicitamente, senao a logica de tres valores do SQL a deixaria orfa "
    f"(fora das DUAS fatias) [id={id_sem_codigo}, em R16: "
    f"{id_sem_codigo in fatia_r16}, em R15: {id_sem_codigo in fatia_r15}]",
)
check(
    id_sem_codigo not in fatia_r15,
    "1i. e ela continua fora de R15 - tratar o NULL nao pode abrir a fatia do outro "
    "worker, senao o buraco vira sobreposicao",
)

limpar_fila()
id_reconciliado = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code=_TC.LEAD_JA_ATRIBUIDO.value,
    reconciled_at=PASSADO,
)
sessao = SessionLocal()
check(
    id_reconciliado not in fila_mod.reclamar(
        sessao, criterios=fila_mod.criterios_r15(), limite=100
    ),
    "1g. linha ja reconciliada sai da fila de R15",
)
sessao.close()


print("\n--- GRUPO 2: janela (next_attempt_at) e teto (attempt_count) ---")
limpar_fila()

id_futuro = criar_operacao(status=StatusHandoff.REQUESTED.value, next_attempt_at=FUTURO)
id_passado = criar_operacao(status=StatusHandoff.REQUESTED.value, next_attempt_at=PASSADO)
id_nunca = criar_operacao(status=StatusHandoff.REQUESTED.value, next_attempt_at=None)
id_no_teto = criar_operacao(
    status=StatusHandoff.REQUESTED.value, attempt_count=fila_mod.TETO_TENTATIVAS
)

sessao = SessionLocal()
reclamadas = fila_mod.reclamar(sessao, criterios=fila_mod.criterios_r16(), limite=100)
sessao.close()

check(id_futuro not in reclamadas, "2a. next_attempt_at no FUTURO nao e reclamada")
check(id_passado in reclamadas, "2b. next_attempt_at no PASSADO e reclamada")
check(id_nunca in reclamadas, "2c. next_attempt_at NULL (nunca agendada) e reclamada")
check(
    id_no_teto not in reclamadas,
    f"2d. linha no teto ({fila_mod.TETO_TENTATIVAS} tentativas) sai da fila - o "
    f"dead-letter vive no PREDICADO, nao num status novo",
)

apos = ler_operacao(id_nunca)
check(
    apos.attempt_count == 0,
    f"2e. a reclamacao NAO incrementa attempt_count (achado HIGH da secao 5: um crash "
    f"entre reclamar e chamar a rede nao pode queimar orcamento) [obteve {apos.attempt_count}]",
)
check(
    apos.next_attempt_at is not None,
    "2f. a reclamacao ADIA next_attempt_at (espacamento + exclusao entre workers)",
)
sessao = SessionLocal()
check(
    id_nunca not in fila_mod.reclamar(sessao, criterios=fila_mod.criterios_r16(), limite=100),
    "2g. depois de reclamada, a linha nao e reclamada de novo no mesmo instante",
)
sessao.close()

# O adiamento acima e gravado para o LOTE INTEIRO de uma vez, na reclamacao; a
# partir dai o worker processa as linhas UMA A UMA. Se o lote demorar mais que
# o backoff, as ultimas linhas voltam a ficar elegiveis ANTES de a primeira
# instancia chegar nelas - e uma segunda instancia liga para o CRM em paralelo,
# sobre a mesma linha. A janela so exclui de verdade enquanto couber o pior
# caso do lote, entao a relacao entre as tres constantes e uma INVARIANTE, nao
# uma coincidencia de valores bem escolhidos hoje.
_PIOR_LOTE_R16 = fila_mod.LOTE_R16 * fila_mod.PIOR_CASO_POR_CHAMADA_SEGUNDOS
_PIOR_LOTE_R15 = fila_mod.LOTE_R15 * fila_mod.PIOR_CASO_POR_CHAMADA_SEGUNDOS
check(
    _PIOR_LOTE_R16 <= fila_mod.BACKOFF_SEGUNDOS
    and _PIOR_LOTE_R15 <= fila_mod.BACKOFF_SEGUNDOS,
    f"2h. o pior caso de um lote INTEIRO cabe na janela de exclusao "
    f"(R16 {fila_mod.LOTE_R16}x{fila_mod.PIOR_CASO_POR_CHAMADA_SEGUNDOS}s={_PIOR_LOTE_R16}s, "
    f"R15 {fila_mod.LOTE_R15}x{fila_mod.PIOR_CASO_POR_CHAMADA_SEGUNDOS}s={_PIOR_LOTE_R15}s, "
    f"backoff={fila_mod.BACKOFF_SEGUNDOS}s) - senao uma segunda instancia reclama "
    f"uma linha que a primeira ainda esta processando",
)
check(
    fila_mod.PIOR_CASO_POR_CHAMADA_SEGUNDOS
    == (handoff_mod._TIMEOUT_CRM.connect or 0.0) + (handoff_mod._TIMEOUT_CRM.read or 0.0),
    f"2i. e o 'pior caso' sai do timeout REAL do cliente HTTP, nao de um numero "
    f"recopiado que pode divergir dele [{fila_mod.PIOR_CASO_POR_CHAMADA_SEGUNDOS}s]",
)
# O outro teto, o do CRM: `200/minute` por IP, GLOBAL e compartilhado com todo o
# trafego Conversas->CRM. Rodar varias instancias e o modo PRETENDIDO de escalar
# (SKIP LOCKED), entao a conta que importa e a de K instancias, nao a de uma.
_REQ_POR_MIN_UM_PAR = (60 / fila_mod.CICLO_SEGUNDOS) * (fila_mod.LOTE_R16 + fila_mod.LOTE_R15)
check(
    _REQ_POR_MIN_UM_PAR * 6 <= 100,
    f"2j. seis pares de instancias ainda ficam em menos de metade do teto de 200/min "
    f"({_REQ_POR_MIN_UM_PAR} req/min por par, {_REQ_POR_MIN_UM_PAR * 6} req/min com 6) - "
    f"a aritmetica da secao 5 vale para a fila INTEIRA, nao para uma instancia so",
)


print("\n--- GRUPO 3: exclusao entre instancias ---")
limpar_fila()

_IS_SQLITE_ORIGINAL = fila_mod.IS_SQLITE
sessao = SessionLocal()
try:
    fila_mod.IS_SQLITE = False
    _sql_pg = str(
        fila_mod.consulta_de_reclamacao(
            sessao, criterios=fila_mod.criterios_r16(), limite=10
        ).statement.compile(dialect=postgresql.dialect())
    )
finally:
    fila_mod.IS_SQLITE = _IS_SQLITE_ORIGINAL
    sessao.close()

check(
    "FOR UPDATE SKIP LOCKED" in _sql_pg,
    "3a. (ESTRUTURAL, sempre roda) o ramo PostgreSQL da reclamacao emite "
    "FOR UPDATE SKIP LOCKED - unica forma de provar isso num CI SQLite, que "
    "descarta a clausula silenciosamente no proprio compilador",
)
check(
    "clock_timestamp()" in _sql_pg,
    "3b. a janela compara contra o relogio do BANCO (clock_timestamp), nunca "
    "datetime.now() do processo - fecha R-E da v3",
)
# 3b cobre `agora_do_banco()` (o LADO ESQUERDO da comparacao, no WHERE). Quem
# ESCREVE o proximo agendamento e outra funcao — `instante_futuro()` — e as duas
# sao independentes: provar uma nao prova a outra. Um `datetime.now()` do
# processo aqui reintroduz o R-E pela metade que ninguem estava olhando (o
# relogio do worker adiantado/atrasado em relacao ao do banco desloca TODA a
# fila), e nenhum check pegava isso.
_IS_SQLITE_ORIGINAL_3 = fila_mod.IS_SQLITE
try:
    fila_mod.IS_SQLITE = False
    _sql_backoff = str(fila_mod.instante_futuro(30))
finally:
    fila_mod.IS_SQLITE = _IS_SQLITE_ORIGINAL_3
check(
    "clock_timestamp()" in _sql_backoff,
    f"3b2. o BACKOFF gravado em next_attempt_at tambem sai do relogio do BANCO, "
    f"nao do processo [{_sql_backoff}]",
)

ids_universo = [criar_operacao(status=StatusHandoff.REQUESTED.value) for _ in range(6)]
_resultados = {}
_barreira = threading.Barrier(2)


def _instancia(nome):
    sessao_local = SessionLocal()
    try:
        _barreira.wait(timeout=10)
        # `limite=3` contra um universo de 6: forca as DUAS instancias a
        # trabalharem. Com um limite maior, a que chegasse primeiro levaria
        # tudo e a disjuncao seria trivialmente verdadeira.
        _resultados[nome] = set(
            fila_mod.reclamar(sessao_local, criterios=fila_mod.criterios_r16(), limite=3)
        )
    finally:
        sessao_local.close()


_threads = [threading.Thread(target=_instancia, args=(n,)) for n in ("A", "B")]
for t in _threads:
    t.start()
for t in _threads:
    t.join(timeout=30)

_a, _b = _resultados.get("A", set()), _resultados.get("B", set())
check(
    not (_a & _b),
    f"3c. duas instancias concorrentes NAO reclamam a mesma linha (A={sorted(_a)}, "
    f"B={sorted(_b)}, interseccao={sorted(_a & _b)})",
)
check(
    _a | _b == set(ids_universo),
    f"3d. juntas, as duas instancias cobrem o universo inteiro - a exclusao nao "
    f"PERDE linha nenhuma (faltando: {sorted(set(ids_universo) - (_a | _b))})",
)

_PROOF_URL = os.getenv("DATABASE_URL_WORKERS_PROOF", "").strip()
if not _PROOF_URL:
    print(
        "SKIP 3e. prova REAL de SKIP LOCKED contra PostgreSQL - opcional, opt-in por\n"
        "     DATABASE_URL_WORKERS_PROOF. O check 3a acima e o guarda que roda SEMPRE\n"
        "     no CI (SQLite-only) e ja falha se a clausula sumir do codigo. Para rodar\n"
        "     de verdade:\n"
        "       docker run -d --name bia_workers_proof -p 55433:5432 \\\n"
        "         -e POSTGRES_USER=proof -e POSTGRES_PASSWORD=proof \\\n"
        "         -e POSTGRES_DB=workersproof postgres:16-alpine\n"
        "       DATABASE_URL_WORKERS_PROOF=postgresql://proof:proof@localhost:55433/workersproof \\\n"
        "         python tests/test_v2_workers.py"
    )
else:
    from sqlalchemy import create_engine  # noqa: E402
    from sqlalchemy.orm import sessionmaker as _sessionmaker  # noqa: E402

    _pg = create_engine(_PROOF_URL)
    HandoffOperation.__table__.drop(bind=_pg, checkfirst=True)
    HandoffOperation.__table__.create(bind=_pg)
    _Sessao = _sessionmaker(bind=_pg)
    _sa, _sb = _Sessao(), _Sessao()
    try:
        _semear = _Sessao()
        for _ in range(2):
            _semear.add(HandoffOperation(
                event_id=str(uuid.uuid4()), conversation_id=1, target_user_id=7,
                status=StatusHandoff.REQUESTED.value, attempt_count=0,
            ))
        _semear.commit()
        _semear.close()

        fila_mod.IS_SQLITE = False
        # A reclama 1 linha e NAO commita: o lock de linha continua de pe.
        _ids_a = [
            linha[0] for linha in fila_mod.consulta_de_reclamacao(
                _sa, criterios=fila_mod.criterios_r16(), limite=1
            ).all()
        ]
        # B pede 1 linha ao mesmo tempo: com SKIP LOCKED ele PULA a de A em vez
        # de esperar por ela.
        _ids_b = [
            linha[0] for linha in fila_mod.consulta_de_reclamacao(
                _sb, criterios=fila_mod.criterios_r16(), limite=1
            ).all()
        ]
        check(
            len(_ids_a) == 1 and len(_ids_b) == 1 and _ids_a[0] != _ids_b[0],
            f"3e. (PostgreSQL REAL) duas sessoes concorrentes reclamam linhas DIFERENTES "
            f"sob SKIP LOCKED (A={_ids_a}, B={_ids_b})",
        )
    finally:
        fila_mod.IS_SQLITE = _IS_SQLITE_ORIGINAL
        _sa.rollback()
        _sb.rollback()
        _sa.close()
        _sb.close()
        _pg.dispose()


print("\n--- GRUPO 4: attempt_count no desfecho, 429 e dead-letter (secao 5) ---")
limpar_fila()
CRM.limpar()
COLETOR.limpar()

cid, lead_id = criar_conversa()
ev = str(uuid.uuid4())
op_id = criar_operacao(
    status=StatusHandoff.REQUESTED.value, conversation_id=cid, target_user_id=7, event_id=ev
)

CRM.programar(_RespostaFalsa(500, {"detail": {"error": "HANDOFF_NAO_APLICADO"}}))
sessao = SessionLocal()
r16_mod.processar_ciclo(sessao)
sessao.close()
apos = ler_operacao(op_id)
check(
    apos.attempt_count == 1 and apos.status == StatusHandoff.FAILED.value,
    f"4a. uma tentativa REAL (500) incrementa attempt_count no DESFECHO "
    f"[attempt_count={apos.attempt_count} status={apos.status}]",
)
check(
    apos.error_class == _CE.AMBIGUO_RETRIAVEL.value,
    f"4b. a classe do desfecho e persistida e e o que mantem a linha em R16 "
    f"[error_class={apos.error_class}]",
)
check(
    any("PAGINA" in t for t in COLETOR.textos(logging.CRITICAL)),
    "4c. classe AMBIGUO_RETRIAVEL gera alerta de MAIOR severidade (pagina)",
)

liberar_janela(op_id)
COLETOR.limpar()
CRM.programar(_RespostaFalsa(429, {"error": "Rate limit exceeded: 200 per 1 minute"}))
sessao = SessionLocal()
r16_mod.processar_ciclo(sessao)
sessao.close()
apos_429 = ler_operacao(op_id)
check(
    apos_429.attempt_count == 1 and apos_429.error_code == _TC.LIMITE_TAXA.value,
    f"4d. 429/CRM_LIMITE_TAXA NAO conta como tentativa julgada - attempt_count fica "
    f"em 1 [obteve {apos_429.attempt_count}, error_code={apos_429.error_code}]",
)

for _rodada in range(4):
    liberar_janela(op_id)
    COLETOR.limpar()
    CRM.programar(_RespostaFalsa(500, {"detail": {"error": "HANDOFF_NAO_APLICADO"}}))
    sessao = SessionLocal()
    r16_mod.processar_ciclo(sessao)
    sessao.close()

final = ler_operacao(op_id)
check(
    final.attempt_count == fila_mod.TETO_TENTATIVAS,
    f"4e. dead-letter depois de exatamente {fila_mod.TETO_TENTATIVAS} tentativas REAIS "
    f"- o 429 do meio nao consumiu orcamento [attempt_count={final.attempt_count}]",
)
check(
    final.next_attempt_at is None,
    "4f. a linha em dead-letter para de ser reagendada (next_attempt_at zerado)",
)
check(
    any("DEAD-LETTER" in t for t in COLETOR.textos(logging.ERROR)),
    "4g. dead-letter emite alerta de TICKET (ERROR), nao pagina",
)
sessao = SessionLocal()
check(
    op_id not in fila_mod.reclamar(sessao, criterios=fila_mod.criterios_r16(), limite=100),
    "4h. a linha em dead-letter nao volta a ser reclamada",
)
sessao.close()
check(
    len(CRM.chamadas) == 6 and all(c["corpo"]["event_id"] == ev for c in CRM.chamadas),
    f"4i. TODAS as {len(CRM.chamadas)} reapresentacoes usaram o MESMO event_id - a "
    f"retomada apos um desfecho ambiguo nunca cunha identidade nova, que e o que "
    f"torna o retry seguro contra um CRM que talvez ja tenha aplicado",
)


print("\n--- GRUPO 4b: 429 puro nunca vira dead-letter (secao 5, defesa em profundidade) ---")
# O check 4d prova que UM 429 no meio de falhas reais nao consome orcamento. O
# cenario que o desenho descreve, e que faltava, e o OUTRO: CRM voltando de
# indisponibilidade, fila cheia, e o worker gerando por conta propria varios
# ciclos de 429 consecutivos contra o teto global compartilhado (200/min/IP).
# Se 429 contasse, uma operacao iria para dead-letter sem NUNCA ter sido julgada
# pela logica de negocio - o `SlowAPIMiddleware` recusa ACIMA do router.
limpar_fila()
CRM.limpar()
COLETOR.limpar()

cid4b, _ = criar_conversa()
ev4b = str(uuid.uuid4())
op4b = criar_operacao(
    status=StatusHandoff.REQUESTED.value, conversation_id=cid4b, target_user_id=7, event_id=ev4b
)

_CICLOS_429 = fila_mod.TETO_TENTATIVAS + 2
for _rodada in range(_CICLOS_429):
    liberar_janela(op4b)
    CRM.programar(_RespostaFalsa(429, {"error": "Rate limit exceeded: 200 per 1 minute"}))
    sessao = SessionLocal()
    r16_mod.processar_ciclo(sessao)
    sessao.close()

apos4b = ler_operacao(op4b)
check(
    apos4b.attempt_count == 0,
    f"4j. {_CICLOS_429} ciclos consecutivos de 429 NAO consomem uma unica tentativa "
    f"[attempt_count={apos4b.attempt_count}, esperado 0]",
)
# Dead-letter nao e um status: e `next_attempt_at = NULL` (para de reagendar)
# somado a `attempt_count >= TETO` (o predicado que segura a linha fora da
# fila). As duas metades precisam continuar falsas aqui.
check(
    apos4b.next_attempt_at is not None and apos4b.attempt_count < fila_mod.TETO_TENTATIVAS,
    f"4k. a linha continua REAGENDADA e abaixo do teto mesmo depois de "
    f"{_CICLOS_429} rate limits [next_attempt_at={apos4b.next_attempt_at is not None}, "
    f"attempt_count={apos4b.attempt_count}/{fila_mod.TETO_TENTATIVAS}]",
)
liberar_janela(op4b)
sessao = SessionLocal()
check(
    op4b in fila_mod.reclamar(sessao, criterios=fila_mod.criterios_r16(), limite=100),
    "4l. e continua elegivel para R16 - um 429 e estruturalmente igual a nunca ter "
    "sido tentada, so descoberto uma camada mais funda",
)
sessao.close()


print("\n--- GRUPO 4c: crash depois de um desfecho AMBIGUO retoma sem queimar orcamento ---")
# A composicao que faltava. O check 2e prova o crash pre-rede isoladamente, com
# a linha limpa; 4a prova o desfecho ambiguo isoladamente. O caso real e a
# SEQUENCIA: uma tentativa ambigua ja gastou orcamento (o CRM pode ter
# aplicado), e e justamente ai que um crash loop e mais perigoso — se ele
# tambem contasse, a operacao chegaria ao teto sem nenhuma tentativa nova ter
# sido julgada, e seria abandonada exatamente no estado em que ninguem sabe se
# aplicou. A identidade tem de sobreviver as tres rodadas: um `event_id` novo
# depois de uma ambiguidade transformaria um possivel replay numa SEGUNDA
# aplicacao.
limpar_fila()
CRM.limpar()
COLETOR.limpar()

cid4c, lead4c = criar_conversa()
ev4c = str(uuid.uuid4())
op4c = criar_operacao(
    status=StatusHandoff.REQUESTED.value, conversation_id=cid4c, target_user_id=7, event_id=ev4c
)

# Rodada 1 — a rede responde 500: aplicou ou nao, ninguem sabe.
CRM.programar(_RespostaFalsa(500, {"detail": {"error": "HANDOFF_NAO_APLICADO"}}))
sessao = SessionLocal()
r16_mod.processar_ciclo(sessao)
sessao.close()
ambiguo4c = ler_operacao(op4c)
check(
    ambiguo4c.attempt_count == 1 and ambiguo4c.error_class == _CE.AMBIGUO_RETRIAVEL.value,
    f"4m. rodada 1 termina AMBIGUA e gasta uma tentativa "
    f"[attempt_count={ambiguo4c.attempt_count} error_class={ambiguo4c.error_class}]",
)

# Rodada 2 — o processo morre ANTES de chamar a rede, ja com a ambiguidade
# anterior no historico. `handoff_to_human` esta no namespace de
# `worker_retomada` (import direto), entao e ali que a substituicao precisa ir.
_handoff_real = r16_mod.handoff_to_human


def _morrer_antes_da_rede(*args, **kwargs):
    raise RuntimeError("processo morto depois de reclamar a linha, antes de chamar o CRM")


liberar_janela(op4c)
_chamadas_antes_do_crash = len(CRM.chamadas)
r16_mod.handoff_to_human = _morrer_antes_da_rede
try:
    sessao = SessionLocal()
    try:
        r16_mod.processar_ciclo(sessao)
    except RuntimeError:
        pass  # o processo "morreu"
    finally:
        sessao.close()
finally:
    r16_mod.handoff_to_human = _handoff_real

durante4c = ler_operacao(op4c)
check(
    len(CRM.chamadas) == _chamadas_antes_do_crash,
    f"4n. o crash foi ANTES da rede - nenhuma chamada nova ao CRM "
    f"({len(CRM.chamadas)} no total)",
)
check(
    durante4c.attempt_count == 1,
    f"4o. o crash NAO consumiu orcamento em cima da ambiguidade anterior "
    f"[attempt_count={durante4c.attempt_count}, esperado 1]",
)

# Rodada 3 — sem roteiro, o CRM falso aplica e responde 200.
liberar_janela(op4c)
sessao = SessionLocal()
r16_mod.processar_ciclo(sessao)
sessao.close()

apos4c = ler_operacao(op4c)
conv4c = ler_conversa(cid4c)
check(
    apos4c.status == StatusHandoff.COMPLETED.value and conv4c.state == "WAITING_HUMAN",
    f"4p. a operacao converge na rodada seguinte ao crash [status={apos4c.status} "
    f"state={conv4c.state}]",
)
check(
    apos4c.attempt_count == 2,
    f"4q. so as DUAS tentativas que chegaram ao desfecho contaram "
    f"[attempt_count={apos4c.attempt_count}]",
)
check(
    all(c["corpo"]["event_id"] == ev4c for c in CRM.chamadas),
    f"4r. a identidade sobreviveu a ambiguidade E ao crash - as "
    f"{len(CRM.chamadas)} chamadas usaram o mesmo event_id",
)


print("\n--- GRUPO 5: R16 retoma com a MESMA identidade ---")
limpar_fila()
CRM.limpar()

cid5, lead5 = criar_conversa()
ev5 = str(uuid.uuid4())
op5 = criar_operacao(
    status=StatusHandoff.REQUESTED.value, conversation_id=cid5, target_user_id=7, event_id=ev5
)

sessao = SessionLocal()
resultado5 = r16_mod.processar_ciclo(sessao)
sessao.close()

apos5 = ler_operacao(op5)
conv5 = ler_conversa(cid5)
check(
    apos5.status == StatusHandoff.COMPLETED.value and conv5.state == "WAITING_HUMAN",
    f"5a. R16 retoma uma linha presa em REQUESTED e a conversa chega a WAITING_HUMAN "
    f"[status={apos5.status} state={conv5.state}]",
)
check(
    len(CRM.chamadas) == 1 and CRM.chamadas[0]["corpo"]["event_id"] == ev5,
    "5b. a retomada usa o MESMO event_id - nenhuma identidade nova, nenhum LLM/triagem",
)
check(
    conv5.atendente_id == 7 and conv5.responsavel_id == 7,
    f"5c. o alvo tambem e o MESMO da tentativa original [atendente={conv5.atendente_id}]",
)
check(resultado5.concluidas == 1, f"5d. o ciclo contabiliza a conclusao [{resultado5}]")


print("\n--- GRUPO 6: R15 Direcao A (Conversas converge para o CRM) ---")
limpar_fila()
CRM.limpar()
COLETOR.limpar()

cid6, lead6 = criar_conversa()
CRM.donos[lead6] = 42  # o CRM ja deu este lead para o usuario 42
ev6 = str(uuid.uuid4())
op6 = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code=_TC.LEAD_JA_ATRIBUIDO.value,
    error_class=_CE.NAO_APLICADO_TERMINAL.value, conversation_id=cid6,
    target_user_id=7, event_id=ev6,
)

sessao = SessionLocal()
r15_mod.processar_ciclo(sessao)
sessao.close()

apos6 = ler_operacao(op6)
conv6 = ler_conversa(cid6)
derivado6 = str(uuid.uuid5(r15_mod.NS_RECONCILIACAO, f"convergencia:{ev6}"))
op_derivada6 = ler_operacao_por_event_id(derivado6)

check(
    conv6.atendente_id == 42 and conv6.responsavel_id == 42 and conv6.state == "WAITING_HUMAN",
    f"6a. Direcao A: a conversa converge para o dono que o CRM JA tem "
    f"[atendente={conv6.atendente_id} state={conv6.state}]",
)
check(apos6.reconciled_at is not None, "6b. Direcao A marca reconciled_at na linha original")
check(
    op_derivada6 is not None and op_derivada6.status == StatusHandoff.COMPLETED.value,
    f"6c. a operacao derivada usa uuid5(NS, 'convergencia:<event_id>') e completa "
    f"[{op_derivada6}]",
)
check(
    op_derivada6 is not None and op_derivada6.target_user_id == 42,
    "6d. a operacao derivada aponta para o dono do CRM, nao para o alvo original recusado",
)


print("\n--- GRUPO 7: R15 Direcao B (CRM converge para o Conversas) ---")
limpar_fila()
CRM.limpar()
COLETOR.limpar()

# Humano 55 assumiu a conversa enquanto o handoff para 7 estava em voo.
cid7, lead7 = criar_conversa(
    is_bot_active=False, atendente_id=55, responsavel_id=None,
    queued_at=datetime.now(timezone.utc),
)
CRM.donos[lead7] = 7  # o CRM aplicou o alvo original antes da recusa local
ev7 = str(uuid.uuid4())
op7 = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    error_class=None, conversation_id=cid7, target_user_id=7, event_id=ev7,
)

sessao = SessionLocal()
r15_mod.processar_ciclo(sessao)
sessao.close()

apos7 = ler_operacao(op7)
derivado7 = str(uuid.uuid5(r15_mod.NS_RECONCILIACAO, f"correcao:{ev7}:55"))
op_derivada7 = ler_operacao_por_event_id(derivado7)

check(
    CRM.donos[lead7] == 55,
    f"7a. Direcao B: o CRM passa a apontar para o atendente LOCAL [dono={CRM.donos[lead7]}]",
)
check(apos7.reconciled_at is not None, "7b. Direcao B marca reconciled_at na linha original")
check(
    op_derivada7 is not None,
    f"7c. a chave derivada e uuid5(NS, 'correcao:<event_id>:<target_user_id>') [{derivado7}]",
)
check(
    str(uuid.uuid5(r15_mod.NS_RECONCILIACAO, f"correcao:{ev7}:55"))
    != str(uuid.uuid5(r15_mod.NS_RECONCILIACAO, f"correcao:{ev7}:56")),
    "7d. os DOIS componentes sao obrigatorios: um alvo diferente produz uma OPERACAO "
    "diferente, nunca um IDEMPOTENCY_PAYLOAD_CONFLICT terminal",
)

# Premissa da Direcao B nao vale: o atendente local JA e o alvo original.
CRM.limpar()
cid7b, lead7b = criar_conversa(
    is_bot_active=False, atendente_id=7, responsavel_id=None,
    queued_at=datetime.now(timezone.utc),
)
op7b = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    conversation_id=cid7b, target_user_id=7,
)
sessao = SessionLocal()
r15_mod.processar_ciclo(sessao)
sessao.close()
check(
    ler_operacao(op7b).reconciled_at is not None and not CRM.chamadas,
    "7e. sem divergencia (atendente local == alvo original), a linha fecha sem "
    "nenhuma chamada ao CRM",
)

# O caso que `resultado.sucesso` sozinho classificaria errado: o humano JA
# respondeu, a conversa esta em HUMAN_ACTIVE, `WAITING_HUMAN` nem e alcancavel
# no grafo de transicoes -> `handoff_to_human` devolve ESTADO_LOCAL_RECUSADO
# mesmo com o CRM ja corrigido, que era o alvo INTEIRO da Direcao B.
limpar_fila()
CRM.limpar()
cid7c, lead7c = criar_conversa(
    state="HUMAN_ACTIVE", is_bot_active=False, atendente_id=88, responsavel_id=88,
    queued_at=datetime.now(timezone.utc),
    primeira_resposta_humana_at=datetime.now(timezone.utc),
)
CRM.donos[lead7c] = 7
op7c = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    conversation_id=cid7c, target_user_id=7,
)
sessao = SessionLocal()
r15_mod.processar_ciclo(sessao)
sessao.close()
check(
    CRM.donos[lead7c] == 88 and ler_operacao(op7c).reconciled_at is not None,
    f"7f. Direcao B converge mesmo quando handoff_to_human reporta FALHA (estado local "
    f"recusado em HUMAN_ACTIVE): o alvo da direcao e lead.responsavel_id, e ele foi "
    f"alcancado [dono={CRM.donos[lead7c]}]",
)


print("\n--- GRUPO 7b: crash DURANTE a reconciliacao retoma sem duplicar efeito ---")
# O `operation_id` derivado e uuid5 DETERMINISTICO (secao 4) e o desenho chama
# isso de "crash-safe" — mas nada provava a propriedade em si, so a formula
# (checks 6c/7c/7d). A prova precisa de um crash de verdade DEPOIS que o CRM ja
# foi corrigido e ANTES de `reconciled_at` ser gravado: e a janela em que o
# worker morre tendo feito a metade externa do trabalho e nenhuma da interna.
# Se a chave derivada nao fosse deterministica, a rodada seguinte cunharia um
# `event_id` NOVO e aplicaria a correcao DUAS vezes no CRM.
limpar_fila()
CRM.limpar()
COLETOR.limpar()

cid7g, lead7g = criar_conversa(
    is_bot_active=False, atendente_id=55, responsavel_id=None,
    queued_at=datetime.now(timezone.utc),
)
CRM.donos[lead7g] = 7
ev7g = str(uuid.uuid4())
op7g = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    error_class=None, conversation_id=cid7g, target_user_id=7, event_id=ev7g,
)

_marcar_real = r15_mod.marcar_reconciliado


def _morrer_antes_de_marcar(*args, **kwargs):
    raise RuntimeError("processo morto depois de corrigir o CRM, antes de reconciled_at")


r15_mod.marcar_reconciliado = _morrer_antes_de_marcar
try:
    sessao = SessionLocal()
    try:
        r15_mod.processar_ciclo(sessao)
    except RuntimeError:
        pass  # o processo "morreu" no meio da reconciliacao
    finally:
        sessao.close()
finally:
    r15_mod.marcar_reconciliado = _marcar_real

_derivado7g = str(uuid.uuid5(r15_mod.NS_RECONCILIACAO, f"correcao:{ev7g}:55"))
durante7g = ler_operacao(op7g)
check(
    CRM.donos[lead7g] == 55,
    f"7g. o crash aconteceu DEPOIS de o CRM ja ter sido corrigido "
    f"[dono={CRM.donos[lead7g]}]",
)
check(
    durante7g.reconciled_at is None,
    "7h. e ANTES de reconciled_at - a linha continua na fila de R15, que e o rastro "
    "que torna a retomada possivel",
)
_chamadas_antes = len(CRM.chamadas)

liberar_janela(op7g)
sessao = SessionLocal()
r15_mod.processar_ciclo(sessao)
sessao.close()

apos7g = ler_operacao(op7g)
check(
    apos7g.reconciled_at is not None,
    "7i. a rodada seguinte reconcilia a MESMA linha ate o fim, sem intervencao manual",
)
# O desfecho e decidido lendo o dono no CRM, nao repetindo a escrita: como a
# correcao ja tinha acontecido antes do crash, a retomada RECONHECE a
# convergencia e fecha a linha sem tocar o CRM de novo. Isso e mais forte do que
# depender da idempotencia por `event_id` — a chamada duplicada nem chega a ser
# feita.
check(
    len(CRM.chamadas) == _chamadas_antes,
    f"7j. a retomada NAO reescreve o CRM - ela le o dono, ve que ja convergiu e "
    f"fecha ({len(CRM.chamadas)} chamadas no total, {_chamadas_antes} antes da retomada)",
)
check(
    CRM.donos[lead7g] == 55,
    f"7k. o dono no CRM continua o mesmo apos a retomada - zero efeito novo "
    f"[dono={CRM.donos[lead7g]}]",
)

# Segundo ponto de crash, o espelho do anterior: o processo morre ANTES de o CRM
# ser corrigido. Aqui a retomada PRECISA reapresentar a correcao, e e este o
# caminho em que o `operation_id` derivado ser deterministico deixa de ser
# detalhe: se a rodada seguinte cunhasse uma chave diferente, o CRM veria duas
# operacoes distintas para a mesma correcao.
limpar_fila()
CRM.limpar()
COLETOR.limpar()

cid7m, lead7m = criar_conversa(
    is_bot_active=False, atendente_id=55, responsavel_id=None,
    queued_at=datetime.now(timezone.utc),
)
CRM.donos[lead7m] = 7
ev7m = str(uuid.uuid4())
op7m = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    error_class=None, conversation_id=cid7m, target_user_id=7, event_id=ev7m,
)

_handoff_r15_real = r15_mod.handoff_to_human
_chave_da_tentativa_perdida = []


def _morrer_antes_de_corrigir(*args, **kwargs):
    """Registra a chave que a tentativa PERDIDA teria usado, e morre."""
    _chave_da_tentativa_perdida.append(kwargs["event_id"])
    raise RuntimeError("processo morto antes de a correcao chegar ao CRM")


r15_mod.handoff_to_human = _morrer_antes_de_corrigir
try:
    sessao = SessionLocal()
    try:
        r15_mod.processar_ciclo(sessao)
    except RuntimeError:
        pass
    finally:
        sessao.close()
finally:
    r15_mod.handoff_to_human = _handoff_r15_real

check(
    len(_chave_da_tentativa_perdida) == 1 and CRM.donos[lead7m] == 7
    and ler_operacao(op7m).reconciled_at is None,
    f"7l. crash ANTES da correcao: o CRM ficou intocado e a linha continua pendente "
    f"[dono={CRM.donos[lead7m]}]",
)

liberar_janela(op7m)
sessao = SessionLocal()
r15_mod.processar_ciclo(sessao)
sessao.close()

check(
    CRM.donos[lead7m] == 55 and ler_operacao(op7m).reconciled_at is not None,
    f"7m. a retomada aplica a correcao que se perdeu [dono={CRM.donos[lead7m]}]",
)
check(
    CRM.chamadas and CRM.chamadas[-1]["corpo"]["event_id"] == _chave_da_tentativa_perdida[0],
    f"7n. e recomputa EXATAMENTE a chave da tentativa perdida - uuid5 e funcao pura "
    f"das mesmas entradas, entao um crash nunca cunha uma operacao nova "
    f"[perdida={_chave_da_tentativa_perdida[0]}, "
    f"retomada={CRM.chamadas[-1]['corpo']['event_id'] if CRM.chamadas else None}]",
)
check(
    _chave_da_tentativa_perdida[0]
    == str(uuid.uuid5(r15_mod.NS_RECONCILIACAO, f"correcao:{ev7m}:55")),
    "7o. e a chave e a formula documentada, nao um valor qualquer estavel por acaso",
)


print("\n--- GRUPO 8: a comparacao current_responsavel_id x atendente_id (achado HIGH) ---")
limpar_fila()
CRM.limpar()
COLETOR.limpar()

# 8a - o 409 traz o dono que JA e o alvo local: convergencia REAL, sem ticket.
cid8a, lead8a = criar_conversa(
    is_bot_active=False, atendente_id=33, responsavel_id=None,
    queued_at=datetime.now(timezone.utc),
)
CRM.donos[lead8a] = 33  # um terceiro escritor ja levou o CRM ao alvo local
op8a = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    conversation_id=cid8a, target_user_id=7,
)
CRM.programar(_RespostaFalsa(409, {"detail": {
    "error": "LEAD_ALREADY_ASSIGNED", "current_responsavel_id": 33,
}}))
sessao = SessionLocal()
r15_mod.processar_ciclo(sessao)
sessao.close()

check(
    ler_operacao(op8a).reconciled_at is not None,
    "8a. 409 cujo dono atual BATE com o atendente local => convergencia real, "
    "reconciled_at marcado",
)
check(
    any("RECONCILIADO_JA_DIVERGIU" in t for t in COLETOR.textos()),
    "8b. o caso resolvido e registrado como RECONCILIADO_JA_DIVERGIU/SUPERADO_TERMINAL",
)
check(
    not any("divergencia NOVA" in t for t in COLETOR.textos(logging.ERROR)),
    "8c. o caso resolvido NAO gera ticket",
)

# 8d - o 409 traz um dono que NAO e nem o esperado nem o pretendido.
# `limpar_fila()` e obrigatorio aqui, e o motivo e informativo: a operacao
# DERIVADA de 8a terminou em FAILED/CRM_LEAD_JA_ATRIBUIDO, o que a coloca na
# fila da Direcao A para o ciclo SEGUINTE (encadeamento previsto - cada
# operacao derivada e uma linha de primeira classe). Sem limpar, ela seria
# reclamada antes de op8b e consumiria a resposta programada abaixo.
limpar_fila()
CRM.limpar()
COLETOR.limpar()
cid8b, lead8b = criar_conversa(
    is_bot_active=False, atendente_id=33, responsavel_id=None,
    queued_at=datetime.now(timezone.utc),
)
CRM.donos[lead8b] = 99  # terceiro escritor levou o lead para OUTRO lugar
op8b = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    conversation_id=cid8b, target_user_id=7,
)
CRM.programar(_RespostaFalsa(409, {"detail": {
    "error": "LEAD_ALREADY_ASSIGNED", "current_responsavel_id": 99,
}}))
sessao = SessionLocal()
r15_mod.processar_ciclo(sessao)
sessao.close()

check(
    ler_operacao(op8b).reconciled_at is None,
    "8d. 409 cujo dono atual NAO bate com o atendente local => divergencia NOVA, "
    "reconciled_at continua NULL (nunca suprimida como 'resolvida')",
)
_erros_8 = COLETOR.textos(logging.ERROR)
check(
    any("divergencia NOVA" in t for t in _erros_8),
    f"8e. divergencia nova gera TICKET [{_erros_8}]",
)
check(
    any(_CE.AMBIGUO_RETRIAVEL.value in t for t in _erros_8),
    "8f. a divergencia nova e tratada como AMBIGUO_RETRIAVEL, como manda a secao 6.2",
)


print("\n--- GRUPO 8b: R15 tambem tem teto e dead-letter ---")
limpar_fila()
CRM.limpar()

# Divergencia que NUNCA converge porque o CRM esta quebrado a cada tentativa.
# O 500 e escolhido de proposito: a operacao DERIVADA que ele produz e
# FAILED/CRM_HTTP_ERRO/AMBIGUO_RETRIAVEL, que pertence a R16 - entao ela nao
# volta para a fila de R15 e o cenario mede exatamente uma coisa, o teto.
cid8c, lead8c = criar_conversa(
    is_bot_active=False, atendente_id=33, responsavel_id=None,
    queued_at=datetime.now(timezone.utc),
)
CRM.donos[lead8c] = 99
op8c = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    conversation_id=cid8c, target_user_id=7,
)
for _rodada in range(fila_mod.TETO_TENTATIVAS):
    liberar_janela(op8c)
    COLETOR.limpar()
    CRM.programar(_RespostaFalsa(500, {"detail": {"error": "HANDOFF_NAO_APLICADO"}}))
    sessao = SessionLocal()
    r15_mod.processar_ciclo(sessao)
    sessao.close()

_final_8c = ler_operacao(op8c)
check(
    _final_8c.attempt_count == fila_mod.TETO_TENTATIVAS and _final_8c.reconciled_at is None,
    f"8g. uma divergencia irresoluvel para no teto em vez de gerar ticket a cada ciclo "
    f"para sempre [attempt_count={_final_8c.attempt_count}]",
)
check(
    any("DEAD-LETTER" in t and "CONTINUA de pe" in t for t in COLETOR.textos(logging.ERROR)),
    "8h. e o dead-letter de R15 nao e silencioso: diz explicitamente que a divergencia "
    "continua de pe",
)
sessao = SessionLocal()
check(
    op8c not in fila_mod.reclamar(sessao, criterios=fila_mod.criterios_r15(), limite=100),
    "8i. a linha em dead-letter sai da fila de R15",
)
sessao.close()


print("\n--- GRUPO 9: sync_responsavel_to_crm e PROIBIDO nos workers (secao 4) ---")

_MODULOS_WORKER = {
    "worker_fila.py": CONVERSAS_DIR / "app" / "v2" / "worker_fila.py",
    "worker_retomada.py": CONVERSAS_DIR / "app" / "v2" / "worker_retomada.py",
    "worker_reconciliacao.py": CONVERSAS_DIR / "app" / "v2" / "worker_reconciliacao.py",
}
_PROIBIDO = "sync_responsavel_to_crm"
# Todo nome importado ou referenciado por cada worker - montado uma vez aqui e
# reusado pelo guard por ACAO (9f) mais abaixo.
_REFERENCIAS_POR_WORKER = {}

for _nome, _caminho in _MODULOS_WORKER.items():
    _fonte = _caminho.read_text(encoding="utf-8")
    _arvore = ast.parse(_fonte)

    _importados = set()
    for _no in ast.walk(_arvore):
        if isinstance(_no, ast.ImportFrom):
            _importados.update(a.name for a in _no.names)
        elif isinstance(_no, ast.Import):
            _importados.update(a.name for a in _no.names)
    _referencias = {
        _no.id for _no in ast.walk(_arvore) if isinstance(_no, ast.Name)
    } | {
        _no.attr for _no in ast.walk(_arvore) if isinstance(_no, ast.Attribute)
    }
    _REFERENCIAS_POR_WORKER[_nome] = _importados | _referencias

    check(
        _PROIBIDO not in _importados and _PROIBIDO not in _referencias,
        f"9. (AST) {_nome} nao importa nem referencia {_PROIBIDO} - a escrita de "
        f"lead.responsavel_id so pode passar pelo endpoint protegido por CAS",
    )
    # O modulo de reconciliacao MENCIONA o nome na docstring, de proposito (e a
    # proibicao documentada). Por isso o guard textual so vale para os outros
    # dois, e a prova real e a AST acima.
    if _nome != "worker_reconciliacao.py":
        check(
            _PROIBIDO not in _fonte,
            f"9b. (texto) {_nome} nao menciona {_PROIBIDO} em lugar nenhum",
        )

check(
    _PROIBIDO not in dir(r15_mod) and _PROIBIDO not in dir(r16_mod),
    "9c. (runtime) o nome proibido nao existe no namespace de nenhum dos dois workers",
)

# 9d - guard por ACAO, complementar ao guard por NOME acima. Os checks 9/9b/9c
# procuram `sync_responsavel_to_crm` LITERALMENTE, entao passam por cima de um
# wrapper com outro nome ou de um `UPDATE leads SET responsavel_id` cru montado
# a mao. O que a secao 4 proibe nao e um identificador, e a ESCRITA em
# `leads.responsavel_id` por fora do endpoint protegido por CAS - e essa
# escrita, em SQL, tem uma forma so. A varredura cobre os tres workers E o
# fechamento transitivo do que eles importam de dentro de `conversas/app`,
# porque a proibicao vale para o caminho inteiro, nao so para o arquivo de cima.
_UPDATE_LEADS = re.compile(r"UPDATE\s+leads\b", re.IGNORECASE)


def _modulo_para_caminho(nome_modulo):
    """`app.services.crm` -> conversas/app/services/crm.py (ou .../__init__.py)."""
    base = CONVERSAS_DIR.joinpath(*nome_modulo.split("."))
    for candidato in (base.with_suffix(".py"), base / "__init__.py"):
        if candidato.is_file():
            return candidato
    return None


def _fechamento_de_imports(caminhos_iniciais):
    """Fonte de cada modulo alcancavel a partir dos iniciais, sem sair de `conversas/`."""
    pendentes, vistos = list(caminhos_iniciais), {}
    while pendentes:
        caminho = pendentes.pop()
        if caminho in vistos:
            continue
        fonte = caminho.read_text(encoding="utf-8")
        vistos[caminho] = fonte
        for _no in ast.walk(ast.parse(fonte)):
            if isinstance(_no, ast.Import):
                _nomes = [a.name for a in _no.names]
            elif isinstance(_no, ast.ImportFrom) and _no.module and not _no.level:
                # `from app.v2 import worker_fila`: tanto o pacote quanto cada
                # nome importado podem ser um modulo.
                _nomes = [_no.module] + [f"{_no.module}.{a.name}" for a in _no.names]
            else:
                continue
            for _nome in _nomes:
                _alvo = _modulo_para_caminho(_nome)
                if _alvo is not None and _alvo not in vistos:
                    pendentes.append(_alvo)
    return vistos


def _escritoras_de_leads(fontes):
    """
    Nome de toda FUNCAO, no fechamento, cujo corpo monta um `UPDATE leads` —
    mais os arquivos que o fazem FORA de qualquer funcao (esses rodariam no
    import, e nao ha caminho de chamada que os evite).

    Granularidade de funcao, e nao de arquivo, de proposito: `services/crm.py`
    e alcancavel a partir de `worker_reconciliacao` (ele importa dali
    `get_leads_responsaveis`, uma LEITURA) e contem a escrita proibida em
    OUTRA funcao. A secao 4 nao proibe o arquivo — proibe a escrita chegar aos
    workers, e o desenho e explicito em manter o caminho legado disponivel para
    a UI. Marcar o arquivo inteiro daria um falso positivo permanente.
    """
    escritoras, fora_de_funcao = set(), []
    for caminho, fonte in fontes.items():
        if not _UPDATE_LEADS.search(fonte):
            continue
        coberto = False
        for _no in ast.walk(ast.parse(fonte)):
            if isinstance(_no, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _UPDATE_LEADS.search(ast.get_source_segment(fonte, _no) or ""):
                    escritoras.add(_no.name)
                    coberto = True
        if not coberto:
            fora_de_funcao.append(caminho.relative_to(ROOT).as_posix())
    return escritoras, fora_de_funcao


_ALCANCAVEIS = _fechamento_de_imports(_MODULOS_WORKER.values())
_ESCRITORAS, _ESCRITA_SOLTA = _escritoras_de_leads(_ALCANCAVEIS)

check(
    _ESCRITORAS,
    f"9d. a varredura por ACAO encontra de fato quem escreve em `leads` "
    f"({sorted(_ESCRITORAS)}) - um guard que nao acha nada nao guarda nada, e este "
    f"aqui deriva a lista proibida do que o codigo FAZ em vez de do nome que ele tem",
)
check(
    not _ESCRITA_SOLTA,
    f"9e. e nenhuma dessas escritas mora fora de uma funcao (rodaria no import, sem "
    f"caminho de chamada que a evite) [{_ESCRITA_SOLTA}]",
)
_ESCRITORAS_REFERENCIADAS = {
    _nome: sorted(_ESCRITORAS & _refs) for _nome, _refs in _REFERENCIAS_POR_WORKER.items()
}
check(
    not any(_ESCRITORAS_REFERENCIADAS.values()),
    f"9f. (acao) e NENHUM dos tres workers importa ou referencia qualquer uma delas - "
    f"e o guard que o check 9 por NOME nao da: um wrapper com outro nome entraria "
    f"nesta lista sozinho, por escrever em `leads`, sem ninguem lembrar de adiciona-lo "
    f"[{ {k: v for k, v in _ESCRITORAS_REFERENCIADAS.items() if v} }]",
)
_WORKERS_COM_SQL_CRU = sorted(
    _nome for _nome, _caminho in _MODULOS_WORKER.items()
    if _UPDATE_LEADS.search(_caminho.read_text(encoding="utf-8"))
)
check(
    not _WORKERS_COM_SQL_CRU,
    f"9g. e nenhum deles monta um `UPDATE leads` proprio, escrito a mao - o caminho "
    f"nao e so 'nao chamar a funcao errada', e nao escrever a query em lugar nenhum "
    f"[{_WORKERS_COM_SQL_CRU}]",
)


print("\n--- GRUPO 10: _registrar_falha persiste error_class junto de error_code ---")
limpar_fila()
CRM.limpar()

cid10, lead10 = criar_conversa()
ev10 = str(uuid.uuid4())
CRM.programar(_RespostaFalsa(500, {"detail": {"error": "HANDOFF_NAO_APLICADO"}}))
handoff_to_human(db, conversation_id=cid10, target_user_id=7, reason=MOTIVO, event_id=ev10)
op10 = ler_operacao_por_event_id(ev10)
_linha10 = ler_operacao(op10.id)
check(
    _linha10.error_code == _TC.HTTP_ERRO.value
    and _linha10.error_class == _CE.AMBIGUO_RETRIAVEL.value,
    f"10a. a falha grava CODIGO e CLASSE na mesma transacao "
    f"[{_linha10.error_code}/{_linha10.error_class}]",
)

CRM.limpar()
handoff_to_human(db, conversation_id=cid10, target_user_id=7, reason=MOTIVO, event_id=ev10)
_linha10b = ler_operacao(op10.id)
check(
    _linha10b.status == StatusHandoff.COMPLETED.value
    and _linha10b.error_code is None and _linha10b.error_class is None,
    f"10b. o sucesso limpa os DOIS campos - uma linha COMPLETED nao carrega classe "
    f"velha [{_linha10b.error_code}/{_linha10b.error_class}]",
)

# Estado local recusado: `error_class` NULL de proposito (nenhum membro de
# ClasseErroCrm descreve uma recusa que nao veio do CRM), e a linha e da fatia
# de R15 por CODIGO.
CRM.limpar()
cid10c, lead10c = criar_conversa(
    state="HUMAN_ACTIVE", is_bot_active=False, atendente_id=88, responsavel_id=88,
    queued_at=datetime.now(timezone.utc),
    primeira_resposta_humana_at=datetime.now(timezone.utc),
)
ev10c = str(uuid.uuid4())
handoff_to_human(db, conversation_id=cid10c, target_user_id=7, reason=MOTIVO, event_id=ev10c)
op10c = ler_operacao_por_event_id(ev10c)
_linha10c = ler_operacao(op10c.id)
check(
    _linha10c.error_code == "ESTADO_LOCAL_RECUSADO" and _linha10c.error_class is None,
    f"10c. ESTADO_LOCAL_RECUSADO grava classe NULL - a recusa nao veio do CRM "
    f"[{_linha10c.error_code}/{_linha10c.error_class}]",
)
sessao = SessionLocal()
_fatia_r16_10c = fila_mod.reclamar(sessao, criterios=fila_mod.criterios_r16(), limite=100)
sessao.close()
check(
    op10c.id not in _fatia_r16_10c,
    "10d. e a linha, por consequencia, fica fora da fila de R16 por DUAS vias "
    "(classe NULL e codigo exclusivo de R15)",
)


print("\n--- GRUPO 11: attempt_count conta ENTREGA julgada, nem mais nem menos ---")
# Os dois lados do mesmo achado. `attempt_count` e o gate de dead-letter, entao
# ele precisa contar EXATAMENTE "a operacao foi entregue a handoff_to_human e
# houve veredito" - e as duas inferencias indiretas que existiam erravam em
# direcoes OPOSTAS:
#   - por RETORNO ("a funcao voltou sem lancar"): um VEREDITO de
#     `handoff_to_human` (ValueError/HandoffInconsistente/CampoInvalido -
#     conversa apagada, event_id reusado) nao contava, e a linha
#     permanentemente ruim girava para sempre, sem nunca chegar ao dead-letter
#     que o docstring de `processar_ciclo` promete existir para ela;
#   - por `error_code != CRM_LIMITE_TAXA`: os early-returns de R15, que NAO
#     chamam o CRM nenhuma vez, contavam assim mesmo (o codigo fica None, e
#     `None != 'CRM_LIMITE_TAXA'` e True).
_CICLOS_RUINS = 12


def _veredicto_do_handoff(*args, **kwargs):
    """O veredito real de uma conversa apagada - `handoff_to_human` levanta isto."""
    raise ValueError("handoff_to_human: conversation_id 999 nao encontrada")


def _rodar_ciclos(modulo, operacao_id, quantos):
    for _ in range(quantos):
        liberar_janela(operacao_id)
        sessao_ciclo = SessionLocal()
        try:
            modulo.processar_ciclo(sessao_ciclo)
        finally:
            sessao_ciclo.close()


# 11a-c - R16: veredito repetido chega ao teto e SAI da fila, com ticket.
limpar_fila()
CRM.limpar()
COLETOR.limpar()
cid11, _ = criar_conversa()
op11 = criar_operacao(status=StatusHandoff.REQUESTED.value, conversation_id=cid11)

_handoff_r16_real = r16_mod.handoff_to_human
r16_mod.handoff_to_human = _veredicto_do_handoff
try:
    _rodar_ciclos(r16_mod, op11, _CICLOS_RUINS)
finally:
    r16_mod.handoff_to_human = _handoff_r16_real

_apos11 = ler_operacao(op11)
check(
    _apos11.attempt_count == fila_mod.TETO_TENTATIVAS,
    f"11a. (R16) {_CICLOS_RUINS} ciclos de VEREDITO param no teto de "
    f"{fila_mod.TETO_TENTATIVAS} - a excecao passa pela contabilidade antes de subir "
    f"para o laco [attempt_count={_apos11.attempt_count}]",
)
liberar_janela(op11)
sessao = SessionLocal()
check(
    op11 not in fila_mod.reclamar(sessao, criterios=fila_mod.criterios_r16(), limite=100),
    "11b. (R16) e a linha permanentemente ruim ALCANCA o dead-letter: para de ser "
    "elegivel em vez de girar para sempre",
)
sessao.close()
check(
    any("DEAD-LETTER" in t for t in COLETOR.textos(logging.ERROR)),
    "11c. (R16) o dead-letter por veredito nao e silencioso - emite o TICKET, que e a "
    "unica coisa que leva o caso a um humano",
)

# 11d - a fronteira: veredito conta, CRASH nao. `except ValueError` e o
# vocabulario que `handoff_to_human` DECLARA levantar; qualquer outra excecao e
# infra/bug, e a garantia da secao 5 ("crash devolve a linha a fila sem queimar
# orcamento") continua valendo para ela.
limpar_fila()
CRM.limpar()
cid11d, _ = criar_conversa()
op11d = criar_operacao(status=StatusHandoff.REQUESTED.value, conversation_id=cid11d)


def _crash_generico(*args, **kwargs):
    raise RuntimeError("OOM-kill simulado: nao e veredito sobre a operacao")


r16_mod.handoff_to_human = _crash_generico
try:
    _rodar_ciclos(r16_mod, op11d, _CICLOS_RUINS)
finally:
    r16_mod.handoff_to_human = _handoff_r16_real

check(
    ler_operacao(op11d).attempt_count == 0,
    f"11d. {_CICLOS_RUINS} crashes que NAO sao veredito continuam sem consumir "
    f"orcamento - a correcao do achado 1 nao desfez a da secao 5 "
    f"[attempt_count={ler_operacao(op11d).attempt_count}]",
)

# 11e-f - R15: o mesmo veredito, pelo caminho das duas direcoes.
limpar_fila()
CRM.limpar()
COLETOR.limpar()
cid11e, lead11e = criar_conversa(
    is_bot_active=False, atendente_id=55, queued_at=datetime.now(timezone.utc)
)
op11e = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    conversation_id=cid11e, target_user_id=7,
)
_handoff_r15_real2 = r15_mod.handoff_to_human
r15_mod.handoff_to_human = _veredicto_do_handoff
try:
    _rodar_ciclos(r15_mod, op11e, _CICLOS_RUINS)
finally:
    r15_mod.handoff_to_human = _handoff_r15_real2

_apos11e = ler_operacao(op11e)
check(
    _apos11e.attempt_count == fila_mod.TETO_TENTATIVAS and _apos11e.reconciled_at is None,
    f"11e. (R15) o veredito tambem para no teto do lado da reconciliacao "
    f"[attempt_count={_apos11e.attempt_count}]",
)
liberar_janela(op11e)
sessao = SessionLocal()
check(
    op11e not in fila_mod.reclamar(sessao, criterios=fila_mod.criterios_r15(), limite=100),
    "11f. (R15) e a linha sai da fila em vez de gerar ticket a cada ciclo para sempre",
)
sessao.close()

# 11g-h - EARLY-RETURN da Direcao B (`atendente_id is None`): nenhuma chamada ao
# CRM, entao nenhuma tentativa. O contrario do que a condicao por `error_code`
# fazia.
limpar_fila()
CRM.limpar()
cid11g, _ = criar_conversa(is_bot_active=False, atendente_id=None)
op11g = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code="ESTADO_LOCAL_RECUSADO",
    conversation_id=cid11g, target_user_id=7,
)
_rodar_ciclos(r15_mod, op11g, 3)
check(
    not CRM.chamadas,
    f"11g. (R15 Direcao B, sem atendente local) o ramo nao chama o CRM nenhuma vez "
    f"[{len(CRM.chamadas)} chamada(s)]",
)
check(
    ler_operacao(op11g).attempt_count == 0,
    f"11h. e por isso NAO consome tentativa - `tentou_rede` e um campo explicito de "
    f"`_Passo`, nao mais uma inferencia a partir de error_code=None "
    f"[attempt_count={ler_operacao(op11g).attempt_count}]",
)

# 11i-j - EARLY-RETURN da Direcao A: o dono do lead esta ilegivel no CRM.
limpar_fila()
CRM.limpar()
cid11i, lead11i = criar_conversa()
CRM.donos.pop(lead11i, None)  # lead ausente do retorno => leitura.lido = False
op11i = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code=_TC.LEAD_JA_ATRIBUIDO.value,
    conversation_id=cid11i, target_user_id=7,
)
_rodar_ciclos(r15_mod, op11i, 3)
check(
    not CRM.chamadas and ler_operacao(op11i).attempt_count == 0,
    f"11i. (R15 Direcao A, dono ilegivel) adiar a decisao nao e uma tentativa: "
    f"{len(CRM.chamadas)} chamada(s), attempt_count={ler_operacao(op11i).attempt_count}",
)

# E o segundo early-return da Direcao A: o lead existe, mas esta SEM dono.
limpar_fila()
CRM.limpar()
cid11j, lead11j = criar_conversa()
CRM.donos[lead11j] = None  # lido, porem sem dono: nao ha alvo para convergir
op11j = criar_operacao(
    status=StatusHandoff.FAILED.value, error_code=_TC.LEAD_JA_ATRIBUIDO.value,
    conversation_id=cid11j, target_user_id=7,
)
_rodar_ciclos(r15_mod, op11j, 3)
check(
    not CRM.chamadas and ler_operacao(op11j).attempt_count == 0,
    f"11j. (R15 Direcao A, lead sem dono) idem - o ticket e o sinal, o contador nao: "
    f"{len(CRM.chamadas)} chamada(s), attempt_count={ler_operacao(op11j).attempt_count}",
)

# 11k - o desfecho de R16 e um StrEnum, como no modulo irmao. Com string solta,
# um typo numa comparacao por `==` nunca bate e nunca levanta: o ciclo passaria
# a nao contabilizar nada, em silencio.
check(
    issubclass(r16_mod.DesfechoRetomada, StrEnum)
    and [m.value for m in r16_mod.DesfechoRetomada] == ["concluida", "falhada", "ignorada"],
    f"11k. DesfechoRetomada e StrEnum com os mesmos tres tokens de antes "
    f"{[m.value for m in r16_mod.DesfechoRetomada]} - comparado por `is`, um typo vira "
    f"AttributeError imediato em vez de um ramo que nunca dispara",
)


db.close()
_logger_v2.removeHandler(COLETOR)
time.sleep(0.05)

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
