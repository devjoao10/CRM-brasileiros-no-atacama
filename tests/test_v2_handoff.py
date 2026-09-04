"""
BIA-V2 Fase 6 - handoff BIA -> humano (`conversas/app/v2/handoff.py`).

Fonte normativa: docs/superpowers/plans/2026-08-29-bia-v2.md, secao "Fase 6 -
Handoff". `handoff_to_human()` registra a intencao, confirma com o CRM, aplica
o estado local e fecha a operacao - e precisa ser IDEMPOTENTE e RECONCILIAVEL,
porque o processo pode morrer em qualquer ponto entre esses passos.

NENHUMA CHAMADA DE REDE REAL. O CRM e substituido por `_CRMFalso`, que se
comporta como o contrato manda: guarda os `event_id` ja vistos e responde
`replayed=true` quando reconhece um. A substituicao troca o ATRIBUTO `httpx`
do modulo sob teste (nunca o `httpx` global do processo), entao nada mais no
ambiente e afetado.

CENARIOS ADVERSARIAIS COBERTOS (um teste real por item, nunca prosa):
  1  caminho feliz + as SETE invariantes de WAITING_HUMAN, uma a uma
  2  sucesso duplo em conversas diferentes, sem cruzar ids
  3  atendente X => responsavel X (briefing): tres campos, um unico valor
  4  mesmo event_id repetido => replay puro, efeito zero duplicado
  5  crash DEPOIS do commit local => segunda chamada e replay (sessao nova)
  6  falha do CRM (rede e 4xx/5xx reais) => nunca WAITING_HUMAN, HANDOFF_FAILED
  7  200 com corpo/eco invalido => tratado como falha, nunca WAITING_HUMAN
  8  BIA_V2_CRM_API_KEY vazia => falha registrada, ZERO chamada HTTP
  9  operacao FAILED retomada com o mesmo event_id, CRM agora funcionando
 10  CRM ok + escrita local recusada => nada pela metade, nada preso em REQUESTED
 11  state mudou ENTRE o inicio e o fim do handoff => recusa, nao sucesso mudo
 12  crash depois do CRM e antes do commit local => reconciliacao na 2a chamada
 13  operacao ja COMPLETED chamada de novo => mesmo resultado, zero efeito
 14  target_user_id invalido/adulterado (tipo errado, e chave reusada com
     parametros diferentes) => recusado alto, sem escrita nenhuma
 15  entrada invalida (conversation_id, reason, event_id, conversa inexistente)
 16  AUTO-VERIFICACAO: a sessao e fechada SEM o teste comitar nada, e os dados
     continuam la - prova de que quem comita e `handoff_to_human`, nao o teste

CENARIOS ACRESCENTADOS PELA REVISAO DA FASE 6 (achados F1/F2/#3/#4):
 8c/d credencial nao-ASCII (aspas curvas) => falha explicita, nunca excecao
      crua escapando do httpx.Client (achado #3)
  F1  INVESTIGACAO ADVERSARIAL: humano responde de verdade (duas sessoes reais)
      DURANTE o round-trip ao CRM => resultado REAL observado (nao suposto)
  #4  IntegrityError simulado dentro de _aplicar_estado_local => tratado no
      MESMO ponto que TransicaoInvalida, nunca escapa cru
  F2  RETOMADA CONCORRENTE de uma operacao REQUESTED por DUAS THREADS REAIS
      (nao sequencial) => so uma aplica o efeito, a outra recebe replay,
      state_version e eventos nunca duplicam

TODA verificacao de efeito le por uma SESSAO NOVA (`ler_conversa`,
`ler_operacao`, `contar_eventos`). Ler pela mesma sessao que escreveu provaria
apenas o que esta na memoria dela; o que importa e o que foi COMITADO.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_v2_handoff.py
"""
import os
import pathlib
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_handoff_test.db"


def _remover_com_retry(caminho: pathlib.Path, tentativas: int = 5, espera_s: float = 0.2) -> bool:
    """
    F7 (revisao da Fase 6) — apaga `caminho` se existir, tolerando o atraso de
    "pending delete" do Windows: um handle recem-fechado de uma execucao
    anterior deste MESMO arquivo (outro processo, ou o watchdog do antivirus)
    pode manter o arquivo bloqueado por um instante depois do processo
    anterior encerrar. Sem isto, `unlink()` podia falhar (ou aparentar
    suceder sem de fato liberar o nome) e a execucao seguinte reusava um
    arquivo com dados da rodada ANTERIOR — foi assim que a primeira execucao
    desta revisao falhou com `UNIQUE constraint failed: conversations.whatsapp`
    sem nenhuma mudanca de codigo entre as rodadas. Devolve True se o caminho
    NAO existe mais ao final (apagado agora ou ja nao existia).
    """
    for _ in range(tentativas):
        if not caminho.exists():
            return True
        try:
            caminho.unlink()
            return True
        except OSError:
            time.sleep(espera_s)
    return not caminho.exists()


if not _remover_com_retry(DB_FILE):
    # Ainda bloqueado apos as tentativas: nome UNICO para esta execucao, em vez
    # de arriscar reusar um arquivo que pode nao ter sido zerado de verdade
    # (o sintoma original de F7). `scratch/` e descartavel e nao versionado
    # (tests/CLAUDE.md) - um arquivo extra ocasional aqui nao se acumula em
    # nenhum artefato rastreado.
    DB_FILE = SCRATCH / f"v2_handoff_test_{uuid.uuid4().hex}.db"

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
# Lidas por app.config NO IMPORT e reexportadas para o namespace de handoff.py -
# precisam existir ANTES do import, nao adianta definir depois.
os.environ["CRM_BASE_URL"] = "http://crm-de-teste.invalido"
# BIA_V2_CRM_API_KEY, nao CRM_API_KEY (achado #2 da revisao da Fase 6 - colide
# com uma variavel de ambiente do n8n; ver o comentario em
# conversas/app/config.py).
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
from sqlalchemy.exc import IntegrityError  # noqa: E402

# Modelos com relationship() por NOME precisam estar registrados antes de
# qualquer query (mesmo padrao de tests/test_v2_gate.py).
import app.models.media_asset  # noqa: E402,F401
import app.models.note  # noqa: E402,F401
import app.models.tag  # noqa: E402,F401
import app.v2.handoff as handoff_mod  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402
from app.models.handoff_op import HandoffOperation, StatusHandoff  # noqa: E402
from app.services.outbound import record_outbound_message  # noqa: E402
from app.v2.eventos_validacao import CampoInvalido, TipoEvento  # noqa: E402
from app.v2.handoff import HandoffInconsistente, handoff_to_human  # noqa: E402

Base.metadata.create_all(
    bind=engine,
    tables=[
        Conversation.__table__, ConversationEvent.__table__, HandoffOperation.__table__,
        Message.__table__,
    ],
)

MOTIVO = "handoff_bia"


# ---------------------------------------------------------------------------
# CRM falso - idempotente por event_id, como o contrato HTTP exige.
# ---------------------------------------------------------------------------
class _RespostaFalsa:
    def __init__(self, status_code, corpo=None, *, json_quebrado=False):
        self.status_code = status_code
        self._corpo = corpo
        self._json_quebrado = json_quebrado

    def json(self):
        if self._json_quebrado:
            raise ValueError("corpo nao e JSON")
        return self._corpo


