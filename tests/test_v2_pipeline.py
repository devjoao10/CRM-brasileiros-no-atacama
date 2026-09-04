"""
BIA-V2 Fase 10 - pipeline + endpoint (`conversas/app/v2/pipeline.py`,
`conversas/app/routers/v2_agent.py`).

Payload sintetico percorrendo o pipeline inteiro. Dry_run e o modo dos
Cenarios 1-13 (ver docstring de pipeline.py, bloco "DRY_RUN x LIVE"): prova o
contrato da Fase 10 - a cadeia de eventos esperada e gerada, NENHUMA linha de
`conversations` e escrita, e ZERO linha aparece em `handoff_operations`
nesses cenarios. O Cenario 14 e a excecao deliberada: regressao do Fix 2
(revisao pos-Fase-10, achado do code-reviewer) em modo "live", com
`handoff_to_human` TAMBEM substituida - a funcao REAL nunca roda, entao a
garantia de zero efeito real se mantem (reconfirmado no Cenario 16).

FRONTEIRA SUBSTITUIDA: a chamada de rede ao Gemini
(`interpretador._chamar_llm` / `comunicador._chamar_gemini`) - mesmo padrao
ja documentado nos proprios modulos e usado por outras fases (nenhum teste
deste projeto bate rede real, `.claude/rules/05-testing.md`) - e, SO no
Cenario 14, `handoff_to_human` (ver ali o porque). `selecionar_atendente` e
temporariamente substituida nos cenarios que precisam de um atendente
"encontrado" (Cenarios 7 e 14) - nos demais, a funcao REAL roda contra uma
tabela `users` vazia e devolve None organicamente (comportamento documentado
de `atendentes_elegiveis`/`resolver_atendente_elegivel`,
`app/services/atendimento.py` - sem usuario ativo configurado, devolve `[]`).

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Cenario 15 e a camada HTTP de verdade (`fastapi.testclient.TestClient` contra
o app real - Fix 5 da revisao pos-Fase-10: nenhum cenario 1-14 passa pelo
router/FastAPI, so por chamada direta a `executar_pipeline()`, e foi assim
que o achado do Fix 1 escapou da bateria original). Tambem pina o Fix 6
(`event_id=0`/`False` rejeitados pelo Pydantic ANTES de `CampoInvalido` - 422
com corpo diferente do 422 de dominio). Precisa de
`V2_AGENT_WEBHOOK_AUTH_VALUE` (Fix 3) configurada no ambiente do teste - ver
`os.environ` logo abaixo.

Roda standalone:  python tests/test_v2_pipeline.py
"""
import json
import os
import pathlib
import sys
import uuid
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_pipeline_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
os.environ["BIA_V2_CRM_API_KEY"] = ""  # dry_run nunca chama o CRM; nao importa aqui.
os.environ.setdefault("V2_MODE", "dry_run")
_WEBHOOK_AUTH_VALUE_TESTE = "segredo-teste-v2-agent-webhook"
os.environ["V2_AGENT_WEBHOOK_AUTH_VALUE"] = _WEBHOOK_AUTH_VALUE_TESTE  # Fix 3 - credencial da rota HTTP

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


# Modelos com relationship() por NOME precisam estar registrados antes de
# qualquer query (mesmo padrao de tests/test_v2_handoff.py e test_v2_gate.py).
import app.models.media_asset  # noqa: E402,F401
import app.models.note  # noqa: E402,F401
import app.models.tag  # noqa: E402,F401
import app.v2.comunicador as comunicador_mod  # noqa: E402
import app.v2.interpretador as interpretador_mod  # noqa: E402
import app.v2.pipeline as pipeline_mod  # noqa: E402
from app.auth import User  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402
from app.models.handoff_op import HandoffOperation  # noqa: E402
from app.v2.contratos import HandoffResult  # noqa: E402
from app.v2.eventos import EventoDuplicado  # noqa: E402
from app.v2.eventos_validacao import OrigemEvento, TipoEvento  # noqa: E402
from app.v2.pipeline import (  # noqa: E402
    ConversationInvalidaParaV2,
    EtapaFinal,
    InboundContextV2,
    executar_pipeline,
)
from app.v2.prefiltro import MensagemLote  # noqa: E402

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
# Fronteira de IA substituida - ver docstring do modulo. Configuravel por
# cenario via `_configurar_ia`; contadores provam quantas vezes cada
# fronteira foi de fato chamada.
# ---------------------------------------------------------------------------
_estado_ia = {
    "interpretador_resposta": None,
    "interpretador_excecao": None,
    "interpretador_chamadas": 0,
    "interpretador_gancho": None,
    "comunicador_resposta": "Oi! Recebi sua mensagem.",
    "comunicador_chamadas": 0,
}


