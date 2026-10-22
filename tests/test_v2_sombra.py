"""
BIA-V2 SOMBRA - prova de EFEITO ZERO do endpoint de observacao
(`conversas/app/v2/sombra.py`, `conversas/app/routers/v2_agent_sombra.py`).

A pergunta que este arquivo responde e uma so: "rodar a sombra sobre uma
conversa REAL (`state='LEGACY'`) muda ALGUMA coisa?". A resposta tem de ser
nao em todos os cenarios, inclusive com `V2_MODE=live` (Cenario H) - a
seguranca da sombra e ESTRUTURAL, nunca de configuracao.

O QUE E FOTOGRAFADO ANTES E DEPOIS DE CADA CHAMADA
====================================================
`_snapshot_global` le por uma SESSAO NOVA (so enxerga o que foi COMITADO) e
compara, campo a campo:

  - `conversations`: state, state_version, is_bot_active, atendente_id,
    responsavel_id, queued_at, primeira_resposta_humana_at, status, closed_at;
  - lado CRM: `leads.responsavel_id`;
  - contagem de linhas de `handoff_operations` e `lead_handoff_operations`;
  - contagem de linhas de `conversation_events`.

As duas tabelas do lado CRM (`leads`, `lead_handoff_operations`) sao criadas
aqui por DDL cru, com as colunas MINIMAS que as asserções leem: o processo do
Conversas nao pode importar os modelos do CRM (os dois pacotes se chamam
`app`). O que elas provam e o que interessa - se a sombra escrevesse do lado
CRM, a contagem mudaria.

A BARREIRA NAO E DECORATIVA (Cenarios I e J)
==============================================
Provar "nada mudou" so vale se houver prova de que a barreira REAGE. O Cenario
I ataca `_sessao_somente_leitura` diretamente; o Cenario J injeta uma escrita
DENTRO do fluxo real da rota HTTP (substituindo
`selecionar_atendente_sem_aviso`, o unico ponto do fluxo que recebe a
`Session`) e mostra que o pedido morre com 500 e sem deixar linha nenhuma para
tras.

A BARREIRA SOBREVIVE AO RELEASE DE CONEXAO (Cenario M)
========================================================
`simular_pipeline` devolve a conexao ao pool antes de CADA ida ao Gemini
(`_soltar_conexao`), para nao zerar o pool que a rota compartilha com o webhook
real da Meta. O Cenario M prova que isso nao enfraquece o efeito zero: a
conexao sai mesmo (M1/M2), a barreira continua armada na sessao solta (M3) e
depois da transacao reabrir (M4), e soltar uma sessao SUJA levanta em vez de
descartar em silencio (M5). O Cenario J prova o mesmo ponta a ponta - a
escrita que ele injeta acontece DEPOIS do primeiro release.

O SINAL OPERACIONAL FALSO (Cenario N)
=======================================
A sombra usa `selecionar_atendente_sem_aviso`: mesma selecao, sem o
`logger.warning` da V1 que anunciaria "conversa entra na fila sem dono" para
conversa que a sombra jamais enfileirou. N1 prova o silencio, N3 e o controle
negativo (o caminho VIVO continua avisando) e N4 prova que os dois devolvem o
MESMO id - o silencio nao mexeu na selecao.

TETO DE TAMANHO DO CORPO (K11-K16)
====================================
Cada pedido aceito paga uma chamada de modelo. K11/K12/K14 provam a recusa do
corpo gigante no CONTRATO (422, sem tocar o Gemini); K13 e K15 sao os
controles positivos que impedem o teto de virar over-rejection de trafego
realista - inclusive `mensagem_interpretacao` vazia, que e como chega um lote
de reaction.

FRONTEIRA SUBSTITUIDA: so a chamada de rede ao Gemini
(`interpretador._chamar_llm` / `comunicador._chamar_gemini`), mesmo padrao ja
usado por `tests/test_v2_pipeline.py` e documentado nos proprios modulos.
`selecionar_atendente`, `avaliar_prefiltro`, `avaliar_triagem`,
`gate.motivo_recusa` e `output_guard.filtrar_saida` rodam de VERDADE.

Nenhum dado real de cliente: nomes, emails e numeros abaixo sao obviamente
ficticios.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_v2_sombra.py
"""
import ast
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_sombra_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
os.environ["BIA_V2_CRM_API_KEY"] = ""
# Comeca em dry_run; o Cenario H vira para "live" e reprova o efeito zero.
os.environ["V2_MODE"] = "dry_run"
os.environ["ATENDENTES_ELEGIVEIS"] = ""
_WEBHOOK_AUTH_VALUE_TESTE = "segredo-teste-v2-agent-webhook"
os.environ["V2_AGENT_WEBHOOK_AUTH_VALUE"] = _WEBHOOK_AUTH_VALUE_TESTE
# Credencial PROPRIA da sombra, DIFERENTE da credencial viva acima de proposito -
# o Cenario K4b prova exatamente que uma nao abre a rota da outra.
_SHADOW_WEBHOOK_AUTH_VALUE_TESTE = "segredo-teste-v2-agent-webhook-sombra"
os.environ["V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE"] = _SHADOW_WEBHOOK_AUTH_VALUE_TESTE

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


# Modelos com relationship() por NOME precisam estar registrados antes de
# qualquer query (mesmo padrao de tests/test_v2_pipeline.py).
import app.models.media_asset  # noqa: E402,F401
import app.models.note  # noqa: E402,F401
import app.models.tag  # noqa: E402,F401
import app.v2.comunicador as comunicador_mod  # noqa: E402
import app.v2.interpretador as interpretador_mod  # noqa: E402
import app.v2.sombra as sombra_mod  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.auth import User  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402
from app.models.handoff_op import HandoffOperation  # noqa: E402
from app.v2.comunicador import ComunicadorIndisponivel  # noqa: E402
from app.v2.pipeline import EtapaFinal  # noqa: E402
from app.v2.prefiltro import MensagemLote  # noqa: E402
from app.v2.sombra import (  # noqa: E402
    EntradaSombra,
    EscritaProibidaNaSombra,
    _sessao_somente_leitura,
    simular_pipeline,
)

Base.metadata.create_all(
    bind=engine,
    tables=[
        Conversation.__table__, Message.__table__, ConversationEvent.__table__,
        HandoffOperation.__table__, User.__table__,
    ],
)