class _CRMFalso:
    """
    Sem roteiro programado, responde 200 ecoando os tres parametros e marcando
    `replayed` conforme ja tenha visto aquele `event_id` - o comportamento
    idempotente do contrato. `programar(...)` empilha respostas (ou excecoes,
    levantadas na hora) para as PROXIMAS chamadas.

    `gancho` roda ANTES de responder, recebendo o corpo enviado: e como se
    simula "o mundo mudou enquanto a chamada estava em voo" (cenario 11).
    """

    def __init__(self):
        self.chamadas = []
        self.vistos = set()
        self.roteiro = []
        self.gancho = None

    def programar(self, *itens):
        self.roteiro.extend(itens)

    def limpar(self):
        self.chamadas = []
        self.roteiro = []
        self.gancho = None

    def post(self, url, json=None, headers=None):
        self.chamadas.append({"url": url, "corpo": json, "headers": headers or {}})
        if self.gancho is not None:
            self.gancho(json)
        if self.roteiro:
            item = self.roteiro.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        replayed = json["event_id"] in self.vistos
        self.vistos.add(json["event_id"])
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


# Substitui SO o atributo `httpx` do modulo sob teste. `RequestError` continua
# sendo a classe real, para que o `except` de handoff.py capture o que uma
# falha de rede de verdade levantaria.
handoff_mod.httpx = SimpleNamespace(
    Client=_ClientFalso, RequestError=httpx.RequestError, Timeout=httpx.Timeout,
)

db = SessionLocal()
_contador = iter(range(1, 100000))


def novo_id():
    return str(uuid.uuid4())


def criar_conversa(*, state="AI_TRIAGE", state_version=0, is_bot_active=True,
                   atendente_id=None, responsavel_id=None, queued_at=None,
                   primeira_resposta_humana_at=None, status="aberta"):
    """Conversa V2 minima. Devolve (conversation_id, lead_id)."""
    n = next(_contador)
    conv = Conversation(
        lead_id=9000 + n,
        whatsapp=f"5511900{n:06d}",
        status=status,
        unread_count=0,
        is_bot_active=is_bot_active,
        atendente_id=atendente_id,
        responsavel_id=responsavel_id,
        queued_at=queued_at,
        primeira_resposta_humana_at=primeira_resposta_humana_at,
        last_customer_msg_at=datetime.now(timezone.utc),
        state=state,
        state_version=state_version,
    )
    db.add(conv)
    db.commit()
    return conv.id, conv.lead_id


def ler_conversa(cid):
    """Le por uma SESSAO NOVA: so enxerga o que foi COMITADO."""
    sessao = SessionLocal()
    try:
        c = sessao.get(Conversation, cid)
        if c is None:
            return None
        return SimpleNamespace(
            state=c.state, state_version=c.state_version, is_bot_active=c.is_bot_active,
            atendente_id=c.atendente_id, responsavel_id=c.responsavel_id,
            queued_at=c.queued_at, primeira_resposta_humana_at=c.primeira_resposta_humana_at,
            status=c.status, closed_at=c.closed_at,
        )
    finally:
        sessao.close()


def ler_operacao(event_id):
    sessao = SessionLocal()
    try:
        op = sessao.query(HandoffOperation).filter_by(event_id=event_id).one_or_none()
        if op is None:
            return None
        return SimpleNamespace(
            status=op.status, error_code=op.error_code,
            conversation_id=op.conversation_id, target_user_id=op.target_user_id,
        )
    finally:
        sessao.close()


def contar_eventos(cid, tipo):
    sessao = SessionLocal()
    try:
        return (
            sessao.query(ConversationEvent)
            .filter_by(conversation_id=cid, event_type=tipo.value)
            .count()
        )
    finally:
        sessao.close()


def trilha(cid):
    """Todos os eventos da conversa, em ordem de insercao."""
    sessao = SessionLocal()
    try:
        return [
            (e.event_type, e.action, e.result, e.error_code, e.target_user_id)
            for e in sessao.query(ConversationEvent)
            .filter_by(conversation_id=cid).order_by(ConversationEvent.id).all()
        ]
    finally:
        sessao.close()


print("=== Fase 6 - handoff BIA -> humano ===\n")

# ===========================================================================
# 1. Caminho feliz + as SETE invariantes de WAITING_HUMAN
# ===========================================================================
CRM.limpar()
cid1, lead1 = criar_conversa()
ev1 = novo_id()
res1 = handoff_to_human(db, conversation_id=cid1, target_user_id=41, reason=MOTIVO, event_id=ev1)

check(res1.sucesso is True and res1.replayed is False, f"1a. caminho feliz devolve sucesso sem replay ({res1})")
check(res1.error_code is None, f"1b. sucesso vem sem error_code ({res1.error_code!r})")
check(
    res1.conversation_id == cid1 and res1.target_user_id == 41 and res1.event_id == ev1,
    f"1c. resultado ecoa os ids da operacao ({res1})",
)

c1 = ler_conversa(cid1)
check(c1.state == "WAITING_HUMAN", f"1d. conversa comitada em WAITING_HUMAN (state={c1.state!r})")
check(c1.state_version == 1, f"1e. state_version incrementou exatamente 1x (={c1.state_version})")

# As SETE invariantes do plano, uma a uma - nao uma checagem agregada.
check(c1.is_bot_active is False, "1f. invariante 1/7: is_bot_active == False")
check(c1.atendente_id is not None, "1g. invariante 2/7: atendente_id IS NOT NULL")
check(c1.responsavel_id is not None, "1h. invariante 3/7: responsavel_id IS NOT NULL")
check(c1.atendente_id == c1.responsavel_id, "1i. invariante 4/7: atendente_id == responsavel_id")
check(c1.queued_at is not None, "1j. invariante 5/7: queued_at IS NOT NULL")
check(c1.primeira_resposta_humana_at is None, "1k. invariante 6/7: primeira_resposta_humana_at IS NULL")
check(c1.status == "aberta", "1l. invariante 7/7: status == 'aberta'")

op1 = ler_operacao(ev1)
check(op1 is not None and op1.status == StatusHandoff.COMPLETED.value,
      f"1m. handoff_operations COMPLETED (={op1 and op1.status})")
check(op1 is not None and op1.error_code is None, "1n. operacao concluida nao carrega error_code")

check(contar_eventos(cid1, TipoEvento.HANDOFF_REQUESTED) == 1, "1o. exatamente 1 HANDOFF_REQUESTED")
check(contar_eventos(cid1, TipoEvento.HUMAN_SELECTED) == 1, "1p. exatamente 1 HUMAN_SELECTED")
check(contar_eventos(cid1, TipoEvento.HANDOFF_COMPLETED) == 1, "1q. exatamente 1 HANDOFF_COMPLETED")
check(contar_eventos(cid1, TipoEvento.HANDOFF_FAILED) == 0, "1r. nenhum HANDOFF_FAILED no caminho feliz")
check(
    all(acao == MOTIVO for _, acao, _, _, _ in trilha(cid1)),
    f"1s. `reason` foi persistido na coluna `action` dos tres eventos (trilha={trilha(cid1)})",
)

check(len(CRM.chamadas) == 1, f"1t. CRM chamado exatamente 1x ({len(CRM.chamadas)})")
_chamada = CRM.chamadas[0]
check(
    _chamada["corpo"] == {"event_id": ev1, "lead_id": lead1, "target_user_id": 41},
    f"1u. corpo enviado ao CRM segue o contrato fixado ({_chamada['corpo']})",
)
check(
    _chamada["url"].endswith("/api/v2/lead-domain/handoff"),
    f"1v. URL do contrato ({_chamada['url']})",
)
check(
    _chamada["headers"].get("X-API-Key") == "chave-de-teste",
    "1w. credencial vai em X-API-Key (env var, nunca hardcoded)",
)


# ===========================================================================
# 2. Sucesso duplo (briefing) + 3. atendente X => responsavel X
# ===========================================================================
CRM.limpar()
cid2, lead2 = criar_conversa()
ev2 = novo_id()
res2 = handoff_to_human(db, conversation_id=cid2, target_user_id=77, reason=MOTIVO, event_id=ev2)
c2 = ler_conversa(cid2)
check(res2.sucesso is True and c2.state == "WAITING_HUMAN", "2a. segunda conversa tambem conclui (sucesso duplo)")
check(
    ler_conversa(cid1).atendente_id == 41 and c2.atendente_id == 77,
    "2b. as duas operacoes nao cruzam ids (41 e 77 continuam separados)",
)