def _fake_chamar_llm(mensagem):
    _estado_ia["interpretador_chamadas"] += 1
    if _estado_ia["interpretador_gancho"] is not None:
        _estado_ia["interpretador_gancho"]()
    if _estado_ia["interpretador_excecao"] is not None:
        raise _estado_ia["interpretador_excecao"]
    return json.dumps(_estado_ia["interpretador_resposta"])


def _fake_chamar_gemini(prompt):
    _estado_ia["comunicador_chamadas"] += 1
    return _estado_ia["comunicador_resposta"]


interpretador_mod._chamar_llm = _fake_chamar_llm
comunicador_mod._chamar_gemini = _fake_chamar_gemini


def _configurar_ia(*, resposta_interpretador=None, excecao_interpretador=None,
                    gancho_interpretador=None, resposta_comunicador="Oi! Recebi sua mensagem."):
    _estado_ia["interpretador_resposta"] = resposta_interpretador
    _estado_ia["interpretador_excecao"] = excecao_interpretador
    _estado_ia["interpretador_gancho"] = gancho_interpretador
    _estado_ia["interpretador_chamadas"] = 0
    _estado_ia["comunicador_resposta"] = resposta_comunicador
    _estado_ia["comunicador_chamadas"] = 0


def _resposta_ia(*, explicit_human_request, extracted=None, duvidas=None, intent=None):
    return {
        "intent": intent,
        "explicit_human_request": explicit_human_request,
        "extracted": extracted or {},
        "duvidas": duvidas or [],
    }


# ---------------------------------------------------------------------------
# Fixtures e helpers - mesmo estilo de tests/test_v2_gate.py / test_v2_handoff.py.
# ---------------------------------------------------------------------------
def criar_conversa(*, state="AI_TRIAGE", state_version=0, is_bot_active=True, atendente_id=None):
    n = next(_contador)
    conv = Conversation(
        lead_id=7000 + n,
        whatsapp=f"5511900{n:06d}",
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


def _entrada(cid, *, lote=None, mensagem="Ola, meu nome e Joao.", event_id=None):
    return InboundContextV2(
        conversation_id=cid,
        origem=OrigemEvento.SINTETICO,
        lote=lote if lote is not None else [MensagemLote(msg_type="text", content=mensagem)],
        mensagem_interpretacao=mensagem,
        event_id=event_id,
    )


print("=== Fase 10 - pipeline + endpoint (dry_run) ===\n")

# ===========================================================================
# 1. CENARIO PRINCIPAL - percorre o pipeline inteiro: pedido explicito de
#    humano, SEM atendente elegivel (tabela `users` vazia) -> HANDOFF_FAILED
#    -> segue para o comunicador -> RESPOSTA_GERADA. Prova a cadeia de
#    eventos esperada de ponta a ponta e ausencia total de efeito real.
# ===========================================================================
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=True))
cid1 = criar_conversa()
snap1_antes = snapshot_conversa(cid1)
res1 = executar_pipeline(db, _entrada(cid1, mensagem="Quero falar com um atendente."))

