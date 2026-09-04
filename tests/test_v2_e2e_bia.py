"""
BIA-V2 - teste de aceitacao E2E local (entrega final antes de fechar o
trabalho da V2). Cobre 7 cenarios nomeados (A-G) batendo no endpoint HTTP
REAL (`POST /webhook/agent-bia-v2`, `conversas/app/routers/v2_agent.py`) via
`fastapi.testclient.TestClient` - ao contrario de `tests/test_v2_pipeline.py`,
que testa majoritariamente `executar_pipeline()` direto. Aqui o alvo e a
PILHA INTEIRA: roteador + autenticacao + `executar_pipeline` (Fases 1-9
orquestradas pela Fase 10).

Sem rede real (Gemini/CRM/n8n) e sem efeito de producao - mesma convencao do
resto da suite (`tests/CLAUDE.md`). SQLite descartavel proprio deste arquivo.

CENARIOS
  A  Triagem incompleta -> pipeline NAO avanca para handoff; resposta final
     (via comunicador, acao=solicitar_campos_faltantes) menciona os campos
     que faltam.
  B  Triagem completa (sem pedido explicito - isola o gatilho
     `resultado_triagem.completa`) -> handoff bem sucedido em V2_MODE="live"
     -> conversa em WAITING_HUMAN. `handoff_to_human` roda de verdade; o CRM
     e substituido por um fake HTTP (mesmo padrao de tests/test_v2_handoff.py).
  C  Mesma triagem completa de B, mas SEM atendente elegivel
     (`selecionar_atendente` substituida para devolver None,
     deliberadamente, para nao depender da ordem de execucao em relacao a B
     - ver nota no bloco do Cenario C) -> SEM_ATENDENTE_ELEGIVEL, conversa
     permanece em AI_TRIAGE.
  D  Um segundo "writer" muda `state_version`/`atendente_id` da conversa
     ENQUANTO a IA "pensa" (dentro da fronteira de interpretador, mesma
     tecnica de tests/test_v2_pipeline.py Cenario 8) -> o gate recusa o
     envio -> resposta descartada, nunca chega ao cliente.
  E  A IA Comunicadora (fake) tenta mandar um valor monetario -> o
     output_guard bloqueia -> o texto NUNCA aparece na resposta final.
  F  Mesmo payload, mesmo `event_id`, duas vezes -> primeira chamada
     processa normalmente (200), segunda da 409 (EventoDuplicado) sem
     nenhum efeito adicional.
  G  Cenario de handoff completo em V2_MODE="dry_run" (default) - reusa o
     setup de B - prova zero efeito colateral real: zero mudanca em
     `conversations`, zero linha nova em `handoff_operations`,
     `handoff_to_human` real nunca chamado.

FRONTEIRAS SUBSTITUIDAS (nenhuma diferente do que o resto da suite ja faz):
  - `interpretador._chamar_llm` / `comunicador._chamar_gemini` - SEMPRE,
    para todos os 7 cenarios (mesmo padrao de tests/test_v2_pipeline.py).
  - `handoff.httpx` - SO no Cenario B (o unico que exercita
    `handoff_to_human` de verdade), mesmo padrao de tests/test_v2_handoff.py.
  - `pipeline.selecionar_atendente` - SO no Cenario C (forca "ninguem
    elegivel" sem depender de qual conversa/usuario outro cenario deste
    MESMO arquivo ja criou).
  - `pipeline.handoff_to_human` - SO no Cenario G (fake que levanta
    AssertionError se for chamado - prova ADICIONAL, em cima da garantia
    estrutural do proprio pipeline.py, de que dry_run nunca aciona a funcao
    com efeito real).

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_v2_e2e_bia.py
"""
import json
import os
import pathlib
import sys
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_e2e_bia_test.db"


