"""
BIA-V2 Fase 7 - Authorization gate (`conversas/app/v2/gate.py`).

Fonte normativa: docs/superpowers/plans/2026-08-29-bia-v2.md, secao "Fase 7 -
Authorization gate". `autorizar_envio()` e a ultima checagem antes de a
resposta que a IA gerou ser realmente enviada - rele o estado ATUAL da
conversa e recusa se algo mudou enquanto a IA processava.

Cobre, na ordem em que `autorizar_envio` as checa:

  1. `state_version` divergente
  2. `state` divergente (mesma version - checagem independente de 1)
  3. bot inativo (`is_bot_active is False`)
  4. humano assumiu (`atendente_id is not None`)
  5. janela Meta fechada (`last_customer_msg_at` antigo ou None)

Mais o caminho feliz (nada mudou - devolve True, zero evento), o erro de
chamador (`conversation_id` inexistente) e o TESTE DO BRIEFING: duas SESSOES
SEPARADAS (simulando dois requests concorrentes) provam que a "releitura sob
lock" e real - sem `db.expire_all()` dentro do gate, a sessao da IA
devolveria o objeto CACHEADO (state_version=10) mesmo depois do commit do
handoff em outra sessao, e o bug que este WP existe para fechar passaria
despercebido.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_v2_gate.py
"""
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_gate_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


# Modelos com relationship() por NOME precisam estar registrados antes de
# qualquer query (mapper configuration resolve "Message"/"ConversationTag"/
# "ConversationNote"/"MediaAsset") - mesmo padrao de tests/test_v2_estados.py.
import app.models.media_asset  # noqa: E402,F401
import app.models.note  # noqa: E402,F401
import app.models.tag  # noqa: E402,F401
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402
from app.services.atendimento import aplicar_estado_humano  # noqa: E402
from app.v2.estados import Estado, aplicar_transicao  # noqa: E402
from app.v2.eventos_validacao import TipoEvento  # noqa: E402
from app.v2.gate import autorizar_envio  # noqa: E402

Base.metadata.create_all(bind=engine, tables=[Conversation.__table__, ConversationEvent.__table__])
db = SessionLocal()

_contador_wa = iter(range(1, 1000))


def _criar_conversa(*, state, state_version, is_bot_active=True, atendente_id=None,
                     last_customer_msg_at=None, session=None, status="aberta"):
    """Conversa V2 minima, com os campos que `autorizar_envio` le definidos
    diretamente (nao via `aplicar_transicao` - a maioria dos casos aqui testa
    UMA condicao isolada, nao uma transicao valida de verdade)."""
    s = session or db
    conv = Conversation(
        lead_id=1,
        whatsapp=f"5511900{next(_contador_wa):06d}",
        status=status,
        unread_count=0,
        is_bot_active=is_bot_active,
        atendente_id=atendente_id,
        state=state,
        state_version=state_version,
        last_customer_msg_at=last_customer_msg_at,
    )
    s.add(conv)
    s.commit()
    return conv.id


def _eventos_discard(cid, session=None):
    s = session or db
    return (
        s.query(ConversationEvent)
        .filter(
            ConversationEvent.conversation_id == cid,
            ConversationEvent.event_type == TipoEvento.AI_RESPONSE_DISCARDED.value,
        )
        .all()
    )


_AGORA = datetime.now(timezone.utc)

print("=== Fase 7 - authorization gate ===\n")

# ---------------------------------------------------------------------------
# 1. Caminho feliz - nada mudou -> True, zero evento
# ---------------------------------------------------------------------------
cid_ok = _criar_conversa(state="AI_TRIAGE", state_version=5, is_bot_active=True,
                          atendente_id=None, last_customer_msg_at=_AGORA)
resultado = autorizar_envio(db, conversation_id=cid_ok, state_esperado="AI_TRIAGE", version_esperada=5)
check(resultado is True, f"1. nada mudou -> True (obteve {resultado})")
check(len(_eventos_discard(cid_ok)) == 0, "1b. nenhum AI_RESPONSE_DISCARDED emitido no caminho feliz")