# Briefing: UM target_user_id alimenta os tres campos - nunca ids divergentes.
check(
    c2.atendente_id == 77 and c2.responsavel_id == 77,
    f"3a. atendente X => responsavel X nos dois campos locais "
    f"(atendente={c2.atendente_id}, responsavel={c2.responsavel_id})",
)
check(
    CRM.chamadas[0]["corpo"]["target_user_id"] == 77,
    "3b. o MESMO valor foi enviado ao CRM, que grava lead.responsavel_id",
)


# ===========================================================================
# 4. Mesmo event_id repetido => replay puro (idempotencia completa)
# ===========================================================================
CRM.limpar()
_trilha_antes = trilha(cid1)
res_replay = handoff_to_human(db, conversation_id=cid1, target_user_id=41, reason=MOTIVO, event_id=ev1)
check(res_replay.sucesso is True and res_replay.replayed is True,
      f"4a. mesmo event_id devolve sucesso com replayed=True ({res_replay})")
check(len(CRM.chamadas) == 0, f"4b. replay nao chama o CRM ({len(CRM.chamadas)} chamadas)")
check(trilha(cid1) == _trilha_antes, "4c. replay nao acrescenta NENHUM evento")
check(ler_conversa(cid1).state_version == 1, "4d. replay nao incrementa state_version (efeito zero duplicado)")
check(ler_operacao(ev1).status == StatusHandoff.COMPLETED.value, "4e. operacao continua COMPLETED apos o replay")


# ===========================================================================
# 5. Crash DEPOIS do commit local => segunda chamada, em SESSAO NOVA, e replay
# ===========================================================================
CRM.limpar()
_sessao_nova = SessionLocal()
try:
    res_pos_crash = handoff_to_human(
        _sessao_nova, conversation_id=cid1, target_user_id=41, reason=MOTIVO, event_id=ev1
    )
finally:
    _sessao_nova.close()
check(
    res_pos_crash.sucesso is True and res_pos_crash.replayed is True and len(CRM.chamadas) == 0,
    f"5. apos o commit local, uma SESSAO NOVA com o mesmo event_id e replay puro ({res_pos_crash})",
)


# ===========================================================================
# 6. Falha do CRM => nunca WAITING_HUMAN, HANDOFF_FAILED emitido
# ===========================================================================
_CASOS_FALHA_CRM = [
    ("rede", httpx.ConnectError("sem rota para o host"), "CRM_INDISPONIVEL"),
    ("timeout", httpx.ReadTimeout("estourou"), "CRM_INDISPONIVEL"),
    ("401", _RespostaFalsa(401, {}), "CRM_NAO_AUTORIZADO"),
    ("404", _RespostaFalsa(404, {}), "CRM_NAO_ENCONTRADO"),
    ("409", _RespostaFalsa(409, {}), "CRM_CONFLITO"),
    ("422", _RespostaFalsa(422, {}), "CRM_RECUSADO"),
    ("500", _RespostaFalsa(500, {}), "CRM_HTTP_ERRO"),
]
for _rotulo, _item, _token in _CASOS_FALHA_CRM:
    CRM.limpar()
    _cid, _ = criar_conversa()
    _ev = novo_id()
    CRM.programar(_item)
    _res = handoff_to_human(db, conversation_id=_cid, target_user_id=55, reason=MOTIVO, event_id=_ev)
    _c = ler_conversa(_cid)
    _op = ler_operacao(_ev)
    check(
        _res.sucesso is False and _res.error_code == _token,
        f"6.{_rotulo} falha do CRM devolve sucesso=False com {_token} (obteve {_res.error_code!r})",
    )
    check(
        _c.state == "AI_TRIAGE" and _c.is_bot_active is True and _c.atendente_id is None
        and _c.responsavel_id is None and _c.state_version == 0,
        f"6.{_rotulo} conversa INTACTA - nunca chega perto de WAITING_HUMAN "
        f"(state={_c.state!r}, bot={_c.is_bot_active}, atendente={_c.atendente_id})",
    )
    check(_op is not None and _op.status == StatusHandoff.FAILED.value and _op.error_code == _token,
          f"6.{_rotulo} operacao marcada FAILED com o motivo (={_op and (_op.status, _op.error_code)})")
    check(contar_eventos(_cid, TipoEvento.HANDOFF_FAILED) == 1, f"6.{_rotulo} exatamente 1 HANDOFF_FAILED")
    check(
        contar_eventos(_cid, TipoEvento.HANDOFF_REQUESTED) == 1
        and contar_eventos(_cid, TipoEvento.HUMAN_SELECTED) == 0
        and contar_eventos(_cid, TipoEvento.HANDOFF_COMPLETED) == 0,
        f"6.{_rotulo} trilha coerente: REQUESTED sim, HUMAN_SELECTED/COMPLETED nao",
    )


# ===========================================================================
# 7. 200 com corpo invalido ou eco divergente => falha, nunca WAITING_HUMAN
# ===========================================================================
CRM.limpar()
cid7a, _ = criar_conversa()
ev7a = novo_id()
CRM.programar(_RespostaFalsa(200, None, json_quebrado=True))
res7a = handoff_to_human(db, conversation_id=cid7a, target_user_id=55, reason=MOTIVO, event_id=ev7a)
check(
    res7a.sucesso is False and res7a.error_code == "CRM_RESPOSTA_INVALIDA"
    and ler_conversa(cid7a).state == "AI_TRIAGE",
    f"7a. 200 com corpo nao-JSON e falha, e a conversa nao muda ({res7a})",
)

CRM.limpar()
cid7b, lead7b = criar_conversa()
ev7b = novo_id()
# 200 "de sucesso", mas confirmando OUTRO atendente: aceitar isso mandaria a
# conversa para um dono que o CRM nunca registrou.
CRM.programar(_RespostaFalsa(200, {"event_id": ev7b, "lead_id": lead7b, "target_user_id": 999, "replayed": False}))
res7b = handoff_to_human(db, conversation_id=cid7b, target_user_id=55, reason=MOTIVO, event_id=ev7b)
check(
    res7b.sucesso is False and res7b.error_code == "CRM_RESPOSTA_INVALIDA"
    and ler_conversa(cid7b).state == "AI_TRIAGE" and ler_conversa(cid7b).atendente_id is None,
    f"7b. 200 cujo eco diverge do que foi enviado NAO autoriza o estado local ({res7b})",
)

CRM.limpar()
cid7c, _ = criar_conversa()
ev7c = novo_id()
# JSON valido que nao e objeto (rota trocada, proxy respondendo outra coisa).
CRM.programar(_RespostaFalsa(200, ["nao", "e", "um", "objeto"]))
res7c = handoff_to_human(db, conversation_id=cid7c, target_user_id=55, reason=MOTIVO, event_id=ev7c)
check(
    res7c.sucesso is False and res7c.error_code == "CRM_RESPOSTA_INVALIDA"
    and ler_conversa(cid7c).state == "AI_TRIAGE",
    f"7c. 200 com JSON que nao e objeto tambem e falha, sem AttributeError ({res7c})",
)


# ===========================================================================
# 8. BIA_V2_CRM_API_KEY vazia => falha registrada, ZERO chamada HTTP
# ===========================================================================
CRM.limpar()
cid8, _ = criar_conversa()
ev8 = novo_id()
_chave_original = handoff_mod.BIA_V2_CRM_API_KEY
handoff_mod.BIA_V2_CRM_API_KEY = ""
try:
    res8 = handoff_to_human(db, conversation_id=cid8, target_user_id=55, reason=MOTIVO, event_id=ev8)
finally:
    handoff_mod.BIA_V2_CRM_API_KEY = _chave_original
