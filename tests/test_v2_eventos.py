"""
BIA-V2 Fase 0 / Task 0.2 - tabela de eventos e `registrar_evento()`.

Objetivo do WP: conseguir reconstruir "o que aconteceu com esta conversa?"
sem abrir workflow nenhum. A auditoria mostrou que hoje isso e impossivel -
`triage_started_at`, `triage_completed_at`, `encerrada_at` e reabertura NAO
existem, e `queued_at` e APAGADO na primeira resposta humana, destruindo o
tempo de fila no instante em que ele se tornaria calculavel.

Cobre PERSISTENCIA/IDEMPOTENCIA: UNIQUE real no banco, `EventoDuplicado`,
SAVEPOINT/compensacao, semantica de `commit`. O CONTRATO por campo/payload
(tipos, allowlist, limites, formato, canonicalizacao de UUID) fica em
`tests/test_v2_eventos_validacao.py` - a mesma divisao dos dois
modulos-fonte (`eventos.py` / `eventos_validacao.py`).

DECISOES DE CONTRATO QUE ESTE TESTE TRAVA
-----------------------------------------
1. `event_id` tem UNIQUE de verdade no banco - nao "o codigo nao chama duas
   vezes". As docs do PostgreSQL sao explicitas: mesmo em Serializable a
   violacao ocorre sob concorrencia, e a constraint e a unica protecao real.
   `event_id` e canonicalizado (RFC 4122, minusculo com hifens) antes de
   persistir - duas grafias do MESMO uuid colidem no UNIQUE.

2. Duplicata levanta `EventoDuplicado`, NAO devolve a linha existente em
   silencio. Devolver seria conveniente para idempotencia, mas mascararia
   o caso em que dois eventos DIFERENTES colidem no mesmo id. Quem quer
   idempotencia (Fase 6, handoff) captura a excecao de proposito.

3. `commit` e keyword-only e SEM DEFAULT: toda chamada declara
   explicitamente se commita ou deixa a transacao com o caller.

Roda standalone:  python tests/test_v2_eventos.py
"""
import inspect as py_inspect
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
    EventoDuplicado,
    _persistir_ou_compensar,
    registrar_evento,
)
from app.v2.eventos_validacao import TipoEvento  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

# UUID fixo reutilizado pelos checks 4 e 5 (precisa ser o MESMO id para o
# check 5 provar duplicata).
EVENT_ID_TESTE = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"

# --- 1. Gravacao basica -----------------------------------------------------
ev = registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, commit=True, conversation_id=42, lead_id=7)
check(ev.id is not None, "1. evento e persistido e recebe id")
check(ev.event_id, "1b. event_id e gerado quando nao informado")
check(ev.event_type == "MESSAGE_RECEIVED", "1c. event_type gravado como string do enum")
check(ev.created_at is not None, "1d. created_at preenchido")

# --- 2. Evento sem conversa (o WP exige que seja aceito) -------------------
ev_solto = registrar_evento(db, tipo=TipoEvento.MESSAGE_IGNORED, commit=True)
check(ev_solto.conversation_id is None, "2. evento sem conversation_id e aceito")

# --- 3. UNIQUE REAL no banco, nao so checagem de aplicacao -----------------
indices = inspect(engine).get_indexes("conversation_events")
uniques = inspect(engine).get_unique_constraints("conversation_events")
tem_unique = any(
    ix.get("unique") and ix.get("column_names") == ["event_id"] for ix in indices
) or any(uc.get("column_names") == ["event_id"] for uc in uniques)
check(tem_unique, "3. event_id tem UNIQUE de verdade no schema do banco")

# --- 4. Gravacao com event_id explicito, para o check 5 duplicar ----------
registrar_evento(db, tipo=TipoEvento.HANDOFF_COMPLETED, commit=True, event_id=EVENT_ID_TESTE, conversation_id=42)

# --- 5. Duplicata levanta excecao tipada, nao devolve linha existente -----
levantou = False
capturado = None
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, commit=True, event_id=EVENT_ID_TESTE)
except EventoDuplicado as exc:
    levantou = True
    capturado = exc
check(levantou, "5. event_id duplicado levanta EventoDuplicado")
check(
    capturado is not None and getattr(capturado, "event_id", None) == EVENT_ID_TESTE,
    "5b. a excecao carrega o event_id, para o caller decidir",
)

# --- 6. A sessao continua utilizavel depois da duplicata -------------------
# Se o rollback do SAVEPOINT nao for feito, a transacao fica abortada no
# PostgreSQL e TODA operacao seguinte da mesma sessao falha.
ev_pos = registrar_evento(db, tipo=TipoEvento.TRIAGE_COMPLETED, commit=True, conversation_id=42)
check(ev_pos.id is not None, "6. sessao continua utilizavel apos EventoDuplicado")

# --- 7. Canonicalizacao de UUID: duas grafias colidem no MESMO event_id --
UUID_MAIUSCULO = "C2FFBD11-8D1C-4A12-9C3D-1234567890AB"
UUID_SEM_HIFEN = "c2ffbd118d1c4a129c3d1234567890ab"
ev_canonico = registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, commit=True, event_id=UUID_MAIUSCULO)
check(
    ev_canonico.event_id == "c2ffbd11-8d1c-4a12-9c3d-1234567890ab",
    f"7. event_id maiusculo e persistido na forma canonica (gravado: {ev_canonico.event_id!r})",
)
levantou = False
colidiu_com_canonico = False
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, commit=True, event_id=UUID_SEM_HIFEN)
except EventoDuplicado as exc:
    levantou = True
    colidiu_com_canonico = exc.event_id == "c2ffbd11-8d1c-4a12-9c3d-1234567890ab"