# Tabelas do lado CRM, com as colunas minimas que as asserções leem - ver o
# bloco correspondente no docstring do modulo.
with engine.begin() as _conexao:
    _conexao.execute(text(
        "CREATE TABLE IF NOT EXISTS leads ("
        " id INTEGER PRIMARY KEY, nome TEXT, responsavel_id INTEGER)"
    ))
    _conexao.execute(text(
        "CREATE TABLE IF NOT EXISTS lead_handoff_operations ("
        " id INTEGER PRIMARY KEY, event_id TEXT, lead_id INTEGER, status TEXT)"
    ))

db = SessionLocal()
_contador = iter(range(1, 100000))

# Atendente ficticio, ATIVO - `selecionar_atendente` roda de verdade contra
# esta linha. Quem controla a elegibilidade por cenario e a env var
# ATENDENTES_ELEGIVEIS, lida a cada chamada por `_ids_configurados()`.
ATENDENTE = User(
    nome="Atendente Ficticia",
    email="atendente.ficticia@exemplo.invalido",
    hashed_password="nao-e-um-hash-real",
    role="USER",
    is_active=True,
    email_verified=True,
)
db.add(ATENDENTE)
db.commit()
ID_ATENDENTE = ATENDENTE.id


def _com_atendente_disponivel():
    os.environ["ATENDENTES_ELEGIVEIS"] = str(ID_ATENDENTE)


def _sem_atendente_disponivel():
    # Id que nao existe na tabela -> `atendentes_elegiveis` devolve [] e
    # `resolver_atendente_elegivel` devolve None ORGANICAMENTE (sem stub).
    os.environ["ATENDENTES_ELEGIVEIS"] = "999999"


# ---------------------------------------------------------------------------
# Fronteira de IA substituida - so a chamada de rede ao Gemini.
# ---------------------------------------------------------------------------
_estado_ia = {
    "interpretador_resposta": None,
    "interpretador_excecao": None,
    "comunicador_resposta": "Oi! Recebi sua mensagem.",
    "comunicador_excecao": None,
}


def _fake_chamar_llm(mensagem):
    if _estado_ia["interpretador_excecao"] is not None:
        raise _estado_ia["interpretador_excecao"]
    return json.dumps(_estado_ia["interpretador_resposta"])


def _fake_chamar_gemini(prompt):
    if _estado_ia["comunicador_excecao"] is not None:
        raise _estado_ia["comunicador_excecao"]
    return _estado_ia["comunicador_resposta"]


interpretador_mod._chamar_llm = _fake_chamar_llm
comunicador_mod._chamar_gemini = _fake_chamar_gemini


def _configurar_ia(*, resposta_interpretador=None, excecao_interpretador=None,
                   resposta_comunicador="Oi! Recebi sua mensagem.", excecao_comunicador=None):
    _estado_ia["interpretador_resposta"] = resposta_interpretador
    _estado_ia["interpretador_excecao"] = excecao_interpretador
    _estado_ia["comunicador_resposta"] = resposta_comunicador
    _estado_ia["comunicador_excecao"] = excecao_comunicador


def _resposta_ia(*, explicit_human_request, extracted=None, duvidas=None, intent=None):
    return {
        "intent": intent,
        "explicit_human_request": explicit_human_request,
        "extracted": extracted or {},
        "duvidas": duvidas or [],
    }


# Triagem COMPLETA (nenhum campo faltante) - ver `avaliar_triagem`/triagem.py.
_TRIAGEM_COMPLETA = {
    "nome": "Cliente Ficticio",
    "email": "cliente.ficticio@exemplo.invalido",
    "destinos": [{"destino": "San Pedro de Atacama",
                  "data_inicio": "2027-03-10", "data_fim": "2027-03-15"}],
    "total_pessoas": 2,
    "adultos": 2,
    "criancas": 0,
    "datas_definidas": True,
}


# ---------------------------------------------------------------------------
# Fixtures e snapshot global.
# ---------------------------------------------------------------------------
def criar_conversa_legacy(*, is_bot_active=True, atendente_id=None, status="aberta"):
    """Conversa REAL como as de producao: `state='LEGACY'`, que o endpoint
    VIVO recusa com 409 (e continuara recusando - ver Cenario K)."""
    n = next(_contador)
    lead_id = 7000 + n
    conv = Conversation(
        lead_id=lead_id,
        whatsapp=f"5511900{n:06d}",
        status=status,
        unread_count=0,
        is_bot_active=is_bot_active,
        atendente_id=atendente_id,
        last_customer_msg_at=datetime.now(timezone.utc),
        state="LEGACY",
        state_version=0,
    )
    db.add(conv)
    db.commit()
    with engine.begin() as conexao:
        conexao.execute(
            text("INSERT INTO leads (id, nome, responsavel_id) VALUES (:i, :n, :r)"),
            {"i": lead_id, "n": "Lead Ficticio", "r": 4242},
        )
    return conv.id, lead_id


_COLUNAS_SNAPSHOT = (
    "state", "state_version", "is_bot_active", "atendente_id", "responsavel_id",
    "queued_at", "primeira_resposta_humana_at", "status", "closed_at",
)


def _snapshot_global(cid, lead_id):
    """Fotografa por uma SESSAO NOVA: so o que foi COMITADO aparece."""
    sessao = SessionLocal()
    try:
        conversa = sessao.get(Conversation, cid)
        foto = {f"conversa.{campo}": getattr(conversa, campo) for campo in _COLUNAS_SNAPSHOT}
        foto["handoff_operations"] = sessao.query(HandoffOperation).count()
        foto["conversation_events"] = sessao.query(ConversationEvent).count()
        foto["lead.responsavel_id"] = sessao.execute(
            text("SELECT responsavel_id FROM leads WHERE id = :i"), {"i": lead_id}
        ).scalar()
        foto["lead_handoff_operations"] = sessao.execute(
            text("SELECT COUNT(*) FROM lead_handoff_operations")
        ).scalar()
        return foto
    finally:
        sessao.close()


def _diferencas(antes, depois):
    return {k: (antes[k], depois[k]) for k in antes if antes[k] != depois[k]}


def com_prova_de_efeito_zero(rotulo, cid, lead_id, acao):
    """Executa `acao()` entre duas fotos e exige que sejam IDENTICAS."""
    antes = _snapshot_global(cid, lead_id)
    try:
        resultado = acao()
    finally:
        depois = _snapshot_global(cid, lead_id)
    check(
        depois == antes,
        f"{rotulo}: EFEITO ZERO - nada mudou em conversations/leads/"
        f"handoff_operations/lead_handoff_operations/conversation_events "
        f"(diferencas: {_diferencas(antes, depois)})",
    )
    return resultado


