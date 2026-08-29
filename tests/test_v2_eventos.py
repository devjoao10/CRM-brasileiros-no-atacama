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

2. Duplicata levanta `EventoDuplicado`, NAO devolve a linha existente em
   silencio. Devolver seria conveniente para idempotencia, mas mascararia
   o caso em que dois eventos DIFERENTES colidem no mesmo id. Quem quer
   idempotencia (Fase 6, handoff) captura a excecao de proposito.

3. `payload` tem ALLOWLIST DE CHAVES. Nao existe chave para conteudo de
   mensagem, telefone, nome, e-mail ou token — logo nao ha como grava-los.
   A defesa e estrutural, nao heuristica: pelo mesmo motivo do
   DEPLOY-GATE-01, nao existe regex confiavel para "isto e sensivel".

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


from sqlalchemy import inspect  # noqa: E402

from app.database import IS_SQLITE, Base, SessionLocal, engine  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402
from app.v2.eventos import (  # noqa: E402
    CHAVES_PAYLOAD_PERMITIDAS,
    EventoDuplicado,
    PayloadInvalido,
    TipoEvento,
    registrar_evento,
)

Base.metadata.create_all(bind=engine)
db = SessionLocal()

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
    event_id="evt-completo-001",
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
    payload={"motivo": "triagem_completa"},
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
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, event_id="evt-completo-001")
except EventoDuplicado as exc:
    levantou = True
    capturado = exc
check(levantou, "6. event_id duplicado levanta EventoDuplicado")
check(
    capturado is not None and getattr(capturado, "event_id", None) == "evt-completo-001",
    "6b. a excecao carrega o event_id, para o caller decidir",
)

# --- 7. A sessao continua utilizavel depois da duplicata -----------------
# Se o rollback do SAVEPOINT nao for feito, a transacao fica abortada no
# PostgreSQL e TODA operacao seguinte da mesma sessao falha.
ev_pos = registrar_evento(db, tipo=TipoEvento.TRIAGE_COMPLETED, conversation_id=42)
check(ev_pos.id is not None, "7. sessao continua utilizavel apos EventoDuplicado")

# --- 8. Payload: allowlist de chaves -------------------------------------
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
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, payload={"motivo": "x" * 5000})
except PayloadInvalido:
    levantou = True
check(levantou, "10. valor acima do limite de tamanho levanta PayloadInvalido")

levantou = False
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, payload={"motivo": {"aninhado": True}})
except PayloadInvalido:
    levantou = True
check(levantou, "11. valor nao-escalar levanta PayloadInvalido (payload nao e deposito de contexto)")

ev_lista = registrar_evento(
    db, tipo=TipoEvento.TRIAGE_DATA_UPDATED, payload={"campos_faltantes": ["email", "duracao"]}
)
check(ev_lista.payload["campos_faltantes"] == ["email", "duracao"], "12. lista de strings e aceita")

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

# --- 15. V1 intacta: a tabela nova nao aparece no model da conversa ------
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