def _remover_com_retry(caminho: pathlib.Path, tentativas: int = 5, espera_s: float = 0.2) -> bool:
    """Mesma receita de tests/test_v2_handoff.py: tolera o atraso de "pending
    delete" do Windows num arquivo recem-liberado por uma execucao anterior
    deste MESMO arquivo. Devolve True se `caminho` NAO existe mais ao final."""
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
    DB_FILE = SCRATCH / f"v2_e2e_bia_test_{uuid.uuid4().hex}.db"

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
os.environ.setdefault("V2_MODE", "dry_run")
# Fixa "todos os usuarios ativos sao elegiveis" (default) - o Cenario B/G
# depende de EXATAMENTE um usuario ativo na tabela para ser deterministico;
# um valor herdado do ambiente do host quebraria isso em silencio.
os.environ["ATENDENTES_ELEGIVEIS"] = ""
# Credencial da ponte Conversas -> CRM. So o Cenario B a usa de verdade (o
# httpx do modulo handoff e substituido por um fake - nunca sai da maquina);
# precisa ser nao-vazia para o guard `if not BIA_V2_CRM_API_KEY` de
# handoff.py nao recusar antes mesmo de chegar no fake.
os.environ["BIA_V2_CRM_API_KEY"] = "chave-de-teste-e2e"
os.environ["CRM_BASE_URL"] = "http://crm-de-teste.invalido"
_WEBHOOK_AUTH_VALUE_TESTE = "segredo-teste-v2-e2e-bia"
os.environ["V2_AGENT_WEBHOOK_AUTH_VALUE"] = _WEBHOOK_AUTH_VALUE_TESTE

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


import httpx  # noqa: E402

# Modelos com relationship() por NOME precisam estar registrados antes de
# qualquer query (mesmo padrao de tests/test_v2_pipeline.py e test_v2_handoff.py).
import app.models.media_asset  # noqa: E402,F401
import app.models.note  # noqa: E402,F401
import app.models.tag  # noqa: E402,F401
import app.v2.comunicador as comunicador_mod  # noqa: E402
import app.v2.handoff as handoff_mod  # noqa: E402
import app.v2.interpretador as interpretador_mod  # noqa: E402
import app.v2.pipeline as pipeline_mod  # noqa: E402
from app.auth import User  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402
from app.models.handoff_op import HandoffOperation, StatusHandoff  # noqa: E402
from app.v2.eventos_validacao import OrigemEvento, TipoEvento  # noqa: E402
from app.v2.pipeline import EtapaFinal  # noqa: E402

Base.metadata.create_all(
    bind=engine,
    tables=[
        Conversation.__table__, Message.__table__, ConversationEvent.__table__,
        HandoffOperation.__table__, User.__table__,
    ],
)

db = SessionLocal()
_contador = iter(range(1, 100000))


# ---------------------------------------------------------------------------
# Fronteira de IA substituida - SEMPRE, para os 7 cenarios (mesmo padrao de
# tests/test_v2_pipeline.py). `comunicador_modo="eco"` devolve o PROPRIO
# prompt como resposta - usado so no Cenario A, para provar (sem adivinhar a
# redacao de um LLM de verdade) que os campos que faltam realmente chegaram
# na entrada do comunicador e sairiam mencionados na mensagem final.
# ---------------------------------------------------------------------------
_estado_ia = {
    "interpretador_resposta": None,
    "interpretador_gancho": None,
    "interpretador_chamadas": 0,
    "comunicador_modo": "fixo",
    "comunicador_resposta": "Oi! Recebi sua mensagem.",
    "comunicador_chamadas": 0,
    "comunicador_ultimo_prompt": None,
}


def _fake_chamar_llm(mensagem):
    _estado_ia["interpretador_chamadas"] += 1
    if _estado_ia["interpretador_gancho"] is not None:
        _estado_ia["interpretador_gancho"]()
    return json.dumps(_estado_ia["interpretador_resposta"])


def _fake_chamar_gemini(prompt):
    _estado_ia["comunicador_chamadas"] += 1
    _estado_ia["comunicador_ultimo_prompt"] = prompt
    if _estado_ia["comunicador_modo"] == "eco":
        return prompt
    return _estado_ia["comunicador_resposta"]


interpretador_mod._chamar_llm = _fake_chamar_llm
comunicador_mod._chamar_gemini = _fake_chamar_gemini


def _configurar_ia(*, explicit_human_request, extracted=None, duvidas=None,
                    gancho_interpretador=None, comunicador_modo="fixo",
                    resposta_comunicador="Oi! Recebi sua mensagem."):
    _estado_ia["interpretador_resposta"] = {
        "intent": None,
        "explicit_human_request": explicit_human_request,
        "extracted": extracted or {},
        "duvidas": duvidas or [],
    }
    _estado_ia["interpretador_gancho"] = gancho_interpretador
    _estado_ia["interpretador_chamadas"] = 0
    _estado_ia["comunicador_modo"] = comunicador_modo
    _estado_ia["comunicador_resposta"] = resposta_comunicador
    _estado_ia["comunicador_chamadas"] = 0
    _estado_ia["comunicador_ultimo_prompt"] = None