check(
    res8.sucesso is False and res8.error_code == "CRM_SEM_CREDENCIAL" and len(CRM.chamadas) == 0,
    f"8a. credencial ausente falha ANTES de qualquer HTTP ({res8}, chamadas={len(CRM.chamadas)})",
)
check(
    contar_eventos(cid8, TipoEvento.HANDOFF_FAILED) == 1 and ler_conversa(cid8).state == "AI_TRIAGE",
    "8b. credencial ausente NAO e no-op silencioso: fica na trilha e nao muda a conversa",
)

# #3 (achado LOW da revisao da Fase 6) - credencial MALFORMADA (nao-ASCII, ex.:
# aspas curvas de copy-paste) precisa do MESMO tratamento que credencial
# ausente - nunca UnicodeEncodeError cru escapando de dentro do httpx.Client.
CRM.limpar()
cid8c, _ = criar_conversa()
ev8c = novo_id()
handoff_mod.BIA_V2_CRM_API_KEY = "chave-com-aspas-curvas-“”"
try:
    res8c = handoff_to_human(db, conversation_id=cid8c, target_user_id=55, reason=MOTIVO, event_id=ev8c)
    _excecao_8c = None
except Exception as exc:  # noqa: BLE001 - queremos ver qualquer excecao crua no relatorio
    res8c = None
    _excecao_8c = exc
finally:
    handoff_mod.BIA_V2_CRM_API_KEY = _chave_original
check(
    _excecao_8c is None and res8c is not None
    and res8c.sucesso is False and res8c.error_code == "CRM_SEM_CREDENCIAL" and len(CRM.chamadas) == 0,
    f"8c. credencial nao-ASCII vira HandoffResult de falha, nunca excecao crua "
    f"(res={res8c}, excecao={_excecao_8c!r}, chamadas={len(CRM.chamadas)})",
)
check(
    contar_eventos(cid8c, TipoEvento.HANDOFF_FAILED) == 1 and ler_conversa(cid8c).state == "AI_TRIAGE",
    "8d. credencial nao-ASCII tambem fica na trilha e nao muda a conversa",
)


# ===========================================================================
# 9. Operacao FAILED retomada com o MESMO event_id, CRM agora funcionando
# ===========================================================================
CRM.limpar()
cid9, lead9 = criar_conversa()
ev9 = novo_id()
CRM.programar(httpx.ConnectError("CRM fora do ar"))
res9a = handoff_to_human(db, conversation_id=cid9, target_user_id=61, reason=MOTIVO, event_id=ev9)
check(res9a.sucesso is False and ler_operacao(ev9).status == StatusHandoff.FAILED.value,
      "9a. primeira tentativa falha e fica FAILED")

res9b = handoff_to_human(db, conversation_id=cid9, target_user_id=61, reason=MOTIVO, event_id=ev9)
c9 = ler_conversa(cid9)
op9 = ler_operacao(ev9)
check(res9b.sucesso is True and res9b.replayed is False,
      f"9b. nova tentativa sob a MESMA chave conclui ({res9b})")
check(c9.state == "WAITING_HUMAN" and c9.atendente_id == 61 and c9.responsavel_id == 61,
      f"9c. estado aplicado na retomada (state={c9.state!r}, atendente={c9.atendente_id})")
check(op9.status == StatusHandoff.COMPLETED.value and op9.error_code is None,
      f"9d. operacao passa a COMPLETED e o error_code anterior e limpo (={op9.status}, {op9.error_code!r})")
check(
    contar_eventos(cid9, TipoEvento.HANDOFF_REQUESTED) == 2
    and contar_eventos(cid9, TipoEvento.HANDOFF_FAILED) == 1
    and contar_eventos(cid9, TipoEvento.HANDOFF_COMPLETED) == 1,
    f"9e. trilha conta as DUAS tentativas (REQUESTED x2, FAILED x1, COMPLETED x1) - trilha={trilha(cid9)}",
)
check(
    CRM.chamadas[-1]["corpo"]["event_id"] == ev9,
    "9f. a retomada reusa a MESMA idempotency key - e o que protege contra aplicacao dupla no CRM",
)


# ===========================================================================
# 10. CRM ok + escrita local recusada => nada pela metade, nada preso em REQUESTED
# ===========================================================================
CRM.limpar()
_agora = datetime.now(timezone.utc)
# Linha divergente (R10): um writer da V1 ja marcou atendimento humano, mas o
# `state` continua AI_TRIAGE. `WAITING_HUMAN` exige o oposto disso.
cid10, _ = criar_conversa(primeira_resposta_humana_at=_agora - timedelta(minutes=5))
ev10 = novo_id()
res10 = handoff_to_human(db, conversation_id=cid10, target_user_id=88, reason=MOTIVO, event_id=ev10)
c10 = ler_conversa(cid10)
op10 = ler_operacao(ev10)
check(res10.sucesso is False and res10.error_code == "ESTADO_LOCAL_RECUSADO",
      f"10a. estado local recusado vira falha explicita ({res10})")
check(c10.state == "AI_TRIAGE" and c10.state_version == 0,
      f"10b. conversa NAO transicionou (state={c10.state!r}, version={c10.state_version})")
check(
    c10.is_bot_active is True and c10.atendente_id is None and c10.responsavel_id is None
    and c10.queued_at is None,
    f"10c. NENHUM campo operacional ficou pela metade - o rollback desfez os quatro "
    f"(bot={c10.is_bot_active}, atendente={c10.atendente_id}, responsavel={c10.responsavel_id}, "
    f"fila={c10.queued_at})",
)
check(op10.status == StatusHandoff.FAILED.value,
      f"10d. operacao NAO fica presa em REQUESTED para sempre (={op10.status})")
check(
    contar_eventos(cid10, TipoEvento.HANDOFF_FAILED) == 1
    and contar_eventos(cid10, TipoEvento.HUMAN_SELECTED) == 0,
    "10e. HANDOFF_FAILED emitido e HUMAN_SELECTED nunca - a selecao nao chegou a valer",
)


# ===========================================================================
# 11. `state` mudou ENTRE o inicio e o fim do handoff
# ===========================================================================
CRM.limpar()
cid11, _ = criar_conversa()
ev11 = novo_id()


def _humano_assume_no_meio(_corpo):
    """Roda DENTRO da chamada ao CRM, por uma sessao SEPARADA: um humano
    assumiu a conversa enquanto o handoff estava em voo. `HUMAN_ACTIVE` nao
    tem aresta para `WAITING_HUMAN` na allowlist de `estados.py`."""
    outra = SessionLocal()
    try:
        conv = outra.get(Conversation, cid11)
        conv.is_bot_active = False
        conv.atendente_id = 99
        conv.responsavel_id = 99
        conv.queued_at = datetime.now(timezone.utc)
        conv.primeira_resposta_humana_at = datetime.now(timezone.utc)
        conv.state = "HUMAN_ACTIVE"
        conv.state_version = 5
        outra.commit()
    finally:
        outra.close()


CRM.gancho = _humano_assume_no_meio
res11 = handoff_to_human(db, conversation_id=cid11, target_user_id=42, reason=MOTIVO, event_id=ev11)
CRM.gancho = None
c11 = ler_conversa(cid11)
check(res11.sucesso is False and res11.error_code == "ESTADO_LOCAL_RECUSADO",
      f"11a. mudanca de state em voo vira FALHA explicita, nunca sucesso mudo ({res11})")
check(c11.state == "HUMAN_ACTIVE" and c11.state_version == 5,
      f"11b. a conversa continua como o outro writer a deixou (state={c11.state!r}, v={c11.state_version})")
check(c11.atendente_id == 99 and c11.responsavel_id == 99,
      f"11c. o dono definido pelo outro writer NAO foi sobrescrito (atendente={c11.atendente_id})")
check(ler_operacao(ev11).status == StatusHandoff.FAILED.value, "11d. operacao registrada como FAILED")


