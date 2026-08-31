"""
BIA-V2 Fase 0 / Task 0.2 - contrato de validacao de conversation_events.

Cobre o CONTRATO por campo/payload de `conversas/app/v2/eventos_validacao.py`,
consumido por `registrar_evento()`: tipos, allowlist de payload, limites de
tamanho, formato de token, intervalo inteiro, canonicalizacao de UUID e
sanitizacao de mensagem de excecao. Testes de PERSISTENCIA/IDEMPOTENCIA
(UNIQUE no banco, EventoDuplicado, commit/SAVEPOINT) ficam em
`tests/test_v2_eventos.py` - a mesma divisao dos dois modulos-fonte.

Roda standalone:  python tests/test_v2_eventos_validacao.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_eventos_validacao_test.db"
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


from app.database import Base, SessionLocal, engine  # noqa: E402
from app.v2.eventos import registrar_evento  # noqa: E402
from app.v2.eventos_validacao import (  # noqa: E402
    CHAVES_PAYLOAD_PERMITIDAS,
    CampoInvalido,
    OrigemEvento,
    PayloadInvalido,
    ResultadoEvento,
    TipoEvento,
)

Base.metadata.create_all(bind=engine)
db = SessionLocal()


def _tenta_registrar(tipo=TipoEvento.MESSAGE_RECEIVED, **kwargs):
    """Chama registrar_evento (commit=True por padrao) e devolve (ok, excecao)."""
    kwargs.setdefault("commit", True)
    try:
        registrar_evento(db, tipo=tipo, **kwargs)
        return True, None
    except Exception as exc:  # noqa: BLE001 - queremos inspecionar QUALQUER tipo levantado
        return False, exc


# --- 1. Os 18 tipos exigidos pelo WP existem -------------------------------
TIPOS_EXIGIDOS = {
    "MESSAGE_RECEIVED", "MESSAGE_IGNORED",
    "AI_INTERPRETATION_STARTED", "AI_INTERPRETATION_COMPLETED", "AI_INTERPRETATION_FAILED",
    "TRIAGE_DATA_UPDATED", "TRIAGE_COMPLETED",
    "HANDOFF_REQUESTED", "HUMAN_SELECTED", "HANDOFF_COMPLETED", "HANDOFF_FAILED",
    "HUMAN_FIRST_RESPONSE",
    "AI_RESPONSE_GENERATED", "AI_RESPONSE_DISCARDED",
    "MESSAGE_SENT", "MESSAGE_SEND_FAILED",
    "CONVERSATION_CLOSED", "CONVERSATION_REOPENED",
}
declarados = {t.value for t in TipoEvento}
check(TIPOS_EXIGIDOS <= declarados, f"1. os 18 tipos do WP existem (faltam: {TIPOS_EXIGIDOS - declarados})")

# --- 2. Todos os campos do contrato do WP existem e gravam o valor certo --
ev_completo = registrar_evento(
    db,
    tipo=TipoEvento.HANDOFF_COMPLETED,
    commit=True,
    conversation_id=42,
    lead_id=7,
    message_id=99,
    whatsapp_msg_id="wamid.TESTE123",
    state_before="AI_TRIAGE",
    state_after="WAITING_HUMAN",
    action="handoff_to_human",
    target_user_id=5,
    model="gemini-2.5-flash",
    model_attempt=2,
    duration_ms=1234,
    result="sucesso",
    error_code=None,
    payload={"origem": "webhook"},
)
campos = [
    "event_id", "event_type", "conversation_id", "lead_id", "message_id",
    "whatsapp_msg_id", "state_before", "state_after", "action", "target_user_id",
    "model", "model_attempt", "duration_ms", "result", "error_code", "created_at",
]
faltando = [c for c in campos if not hasattr(ev_completo, c)]
check(not faltando, f"2. todos os campos do contrato existem (faltam: {faltando})")
check(ev_completo.target_user_id == 5 and ev_completo.duration_ms == 1234, "2b. valores gravados corretamente")

# --- 3. Payload: allowlist de chaves tipada --------------------------------
check(CHAVES_PAYLOAD_PERMITIDAS == frozenset({"origem"}), "3. allowlist tem exatamente 1 chave: origem")
proibidas = {"content", "mensagem", "texto", "whatsapp", "telefone", "nome", "email", "token"}
check(not (proibidas & CHAVES_PAYLOAD_PERMITIDAS), "3b. allowlist NAO contem chave de PII/segredo")

ok, exc = _tenta_registrar(payload={"content": "oi, quero viajar"})
check(not ok and isinstance(exc, PayloadInvalido), "4. chave fora da allowlist levanta PayloadInvalido")

ok, exc = _tenta_registrar(payload={"origem": {"aninhado": True}})
check(not ok and isinstance(exc, PayloadInvalido), "5. valor de tipo errado (dict onde se espera enum) levanta PayloadInvalido")

ev_payload_completo = registrar_evento(
    db, tipo=TipoEvento.TRIAGE_DATA_UPDATED, commit=True,
    payload={"origem": OrigemEvento.WEBHOOK},
)
check(
    ev_payload_completo.payload["origem"] == "webhook",
    "6. a chave tipada aceita valor valido (enum por membro OU por string exata)",
)

ev_payload_str = registrar_evento(
    db, tipo=TipoEvento.TRIAGE_DATA_UPDATED, commit=True, payload={"origem": "replay"},
)
check(ev_payload_str.payload["origem"] == "replay", "6b. origem aceita a string exata do enum")

# `explicit_human_request` saiu da Fase 0: o argumento de bool primitivo sem
# import cobria acoplamento de IMPORT, nao o SEMANTICO. O fato pertence ao
# dominio de interpretacao/handoff, nao a infraestrutura neutra de eventos.
for chave_removida in ("motivo", "intent", "tentativa", "prefiltro_motivo",
                       "campos_faltantes", "explicit_human_request"):
    ok, exc = _tenta_registrar(payload={chave_removida: "qualquer coisa"})
    check(not ok and isinstance(exc, PayloadInvalido), f"7. chave removida {chave_removida!r} e rejeitada como fora da allowlist")

VALORES_MALICIOSOS = [
    "5551999999999",                              # telefone
    "cliente@gmail.com",                          # e-mail
    "eyJhbGciOiJIUzI1NiJ9.abc",                   # token JWT
    "quero viajar em janeiro com minha esposa",   # frase de cliente
]
for v in VALORES_MALICIOSOS:
    ok, _ = _tenta_registrar(payload={"origem": v})
    check(not ok, f"8. origem rejeita valor fora do vocabulario fechado ({v!r})")

# --- 9. RecursionError vira PayloadInvalido, nao escapa cru ----------------
lista_aninhada = []
for _ in range(2000):
    lista_aninhada = [lista_aninhada]
ok, exc = _tenta_registrar(payload={"origem": lista_aninhada})
check(
    not ok and isinstance(exc, PayloadInvalido) and not isinstance(exc, RecursionError),
    f"9. lista profundamente aninhada no payload vira PayloadInvalido, nunca RecursionError (exc={exc!r})",
)

# --- 10. int patologico (chave de payload) vira PayloadInvalido, nao ValueError cru
ok = False
levantou_tipado = False
vazou_cru = False
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, commit=True, payload={10 ** 5000: "x"})
except PayloadInvalido:
    levantou_tipado = True
except ValueError:
    vazou_cru = True
check(
    levantou_tipado and not vazou_cru,
    "10. chave de payload com int de milhares de digitos vira PayloadInvalido, nao ValueError cru",
)

# --- 11. event_id: UUID de verdade, canonicalizado -------------------------
ok, exc = _tenta_registrar(event_id="x" * 36)
check(not ok and isinstance(exc, CampoInvalido) and exc.campo == "event_id", "11. event_id com 36 chars que NAO e UUID e rejeitado")

ev_uuid_valido = registrar_evento(
    db, tipo=TipoEvento.MESSAGE_RECEIVED, commit=True, event_id="0f8fad5b-d9cb-469f-a165-70867728950e",
)
check(ev_uuid_valido.event_id == "0f8fad5b-d9cb-469f-a165-70867728950e", "11b. event_id UUID bem formado e aceito")

# --- 12. whatsapp_msg_id: NUL e caractere de controle rejeitados -----------
ok, exc = _tenta_registrar(whatsapp_msg_id="wamid.TESTE\x00123")
check(not ok and isinstance(exc, CampoInvalido) and exc.campo == "whatsapp_msg_id", "12. whatsapp_msg_id com NUL e rejeitado")
ok, exc = _tenta_registrar(whatsapp_msg_id="wamid.TESTE\x01123")
check(not ok and isinstance(exc, CampoInvalido) and exc.campo == "whatsapp_msg_id", "12b. whatsapp_msg_id com caractere de controle e rejeitado")

# --- 12c. model/whatsapp_msg_id: string vazia carrega zero informacao -----
# Ausente = None ("nao informado"); "" nao e um valor de dominio valido para
# nenhum dos dois. Sem esta checagem, "" passava direto: nem o limite de
# tamanho nem a regex de caractere proibido rejeitam string vazia.
ok, exc = _tenta_registrar(model="")
check(not ok and isinstance(exc, CampoInvalido) and exc.campo == "model", f"12c. model='' e rejeitado - ausente e None, nao string vazia (exc={exc!r})")
ok, exc = _tenta_registrar(whatsapp_msg_id="")
check(not ok and isinstance(exc, CampoInvalido) and exc.campo == "whatsapp_msg_id", f"12d. whatsapp_msg_id='' e rejeitado - ausente e None, nao string vazia (exc={exc!r})")

# --- 13. Toda coluna string tem limite de tamanho validado em Python ------
CASOS_LIMITE = [
    ("whatsapp_msg_id", "w" * 100, "w" * 101),
    ("model", "m" * 64, "m" * 65),
    ("state_before", "A" * 32, "A" * 33),
    ("action", "a" * 64, "a" * 65),
    ("error_code", "A" * 64, "A" * 65),
]
for campo, valor_no_limite, valor_acima in CASOS_LIMITE:
    ok_no_limite, exc_no_limite = _tenta_registrar(**{campo: valor_no_limite})
    check(ok_no_limite, f"13. {campo} no limite exato ({len(valor_no_limite)} chars) passa (exc={exc_no_limite!r})")
    ok_acima, exc_acima = _tenta_registrar(**{campo: valor_acima})
    check(
        not ok_acima and isinstance(exc_acima, CampoInvalido) and exc_acima.campo == campo,
        f"13. {campo} acima do limite ({len(valor_acima)} chars) levanta CampoInvalido (exc={exc_acima!r})",
    )

# --- 14. Campos inteiros: bool/tipo/min/max validados em Python -----------
_INT4_MAX = 2_147_483_647
CASOS_INTEIRO = [
    ("conversation_id", 1),
    ("lead_id", 1),
    ("message_id", 1),
    ("target_user_id", 1),
    ("model_attempt", 0),
    ("duration_ms", 0),
]
for campo, minimo in CASOS_INTEIRO:
    ok, exc = _tenta_registrar(**{campo: True})
    check(not ok and isinstance(exc, CampoInvalido) and exc.campo == campo, f"14. {campo}: bool rejeitado")

    ok, exc = _tenta_registrar(**{campo: "42"})
    check(not ok and isinstance(exc, CampoInvalido) and exc.campo == campo, f"14. {campo}: tipo errado (str) rejeitado")

    ok, exc = _tenta_registrar(**{campo: minimo})
    check(ok, f"14. {campo}: minimo permitido ({minimo}) aceito (exc={exc!r})")

    ok, exc = _tenta_registrar(**{campo: _INT4_MAX})
    check(ok, f"14. {campo}: maximo int4 ({_INT4_MAX}) aceito (exc={exc!r})")

    ok, exc = _tenta_registrar(**{campo: minimo - 1})
    check(not ok and isinstance(exc, CampoInvalido) and exc.campo == campo, f"14. {campo}: abaixo do minimo ({minimo - 1}) rejeitado")

    ok, exc = _tenta_registrar(**{campo: _INT4_MAX + 1})
    check(not ok and isinstance(exc, CampoInvalido) and exc.campo == campo, f"14. {campo}: acima do maximo int4 rejeitado")

# --- 15. tipo aceita TipoEvento ou string valida; result e enum fechado ---
ev_tipo_str = registrar_evento(db, tipo="MESSAGE_RECEIVED", commit=True)
check(ev_tipo_str.event_type == "MESSAGE_RECEIVED", "15a. tipo aceita string valida e converte")

ok, exc = _tenta_registrar(tipo="NAO_EXISTE")
check(not ok and isinstance(exc, CampoInvalido) and not isinstance(exc, AttributeError), f"15b. tipo com string invalida levanta CampoInvalido (exc={exc!r})")

ok, exc = _tenta_registrar(tipo=123)
check(not ok and isinstance(exc, CampoInvalido) and not isinstance(exc, AttributeError), f"15c. tipo de tipo errado (int) levanta CampoInvalido (exc={exc!r})")

ev_result_enum = registrar_evento(db, tipo=TipoEvento.MESSAGE_SENT, commit=True, result=ResultadoEvento.FALHA)
check(ev_result_enum.result == "falha", "15d. result aceita membro do enum ResultadoEvento")

ok, exc = _tenta_registrar(result="parcial")
check(not ok and isinstance(exc, CampoInvalido) and exc.campo == "result", f"15e. result fora do vocabulario levanta CampoInvalido (exc={exc!r})")

# --- 16. action/error_code: alem de tamanho, o FORMATO tambem e exigido --
ok, exc = _tenta_registrar(action="Nao_Snake")
check(not ok and isinstance(exc, CampoInvalido) and exc.campo == "action", f"16a. action fora do formato ^[a-z][a-z0-9_]*$ e rejeitado (exc={exc!r})")
ok, exc = _tenta_registrar(error_code="minusculo")
check(not ok and isinstance(exc, CampoInvalido) and exc.campo == "error_code", f"16b. error_code fora do formato ^[A-Z][A-Z0-9_]*$ e rejeitado (exc={exc!r})")

# --- 17. Excecoes NUNCA ecoam o valor recebido em str(exc) -----------------
SENSIVEL = "5551999999999-dado-sensivel-do-cliente"

ok, exc = _tenta_registrar(event_id=SENSIVEL)
check(not ok and isinstance(exc, CampoInvalido) and SENSIVEL not in str(exc), "17a. event_id invalido nao ecoa o valor recebido")

ok, exc = _tenta_registrar(tipo=SENSIVEL)
check(not ok and isinstance(exc, CampoInvalido) and SENSIVEL not in str(exc), "17b. tipo invalido nao ecoa o valor recebido")

ok, exc = _tenta_registrar(result=SENSIVEL)
check(not ok and isinstance(exc, CampoInvalido) and SENSIVEL not in str(exc), "17c. result invalido nao ecoa o valor recebido")

ok = False
mensagem = ""
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, commit=True, payload={SENSIVEL: "x"})
except PayloadInvalido as exc:
    ok = True
    mensagem = str(exc)
check(ok and SENSIVEL not in mensagem, "17d. chave de payload desconhecida nao ecoa o valor recebido")

# --- 18. V1 intacta: a tabela nova nao aparece no model da conversa ------
# Import feito por ULTIMO de proposito: `Conversation.tags` referencia
# `ConversationTag`/`conversation_tag_links` por string, e este arquivo nunca
# importa esses modelos. Fora de ordem, o import registraria um mapper
# incompleto e QUALQUER `ConversationEvent(...)` construido depois dispararia
# `InvalidRequestError` ao tentar configurar TODOS os mappers pendentes.
from app.models.conversation import Conversation  # noqa: E402

check(
    not any("event" in c.name for c in Conversation.__table__.columns),
    "18. tabela de eventos nao adiciona coluna a conversations",
)

db.close()

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