# state_esperado tambem aceita um membro de Estado (StrEnum), nao so a string crua.
resultado_enum = autorizar_envio(db, conversation_id=cid_ok, state_esperado=Estado.AI_TRIAGE, version_esperada=5)
check(resultado_enum is True, f"1c. state_esperado=Estado.AI_TRIAGE (enum) tambem funciona (obteve {resultado_enum})")
check(len(_eventos_discard(cid_ok)) == 0, "1d. ainda nenhum evento apos a chamada com Estado")

# ---------------------------------------------------------------------------
# 2. state_version divergente
# ---------------------------------------------------------------------------
cid_ver = _criar_conversa(state="AI_TRIAGE", state_version=6, is_bot_active=True,
                           atendente_id=None, last_customer_msg_at=_AGORA)
resultado = autorizar_envio(db, conversation_id=cid_ver, state_esperado="AI_TRIAGE", version_esperada=5)
check(resultado is False, f"2. state_version divergente -> False (obteve {resultado})")
eventos = _eventos_discard(cid_ver)
check(len(eventos) == 1, f"2b. exatamente 1 evento emitido (achou {len(eventos)})")
check(
    bool(eventos) and eventos[0].error_code == "STATE_VERSION_DIVERGENTE",
    f"2c. error_code correto (obteve {eventos[0].error_code if eventos else None})",
)
check(bool(eventos) and eventos[0].result == "ignorado", f"2d. result='ignorado' (obteve {eventos[0].result if eventos else None})")
check(bool(eventos) and eventos[0].conversation_id == cid_ver, "2e. evento carrega o conversation_id certo")

# ---------------------------------------------------------------------------
# 3. state divergente, MESMA version (checagem independente de 2)
# ---------------------------------------------------------------------------
cid_state = _criar_conversa(state="CLOSED", state_version=5, is_bot_active=True,
                             atendente_id=None, last_customer_msg_at=_AGORA)
resultado = autorizar_envio(db, conversation_id=cid_state, state_esperado="AI_TRIAGE", version_esperada=5)
check(resultado is False, f"3. state divergente (mesma version) -> False (obteve {resultado})")
eventos = _eventos_discard(cid_state)
check(
    len(eventos) == 1 and eventos[0].error_code == "STATE_DIVERGENTE",
    f"3b. error_code=STATE_DIVERGENTE (achou {[e.error_code for e in eventos]})",
)

# ---------------------------------------------------------------------------
# 4. bot inativo
# ---------------------------------------------------------------------------
cid_bot = _criar_conversa(state="AI_TRIAGE", state_version=5, is_bot_active=False,
                           atendente_id=None, last_customer_msg_at=_AGORA)
resultado = autorizar_envio(db, conversation_id=cid_bot, state_esperado="AI_TRIAGE", version_esperada=5)
check(resultado is False, f"4. bot inativo -> False (obteve {resultado})")
eventos = _eventos_discard(cid_bot)
check(
    len(eventos) == 1 and eventos[0].error_code == "BOT_INATIVO",
    f"4b. error_code=BOT_INATIVO (achou {[e.error_code for e in eventos]})",
)

# ---------------------------------------------------------------------------
# 5. humano assumiu (atendente_id preenchido)
# ---------------------------------------------------------------------------
cid_at = _criar_conversa(state="AI_TRIAGE", state_version=5, is_bot_active=True,
                          atendente_id=99, last_customer_msg_at=_AGORA)
resultado = autorizar_envio(db, conversation_id=cid_at, state_esperado="AI_TRIAGE", version_esperada=5)
check(resultado is False, f"5. atendente_id preenchido -> False (obteve {resultado})")
eventos = _eventos_discard(cid_at)
check(
    len(eventos) == 1 and eventos[0].error_code == "HUMANO_ASSUMIU",
    f"5b. error_code=HUMANO_ASSUMIU (achou {[e.error_code for e in eventos]})",
)