check(res1.etapa_final == EtapaFinal.RESPOSTA_GERADA, f"1a. desfecho RESPOSTA_GERADA (obteve {res1.etapa_final})")
check(res1.texto_resposta == "Oi! Recebi sua mensagem.", f"1b. texto da IA repassado ({res1.texto_resposta!r})")
check(
    trilha_tipos(cid1) == [
        TipoEvento.MESSAGE_RECEIVED.value,
        TipoEvento.AI_INTERPRETATION_STARTED.value,
        TipoEvento.AI_INTERPRETATION_COMPLETED.value,
        TipoEvento.TRIAGE_DATA_UPDATED.value,
        TipoEvento.HANDOFF_FAILED.value,
        TipoEvento.AI_RESPONSE_GENERATED.value,
    ],
    f"1c. cadeia de eventos esperada, na ordem (obteve {trilha_tipos(cid1)})",
)
check(_estado_ia["interpretador_chamadas"] == 1, "1d. interpretador chamado exatamente 1x")
check(_estado_ia["comunicador_chamadas"] == 1, "1e. comunicador chamado exatamente 1x")
check(
    contar_eventos(cid1, TipoEvento.HANDOFF_FAILED) == 1,
    "1f. exatamente 1 HANDOFF_FAILED (sem atendente elegivel)",
)
_sessao_hf1 = SessionLocal()
_evt_hf1 = (
    _sessao_hf1.query(ConversationEvent)
    .filter_by(conversation_id=cid1, event_type=TipoEvento.HANDOFF_FAILED.value).first()
)
check(
    _evt_hf1 is not None and _evt_hf1.error_code == "SEM_ATENDENTE_ELEGIVEL",
    f"1g. error_code=SEM_ATENDENTE_ELEGIVEL, token literal do R5 do plano (obteve {_evt_hf1 and _evt_hf1.error_code})",
)
_sessao_hf1.close()
check(
    snapshot_conversa(cid1) == snap1_antes,
    f"1h. NENHUMA coluna de conversations mudou (antes={snap1_antes}, depois={snapshot_conversa(cid1)})",
)
check(
    db.query(HandoffOperation).filter_by(conversation_id=cid1).count() == 0,
    "1i. ZERO linha em handoff_operations",
)


# ===========================================================================
# 2. Pre-filtro ignora (reacao isolada) -> MESSAGE_IGNORED, IA nunca chamada.
# ===========================================================================
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=False))
cid2 = criar_conversa()
snap2_antes = snapshot_conversa(cid2)
res2 = executar_pipeline(db, _entrada(cid2, lote=[MensagemLote(msg_type="reaction", content="")]))

check(res2.etapa_final == EtapaFinal.MENSAGEM_IGNORADA, f"2a. desfecho MENSAGEM_IGNORADA (obteve {res2.etapa_final})")
check(_estado_ia["interpretador_chamadas"] == 0, "2b. interpretador NUNCA chamado")
check(_estado_ia["comunicador_chamadas"] == 0, "2c. comunicador NUNCA chamado")
check(trilha_tipos(cid2) == [TipoEvento.MESSAGE_RECEIVED.value, TipoEvento.MESSAGE_IGNORED.value],
      f"2d. trilha = MESSAGE_RECEIVED + MESSAGE_IGNORED (obteve {trilha_tipos(cid2)})")
_sessao_mi2 = SessionLocal()
_evt_mi2 = (
    _sessao_mi2.query(ConversationEvent)
    .filter_by(conversation_id=cid2, event_type=TipoEvento.MESSAGE_IGNORED.value).first()
)
check(
    _evt_mi2 is not None and _evt_mi2.action == "lote_sem_conteudo_substantivo",
    f"2e. action = motivo do pre-filtro (obteve {_evt_mi2 and _evt_mi2.action!r})",
)
_sessao_mi2.close()
check(snapshot_conversa(cid2) == snap2_antes, "2f. NENHUMA coluna de conversations mudou")


# ===========================================================================
# 3. Bot inativo -> curto-circuito ANTES do pre-filtro, IA nunca chamada.
# ===========================================================================
cid3 = criar_conversa(is_bot_active=False, atendente_id=42)
snap3_antes = snapshot_conversa(cid3)
res3 = executar_pipeline(db, _entrada(cid3))

check(res3.etapa_final == EtapaFinal.BOT_INATIVO, f"3a. desfecho BOT_INATIVO (obteve {res3.etapa_final})")
check(_estado_ia["interpretador_chamadas"] == 0, "3b. interpretador NUNCA chamado")
check(trilha_tipos(cid3) == [TipoEvento.MESSAGE_RECEIVED.value],
      f"3c. so MESSAGE_RECEIVED na trilha (obteve {trilha_tipos(cid3)})")
check(snapshot_conversa(cid3) == snap3_antes, "3d. NENHUMA coluna de conversations mudou")


# ===========================================================================
# 4. CLOSED -> desfecho proprio (reabertura NAO implementada nesta fase),
#    nao e erro, nao levanta excecao.
# ===========================================================================
cid4 = criar_conversa(state="CLOSED", is_bot_active=False)
snap4_antes = snapshot_conversa(cid4)
res4 = executar_pipeline(db, _entrada(cid4))
check(
    res4.etapa_final == EtapaFinal.CONVERSA_FECHADA_REABERTURA_NAO_IMPLEMENTADA,
    f"4a. desfecho CONVERSA_FECHADA_REABERTURA_NAO_IMPLEMENTADA (obteve {res4.etapa_final})",
)
check(_estado_ia["interpretador_chamadas"] == 0, "4b. interpretador NUNCA chamado")
check(snapshot_conversa(cid4) == snap4_antes, "4c. NENHUMA coluna de conversations mudou")