def _entrada(cid, *, lote=None, mensagem="Ola, quero saber do Atacama."):
    return EntradaSombra(
        conversation_id=cid,
        lote=lote if lote is not None else [MensagemLote(msg_type="text", content=mensagem)],
        mensagem_interpretacao=mensagem,
    )


print("=" * 70)
print("CENARIOS A-H - sombra sobre conversa REAL (state='LEGACY')")
print("=" * 70)

# ===========================================================================
# A. Triagem INCOMPLETA - a IA extraiu pouca coisa, ninguem pediu humano.
#    Desfecho esperado: resposta redigida, sem handoff.
# ===========================================================================
_sem_atendente_disponivel()
_configurar_ia(resposta_interpretador=_resposta_ia(
    explicit_human_request=False, extracted={"nome": "Cliente Ficticio"}))
cidA, leadA = criar_conversa_legacy()
resA = com_prova_de_efeito_zero("A", cidA, leadA, lambda: simular_pipeline(db, _entrada(cidA)))
check(resA.etapa_final == EtapaFinal.RESPOSTA_GERADA,
      f"A1. triagem incompleta -> RESPOSTA_GERADA (obteve {resA.etapa_final})")
check(resA.triagem_completa is False, f"A2. triagem_completa=False (obteve {resA.triagem_completa!r})")
check(resA.handoff_sugerido is False, f"A3. nenhum handoff sugerido (obteve {resA.handoff_sugerido!r})")
check(resA.responderia is True and resA.texto_resposta == "Oi! Recebi sua mensagem.",
      f"A4. responderia com o texto redigido (obteve {resA.responderia!r}/{resA.texto_resposta!r})")
check(resA.latencia_ms >= 0, f"A5. latencia_ms medida (obteve {resA.latencia_ms!r})")

# ===========================================================================
# B. Triagem COMPLETA, com atendente elegivel -> a V2 encaminharia.
# ===========================================================================
_com_atendente_disponivel()
_configurar_ia(resposta_interpretador=_resposta_ia(
    explicit_human_request=False, extracted=_TRIAGEM_COMPLETA))
cidB, leadB = criar_conversa_legacy()
resB = com_prova_de_efeito_zero("B", cidB, leadB, lambda: simular_pipeline(db, _entrada(cidB)))
check(resB.etapa_final == EtapaFinal.HANDOFF_SIMULADO_DRY_RUN,
      f"B1. triagem completa -> HANDOFF_SIMULADO_DRY_RUN (obteve {resB.etapa_final})")
check(resB.triagem_completa is True, f"B2. triagem_completa=True (obteve {resB.triagem_completa!r})")
check(resB.handoff_sugerido is True and resB.atendente_sugerido_id == ID_ATENDENTE,
      f"B3. handoff sugerido para o atendente real (obteve {resB.handoff_sugerido!r}/"
      f"{resB.atendente_sugerido_id!r}, esperado {ID_ATENDENTE})")
check(resB.responderia is False and resB.texto_resposta is None,
      "B4. sem resposta apos handoff sugerido (fiel ao pipeline vivo)")

# ===========================================================================
# C. Pedido EXPLICITO de humano (triagem ainda incompleta).
# ===========================================================================
_com_atendente_disponivel()
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=True))
cidC, leadC = criar_conversa_legacy()
resC = com_prova_de_efeito_zero("C", cidC, leadC, lambda: simular_pipeline(db, _entrada(cidC)))
check(resC.etapa_final == EtapaFinal.HANDOFF_SIMULADO_DRY_RUN,
      f"C1. pedido explicito -> HANDOFF_SIMULADO_DRY_RUN (obteve {resC.etapa_final})")
check(resC.handoff_sugerido is True and resC.atendente_sugerido_id == ID_ATENDENTE,
      f"C2. atendente sugerido (obteve {resC.atendente_sugerido_id!r})")
check(resC.triagem_completa is False,
      f"C3. handoff por pedido explicito, nao por triagem completa (obteve {resC.triagem_completa!r})")

# ===========================================================================
# D. Pedido explicito SEM atendente elegivel - a V2 seguiria para o
#    comunicador (mesmo fallthrough do caminho vivo), sem anunciar handoff.
# ===========================================================================
_sem_atendente_disponivel()
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=True))
cidD, leadD = criar_conversa_legacy()
resD = com_prova_de_efeito_zero("D", cidD, leadD, lambda: simular_pipeline(db, _entrada(cidD)))
check(resD.etapa_final == EtapaFinal.RESPOSTA_GERADA,
      f"D1. sem atendente -> segue para o comunicador (obteve {resD.etapa_final})")
check(resD.handoff_sugerido is True and resD.atendente_sugerido_id is None,
      f"D2. handoff era desejado mas nao ha quem receber (obteve {resD.handoff_sugerido!r}/"
      f"{resD.atendente_sugerido_id!r})")

# ===========================================================================
# E. Humano JA atribuido. Dois sub-casos, porque a V1 desliga o bot no claim:
#    E1 = bot desligado (para no passo 5); E2 = bot ligado (para no gate).
# ===========================================================================
_sem_atendente_disponivel()
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=False))
cidE1, leadE1 = criar_conversa_legacy(atendente_id=ID_ATENDENTE, is_bot_active=False)
resE1 = com_prova_de_efeito_zero("E1", cidE1, leadE1, lambda: simular_pipeline(db, _entrada(cidE1)))
check(resE1.etapa_final == EtapaFinal.BOT_INATIVO,
      f"E1a. humano assumiu e bot desligado -> BOT_INATIVO (obteve {resE1.etapa_final})")
check(resE1.responderia is False and resE1.triagem_completa is None,
      f"E1b. nem chegou a triagem (obteve triagem_completa={resE1.triagem_completa!r})")

cidE2, leadE2 = criar_conversa_legacy(atendente_id=ID_ATENDENTE, is_bot_active=True)
resE2 = com_prova_de_efeito_zero("E2", cidE2, leadE2, lambda: simular_pipeline(db, _entrada(cidE2)))
check(resE2.etapa_final == EtapaFinal.RESPOSTA_DESCARTADA_GATE,
      f"E2a. bot ligado mas humano dono -> o gate recusaria (obteve {resE2.etapa_final})")
check(resE2.erro_tecnico == "HUMANO_ASSUMIU",
      f"E2b. token de recusa do gate REAL, sem persistir evento (obteve {resE2.erro_tecnico!r})")