# ---------------------------------------------------------------------------
# 5.1 conversa ENCERRADA (R8/R10 - o furo do `status`, inventario secao 0.5)
#
# O cenario EXATO, e nao uma aproximacao: um humano manda
# `PUT /api/conversations/{id}` com {"status":"encerrada"} ISOLADO, sem claim
# antes. `update_conversation` so escreve `conversation.status` nesse ramo -
# NAO desliga o bot, NAO define atendente_id, NAO toca state/state_version
# (a V1 nunca os toca, R8). Entao as cinco condicoes ANTIGAS passavam todas e a
# IA era autorizada a falar numa conversa que o humano acabara de encerrar.
#
# Este e o caso em que a resposta em voo precisa ser invalidada por INTERVENCAO
# HUMANA, e o unico sinal que a V1 deixa e o proprio `status`.
# ---------------------------------------------------------------------------
cid_encerrada = _criar_conversa(state="AI_TRIAGE", state_version=5, is_bot_active=True,
                                 atendente_id=None, last_customer_msg_at=_AGORA)
# A IA le o estado e "comeca a gerar".
_state_lido, _versao_lida = "AI_TRIAGE", 5
# O humano encerra pela V1 - a MESMA escrita que `update_conversation` faz.
_conv_encerrada = db.query(Conversation).filter(Conversation.id == cid_encerrada).first()
_conv_encerrada.status = "encerrada"
db.commit()
check(
    (_conv_encerrada.state, _conv_encerrada.state_version, _conv_encerrada.is_bot_active)
    == ("AI_TRIAGE", 5, True),
    f"5.1a pre-condicao REAL: o encerramento V1 nao mexeu em state/state_version "
    f"nem desligou o bot (state={_conv_encerrada.state!r}, "
    f"v={_conv_encerrada.state_version}, bot={_conv_encerrada.is_bot_active}) - "
    f"sem isso a prova seria vazia, porque outra condicao pegaria o caso",
)
resultado = autorizar_envio(
    db, conversation_id=cid_encerrada, state_esperado=_state_lido, version_esperada=_versao_lida
)
check(
    resultado is False,
    f"5.1b (prop.7) a resposta em voo e INVALIDADA depois de o humano encerrar "
    f"(obteve {resultado})",
)
eventos = _eventos_discard(cid_encerrada)
check(
    len(eventos) == 1 and eventos[0].error_code == "CONVERSA_ENCERRADA",
    f"5.1c error_code=CONVERSA_ENCERRADA (achou {[e.error_code for e in eventos]})",
)

# 'aguardando' (status LEGADO e derivado) tambem nao autoriza - fail closed.
cid_aguardando = _criar_conversa(state="AI_TRIAGE", state_version=5, is_bot_active=True,
                                  atendente_id=None, last_customer_msg_at=_AGORA,
                                  status="aguardando")
resultado = autorizar_envio(db, conversation_id=cid_aguardando, state_esperado="AI_TRIAGE",
                            version_esperada=5)
check(
    resultado is False and _eventos_discard(cid_aguardando)[0].error_code == "CONVERSA_ENCERRADA",
    f"5.1d o status LEGADO 'aguardando' tambem recusa (fail closed) - so 'aberta' "
    f"autoriza (obteve {resultado})",
)

# ---------------------------------------------------------------------------
# 6. janela Meta fechada - last_customer_msg_at antigo
# ---------------------------------------------------------------------------
cid_janela_velha = _criar_conversa(state="AI_TRIAGE", state_version=5, is_bot_active=True,
                                    atendente_id=None, last_customer_msg_at=_AGORA - timedelta(hours=25))
resultado = autorizar_envio(db, conversation_id=cid_janela_velha, state_esperado="AI_TRIAGE", version_esperada=5)
check(resultado is False, f"6. last_customer_msg_at ha 25h -> False (obteve {resultado})")
eventos = _eventos_discard(cid_janela_velha)
check(
    len(eventos) == 1 and eventos[0].error_code == "JANELA_FECHADA",
    f"6b. error_code=JANELA_FECHADA (achou {[e.error_code for e in eventos]})",
)

# ---------------------------------------------------------------------------
# 7. janela Meta fechada - last_customer_msg_at None
# ---------------------------------------------------------------------------
cid_janela_none = _criar_conversa(state="AI_TRIAGE", state_version=5, is_bot_active=True,
                                   atendente_id=None, last_customer_msg_at=None)