# ---------------------------------------------------------------------------
# Fixtures e helpers - mesmo estilo de tests/test_v2_pipeline.py / test_v2_handoff.py.
# ---------------------------------------------------------------------------
def criar_conversa(*, state="AI_TRIAGE", state_version=0, is_bot_active=True, atendente_id=None):
    n = next(_contador)
    conv = Conversation(
        lead_id=6000 + n,
        whatsapp=f"5511901{n:06d}",
        status="aberta",
        unread_count=0,
        is_bot_active=is_bot_active,
        atendente_id=atendente_id,
        last_customer_msg_at=datetime.now(timezone.utc),
        state=state,
        state_version=state_version,
    )
    db.add(conv)
    db.commit()
    return conv.id


def criar_atendente_ativo(nome="Atendente E2E"):
    """Usuario ATIVO unico - com `ATENDENTES_ELEGIVEIS=""` (default), a regra
    de menor-carga-primeiro (app/services/atendimento.py) devolve este id
    direto assim que ele for o UNICO elegivel (`len(elegiveis) == 1`), sem
    nem precisar computar carga - determinismo simples, sem depender de
    quantas conversas ja foram atribuidas a ele por outro cenario."""
    n = next(_contador)
    user = User(
        nome=nome, email=f"atendente_e2e_{n}@teste.com", hashed_password="hash-fake-e2e",
        role="USER", is_active=True, email_verified=True,
    )
    db.add(user)
    db.commit()
    return user.id


_COLUNAS_SNAPSHOT = (
    "state", "state_version", "is_bot_active", "atendente_id", "responsavel_id",
    "queued_at", "primeira_resposta_humana_at", "status", "closed_at",
)


def snapshot_conversa(cid):
    """Le por uma SESSAO NOVA: so enxerga o que foi COMITADO."""
    sessao = SessionLocal()
    try:
        c = sessao.get(Conversation, cid)
        return {campo: getattr(c, campo) for campo in _COLUNAS_SNAPSHOT}
    finally:
        sessao.close()


def contar_conversas():
    sessao = SessionLocal()
    try:
        return sessao.query(Conversation).count()
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


def trilha_tipos(cid):
    sessao = SessionLocal()
    try:
        return [
            e.event_type
            for e in sessao.query(ConversationEvent).filter_by(conversation_id=cid)
            .order_by(ConversationEvent.id).all()
        ]
    finally:
        sessao.close()


def evento_mais_recente(cid, tipo):
    sessao = SessionLocal()
    try:
        e = (
            sessao.query(ConversationEvent)
            .filter_by(conversation_id=cid, event_type=tipo.value)
            .order_by(ConversationEvent.id.desc())
            .first()
        )
        if e is None:
            return None
        return SimpleNamespace(error_code=e.error_code, action=e.action)
    finally:
        sessao.close()


def ler_operacao_handoff(cid):
    sessao = SessionLocal()
    try:
        op = (
            sessao.query(HandoffOperation)
            .filter_by(conversation_id=cid)
            .order_by(HandoffOperation.id.desc())
            .first()
        )
        if op is None:
            return None
        return SimpleNamespace(status=op.status, error_code=op.error_code, target_user_id=op.target_user_id)
    finally:
        sessao.close()


def contar_handoff_operations():
    sessao = SessionLocal()
    try:
        return sessao.query(HandoffOperation).count()
    finally:
        sessao.close()


def _payload_http(cid, *, mensagem="Ola.", event_id=None):
    """Corpo JSON puro (mesmo formato de `_payload_http` de
    tests/test_v2_pipeline.py) - o alvo aqui e a rota HTTP real, entao o
    corpo vira JSON de verdade, exatamente como um chamador real mandaria."""
    corpo = {
        "conversation_id": cid,
        "origem": OrigemEvento.SINTETICO.value,
        "lote": [{"msg_type": "text", "content": mensagem}],
        "mensagem_interpretacao": mensagem,
    }
    if event_id is not None:
        corpo["event_id"] = event_id
    return corpo