# ===========================================================================
# 12. Crash depois do CRM e antes do commit local => reconciliacao na 2a chamada
# ===========================================================================
# A morte e simulada no PRIMEIRO evento pos-CRM, de proposito: ali nenhum
# SAVEPOINT chegou a existir, entao o que se prova e o comportamento de
# handoff.py, nao a semantica de rollback de SAVEPOINT do pysqlite (instavel no
# SQLite, ver .claude/memory/pysqlite-savepoint-rollback-gap.md; provada
# correta contra PostgreSQL real no cenario A de tests/test_v2_r12_postgres_proof.py).
CRM.limpar()
cid12, lead12 = criar_conversa()
ev12 = novo_id()
_registrar_real = handoff_mod.registrar_evento


def _registrar_com_morte(db_, *, tipo, **kwargs):
    if tipo is TipoEvento.HUMAN_SELECTED:
        raise RuntimeError("processo morreu depois do CRM, antes do commit local")
    return _registrar_real(db_, tipo=tipo, **kwargs)


handoff_mod.registrar_evento = _registrar_com_morte
_sessao_morta = SessionLocal()
_morreu = False
try:
    handoff_to_human(_sessao_morta, conversation_id=cid12, target_user_id=44, reason=MOTIVO, event_id=ev12)
except RuntimeError:
    _morreu = True
finally:
    # Fechar a sessao e o que um processo morto faz: a transacao aberta some.
    _sessao_morta.close()
    handoff_mod.registrar_evento = _registrar_real

c12_antes = ler_conversa(cid12)
op12_antes = ler_operacao(ev12)
check(_morreu, "12a. a morte simulada de fato interrompeu a operacao")
check(
    c12_antes.state == "AI_TRIAGE" and c12_antes.is_bot_active is True
    and c12_antes.atendente_id is None and c12_antes.state_version == 0,
    f"12b. nada do estado local sobreviveu ao crash (state={c12_antes.state!r}, "
    f"bot={c12_antes.is_bot_active}, atendente={c12_antes.atendente_id})",
)
check(op12_antes is not None and op12_antes.status == StatusHandoff.REQUESTED.value,
      f"12c. a operacao ficou REQUESTED - o rastro que torna a reconciliacao possivel "
      f"(={op12_antes and op12_antes.status})")
check(contar_eventos(cid12, TipoEvento.HANDOFF_REQUESTED) == 1
      and contar_eventos(cid12, TipoEvento.HUMAN_SELECTED) == 0,
      "12d. so o HANDOFF_REQUESTED (comitado antes do CRM) sobreviveu")
check(len(CRM.chamadas) == 1, f"12e. o CRM chegou a ser chamado antes da morte ({len(CRM.chamadas)})")

# Segunda chamada, mesmo event_id: RETOMA em vez de comecar do zero.
res12 = handoff_to_human(db, conversation_id=cid12, target_user_id=44, reason=MOTIVO, event_id=ev12)
c12 = ler_conversa(cid12)
check(res12.sucesso is True and res12.replayed is False,
      f"12f. a retomada APLICA o estado (replayed=False: nao e um no-op) ({res12})")
check(c12.state == "WAITING_HUMAN" and c12.atendente_id == 44 and c12.responsavel_id == 44,
      f"12g. estado local finalmente aplicado (state={c12.state!r}, atendente={c12.atendente_id})")
check(ler_operacao(ev12).status == StatusHandoff.COMPLETED.value, "12h. operacao fechada como COMPLETED")
check(contar_eventos(cid12, TipoEvento.HANDOFF_REQUESTED) == 1,
      f"12i. a RETOMADA nao emite um segundo HANDOFF_REQUESTED "
      f"(={contar_eventos(cid12, TipoEvento.HANDOFF_REQUESTED)})")
check(
    len(CRM.chamadas) == 2 and CRM.chamadas[1]["corpo"]["event_id"] == ev12,
    "12j. o CRM foi rechamado com a MESMA idempotency key",
)
check(
    CRM.chamadas[1]["corpo"] == CRM.chamadas[0]["corpo"],
    f"12k. corpo IDENTICO nas duas chamadas - o CRM ve a mesma operacao, nao outra "
    f"({CRM.chamadas[0]['corpo']} vs {CRM.chamadas[1]['corpo']})",
)


# ===========================================================================
# 13. Operacao ja COMPLETED chamada de novo (coberto tambem em 4/5, aqui apos
#     uma retomada - o caminho que mais facilmente reaplicaria por engano)
# ===========================================================================
CRM.limpar()
_trilha12 = trilha(cid12)
res13 = handoff_to_human(db, conversation_id=cid12, target_user_id=44, reason=MOTIVO, event_id=ev12)
check(
    res13.sucesso is True and res13.replayed is True and len(CRM.chamadas) == 0
    and trilha(cid12) == _trilha12 and ler_conversa(cid12).state_version == c12.state_version,
    f"13. operacao reconciliada e chamada de novo devolve o mesmo resultado, sem efeito ({res13})",
)


# ===========================================================================
# 14. target_user_id invalido / adulterado
# ===========================================================================
_VALORES_INVALIDOS = ["7", True, False, None, 0, -1, 1.5, [41], {"id": 41}]
for _valor in _VALORES_INVALIDOS:
    CRM.limpar()
    _cid, _ = criar_conversa()
    _ev = novo_id()
    try:
        handoff_to_human(db, conversation_id=_cid, target_user_id=_valor, reason=MOTIVO, event_id=_ev)
        _resultado_ruim = "nao levantou"
    except CampoInvalido:
        _resultado_ruim = None
    except Exception as exc:  # noqa: BLE001 - queremos o tipo errado no relatorio, nao mascarado
        _resultado_ruim = f"levantou {type(exc).__name__}"
    check(_resultado_ruim is None, f"14a. target_user_id={_valor!r} recusado com CampoInvalido ({_resultado_ruim})")
    check(
        ler_operacao(_ev) is None and ler_conversa(_cid).state == "AI_TRIAGE"
        and contar_eventos(_cid, TipoEvento.HANDOFF_REQUESTED) == 0,
        f"14b. target_user_id={_valor!r}: recusa ANTES de qualquer escrita (zero linha, zero evento)",
    )

# Chave de idempotencia reusada com parametros DIFERENTES (retry adulterado).
CRM.limpar()
_trilha1 = trilha(cid1)
try:
    handoff_to_human(db, conversation_id=cid1, target_user_id=999, reason=MOTIVO, event_id=ev1)
    _res_adulterado = "nao levantou"
except HandoffInconsistente:
    _res_adulterado = None
except Exception as exc:  # noqa: BLE001
    _res_adulterado = f"levantou {type(exc).__name__}"
check(_res_adulterado is None,
      f"14c. mesmo event_id com target_user_id DIFERENTE levanta HandoffInconsistente ({_res_adulterado})")

cid14, _ = criar_conversa()
try:
    handoff_to_human(db, conversation_id=cid14, target_user_id=41, reason=MOTIVO, event_id=ev1)
    _res_outra_conversa = "nao levantou"
except HandoffInconsistente:
    _res_outra_conversa = None
except Exception as exc:  # noqa: BLE001
    _res_outra_conversa = f"levantou {type(exc).__name__}"
check(_res_outra_conversa is None,
      f"14d. mesmo event_id com conversation_id DIFERENTE levanta HandoffInconsistente ({_res_outra_conversa})")
check(
    trilha(cid1) == _trilha1 and ler_conversa(cid1).atendente_id == 41
    and ler_conversa(cid14).state == "AI_TRIAGE" and len(CRM.chamadas) == 0,
    "14e. nenhuma das duas recusas produziu efeito em qualquer das conversas",
)
check(
    "999" not in str(_res_adulterado or "") and "event_id ja registrado" in str(HandoffInconsistente(ev1)),
    "14f. a mensagem de HandoffInconsistente nao ecoa valor recebido (so o token)",
)