# ===========================================================================
# 5. LEGACY e NEW -> ConversationInvalidaParaV2. MESSAGE_RECEIVED e gravado
#    MESMO ASSIM (decisao documentada em pipeline.py - evidencia de R9).
# ===========================================================================
for _rotulo, _state in (("LEGACY", "LEGACY"), ("NEW", "NEW")):
    _cid = criar_conversa(state=_state)
    _snap_antes = snapshot_conversa(_cid)
    _levantou = None
    try:
        executar_pipeline(db, _entrada(_cid))
    except ConversationInvalidaParaV2 as exc:
        _levantou = exc
    check(_levantou is not None, f"5.{_rotulo}a. ConversationInvalidaParaV2 levantada")
    check(
        _levantou is not None and _levantou.state == _state,
        f"5.{_rotulo}b. .state carrega o valor estruturado (obteve {_levantou and _levantou.state!r})",
    )
    check(
        contar_eventos(_cid, TipoEvento.MESSAGE_RECEIVED) == 1,
        f"5.{_rotulo}c. MESSAGE_RECEIVED gravado ANTES da rejeicao (decisao documentada)",
    )
    check(snapshot_conversa(_cid) == _snap_antes, f"5.{_rotulo}d. NENHUMA coluna de conversations mudou")


# ===========================================================================
# 6. Triagem incompleta, sem pedido explicito -> segue ate o comunicador com
#    "solicitar_campos_faltantes" (nao ha teste direto do argumento interno;
#    provado pela trilha TRIAGE_DATA_UPDATED, nao TRIAGE_COMPLETED).
# ===========================================================================
_configurar_ia(
    resposta_interpretador=_resposta_ia(
        explicit_human_request=False,
        extracted={"nome": "Joao", "email": "joao@x.com"},
        duvidas=["Quantos dias fica o pacote?"],
    ),
    resposta_comunicador="Legal, Joao! Quantas pessoas vao viajar?",
)
cid6 = criar_conversa()
snap6_antes = snapshot_conversa(cid6)
res6 = executar_pipeline(db, _entrada(cid6))
check(res6.etapa_final == EtapaFinal.RESPOSTA_GERADA, f"6a. desfecho RESPOSTA_GERADA (obteve {res6.etapa_final})")
check(
    trilha_tipos(cid6) == [
        TipoEvento.MESSAGE_RECEIVED.value, TipoEvento.AI_INTERPRETATION_STARTED.value,
        TipoEvento.AI_INTERPRETATION_COMPLETED.value, TipoEvento.TRIAGE_DATA_UPDATED.value,
        TipoEvento.AI_RESPONSE_GENERATED.value,
    ],
    f"6b. sem handoff nesta trilha - nenhum gatilho de encaminhamento (obteve {trilha_tipos(cid6)})",
)
check(snapshot_conversa(cid6) == snap6_antes, "6c. NENHUMA coluna de conversations mudou")


# ===========================================================================
# 7. Handoff decidido (triagem completa), atendente ENCONTRADO, dry_run ->
#    HANDOFF_SIMULADO_DRY_RUN. handoff_to_human NUNCA chamado: zero evento
#    novo de handoff, zero linha em handoff_operations, estado inalterado.
#    `selecionar_atendente` e substituida SO neste cenario.
# ===========================================================================
_original_selecionar_atendente = pipeline_mod.selecionar_atendente
pipeline_mod.selecionar_atendente = lambda _db: 555
try:
    _configurar_ia(resposta_interpretador=_resposta_ia(
        explicit_human_request=True,
        extracted={
            "nome": "Maria", "email": "maria@x.com", "total_pessoas": 2, "adultos": 2,
            "criancas": 0, "datas_definidas": False,
            "destinos": [{"destino": "Atacama", "dias": 5}],
        },
    ))
    cid7 = criar_conversa()
    snap7_antes = snapshot_conversa(cid7)
    res7 = executar_pipeline(db, _entrada(cid7, mensagem="Quero falar com uma pessoa, por favor."))
finally:
    pipeline_mod.selecionar_atendente = _original_selecionar_atendente