# Triagem completa - mesma forma ja provada em tests/test_v2_pipeline.py
# (Cenario 7): nome/email presentes, destino com identidade + `dias` (modo
# "dias", datas_definidas=False), contagem de pessoas coerente (2 = 2+0).
# Reusada pelos Cenarios B, C e G (a propria tarefa pede isso para C e G).
_TRIAGEM_COMPLETA = {
    "nome": "Maria", "email": "maria@x.com", "total_pessoas": 2, "adultos": 2,
    "criancas": 0, "datas_definidas": False,
    "destinos": [{"destino": "Atacama", "dias": 5}],
}

AUTH_HEADERS = {"X-Webhook-Auth": _WEBHOOK_AUTH_VALUE_TESTE}

from app.main import app as fastapi_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

http_client = TestClient(fastapi_app)

print("=== BIA-V2 - E2E local (Cenarios A-G) ===\n")


# ===========================================================================
# CENARIO A - Triagem incompleta -> pedido de campo faltante.
#
# So "nome" e "destino" sao extraidos (email/contagens/datas-ou-dias ficam de
# fora, `datas_definidas` nunca informado) -> `avaliar_triagem` devolve
# `completa=False`, e sem pedido explicito de humano o pipeline NUNCA chega
# perto de `selecionar_atendente` - segue direto para o comunicador com
# `acao="solicitar_campos_faltantes"`. `comunicador_modo="eco"` devolve o
# PROPRIO prompt como resposta final: `_montar_prompt` (comunicador.py)
# escreve "Campos que ainda faltam: <lista>" quando a acao e essa - entao os
# nomes dos campos aparecerem na resposta HTTP final PROVA que a lista
# calculada pela triagem atravessou comunicador+guard sem se perder, sem
# este teste precisar adivinhar a redacao de um LLM de verdade.
# ===========================================================================
_configurar_ia(
    explicit_human_request=False,
    extracted={"nome": "Carlos", "destinos": [{"destino": "Atacama"}]},
    comunicador_modo="eco",
)
cidA = criar_conversa()
snapA_antes = snapshot_conversa(cidA)
rA = http_client.post(
    "/webhook/agent-bia-v2",
    json=_payload_http(cidA, mensagem="Oi, sou o Carlos e quero ir pro Atacama."),
    headers=AUTH_HEADERS,
)
check(rA.status_code == 200, f"A1. 200 de sucesso (veio {rA.status_code}: {rA.text[:200]})")
corpoA = rA.json()
check(
    corpoA.get("etapa_final") == EtapaFinal.RESPOSTA_GERADA.value,
    f"A2. desfecho RESPOSTA_GERADA - nao avancou para handoff (obteve {corpoA.get('etapa_final')})",
)
_campos_faltantes_esperados_A = ["email", "total_pessoas", "adultos", "criancas", "datas_definidas"]
_texto_final_A = corpoA.get("texto_resposta") or ""
check(
    "Acao a comunicar: solicitar_campos_faltantes" in _texto_final_A,
    f"A3. acao repassada ao comunicador foi solicitar_campos_faltantes (texto={_texto_final_A!r})",
)
check(
    all(campo in _texto_final_A for campo in _campos_faltantes_esperados_A),
    f"A4. a resposta final menciona TODOS os campos que faltam {_campos_faltantes_esperados_A} "
    f"(texto={_texto_final_A!r})",
)
check(
    trilha_tipos(cidA) == [
        TipoEvento.MESSAGE_RECEIVED.value, TipoEvento.AI_INTERPRETATION_STARTED.value,
        TipoEvento.AI_INTERPRETATION_COMPLETED.value, TipoEvento.TRIAGE_DATA_UPDATED.value,
        TipoEvento.AI_RESPONSE_GENERATED.value,
    ],
    f"A5. trilha sem NENHUM evento de handoff - nunca chegou perto (obteve {trilha_tipos(cidA)})",
)
check(snapshot_conversa(cidA) == snapA_antes, "A6. NENHUMA coluna de conversations mudou")