# ===========================================================================
# 15. Demais entradas invalidas
# ===========================================================================
_cid15, _ = criar_conversa()
_CASOS_ENTRADA = [
    ("conversation_id string", {"conversation_id": "1"}, CampoInvalido),
    ("conversation_id zero", {"conversation_id": 0}, CampoInvalido),
    ("reason com espaco", {"reason": "handoff bia"}, CampoInvalido),
    ("reason maiusculo", {"reason": "HANDOFF"}, CampoInvalido),
    ("reason vazio", {"reason": ""}, CampoInvalido),
    ("reason nao-str", {"reason": None}, CampoInvalido),
    ("event_id nao-uuid", {"event_id": "nao-e-uuid"}, CampoInvalido),
    ("event_id vazio", {"event_id": ""}, CampoInvalido),
    ("conversa inexistente", {"conversation_id": 987654}, ValueError),
]
for _rotulo, _override, _esperado in _CASOS_ENTRADA:
    CRM.limpar()
    _kwargs = {
        "conversation_id": _cid15, "target_user_id": 41,
        "reason": MOTIVO, "event_id": novo_id(),
    }
    _kwargs.update(_override)
    try:
        handoff_to_human(db, **_kwargs)
        _obtido = "nao levantou"
    except _esperado:
        _obtido = None
    except Exception as exc:  # noqa: BLE001
        _obtido = f"levantou {type(exc).__name__}"
    check(_obtido is None, f"15.{_rotulo}: recusado com {_esperado.__name__} ({_obtido})")
check(
    ler_conversa(_cid15).state == "AI_TRIAGE" and len(CRM.chamadas) == 0,
    "15z. nenhuma entrada invalida chegou ao CRM nem tocou a conversa",
)

# `event_id` em MAIUSCULAS e o MESMO UUID: precisa ser reconhecido como replay,
# nao virar uma segunda operacao (canonicalizacao de `validar_event_id`).
CRM.limpar()
res_canon = handoff_to_human(
    db, conversation_id=cid1, target_user_id=41, reason=MOTIVO, event_id=ev1.upper()
)
check(
    res_canon.replayed is True and res_canon.event_id == ev1 and len(CRM.chamadas) == 0,
    f"15zz. event_id em maiusculas e canonicalizado e cai no replay, nao numa operacao nova ({res_canon})",
)


# ===========================================================================
# 16. AUTO-VERIFICACAO: quem comita e handoff_to_human, nao o teste
# ===========================================================================
# `get_db()` so faz `db.close()` - nao comita e nao faz rollback explicito. Uma
# operacao composta que esquece o commit final SOME EM SILENCIO. Aqui a sessao e
# fechada sem UM UNICO commit do lado do teste, nos tres desfechos.
CRM.limpar()
cid16a, _ = criar_conversa()
cid16b, _ = criar_conversa()
ev16a, ev16b = novo_id(), novo_id()
_sessao_sem_commit = SessionLocal()
try:
    handoff_to_human(_sessao_sem_commit, conversation_id=cid16a, target_user_id=71,
                     reason=MOTIVO, event_id=ev16a)
    CRM.programar(httpx.ConnectError("fora do ar"))
    handoff_to_human(_sessao_sem_commit, conversation_id=cid16b, target_user_id=72,
                     reason=MOTIVO, event_id=ev16b)
    _pendencias = (len(_sessao_sem_commit.new), len(_sessao_sem_commit.dirty), len(_sessao_sem_commit.deleted))
finally:
    _sessao_sem_commit.close()

check(_pendencias == (0, 0, 0), f"16a. a sessao nao fica com escrita pendente apos a operacao ({_pendencias})")
check(
    ler_conversa(cid16a).state == "WAITING_HUMAN"
    and ler_operacao(ev16a).status == StatusHandoff.COMPLETED.value
    and contar_eventos(cid16a, TipoEvento.HANDOFF_COMPLETED) == 1,
    "16b. SUCESSO persistiu sem o teste comitar nada",
)
check(
    ler_operacao(ev16b).status == StatusHandoff.FAILED.value
    and contar_eventos(cid16b, TipoEvento.HANDOFF_FAILED) == 1,
    "16c. FALHA persistiu sem o teste comitar nada",
)


# ===========================================================================
# F1. INVESTIGACAO ADVERSARIAL (revisao da Fase 6) - reproduz EXATAMENTE o
# cenario do revisor: um humano responde DE VERDADE (record_outbound_message
# -> marcar_atendimento_humano, app/services/outbound.py) DURANTE o
# round-trip HTTP ao CRM, comitando numa SESSAO SEPARADA - nao um mock, uma
# segunda sessao real que comita antes do commit final do handoff. O revisor
# afirmou que isso "passa despercebido (CHECK do banco satisfeito, ninguem
# falha)"; este teste prova o resultado REAL, nao presume nem a duvida nem a
# afirmacao do revisor.
# ===========================================================================
CRM.limpar()
cidF1, _ = criar_conversa()
evF1 = novo_id()


def _humano_responde_no_meio_f1(_corpo):
    """Roda DENTRO da chamada ao CRM (CRM.gancho), por uma SESSAO SEPARADA que
    COMITA de verdade - reproduz o cenario do revisor para F1."""
    outra = SessionLocal()
    try:
        conv = outra.get(Conversation, cidF1)
        record_outbound_message(
            outra, conv, "Oi! Ja estou vendo seu caso.", "text",
            {"messages": [{"id": "wamid.TESTE_F1"}]},
            autor_user_id=999,
        )
    finally:
        outra.close()


CRM.gancho = _humano_responde_no_meio_f1
resF1 = handoff_to_human(db, conversation_id=cidF1, target_user_id=42, reason=MOTIVO, event_id=evF1)
CRM.gancho = None
cF1 = ler_conversa(cidF1)
opF1 = ler_operacao(evF1)
check(
    resF1.sucesso is False and resF1.error_code == "ESTADO_LOCAL_RECUSADO",
    f"F1a. RESULTADO REAL: resposta humana concorrente vira FALHA explicita, "
    f"NUNCA sucesso silencioso como o revisor temia ({resF1})",
)
check(
    cF1.atendente_id == 999 and cF1.primeira_resposta_humana_at is not None,
    f"F1b. o efeito do humano concorrente SOBREVIVE intacto - o handoff nao o "
    f"sobrescreveu (atendente={cF1.atendente_id}, primeira_resposta={cF1.primeira_resposta_humana_at})",
)
check(
    cF1.state == "AI_TRIAGE" and cF1.state_version == 0,
    f"F1c. a conversa NAO transicionou para WAITING_HUMAN (state={cF1.state!r}, version={cF1.state_version})",
)
check(
    opF1 is not None and opF1.status == StatusHandoff.FAILED.value,
    f"F1d. operacao registrada FAILED - nao presa em REQUESTED, nao COMPLETED por engano (={opF1 and opF1.status})",
)
check(
    contar_eventos(cidF1, TipoEvento.HUMAN_SELECTED) == 0
    and contar_eventos(cidF1, TipoEvento.HANDOFF_FAILED) == 1,
    "F1e. nenhum HUMAN_SELECTED (a selecao da BIA nunca chegou a valer) e exatamente 1 HANDOFF_FAILED",
)


# ===========================================================================
# #4. IntegrityError (ex.: violacao do CHECK ck_conversations_estado_humano)
# tratada no MESMO ponto que TransicaoInvalida - nao escapa crua do
# db.flush() dentro de _aplicar_estado_local. O gatilho real e inalcancavel
# hoje (a validacao Python ja cobre o que o CHECK cobre); o teste prova o
# EXCEPT simulando a excecao no ponto que _aplicar_estado_local chama.
# ===========================================================================
CRM.limpar()
cid4, _ = criar_conversa()
ev4 = novo_id()
_aplicar_transicao_original = handoff_mod.aplicar_transicao