resultado = autorizar_envio(db, conversation_id=cid_janela_none, state_esperado="AI_TRIAGE", version_esperada=5)
check(resultado is False, f"7. last_customer_msg_at=None -> False (obteve {resultado})")
eventos = _eventos_discard(cid_janela_none)
check(
    len(eventos) == 1 and eventos[0].error_code == "JANELA_FECHADA",
    f"7b. error_code=JANELA_FECHADA com last_customer_msg_at=None (achou {[e.error_code for e in eventos]})",
)

# ---------------------------------------------------------------------------
# 8. conversation_id inexistente -> erro de chamador, nao uma sexta condicao
# ---------------------------------------------------------------------------
levantou = False
try:
    autorizar_envio(db, conversation_id=999999999, state_esperado="AI_TRIAGE", version_esperada=1)
except ValueError:
    levantou = True
check(levantou, "8. conversation_id inexistente levanta ValueError (nao devolve False em silencio)")

# ---------------------------------------------------------------------------
# 9. TESTE DO BRIEFING - duas sessoes separadas, releitura sob lock de verdade
# ---------------------------------------------------------------------------
# IA comeca em state_version=10 (sessao propria, `db_ia`). Em PARALELO (outra
# sessao, `db_humano` - simula outro request), um humano assume de verdade:
# aplicar_estado_humano() (atendimento.py, V1, so LEITURA aqui - nao mexido)
# seguido de aplicar_transicao() (estados.py, Fase 4) para AI_TRIAGE ->
# WAITING_HUMAN, que incrementa state_version para 11 - a MESMA composicao
# que a Fase 6 (handoff, ainda bloqueada) fara de verdade.
#
# Se `autorizar_envio` nao descartasse o cache de `db_ia` antes de reler, a
# query devolveria o objeto JA CARREGADO por `db_ia` (state_version=10,
# obsoleto) em vez do valor persistido por `db_humano` - o bug que este WP
# existe para fechar passaria despercebido. Duas sessoes tornam esse risco
# real no teste, nao so teorico.
db_ia = SessionLocal()
db_humano = SessionLocal()

cid_briefing = _criar_conversa(
    state="AI_TRIAGE", state_version=10, is_bot_active=True,
    atendente_id=None, last_customer_msg_at=_AGORA, session=db,
)

conv_ia = db_ia.query(Conversation).filter(Conversation.id == cid_briefing).first()
check(
    conv_ia is not None and conv_ia.state_version == 10,
    f"9a. pre-condicao: a sessao da IA le state_version=10 (obteve {conv_ia and conv_ia.state_version})",
)

ATENDENTE_ID_TESTE = 42
conv_humano = db_humano.query(Conversation).filter(Conversation.id == cid_briefing).first()
conv_humano.responsavel_id = ATENDENTE_ID_TESTE
aplicar_estado_humano(conv_humano, ATENDENTE_ID_TESTE)
aplicar_transicao(conv_humano, Estado.WAITING_HUMAN, motivo="handoff_teste")
db_humano.commit()
check(
    conv_humano.state_version == 11,
    f"9b. pre-condicao: o handoff (outra sessao) levou state_version a 11 (obteve {conv_humano.state_version})",
)
db_humano.close()

# A IA "termina" de gerar e chama o gate NA PROPRIA SESSAO (db_ia), que ainda
# tem `conv_ia` com state_version=10 na identity map.
resultado = autorizar_envio(db_ia, conversation_id=cid_briefing, state_esperado="AI_TRIAGE", version_esperada=10)
check(
    resultado is False,
    f"9c. BRIEFING: IA version_esperada=10, humano assumiu (virou 11) -> resposta descartada (obteve {resultado})",
)
eventos = _eventos_discard(cid_briefing, session=db_ia)
check(
    len(eventos) == 1 and eventos[0].error_code == "STATE_VERSION_DIVERGENTE",
    f"9d. evento emitido com error_code=STATE_VERSION_DIVERGENTE (achou {[e.error_code for e in eventos]})",
)
db_ia.close()


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