# ===========================================================================
# CENARIO B - Triagem completa -> handoff -> WAITING_HUMAN -> handoff
# concluido, em V2_MODE="live".
#
# DECISAO/AMBIGUIDADE RESOLVIDA: `explicit_human_request=False` de proposito
# - isola o gatilho de "triagem completa" (`resultado_triagem.completa`) do
# gatilho de "pedido explicito", que ja e o unico caminho exercitado pelos
# testes HTTP existentes (test_v2_pipeline.py, Cenario 15). Handoff bem
# sucedido faz `executar_pipeline` retornar IMEDIATAMENTE com
# `HANDOFF_CONCLUIDO` (pipeline.py, passo 9b: "a funcao PARA aqui, SEM
# chamar o gate/comunicador... mandar a IA falar depois disso e exatamente o
# que o gate recusaria (BOT_INATIVO)") - por isso a prova de "handoff bem
# sucedido" AQUI e o proprio `etapa_final=handoff_concluido` + a conversa em
# WAITING_HUMAN + o comunicador NUNCA chamado, e nao um texto de mensagem
# "autorizada pelo gate" (nao existe texto nenhum neste ramo - o gate nem
# chega a rodar). Ver o relatorio desta tarefa para a explicacao completa.
#
# O CRM e substituido por um fake HTTP minimo (so o caminho feliz - as
# adversidades do CRM ja sao cobertas exaustivamente por
# tests/test_v2_handoff.py), mesmo padrao daquele arquivo: troca o ATRIBUTO
# `httpx` do MODULO handoff.py, nunca o httpx global do processo.
# ===========================================================================
class _RespostaCRMFalsa:
    def __init__(self, status_code, corpo):
        self.status_code = status_code
        self._corpo = corpo

    def json(self):
        return self._corpo


class _CRMFalsoB:
    """So o caminho feliz: sempre 200, ecoando o corpo com replayed=False."""

    def __init__(self):
        self.chamadas = []

    def post(self, url, json=None, headers=None):
        self.chamadas.append({"url": url, "corpo": json, "headers": headers or {}})
        return _RespostaCRMFalsa(200, {**json, "replayed": False})


CRM_B = _CRMFalsoB()


class _ClientCRMFalso:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        return CRM_B.post(*args, **kwargs)


atendente_b_id = criar_atendente_ativo("Atendente Cenario B")
_httpx_original_handoff = handoff_mod.httpx
_v2_mode_original = pipeline_mod.V2_MODE
handoff_mod.httpx = SimpleNamespace(
    Client=_ClientCRMFalso, RequestError=httpx.RequestError, Timeout=httpx.Timeout,
)
pipeline_mod.V2_MODE = "live"
try:
    _configurar_ia(explicit_human_request=False, extracted=_TRIAGEM_COMPLETA)
    cidB = criar_conversa()
    rB = http_client.post(
        "/webhook/agent-bia-v2",
        json=_payload_http(cidB, mensagem="Meu nome e Maria, meu email e maria@x.com, "
                                          "somos 2 adultos indo 5 dias pro Atacama."),
        headers=AUTH_HEADERS,
    )
finally:
    handoff_mod.httpx = _httpx_original_handoff
    pipeline_mod.V2_MODE = _v2_mode_original

check(rB.status_code == 200, f"B1. 200 de sucesso (veio {rB.status_code}: {rB.text[:200]})")
corpoB = rB.json()
check(
    corpoB.get("etapa_final") == EtapaFinal.HANDOFF_CONCLUIDO.value and corpoB.get("atendente_id") == atendente_b_id,
    f"B2. desfecho HANDOFF_CONCLUIDO com o atendente elegivel encontrado (obteve {corpoB})",
)
snapB = snapshot_conversa(cidB)
check(snapB["state"] == "WAITING_HUMAN", f"B3. conversa transicionou para WAITING_HUMAN (state={snapB['state']!r})")
check(snapB["state_version"] == 1, f"B4. state_version incrementou exatamente 1x (={snapB['state_version']})")
check(
    snapB["is_bot_active"] is False and snapB["atendente_id"] == atendente_b_id
    and snapB["responsavel_id"] == atendente_b_id and snapB["queued_at"] is not None
    and snapB["primeira_resposta_humana_at"] is None and snapB["status"] == "aberta",
    f"B5. as sete invariantes de WAITING_HUMAN valem (snapshot={snapB})",
)
opB = ler_operacao_handoff(cidB)
check(
    opB is not None and opB.status == StatusHandoff.COMPLETED.value and opB.target_user_id == atendente_b_id,
    f"B6. handoff_operations COMPLETED com o atendente correto (obteve {opB})",
)
check(
    _estado_ia["comunicador_chamadas"] == 0,
    "B7. comunicador NUNCA chamado - handoff bem sucedido para antes do gate/comunicador (ver docstring do Cenario B)",
)
check(len(CRM_B.chamadas) == 1, f"B8. o CRM (fake) foi chamado exatamente 1x ({len(CRM_B.chamadas)})")
check(
    CRM_B.chamadas[0]["corpo"] == {"event_id": CRM_B.chamadas[0]["corpo"]["event_id"],
                                     "lead_id": CRM_B.chamadas[0]["corpo"]["lead_id"],
                                     "target_user_id": atendente_b_id},
    f"B9. corpo enviado ao CRM segue o contrato (target_user_id correto) ({CRM_B.chamadas[0]['corpo']})",
)