# ===========================================================================
# F. Mensagem que o PRE-FILTRO ignora (reaction) - nem chama o Gemini.
# ===========================================================================
_configurar_ia(excecao_interpretador=AssertionError(
    "F: o Gemini NAO pode ser chamado quando o pre-filtro ignora o lote"))
cidF, leadF = criar_conversa_legacy()
resF = com_prova_de_efeito_zero(
    "F", cidF, leadF,
    lambda: simular_pipeline(db, _entrada(cidF, lote=[MensagemLote(msg_type="reaction", content="👍")])),
)
check(resF.etapa_final == EtapaFinal.MENSAGEM_IGNORADA,
      f"F1. lote sem conteudo substantivo -> MENSAGEM_IGNORADA (obteve {resF.etapa_final})")
check(resF.responderia is False, "F2. nao responderia")

# ===========================================================================
# G. Falha da IA. G1 = interpretadora fora do ar; G2 = comunicadora fora do
#    ar. Nenhuma das duas pode virar 500 nem deixar rastro.
# ===========================================================================
_sem_atendente_disponivel()
_configurar_ia(excecao_interpretador=RuntimeError("Gemini ficticio indisponivel"))
cidG1, leadG1 = criar_conversa_legacy()
resG1 = com_prova_de_efeito_zero("G1", cidG1, leadG1, lambda: simular_pipeline(db, _entrada(cidG1)))
check(resG1.etapa_final == EtapaFinal.INTERPRETACAO_FALHOU,
      f"G1a. falha da interpretadora -> INTERPRETACAO_FALHOU (obteve {resG1.etapa_final})")
check(resG1.erro_tecnico == "CHAMADA_LLM_FALHOU",
      f"G1b. token fechado, sem excecao crua do SDK (obteve {resG1.erro_tecnico!r})")

_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=False),
               excecao_comunicador=ComunicadorIndisponivel("chamada_llm_falhou"))
cidG2, leadG2 = criar_conversa_legacy()
resG2 = com_prova_de_efeito_zero("G2", cidG2, leadG2, lambda: simular_pipeline(db, _entrada(cidG2)))
check(resG2.etapa_final == EtapaFinal.COMUNICADOR_INDISPONIVEL,
      f"G2a. falha da comunicadora -> COMUNICADOR_INDISPONIVEL (obteve {resG2.etapa_final})")
check(resG2.erro_tecnico == "chamada_llm_falhou",
      f"G2b. token fechado (obteve {resG2.erro_tecnico!r})")
check(resG2.responderia is False, "G2c. nao responderia")

# ===========================================================================
# H. `V2_MODE` acidentalmente em "live". A sombra tem de continuar com efeito
#    zero E com o MESMO desfecho - ela nao le essa variavel em lugar nenhum.
# ===========================================================================
import app.config as config_mod  # noqa: E402
import app.v2.pipeline as pipeline_mod  # noqa: E402

_v2_mode_original_env = os.environ["V2_MODE"]
_v2_mode_original_config = config_mod.V2_MODE
_v2_mode_original_pipeline = pipeline_mod.V2_MODE
os.environ["V2_MODE"] = "live"
config_mod.V2_MODE = "live"
pipeline_mod.V2_MODE = "live"  # o pipeline VIVO passaria a chamar handoff_to_human aqui.
try:
    check(not pipeline_mod._modo_dry_run(),
          "H0. o pipeline VIVO esta de fato em modo live neste ponto (a montagem do cenario vale)")
    _com_atendente_disponivel()
    _configurar_ia(resposta_interpretador=_resposta_ia(
        explicit_human_request=True, extracted=_TRIAGEM_COMPLETA))
    cidH, leadH = criar_conversa_legacy()
    resH = com_prova_de_efeito_zero("H", cidH, leadH, lambda: simular_pipeline(db, _entrada(cidH)))
    check(resH.etapa_final == EtapaFinal.HANDOFF_SIMULADO_DRY_RUN,
          f"H1. V2_MODE=live NAO muda o desfecho da sombra (obteve {resH.etapa_final})")
    check(resH.atendente_sugerido_id == ID_ATENDENTE,
          f"H2. atendente sugerido, mas NENHUM handoff executado (obteve {resH.atendente_sugerido_id!r})")
    check(db.query(HandoffOperation).count() == 0,
          f"H3. ZERO linhas em handoff_operations mesmo com V2_MODE=live "
          f"(obteve {db.query(HandoffOperation).count()})")
finally:
    os.environ["V2_MODE"] = _v2_mode_original_env
    config_mod.V2_MODE = _v2_mode_original_config
    pipeline_mod.V2_MODE = _v2_mode_original_pipeline


print()
print("=" * 70)
print("CENARIOS I-J - a barreira de escrita REAGE (a prova acima nao e vazia)")
print("=" * 70)

# ===========================================================================
# I. `_sessao_somente_leitura` aborta um flush com mudanca pendente.
# ===========================================================================
_sessao_i = SessionLocal()
try:
    _levantou = False
    try:
        with _sessao_somente_leitura(_sessao_i):
            _sessao_i.add(ConversationEvent(event_id="00000000-0000-4000-8000-000000000001",
                                            event_type="MESSAGE_RECEIVED"))
            _sessao_i.flush()
    except EscritaProibidaNaSombra:
        _levantou = True
    check(_levantou, "I1. flush com objeto novo levanta EscritaProibidaNaSombra")
finally:
    _sessao_i.close()

_sessao_i2 = SessionLocal()
try:
    check(
        _sessao_i2.query(ConversationEvent).filter_by(
            event_id="00000000-0000-4000-8000-000000000001").count() == 0,
        "I2. a linha barrada nao chegou ao banco",
    )
finally:
    _sessao_i2.close()

# ===========================================================================
# J. Injecao de escrita DENTRO do fluxo real da rota HTTP.
#    `selecionar_atendente_sem_aviso` e o unico passo do fluxo que recebe a
#    `Session` - substitui-la por uma versao que escreve simula com fidelidade
#    "alguem acrescentou uma escrita aqui" numa edicao futura.
#
#    ESTE CENARIO TAMBEM PROVA A FIX DA CONEXAO: esse passo roda DEPOIS do
#    primeiro `_soltar_conexao` (que fecha a sessao antes da ida ao Gemini).
#    Se soltar a conexao desarmasse a barreira, a escrita injetada aqui
#    passaria - a rota devolveria 200 e a linha ficaria no banco. O 500 abaixo
#    e, ponta a ponta, a prova de que a barreira sobrevive ao release.
# ===========================================================================
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.routers.v2_agent_sombra as v2_agent_sombra_mod  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.routers.v2_agent_sombra import _verificar_auth_webhook_sombra  # noqa: E402