check(
    res7.etapa_final == EtapaFinal.HANDOFF_SIMULADO_DRY_RUN and res7.atendente_id == 555,
    f"7a. desfecho HANDOFF_SIMULADO_DRY_RUN com o atendente encontrado (obteve {res7.etapa_final}, {res7.atendente_id})",
)
check(
    contar_eventos(cid7, TipoEvento.HANDOFF_REQUESTED) == 0
    and contar_eventos(cid7, TipoEvento.HANDOFF_FAILED) == 0
    and contar_eventos(cid7, TipoEvento.HANDOFF_COMPLETED) == 0,
    f"7b. ZERO evento de handoff gravado (trilha={trilha_tipos(cid7)})",
)
check(_estado_ia["comunicador_chamadas"] == 0, "7c. comunicador NUNCA chamado (handoff simulado para o fluxo)")
check(
    db.query(HandoffOperation).filter_by(conversation_id=cid7).count() == 0,
    "7d. ZERO linha em handoff_operations",
)
check(snapshot_conversa(cid7) == snap7_antes, "7e. NENHUMA coluna de conversations mudou")


# ===========================================================================
# 8. Gate recusa - concorrencia simulada DURANTE a "IA pensando" (dentro da
#    fronteira de interpretador). Comunicador NUNCA chamado.
# ===========================================================================
cid8 = criar_conversa(state_version=3)


def _mutar_state_version_no_meio():
    outra = SessionLocal()
    try:
        conv = outra.get(Conversation, cid8)
        conv.state_version = 4
        outra.commit()
    finally:
        outra.close()


_configurar_ia(
    resposta_interpretador=_resposta_ia(explicit_human_request=False),
    gancho_interpretador=_mutar_state_version_no_meio,
)
snap8_antes_da_mutacao = None  # a mutacao acontece DENTRO da chamada; nao ha "antes" util aqui.
res8 = executar_pipeline(db, _entrada(cid8))

check(
    res8.etapa_final == EtapaFinal.RESPOSTA_DESCARTADA_GATE,
    f"8a. desfecho RESPOSTA_DESCARTADA_GATE (obteve {res8.etapa_final})",
)
check(_estado_ia["comunicador_chamadas"] == 0, "8b. comunicador NUNCA chamado - o gate parou antes")
check(
    contar_eventos(cid8, TipoEvento.AI_RESPONSE_DISCARDED) == 1,
    "8c. exatamente 1 AI_RESPONSE_DISCARDED (emitido pelo proprio gate.py)",
)
check(snapshot_conversa(cid8)["state_version"] == 4, "8d. state_version=4 e o valor REAL (da mutacao concorrente)")


# ===========================================================================
# 9. Guard bloqueia - LLM (fake) tenta mencionar valor monetario.
# ===========================================================================
_configurar_ia(
    resposta_interpretador=_resposta_ia(explicit_human_request=False),
    resposta_comunicador="O pacote custa 5000 reais.",
)
cid9 = criar_conversa()
snap9_antes = snapshot_conversa(cid9)
res9 = executar_pipeline(db, _entrada(cid9))

check(
    res9.etapa_final == EtapaFinal.RESPOSTA_BLOQUEADA_GUARD and res9.texto_resposta is None,
    f"9a. desfecho RESPOSTA_BLOQUEADA_GUARD, sem texto vazando (obteve {res9.etapa_final}, {res9.texto_resposta!r})",
)
_sessao_g9 = SessionLocal()
_evt_g9 = (
    _sessao_g9.query(ConversationEvent)
    .filter_by(conversation_id=cid9, event_type=TipoEvento.AI_RESPONSE_DISCARDED.value).first()
)
check(
    _evt_g9 is not None and _evt_g9.error_code == "VALOR_MONETARIO_DETECTADO",
    f"9b. error_code = motivo do output_guard, maiusculizado (obteve {_evt_g9 and _evt_g9.error_code})",
)
_sessao_g9.close()
check(snapshot_conversa(cid9) == snap9_antes, "9c. NENHUMA coluna de conversations mudou")


# ===========================================================================
# 10. Interpretacao falha -> INTERPRETACAO_FALHOU, nunca degrada, sem triagem.
# ===========================================================================
_configurar_ia(excecao_interpretador=RuntimeError("falha simulada do SDK"))
cid10 = criar_conversa()
snap10_antes = snapshot_conversa(cid10)
res10 = executar_pipeline(db, _entrada(cid10))