# ===========================================================================
# CENARIO C - Sem humano disponivel -> permanece em AI_TRIAGE.
#
# Mesma triagem completa do Cenario B (pedido explicito da tarefa). Em vez de
# depender de "tabela users vazia" (o que quebraria se este cenario rodasse
# DEPOIS de B ou G no MESMO processo/banco, ja que ambos criam um usuario
# ativo), `selecionar_atendente` e substituida para devolver None - a mesma
# alternativa que a propria tarefa autoriza ("ou selecionar_atendente
# retornando None via fake"). Isola o comportamento de "ninguem elegivel"
# sem acoplar este cenario a ordem de execucao dos outros.
# ===========================================================================
_selecionar_atendente_original = pipeline_mod.selecionar_atendente
pipeline_mod.selecionar_atendente = lambda _db: None
try:
    _configurar_ia(explicit_human_request=False, extracted=_TRIAGEM_COMPLETA)
    cidC = criar_conversa()
    snapC_antes = snapshot_conversa(cidC)
    rC = http_client.post(
        "/webhook/agent-bia-v2",
        json=_payload_http(cidC, mensagem="Meu nome e Maria, meu email e maria@x.com, "
                                          "somos 2 adultos indo 5 dias pro Atacama."),
        headers=AUTH_HEADERS,
    )
finally:
    pipeline_mod.selecionar_atendente = _selecionar_atendente_original

check(rC.status_code == 200, f"C1. 200 de sucesso (veio {rC.status_code}: {rC.text[:200]})")
corpoC = rC.json()
check(
    corpoC.get("etapa_final") == EtapaFinal.RESPOSTA_GERADA.value,
    f"C2. sem atendente, cai para o comunicador em vez de travar (obteve {corpoC.get('etapa_final')})",
)
check(snapshot_conversa(cidC) == snapC_antes, "C3. NENHUMA coluna de conversations mudou (nunca saiu de AI_TRIAGE)")
_evt_hf_C = evento_mais_recente(cidC, TipoEvento.HANDOFF_FAILED)
check(
    contar_eventos(cidC, TipoEvento.HANDOFF_FAILED) == 1
    and _evt_hf_C is not None and _evt_hf_C.error_code == "SEM_ATENDENTE_ELEGIVEL",
    f"C4. exatamente 1 HANDOFF_FAILED com error_code=SEM_ATENDENTE_ELEGIVEL (obteve {_evt_hf_C})",
)
check(
    contar_eventos(cidC, TipoEvento.HANDOFF_REQUESTED) == 0 and contar_eventos(cidC, TipoEvento.HANDOFF_COMPLETED) == 0,
    "C5. ZERO HANDOFF_REQUESTED/HANDOFF_COMPLETED - a selecao nunca chegou a valer",
)
check(ler_operacao_handoff(cidC) is None, "C6. ZERO linha em handoff_operations para esta conversa")


# ===========================================================================
# CENARIO D - Humano assume no meio do fluxo -> resposta da IA e descartada.
#
# O "segundo writer" muda `state_version` E `atendente_id` (como a tarefa
# pede) por uma SESSAO SEPARADA, DENTRO da fronteira do interpretador (mesma
# tecnica de tests/test_v2_pipeline.py Cenario 8) - simula um humano
# assumindo a conversa enquanto a IA "pensa". O gate (`autorizar_envio`)
# checa `state_version` ANTES de `atendente_id` (app/v2/gate.py, "AS CINCO
# CONDICOES... EM ORDEM") - por isso o error_code esperado e
# STATE_VERSION_DIVERGENTE, mesmo com as duas colunas divergindo ao mesmo
# tempo; so a PRIMEIRA condicao que falha decide o error_code.
# ===========================================================================
cidD = criar_conversa(state_version=0)