def _aplicar_transicao_com_integrity_error(*_args, **_kwargs):
    raise IntegrityError("simulado", {}, Exception("CHECK ck_conversations_estado_humano"))


handoff_mod.aplicar_transicao = _aplicar_transicao_com_integrity_error
try:
    res4 = handoff_to_human(db, conversation_id=cid4, target_user_id=55, reason=MOTIVO, event_id=ev4)
finally:
    handoff_mod.aplicar_transicao = _aplicar_transicao_original

c4 = ler_conversa(cid4)
op4 = ler_operacao(ev4)
check(
    res4.sucesso is False and res4.error_code == "ESTADO_LOCAL_RECUSADO",
    f"#4a. IntegrityError dentro de _aplicar_estado_local vira falha explicita, "
    f"MESMO error_code de TransicaoInvalida ({res4})",
)
check(
    c4.state == "AI_TRIAGE" and c4.state_version == 0,
    f"#4b. conversa NAO transicionou (state={c4.state!r}, version={c4.state_version})",
)
check(
    op4 is not None and op4.status == StatusHandoff.FAILED.value,
    f"#4c. operacao fica FAILED, nao presa nem propagando excecao crua (={op4 and op4.status})",
)
check(
    contar_eventos(cid4, TipoEvento.HANDOFF_FAILED) == 1,
    "#4d. HANDOFF_FAILED emitido para o IntegrityError, mesma trilha de TransicaoInvalida",
)


# ===========================================================================
# F2. RETOMADA CONCORRENTE de uma operacao REQUESTED - duas chamadas REAIS
# (threads, nao sequenciais) com o MESMO event_id. Um barrier forca as duas a
# disputar a reserva do desfecho (_reservar_desfecho_ou_ceder_para_vencedor)
# no mesmo instante. So uma pode aplicar o efeito local; a outra recebe
# replay de sucesso, nunca duplica state_version nem eventos.
# ===========================================================================
CRM.limpar()
cidF2, _ = criar_conversa()
evF2 = novo_id()

# 1) Produz uma operacao REQUESTED "presa" (processo morreu ANTES do commit
#    local) - mesmo mecanismo do cenario 12: mata no primeiro evento pos-CRM.
_registrar_real_f2 = handoff_mod.registrar_evento


def _registrar_com_morte_f2(db_, *, tipo, **kwargs):
    if tipo is TipoEvento.HUMAN_SELECTED:
        raise RuntimeError("morte simulada para preparar o cenario F2")
    return _registrar_real_f2(db_, tipo=tipo, **kwargs)


handoff_mod.registrar_evento = _registrar_com_morte_f2
_sessao_prep_f2 = SessionLocal()
try:
    try:
        handoff_to_human(_sessao_prep_f2, conversation_id=cidF2, target_user_id=48, reason=MOTIVO, event_id=evF2)
    except RuntimeError:
        pass
finally:
    _sessao_prep_f2.close()
    handoff_mod.registrar_evento = _registrar_real_f2

_op_prep_f2 = ler_operacao(evF2)
check(
    _op_prep_f2 is not None and _op_prep_f2.status == StatusHandoff.REQUESTED.value,
    f"F2-prep. operacao ficou REQUESTED antes da corrida, pronta para a retomada concorrente "
    f"(={_op_prep_f2 and _op_prep_f2.status})",
)
CRM.limpar()  # a chamada da preparacao nao entra nos asserts da corrida em si

# 2) Duas chamadas REAIS e CONCORRENTES (threads, nao sequencial), MESMO
#    event_id. Barrier forca as duas a entrar em _aplicar_estado_local no
#    mesmo instante - sem isso threads poderiam nao interleavar de verdade
#    num SQLite pequeno e rapido o bastante para uma terminar antes da outra
#    comecar. O barrier fica ANTES de _aplicar_estado_local (nao dentro de
#    _reservar_desfecho_ou_ceder_para_vencedor): naquele ponto NENHUMA das
#    duas ainda tomou o lock de escrita do SQLite. Colocar o barrier DEPOIS
#    do flush (dentro de _reservar_...) foi tentado e CAUSA DEADLOCK real:
#    quem vence o flush primeiro fica esperando a outra no barrier enquanto
#    SEGURA o lock de escrita, e a outra nunca alcanca o barrier porque seu
#    proprio flush esta bloqueado esperando esse MESMO lock - as duas batem
#    no timeout do barrier (BrokenBarrierError), nunca chegando a disputar
#    coisa nenhuma. Comportamento observado e corrigido nesta revisao.
_barreira_f2 = threading.Barrier(2)
_aplicar_estado_local_original_f2 = handoff_mod._aplicar_estado_local


def _aplicar_estado_local_com_barreira(db_, conversation_, *, target_user_id, reason):
    _barreira_f2.wait(timeout=5)
    return _aplicar_estado_local_original_f2(db_, conversation_, target_user_id=target_user_id, reason=reason)


handoff_mod._aplicar_estado_local = _aplicar_estado_local_com_barreira

_resultados_f2 = {}
_erros_f2 = {}


def _chamar_f2(nome):
    sessao = SessionLocal()
    try:
        _resultados_f2[nome] = handoff_to_human(
            sessao, conversation_id=cidF2, target_user_id=48, reason=MOTIVO, event_id=evF2
        )
    except Exception as exc:  # noqa: BLE001 - queremos ver qualquer excecao inesperada no relatorio
        _erros_f2[nome] = exc
    finally:
        sessao.close()


_t1_f2 = threading.Thread(target=_chamar_f2, args=("t1",))
_t2_f2 = threading.Thread(target=_chamar_f2, args=("t2",))
_t1_f2.start()
_t2_f2.start()
_t1_f2.join(timeout=15)
_t2_f2.join(timeout=15)
handoff_mod._aplicar_estado_local = _aplicar_estado_local_original_f2

check(not _erros_f2, f"F2a. nenhuma das duas chamadas concorrentes levantou excecao inesperada (erros={_erros_f2})")

c_f2 = ler_conversa(cidF2)
op_f2 = ler_operacao(evF2)
_res_t1_f2 = _resultados_f2.get("t1")
_res_t2_f2 = _resultados_f2.get("t2")

_vencedores_f2 = [r for r in (_res_t1_f2, _res_t2_f2) if r is not None and r.sucesso and not r.replayed]
_replays_f2 = [r for r in (_res_t1_f2, _res_t2_f2) if r is not None and r.sucesso and r.replayed]

check(
    len(_vencedores_f2) == 1,
    f"F2b. exatamente UMA das duas chamadas concorrentes aplicou o efeito local "
    f"(t1={_res_t1_f2}, t2={_res_t2_f2})",
)
check(
    len(_replays_f2) == 1,
    f"F2c. a OUTRA chamada recebeu replay de sucesso (nunca um erro de conflito distinto - "
    f"o event_id identifica a OPERACAO, que de fato terminou com sucesso) (replays={len(_replays_f2)})",
)

check(
    c_f2.state == "WAITING_HUMAN" and c_f2.state_version == 1,
    f"F2d. state_version incrementou EXATAMENTE 1x, nunca 2x (state={c_f2.state!r}, version={c_f2.state_version})",
)
check(
    c_f2.atendente_id == 48 and c_f2.responsavel_id == 48,
    f"F2e. estado aplicado uma unica vez, sem sobra (atendente={c_f2.atendente_id}, responsavel={c_f2.responsavel_id})",
)
check(
    op_f2 is not None and op_f2.status == StatusHandoff.COMPLETED.value,
    f"F2f. operacao termina COMPLETED (={op_f2 and op_f2.status})",
)
check(
    contar_eventos(cidF2, TipoEvento.HUMAN_SELECTED) == 1
    and contar_eventos(cidF2, TipoEvento.HANDOFF_COMPLETED) == 1,
    f"F2g. HUMAN_SELECTED e HANDOFF_COMPLETED aparecem EXATAMENTE 1x cada, nao 2x "
    f"(HUMAN_SELECTED={contar_eventos(cidF2, TipoEvento.HUMAN_SELECTED)}, "
    f"HANDOFF_COMPLETED={contar_eventos(cidF2, TipoEvento.HANDOFF_COMPLETED)})",
)