check(
    levantou and colidiu_com_canonico,
    "7b. mesma UUID sem hifen colide com a gravada maiuscula - prova que ambas canonicalizam igual",
)
# Fecha explicitamente a transacao que o `begin_nested()` acima autoiniciou
# (SQLAlchemy autobegin) e que o `except EventoDuplicado` NAO fecha sozinho -
# `db.in_transaction()` continua True apos o catch. Sem este rollback(), o
# check 9 abaixo (commit=False) herdaria uma transacao real ja aberta e
# MASCARARIA a divergencia SQLite do R12 (o SAVEPOINT passaria a nao ser
# "bare" por acidente de ordem dos testes, nao por correcao real) -
# descoberto empiricamente ao mover este teste de canonicalizacao pra cá.
db.rollback()

# --- 8. Payload rejeitado nao deixa lixo no banco --------------------------
antes = db.query(ConversationEvent).count()
try:
    registrar_evento(db, tipo=TipoEvento.MESSAGE_RECEIVED, commit=True, payload={"token": "abc"})
except Exception:  # noqa: BLE001 - so nos interessa que nada foi gravado
    pass
check(db.query(ConversationEvent).count() == antes, "8. payload invalido nao grava linha parcial")

# --- 9. commit=False deixa o controle da transacao com o caller -----------
# COMO isto e verificado, e por que nao pelo caminho obvio:
#
# O teste natural seria `commit=False` -> `db.rollback()` -> linha sumiu. Ele
# NAO funciona em SQLite, e nao por defeito do nosso codigo: o driver pysqlite
# opera em "legacy transaction mode" e nao emite BEGIN antes de um SAVEPOINT,
# entao o SAVEPOINT nasce fora de qualquer transacao e o ROLLBACK posterior nao
# tem o que desfazer. A doc oficial do SQLAlchemy diz isso com todas as letras:
# "SAVEPOINT statements emitted before a BEGIN fail to properly participate in
# the enclosing transaction" (dialects/sqlite.html). Em PostgreSQL - producao -
# o comportamento e correto. Ver .claude/memory/pysqlite-savepoint-rollback-gap.md
# e o risco R12 no plano.
#
# O contrato de `commit=False` e "NAO commitou; a transacao e sua". Isso se
# verifica de forma independente daquele bug: dado nao commitado e invisivel de
# outra conexao. E uma prova mais forte que o rollback, porque afere o efeito
# externo em vez do estado interno da sessao.
ev_sem_commit = registrar_evento(
    db, tipo=TipoEvento.AI_RESPONSE_DISCARDED, commit=False, conversation_id=42
)
check(ev_sem_commit.id is not None, "9. commit=False ainda faz flush e atribui id")

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
    # DIVERGENCIA DE DIALETO CONHECIDA - R12, nao defeito deste modulo.
    #
    # Em SQLite o `commit=False` NAO e honrado, e o mecanismo e pior do que
    # "o rollback nao desfaz": pysqlite nao emite BEGIN antes do SAVEPOINT,
    # entao o SAVEPOINT inicia sua PROPRIA transacao - e o RELEASE do savepoint
    # mais externo COMITA. A linha ja esta gravada quando `registrar_evento`
    # retorna, mesmo com commit=False.
    #
    # Os dois mecanismos sao incompativeis sem a receita oficial do SQLAlchemy
    # (isolation_level=None no connect + BEGIN manual no begin), que mexe em
    # `conversas/app/database.py` - infra compartilhada, mudanca que exige
    # aprovacao propria. Ver R12 no plano (Option A/B - PostgreSQL real
    # PREFERIDO, HOTFIX-09 nao e mais prerequisito automatico) e
    # .claude/memory/pysqlite-savepoint-rollback-gap.md.
    #
    # O teste trava a divergencia em vez de esconde-la: se alguem aplicar a
    # receita, ESTE ramo passa a falhar e obriga a revisitar o registro.
    check(
        visivel_sem_commit == 1,
        "9b. [SQLite] divergencia R12 travada: RELEASE do savepoint comita, commit=False nao vale",
    )
else:
    check(
        visivel_sem_commit == 0,
        "9b. commit=False NAO commita - linha invisivel de outra conexao",
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
check(visivel_pos_commit == 1, "9c. apos o commit do caller, a linha fica visivel")

# --- 10. IntegrityError que NAO e por event_id duplicado nao vira EventoDuplicado
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
    "10. IntegrityError por NOT NULL (event_type) nao vira EventoDuplicado - re-consulta prova que nao existe",
)

ev_pos_integrity = registrar_evento(db, tipo=TipoEvento.TRIAGE_COMPLETED, commit=True, conversation_id=42)
check(ev_pos_integrity.id is not None, "10b. sessao continua utilizavel apos IntegrityError nao-duplicado")

# --- 11. NOT NULL sem server_default: INSERT sem event_type FALHA ---------
# Documenta a excecao ao Global Constraint (ver plano, secao Global Constraints).
levantou_notnull = False
try:
    with db.begin_nested():
        db.add(ConversationEvent(event_id=str(uuid.uuid4()), event_type=None))
        db.flush()
except IntegrityError:
    levantou_notnull = True
check(levantou_notnull, "11. ConversationEvent sem event_type (NOT NULL, sem server_default) falha no INSERT")

# --- 12. commit e keyword-only e SEM DEFAULT -------------------------------
assinatura = py_inspect.signature(registrar_evento)
parametro_commit = assinatura.parameters["commit"]
check(parametro_commit.default is py_inspect.Parameter.empty, "12. commit nao tem default (decisao explicita obrigatoria)")
check(parametro_commit.kind is py_inspect.Parameter.KEYWORD_ONLY, "12b. commit e keyword-only")

db.close()

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