http = TestClient(fastapi_app)
http_sem_reraise = TestClient(fastapi_app, raise_server_exceptions=False)
AUTH_HEADERS = {"X-Webhook-Auth": _WEBHOOK_AUTH_VALUE_TESTE}
AUTH_HEADERS_SOMBRA = {"X-Webhook-Auth": _SHADOW_WEBHOOK_AUTH_VALUE_TESTE}


def _payload_http(cid, *, mensagem="Ola, quero saber do Atacama.", lote=None):
    return {
        "conversation_id": cid,
        "lote": lote if lote is not None else [{"msg_type": "text", "content": mensagem}],
        "mensagem_interpretacao": mensagem,
    }


_selecionar_original = sombra_mod.selecionar_atendente_sem_aviso


def _selecao_que_escreve(sessao):
    sessao.add(ConversationEvent(event_id="00000000-0000-4000-8000-000000000002",
                                 event_type="MESSAGE_RECEIVED"))
    sessao.flush()
    return None


_com_atendente_disponivel()
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=True))
cidJ, leadJ = criar_conversa_legacy()
sombra_mod.selecionar_atendente_sem_aviso = _selecao_que_escreve
try:
    antesJ = _snapshot_global(cidJ, leadJ)
    rJ = http_sem_reraise.post(
        "/webhook/agent-bia-v2-shadow", json=_payload_http(cidJ), headers=AUTH_HEADERS_SOMBRA
    )
    depoisJ = _snapshot_global(cidJ, leadJ)
finally:
    sombra_mod.selecionar_atendente_sem_aviso = _selecionar_original

check(rJ.status_code == 500,
      f"J1. escrita injetada no fluxo real vira 500 RUIDOSO, nunca gravacao calada "
      f"(obteve {rJ.status_code})")
check(depoisJ == antesJ,
      f"J2. o pedido que falhou nao deixou NADA para tras (diferencas: {_diferencas(antesJ, depoisJ)})")


print()
print("=" * 70)
print("CENARIO K - a rota VIVA nao mudou; auth da sombra; camada HTTP")
print("=" * 70)

# ===========================================================================
# K. Regressao da rota VIVA: `/webhook/agent-bia-v2` continua recusando uma
#    conversa LEGACY com 409, exatamente como antes.
# ===========================================================================
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=False))
cidK, leadK = criar_conversa_legacy()
snapK_antes = _snapshot_global(cidK, leadK)
rK = http.post(
    "/webhook/agent-bia-v2",
    json={
        "conversation_id": cidK,
        "origem": "sintetico",
        "lote": [{"msg_type": "text", "content": "Ola"}],
        "mensagem_interpretacao": "Ola",
    },
    headers=AUTH_HEADERS,
)
check(rK.status_code == 409,
      f"K1. a rota VIVA continua recusando LEGACY com 409 (obteve {rK.status_code}: {rK.text[:160]})")
snapK_depois = _snapshot_global(cidK, leadK)
check(
    all(snapK_antes[c] == snapK_depois[c] for c in snapK_antes if c.startswith("conversa.")),
    "K2. a recusa da rota viva nao mexeu em nenhuma coluna de conversations "
    "(o MESSAGE_RECEIVED que ela grava ANTES da recusa e comportamento pre-existente, "
    "documentado no passo 2 de pipeline.py, e nao e alterado por esta tarefa)",
)

# K3-K5. Auth da sombra: ausente -> 401, errada -> 401, correta -> 200.
_sem_atendente_disponivel()
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=False))
cidK2, leadK2 = criar_conversa_legacy()
antesK2 = _snapshot_global(cidK2, leadK2)
rK3 = http.post("/webhook/agent-bia-v2-shadow", json=_payload_http(cidK2))
check(rK3.status_code == 401, f"K3. sombra sem cabecalho -> 401 (obteve {rK3.status_code})")
rK4 = http.post("/webhook/agent-bia-v2-shadow", json=_payload_http(cidK2),
                headers={"X-Webhook-Auth": "chave-errada"})
check(rK4.status_code == 401, f"K4. sombra com cabecalho errado -> 401 (obteve {rK4.status_code})")

# K4b. O PONTO CENTRAL desta tarefa: a credencial da rota VIVA (AUTH_HEADERS,
# V2_AGENT_WEBHOOK_AUTH_VALUE) NAO pode abrir a sombra. Antes da separacao de
# credenciais as duas rotas comparavam contra a MESMA variavel e esta chamada
# teria devolvido 200 - exatamente o cenario de URL do n8n configurada sem o
# sufixo `-shadow` que motivou a separacao (ver o comentario de
# V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE em app/config.py).
rK4b = http.post("/webhook/agent-bia-v2-shadow", json=_payload_http(cidK2), headers=AUTH_HEADERS)
check(rK4b.status_code == 401,
      f"K4b. credencial da rota VIVA apresentada a sombra -> 401 (obteve {rK4b.status_code})")

# K4c. Fail-closed: V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE vazia recusa TODA
# chamada, mesmo com um cabecalho de cara CORRETA (o valor enviado bate com o
# segredo de teste - e a VARIAVEL de configuracao que esta ausente). Mesma
# disciplina de V2_AGENT_WEBHOOK_AUTH_VALUE (app/routers/v2_agent.py). Patch
# no MODULO do router (nao em `config_mod`), mesma tecnica do Cenario H para
# V2_MODE: a rota le o nome importado no seu proprio namespace, nao o de
# app.config, em tempo de chamada.
_valor_original_shadow = v2_agent_sombra_mod.V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE
v2_agent_sombra_mod.V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE = ""
try:
    rK4c = http.post("/webhook/agent-bia-v2-shadow", json=_payload_http(cidK2), headers=AUTH_HEADERS_SOMBRA)
finally:
    v2_agent_sombra_mod.V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE = _valor_original_shadow
check(rK4c.status_code == 401,
      f"K4c. V2_AGENT_SHADOW_WEBHOOK_AUTH_VALUE vazia recusa mesmo com cabecalho de cara correta "
      f"(obteve {rK4c.status_code})")