check(
    res10.etapa_final == EtapaFinal.INTERPRETACAO_FALHOU and res10.detalhe == "CHAMADA_LLM_FALHOU",
    f"10a. desfecho INTERPRETACAO_FALHOU com detalhe correto (obteve {res10.etapa_final}, {res10.detalhe})",
)
check(
    trilha_tipos(cid10) == [
        TipoEvento.MESSAGE_RECEIVED.value, TipoEvento.AI_INTERPRETATION_STARTED.value,
        TipoEvento.AI_INTERPRETATION_FAILED.value,
    ],
    f"10b. trilha para exatamente onde a falha aconteceu, sem TRIAGE_* (obteve {trilha_tipos(cid10)})",
)
check(snapshot_conversa(cid10) == snap10_antes, "10c. NENHUMA coluna de conversations mudou")


# ===========================================================================
# 11. EventoDuplicado - mesma conversa, mesmo event_id, segunda chamada.
# ===========================================================================
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=False))
cid11 = criar_conversa()
ev11 = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
res11a = executar_pipeline(db, _entrada(cid11, event_id=ev11))
check(res11a.etapa_final == EtapaFinal.RESPOSTA_GERADA, "11a. primeira chamada conclui normalmente")

_levantou_dup = None
try:
    executar_pipeline(db, _entrada(cid11, event_id=ev11))
except EventoDuplicado as exc:
    _levantou_dup = exc
check(_levantou_dup is not None, "11b. segunda chamada com o MESMO event_id levanta EventoDuplicado")
check(
    _levantou_dup is not None and _levantou_dup.event_id == ev11,
    f"11c. .event_id carrega a chave estruturada (obteve {_levantou_dup and _levantou_dup.event_id})",
)
check(
    contar_eventos(cid11, TipoEvento.MESSAGE_RECEIVED) == 1,
    "11d. a segunda tentativa NAO produziu um segundo MESSAGE_RECEIVED",
)


# ===========================================================================
# 12. Payload malformado (extra="forbid") - prova que InboundContextV2 e
#     estrito, mesmo rigor dos 6 contratos da Fase 1.
# ===========================================================================
_payload_extra_rejeitado = False
try:
    InboundContextV2(
        conversation_id=cid1, origem=OrigemEvento.SINTETICO, lote=[],
        mensagem_interpretacao="oi", campo_fantasma="nao deveria existir",
    )
except Exception as exc:  # noqa: BLE001 - queremos confirmar que ALGO rejeita, tipo exato e ValidationError do pydantic
    _payload_extra_rejeitado = type(exc).__name__ == "ValidationError"
check(_payload_extra_rejeitado, "12a. campo extra no payload e rejeitado (extra=forbid)")

_event_id_vazio_rejeitado = False
try:
    InboundContextV2(
        conversation_id=cid1, origem=OrigemEvento.SINTETICO, lote=[],
        mensagem_interpretacao="oi", event_id="",
    )
    executar_pipeline  # no-op, so para deixar claro que nao validamos aqui
except Exception:  # noqa: BLE001
    pass
# `event_id=""` passa a VALIDACAO PYDANTIC (e StrictStr valido) - quem recusa
# e `registrar_evento`/`validar_event_id`, dentro de `executar_pipeline`. Prova
# a regra fim-a-fim, nao so no schema:
_entrada_vazia = InboundContextV2(
    conversation_id=cid1, origem=OrigemEvento.SINTETICO,
    lote=[MensagemLote(msg_type="reaction", content="")],
    mensagem_interpretacao="oi", event_id="",
)
_levantou_vazio = None
try:
    executar_pipeline(db, _entrada_vazia)
except Exception as exc:  # noqa: BLE001
    _levantou_vazio = exc
check(
    _levantou_vazio is not None and type(_levantou_vazio).__name__ == "CampoInvalido",
    f"12b. event_id='' vira CampoInvalido (NUNCA None) - obteve {_levantou_vazio!r}",
)


# ===========================================================================
# 13. PROVA GLOBAL DE DRY-RUN - em todos os cenarios acima, nenhuma linha de
#     handoff_operations foi criada em NENHUM momento (a unica funcao com
#     efeito real, handoff_to_human, nunca foi chamada).
# ===========================================================================
check(
    db.query(HandoffOperation).count() == 0,
    f"13. ZERO linhas em handoff_operations no banco INTEIRO (obteve {db.query(HandoffOperation).count()})",
)