# ===========================================================================
# F2b (acompanhamento do coordenador apos a revisao) - retomada CONCORRENTE
# de uma operacao FAILED (nao REQUESTED). Achado MEDIUM de um code-reviewer
# de arquitetura independente: o ramo `else` de `_abrir_tentativa` reabre
# QUALQUER status existente (inclusive FAILED) de volta para REQUESTED via
# um UPDATE simples, sem lock nem claim proprio algum - pergunta: a MESMA
# `_reservar_desfecho_ou_ceder_para_vencedor` (que fecha F2 para retomadas
# de uma REQUESTED) tambem fecha o caminho que passa por ali?
#
# UM barrier REUTILIZADO em DOIS pontos (threading.Barrier(2) libera e
# automaticamente se rearma para a proxima rodada): forca as duas threads a
# reabrir a operacao FAILED (_abrir_tentativa) no MESMO instante, e forca as
# duas a disputar a reserva do desfecho (dentro de _aplicar_estado_local) no
# MESMO instante tambem - o cenario mais adversarial que o achado descreve.
# ===========================================================================
CRM.limpar()
cidF2b, _ = criar_conversa()
evF2b = novo_id()

# 1) Produz uma operacao FAILED de VERDADE (falha real do CRM, nao um crash
#    simulado) - exatamente o estado de partida que o achado descreve.
CRM.programar(httpx.ConnectError("CRM fora do ar (preparacao F2b)"))
_res_prep_f2b = handoff_to_human(db, conversation_id=cidF2b, target_user_id=51, reason=MOTIVO, event_id=evF2b)
_op_prep_f2b = ler_operacao(evF2b)
check(
    _res_prep_f2b.sucesso is False and _op_prep_f2b is not None
    and _op_prep_f2b.status == StatusHandoff.FAILED.value,
    f"F2b-prep. operacao ficou FAILED antes da corrida (={_op_prep_f2b and _op_prep_f2b.status})",
)
CRM.limpar()  # a chamada da preparacao nao entra nos asserts da corrida em si

# 2) Duas chamadas REAIS e CONCORRENTES (threads, nao sequencial), MESMO
#    event_id, retomando a MESMA operacao FAILED.
_barreira_f2b = threading.Barrier(2)
_abrir_tentativa_original_f2b = handoff_mod._abrir_tentativa
_aplicar_estado_local_original_f2b = handoff_mod._aplicar_estado_local


def _abrir_tentativa_com_barreira(db_, operacao_, *, event_id, conversation, target_user_id, reason):
    _barreira_f2b.wait(timeout=5)  # rodada 1: as duas reabrem a FAILED juntas
    return _abrir_tentativa_original_f2b(
        db_, operacao_, event_id=event_id, conversation=conversation,
        target_user_id=target_user_id, reason=reason,
    )


def _aplicar_estado_local_com_barreira_f2b(db_, conversation_, *, target_user_id, reason):
    _barreira_f2b.wait(timeout=5)  # rodada 2 (barrier rearmado): disputam a reserva do desfecho
    return _aplicar_estado_local_original_f2b(db_, conversation_, target_user_id=target_user_id, reason=reason)


handoff_mod._abrir_tentativa = _abrir_tentativa_com_barreira
handoff_mod._aplicar_estado_local = _aplicar_estado_local_com_barreira_f2b

_resultados_f2b = {}
_erros_f2b = {}


def _chamar_f2b(nome):
    sessao = SessionLocal()
    try:
        _resultados_f2b[nome] = handoff_to_human(
            sessao, conversation_id=cidF2b, target_user_id=51, reason=MOTIVO, event_id=evF2b
        )
    except Exception as exc:  # noqa: BLE001 - queremos ver qualquer excecao inesperada no relatorio
        _erros_f2b[nome] = exc
    finally:
        sessao.close()


_t1_f2b = threading.Thread(target=_chamar_f2b, args=("t1",))
_t2_f2b = threading.Thread(target=_chamar_f2b, args=("t2",))
_t1_f2b.start()
_t2_f2b.start()
_t1_f2b.join(timeout=15)
_t2_f2b.join(timeout=15)
handoff_mod._abrir_tentativa = _abrir_tentativa_original_f2b
handoff_mod._aplicar_estado_local = _aplicar_estado_local_original_f2b

check(
    not _erros_f2b,
    f"F2b-a. nenhuma das duas retomadas concorrentes de uma operacao FAILED levantou excecao "
    f"inesperada (erros={_erros_f2b})",
)

c_f2b = ler_conversa(cidF2b)
op_f2b = ler_operacao(evF2b)
_res_t1_f2b = _resultados_f2b.get("t1")
_res_t2_f2b = _resultados_f2b.get("t2")

_vencedores_f2b = [r for r in (_res_t1_f2b, _res_t2_f2b) if r is not None and r.sucesso and not r.replayed]
_replays_f2b = [r for r in (_res_t1_f2b, _res_t2_f2b) if r is not None and r.sucesso and r.replayed]

check(
    len(_vencedores_f2b) == 1,
    f"F2b-b. exatamente UMA das duas retomadas concorrentes de uma FAILED aplicou o efeito local "
    f"(t1={_res_t1_f2b}, t2={_res_t2_f2b})",
)
check(
    len(_replays_f2b) == 1,
    f"F2b-c. a OUTRA recebeu replay de sucesso - a MESMA _reservar_desfecho_ou_ceder_para_vencedor "
    f"tambem protege o caminho que passa pelo ramo `else` de _abrir_tentativa (replays={len(_replays_f2b)})",
)
check(
    c_f2b.state == "WAITING_HUMAN" and c_f2b.state_version == 1,
    f"F2b-d. state_version incrementou EXATAMENTE 1x, mesmo com as DUAS retomadas reabrindo a "
    f"FAILED simultaneamente via _abrir_tentativa (state={c_f2b.state!r}, version={c_f2b.state_version})",
)
check(
    c_f2b.atendente_id == 51 and c_f2b.responsavel_id == 51,
    f"F2b-e. estado aplicado uma unica vez, sem sobra (atendente={c_f2b.atendente_id}, responsavel={c_f2b.responsavel_id})",
)
check(
    op_f2b is not None and op_f2b.status == StatusHandoff.COMPLETED.value,
    f"F2b-f. operacao termina COMPLETED (={op_f2b and op_f2b.status})",
)
check(
    contar_eventos(cidF2b, TipoEvento.HUMAN_SELECTED) == 1
    and contar_eventos(cidF2b, TipoEvento.HANDOFF_COMPLETED) == 1,
    f"F2b-g. HUMAN_SELECTED e HANDOFF_COMPLETED aparecem EXATAMENTE 1x cada, nao 2x "
    f"(HUMAN_SELECTED={contar_eventos(cidF2b, TipoEvento.HUMAN_SELECTED)}, "
    f"HANDOFF_COMPLETED={contar_eventos(cidF2b, TipoEvento.HANDOFF_COMPLETED)})",
)
_qtd_requested_f2b = contar_eventos(cidF2b, TipoEvento.HANDOFF_REQUESTED)
check(
    _qtd_requested_f2b == 3,
    f"F2b-h. HANDOFF_REQUESTED aparece 3x (1 da preparacao FAILED + 1 de CADA retomada concorrente "
    f"reabrindo via _abrir_tentativa's else, redundante mas TOLERADO - nao toca `conversations` nem "
    f"duplica o efeito caro, mesma tolerancia ja documentada para duplo-FAILED) - o numero exato aqui "
    f"e OBSERVADO, nao presumido (={_qtd_requested_f2b}, trilha completa={trilha(cidF2b)})",
)


db.close()

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