# K4d. Cabecalho nao-ASCII -> 401, nunca 500 (`secrets.compare_digest` levanta
# TypeError para nao-ASCII; o guard `.isascii()` tem de rodar ANTES dele).
# NAO da para provar isto pelo cliente HTTP de teste: o httpx (usado por
# `TestClient`) recusa por conta propria montar um cabecalho Python `str` fora
# do ASCII (UnicodeEncodeError antes mesmo do request sair - confirmado
# empiricamente com httpx 0.28.1). Por isso esta prova chama a dependency
# diretamente, exatamente como o FastAPI a chamaria apos decodificar os bytes
# da rede (Starlette decodifica cabecalho em latin-1, entao um cabecalho REAL
# vindo de um cliente nao-Python pode chegar aqui com texto nao-ASCII).
_levantou_k4d = False
try:
    _verificar_auth_webhook_sombra(x_webhook_auth="café-não-é-ascii")
except HTTPException as exc:
    _levantou_k4d = exc.status_code == 401
check(_levantou_k4d, "K4d. cabecalho nao-ASCII -> HTTPException 401, nunca TypeError/500")

rK5 = http.post("/webhook/agent-bia-v2-shadow", json=_payload_http(cidK2), headers=AUTH_HEADERS_SOMBRA)
check(rK5.status_code == 200, f"K5. sombra com cabecalho correto -> 200 (obteve {rK5.status_code}: {rK5.text[:200]})")
depoisK2 = _snapshot_global(cidK2, leadK2)
check(depoisK2 == antesK2,
      f"K6. nenhuma das chamadas de auth (401 sem cabecalho, 401 errada, 401 credencial viva, "
      f"401 variavel vazia, 401 nao-ASCII, 200 correta) mudou NADA "
      f"(diferencas: {_diferencas(antesK2, depoisK2)})")

_corpo_k5 = rK5.json()
check(
    set(_corpo_k5) == {
        "conversation_id", "etapa_final", "triagem_completa", "handoff_sugerido",
        "atendente_sugerido_id", "responderia", "texto_resposta", "erro_tecnico", "latencia_ms",
    },
    f"K7. o corpo da resposta tem EXATAMENTE os 9 campos do contrato (obteve {sorted(_corpo_k5)})",
)
check(
    _corpo_k5["etapa_final"] == EtapaFinal.RESPOSTA_GERADA.value
    and _corpo_k5["conversation_id"] == cidK2,
    f"K8. o corpo reflete o desfecho real (obteve {rK5.text[:200]})",
)

# K9. 404 - conversation_id inexistente.
rK9 = http.post("/webhook/agent-bia-v2-shadow", json=_payload_http(999999999), headers=AUTH_HEADERS_SOMBRA)
check(rK9.status_code == 404, f"K9. conversation_id inexistente -> 404 (obteve {rK9.status_code})")

# K10. `extra="forbid"`: campo desconhecido no corpo -> 422, nada processado.
_corpo_k10 = _payload_http(cidK2)
_corpo_k10["event_id"] = "00000000-0000-4000-8000-000000000003"
rK10 = http.post("/webhook/agent-bia-v2-shadow", json=_corpo_k10, headers=AUTH_HEADERS_SOMBRA)
check(rK10.status_code == 422,
      f"K10. campo fora do contrato (event_id) -> 422 (obteve {rK10.status_code})")

# ---------------------------------------------------------------------------
# K11-K15. TETO DE TAMANHO do corpo. Cada pedido aceito paga UMA chamada de
# modelo; sem teto, 1.000.000 de caracteres ou 5.000 itens de lote passavam.
# O corpo grande tem de morrer no CONTRATO (422), antes de qualquer efeito -
# e o teto nao pode reprovar um lote realista, por isso os controles positivos
# K13/K15, que passam RASPANDO no limite e precisam ser aceitos.
# ---------------------------------------------------------------------------
_snap_bounds_antes = _snapshot_global(cidK2, leadK2)

# K11. mensagem_interpretacao acima de 16000 caracteres -> 422.
rK11 = http.post("/webhook/agent-bia-v2-shadow",
                 json=_payload_http(cidK2, mensagem="a" * 16001), headers=AUTH_HEADERS_SOMBRA)
check(rK11.status_code == 422,
      f"K11. mensagem_interpretacao com 16001 caracteres -> 422 (obteve {rK11.status_code})")

# K12. lote com mais de 50 itens -> 422.
_lote_grande = [{"msg_type": "text", "content": f"m{i}"} for i in range(51)]
rK12 = http.post("/webhook/agent-bia-v2-shadow",
                 json=_payload_http(cidK2, lote=_lote_grande), headers=AUTH_HEADERS_SOMBRA)
check(rK12.status_code == 422,
      f"K12. lote com 51 itens -> 422 (obteve {rK12.status_code})")

# K13. CONTROLE POSITIVO: exatamente no limite (16000 / 50) continua aceito -
#      o teto protege contra abuso sem cegar a observacao de um lote real.
_configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=False))
rK13 = http.post(
    "/webhook/agent-bia-v2-shadow",
    json=_payload_http(cidK2, mensagem="a" * 16000,
                       lote=[{"msg_type": "text", "content": f"m{i}"} for i in range(50)]),
    headers=AUTH_HEADERS_SOMBRA,
)
check(rK13.status_code == 200,
      f"K13. corpo exatamente no limite (16000 chars / 50 itens) -> 200 (obteve {rK13.status_code})")

# K14. O corpo gigante NAO chegou a chamar o Gemini: se tivesse chegado, esta
#      excecao de sabotagem teria virado INTERPRETACAO_FALHOU em vez de 422.
_configurar_ia(excecao_interpretador=AssertionError(
    "K14: o Gemini NAO pode ser chamado por um corpo reprovado no contrato"))
rK14 = http.post("/webhook/agent-bia-v2-shadow",
                 json=_payload_http(cidK2, mensagem="a" * 200000), headers=AUTH_HEADERS_SOMBRA)
check(rK14.status_code == 422,
      f"K14. corpo de 200k caracteres morre no contrato, sem tocar o Gemini "
      f"(obteve {rK14.status_code})")

# K15. mensagem_interpretacao VAZIA continua aceita DE PROPOSITO (sem
#      min_length): lote de reaction chega assim, e esse e justamente o
#      desfecho MENSAGEM_IGNORADA que a sombra existe para observar.
_configurar_ia(excecao_interpretador=AssertionError(
    "K15: pre-filtro ignora o lote, o Gemini nao deve ser chamado"))