# ===========================================================================
# 14. FIX 2 (achado do code-reviewer, contradizia o proprio docstring do
#     modulo) - falha de handoff em modo "live" NAO retorna em silencio: cai
#     para o comunicador, exatamente como o ramo irmao `atendente_id is None`
#     (Cenario 1) ja fazia. UNICO cenario deste arquivo em V2_MODE="live" -
#     `handoff_to_human` e TAMBEM substituida aqui (a funcao REAL nunca
#     roda), entao a garantia de zero efeito real do Cenario 13 nao e
#     violada - reconfirmado no Cenario 16. `V2_MODE`, `selecionar_atendente`
#     e `handoff_to_human` sao restauradas no finally.
# ===========================================================================
_original_v2_mode_pipeline = pipeline_mod.V2_MODE
_original_handoff_to_human = pipeline_mod.handoff_to_human
pipeline_mod.selecionar_atendente = lambda _db: 777


def _handoff_falha_fake(db_, *, conversation_id, target_user_id, reason, event_id):
    return HandoffResult(
        sucesso=False,
        replayed=False,
        conversation_id=conversation_id,
        target_user_id=target_user_id,
        event_id=event_id,
        error_code="CRM_INDISPONIVEL",
    )


pipeline_mod.handoff_to_human = _handoff_falha_fake
pipeline_mod.V2_MODE = "live"
try:
    _configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=True))
    cid14 = criar_conversa()
    res14 = executar_pipeline(db, _entrada(cid14, mensagem="Quero falar com um atendente."))
finally:
    pipeline_mod.selecionar_atendente = _original_selecionar_atendente
    pipeline_mod.handoff_to_human = _original_handoff_to_human
    pipeline_mod.V2_MODE = _original_v2_mode_pipeline

check(
    res14.etapa_final == EtapaFinal.RESPOSTA_GERADA and res14.texto_resposta is not None,
    f"14a. FIX 2: falha de handoff (modo live) cai para o comunicador em vez de "
    f"devolver silencio (obteve {res14.etapa_final}, texto={res14.texto_resposta!r})",
)
check(
    _estado_ia["comunicador_chamadas"] == 1,
    "14b. FIX 2: comunicador foi REALMENTE chamado apos a falha do handoff (prova o fallthrough)",
)


# ===========================================================================
# 15. FIX 5 (achado dos 4 revisores) - camada HTTP de verdade, via
#     `fastapi.testclient.TestClient` contra o app real (mesmo padrao de
#     tests/test_v2_lead_domain.py, Fase 6). Nenhum cenario 1-14 acima passa
#     pelo router/FastAPI - so por chamada direta a `executar_pipeline()` -,
#     e foi assim que o achado do Fix 1 escapou da bateria original. Tambem
#     pina o FIX 6 (`event_id=0`/`False`).
# ===========================================================================
from app.main import app as fastapi_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

http_client = TestClient(fastapi_app)
AUTH_HEADERS = {"X-Webhook-Auth": _WEBHOOK_AUTH_VALUE_TESTE}


def _payload_http(cid, *, mensagem="Ola, meu nome e Joao.", event_id=None):
    """Mesmo formato de `_entrada()`, como dict JSON puro - para requests HTTP
    reais via TestClient, onde nao ha objeto Pydantic para montar do lado do
    teste (o corpo vira JSON de verdade, exatamente como um chamador real
    mandaria)."""
    corpo = {
        "conversation_id": cid,
        "origem": OrigemEvento.SINTETICO.value,
        "lote": [{"msg_type": "text", "content": mensagem}],
        "mensagem_interpretacao": mensagem,
    }
    if event_id is not None:
        corpo["event_id"] = event_id
    return corpo


# 15a. 200 de sucesso.
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=False))
cid15a = criar_conversa()
r15a = http_client.post("/webhook/agent-bia-v2", json=_payload_http(cid15a), headers=AUTH_HEADERS)
check(r15a.status_code == 200, f"15a. 200 de sucesso via HTTP real (veio {r15a.status_code}: {r15a.text[:200]})")
check(
    r15a.status_code == 200 and r15a.json().get("etapa_final") == EtapaFinal.RESPOSTA_GERADA.value,
    f"15a2. corpo JSON reflete o desfecho real (veio {r15a.text[:200]})",
)

# 15b. 404 - conversation_id inexistente.
r15b = http_client.post("/webhook/agent-bia-v2", json=_payload_http(999999999), headers=AUTH_HEADERS)
check(r15b.status_code == 404, f"15b. 404 para conversation_id inexistente (veio {r15b.status_code})")