def _humano_assume_no_meio_D():
    outra = SessionLocal()
    try:
        conv = outra.get(Conversation, cidD)
        conv.state_version = 1
        conv.atendente_id = 999999
        outra.commit()
    finally:
        outra.close()


_configurar_ia(explicit_human_request=False, gancho_interpretador=_humano_assume_no_meio_D)
rD = http_client.post("/webhook/agent-bia-v2", json=_payload_http(cidD), headers=AUTH_HEADERS)

check(rD.status_code == 200, f"D1. o endpoint ainda devolve 200 (desfecho de negocio, nao erro) (veio {rD.status_code})")
corpoD = rD.json()
check(
    corpoD.get("etapa_final") == EtapaFinal.RESPOSTA_DESCARTADA_GATE.value and corpoD.get("texto_resposta") is None,
    f"D2. desfecho RESPOSTA_DESCARTADA_GATE, sem texto nenhum saindo (obteve {corpoD})",
)
check(
    _estado_ia["comunicador_chamadas"] == 0,
    "D3. comunicador NUNCA chamado - o gate recusou antes de chegar la",
)
check(
    contar_eventos(cidD, TipoEvento.AI_RESPONSE_DISCARDED) == 1,
    "D4. exatamente 1 AI_RESPONSE_DISCARDED",
)
_evt_gate_D = evento_mais_recente(cidD, TipoEvento.AI_RESPONSE_DISCARDED)
check(
    _evt_gate_D is not None and _evt_gate_D.error_code == "STATE_VERSION_DIVERGENTE",
    f"D5. error_code=STATE_VERSION_DIVERGENTE - a primeira das cinco condicoes do gate a falhar (obteve {_evt_gate_D})",
)
snapD = snapshot_conversa(cidD)
check(
    snapD["state_version"] == 1 and snapD["atendente_id"] == 999999,
    f"D6. o valor REAL (do segundo writer) e o que prevalece - a IA nunca sobrescreve por cima (snapshot={snapD})",
)


# ===========================================================================
# CENARIO E - Preco no output -> bloqueado pelo output_guard.
# ===========================================================================
_configurar_ia(
    explicit_human_request=False,
    comunicador_modo="fixo",
    resposta_comunicador="O pacote custa 5000 reais.",
)
cidE = criar_conversa()
snapE_antes = snapshot_conversa(cidE)
rE = http_client.post("/webhook/agent-bia-v2", json=_payload_http(cidE), headers=AUTH_HEADERS)

check(rE.status_code == 200, f"E1. 200 de sucesso HTTP (o bloqueio e um desfecho de negocio) (veio {rE.status_code})")
corpoE = rE.json()
check(
    corpoE.get("etapa_final") == EtapaFinal.RESPOSTA_BLOQUEADA_GUARD.value and corpoE.get("texto_resposta") is None,
    f"E2. desfecho RESPOSTA_BLOQUEADA_GUARD, valor monetario NUNCA vaza na resposta (obteve {corpoE})",
)
check("5000" not in rE.text, f"E3. o valor monetario original nao aparece em NENHUM lugar do corpo HTTP ({rE.text[:200]})")
_evt_guard_E = evento_mais_recente(cidE, TipoEvento.AI_RESPONSE_DISCARDED)
check(
    _evt_guard_E is not None and _evt_guard_E.error_code == "VALOR_MONETARIO_DETECTADO",
    f"E4. error_code=VALOR_MONETARIO_DETECTADO (obteve {_evt_guard_E})",
)
check(snapshot_conversa(cidE) == snapE_antes, "E5. NENHUMA coluna de conversations mudou")


# ===========================================================================
# CENARIO F - Replay/evento duplicado -> sem efeito duplicado.
# ===========================================================================
_configurar_ia(explicit_human_request=False)
cidF = criar_conversa()
evF = str(uuid.uuid4())
rF1 = http_client.post(
    "/webhook/agent-bia-v2", json=_payload_http(cidF, event_id=evF), headers=AUTH_HEADERS
)
check(rF1.status_code == 200, f"F1. primeira chamada com event_id novo da 200 (veio {rF1.status_code})")
snapF_apos_primeira = snapshot_conversa(cidF)
_chamadas_interpretador_apos_F1 = _estado_ia["interpretador_chamadas"]