rK15 = http.post(
    "/webhook/agent-bia-v2-shadow",
    json=_payload_http(cidK2, mensagem="", lote=[{"msg_type": "reaction", "content": "👍"}]),
    headers=AUTH_HEADERS_SOMBRA,
)
check(rK15.status_code == 200
      and rK15.json()["etapa_final"] == EtapaFinal.MENSAGEM_IGNORADA.value,
      f"K15. mensagem vazia + reaction -> 200/MENSAGEM_IGNORADA, nao 422 "
      f"(obteve {rK15.status_code}: {rK15.text[:160]})")

check(_snapshot_global(cidK2, leadK2) == _snap_bounds_antes,
      f"K16. as cinco chamadas de teto (422/422/200/422/200) nao mudaram NADA "
      f"(diferencas: {_diferencas(_snap_bounds_antes, _snapshot_global(cidK2, leadK2))})")


print()
print("=" * 70)
print("CENARIO L - garantia ESTRUTURAL (AST): o que a sombra nao pode alcancar")
print("=" * 70)

# ===========================================================================
# L. A prova por comportamento (A-K) cobre os caminhos exercitados. Esta cobre
#    os que NAO existem: os nomes abaixo nao aparecem em NENHUMA posicao de
#    codigo de `sombra.py` (docstrings e comentarios sao ignorados de
#    proposito - o modulo os MENCIONA em prosa justamente para explicar a
#    ausencia, e uma busca por string cruas falharia por causa disso).
# ===========================================================================
_FONTE_SOMBRA = (CONVERSAS_DIR / "app" / "v2" / "sombra.py").read_text(encoding="utf-8")
_ARVORE_SOMBRA = ast.parse(_FONTE_SOMBRA)

_nomes_no_codigo = set()
for _no in ast.walk(_ARVORE_SOMBRA):
    if isinstance(_no, ast.Name):
        _nomes_no_codigo.add(_no.id)
    elif isinstance(_no, ast.Attribute):
        _nomes_no_codigo.add(_no.attr)
    elif isinstance(_no, ast.alias):
        _nomes_no_codigo.add(_no.name.split(".")[-1])
        if _no.asname:
            _nomes_no_codigo.add(_no.asname)
    elif isinstance(_no, ast.ImportFrom) and _no.module:
        _nomes_no_codigo.update(_no.module.split("."))
    # Nomes de argumento nomeado (`f(add=...)`) e de parametro (`def f(add)`)
    # tambem entram: sem eles, `db.execute(update(...), execution_options=...)`
    # deixaria parte da expressao fora da varredura. Custa nada e fecha a
    # brecha de uma escrita entrar disfarcada de keyword.
    elif isinstance(_no, ast.keyword) and _no.arg:
        _nomes_no_codigo.add(_no.arg)
    elif isinstance(_no, ast.arg):
        _nomes_no_codigo.add(_no.arg)

_PROIBIDOS = {
    "V2_MODE": "a seguranca da sombra nao pode depender de configuracao (Cenario H)",
    "registrar_evento": "escreve conversation_events",
    "handoff_to_human": "unica funcao do pipeline com efeito real irreversivel",
    "autorizar_envio": "persiste AI_RESPONSE_DISCARDED em toda recusa",
    "aplicar_transicao": "escreve state/state_version/closed_at",
    "aplicar_estado_humano": "escreve is_bot_active/atendente_id/queued_at",
    "commit": "nenhum caminho da sombra commita",
    "TipoEvento": "so serve para construir evento",
    "ConversationEvent": "linha de evento",
    "HandoffOperation": "linha de operacao de handoff",
    # DML de nivel Core NAO passa por `before_flush` - a barreira da sessao nao
    # a enxerga (ver "ALCANCE EXATO DESTA CAMADA" no docstring de sombra.py).
    # Hoje quem cobriria esse ponto cego em execucao e so o rollback final;
    # estes nomes travam a porta em tempo de teste, que e mais cedo.
    "execute": "DML de Core (`db.execute(update(...))`) escapa do before_flush",
    "text": "`db.execute(text('UPDATE ...'))` escapa do before_flush",
    "update": "`update()` do Core escreve sem passar pela unidade de trabalho",
    "add": "poe objeto novo na sessao",
    "add_all": "poe objetos novos na sessao",
    "merge": "traz objeto para a sessao ja sujo",
    "flush": "forca a unidade de trabalho a escrever",
    "bulk_save_objects": "escrita em massa, fora da unidade de trabalho normal",
}
for _nome, _porque in _PROIBIDOS.items():
    check(_nome not in _nomes_no_codigo,
          f"L. `{_nome}` nao e alcancavel em sombra.py - {_porque}")

# L2. E o contrario: os modulos que ela DEVE reusar estao la (prova de que a
#     sombra compoe o caminho vivo em vez de reimplementar as regras).
#     `selecionar_atendente_sem_aviso` e o envelope silencioso de `selecao.py`
#     que ADIA a chamada da regra da V1 em vez de copia-la - a regra de menor
#     carga/desempate por id continua morando (e sendo executada) na V1.
for _reusado in ("avaliar_prefiltro", "avaliar_triagem", "selecionar_atendente_sem_aviso",
                 "motivo_recusa", "redigir_resposta", "interpretar_sem_efeito", "EtapaFinal"):
    check(_reusado in _nomes_no_codigo,
          f"L2. `{_reusado}` e reusado do caminho vivo, nao reimplementado")


print()
print("=" * 70)
print("CENARIO M - soltar a conexao para o Gemini NAO desarma a barreira")
print("=" * 70)

# ===========================================================================
# M. `simular_pipeline` devolve a conexao ao pool antes de CADA ida ao Gemini
#    (`_soltar_conexao`), para nao zerar o pool compartilhado com o webhook
#    real da Meta - o incidente AUDIT-2026-08-WF2 (D1) de webhook.py.
#    A pergunta que este cenario responde: isso enfraquece o efeito zero?
#    A barreira e registrada na INSTANCIA de `Session`, e `close()` mexe na
#    conexao, nao no dispatch de eventos.
# ===========================================================================
from app.v2.sombra import _soltar_conexao  # noqa: E402