# 15c. 409 - EventoDuplicado no replay do MESMO event_id.
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=False))
cid15c = criar_conversa()
ev15c = str(uuid.uuid4())
r15c1 = http_client.post(
    "/webhook/agent-bia-v2", json=_payload_http(cid15c, event_id=ev15c), headers=AUTH_HEADERS
)
check(r15c1.status_code == 200, f"15c. primeira chamada com event_id novo da 200 (veio {r15c1.status_code})")
r15c2 = http_client.post(
    "/webhook/agent-bia-v2", json=_payload_http(cid15c, event_id=ev15c), headers=AUTH_HEADERS
)
check(
    r15c2.status_code == 409 and r15c2.json().get("event_id") == ev15c,
    f"15c2. replay do MESMO event_id da 409 via handler GLOBAL (veio {r15c2.status_code}: {r15c2.text[:200]})",
)

# 15d. FIX 1 - event_id="" tem que dar 422 (CampoInvalido), NUNCA 404. Este e
#      exatamente o caso que provava o bug: o `except ValueError` generico
#      interceptava ANTES do handler global, e devolvia 404.
cid15d = criar_conversa()
r15d = http_client.post(
    "/webhook/agent-bia-v2", json=_payload_http(cid15d, event_id=""), headers=AUTH_HEADERS
)
check(
    r15d.status_code == 422 and r15d.json().get("campo") == "event_id",
    f"15d. FIX 1: event_id='' da 422 de dominio (nao 404) via HTTP real "
    f"(veio {r15d.status_code}: {r15d.text[:200]})",
)

# 15e. FIX 3 - sem a credencial nova (ou com credencial errada), 401, e NADA
#      e processado (nem o MESSAGE_RECEIVED e gravado).
cid15e = criar_conversa()
r15e_sem = http_client.post("/webhook/agent-bia-v2", json=_payload_http(cid15e))
check(r15e_sem.status_code == 401, f"15e. FIX 3: sem credencial da 401 (veio {r15e_sem.status_code})")
r15e_errada = http_client.post(
    "/webhook/agent-bia-v2", json=_payload_http(cid15e), headers={"X-Webhook-Auth": "chave-errada"}
)
check(r15e_errada.status_code == 401, f"15e2. FIX 3: credencial errada da 401 (veio {r15e_errada.status_code})")
check(
    contar_eventos(cid15e, TipoEvento.MESSAGE_RECEIVED) == 0,
    "15e3. FIX 3: nada foi gravado sem autenticacao valida (o gate barra ANTES do handler)",
)

# 15f. FIX 6 (pin) - event_id=0 / event_id=False sao rejeitados pelo Pydantic
#      (StrictStr) UMA CAMADA ANTES de CampoInvalido: 422 tambem, mas com o
#      corpo NATIVO do FastAPI (sem a chave "campo"). A propriedade de
#      seguranca (nunca vira None em silencio) se mantem hoje; este teste so
#      pina a FORMA da resposta, para que uma futura unificacao dos tipos nao
#      reabra a coercao silenciosa sem que um teste acuse a mudanca.
for _rotulo, _valor in (("event_id=0", 0), ("event_id=False", False)):
    _cid_f6 = criar_conversa()
    _corpo_f6 = _payload_http(_cid_f6)
    _corpo_f6["event_id"] = _valor
    _r_f6 = http_client.post("/webhook/agent-bia-v2", json=_corpo_f6, headers=AUTH_HEADERS)
    check(
        _r_f6.status_code == 422 and "campo" not in _r_f6.json(),
        f"15f.{_rotulo}. FIX 6 (pin): 422 nativo do Pydantic, sem a chave 'campo' "
        f"(veio {_r_f6.status_code}: {_r_f6.text[:200]})",
    )


# ===========================================================================
# 16. Reconfirma a prova global do Cenario 13 depois dos Cenarios 14 e 15 (o
#     14 e o unico deste arquivo em modo "live", e o 15 passa pela camada
#     HTTP de verdade): ainda ZERO linha real em handoff_operations.
# ===========================================================================
check(
    db.query(HandoffOperation).count() == 0,
    f"16. ZERO linhas em handoff_operations mesmo apos os Cenarios 14-15 "
    f"(obteve {db.query(HandoffOperation).count()})",
)


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