rF2 = http_client.post(
    "/webhook/agent-bia-v2", json=_payload_http(cidF, event_id=evF), headers=AUTH_HEADERS
)
check(
    rF2.status_code == 409 and rF2.json().get("event_id") == evF,
    f"F2. replay do MESMO event_id da 409 (EventoDuplicado) (veio {rF2.status_code}: {rF2.text[:200]})",
)
check(
    contar_eventos(cidF, TipoEvento.MESSAGE_RECEIVED) == 1,
    "F3. a segunda chamada NAO produziu um segundo MESSAGE_RECEIVED",
)
check(
    snapshot_conversa(cidF) == snapF_apos_primeira,
    "F4. NENHUMA coluna de conversations mudou entre a 1a e a 2a chamada (zero efeito duplicado)",
)
check(
    _estado_ia["interpretador_chamadas"] == _chamadas_interpretador_apos_F1,
    "F5. a IA Interpretadora NAO foi chamada de novo - o replay e barrado ANTES da fronteira de IA",
)


# ===========================================================================
# CENARIO G - Dry-run -> zero efeitos colaterais externos reais.
#
# Reusa o setup de B (mesma triagem completa, mesmo atendente ja ativo -
# len(elegiveis) == 1 devolve o mesmo id independente de carga previa), mas
# em V2_MODE default ("dry_run") e SEM substituir a chamada ao CRM: se o
# pipeline tentasse mesmo assim chamar `handoff_to_human` de verdade, cairia
# no httpx REAL e falharia contra "http://crm-de-teste.invalido" - o teste
# nao dependeria so da propria asserção para pegar isso. Alem disso,
# substitui `pipeline.handoff_to_human` por um fake que levanta
# AssertionError se for chamado, e conta chamadas - prova redundante,
# explicita, exigida pela propria tarefa.
# ===========================================================================
def _handoff_jamais_deveria_ser_chamado_G(*_args, **_kwargs):
    raise AssertionError("Cenario G: handoff_to_human NAO deveria ser chamado em modo dry_run")


_handoff_original_G = pipeline_mod.handoff_to_human
_handoff_chamadas_G = {"n": 0}


def _guard_handoff_G(*args, **kwargs):
    _handoff_chamadas_G["n"] += 1
    return _handoff_jamais_deveria_ser_chamado_G(*args, **kwargs)


pipeline_mod.handoff_to_human = _guard_handoff_G
_handoff_ops_antes_G = contar_handoff_operations()
_conversas_antes_G = contar_conversas()
try:
    _configurar_ia(explicit_human_request=False, extracted=_TRIAGEM_COMPLETA)
    cidG = criar_conversa()
    snapG_antes = snapshot_conversa(cidG)
    rG = http_client.post(
        "/webhook/agent-bia-v2",
        json=_payload_http(cidG, mensagem="Meu nome e Maria, meu email e maria@x.com, "
                                          "somos 2 adultos indo 5 dias pro Atacama."),
        headers=AUTH_HEADERS,
    )
finally:
    pipeline_mod.handoff_to_human = _handoff_original_G

check(rG.status_code == 200, f"G1. 200 de sucesso (veio {rG.status_code}: {rG.text[:200]})")
corpoG = rG.json()
check(
    corpoG.get("etapa_final") == EtapaFinal.HANDOFF_SIMULADO_DRY_RUN.value and corpoG.get("atendente_id") == atendente_b_id,
    f"G2. desfecho HANDOFF_SIMULADO_DRY_RUN com o atendente elegivel encontrado (obteve {corpoG})",
)
check(
    _handoff_chamadas_G["n"] == 0,
    f"G3. handoff_to_human REAL nunca foi chamado ({_handoff_chamadas_G['n']} chamadas)",
)
check(
    snapshot_conversa(cidG) == snapG_antes,
    f"G4. ZERO mudanca em conversations para esta conversa (antes={snapG_antes}, depois={snapshot_conversa(cidG)})",
)
check(
    contar_conversas() == _conversas_antes_G + 1,
    "G5. a UNICA linha nova em conversations e a que este proprio teste criou (fixture), nenhuma outra",
)
check(
    contar_handoff_operations() == _handoff_ops_antes_G,
    f"G6. ZERO linha nova em handoff_operations (antes={_handoff_ops_antes_G}, depois={contar_handoff_operations()})",
)
check(
    _estado_ia["comunicador_chamadas"] == 0,
    "G7. comunicador NUNCA chamado - dry_run tambem para antes do gate/comunicador, igual ao live bem sucedido",
)


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