_sessao_m = SessionLocal()
try:
    with _sessao_somente_leitura(_sessao_m):
        # Leitura -> transacao aberta, conexao retirada do pool.
        _sessao_m.get(Conversation, cidA)
        check(_sessao_m.in_transaction(),
              "M1. antes do release a sessao segura uma conexao (transacao aberta)")

        _soltar_conexao(_sessao_m)
        check(not _sessao_m.in_transaction(),
              "M2. depois de `_soltar_conexao` a conexao voltou ao pool "
              "(nenhuma transacao aberta durante a espera pelo Gemini)")

        # A MESMA sessao, ja solta: a barreira tem de continuar valendo.
        _levantou_m = False
        try:
            _sessao_m.add(ConversationEvent(
                event_id="00000000-0000-4000-8000-000000000004",
                event_type="MESSAGE_RECEIVED"))
            _sessao_m.flush()
        except EscritaProibidaNaSombra:
            _levantou_m = True
        check(_levantou_m,
              "M3. BARREIRA AINDA ARMADA depois do release - escrita na sessao "
              "solta continua morrendo em EscritaProibidaNaSombra")
        _sessao_m.rollback()

        # Reabre a transacao (o que a proxima consulta do fluxo real faz) e
        # confirma que a barreira sobrevive tambem ao segundo ciclo.
        _sessao_m.get(Conversation, cidA, populate_existing=True)
        _levantou_m2 = False
        try:
            _sessao_m.add(ConversationEvent(
                event_id="00000000-0000-4000-8000-000000000005",
                event_type="MESSAGE_RECEIVED"))
            _sessao_m.flush()
        except EscritaProibidaNaSombra:
            _levantou_m2 = True
        check(_levantou_m2,
              "M4. barreira ainda armada depois da transacao REABRIR "
              "(release -> nova consulta -> nova transacao)")
        _sessao_m.rollback()
finally:
    _sessao_m.close()

# M5. `_soltar_conexao` RECUSA soltar uma sessao suja. Sem isto, `close()`
#     descartaria a escrita pendente em silencio - trocaria o 500 ruidoso da
#     barreira por um sumico calado, que e o desfecho que este modulo proibe.
_sessao_m2 = SessionLocal()
try:
    _levantou_m5 = False
    try:
        with _sessao_somente_leitura(_sessao_m2):
            _sessao_m2.add(ConversationEvent(
                event_id="00000000-0000-4000-8000-000000000006",
                event_type="MESSAGE_RECEIVED"))
            _soltar_conexao(_sessao_m2)
    except EscritaProibidaNaSombra:
        _levantou_m5 = True
    check(_levantou_m5,
          "M5. soltar a conexao com a sessao SUJA levanta EscritaProibidaNaSombra "
          "(close() nao vira descarte silencioso)")
finally:
    _sessao_m2.close()

_sessao_m3 = SessionLocal()
try:
    check(
        _sessao_m3.query(ConversationEvent).filter(
            ConversationEvent.event_id.in_((
                "00000000-0000-4000-8000-000000000004",
                "00000000-0000-4000-8000-000000000005",
                "00000000-0000-4000-8000-000000000006",
            ))).count() == 0,
        "M6. nenhuma das linhas barradas no Cenario M chegou ao banco",
    )
finally:
    _sessao_m3.close()


print()
print("=" * 70)
print("CENARIO N - a sombra nao emite o WARNING operacional da V1")
print("=" * 70)

# ===========================================================================
# N. `resolver_atendente_elegivel` (V1) loga
#    "Handoff sem atendente elegivel - conversa entra na fila sem dono" quando
#    nao ha ninguem elegivel. A frase e byte a byte a da V1 e nao tem marcador:
#    durante a janela de observacao ela produziria uma rajada de avisos de
#    "conversa sem dono" para conversas que a sombra NUNCA enfileirou - sinal
#    operacional falso. `selecionar_atendente_sem_aviso` existe para isso.
#    O caminho VIVO continua logando (N3): la o aviso e verdadeiro.
# ===========================================================================
import logging  # noqa: E402

from app.v2.selecao import selecionar_atendente, selecionar_atendente_sem_aviso  # noqa: E402


class _CapturaLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.mensagens = []

    def emit(self, record):
        self.mensagens.append(record.getMessage())


_logger_v1 = logging.getLogger("app.services.atendimento")
_captura = _CapturaLog()
_logger_v1.addHandler(_captura)
_nivel_original = _logger_v1.level
_logger_v1.setLevel(logging.WARNING)
try:
    _sem_atendente_disponivel()
    _configurar_ia(resposta_interpretador=_resposta_ia(explicit_human_request=True))
    cidN, leadN = criar_conversa_legacy()

    _captura.mensagens.clear()
    resN = com_prova_de_efeito_zero(
        "N", cidN, leadN, lambda: simular_pipeline(db, _entrada(cidN)))
    _avisos_sombra = [m for m in _captura.mensagens if "sem atendente elegivel" in m]
    check(not _avisos_sombra,
          f"N1. a sombra NAO emitiu o warning de 'conversa sem dono' "
          f"(capturou {_avisos_sombra!r})")
    check(resN.handoff_sugerido is True and resN.atendente_sugerido_id is None,
          f"N2. e o desfecho e o MESMO de antes: queria encaminhar, sem quem receber "
          f"(obteve {resN.handoff_sugerido!r}/{resN.atendente_sugerido_id!r})")

    # N3. Controle negativo: a funcao do caminho VIVO continua avisando. Sem
    #     isto, N1 passaria trivialmente se o logger estivesse mudo por engano.
    _captura.mensagens.clear()
    _resultado_vivo = selecionar_atendente(db)
    check(any("sem atendente elegivel" in m for m in _captura.mensagens),
          f"N3. CONTROLE: `selecionar_atendente` (caminho vivo) AINDA avisa "
          f"(capturou {_captura.mensagens!r})")

    # N4. Os dois devolvem o MESMO valor - o silencio nao mudou a selecao.
    #     Sem atendente: ambos None. Com atendente: ambos o mesmo id.
    check(_resultado_vivo == selecionar_atendente_sem_aviso(db) == None,  # noqa: E711
          "N4a. sem atendente elegivel, vivo e sombra devolvem o mesmo None")
    _com_atendente_disponivel()
    check(selecionar_atendente(db) == selecionar_atendente_sem_aviso(db) == ID_ATENDENTE,
          f"N4b. com atendente elegivel, vivo e sombra devolvem o MESMO id "
          f"(obteve {selecionar_atendente(db)!r} vs "
          f"{selecionar_atendente_sem_aviso(db)!r}, esperado {ID_ATENDENTE})")
finally:
    _logger_v1.removeHandler(_captura)
    _logger_v1.setLevel(_nivel_original)


fastapi_app.dependency_overrides.clear()
db.close()

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
