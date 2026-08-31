"""
BIA-V2 Fase 0 / Task 0.2 — tabela de eventos e `registrar_evento()`.

Objetivo do WP: conseguir reconstruir "o que aconteceu com esta conversa?"
sem abrir workflow nenhum. A auditoria mostrou que hoje isso e impossivel —
`triage_started_at`, `triage_completed_at`, `encerrada_at` e reabertura NAO
existem, e `queued_at` e APAGADO na primeira resposta humana, destruindo o
tempo de fila no instante em que ele se tornaria calculavel.

DECISOES DE CONTRATO QUE ESTE TESTE TRAVA
-----------------------------------------
1. `event_id` tem UNIQUE de verdade no banco — nao "o codigo nao chama duas
   vezes". As docs do PostgreSQL sao explicitas: mesmo em Serializable a
   violacao ocorre sob concorrencia, e a constraint e a unica protecao real.
   Alem disso, `event_id` e validado como UUID de verdade (RFC 4122), nao
   "qualquer string de ate 36 caracteres".

2. Duplicata levanta `EventoDuplicado`, NAO devolve a linha existente em
   silencio. Devolver seria conveniente para idempotencia, mas mascararia
   o caso em que dois eventos DIFERENTES colidem no mesmo id. Quem quer
   idempotencia (Fase 6, handoff) captura a excecao de proposito.

3. `payload` tem ALLOWLIST DE CHAVES TIPADA (4 chaves: bool, lista de um
   vocabulario fixo, ou StrEnum). Nao existe chave para conteudo de
   mensagem, telefone, nome, e-mail ou token — logo nao ha como grava-los.
   A defesa e estrutural, nao heuristica.

4. Todo campo fora do payload (event_id, event_type, whatsapp_msg_id,
   state_before/after, action, model, result, error_code) e validado contra
   tamanho maximo e, quando aplicavel, formato de token tecnico — SINTATICO,
   nunca controle de PII (ver docstring de conversas/app/v2/eventos.py).

Roda standalone:  python tests/test_v2_eventos.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_eventos_test.db"
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


import uuid  # noqa: E402

from sqlalchemy import inspect  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.database import IS_SQLITE, Base, SessionLocal, engine  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402
from app.v2.eventos import (  # noqa: E402
    CAMPOS_TRIAGEM,
    CHAVES_PAYLOAD_PERMITIDAS,
    CampoInvalido,
    EventoDuplicado,
    MotivoPrefiltro,
    OrigemEvento,
    PayloadInvalido,
    ResultadoEvento,
    TipoEvento,
    _persistir_ou_compensar,
    registrar_evento,
)

Base.metadata.create_all(bind=engine)
db = SessionLocal()

# UUID fixo reutilizado pelos checks 3 e 6 (precisa ser o MESMO id para o
# check 6 provar duplicata). `event_id` agora exige UUID de verdade — ver
# secao 17 abaixo para a rejeicao de string nao-UUID.
EVENT_ID_TESTE = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"


def _tenta_registrar(tipo=TipoEvento.MESSAGE_RECEIVED, **kwargs):
    """Chama registrar_evento e devolve (ok, excecao) em vez de deixar propagar."""
    try:
        registrar_evento(db, tipo=tipo, **kwargs)
        return True, None
    except Exception as exc:  # noqa: BLE001 - queremos inspecionar QUALQUER tipo levantado
        return False, exc


# --- 1. Os 18 tipos exigidos pelo WP existem ------------------------------
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

# --- 2. Gravacao basica ---------------------------------------------------
ev = registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, conversation_id=42, lead_id=7)
check(ev.id is not None, "2. evento e persistido e recebe id")
check(ev.event_id, "2b. event_id e gerado quando nao informado")
check(ev.event_type == "MESSAGE_RECEIVED", "2c. event_type gravado como string do enum")
check(ev.created_at is not None, "2d. created_at preenchido")

# --- 3. Todos os campos do contrato do WP ---------------------------------
ev_completo = registrar_evento(
    db,
    tipo=TipoEvento.HANDOFF_COMPLETED,
    event_id=EVENT_ID_TESTE,
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
check(not faltando, f"3. todos os campos do contrato existem (faltam: {faltando})")
check(ev_completo.target_user_id == 5 and ev_completo.duration_ms == 1234, "3b. valores gravados corretamente")

# --- 4. Evento sem conversa (o WP exige que seja aceito) ------------------
ev_solto = registrar_evento(db, tipo=TipoEvento.MESSAGE_IGNORED)
check(ev_solto.conversation_id is None, "4. evento sem conversation_id e aceito")

# --- 5. UNIQUE REAL no banco, nao so checagem de aplicacao ----------------
indices = inspect(engine).get_indexes("conversation_events")
uniques = inspect(engine).get_unique_constraints("conversation_events")
tem_unique = any(
    ix.get("unique") and ix.get("column_names") == ["event_id"] for ix in indices
) or any(uc.get("column_names") == ["event_id"] for uc in uniques)
check(tem_unique, "5. event_id tem UNIQUE de verdade no schema do banco")

# --- 6. Duplicata levanta excecao tipada, nao devolve linha existente -----
levantou = False
capturado = None
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, event_id=EVENT_ID_TESTE)
except EventoDuplicado as exc:
    levantou = True
    capturado = exc
check(levantou, "6. event_id duplicado levanta EventoDuplicado")
check(
    capturado is not None and getattr(capturado, "event_id", None) == EVENT_ID_TESTE,
    "6b. a excecao carrega o event_id, para o caller decidir",
)

# --- 7. A sessao continua utilizavel depois da duplicata -----------------
# Se o rollback do SAVEPOINT nao for feito, a transacao fica abortada no
# PostgreSQL e TODA operacao seguinte da mesma sessao falha.
ev_pos = registrar_evento(db, tipo=TipoEvento.TRIAGE_COMPLETED, conversation_id=42)
check(ev_pos.id is not None, "7. sessao continua utilizavel apos EventoDuplicado")

# --- 8. Payload: allowlist de chaves tipada -------------------------------
check(len(CHAVES_PAYLOAD_PERMITIDAS) > 0, "8. existe allowlist de chaves de payload")
proibidas = {"content", "mensagem", "texto", "whatsapp", "telefone", "nome", "email", "token"}
check(
    not (proibidas & CHAVES_PAYLOAD_PERMITIDAS),
    f"8b. allowlist NAO contem chave de PII/segredo (intersecao: {proibidas & CHAVES_PAYLOAD_PERMITIDAS})",
)

levantou = False
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, payload={"content": "oi, quero viajar"})
except PayloadInvalido:
    levantou = True
check(levantou, "9. chave fora da allowlist levanta PayloadInvalido")

levantou = False
try:
    # 21 itens validos (repete o vocabulario de 8 palavras 3x, corta em 21) —
    # cada item individualmente valido, mas a LISTA excede o limite de 20.
    lista_grande_demais = (list(CAMPOS_TRIAGEM) * 3)[:21]
    registrar_evento(
        db, tipo=TipoEvento.TRIAGE_DATA_UPDATED,
        payload={"campos_faltantes": lista_grande_demais},
    )
except PayloadInvalido:
    levantou = True
check(levantou, "10. campos_faltantes acima do tamanho maximo de lista levanta PayloadInvalido")

levantou = False
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, payload={"explicit_human_request": {"aninhado": True}})
except PayloadInvalido:
    levantou = True
check(levantou, "11. valor de tipo errado (dict onde se espera bool) levanta PayloadInvalido")

ev_payload_completo = registrar_evento(
    db, tipo=TipoEvento.TRIAGE_DATA_UPDATED,
    payload={
        "campos_faltantes": ["email", "destinos"],
        "explicit_human_request": False,
        "prefiltro_motivo": MotivoPrefiltro.MENSAGEM_VAZIA,  # aceita membro do enum
        "origem": "webhook",                                  # aceita string exata
    },
)
check(
    ev_payload_completo.payload["campos_faltantes"] == ["email", "destinos"]
    and ev_payload_completo.payload["explicit_human_request"] is False
    and ev_payload_completo.payload["prefiltro_motivo"] == "mensagem_vazia"
    and ev_payload_completo.payload["origem"] == "webhook",
    "12. as 4 chaves tipadas aceitam valor valido (enum por membro OU por string exata)",
)

# --- 13. Payload rejeitado nao deixa lixo no banco -----------------------
antes = db.query(ConversationEvent).count()
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, payload={"token": "abc"})
except PayloadInvalido:
    pass
check(db.query(ConversationEvent).count() == antes, "13. payload invalido nao grava linha parcial")

# --- 14. commit=False deixa o controle da transacao com o caller ---------
# COMO isto e verificado, e por que nao pelo caminho obvio:
#
# O teste natural seria `commit=False` -> `db.rollback()` -> linha sumiu. Ele
# NAO funciona em SQLite, e nao por defeito do nosso codigo: o driver pysqlite
# opera em "legacy transaction mode" e nao emite BEGIN antes de um SAVEPOINT,
# entao o SAVEPOINT nasce fora de qualquer transacao e o ROLLBACK posterior nao
# tem o que desfazer. A doc oficial do SQLAlchemy diz isso com todas as letras:
# "SAVEPOINT statements emitted before a BEGIN fail to properly participate in
# the enclosing transaction" (dialects/sqlite.html). Em PostgreSQL — producao —
# o comportamento e correto. Ver .claude/memory/pysqlite-savepoint-rollback-gap.md
# e o risco R12 no plano.
#
# O contrato de `commit=False` e "NAO commitou; a transacao e sua". Isso se
# verifica de forma independente daquele bug: dado nao commitado e invisivel de
# outra conexao. E uma prova mais forte que o rollback, porque afere o efeito
# externo em vez do estado interno da sessao.
ev_sem_commit = registrar_evento(
    db, tipo=TipoEvento.AI_RESPONSE_DISCARDED, conversation_id=42, commit=False
)
check(ev_sem_commit.id is not None, "14. commit=False ainda faz flush e atribui id")

outra_sessao = SessionLocal()
try:
    visivel_sem_commit = (
        outra_sessao.query(ConversationEvent)
        .filter_by(event_type="AI_RESPONSE_DISCARDED")
        .count()
    )
finally:
    outra_sessao.close()

if IS_SQLITE:
    # DIVERGENCIA DE DIALETO CONHECIDA — R12, nao defeito deste modulo.
    #
    # Em SQLite o `commit=False` NAO e honrado, e o mecanismo e pior do que
    # "o rollback nao desfaz": pysqlite nao emite BEGIN antes do SAVEPOINT,
    # entao o SAVEPOINT inicia sua PROPRIA transacao — e o RELEASE do savepoint
    # mais externo COMITA. A linha ja esta gravada quando `registrar_evento`
    # retorna, mesmo com commit=False.
    #
    # Os dois mecanismos sao incompativeis sem a receita oficial do SQLAlchemy
    # (isolation_level=None no connect + BEGIN manual no begin), que mexe em
    # `conversas/app/database.py` — infra compartilhada, mudanca que exige
    # aprovacao propria. Ver R12 no plano e
    # .claude/memory/pysqlite-savepoint-rollback-gap.md.
    #
    # O teste trava a divergencia em vez de esconde-la: se alguem aplicar a
    # receita, ESTE ramo passa a falhar e obriga a revisitar o registro.
    check(
        visivel_sem_commit == 1,
        "14b. [SQLite] divergencia R12 travada: RELEASE do savepoint comita, commit=False nao vale",
    )
else:
    check(
        visivel_sem_commit == 0,
        "14b. commit=False NAO commita — linha invisivel de outra conexao",
    )

db.commit()
outra_sessao = SessionLocal()
try:
    visivel_pos_commit = (
        outra_sessao.query(ConversationEvent)
        .filter_by(event_type="AI_RESPONSE_DISCARDED")
        .count()
    )
finally:
    outra_sessao.close()
check(visivel_pos_commit == 1, "14c. apos o commit do caller, a linha fica visivel")

# --- 16. Toda coluna tem limite de tamanho validado em Python -------------
# Pelo menos 3 campos, no limite exato (passa) e limite+1 (levanta) — aqui 5.
CASOS_LIMITE = [
    ("whatsapp_msg_id", "w" * 100, "w" * 101),
    ("model", "m" * 64, "m" * 65),
    ("state_before", "A" * 32, "A" * 33),
    ("action", "a" * 64, "a" * 65),
    ("error_code", "A" * 64, "A" * 65),
]
for campo, valor_no_limite, valor_acima in CASOS_LIMITE:
    ok_no_limite, exc_no_limite = _tenta_registrar(**{campo: valor_no_limite})
    check(ok_no_limite, f"16. {campo} no limite exato ({len(valor_no_limite)} chars) passa (exc={exc_no_limite!r})")

    ok_acima, exc_acima = _tenta_registrar(**{campo: valor_acima})
    check(
        not ok_acima and isinstance(exc_acima, CampoInvalido) and exc_acima.campo == campo,
        f"16. {campo} acima do limite ({len(valor_acima)} chars) levanta CampoInvalido (exc={exc_acima!r})",
    )

# --- 17. event_id precisa ser UUID de verdade, nao qualquer string de 36 chars
ok, exc = _tenta_registrar(event_id="x" * 36)
check(
    not ok and isinstance(exc, CampoInvalido) and exc.campo == "event_id",
    f"17a. event_id com 36 chars que NAO e UUID e rejeitado (exc={exc!r})",
)

ev_uuid_valido = registrar_evento(
    db, tipo=TipoEvento.MESSAGE_RECEIVED, event_id="0f8fad5b-d9cb-469f-a165-70867728950e",
)
check(ev_uuid_valido.event_id == "0f8fad5b-d9cb-469f-a165-70867728950e", "17b. event_id UUID bem formado e aceito")

# --- 18. tipo aceita TipoEvento ou string valida; result e enum fechado --
ev_tipo_str = registrar_evento(db, tipo="MESSAGE_RECEIVED")
check(ev_tipo_str.event_type == "MESSAGE_RECEIVED", "18a. tipo aceita string valida e converte")

ok, exc = _tenta_registrar(tipo="NAO_EXISTE")
check(
    not ok and isinstance(exc, CampoInvalido) and not isinstance(exc, AttributeError),
    f"18b. tipo com string invalida levanta CampoInvalido, nunca AttributeError/ValueError cru (exc={exc!r})",
)

ok, exc = _tenta_registrar(tipo=123)
check(
    not ok and isinstance(exc, CampoInvalido) and not isinstance(exc, AttributeError),
    f"18c. tipo de tipo errado (int) levanta CampoInvalido, nunca AttributeError (exc={exc!r})",
)

ev_result_enum = registrar_evento(db, tipo=TipoEvento.MESSAGE_SENT, result=ResultadoEvento.FALHA)
check(ev_result_enum.result == "falha", "18d. result aceita membro do enum ResultadoEvento")

ok, exc = _tenta_registrar(result="parcial")
check(
    not ok and isinstance(exc, CampoInvalido) and exc.campo == "result",
    f"18e. result fora do vocabulario sucesso/falha/ignorado levanta CampoInvalido (exc={exc!r})",
)

# --- 19. action/error_code: alem de tamanho, o FORMATO tambem e exigido --
ok, exc = _tenta_registrar(action="Nao_Snake")
check(
    not ok and isinstance(exc, CampoInvalido) and exc.campo == "action",
    f"19a. action fora do formato ^[a-z][a-z0-9_]*$ e rejeitado (exc={exc!r})",
)
ok, exc = _tenta_registrar(error_code="minusculo")
check(
    not ok and isinstance(exc, CampoInvalido) and exc.campo == "error_code",
    f"19b. error_code fora do formato ^[A-Z][A-Z0-9_]*$ e rejeitado (exc={exc!r})",
)

# --- 20. Contrato tipado do payload rejeita PII/token/prosa nas 4 chaves --
# O regex de token e SINTATICO (ver docstring do modulo) — quem barra PII no
# payload e o vocabulario FECHADO destas 4 chaves, nao um filtro de conteudo.
VALORES_MALICIOSOS = [
    "5551999999999",                              # telefone
    "cliente@gmail.com",                          # e-mail
    "eyJhbGciOiJIUzI1NiJ9.abc",                   # token JWT
    "quero viajar em janeiro com minha esposa",   # frase de cliente
]


def _todos_rejeitados(payloads_ruins):
    rejeitados = 0
    for p in payloads_ruins:
        try:
            registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, payload=p)
        except PayloadInvalido:
            rejeitados += 1
    return rejeitados == len(payloads_ruins)


check(
    _todos_rejeitados([{"prefiltro_motivo": v} for v in VALORES_MALICIOSOS]),
    "20a. prefiltro_motivo rejeita telefone/e-mail/token/frase (vocabulario fechado)",
)
check(
    _todos_rejeitados([{"origem": v} for v in VALORES_MALICIOSOS]),
    "20b. origem rejeita telefone/e-mail/token/frase",
)
check(
    _todos_rejeitados([{"campos_faltantes": [v]} for v in VALORES_MALICIOSOS]),
    "20c. campos_faltantes rejeita item telefone/e-mail/token/frase",
)
check(
    _todos_rejeitados([{"explicit_human_request": v} for v in VALORES_MALICIOSOS]),
    "20d. explicit_human_request rejeita telefone/e-mail/token/frase (so aceita bool)",
)

for chave_removida in ("motivo", "intent", "tentativa"):
    levantou = False
    try:
        registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, payload={chave_removida: "qualquer coisa"})
    except PayloadInvalido:
        levantou = True
    check(levantou, f"20e. chave removida {chave_removida!r} e rejeitada como fora da allowlist")

# --- 21. json.dumps com int patologico vira PayloadInvalido, nao ValueError cru
levantou_tipado = False
vazou_cru = False
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, payload={"campos_faltantes": [10 ** 5000]})
except PayloadInvalido:
    levantou_tipado = True
except ValueError:
    vazou_cru = True
check(
    levantou_tipado and not vazou_cru,
    "21. int com milhares de digitos (sys.get_int_max_str_digits) vira PayloadInvalido, nao ValueError cru",
)

# --- 22. IntegrityError que NAO e por event_id duplicado nao vira EventoDuplicado
evento_sem_tipo = ConversationEvent(event_id=str(uuid.uuid4()), event_type=None, conversation_id=42)
levantou_integrity = False
levantou_duplicado_errado = False
try:
    _persistir_ou_compensar(db, evento_sem_tipo, evento_sem_tipo.event_id)
except EventoDuplicado:
    levantou_duplicado_errado = True
except IntegrityError:
    levantou_integrity = True
check(
    levantou_integrity and not levantou_duplicado_errado,
    "22. IntegrityError por NOT NULL (event_type) nao vira EventoDuplicado - re-consulta prova que nao existe",
)

ev_pos_integrity = registrar_evento(db, tipo=TipoEvento.TRIAGE_COMPLETED, conversation_id=42)
check(ev_pos_integrity.id is not None, "22b. sessao continua utilizavel apos IntegrityError nao-duplicado")

# --- 23. NOT NULL sem server_default: INSERT sem event_type FALHA --------
# Documenta a excecao ao Global Constraint (ver plano, secao Global Constraints).
levantou_notnull = False
try:
    with db.begin_nested():
        db.add(ConversationEvent(event_id=str(uuid.uuid4()), event_type=None))
        db.flush()
except IntegrityError:
    levantou_notnull = True
check(levantou_notnull, "23. ConversationEvent sem event_type (NOT NULL, sem server_default) falha no INSERT")

# --- 15. V1 intacta: a tabela nova nao aparece no model da conversa ------
# Import feito por ULTIMO de proposito: `Conversation.tags` referencia
# `ConversationTag`/`conversation_tag_links` por string, e este arquivo nunca
# importa esses modelos. Fora de ordem, o import registraria um mapper
# incompleto e QUALQUER `ConversationEvent(...)` construido depois dispararia
# `InvalidRequestError` ao tentar configurar TODOS os mappers pendentes —
# nao e falha deste modulo, e reflexo do registry compartilhado do SQLAlchemy.
from app.models.conversation import Conversation  # noqa: E402

check(
    not any("event" in c.name for c in Conversation.__table__.columns),
    "15. tabela de eventos nao adiciona coluna a conversations",
)

db.close()

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
